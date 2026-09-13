"""Run one motion-disabled Phase 2 commissioning session."""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from experiments.phase1.common.manifest import utc_now_iso, write_json_atomic
from experiments.phase1.common.telemetry_jetson import (
    TegrastatsSampler,
    load_resource_samples,
    validate_resource_samples,
)
from experiments.phase1.run_carryover_session import (
    DEFAULT_ASR_INPUT,
    DEFAULT_VLM_INPUT,
)
from experiments.phase1.run_formal_session import ThermalMonitor
from experiments.phase1.workloads.asr.adapter import (
    fixed_asr_payload,
    load_phase0_asr_runtime,
)
from experiments.phase1.workloads.vlm.adapter import fixed_c100_payload
from experiments.phase2.commissioning import (
    COMMISSIONING_ATTEMPT,
    COMMISSIONING_PROTOCOL_ID,
    COMMISSIONING_PROTOCOL_SHA256,
    COMMISSIONING_SESSION_COUNT,
    DEFAULT_COMMISSIONING_PROTOCOL_PATH,
    canonical_commissioning_protocol_text,
    commissioning_conditions,
    commissioning_protocol_sha256,
    load_commissioning_protocol,
)
from experiments.phase2.commissioning_preflight import (
    build_commissioning_preflight,
    commissioning_preflight_errors,
)
from experiments.phase2.observations import capture_boundary_observation
from experiments.phase2.privacy import validate_privacy_boundary
from experiments.phase2.run_correctness_pilot import (
    PilotInfrastructureError,
    PilotSafetyError,
    _append_jsonl,
    _artifact_inventory,
    _resolve_output_root,
    _run_invocation,
    _run_observed_prefetch,
    _run_unit,
)
from jetson.phase1_runtime import PeriodicProbe


COMMISSIONING_SESSION_SCHEMA_VERSION = "0.1.0"
DEFAULT_OUTPUT_ROOT = (
    Path(__file__).resolve().parents[1] / "runs" / "phase2-commissioning"
)
_COLLECTION_RE = re.compile(r"^[0-9]{8}T[0-9]{6}Z_phase2_commissioning_v1$")


class CommissioningSessionError(RuntimeError):
    """One commissioning session cannot produce interpretable evidence."""


class CommissioningInfrastructureError(CommissioningSessionError):
    """A required target facility failed during commissioning."""


class CommissioningSafetyError(CommissioningSessionError):
    """The no-motion or thermal boundary could not be preserved."""


class _SamplerStopReport(Protocol):
    successful: bool

    def to_dict(self) -> dict[str, object]: ...


class _CommissioningSampler(Protocol):
    @property
    def is_running(self) -> bool: ...

    def start(self, *, first_sample_timeout_s: float) -> None: ...

    def wait_for_sample_at_or_after(
        self, monotonic_ns: int, *, timeout_s: float
    ) -> int: ...

    def stop(self) -> _SamplerStopReport: ...


def make_collection_id(now: datetime | None = None) -> str:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return current.astimezone(timezone.utc).strftime(
        "%Y%m%dT%H%M%SZ_phase2_commissioning_v1"
    )


def _prior_session(
    collection_dir: Path, session_index: int
) -> Mapping[str, object] | None:
    directories = (
        sorted(path for path in collection_dir.iterdir() if path.is_dir())
        if collection_dir.exists()
        else []
    )
    if len(directories) != session_index - 1:
        raise CommissioningSessionError(
            "collection does not contain the exact prior commissioning sessions"
        )
    previous: Mapping[str, object] | None = None
    for index, directory in enumerate(directories, start=1):
        if directory.name != f"session-{index:02d}-attempt-01":
            raise CommissioningSessionError(
                "prior commissioning session names are invalid"
            )
        try:
            value = json.loads(
                (directory / "manifest.json").read_text(encoding="utf-8")
            )
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise CommissioningSessionError(
                "a prior commissioning manifest cannot be loaded"
            ) from exc
        if not isinstance(value, Mapping) or value.get("status") != "completed":
            raise CommissioningSessionError(
                "a prior commissioning session is incomplete"
            )
        previous = value
    return previous


def _failure_class(exc: BaseException) -> str:
    if isinstance(exc, (CommissioningInfrastructureError, PilotInfrastructureError)):
        return "infrastructure"
    if isinstance(exc, (CommissioningSafetyError, PilotSafetyError)):
        return "safety"
    return "commissioning_execution"


def run_session(
    args: argparse.Namespace,
    *,
    repo_root: Path | str | None = None,
    preflight_builder: Callable[..., dict[str, object]] = (
        build_commissioning_preflight
    ),
    sampler_factory: Callable[..., Any] = TegrastatsSampler,
    unit_runner: Callable[..., tuple[dict[str, object], int]] = _run_unit,
    invocation_runner: Callable[..., tuple[Path, dict[str, object]]] = (
        _run_invocation
    ),
    observation_reader: Callable[..., object] = capture_boundary_observation,
    treatment_runner: Callable[..., tuple[object, Mapping[str, object]]] = (
        _run_observed_prefetch
    ),
    resource_reader: Callable[[Path | str], Sequence[dict[str, Any]]] = (
        load_resource_samples
    ),
    probe_factory: Callable[..., object] = PeriodicProbe,
    thermal_monitor_factory: Callable[..., ThermalMonitor] = ThermalMonitor,
    payloads_override: Mapping[str, object] | None = None,
    whisper_model_path: Path | str | None = None,
) -> Path:
    """Execute exactly one reviewed, non-replaceable commissioning session."""

    root = (
        Path(repo_root).resolve()
        if repo_root is not None
        else Path(__file__).resolve().parents[2]
    )
    protocol_path = Path(args.protocol).resolve()
    protocol = load_commissioning_protocol(protocol_path)
    if commissioning_protocol_sha256(protocol) != COMMISSIONING_PROTOCOL_SHA256:
        raise CommissioningSessionError("commissioning protocol identity failed")
    if (
        isinstance(args.session_index, bool)
        or not isinstance(args.session_index, int)
        or not 1 <= args.session_index <= COMMISSIONING_SESSION_COUNT
        or args.attempt != COMMISSIONING_ATTEMPT
    ):
        raise CommissioningSessionError(
            "only commissioning sessions 1-2 and attempt 1 are permitted"
        )
    collection_id = args.collection_id or make_collection_id()
    if _COLLECTION_RE.fullmatch(collection_id) is None:
        raise CommissioningSessionError(
            "collection id does not match the commissioning contract"
        )
    if args.session_index > 1 and args.collection_id is None:
        raise CommissioningSessionError(
            "later commissioning sessions require the existing collection id"
        )

    output_root = _resolve_output_root(Path(args.output_root), root)
    collection_dir = output_root / collection_id
    previous = _prior_session(collection_dir, args.session_index)
    session_dir = collection_dir / f"session-{args.session_index:02d}-attempt-01"
    if session_dir.exists():
        raise FileExistsError(
            f"refusing to overwrite commissioning session: {session_dir}"
        )

    preflight = preflight_builder(
        root,
        protocol,
        collection_id=collection_id,
        session_index=args.session_index,
        previous_session=previous,
        asr_input=args.asr_input,
        vlm_input=args.vlm_input,
        services_restarted=args.confirm_services_restarted,
        dynamic_dvfs_confirmed=args.confirm_dynamic_dvfs,
        protocol_path=protocol_path,
    )
    preflight_errors = commissioning_preflight_errors(preflight)
    if preflight_errors:
        raise CommissioningSessionError(
            "commissioning preflight failed: " + "; ".join(preflight_errors)
        )
    validate_privacy_boundary(preflight)

    target = preflight.get("target_preflight")
    target_record = target if isinstance(target, Mapping) else {}
    whisper = target_record.get("whisper_file")
    whisper_record = whisper if isinstance(whisper, Mapping) else {}
    expected_size = whisper_record.get("size_bytes")
    if (
        isinstance(expected_size, bool)
        or not isinstance(expected_size, int)
        or expected_size <= 0
    ):
        raise CommissioningSessionError(
            "commissioning preflight Whisper size is invalid"
        )
    model_path = (
        Path(whisper_model_path or load_phase0_asr_runtime().whisper_model)
        .expanduser()
        .resolve()
    )
    payloads = dict(
        payloads_override
        or {
            "asr": fixed_asr_payload(args.asr_input),
            "vlm": fixed_c100_payload(args.vlm_input),
        }
    )
    if set(payloads) != {"asr", "vlm"}:
        raise CommissioningSessionError("fixed payloads must cover ASR and VLM")
    conditions = commissioning_conditions(protocol, args.session_index)
    parameters = protocol.get("parameters")
    if not isinstance(parameters, Mapping):
        raise CommissioningSessionError("commissioning parameters are missing")

    injected = (
        any(
            value is not default
            for value, default in (
                (preflight_builder, build_commissioning_preflight),
                (sampler_factory, TegrastatsSampler),
                (unit_runner, _run_unit),
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
        canonical_commissioning_protocol_text(protocol),
        encoding="utf-8",
        newline="\n",
    )
    write_json_atomic(session_dir / "preflight.json", preflight)
    ledger_path = session_dir / "ledger.jsonl"
    ledger_path.touch(exist_ok=False)
    manifest_path = session_dir / "manifest.json"
    protocol_identity = preflight.get("protocol")
    identity = protocol_identity if isinstance(protocol_identity, Mapping) else {}
    prior_value = preflight.get("prior_session")
    prior_record = prior_value if isinstance(prior_value, Mapping) else {}
    separation_value = preflight.get("separation")
    separation_record = (
        separation_value if isinstance(separation_value, Mapping) else {}
    )
    manifest: dict[str, Any] = {
        "commissioning_session_schema_version": (COMMISSIONING_SESSION_SCHEMA_VERSION),
        "artifact_kind": "phase2_commissioning_session",
        "collection_id": collection_id,
        "session_id": session_dir.name,
        "session_index": args.session_index,
        "attempt": COMMISSIONING_ATTEMPT,
        "protocol_id": COMMISSIONING_PROTOCOL_ID,
        "protocol_sha256": COMMISSIONING_PROTOCOL_SHA256,
        "conditions": list(conditions),
        "status": "running",
        "failure_class": None,
        "failure_code": None,
        "created_at": utc_now_iso(),
        "completed_at": None,
        "formal_evidence": False,
        "confirmatory_data": False,
        "application_slice_authorized": False,
        "development_injection": injected,
        "preflight": {
            "captured_at": preflight.get("captured_at"),
            "protocol_commit": identity.get("protocol_commit"),
            "runner_commit": identity.get("runner_commit"),
            "service_identity": preflight.get("service_identity"),
            "prior_session_index": prior_record.get("session_index"),
            "separation_s": separation_record.get("observed_s"),
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
    validate_privacy_boundary(manifest)
    write_json_atomic(manifest_path, manifest)

    monitor = thermal_monitor_factory(stop_tj_c=float(parameters["thermal_stop_tj_c"]))
    sampler: _CommissioningSampler | None = None
    ordinal = 0
    try:
        try:
            sampler = sampler_factory(
                session_dir,
                int(parameters["resource_interval_ms"]),
                sample_callback=monitor.observe,
            )
            assert sampler is not None
            sampler.start(first_sample_timeout_s=5.0)
        except BaseException as exc:
            raise CommissioningInfrastructureError(
                "commissioning resource sampler could not start"
            ) from exc
        try:
            manifest["thermal"]["readiness"] = monitor.wait_below(
                maximum_tj_c=float(parameters["thermal_start_maximum_tj_c"]),
                consecutive_samples=int(
                    parameters["thermal_start_consecutive_samples"]
                ),
                timeout_s=args.thermal_wait_timeout_s,
            )
        except BaseException as exc:
            raise CommissioningSafetyError(
                "commissioning thermal start requirement was not met"
            ) from exc
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
            unit, ordinal = unit_runner(
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
                    "status": unit.get("status"),
                },
            )
            manifest["completed_units"] = unit_index
            write_json_atomic(manifest_path, manifest)

        try:
            assert sampler is not None
            sampler.wait_for_sample_at_or_after(time.monotonic_ns(), timeout_s=2.0)
            stop_report = sampler.stop()
        except BaseException as exc:
            raise CommissioningInfrastructureError(
                "commissioning resource sampler could not close"
            ) from exc
        manifest["resource_sampler_report"] = stop_report.to_dict()
        if not stop_report.successful:
            raise CommissioningInfrastructureError(
                "commissioning resource sampler closure was incomplete"
            )
        resource_errors = validate_resource_samples(
            list(resource_reader(session_dir / "resources.jsonl"))
        )
        if resource_errors:
            raise CommissioningInfrastructureError(
                "commissioning resource trace failed validation"
            )
        if monitor.stop_requested.is_set():
            raise CommissioningSafetyError(
                "commissioning thermal stop threshold was reached"
            )
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
    parser.add_argument("--session-index", type=int, required=True)
    parser.add_argument("--attempt", type=int, default=COMMISSIONING_ATTEMPT)
    parser.add_argument("--collection-id")
    parser.add_argument(
        "--protocol", type=Path, default=DEFAULT_COMMISSIONING_PROTOCOL_PATH
    )
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
        session_dir = run_session(args)
    except BaseException as exc:
        if isinstance(exc, KeyboardInterrupt):
            print("Phase 2 commissioning interrupted", file=sys.stderr)
        else:
            print(
                f"Phase 2 commissioning failed: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
        return 1
    print(session_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
