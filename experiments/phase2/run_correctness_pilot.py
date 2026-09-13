"""Run one motion-disabled Phase 2 control/treatment correctness pair."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

from experiments.phase1.carryover.observation import (
    ObservationError,
    ObservedProcessFactory,
    read_process_counters,
)
from experiments.phase1.common.manifest import (
    sha256_file,
    utc_now_iso,
    write_json_atomic,
)
from experiments.phase1.common.telemetry_jetson import (
    TegrastatsSampler,
    load_resource_samples,
    validate_resource_samples,
)
from experiments.phase1.formal.run import (
    FormalCondition,
    FormalRunSpec,
    run_formal_workload,
)
from experiments.phase1.run_carryover_session import (
    DEFAULT_ASR_INPUT,
    DEFAULT_VLM_INPUT,
)
from experiments.phase1.run_formal_session import ThermalMonitor
from experiments.phase1.workloads.asr.adapter import (
    FixedInputASRAdapter,
    fixed_asr_payload,
    load_phase0_asr_runtime,
)
from experiments.phase1.workloads.vlm.adapter import fixed_c100_payload
from experiments.phase1.workloads.vlm.process_adapter import ProcessIsolatedVLMAdapter
from experiments.phase2.correctness import (
    CORRECTNESS_PROTOCOL_ID,
    CORRECTNESS_PROTOCOL_SHA256,
    DEFAULT_PROTOCOL_PATH,
    canonical_protocol_text,
    load_protocol,
    protocol_sha256,
)
from experiments.phase2.evidence import (
    CONTROL_CONDITION,
    PREFETCH_CONDITION,
    build_unit_evidence,
)
from experiments.phase2.observations import (
    capture_boundary_observation,
    validate_process_observation,
)
from experiments.phase2.preflight import (
    build_correctness_preflight,
    correctness_preflight_errors,
)
from experiments.phase2.privacy import validate_privacy_boundary
from experiments.phase2.residency.prefetch import run_sequential_prefetch
from jetson.phase1_runtime import PeriodicProbe, RuntimeEvent


CORRECTNESS_PILOT_SCHEMA_VERSION = "0.1.0"
CORRECTNESS_UNIT_SCHEMA_VERSION = "0.1.0"
CORRECTNESS_INVOCATION_SCHEMA_VERSION = "0.1.0"
PHASE2_EVENT_SCHEMA_VERSION = "0.1.0"
DEFAULT_OUTPUT_ROOT = (
    Path(__file__).resolve().parents[1] / "runs" / "phase2-correctness-pilot"
)

_COLLECTION_RE = re.compile(r"^[0-9]{8}T[0-9]{6}Z_phase2_correctness_pilot_v1$")
_EVENT_ID_RE = re.compile(
    r"^[0-9]{8}T[0-9]{6}Z_phase2_pilot_(?:asr|vlm|unit)_[0-9]{3}$"
)
_TIMEOUTS = {
    "asr": {"validity": 180.0, "completion": 150.0, "join": 10.0},
    "vlm": {"validity": 900.0, "completion": 720.0, "join": 720.0},
}
_PRIVATE_RUNTIME_KEYS = {
    "pid",
    "process_id",
    "process_name",
    "thread_name",
}


class CorrectnessPilotError(RuntimeError):
    """The correctness pair cannot continue without invalid evidence."""


class PilotInfrastructureError(CorrectnessPilotError):
    """Telemetry or another required host facility failed."""


class PilotSafetyError(CorrectnessPilotError):
    """A no-motion or thermal boundary could not be preserved."""


class Phase2EventRecorder:
    """Record ordered Phase 2 events without process or thread identities."""

    def __init__(self, run_dir: Path | str, run_id: str) -> None:
        if not isinstance(run_id, str) or _EVENT_ID_RE.fullmatch(run_id) is None:
            raise ValueError("invalid Phase 2 event run id")
        self.run_id = run_id
        self.path = Path(run_dir) / "events.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            raise FileExistsError(f"refusing to overwrite event trace: {self.path}")
        self._stream: TextIO = self.path.open(
            "w", encoding="utf-8", buffering=64 * 1024, newline="\n"
        )
        self._sequence = 0
        self._last_monotonic_ns = 0
        self._closed = False
        self._lock = threading.Lock()

    def emit(self, event: RuntimeEvent) -> None:
        if not isinstance(event, RuntimeEvent):
            raise TypeError("event must be a RuntimeEvent")
        with self._lock:
            if self._closed:
                raise RuntimeError("event recorder is closed")
            monotonic_ns = time.monotonic_ns()
            if monotonic_ns < self._last_monotonic_ns:
                raise RuntimeError("event recorder monotonic time moved backwards")
            item: dict[str, object] = {
                "phase2_event_schema_version": PHASE2_EVENT_SCHEMA_VERSION,
                "run_id": self.run_id,
                "seq": self._sequence,
                "event": event.event,
                "component": event.component,
                "status": event.status.value,
                "monotonic_ns": monotonic_ns,
                "wall_time_ns": time.time_ns(),
                "details": _sanitize_record(dict(event.details)),
            }
            optional: dict[str, object | None] = {
                "task_id": event.task_id,
                "task_kind": (
                    event.task_kind.value if event.task_kind is not None else None
                ),
                "parent_task_id": event.parent_task_id,
                "source_monotonic_ns": event.source_monotonic_ns,
                "deadline_monotonic_ns": event.deadline_monotonic_ns,
                "state_scope_id": (
                    event.state_token.scope_id
                    if event.state_token is not None
                    else None
                ),
                "state_generation": (
                    event.state_token.generation
                    if event.state_token is not None
                    else None
                ),
            }
            item.update(
                {key: value for key, value in optional.items() if value is not None}
            )
            validate_privacy_boundary(item)
            self._stream.write(
                json.dumps(
                    item, ensure_ascii=False, allow_nan=False, separators=(",", ":")
                )
                + "\n"
            )
            self._sequence += 1
            self._last_monotonic_ns = monotonic_ns

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._stream.flush()
            self._stream.close()
            self._closed = True


class PrefetchProcessObserver:
    """Sample one already-owned prefetch child without retaining its identity."""

    def __init__(
        self,
        *,
        sample_interval_s: float = 0.01,
        reader: Callable[[int], Mapping[str, object]] = read_process_counters,
    ) -> None:
        if (
            isinstance(sample_interval_s, bool)
            or not isinstance(sample_interval_s, (int, float))
            or not math.isfinite(float(sample_interval_s))
            or sample_interval_s <= 0
        ):
            raise ValueError("sample_interval_s must be positive and finite")
        if not callable(reader):
            raise TypeError("reader must be callable")
        self._interval = float(sample_interval_s)
        self._reader = reader
        self._pid: int | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._samples: list[dict[str, object]] = []
        self._read_error_count = 0
        self._thread_error: str | None = None

    @property
    def started(self) -> bool:
        return self._pid is not None

    def _capture(self) -> None:
        assert self._pid is not None
        sample = dict(self._reader(self._pid))
        sample["observed_monotonic_ns"] = time.monotonic_ns()
        self._samples.append(sample)

    def start(self, pid: int) -> None:
        if self._pid is not None:
            raise ObservationError("prefetch process observer has already started")
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            raise ObservationError("prefetch child identity is invalid")
        self._pid = pid
        try:
            self._capture()
            self._thread = threading.Thread(
                target=self._sample,
                name="phase2-prefetch-process-observer",
                daemon=False,
            )
            self._thread.start()
        except BaseException:
            self._pid = None
            self._thread = None
            raise

    def _sample(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self._capture()
            except ObservationError:
                self._read_error_count += 1
            except BaseException as exc:
                self._thread_error = type(exc).__name__.lower()
                return

    def finish(self, *, join_timeout_s: float = 2.0) -> dict[str, object]:
        if self._pid is None or self._thread is None:
            raise ObservationError("prefetch process observer was not started")
        self._stop.set()
        self._thread.join(join_timeout_s)
        if self._thread.is_alive():
            raise ObservationError("prefetch process observer thread did not join")
        if self._thread_error is not None:
            raise ObservationError("prefetch process observer failed")
        if not self._samples:
            raise ObservationError("prefetch process observer collected no samples")
        last = self._samples[-1]
        report = {
            "observation_schema_version": "0.2.0",
            "method": "sampled_linux_proc",
            "sample_interval_ms": self._interval * 1000,
            "sample_count": len(self._samples),
            "read_error_count": self._read_error_count,
            "process_exit_observed": True,
            "user_time_s": last.get("user_time_s"),
            "system_time_s": last.get("system_time_s"),
            "minor_faults": last.get("minor_faults"),
            "major_faults": last.get("major_faults"),
            "maximum_rss_bytes": max(
                int(item.get("high_water_rss_bytes", 0)) for item in self._samples
            ),
            "voluntary_context_switches": last.get("voluntary_context_switches"),
            "involuntary_context_switches": last.get("involuntary_context_switches"),
            "pid_recorded": False,
            "command_recorded": False,
        }
        validate_process_observation(report)
        return report


def _sanitize_record(value: object) -> object:
    if isinstance(value, Mapping):
        return {
            str(key): _sanitize_record(child)
            for key, child in value.items()
            if str(key) not in _PRIVATE_RUNTIME_KEYS
        }
    if isinstance(value, list):
        return [_sanitize_record(item) for item in value]
    if isinstance(value, tuple):
        return [_sanitize_record(item) for item in value]
    return value


def make_collection_id(now: datetime | None = None) -> str:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return current.astimezone(timezone.utc).strftime(
        "%Y%m%dT%H%M%SZ_phase2_correctness_pilot_v1"
    )


def make_event_id(kind: str, ordinal: int, now: datetime | None = None) -> str:
    if kind not in {"asr", "vlm", "unit"}:
        raise ValueError("unsupported Phase 2 event kind")
    if (
        isinstance(ordinal, bool)
        or not isinstance(ordinal, int)
        or not 1 <= ordinal <= 999
    ):
        raise ValueError("event ordinal must be between 1 and 999")
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    stamp = current.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}_phase2_pilot_{kind}_{ordinal:03d}"


def _append_jsonl(path: Path, value: Mapping[str, object]) -> None:
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(
            json.dumps(
                dict(value),
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        )
        stream.flush()
        os.fsync(stream.fileno())


def _artifact_inventory(session_dir: Path) -> dict[str, object]:
    files = sorted(
        path
        for path in session_dir.rglob("*")
        if path.is_file()
        and path.name != "manifest.json"
        and not path.name.endswith(".tmp")
    )
    return {
        "count": len(files),
        "files": [
            {
                "relative_name": path.relative_to(session_dir).as_posix(),
                "size_bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
            for path in files
        ],
    }


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _resolve_output_root(output_root: Path, repo_root: Path) -> Path:
    expanded = output_root.expanduser()
    resolved = (expanded if expanded.is_absolute() else repo_root / expanded).resolve()
    if _is_relative_to(resolved, repo_root):
        ignored = (repo_root / "experiments" / "runs").resolve()
        if not _is_relative_to(resolved, ignored):
            raise CorrectnessPilotError(
                "repository-local pilot output must be inside experiments/runs"
            )
    return resolved


def _default_adapter(
    workload: str,
) -> tuple[object, ObservedProcessFactory | None]:
    if workload == "asr":
        observer = ObservedProcessFactory(sample_interval_s=0.01)
        return (
            FixedInputASRAdapter(
                process_factory=observer,
                execution_timeout_s=120.0,
                poll_interval_s=0.05,
                terminate_timeout_s=2.0,
                kill_timeout_s=2.0,
            ),
            observer,
        )
    if workload == "vlm":
        return (
            ProcessIsolatedVLMAdapter(
                execution_timeout_s=600.0,
                poll_interval_s=0.02,
                join_timeout_s=5.0,
                terminate_join_timeout_s=5.0,
            ),
            None,
        )
    raise ValueError("correctness pilot supports only ASR and VLM")


def _run_invocation(
    session_dir: Path,
    *,
    ordinal: int,
    unit_index: int,
    condition: str,
    workload: str,
    role: str,
    payloads: Mapping[str, object],
    thermal_monitor: ThermalMonitor,
    adapter_factory: Callable[
        [str], tuple[object, ObservedProcessFactory | None]
    ] = _default_adapter,
) -> tuple[Path, dict[str, object]]:
    del unit_index
    if workload not in _TIMEOUTS or role not in {
        "primer_1",
        "primer_2",
        "vlm",
        "measured_asr",
        "recovery_asr",
    }:
        raise CorrectnessPilotError("invocation identity is invalid")
    safe_condition = "control" if condition == CONTROL_CONDITION else "prefetch"
    run_dir = session_dir / "runs" / f"{ordinal:03d}-{workload}-{safe_condition}-{role}"
    if run_dir.exists():
        raise FileExistsError(f"refusing to overwrite pilot invocation: {run_dir}")
    run_dir.mkdir(parents=True)
    task_id = make_event_id(workload, ordinal)
    recorder = Phase2EventRecorder(run_dir, task_id)
    timeout = _TIMEOUTS[workload]
    spec = FormalRunSpec(
        workload=workload,
        condition=FormalCondition.SYNC,
        role="warmup" if role.startswith("primer") else "measured",
        prelude_s=0.001,
        postlude_s=0.001,
        result_validity_s=timeout["validity"],
        completion_timeout_s=timeout["completion"],
        join_timeout_s=timeout["join"],
        probe_period_ns=100_000_000,
        probe_deadline_ns=100_000_000,
    )
    adapter, observer = adapter_factory(workload)
    report: dict[str, object] | None = None
    process_observation: dict[str, object] | None = None
    failure: BaseException | None = None
    started_at = utc_now_iso()
    try:
        report = run_formal_workload(
            spec,
            payloads[workload],
            recorder,
            adapter,
            task_id=task_id,
            thermal_stop=thermal_monitor.stop_requested,
            task_protocol="phase2_correctness_pilot",
            state_scope_id="phase2-correctness-pilot",
        )
    except BaseException as exc:
        failure = exc
    finally:
        if observer is not None:
            try:
                process_observation = observer.finish()
            except ObservationError as exc:
                if failure is None:
                    failure = exc
        recorder.close()
    if failure is not None or report is None:
        write_json_atomic(
            run_dir / "run.json",
            {
                "correctness_invocation_schema_version": (
                    CORRECTNESS_INVOCATION_SCHEMA_VERSION
                ),
                "artifact_kind": "phase2_correctness_pilot_invocation",
                "condition": condition,
                "workload": workload,
                "pilot_role": role,
                "status": "failed",
                "failure_code": (
                    type(failure).__name__.lower() if failure else "missing_report"
                ),
                "started_at": started_at,
                "completed_at": utc_now_iso(),
                "formal_evidence": False,
            },
        )
        if failure is not None:
            raise failure
        raise CorrectnessPilotError("pilot invocation did not return a report")

    converted = {
        key: value
        for key, value in report.items()
        if key not in {"formal_run_schema_version", "condition", "role"}
    }
    record = {
        "correctness_invocation_schema_version": CORRECTNESS_INVOCATION_SCHEMA_VERSION,
        "artifact_kind": "phase2_correctness_pilot_invocation",
        "condition": condition,
        "pilot_role": role,
        **converted,
        "process_observation": process_observation,
        "started_at": started_at,
        "completed_at": utc_now_iso(),
        "formal_evidence": False,
        "application_slice_authorized": False,
        "raw_input_recorded": False,
        "raw_output_recorded": False,
    }
    sanitized = _sanitize_record(record)
    if not isinstance(sanitized, dict):
        raise AssertionError("sanitized invocation must be an object")
    validate_privacy_boundary(sanitized)
    write_json_atomic(run_dir / "run.json", sanitized)
    if sanitized.get("valid") is not True:
        raise CorrectnessPilotError("one or more invocation lifecycle checks failed")
    if workload == "asr" and process_observation is None:
        raise CorrectnessPilotError("ASR process observation is missing")
    return run_dir, sanitized


def _adapter_boundary(run: Mapping[str, object], name: str) -> int:
    adapter = run.get("adapter")
    value = adapter.get(name) if isinstance(adapter, Mapping) else None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise CorrectnessPilotError(f"invocation adapter boundary is invalid: {name}")
    return value


def _adapter_duration_ms(run: Mapping[str, object]) -> float:
    started = _adapter_boundary(run, "started_monotonic_ns")
    finished = _adapter_boundary(run, "finished_monotonic_ns")
    if finished <= started:
        raise CorrectnessPilotError("invocation duration is invalid")
    return (finished - started) / 1e6


def _validate_vlm_lifecycle(run: Mapping[str, object]) -> None:
    gates = run.get("gates")
    by_name = (
        {
            item.get("name"): item
            for item in gates
            if isinstance(item, Mapping) and isinstance(item.get("name"), str)
        }
        if isinstance(gates, list)
        else {}
    )
    required = {
        "child_process_reaped",
        "model_unload_claim_bounded",
        "residency_contract_verified",
    }
    if any(by_name.get(name, {}).get("passed") is not True for name in required):
        raise CorrectnessPilotError(
            "VLM unload or child lifecycle evidence is incomplete"
        )


def _probe_record(report: object, *, reference_ms: float) -> dict[str, object]:
    max_gap_ns = int(getattr(report, "max_gap_ns"))
    return {
        "implementation": "independent_thread",
        "joined": bool(getattr(report, "joined")),
        "tick_count": int(getattr(report, "tick_count")),
        "skipped_releases": int(getattr(report, "skipped_releases")),
        "deadline_miss_count": int(getattr(report, "deadline_miss_count")),
        "max_lateness_ns": int(getattr(report, "max_lateness_ns")),
        "max_gap_ns": max_gap_ns,
        "error_code": getattr(report, "error_code"),
        "responsiveness_reference_ms": reference_ms,
        "responsiveness_reference_met": max_gap_ns <= reference_ms * 1e6,
    }


def _check_thermal(monitor: ThermalMonitor) -> None:
    if monitor.stop_requested.is_set():
        raise PilotSafetyError("thermal stop threshold was reached")


def _run_observed_prefetch(
    model_path: Path,
    *,
    expected_size_bytes: int,
    buffer_size_bytes: int,
    timeout_s: float,
    stop_requested: Callable[[], bool],
    observer_factory: Callable[[], PrefetchProcessObserver] = PrefetchProcessObserver,
) -> tuple[object, dict[str, object]]:
    observer = observer_factory()
    try:
        action = run_sequential_prefetch(
            model_path,
            expected_size_bytes=expected_size_bytes,
            buffer_size_bytes=buffer_size_bytes,
            timeout_s=timeout_s,
            child_started_callback=observer.start,
            stop_requested=stop_requested,
        )
    finally:
        process = observer.finish() if observer.started else None
    if process is None:
        raise ObservationError("prefetch process observation is missing")
    return action, process


def _run_unit(
    session_dir: Path,
    *,
    unit_index: int,
    condition: str,
    ordinal_start: int,
    model_path: Path,
    expected_size_bytes: int,
    payloads: Mapping[str, object],
    parameters: Mapping[str, object],
    thermal_monitor: ThermalMonitor,
    sampler: object,
    resource_path: Path,
    invocation_runner: Callable[..., tuple[Path, dict[str, object]]],
    observation_reader: Callable[..., object],
    treatment_runner: Callable[..., tuple[object, Mapping[str, object]]],
    resource_reader: Callable[[Path | str], Sequence[Mapping[str, Any]]],
    probe_factory: Callable[..., object],
) -> tuple[dict[str, object], int]:
    label = "control" if condition == CONTROL_CONDITION else "prefetch"
    unit_dir = session_dir / "units" / f"unit-{unit_index:02d}-{label}"
    unit_dir.mkdir(parents=True, exist_ok=False)
    recorder = Phase2EventRecorder(unit_dir, make_event_id("unit", unit_index))
    probe = probe_factory(
        period_ns=int(parameters["probe_period_ms"] * 1_000_000),
        deadline_ns=int(parameters["probe_deadline_ms"] * 1_000_000),
        event_sink=recorder,
        thread_name=f"phase2-correctness-unit-{unit_index:02d}-probe",
    )
    completed_steps: list[str] = []
    observations: dict[str, object] = {}
    invocations: dict[str, str] = {}
    action: object | None = None
    action_process: Mapping[str, object] | None = None
    evidence: dict[str, object] | None = None
    probe_result: dict[str, object] | None = None
    failure: BaseException | None = None
    ordinal = ordinal_start
    try:
        probe.start()
        _check_thermal(thermal_monitor)
        observations["pre_unit"] = observation_reader(
            model_path,
            boundary="pre_unit",
            expected_size_bytes=expected_size_bytes,
        ).to_dict()
        completed_steps.append("pre_unit_observation")

        runs: dict[str, dict[str, object]] = {}
        for role in ("primer_1", "primer_2"):
            ordinal += 1
            run_dir, run = invocation_runner(
                session_dir,
                ordinal=ordinal,
                unit_index=unit_index,
                condition=condition,
                workload="asr",
                role=role,
                payloads=payloads,
                thermal_monitor=thermal_monitor,
            )
            runs[role] = run
            invocations[role] = run_dir.relative_to(session_dir).as_posix()
            completed_steps.append(f"asr_{role}")
            _check_thermal(thermal_monitor)
        observations["post_primer"] = observation_reader(
            model_path,
            boundary="post_primer",
            expected_size_bytes=expected_size_bytes,
        ).to_dict()
        completed_steps.append("post_primer_observation")

        ordinal += 1
        run_dir, vlm = invocation_runner(
            session_dir,
            ordinal=ordinal,
            unit_index=unit_index,
            condition=condition,
            workload="vlm",
            role="vlm",
            payloads=payloads,
            thermal_monitor=thermal_monitor,
        )
        _validate_vlm_lifecycle(vlm)
        invocations["vlm"] = run_dir.relative_to(session_dir).as_posix()
        completed_steps.extend(["frozen_vlm", "confirmed_vlm_unload_and_child_reap"])
        _check_thermal(thermal_monitor)

        common = observation_reader(
            model_path,
            boundary="post_vlm",
            expected_size_bytes=expected_size_bytes,
        ).to_dict()
        observations["post_vlm"] = common
        completed_steps.append("common_post_vlm_observation")

        verified: Mapping[str, object] | None = None
        if condition == PREFETCH_CONDITION:
            action, action_process = treatment_runner(
                model_path,
                expected_size_bytes=expected_size_bytes,
                buffer_size_bytes=int(parameters["prefetch_buffer_size_bytes"]),
                timeout_s=float(parameters["prefetch_timeout_s"]),
                stop_requested=thermal_monitor.stop_requested.is_set,
            )
            completed_steps.append("condition_action")
            _check_thermal(thermal_monitor)
            verified = observation_reader(
                model_path,
                boundary="post_action",
                expected_size_bytes=expected_size_bytes,
            ).to_dict()
            observations["post_action"] = verified
        else:
            completed_steps.append("condition_action")

        ordinal += 1
        run_dir, measured = invocation_runner(
            session_dir,
            ordinal=ordinal,
            unit_index=unit_index,
            condition=condition,
            workload="asr",
            role="measured_asr",
            payloads=payloads,
            thermal_monitor=thermal_monitor,
        )
        invocations["measured_asr"] = run_dir.relative_to(session_dir).as_posix()
        completed_steps.append("measured_asr")
        _check_thermal(thermal_monitor)
        result_available = _adapter_boundary(measured, "finished_monotonic_ns")
        try:
            sampler.wait_for_sample_at_or_after(result_available, timeout_s=2.0)
        except (RuntimeError, TimeoutError) as exc:
            raise PilotInfrastructureError(
                "resource sampler did not cover the measured ASR boundary"
            ) from exc
        observations["post_asr"] = observation_reader(
            model_path,
            boundary="post_asr",
            expected_size_bytes=expected_size_bytes,
        ).to_dict()
        completed_steps.append("post_asr_observation")

        ordinal += 1
        run_dir, recovery = invocation_runner(
            session_dir,
            ordinal=ordinal,
            unit_index=unit_index,
            condition=condition,
            workload="asr",
            role="recovery_asr",
            payloads=payloads,
            thermal_monitor=thermal_monitor,
        )
        invocations["recovery_asr"] = run_dir.relative_to(session_dir).as_posix()
        completed_steps.append("recovery_asr")
        _check_thermal(thermal_monitor)

        asr_process = measured.get("process_observation")
        if not isinstance(asr_process, Mapping):
            raise CorrectnessPilotError("measured ASR process evidence is missing")
        resources = resource_reader(resource_path)
        evidence = build_unit_evidence(
            condition=condition,
            common_post_vlm_observation=common,
            verified_post_action_observation=verified,
            prefetch_action=action,
            prefetch_process_observation=action_process,
            asr_started_monotonic_ns=_adapter_boundary(
                measured, "started_monotonic_ns"
            ),
            asr_result_available_monotonic_ns=result_available,
            asr_process_observation=asr_process,
            resource_samples=resources,
            maximum_resource_gap_ns=int(
                parameters["maximum_resource_gap_ms"] * 1_000_000
            ),
        )
        write_json_atomic(unit_dir / "evidence.json", evidence)
    except BaseException as exc:
        failure = exc
    finally:
        try:
            report = probe.stop(
                join_timeout_s=float(parameters["probe_join_timeout_s"])
            )
            probe_result = _probe_record(
                report,
                reference_ms=float(parameters["responsiveness_p95_reference_ms"]),
            )
            if (
                probe_result["joined"] is not True
                or probe_result["error_code"] is not None
                or probe_result["tick_count"] < 2
            ) and failure is None:
                failure = CorrectnessPilotError(
                    "unit responsiveness probe did not close successfully"
                )
        except BaseException as exc:
            if failure is None:
                failure = exc
        recorder.close()

    if failure is not None or evidence is None:
        partial = {
            "correctness_unit_schema_version": CORRECTNESS_UNIT_SCHEMA_VERSION,
            "artifact_kind": "phase2_correctness_pilot_unit",
            "unit_index": unit_index,
            "condition": condition,
            "status": "aborted",
            "failure_code": (
                type(failure).__name__.lower() if failure else "missing_evidence"
            ),
            "completed_steps": completed_steps,
            "observations": observations,
            "invocations": invocations,
            "prefetch_action": (
                action.to_dict()
                if callable(getattr(action, "to_dict", None))
                else action
            ),
            "prefetch_process_observation": action_process,
            "probe": probe_result,
            "formal_evidence": False,
            "application_slice_authorized": False,
        }
        validate_privacy_boundary(partial)
        write_json_atomic(unit_dir / "unit.json", partial)
        if failure is not None:
            raise failure
        raise CorrectnessPilotError("unit evidence is missing")

    primer_2_ms = _adapter_duration_ms(runs["primer_2"])
    unit = {
        "correctness_unit_schema_version": CORRECTNESS_UNIT_SCHEMA_VERSION,
        "artifact_kind": "phase2_correctness_pilot_unit",
        "unit_index": unit_index,
        "condition": condition,
        "status": "completed",
        "completed_steps": completed_steps,
        "observations": observations,
        "invocations": invocations,
        "primer_2_duration_ms": primer_2_ms,
        "measured_asr_duration_ms": _adapter_duration_ms(measured),
        "recovery_asr_duration_ms": _adapter_duration_ms(recovery),
        "evidence_ref": "evidence.json",
        "probe": probe_result,
        "formal_evidence": False,
        "application_slice_authorized": False,
    }
    validate_privacy_boundary(unit)
    write_json_atomic(unit_dir / "unit.json", unit)
    return unit, ordinal


def _failure_class(exc: BaseException) -> str:
    if isinstance(exc, PilotInfrastructureError):
        return "infrastructure"
    if isinstance(exc, PilotSafetyError):
        return "safety"
    return "pilot_execution"


def run_pilot(
    args: argparse.Namespace,
    *,
    repo_root: Path | str | None = None,
    preflight_builder: Callable[..., dict[str, object]] = build_correctness_preflight,
    sampler_factory: Callable[..., object] = TegrastatsSampler,
    invocation_runner: Callable[..., tuple[Path, dict[str, object]]] = _run_invocation,
    observation_reader: Callable[..., object] = capture_boundary_observation,
    treatment_runner: Callable[..., tuple[object, Mapping[str, object]]] = (
        _run_observed_prefetch
    ),
    resource_reader: Callable[[Path | str], Sequence[Mapping[str, Any]]] = (
        load_resource_samples
    ),
    probe_factory: Callable[..., object] = PeriodicProbe,
    thermal_monitor_factory: Callable[..., ThermalMonitor] = ThermalMonitor,
    payloads_override: Mapping[str, object] | None = None,
    whisper_model_path: Path | str | None = None,
) -> Path:
    """Execute one nonformal correctness pair and close every owned lifecycle."""

    root = (
        Path(repo_root).resolve()
        if repo_root is not None
        else Path(__file__).resolve().parents[2]
    )
    protocol_path = Path(args.protocol).resolve()
    protocol = load_protocol(protocol_path)
    if protocol_sha256(protocol) != CORRECTNESS_PROTOCOL_SHA256:
        raise CorrectnessPilotError("correctness protocol identity failed")
    collection_id = args.collection_id or make_collection_id()
    if _COLLECTION_RE.fullmatch(collection_id) is None:
        raise CorrectnessPilotError("collection id does not match the pilot contract")
    output_root = _resolve_output_root(Path(args.output_root), root)
    session_dir = output_root / collection_id
    if session_dir.exists():
        raise FileExistsError(f"refusing to overwrite correctness pilot: {session_dir}")

    preflight = preflight_builder(
        root,
        protocol,
        asr_input=args.asr_input,
        vlm_input=args.vlm_input,
        services_restarted=args.confirm_services_restarted,
        dynamic_dvfs_confirmed=args.confirm_dynamic_dvfs,
        protocol_path=protocol_path,
    )
    errors = correctness_preflight_errors(preflight)
    if errors:
        raise CorrectnessPilotError(
            "correctness preflight failed: " + "; ".join(errors)
        )
    validate_privacy_boundary(preflight)

    model_path = (
        Path(whisper_model_path or load_phase0_asr_runtime().whisper_model)
        .expanduser()
        .resolve()
    )
    whisper = preflight.get("whisper_file")
    expected_size = whisper.get("size_bytes") if isinstance(whisper, Mapping) else None
    if (
        isinstance(expected_size, bool)
        or not isinstance(expected_size, int)
        or expected_size <= 0
    ):
        raise CorrectnessPilotError("preflight Whisper size is invalid")
    payloads = dict(
        payloads_override
        or {
            "asr": fixed_asr_payload(args.asr_input),
            "vlm": fixed_c100_payload(args.vlm_input),
        }
    )
    if set(payloads) != {"asr", "vlm"}:
        raise CorrectnessPilotError("fixed payloads must cover ASR and VLM")

    parameters = protocol.get("parameters")
    conditions = protocol.get("conditions")
    if not isinstance(parameters, Mapping) or conditions != [
        CONTROL_CONDITION,
        PREFETCH_CONDITION,
    ]:
        raise CorrectnessPilotError("pilot schedule is invalid")
    injected = (
        any(
            value is not default
            for value, default in (
                (preflight_builder, build_correctness_preflight),
                (sampler_factory, TegrastatsSampler),
                (invocation_runner, _run_invocation),
                (observation_reader, capture_boundary_observation),
                (treatment_runner, _run_observed_prefetch),
                (resource_reader, load_resource_samples),
                (probe_factory, PeriodicProbe),
                (thermal_monitor_factory, ThermalMonitor),
            )
        )
        or payloads_override is not None
        or whisper_model_path is not None
    )

    session_dir.mkdir(parents=True)
    (session_dir / "protocol.json").write_text(
        canonical_protocol_text(protocol), encoding="utf-8", newline="\n"
    )
    write_json_atomic(session_dir / "preflight.json", preflight)
    ledger_path = session_dir / "ledger.jsonl"
    ledger_path.touch(exist_ok=False)
    manifest_path = session_dir / "manifest.json"
    manifest: dict[str, object] = {
        "correctness_pilot_schema_version": CORRECTNESS_PILOT_SCHEMA_VERSION,
        "artifact_kind": "phase2_correctness_pilot_session",
        "collection_id": collection_id,
        "protocol_id": CORRECTNESS_PROTOCOL_ID,
        "protocol_sha256": CORRECTNESS_PROTOCOL_SHA256,
        "conditions": list(conditions),
        "status": "running",
        "failure_class": None,
        "failure_code": None,
        "created_at": utc_now_iso(),
        "completed_at": None,
        "formal_evidence": False,
        "application_slice_authorized": False,
        "development_injection": injected,
        "preflight": {
            "protocol_commit": preflight["protocol"]["protocol_commit"],
            "runner_commit": preflight["protocol"]["runner_commit"],
        },
        "thermal": {
            "readiness": None,
            "stop_tj_c": parameters["thermal_stop_tj_c"],
            "stop_requested": False,
        },
        "resource_sampler_report": None,
        "completed_units": 0,
        "artifacts": {},
    }
    write_json_atomic(manifest_path, manifest)

    monitor = thermal_monitor_factory(stop_tj_c=float(parameters["thermal_stop_tj_c"]))
    sampler: object | None = None
    ordinal = 0
    try:
        try:
            sampler = sampler_factory(
                session_dir,
                int(parameters["resource_interval_ms"]),
                sample_callback=monitor.observe,
            )
            sampler.start(first_sample_timeout_s=5.0)
        except BaseException as exc:
            raise PilotInfrastructureError("resource sampler could not start") from exc
        try:
            manifest["thermal"]["readiness"] = monitor.wait_below(
                maximum_tj_c=float(parameters["thermal_start_maximum_tj_c"]),
                consecutive_samples=int(
                    parameters["thermal_start_consecutive_samples"]
                ),
                timeout_s=args.thermal_wait_timeout_s,
            )
        except BaseException as exc:
            raise PilotSafetyError("thermal start requirement was not met") from exc
        write_json_atomic(manifest_path, manifest)

        for unit_index, condition in enumerate(conditions, start=1):
            _append_jsonl(
                ledger_path,
                {
                    "event": "unit_started",
                    "at": utc_now_iso(),
                    "unit_index": unit_index,
                    "condition": condition,
                },
            )
            unit, ordinal = _run_unit(
                session_dir,
                unit_index=unit_index,
                condition=condition,
                ordinal_start=ordinal,
                model_path=model_path,
                expected_size_bytes=expected_size,
                payloads=payloads,
                parameters=parameters,
                thermal_monitor=monitor,
                sampler=sampler,
                resource_path=session_dir / "resources.jsonl",
                invocation_runner=invocation_runner,
                observation_reader=observation_reader,
                treatment_runner=treatment_runner,
                resource_reader=resource_reader,
                probe_factory=probe_factory,
            )
            _append_jsonl(
                ledger_path,
                {
                    "event": "unit_completed",
                    "at": utc_now_iso(),
                    "unit_index": unit_index,
                    "condition": condition,
                    "status": unit["status"],
                },
            )
            manifest["completed_units"] = unit_index
            write_json_atomic(manifest_path, manifest)

        try:
            sampler.wait_for_sample_at_or_after(time.monotonic_ns(), timeout_s=2.0)
            stop_report = sampler.stop()
        except BaseException as exc:
            raise PilotInfrastructureError("resource sampler could not close") from exc
        manifest["resource_sampler_report"] = stop_report.to_dict()
        if not stop_report.successful:
            raise PilotInfrastructureError("resource sampler closure was incomplete")
        resource_errors = validate_resource_samples(
            resource_reader(session_dir / "resources.jsonl")
        )
        if resource_errors:
            raise PilotInfrastructureError("resource trace failed validation")
        _check_thermal(monitor)

        manifest["thermal"]["stop_requested"] = False
        manifest["status"] = "completed"
        manifest["completed_at"] = utc_now_iso()
        manifest["artifacts"] = _artifact_inventory(session_dir)
        validate_privacy_boundary(manifest)
        write_json_atomic(manifest_path, manifest)
    except BaseException as exc:
        if sampler is not None and getattr(sampler, "is_running", False):
            try:
                manifest["resource_sampler_report"] = sampler.stop().to_dict()
            except BaseException:
                pass
        manifest["thermal"]["stop_requested"] = monitor.stop_requested.is_set()
        manifest["status"] = "aborted"
        manifest["failure_class"] = _failure_class(exc)
        manifest["failure_code"] = type(exc).__name__.lower()
        manifest["completed_at"] = utc_now_iso()
        manifest["artifacts"] = _artifact_inventory(session_dir)
        validate_privacy_boundary(manifest)
        write_json_atomic(manifest_path, manifest)
        raise
    return session_dir


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--collection-id")
    parser.add_argument("--protocol", type=Path, default=DEFAULT_PROTOCOL_PATH)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--asr-input", type=Path, default=DEFAULT_ASR_INPUT)
    parser.add_argument("--vlm-input", type=Path, default=DEFAULT_VLM_INPUT)
    parser.add_argument("--confirm-services-restarted", action="store_true")
    parser.add_argument("--confirm-dynamic-dvfs", action="store_true")
    parser.add_argument("--thermal-wait-timeout-s", type=float, default=900.0)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        session_dir = run_pilot(args)
    except BaseException as exc:
        if isinstance(exc, KeyboardInterrupt):
            print("Phase 2 correctness pilot interrupted", file=sys.stderr)
        else:
            print(
                f"Phase 2 correctness pilot failed: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
        return 1
    print(session_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
