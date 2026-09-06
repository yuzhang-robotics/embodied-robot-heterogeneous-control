"""Run one fixed-input, motion-disabled Phase 1 VLM condition on Jetson."""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from experiments.phase1.jetson_telemetry import (
    TegrastatsSampler,
    load_resource_samples,
)
from experiments.phase1.manifest import (
    MANIFEST_SCHEMA_VERSION,
    collect_environment,
    require_motion_disabled,
    sha256_file,
    utc_now_iso,
    write_json_atomic,
)
from experiments.phase1.summarize_vlm_slice import build_vlm_summary
from experiments.phase1.summarize_vlm_process_slice import (
    VLM_PROCESS_ISOLATION,
    build_vlm_process_summary,
)
from experiments.phase1.telemetry import EventRecorder, SCHEMA_VERSION
from experiments.phase1.vlm_adapter import FixedInputVLMAdapter, fixed_c100_payload
from experiments.phase1.vlm_preflight import (
    build_vlm_preflight,
    vlm_preflight_errors,
)
from experiments.phase1.vlm_process_adapter import ProcessIsolatedVLMAdapter
from experiments.phase1.vlm_slice import (
    VLMSliceCondition,
    VLMSliceSpec,
    run_vlm_slice,
)
from jetson.vlm_request_contract import current_vlm_workload_contract


DEFAULT_INPUT = (
    Path(__file__).resolve().parents[1]
    / "raw"
    / "phase0-inputs"
    / "vlm"
    / "c100-camera-product.jpg"
)
DEFAULT_OUTPUT_ROOT = Path(__file__).resolve().parents[1] / "runs" / "phase1-vlm-slice"
_SESSION_ID_RE = re.compile(r"^[0-9]{8}T[0-9]{6}Z_phase1_vlm_[a-z][a-z0-9_-]{0,31}$")
THREAD_ISOLATION = "thread"


class VLMRunError(RuntimeError):
    """The real-workload run could not produce valid closed evidence."""


def make_vlm_run_id(
    condition: VLMSliceCondition,
    repetition: int,
    *,
    adapter_isolation: str = THREAD_ISOLATION,
    now: datetime | None = None,
) -> str:
    if not isinstance(condition, VLMSliceCondition):
        raise TypeError("condition must be a VLMSliceCondition")
    if (
        isinstance(repetition, bool)
        or not isinstance(repetition, int)
        or not 0 <= repetition <= 999
    ):
        raise ValueError("repetition must be an integer from 0 to 999")
    if adapter_isolation not in {THREAD_ISOLATION, VLM_PROCESS_ISOLATION}:
        raise ValueError("adapter_isolation must be thread or spawned_process")
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    stamp = current.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    condition_label = condition.value
    if adapter_isolation == VLM_PROCESS_ISOLATION:
        condition_label = condition_label.replace("vlm_", "vlm_process_", 1)
    return f"{stamp}_phase1_{condition_label}_vlm_{repetition:03d}"


def make_vlm_session_id(now: datetime | None = None) -> str:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    stamp = current.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}_phase1_vlm_slice"


def _validate_session_id(value: str) -> str:
    if not isinstance(value, str) or not _SESSION_ID_RE.fullmatch(value):
        raise ValueError(
            "session_id must use YYYYMMDDTHHMMSSZ_phase1_vlm_<lowercase-label>"
        )
    return value


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _resolve_output_root(output_root: Path, repo_root: Path) -> Path:
    expanded = output_root.expanduser()
    resolved = (expanded if expanded.is_absolute() else repo_root / expanded).resolve()
    phase0_source = (repo_root / "experiments" / "phase0").resolve()
    if _is_relative_to(resolved, phase0_source):
        raise VLMRunError("Phase 1 VLM output must not be inside experiments/phase0")
    if _is_relative_to(resolved, repo_root):
        ignored_root = (repo_root / "experiments" / "runs").resolve()
        if not _is_relative_to(resolved, ignored_root):
            raise VLMRunError(
                "repository-local VLM output must be inside experiments/runs"
            )
    return resolved


def _artifact_identity(path: Path) -> dict[str, object]:
    return {
        "size_bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def _completed_artifact_identities(
    run_dir: Path,
    completed_names: set[str],
) -> dict[str, object]:
    names = (
        "preflight.json",
        "events.jsonl",
        "resources.jsonl",
        "scenario.json",
        "summary.json",
        "process.json",
    )
    return {
        name: _artifact_identity(run_dir / name)
        for name in names
        if name in completed_names
    }


def _reproducibility(
    environment: dict[str, object],
    *,
    injected_components: list[str],
) -> dict[str, object]:
    git = environment.get("git")
    record = git if isinstance(git, dict) else {}
    identity_complete = (
        not record.get("error_codes")
        and bool(record.get("commit"))
        and bool(record.get("branch"))
    )
    git_clean = identity_complete and record.get("dirty") is False
    synchronized_main = (
        git_clean
        and record.get("branch") == "main"
        and record.get("upstream") == "origin/main"
        and record.get("upstream_commit") == record.get("commit")
        and str(record.get("ahead_behind", "")).split() == ["0", "0"]
    )
    return {
        "git_identity_complete": identity_complete,
        "git_clean": git_clean,
        "synchronized_main": synchronized_main,
        "development_injection": bool(injected_components),
        "injected_components": injected_components,
        "formal_evidence_eligible": False,
    }


def run_once(
    args: argparse.Namespace,
    *,
    repo_root: Path | str | None = None,
    preflight_builder: Callable[..., dict[str, object]] | None = None,
    sampler_factory: Callable[..., TegrastatsSampler] | None = None,
    adapter_factory: Callable[[], object] | None = None,
) -> Path:
    """Execute one preflighted VLM request and independently validate it."""

    root = (
        Path(repo_root).resolve()
        if repo_root is not None
        else Path(__file__).resolve().parents[2]
    )
    condition = VLMSliceCondition(args.condition)
    adapter_isolation = args.adapter_isolation
    if adapter_isolation not in {THREAD_ISOLATION, VLM_PROCESS_ISOLATION}:
        raise VLMRunError("unsupported VLM adapter isolation")
    if (
        adapter_isolation == VLM_PROCESS_ISOLATION
        and args.process_execution_timeout_s >= args.completion_timeout_s
    ):
        raise VLMRunError(
            "process execution timeout must be below the slice completion timeout"
        )
    session_id = _validate_session_id(args.session_id or make_vlm_session_id())
    injected_components = [
        name
        for name, value in (
            ("preflight_builder", preflight_builder),
            ("sampler_factory", sampler_factory),
            ("adapter_factory", adapter_factory),
        )
        if value is not None
    ]
    resolved_preflight_builder = preflight_builder or build_vlm_preflight
    resolved_sampler_factory = sampler_factory or TegrastatsSampler
    safety = require_motion_disabled()
    payload = fixed_c100_payload(args.input)
    preflight = resolved_preflight_builder(
        root,
        input_payload=payload,
        expected_branch="main",
    )
    failed_checks = vlm_preflight_errors(preflight)
    if failed_checks:
        raise VLMRunError("VLM preflight failed: " + "; ".join(failed_checks))

    output_root = _resolve_output_root(Path(args.output_root), root)
    run_id = make_vlm_run_id(
        condition,
        args.repetition,
        adapter_isolation=adapter_isolation,
    )
    condition_label = (
        condition.value
        if adapter_isolation == THREAD_ISOLATION
        else condition.value.replace("vlm_", "vlm_process_", 1)
    )
    run_dir = output_root / session_id / condition_label / run_id
    if run_dir.exists():
        raise FileExistsError(f"refusing to overwrite VLM run: {run_dir}")
    run_dir.mkdir(parents=True)
    preflight_path = run_dir / "preflight.json"
    manifest_path = run_dir / "manifest.json"
    write_json_atomic(preflight_path, preflight)
    completed_artifact_names = {"preflight.json"}

    spec = VLMSliceSpec(
        condition=condition,
        result_validity_s=args.result_validity_s,
        completion_timeout_s=args.completion_timeout_s,
        join_timeout_s=args.join_timeout_s,
        probe_join_timeout_s=args.probe_join_timeout_s,
        prelude_s=args.prelude_s,
        postlude_s=args.postlude_s,
        probe_period_ns=int(args.probe_period_ms * 1_000_000),
        probe_deadline_ns=int(args.probe_deadline_ms * 1_000_000),
    )
    spec_record = spec.to_dict()
    spec_record["adapter_isolation"] = adapter_isolation
    environment = collect_environment(root)
    manifest: dict[str, object] = {
        "manifest_schema_version": MANIFEST_SCHEMA_VERSION,
        "event_schema_version": SCHEMA_VERSION,
        "artifact_kind": (
            "phase1_fixed_input_vlm_run"
            if adapter_isolation == THREAD_ISOLATION
            else "phase1_fixed_input_vlm_process_run"
        ),
        "run_id": run_id,
        "session_id": session_id,
        "condition": condition.value,
        "adapter_isolation": adapter_isolation,
        "trace_profile": "runtime_threaded_probe",
        "status": "running",
        "created_at": utc_now_iso(),
        "completed_at": None,
        "descriptive_only": True,
        "formal_performance_claim_permitted": False,
        "heterogeneous_inference_claim_permitted": False,
        "safety": safety,
        "environment": environment,
        "reproducibility": _reproducibility(
            environment,
            injected_components=injected_components,
        ),
        "input": {
            "sha256": payload.sha256,
            "size_bytes": payload.size_bytes,
            "media_type": payload.media_type,
            "path_recorded": False,
        },
        "workload_contract": current_vlm_workload_contract(),
        "spec": spec_record,
        "resource_interval_ms": args.resource_interval_ms,
        "resource_sampler_report": None,
        "artifacts": {},
        "failure_code": None,
        "cleanup_error_code": None,
    }
    write_json_atomic(manifest_path, manifest)

    sampler: TegrastatsSampler | None = None
    recorder: EventRecorder | None = None
    try:
        sampler = resolved_sampler_factory(run_dir, args.resource_interval_ms)
        sampler.start(first_sample_timeout_s=args.resource_first_sample_timeout_s)
        recorder = EventRecorder(run_dir, run_id)
        if adapter_factory is not None:
            adapter = adapter_factory()
        elif adapter_isolation == VLM_PROCESS_ISOLATION:
            adapter = ProcessIsolatedVLMAdapter(
                execution_timeout_s=args.process_execution_timeout_s,
                poll_interval_s=args.process_poll_interval_s,
                join_timeout_s=args.process_join_timeout_s,
                terminate_join_timeout_s=args.process_terminate_timeout_s,
            )
        else:
            adapter = FixedInputVLMAdapter()
        report = run_vlm_slice(
            spec,
            payload,
            recorder,
            adapter=adapter,
        )
        recorder.close()
        recorder = None
        completed_artifact_names.add("events.jsonl")
        sampler_report = sampler.stop()
        completed_artifact_names.add("resources.jsonl")
        manifest["resource_sampler_report"] = sampler_report.to_dict()
        if not sampler_report.successful:
            raise VLMRunError("tegrastats did not produce a valid closed trace")

        scenario: dict[str, object] = {
            "spec": spec_record,
            "report": report.to_dict(),
        }
        process_summary: dict[str, object] | None = None
        if adapter_isolation == VLM_PROCESS_ISOLATION:
            process_report = getattr(adapter, "last_process_report", None)
            if process_report is None or not callable(
                getattr(process_report, "to_dict", None)
            ):
                raise VLMRunError("process adapter did not publish supervisor facts")
            process_record = process_report.to_dict()
            scenario["process"] = process_record
            process_summary = build_vlm_process_summary(
                process_record,
                condition=condition,
            )
        scenario_path = run_dir / "scenario.json"
        write_json_atomic(scenario_path, scenario)
        completed_artifact_names.add("scenario.json")
        if process_summary is not None:
            write_json_atomic(run_dir / "process.json", process_summary)
            completed_artifact_names.add("process.json")
        samples = load_resource_samples(run_dir / "resources.jsonl")
        summary = build_vlm_summary(
            run_dir / "events.jsonl",
            condition=condition,
            spec=spec_record,
            report=report.to_dict(),
            resource_samples=samples,
            sampler_report=sampler_report.to_dict(),
            development_injection=bool(injected_components),
        )
        summary_path = run_dir / "summary.json"
        write_json_atomic(summary_path, summary)
        completed_artifact_names.add("summary.json")
        if process_summary is not None and process_summary.get("valid") is not True:
            raise VLMRunError("one or more VLM process Gates failed")
        if summary.get("valid") is not True:
            raise VLMRunError("one or more VLM slice Gates failed")

        artifact_names = [
            "preflight.json",
            "events.jsonl",
            "resources.jsonl",
            "scenario.json",
            "summary.json",
        ]
        if process_summary is not None:
            artifact_names.append("process.json")
        manifest["artifacts"] = {
            name: _artifact_identity(run_dir / name) for name in artifact_names
        }
        manifest["status"] = "completed"
        manifest["completed_at"] = utc_now_iso()
        write_json_atomic(manifest_path, manifest)

        from experiments.phase1.validate_vlm_slice import validate_vlm_slice_dir

        validation_errors = validate_vlm_slice_dir(run_dir)
        if validation_errors:
            raise VLMRunError(
                "final VLM validation failed: " + "; ".join(validation_errors)
            )
    except Exception as exc:
        if recorder is not None:
            recorder.close()
            completed_artifact_names.add("events.jsonl")
        if sampler is not None and sampler.is_running:
            try:
                sampler.stop()
            except Exception as cleanup_exc:
                manifest["cleanup_error_code"] = type(cleanup_exc).__name__.lower()
        if sampler is not None and sampler.stop_report is not None:
            manifest["resource_sampler_report"] = sampler.stop_report.to_dict()
            completed_artifact_names.add("resources.jsonl")
        manifest["artifacts"] = _completed_artifact_identities(
            run_dir,
            completed_artifact_names,
        )
        manifest["status"] = "failed"
        manifest["completed_at"] = utc_now_iso()
        manifest["failure_code"] = type(exc).__name__.lower()
        write_json_atomic(manifest_path, manifest)
        raise
    return run_dir


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--condition",
        choices=[condition.value for condition in VLMSliceCondition],
        required=True,
    )
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--session-id")
    parser.add_argument("--repetition", type=int, default=1)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--result-validity-s", type=float, default=900.0)
    parser.add_argument("--completion-timeout-s", type=float, default=720.0)
    parser.add_argument("--join-timeout-s", type=float, default=720.0)
    parser.add_argument("--probe-join-timeout-s", type=float, default=5.0)
    parser.add_argument("--prelude-s", type=float, default=1.0)
    parser.add_argument("--postlude-s", type=float, default=1.0)
    parser.add_argument("--probe-period-ms", type=float, default=100.0)
    parser.add_argument("--probe-deadline-ms", type=float, default=100.0)
    parser.add_argument("--resource-interval-ms", type=int, default=200)
    parser.add_argument("--resource-first-sample-timeout-s", type=float, default=5.0)
    parser.add_argument(
        "--adapter-isolation",
        choices=(THREAD_ISOLATION, VLM_PROCESS_ISOLATION),
        default=THREAD_ISOLATION,
    )
    parser.add_argument("--process-execution-timeout-s", type=float, default=600.0)
    parser.add_argument("--process-poll-interval-s", type=float, default=0.02)
    parser.add_argument("--process-join-timeout-s", type=float, default=5.0)
    parser.add_argument("--process-terminate-timeout-s", type=float, default=5.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        run_dir = run_once(args)
    except Exception as exc:
        print(f"Phase 1 VLM run failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(run_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
