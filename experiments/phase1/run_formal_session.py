"""Run one complete session from the activated Phase 1 G6 protocol."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping, Sequence

from experiments.phase1.asr_adapter import FixedInputASRAdapter, fixed_asr_payload
from experiments.phase1.formal_preflight import (
    FROZEN_PROTOCOL_SHA256,
    build_formal_preflight,
    formal_preflight_errors,
)
from experiments.phase1.formal_protocol import (
    DEFAULT_PROTOCOL_PATH,
    FORMAL_COLLECTION_STATUS,
    FORMAL_PROTOCOL_ID,
    canonical_protocol_text,
    formal_protocol_errors,
    load_formal_protocol,
    protocol_sha256,
)
from experiments.phase1.formal_run import (
    FORMAL_RUN_SCHEMA_VERSION,
    FormalCondition,
    FormalRunSpec,
    run_formal_workload,
)
from experiments.phase1.jetson_telemetry import (
    TegrastatsSampler,
    load_resource_samples,
    validate_resource_samples,
)
from experiments.phase1.llm_adapter import FixedInputLLMAdapter, fixed_llm_payload
from experiments.phase1.manifest import (
    MANIFEST_SCHEMA_VERSION,
    sha256_file,
    utc_now_iso,
    write_json_atomic,
)
from experiments.phase1.telemetry import EventRecorder, SCHEMA_VERSION
from experiments.phase1.vlm_adapter import fixed_c100_payload
from experiments.phase1.vlm_process_adapter import ProcessIsolatedVLMAdapter
from jetson.phase1_runtime import EventStatus, PeriodicProbe, RuntimeEvent


FORMAL_SESSION_SCHEMA_VERSION = "0.1.0"
DEFAULT_OUTPUT_ROOT = Path(__file__).resolve().parents[1] / "runs" / "phase1-formal"
DEFAULT_ASR_INPUT = (
    Path(__file__).resolve().parents[1]
    / "raw"
    / "phase0-inputs"
    / "asr"
    / "asr_piper_clean_16k.wav"
)
DEFAULT_LLM_INPUT = (
    Path(__file__).resolve().parents[1] / "phase0" / "inputs" / "llm_prompt_zh.txt"
)
DEFAULT_VLM_INPUT = (
    Path(__file__).resolve().parents[1]
    / "raw"
    / "phase0-inputs"
    / "vlm"
    / "c100-camera-product.jpg"
)
_COLLECTION_ID_RE = re.compile(
    r"^[0-9]{8}T[0-9]{6}Z_phase1_formal_[a-z][a-z0-9_-]{0,23}$"
)
_SESSION_NAME_RE = re.compile(
    r"^session-(?P<index>0[1-5])-attempt-(?P<attempt>[0-9]{2})$"
)
_TIMEOUTS = {
    "asr": {"completion": 150.0, "validity": 180.0, "join": 10.0},
    "llm": {"completion": 150.0, "validity": 180.0, "join": 130.0},
    "vlm": {"completion": 720.0, "validity": 900.0, "join": 720.0},
}
_INFRASTRUCTURE_FAILURES = (
    "host_power_loss",
    "device_reboot",
    "unrecoverable_model_service_crash",
    "resource_sampler_failure",
)


class FormalSessionError(RuntimeError):
    """A formal session could not continue without violating the protocol."""


class ThermalMonitor:
    """Track preregistered Tj thresholds from the continuous sampler."""

    def __init__(self, *, stop_tj_c: float, history_size: int = 128) -> None:
        if not math.isfinite(stop_tj_c) or stop_tj_c <= 0:
            raise ValueError("stop_tj_c must be positive and finite")
        if isinstance(history_size, bool) or not isinstance(history_size, int):
            raise TypeError("history_size must be an integer")
        if history_size < 10:
            raise ValueError("history_size must be at least 10")
        self.stop_tj_c = float(stop_tj_c)
        self.stop_requested = threading.Event()
        self._samples: deque[dict[str, int | float]] = deque(maxlen=history_size)
        self._condition = threading.Condition()
        self._error_code: str | None = None

    def observe(self, sample: Mapping[str, object]) -> None:
        temperatures = sample.get("temperatures_c")
        temperature_record = temperatures if isinstance(temperatures, Mapping) else {}
        tj = temperature_record.get("tj")
        sequence = sample.get("seq")
        parse_errors = sample.get("parse_errors")
        with self._condition:
            if parse_errors:
                self._error_code = "resource_parse_error"
                self.stop_requested.set()
            elif (
                isinstance(sequence, bool)
                or not isinstance(sequence, int)
                or isinstance(tj, bool)
                or not isinstance(tj, (int, float))
                or not math.isfinite(tj)
            ):
                self._error_code = "tj_sample_invalid"
                self.stop_requested.set()
            else:
                item = {"seq": sequence, "tj_c": float(tj)}
                self._samples.append(item)
                if float(tj) >= self.stop_tj_c:
                    self.stop_requested.set()
            self._condition.notify_all()

    @property
    def error_code(self) -> str | None:
        with self._condition:
            return self._error_code

    def latest_sequence(self) -> int | None:
        with self._condition:
            return int(self._samples[-1]["seq"]) if self._samples else None

    def wait_below(
        self,
        *,
        maximum_tj_c: float,
        consecutive_samples: int,
        timeout_s: float,
        after_sequence: int | None = None,
    ) -> dict[str, object]:
        if not math.isfinite(maximum_tj_c):
            raise ValueError("maximum_tj_c must be finite")
        if (
            isinstance(consecutive_samples, bool)
            or not isinstance(consecutive_samples, int)
            or consecutive_samples <= 0
        ):
            raise ValueError("consecutive_samples must be a positive integer")
        deadline = time.monotonic() + timeout_s
        with self._condition:
            while True:
                selected = [
                    sample
                    for sample in self._samples
                    if after_sequence is None or sample["seq"] > after_sequence
                ]
                tail = selected[-consecutive_samples:]
                if len(tail) == consecutive_samples and all(
                    sample["tj_c"] <= maximum_tj_c for sample in tail
                ):
                    return {
                        "maximum_tj_c": maximum_tj_c,
                        "consecutive_samples": consecutive_samples,
                        "first_sequence": tail[0]["seq"],
                        "last_sequence": tail[-1]["seq"],
                        "observed_tj_c": [sample["tj_c"] for sample in tail],
                    }
                if self._error_code is not None:
                    raise FormalSessionError(
                        f"thermal monitoring failed: {self._error_code}"
                    )
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        "timed out waiting for the preregistered thermal gate"
                    )
                self._condition.wait(min(remaining, 0.2))


def make_collection_id(now: datetime | None = None) -> str:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return current.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ_phase1_formal_g6")


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
            raise FormalSessionError(
                "repository-local formal output must be inside experiments/runs"
            )
    return resolved


def _append_jsonl(path: Path, value: Mapping[str, object]) -> None:
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(
            json.dumps(
                dict(value),
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            )
            + "\n"
        )
        stream.flush()
        os.fsync(stream.fileno())


def _artifact_identity(path: Path) -> dict[str, object]:
    return {"size_bytes": path.stat().st_size, "sha256": sha256_file(path)}


def _artifact_inventory(session_dir: Path) -> dict[str, object]:
    return {
        path.relative_to(session_dir).as_posix(): _artifact_identity(path)
        for path in sorted(item for item in session_dir.rglob("*") if item.is_file())
        if path.name != "manifest.json"
    }


def _parse_time(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("timestamp is missing")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def _prior_manifests(collection_dir: Path) -> list[tuple[Path, dict[str, object]]]:
    result: list[tuple[Path, dict[str, object]]] = []
    if not collection_dir.is_dir():
        return result
    for path in sorted(collection_dir.glob("session-*-attempt-*/manifest.json")):
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise FormalSessionError(f"invalid prior manifest: {path}")
        result.append((path, value))
    return result


def _check_collection_order(
    collection_dir: Path,
    *,
    session_index: int,
    attempt: int,
    minimum_separation_minutes: int,
    now: datetime,
    replacement_for: str | None,
    infrastructure_failure: str | None,
) -> dict[str, object] | None:
    manifests = _prior_manifests(collection_dir)
    completed: dict[int, list[tuple[Path, dict[str, object]]]] = {}
    for path, manifest in manifests:
        protocol_session = manifest.get("protocol_session")
        if not isinstance(protocol_session, str) or not protocol_session.startswith(
            "session-"
        ):
            raise FormalSessionError(f"prior manifest has no protocol session: {path}")
        index = int(protocol_session.rsplit("-", 1)[-1])
        if manifest.get("status") == "completed":
            completed.setdefault(index, []).append((path, manifest))
    for index in range(1, session_index):
        if len(completed.get(index, [])) != 1:
            raise FormalSessionError(
                f"session-{index:02d} must have exactly one completed attempt first"
            )
    if any(index >= session_index for index in completed):
        raise FormalSessionError("formal sessions cannot be repeated or reordered")
    if attempt == 1:
        if replacement_for is not None or infrastructure_failure is not None:
            raise FormalSessionError("attempt 01 cannot be a replacement")
    else:
        if not replacement_for or not infrastructure_failure:
            raise FormalSessionError(
                "replacement attempts require the prior identifier and failure class"
            )
        if infrastructure_failure not in _INFRASTRUCTURE_FAILURES:
            raise FormalSessionError("replacement failure class is not preregistered")
        expected_name = f"session-{session_index:02d}-attempt-{attempt - 1:02d}"
        if replacement_for != expected_name:
            raise FormalSessionError(f"replacement_for must identify {expected_name}")
        prior = next(
            (
                manifest
                for path, manifest in manifests
                if path.parent.name == replacement_for
            ),
            None,
        )
        if prior is None or prior.get("status") not in {"running", "aborted"}:
            raise FormalSessionError(
                "replacement requires a retained incomplete prior attempt"
            )
        if (
            prior.get("status") == "aborted"
            and prior.get("failure_class") != "infrastructure"
        ):
            raise FormalSessionError("system-under-test failures cannot be replaced")
    if session_index > 1:
        previous = completed[session_index - 1][0][1]
        elapsed = now - _parse_time(previous.get("completed_at"))
        required = minimum_separation_minutes * 60
        if elapsed.total_seconds() < required:
            raise FormalSessionError(
                "the preregistered inter-session separation has not elapsed"
            )
        return previous
    return None


def _services_changed(
    previous: Mapping[str, object] | None,
    current_preflight: Mapping[str, object],
) -> bool:
    if previous is None:
        return True
    prior_preflight = previous.get("preflight")
    prior_record = prior_preflight if isinstance(prior_preflight, Mapping) else {}
    old = prior_record.get("service_identity")
    new = current_preflight.get("service_identity")
    if not isinstance(old, Mapping) or not isinstance(new, Mapping):
        return False
    for service in ("llama-server", "ollama"):
        old_service = old.get(service)
        new_service = new.get(service)
        if not isinstance(old_service, Mapping) or not isinstance(new_service, Mapping):
            return False
        if old_service.get("process_start_identities") == new_service.get(
            "process_start_identities"
        ):
            return False
    return True


def _make_run_id(condition: str, workload: str, sequence: int) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}_phase1_{condition}_{workload}_{sequence:03d}"


def _adapter_factory(workload: str) -> object:
    if workload == "asr":
        return FixedInputASRAdapter(
            execution_timeout_s=120.0,
            poll_interval_s=0.05,
            terminate_timeout_s=2.0,
            kill_timeout_s=2.0,
        )
    if workload == "llm":
        return FixedInputLLMAdapter(request_timeout_s=120.0)
    if workload == "vlm":
        return ProcessIsolatedVLMAdapter(
            execution_timeout_s=600.0,
            poll_interval_s=0.02,
            join_timeout_s=5.0,
            terminate_join_timeout_s=5.0,
        )
    raise ValueError(f"unsupported formal workload: {workload}")


def _run_workload_entry(
    session_dir: Path,
    entry: Mapping[str, object],
    *,
    ordinal: int,
    payloads: Mapping[str, object],
    adapter_factories: Mapping[str, Callable[[], object]],
    thermal_monitor: ThermalMonitor,
) -> tuple[Path, dict[str, object]]:
    workload = str(entry["workload"])
    condition = FormalCondition(str(entry["condition"]))
    role = str(entry["role"])
    run_id = _make_run_id(condition.value, workload, ordinal)
    run_dir = (
        session_dir
        / ("warmups" if role == "warmup" else "measured")
        / (f"{ordinal:03d}-{workload}-{condition.value}")
    )
    if run_dir.exists():
        raise FileExistsError(f"refusing to overwrite formal run: {run_dir}")
    run_dir.mkdir(parents=True)
    recorder = EventRecorder(run_dir, run_id)
    timeout = _TIMEOUTS[workload]
    spec = FormalRunSpec(
        workload=workload,
        condition=condition,
        role=role,
        prelude_s=1.0,
        postlude_s=1.0,
        result_validity_s=timeout["validity"],
        completion_timeout_s=timeout["completion"],
        join_timeout_s=timeout["join"],
        probe_period_ns=100_000_000,
        probe_deadline_ns=100_000_000,
    )
    started_at = utc_now_iso()
    try:
        adapter = adapter_factories[workload]()
        report = run_formal_workload(
            spec,
            payloads[workload],
            recorder,
            adapter,
            task_id=f"formal-{ordinal:03d}-{workload}",
            thermal_stop=thermal_monitor.stop_requested,
        )
    except Exception as exc:
        failed = {
            "formal_run_schema_version": FORMAL_RUN_SCHEMA_VERSION,
            "run_id": run_id,
            "status": "failed",
            "started_at": started_at,
            "completed_at": utc_now_iso(),
            "plan": dict(entry),
            "failure_code": type(exc).__name__.lower(),
        }
        write_json_atomic(run_dir / "run.json", failed)
        raise
    finally:
        recorder.close()
    record = {
        **report,
        "run_id": run_id,
        "status": "completed" if report["valid"] is True else "failed",
        "started_at": started_at,
        "completed_at": utc_now_iso(),
        "plan": dict(entry),
        "input": {
            "sha256": payloads[workload].sha256,
            "size_bytes": payloads[workload].size_bytes,
            "media_type": payloads[workload].media_type,
            "path_recorded": False,
        },
        "raw_input_recorded": False,
        "raw_output_recorded": False,
    }
    write_json_atomic(run_dir / "run.json", record)
    if record["valid"] is not True:
        raise FormalSessionError("one or more formal run Gates failed")
    return run_dir, record


def _run_idle_epoch(
    session_dir: Path,
    *,
    label: str,
    duration_s: int,
    thermal_monitor: ThermalMonitor,
) -> tuple[Path, dict[str, object]]:
    run_id = _make_run_id("formal_idle", "simulated", 0)
    run_dir = session_dir / "idle" / label
    if run_dir.exists():
        raise FileExistsError(f"refusing to overwrite idle epoch: {run_dir}")
    run_dir.mkdir(parents=True)
    recorder = EventRecorder(run_dir, run_id)
    probe = PeriodicProbe(
        period_ns=100_000_000,
        deadline_ns=100_000_000,
        event_sink=recorder,
        thread_name=f"phase1-formal-{label}-probe",
    )
    started_ns = time.monotonic_ns()
    recorder.emit(
        RuntimeEvent(
            event="formal.idle_started",
            component="runner",
            status=EventStatus.STARTED,
            details={"duration_s": duration_s, "label": label},
        )
    )
    probe.start()
    report = None
    try:
        deadline = time.monotonic() + duration_s
        while time.monotonic() < deadline:
            if thermal_monitor.stop_requested.is_set():
                raise FormalSessionError("thermal stop during idle epoch")
            threading.Event().wait(min(0.1, deadline - time.monotonic()))
    finally:
        report = probe.stop(join_timeout_s=5.0)
        recorder.emit(
            RuntimeEvent(
                event="formal.idle_stopped",
                component="runner",
                status=(
                    EventStatus.OK
                    if report.joined and report.error_code is None
                    else EventStatus.ERROR
                ),
                details={"duration_s": duration_s, "label": label},
            )
        )
        recorder.close()
    finished_ns = time.monotonic_ns()
    record = {
        "formal_run_schema_version": FORMAL_RUN_SCHEMA_VERSION,
        "run_id": run_id,
        "status": "completed",
        "role": "idle_reference",
        "condition": "formal_idle",
        "label": label,
        "duration_s": duration_s,
        "started_monotonic_ns": started_ns,
        "finished_monotonic_ns": finished_ns,
        "probe": {
            "implementation": "independent_thread",
            "joined": report.joined,
            "tick_count": report.tick_count,
            "skipped_releases": report.skipped_releases,
            "deadline_miss_count": report.deadline_miss_count,
            "max_lateness_ns": report.max_lateness_ns,
            "max_gap_ns": report.max_gap_ns,
            "error_code": report.error_code,
        },
        "valid": report.joined and report.error_code is None,
    }
    write_json_atomic(run_dir / "run.json", record)
    if record["valid"] is not True:
        raise FormalSessionError("formal idle probe did not close cleanly")
    return run_dir, record


def _session_plan(
    protocol: Mapping[str, object], session_name: str
) -> Mapping[str, object]:
    sessions = protocol.get("sessions")
    if not isinstance(sessions, list):
        raise FormalSessionError("formal protocol contains no session plan")
    selected = [
        session
        for session in sessions
        if isinstance(session, Mapping) and session.get("session") == session_name
    ]
    if len(selected) != 1:
        raise FormalSessionError(f"protocol session is not unique: {session_name}")
    return selected[0]


def run_session(
    args: argparse.Namespace,
    *,
    repo_root: Path | str | None = None,
    preflight_builder: Callable[..., dict[str, object]] | None = None,
    sampler_factory: Callable[..., object] | None = None,
    adapter_factories: Mapping[str, Callable[[], object]] | None = None,
    payloads_override: Mapping[str, object] | None = None,
    entry_runner: Callable[..., tuple[Path, dict[str, object]]] | None = None,
    idle_runner: Callable[..., tuple[Path, dict[str, object]]] | None = None,
) -> Path:
    """Execute one complete, ordered protocol session and close its evidence."""

    root = (
        Path(repo_root).resolve()
        if repo_root is not None
        else Path(__file__).resolve().parents[2]
    )
    protocol_path = Path(args.protocol).resolve()
    protocol = load_formal_protocol(protocol_path)
    injected = [
        name
        for name, value in (
            ("preflight_builder", preflight_builder),
            ("sampler_factory", sampler_factory),
            ("adapter_factories", adapter_factories),
            ("payloads_override", payloads_override),
            ("entry_runner", entry_runner),
            ("idle_runner", idle_runner),
        )
        if value is not None
    ]
    errors = formal_protocol_errors(protocol)
    if errors or protocol_sha256(protocol) != FROZEN_PROTOCOL_SHA256:
        raise FormalSessionError(
            "formal protocol identity failed: " + "; ".join(errors or ["hash"])
        )
    if FORMAL_COLLECTION_STATUS != "active" and not injected:
        raise FormalSessionError(
            "formal protocol is closed after a system-under-test failure"
        )
    if not 1 <= args.session_index <= 5:
        raise FormalSessionError("session_index must be from 1 to 5")
    if not 1 <= args.attempt <= 99:
        raise FormalSessionError("attempt must be from 1 to 99")
    collection_id = args.collection_id or make_collection_id()
    if not _COLLECTION_ID_RE.fullmatch(collection_id):
        raise FormalSessionError(
            "collection_id must use YYYYMMDDTHHMMSSZ_phase1_formal_<label>"
        )
    if args.session_index > 1 and args.collection_id is None:
        raise FormalSessionError("later sessions require the existing collection_id")
    output_root = _resolve_output_root(Path(args.output_root), root)
    collection_dir = output_root / collection_id
    session_name = f"session-{args.session_index:02d}"
    directory_name = f"{session_name}-attempt-{args.attempt:02d}"
    session_dir = collection_dir / directory_name
    if session_dir.exists():
        raise FileExistsError(f"refusing to overwrite formal session: {session_dir}")
    now = datetime.now(timezone.utc)
    previous = _check_collection_order(
        collection_dir,
        session_index=args.session_index,
        attempt=args.attempt,
        minimum_separation_minutes=30,
        now=now,
        replacement_for=args.replacement_for,
        infrastructure_failure=args.infrastructure_failure,
    )
    resolved_preflight_builder = preflight_builder or build_formal_preflight
    preflight = resolved_preflight_builder(
        root,
        protocol,
        asr_input=args.asr_input,
        llm_input=args.llm_input,
        vlm_input=args.vlm_input,
        services_restarted=args.confirm_services_restarted,
        dynamic_dvfs_confirmed=args.confirm_dynamic_dvfs,
        protocol_path=protocol_path,
    )
    preflight_errors = formal_preflight_errors(preflight)
    if preflight_errors:
        raise FormalSessionError(
            "formal preflight failed: " + "; ".join(preflight_errors)
        )
    if not _services_changed(previous, preflight):
        raise FormalSessionError(
            "model service process identities did not change between sessions"
        )

    plan = _session_plan(protocol, session_name)
    session_dir.mkdir(parents=True)
    protocol_copy = session_dir / "protocol.json"
    protocol_copy.write_text(
        canonical_protocol_text(protocol), encoding="utf-8", newline="\n"
    )
    write_json_atomic(session_dir / "preflight.json", preflight)
    ledger_path = session_dir / "ledger.jsonl"
    ledger_path.touch(exist_ok=False)
    manifest_path = session_dir / "manifest.json"
    manifest: dict[str, object] = {
        "manifest_schema_version": MANIFEST_SCHEMA_VERSION,
        "formal_session_schema_version": FORMAL_SESSION_SCHEMA_VERSION,
        "event_schema_version": SCHEMA_VERSION,
        "artifact_kind": "phase1_g6_formal_session",
        "collection_id": collection_id,
        "session_id": directory_name,
        "protocol_session": session_name,
        "attempt": args.attempt,
        "replacement_for": args.replacement_for,
        "infrastructure_failure": args.infrastructure_failure,
        "protocol_id": FORMAL_PROTOCOL_ID,
        "protocol_sha256": FROZEN_PROTOCOL_SHA256,
        "status": "running",
        "failure_class": None,
        "failure_code": None,
        "created_at": utc_now_iso(),
        "completed_at": None,
        "formal_evidence_eligible": False,
        "development_injection": bool(injected),
        "injected_components": injected,
        "preflight": {
            "protocol_commit": preflight["protocol"]["protocol_commit"],
            "runner_commit": preflight["protocol"]["runner_commit"],
            "service_identity": preflight["service_identity"],
        },
        "thermal": {
            "session_start": None,
            "measurement_start": None,
            "stop_tj_c": 85.0,
            "stop_requested": False,
        },
        "resource_sampler_report": None,
        "completed_entries": 0,
        "artifacts": {},
    }
    write_json_atomic(manifest_path, manifest)

    payloads = dict(
        payloads_override
        or {
            "asr": fixed_asr_payload(args.asr_input),
            "llm": fixed_llm_payload(args.llm_input),
            "vlm": fixed_c100_payload(args.vlm_input),
        }
    )
    if set(payloads) != set(_TIMEOUTS):
        raise FormalSessionError("payloads must cover asr, llm and vlm")
    factories = dict(
        adapter_factories
        or {
            workload: (lambda name=workload: _adapter_factory(name))
            for workload in _TIMEOUTS
        }
    )
    if set(factories) != set(_TIMEOUTS) or not all(
        callable(factory) for factory in factories.values()
    ):
        raise FormalSessionError("adapter factories must cover asr, llm and vlm")
    monitor = ThermalMonitor(stop_tj_c=85.0)
    resolved_sampler_factory = sampler_factory or TegrastatsSampler
    resolved_entry_runner = entry_runner or _run_workload_entry
    resolved_idle_runner = idle_runner or _run_idle_epoch
    sampler = None
    failure_class = "system_under_test"
    try:
        try:
            sampler = resolved_sampler_factory(
                session_dir,
                200,
                sample_callback=monitor.observe,
            )
            sampler.start(first_sample_timeout_s=5.0)
        except Exception:
            failure_class = "infrastructure"
            raise
        manifest["thermal"]["session_start"] = monitor.wait_below(
            maximum_tj_c=55.0,
            consecutive_samples=10,
            timeout_s=args.thermal_wait_timeout_s,
        )
        write_json_atomic(manifest_path, manifest)

        ordinal = 0
        for warmup in plan["warmups"]:
            ordinal += 1
            entry = dict(warmup)
            entry["condition"] = "formal_sync"
            entry["ordinal"] = ordinal
            _append_jsonl(
                ledger_path,
                {"event": "entry_started", "at": utc_now_iso(), "plan": entry},
            )
            run_dir, record = resolved_entry_runner(
                session_dir,
                entry,
                ordinal=ordinal,
                payloads=payloads,
                adapter_factories=factories,
                thermal_monitor=monitor,
            )
            _append_jsonl(
                ledger_path,
                {
                    "event": "entry_completed",
                    "at": utc_now_iso(),
                    "plan": entry,
                    "run": run_dir.relative_to(session_dir).as_posix(),
                    "valid": record["valid"],
                },
            )
            manifest["completed_entries"] = ordinal
            write_json_atomic(manifest_path, manifest)

        pre_idle_sequence = monitor.latest_sequence()
        idle = plan["pre_measurement_idle"]
        _append_jsonl(
            ledger_path,
            {"event": "idle_started", "at": utc_now_iso(), "label": "pre_measurement"},
        )
        idle_dir, _ = resolved_idle_runner(
            session_dir,
            label="pre_measurement",
            duration_s=int(idle["duration_s"]),
            thermal_monitor=monitor,
        )
        _append_jsonl(
            ledger_path,
            {
                "event": "idle_completed",
                "at": utc_now_iso(),
                "label": "pre_measurement",
                "run": idle_dir.relative_to(session_dir).as_posix(),
            },
        )
        manifest["thermal"]["measurement_start"] = monitor.wait_below(
            maximum_tj_c=55.0,
            consecutive_samples=10,
            timeout_s=1.0,
            after_sequence=pre_idle_sequence,
        )
        write_json_atomic(manifest_path, manifest)

        for planned in plan["measured_runs"]:
            ordinal += 1
            entry = dict(planned)
            entry["ordinal"] = ordinal
            _append_jsonl(
                ledger_path,
                {"event": "entry_started", "at": utc_now_iso(), "plan": entry},
            )
            run_dir, record = resolved_entry_runner(
                session_dir,
                entry,
                ordinal=ordinal,
                payloads=payloads,
                adapter_factories=factories,
                thermal_monitor=monitor,
            )
            _append_jsonl(
                ledger_path,
                {
                    "event": "entry_completed",
                    "at": utc_now_iso(),
                    "plan": entry,
                    "run": run_dir.relative_to(session_dir).as_posix(),
                    "valid": record["valid"],
                },
            )
            manifest["completed_entries"] = ordinal
            write_json_atomic(manifest_path, manifest)

        post_idle = plan["post_measurement_idle"]
        _append_jsonl(
            ledger_path,
            {"event": "idle_started", "at": utc_now_iso(), "label": "post_measurement"},
        )
        idle_dir, post_idle_record = resolved_idle_runner(
            session_dir,
            label="post_measurement",
            duration_s=int(post_idle["duration_s"]),
            thermal_monitor=monitor,
        )
        _append_jsonl(
            ledger_path,
            {
                "event": "idle_completed",
                "at": utc_now_iso(),
                "label": "post_measurement",
                "run": idle_dir.relative_to(session_dir).as_posix(),
            },
        )
        try:
            sampler.wait_for_sample_at_or_after(
                int(post_idle_record["finished_monotonic_ns"]),
                timeout_s=2.0,
            )
        except (RuntimeError, TimeoutError):
            failure_class = "infrastructure"
            raise
        sampler_report = sampler.stop()
        manifest["resource_sampler_report"] = sampler_report.to_dict()
        if not sampler_report.successful:
            failure_class = "infrastructure"
            raise FormalSessionError("resource sampler did not close successfully")
        samples = load_resource_samples(session_dir / "resources.jsonl")
        resource_errors = validate_resource_samples(samples)
        if resource_errors:
            failure_class = "infrastructure"
            raise FormalSessionError(
                "resource trace validation failed: " + "; ".join(resource_errors)
            )
        if monitor.stop_requested.is_set():
            raise FormalSessionError("thermal stop threshold was reached")

        manifest["thermal"]["stop_requested"] = False
        manifest["status"] = "completed"
        manifest["completed_at"] = utc_now_iso()
        manifest["formal_evidence_eligible"] = not injected
        manifest["artifacts"] = _artifact_inventory(session_dir)
        write_json_atomic(manifest_path, manifest)
    except BaseException as exc:
        if sampler is not None and getattr(sampler, "is_running", False):
            try:
                report = sampler.stop()
                manifest["resource_sampler_report"] = report.to_dict()
            except Exception:
                pass
        manifest["thermal"]["stop_requested"] = monitor.stop_requested.is_set()
        manifest["status"] = "aborted"
        manifest["failure_class"] = failure_class
        manifest["failure_code"] = type(exc).__name__.lower()
        manifest["completed_at"] = utc_now_iso()
        manifest["formal_evidence_eligible"] = False
        manifest["artifacts"] = _artifact_inventory(session_dir)
        write_json_atomic(manifest_path, manifest)
        raise
    return session_dir


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session-index", type=int, required=True)
    parser.add_argument("--attempt", type=int, default=1)
    parser.add_argument("--collection-id")
    parser.add_argument("--replacement-for")
    parser.add_argument(
        "--infrastructure-failure",
        choices=_INFRASTRUCTURE_FAILURES,
    )
    parser.add_argument("--protocol", type=Path, default=DEFAULT_PROTOCOL_PATH)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--asr-input", type=Path, default=DEFAULT_ASR_INPUT)
    parser.add_argument("--llm-input", type=Path, default=DEFAULT_LLM_INPUT)
    parser.add_argument("--vlm-input", type=Path, default=DEFAULT_VLM_INPUT)
    parser.add_argument("--confirm-services-restarted", action="store_true")
    parser.add_argument("--confirm-dynamic-dvfs", action="store_true")
    parser.add_argument("--thermal-wait-timeout-s", type=float, default=900.0)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        session_dir = run_session(args)
    except BaseException as exc:
        if isinstance(exc, KeyboardInterrupt):
            print("Phase 1 formal session interrupted", file=sys.stderr)
        else:
            print(
                f"Phase 1 formal session failed: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
        return 1
    print(session_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
