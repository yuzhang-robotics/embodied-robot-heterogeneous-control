"""Run one motion-disabled ASR/VLM carryover diagnostic session."""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping, Sequence

from experiments.phase1.asr_adapter import (
    FixedInputASRAdapter,
    fixed_asr_payload,
    load_phase0_asr_runtime,
)
from experiments.phase1.carryover_observation import (
    ObservedProcessFactory,
    memory_observation,
)
from experiments.phase1.carryover_preflight import (
    build_carryover_preflight,
    carryover_preflight_errors,
)
from experiments.phase1.carryover_protocol import (
    CARRYOVER_PROTOCOL_ID,
    CARRYOVER_PROTOCOL_STATUS,
    DEFAULT_PROTOCOL_PATH,
    INTERPOSER_INTERVAL_S,
    PRIMER_WARM_MAX_MS,
    RESOURCE_INTERVAL_MS,
    START_LATENESS_MAX_MS,
    canonical_protocol_text,
    load_protocol,
    protocol_sha256,
    session_order,
)
from experiments.phase1.formal_run import (
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
from experiments.phase1.manifest import sha256_file, utc_now_iso, write_json_atomic
from experiments.phase1.run_formal_session import ThermalMonitor
from experiments.phase1.telemetry import EventRecorder, SCHEMA_VERSION
from experiments.phase1.vlm_adapter import fixed_c100_payload
from experiments.phase1.vlm_process_adapter import ProcessIsolatedVLMAdapter


CARRYOVER_SESSION_SCHEMA_VERSION = "0.1.0"
CARRYOVER_RUN_SCHEMA_VERSION = "0.2.0"
DEFAULT_OUTPUT_ROOT = Path(__file__).resolve().parents[1] / "runs" / "phase1-carryover"
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
_COLLECTION_RE = re.compile(r"^[0-9]{8}T[0-9]{6}Z_phase1_asr_vlm_carryover_v2$")
_TIMEOUTS = {
    "asr": {"validity": 180.0, "completion": 150.0, "join": 10.0},
    "llm": {"validity": 180.0, "completion": 150.0, "join": 130.0},
    "vlm": {"validity": 900.0, "completion": 720.0, "join": 720.0},
}


class CarryoverSessionError(RuntimeError):
    """The diagnostic session could not continue without invalidating a unit."""


def make_collection_id(now: datetime | None = None) -> str:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return current.astimezone(timezone.utc).strftime(
        "%Y%m%dT%H%M%SZ_phase1_asr_vlm_carryover_v2"
    )


def make_run_id(
    workload: str,
    ordinal: int,
    now: datetime | None = None,
) -> str:
    """Build one UTC-correlated run ID accepted by the event recorder."""

    if workload not in {"asr", "llm", "vlm"}:
        raise ValueError(f"unsupported carryover workload: {workload}")
    if (
        isinstance(ordinal, bool)
        or not isinstance(ordinal, int)
        or not 1 <= ordinal <= 999
    ):
        raise ValueError("ordinal must be between 1 and 999")
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    stamp = current.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}_phase1_carryover_{workload}_{ordinal:03d}"


def _artifact_inventory(session_dir: Path) -> dict[str, object]:
    paths = sorted(
        path
        for path in session_dir.rglob("*")
        if path.is_file()
        and path.name != "manifest.json"
        and not path.name.endswith(".tmp")
    )
    return {
        "count": len(paths),
        "files": [
            {
                "path": path.relative_to(session_dir).as_posix(),
                "size_bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
            for path in paths
        ],
    }


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


def _adapter_duration_ms(run: Mapping[str, object]) -> float:
    adapter = run.get("adapter")
    if not isinstance(adapter, Mapping):
        raise CarryoverSessionError("run adapter record is missing")
    duration = adapter.get("duration_ns")
    if isinstance(duration, bool) or not isinstance(duration, int) or duration <= 0:
        raise CarryoverSessionError("run adapter duration is invalid")
    return duration / 1_000_000


def _adapter_boundary(run: Mapping[str, object], name: str) -> int:
    adapter = run.get("adapter")
    value = adapter.get(name) if isinstance(adapter, Mapping) else None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise CarryoverSessionError(f"run adapter boundary is invalid: {name}")
    return value


def validate_primer_2(run: Mapping[str, object]) -> float:
    """Require the second primer to prove the unit began in the warm regime."""

    duration_ms = _adapter_duration_ms(run)
    if run.get("valid") is not True or duration_ms > PRIMER_WARM_MAX_MS:
        raise CarryoverSessionError(
            f"ASR primer 2 did not establish warm state: {duration_ms:.3f} ms"
        )
    return duration_ms


def validate_fixed_interval(
    primer_2: Mapping[str, object],
    measured_asr: Mapping[str, object],
    *,
    interposer_finished_monotonic_ns: int,
) -> dict[str, object]:
    """Validate the frozen primer-to-outcome interval and reject overruns."""

    primer_finished = _adapter_boundary(primer_2, "finished_monotonic_ns")
    measured_started = _adapter_boundary(measured_asr, "started_monotonic_ns")
    if (
        isinstance(interposer_finished_monotonic_ns, bool)
        or not isinstance(interposer_finished_monotonic_ns, int)
        or interposer_finished_monotonic_ns < primer_finished
    ):
        raise CarryoverSessionError("interposer completion boundary is invalid")
    target = primer_finished + int(INTERPOSER_INTERVAL_S * 1_000_000_000)
    if interposer_finished_monotonic_ns > target:
        raise CarryoverSessionError("interposer exceeded the fixed interval")
    interval_ns = measured_started - primer_finished
    lateness_ns = measured_started - target
    maximum_lateness_ns = int(START_LATENESS_MAX_MS * 1_000_000)
    if interval_ns < int(INTERPOSER_INTERVAL_S * 1_000_000_000):
        raise CarryoverSessionError("measured ASR started before the fixed interval")
    if lateness_ns > maximum_lateness_ns:
        raise CarryoverSessionError("measured ASR start exceeded the lateness bound")
    return {
        "target_interval_s": INTERPOSER_INTERVAL_S,
        "observed_interval_ms": interval_ns / 1_000_000,
        "start_lateness_ms": lateness_ns / 1_000_000,
        "start_lateness_max_ms": START_LATENESS_MAX_MS,
        "interposer_overrun": False,
    }


def _default_adapter(workload: str) -> tuple[object, ObservedProcessFactory | None]:
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
    if workload == "llm":
        return FixedInputLLMAdapter(request_timeout_s=120.0), None
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
    raise ValueError(f"unsupported diagnostic workload: {workload}")


def _run_invocation(
    session_dir: Path,
    *,
    ordinal: int,
    unit_index: int,
    workload: str,
    diagnostic_role: str,
    payloads: Mapping[str, object],
    thermal_monitor: ThermalMonitor,
    not_before_monotonic_ns: int | None = None,
    adapter_factory: Callable[[str], tuple[object, ObservedProcessFactory | None]] = (
        _default_adapter
    ),
) -> tuple[Path, dict[str, object]]:
    run_dir = session_dir / "runs" / f"{ordinal:03d}-{workload}-{diagnostic_role}"
    if run_dir.exists():
        raise FileExistsError(f"refusing to overwrite diagnostic invocation: {run_dir}")
    run_dir.mkdir(parents=True)
    run_id = make_run_id(workload, ordinal)
    recorder = EventRecorder(run_dir, run_id)
    timeout = _TIMEOUTS[workload]
    role = "warmup" if diagnostic_role.startswith("primer") else "measured"
    spec = FormalRunSpec(
        workload=workload,
        condition=FormalCondition.SYNC,
        role=role,
        prelude_s=0.25,
        postlude_s=0.25,
        result_validity_s=timeout["validity"],
        completion_timeout_s=timeout["completion"],
        join_timeout_s=timeout["join"],
        probe_period_ns=100_000_000,
        probe_deadline_ns=100_000_000,
    )
    adapter, observer = adapter_factory(workload)
    started_at = utc_now_iso()
    try:
        formal_report = run_formal_workload(
            spec,
            payloads[workload],
            recorder,
            adapter,
            task_id=run_id,
            thermal_stop=thermal_monitor.stop_requested,
            task_protocol="phase1_carryover_diagnostic",
            state_scope_id="phase1-carryover",
            not_before_monotonic_ns=not_before_monotonic_ns,
        )
        process_observation = observer.finish() if observer is not None else None
    finally:
        recorder.close()
    converted = {
        key: value
        for key, value in formal_report.items()
        if key not in {"formal_run_schema_version", "condition", "role"}
    }
    record = {
        "carryover_run_schema_version": CARRYOVER_RUN_SCHEMA_VERSION,
        "artifact_kind": "phase1_carryover_diagnostic_invocation",
        "diagnostic_role": diagnostic_role,
        **converted,
        "asr_process_observation": process_observation,
        "started_at": started_at,
        "completed_at": utc_now_iso(),
        "formal_evidence": False,
        "raw_input_recorded": False,
        "raw_output_recorded": False,
    }
    write_json_atomic(run_dir / "run.json", record)
    if record.get("valid") is not True:
        raise CarryoverSessionError("one or more invocation Gates failed")
    if workload == "asr" and process_observation is None:
        raise CarryoverSessionError("ASR process observation is missing")
    return run_dir, record


def _prior_session(
    collection_dir: Path, session_index: int
) -> Mapping[str, object] | None:
    expected = (
        sorted(collection_dir.glob("session-*-attempt-*"))
        if collection_dir.exists()
        else []
    )
    if len(expected) != session_index - 1:
        raise CarryoverSessionError(
            "collection does not contain the expected prior sessions"
        )
    previous: Mapping[str, object] | None = None
    for index, directory in enumerate(expected, start=1):
        if directory.name != f"session-{index:02d}-attempt-01":
            raise CarryoverSessionError("prior diagnostic session names are invalid")
        value = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        if not isinstance(value, Mapping) or value.get("status") != "completed":
            raise CarryoverSessionError("a prior diagnostic session is incomplete")
        previous = value
    return previous


def _services_changed(
    previous: Mapping[str, object] | None, current: Mapping[str, object]
) -> bool:
    if previous is None:
        return True
    prior_preflight = previous.get("preflight")
    prior = prior_preflight if isinstance(prior_preflight, Mapping) else {}
    old = prior.get("service_identity")
    new = current.get("service_identity")
    if not isinstance(old, Mapping) or not isinstance(new, Mapping):
        return False
    return all(old.get(name) != new.get(name) for name in ("llama-server", "ollama"))


def run_session(
    args: argparse.Namespace,
    *,
    repo_root: Path | str | None = None,
    preflight_builder: Callable[..., dict[str, object]] = build_carryover_preflight,
    sampler_factory: Callable[..., TegrastatsSampler] = TegrastatsSampler,
    invocation_runner: Callable[..., tuple[Path, dict[str, object]]] = _run_invocation,
    observation_reader: Callable[[Path | str], dict[str, object]] = memory_observation,
    payloads_override: Mapping[str, object] | None = None,
    whisper_model_path: Path | str | None = None,
) -> Path:
    """Execute the frozen three-unit schedule and close one diagnostic session."""

    root = (
        Path(repo_root).resolve()
        if repo_root is not None
        else Path(__file__).resolve().parents[2]
    )
    protocol_path = Path(args.protocol).resolve()
    protocol = load_protocol(protocol_path)
    if CARRYOVER_PROTOCOL_STATUS != "active":
        raise CarryoverSessionError("carryover diagnostic protocol is not active")
    if not 1 <= args.session_index <= 6 or args.attempt != 1:
        raise CarryoverSessionError("only sessions 1-6 and attempt 1 are permitted")
    collection_id = args.collection_id or make_collection_id()
    if _COLLECTION_RE.fullmatch(collection_id) is None:
        raise CarryoverSessionError(
            "collection id does not match the diagnostic contract"
        )
    if args.session_index > 1 and args.collection_id is None:
        raise CarryoverSessionError("later sessions require the existing collection id")
    output_root = Path(args.output_root).expanduser()
    if not output_root.is_absolute():
        output_root = root / output_root
    output_root = output_root.resolve()
    if root in output_root.parents and not (
        (root / "experiments" / "runs").resolve() in output_root.parents
        or output_root == (root / "experiments" / "runs").resolve()
    ):
        raise CarryoverSessionError(
            "repository-local output must be under experiments/runs"
        )
    collection_dir = output_root / collection_id
    previous = _prior_session(collection_dir, args.session_index)
    session_dir = collection_dir / f"session-{args.session_index:02d}-attempt-01"
    if session_dir.exists():
        raise FileExistsError(
            f"refusing to overwrite diagnostic session: {session_dir}"
        )
    preflight = preflight_builder(
        root,
        protocol,
        asr_input=args.asr_input,
        llm_input=args.llm_input,
        vlm_input=args.vlm_input,
        services_restarted=args.confirm_services_restarted,
        dynamic_dvfs_confirmed=args.confirm_dynamic_dvfs,
        protocol_path=protocol_path,
    )
    errors = carryover_preflight_errors(preflight)
    if errors:
        raise CarryoverSessionError("diagnostic preflight failed: " + "; ".join(errors))
    if not _services_changed(previous, preflight):
        raise CarryoverSessionError(
            "model service identities did not change between sessions"
        )
    payloads = dict(
        payloads_override
        or {
            "asr": fixed_asr_payload(args.asr_input),
            "llm": fixed_llm_payload(args.llm_input),
            "vlm": fixed_c100_payload(args.vlm_input),
        }
    )
    if set(payloads) != {"asr", "llm", "vlm"}:
        raise CarryoverSessionError("fixed payloads must cover ASR, LLM and VLM")
    model_path = Path(whisper_model_path or load_phase0_asr_runtime().whisper_model)
    order = session_order(protocol, args.session_index)
    session_dir.mkdir(parents=True)
    (session_dir / "protocol.json").write_text(
        canonical_protocol_text(protocol), encoding="utf-8", newline="\n"
    )
    write_json_atomic(session_dir / "preflight.json", preflight)
    ledger_path = session_dir / "ledger.jsonl"
    ledger_path.touch(exist_ok=False)
    manifest_path = session_dir / "manifest.json"
    preflight_protocol = preflight.get("protocol")
    if not isinstance(preflight_protocol, Mapping):
        raise CarryoverSessionError("diagnostic preflight protocol record is missing")
    thermal: dict[str, object] = {
        "session_start": None,
        "stop_tj_c": 85.0,
        "stop_requested": False,
    }
    manifest: dict[str, object] = {
        "carryover_session_schema_version": CARRYOVER_SESSION_SCHEMA_VERSION,
        "event_schema_version": SCHEMA_VERSION,
        "artifact_kind": "phase1_asr_vlm_carryover_diagnostic_session",
        "collection_id": collection_id,
        "session_id": session_dir.name,
        "protocol_id": CARRYOVER_PROTOCOL_ID,
        "protocol_sha256": protocol_sha256(protocol),
        "interposer_order": list(order),
        "status": "running",
        "failure_code": None,
        "created_at": utc_now_iso(),
        "completed_at": None,
        "formal_evidence": False,
        "preflight": {
            "protocol_commit": preflight_protocol.get("protocol_commit"),
            "runner_commit": preflight_protocol.get("runner_commit"),
            "service_identity": preflight["service_identity"],
        },
        "thermal": thermal,
        "resource_sampler_report": None,
        "completed_units": 0,
        "artifacts": {},
    }
    write_json_atomic(manifest_path, manifest)
    monitor = ThermalMonitor(stop_tj_c=85.0)
    sampler: TegrastatsSampler | None = None
    ordinal = 0
    try:
        sampler = sampler_factory(
            session_dir,
            RESOURCE_INTERVAL_MS,
            sample_callback=monitor.observe,
        )
        sampler.start(first_sample_timeout_s=5.0)
        thermal["session_start"] = monitor.wait_below(
            maximum_tj_c=55.0,
            consecutive_samples=10,
            timeout_s=args.thermal_wait_timeout_s,
        )
        write_json_atomic(manifest_path, manifest)
        for unit_index, interposer in enumerate(order, start=1):
            unit_dir = session_dir / "units" / f"unit-{unit_index:02d}-{interposer}"
            unit_dir.mkdir(parents=True)
            _append_jsonl(
                ledger_path,
                {
                    "event": "unit_started",
                    "at": utc_now_iso(),
                    "unit": unit_index,
                    "interposer": interposer,
                },
            )
            observations: dict[str, dict[str, object]] = {
                "before_primers": observation_reader(model_path)
            }
            runs: dict[str, dict[str, object]] = {}
            run_paths: dict[str, str] = {}
            for role in ("primer_1", "primer_2"):
                ordinal += 1
                run_path, run = invocation_runner(
                    session_dir,
                    ordinal=ordinal,
                    unit_index=unit_index,
                    workload="asr",
                    diagnostic_role=role,
                    payloads=payloads,
                    thermal_monitor=monitor,
                )
                runs[role] = run
                run_paths[role] = run_path.relative_to(session_dir).as_posix()
            primer_ms = validate_primer_2(runs["primer_2"])
            observations["after_primer_2"] = observation_reader(model_path)
            primer_finished = _adapter_boundary(
                runs["primer_2"], "finished_monotonic_ns"
            )
            target = primer_finished + int(INTERPOSER_INTERVAL_S * 1_000_000_000)
            if interposer == "idle":
                interposer_finished = time.monotonic_ns()
            else:
                ordinal += 1
                run_path, run = invocation_runner(
                    session_dir,
                    ordinal=ordinal,
                    unit_index=unit_index,
                    workload=interposer,
                    diagnostic_role="interposer",
                    payloads=payloads,
                    thermal_monitor=monitor,
                )
                runs["interposer"] = run
                run_paths["interposer"] = run_path.relative_to(session_dir).as_posix()
                interposer_finished = _adapter_boundary(run, "finished_monotonic_ns")
            observations["after_interposer"] = observation_reader(model_path)
            observed_boundary = observations["after_interposer"].get(
                "observed_monotonic_ns"
            )
            if (
                isinstance(observed_boundary, bool)
                or not isinstance(observed_boundary, int)
                or observed_boundary <= 0
            ):
                raise CarryoverSessionError(
                    "post-interposer observation boundary is invalid"
                )
            observation_finished = observed_boundary
            interposer_finished = max(interposer_finished, observation_finished)
            if interposer_finished + 250_000_000 > target:
                raise CarryoverSessionError(
                    "interposer left no room for the fixed start boundary"
                )
            ordinal += 1
            run_path, measured = invocation_runner(
                session_dir,
                ordinal=ordinal,
                unit_index=unit_index,
                workload="asr",
                diagnostic_role="measured_asr",
                payloads=payloads,
                thermal_monitor=monitor,
                not_before_monotonic_ns=target,
            )
            runs["measured_asr"] = measured
            run_paths["measured_asr"] = run_path.relative_to(session_dir).as_posix()
            timing = validate_fixed_interval(
                runs["primer_2"],
                measured,
                interposer_finished_monotonic_ns=interposer_finished,
            )
            observations["after_measured_asr"] = observation_reader(model_path)
            ordinal += 1
            run_path, recovery = invocation_runner(
                session_dir,
                ordinal=ordinal,
                unit_index=unit_index,
                workload="asr",
                diagnostic_role="recovery_asr",
                payloads=payloads,
                thermal_monitor=monitor,
            )
            runs["recovery_asr"] = recovery
            run_paths["recovery_asr"] = run_path.relative_to(session_dir).as_posix()
            unit = {
                "unit_schema_version": "0.1.0",
                "unit_index": unit_index,
                "interposer": interposer,
                "primer_2_duration_ms": primer_ms,
                "timing": timing,
                "observations": observations,
                "runs": run_paths,
                "measured_asr_duration_ms": _adapter_duration_ms(measured),
                "recovery_asr_duration_ms": _adapter_duration_ms(recovery),
                "valid": True,
                "formal_evidence": False,
            }
            write_json_atomic(unit_dir / "unit.json", unit)
            _append_jsonl(
                ledger_path,
                {
                    "event": "unit_completed",
                    "at": utc_now_iso(),
                    "unit": unit_index,
                    "interposer": interposer,
                },
            )
            manifest["completed_units"] = unit_index
            write_json_atomic(manifest_path, manifest)
        sampler.wait_for_sample_at_or_after(time.monotonic_ns(), timeout_s=2.0)
        report = sampler.stop()
        manifest["resource_sampler_report"] = report.to_dict()
        if not report.successful:
            raise CarryoverSessionError("resource sampler did not close successfully")
        resource_errors = validate_resource_samples(
            load_resource_samples(session_dir / "resources.jsonl")
        )
        if resource_errors:
            raise CarryoverSessionError(
                "resource trace validation failed: " + "; ".join(resource_errors)
            )
        if monitor.stop_requested.is_set():
            raise CarryoverSessionError("thermal stop threshold was reached")
        thermal["stop_requested"] = False
        manifest["status"] = "completed"
        manifest["completed_at"] = utc_now_iso()
        manifest["artifacts"] = _artifact_inventory(session_dir)
        write_json_atomic(manifest_path, manifest)
    except BaseException as exc:
        if sampler is not None and getattr(sampler, "is_running", False):
            try:
                manifest["resource_sampler_report"] = sampler.stop().to_dict()
            except Exception:
                pass
        thermal["stop_requested"] = monitor.stop_requested.is_set()
        manifest["status"] = "aborted"
        manifest["failure_code"] = type(exc).__name__.lower()
        manifest["completed_at"] = utc_now_iso()
        manifest["artifacts"] = _artifact_inventory(session_dir)
        write_json_atomic(manifest_path, manifest)
        raise
    return session_dir


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session-index", type=int, required=True)
    parser.add_argument("--attempt", type=int, default=1)
    parser.add_argument("--collection-id")
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
            print("Phase 1 carryover diagnostic interrupted", file=sys.stderr)
        else:
            print(
                f"Phase 1 carryover diagnostic failed: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
        return 1
    print(session_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
