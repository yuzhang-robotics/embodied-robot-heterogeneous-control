"""Run one fixed-input, motion-disabled Phase 1 LLM condition on Jetson."""

from __future__ import annotations

import argparse
import math
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
from experiments.phase1.telemetry import EventRecorder, SCHEMA_VERSION

from .llm_adapter import (
    LLM_EMPTY_HISTORY_SHA256,
    LLM_EXPECTED_SERVED_MODEL_ID,
    LLM_MODEL_SHA256,
    LLM_MODEL_SIZE_BYTES,
    LLM_SERVER_ARGUMENTS,
    FixedInputLLMAdapter,
    fixed_llm_payload,
    llm_request_contract,
)
from .llm_preflight import build_llm_preflight, llm_preflight_errors
from .llm_slice import LLMSliceCondition, LLMSliceSpec, run_llm_slice
from .summarize_llm_slice import build_llm_summary


DEFAULT_INPUT = (
    Path(__file__).resolve().parents[1] / "phase0" / "inputs" / "llm_prompt_zh.txt"
)
DEFAULT_OUTPUT_ROOT = Path(__file__).resolve().parents[1] / "runs" / "phase1-llm-slice"
_SESSION_ID_RE = re.compile(r"^[0-9]{8}T[0-9]{6}Z_phase1_llm_[a-z][a-z0-9_-]{0,31}$")


class LLMRunError(RuntimeError):
    """The real-workload run could not produce valid closed evidence."""


def make_llm_run_id(
    condition: LLMSliceCondition,
    repetition: int,
    *,
    now: datetime | None = None,
) -> str:
    if not isinstance(condition, LLMSliceCondition):
        raise TypeError("condition must be an LLMSliceCondition")
    if (
        isinstance(repetition, bool)
        or not isinstance(repetition, int)
        or not 0 <= repetition <= 999
    ):
        raise ValueError("repetition must be an integer from 0 to 999")
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    stamp = current.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}_phase1_{condition.value}_llm_{repetition:03d}"


def make_llm_session_id(now: datetime | None = None) -> str:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    stamp = current.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}_phase1_llm_slice"


def _validate_session_id(value: str) -> str:
    if not isinstance(value, str) or not _SESSION_ID_RE.fullmatch(value):
        raise ValueError(
            "session_id must use YYYYMMDDTHHMMSSZ_phase1_llm_<lowercase-label>"
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
        raise LLMRunError("Phase 1 LLM output must not be inside experiments/phase0")
    if _is_relative_to(resolved, repo_root):
        ignored_root = (repo_root / "experiments" / "runs").resolve()
        if not _is_relative_to(resolved, ignored_root):
            raise LLMRunError(
                "repository-local LLM output must be inside experiments/runs"
            )
    return resolved


def _artifact_identity(path: Path) -> dict[str, object]:
    return {"size_bytes": path.stat().st_size, "sha256": sha256_file(path)}


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
    """Execute one preflighted LLM request and independently validate it."""

    root = (
        Path(repo_root).resolve()
        if repo_root is not None
        else Path(__file__).resolve().parents[2]
    )
    condition = LLMSliceCondition(args.condition)
    if args.adapter_request_timeout_s >= args.completion_timeout_s:
        raise LLMRunError(
            "adapter request timeout must be below the slice completion timeout"
        )
    if not math.isfinite(args.stale_observation_s) or args.stale_observation_s <= 0:
        raise LLMRunError("stale observation window must be positive and finite")
    if condition is LLMSliceCondition.STALE:
        resource_interval_s = args.resource_interval_ms / 1000.0
        if args.stale_observation_s <= resource_interval_s:
            raise LLMRunError(
                "stale observation window must exceed the resource interval"
            )
        if args.stale_observation_s >= args.adapter_request_timeout_s:
            raise LLMRunError(
                "stale observation window must be below the adapter timeout"
            )
        if args.stale_observation_s >= args.completion_timeout_s:
            raise LLMRunError(
                "stale observation window must be below the completion timeout"
            )
    session_id = _validate_session_id(args.session_id or make_llm_session_id())
    injected_components = [
        name
        for name, value in (
            ("preflight_builder", preflight_builder),
            ("sampler_factory", sampler_factory),
            ("adapter_factory", adapter_factory),
        )
        if value is not None
    ]
    resolved_preflight_builder = preflight_builder or build_llm_preflight
    resolved_sampler_factory = sampler_factory or TegrastatsSampler
    safety = require_motion_disabled()
    payload = fixed_llm_payload(args.input)
    preflight = resolved_preflight_builder(
        root,
        input_payload=payload,
        expected_branch="main",
    )
    failed_checks = llm_preflight_errors(preflight)
    if failed_checks:
        raise LLMRunError("LLM preflight failed: " + "; ".join(failed_checks))

    output_root = _resolve_output_root(Path(args.output_root), root)
    run_id = make_llm_run_id(condition, args.repetition)
    run_dir = output_root / session_id / condition.value / run_id
    if run_dir.exists():
        raise FileExistsError(f"refusing to overwrite LLM run: {run_dir}")
    run_dir.mkdir(parents=True)
    preflight_path = run_dir / "preflight.json"
    manifest_path = run_dir / "manifest.json"
    write_json_atomic(preflight_path, preflight)
    completed_artifact_names = {"preflight.json"}

    spec = LLMSliceSpec(
        condition=condition,
        result_validity_s=args.result_validity_s,
        completion_timeout_s=args.completion_timeout_s,
        join_timeout_s=args.join_timeout_s,
        probe_join_timeout_s=args.probe_join_timeout_s,
        prelude_s=args.prelude_s,
        postlude_s=args.postlude_s,
        stale_observation_s=args.stale_observation_s,
        probe_period_ns=int(args.probe_period_ms * 1_000_000),
        probe_deadline_ns=int(args.probe_deadline_ms * 1_000_000),
    )
    spec_record = spec.to_dict()
    spec_record["adapter_request_timeout_s"] = args.adapter_request_timeout_s
    environment = collect_environment(root)
    runtime = preflight.get("runtime")
    runtime_record = runtime if isinstance(runtime, dict) else {}
    manifest: dict[str, object] = {
        "manifest_schema_version": MANIFEST_SCHEMA_VERSION,
        "event_schema_version": SCHEMA_VERSION,
        "artifact_kind": "phase1_fixed_input_llm_run",
        "run_id": run_id,
        "session_id": session_id,
        "condition": condition.value,
        "adapter_isolation": "llama_http_client_thread",
        "trace_profile": "runtime_threaded_probe",
        "status": "running",
        "created_at": utc_now_iso(),
        "completed_at": None,
        "descriptive_only": True,
        "formal_performance_claim_permitted": False,
        "cancellation_latency_claim_permitted": False,
        "backend_cancellation_claim_permitted": False,
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
            "raw_text_recorded": False,
        },
        "workload_contract": {
            "source": "llama_cpp_openai_http",
            "model": {
                "sha256": LLM_MODEL_SHA256,
                "size_bytes": LLM_MODEL_SIZE_BYTES,
                "served_model_id": LLM_EXPECTED_SERVED_MODEL_ID,
            },
            "source_version": runtime_record.get("source_version"),
            "server_arguments": dict(LLM_SERVER_ARGUMENTS),
            "request": llm_request_contract(),
            "history": {
                "sha256": LLM_EMPTY_HISTORY_SHA256,
                "messages": 0,
                "raw_history_recorded": False,
            },
            "residency_policy": "external_llama_server_resident",
            "raw_prompt_recorded": False,
            "raw_output_recorded": False,
        },
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
        adapter = (
            adapter_factory()
            if adapter_factory is not None
            else FixedInputLLMAdapter(
                request_timeout_s=args.adapter_request_timeout_s,
            )
        )
        report = run_llm_slice(spec, payload, recorder, adapter=adapter)
        recorder.close()
        recorder = None
        completed_artifact_names.add("events.jsonl")
        sampler_report = sampler.stop()
        completed_artifact_names.add("resources.jsonl")
        manifest["resource_sampler_report"] = sampler_report.to_dict()
        if not sampler_report.successful:
            raise LLMRunError("tegrastats did not produce a valid closed trace")

        report_record = report.to_dict()
        scenario = {"spec": spec_record, "report": report_record}
        scenario_path = run_dir / "scenario.json"
        write_json_atomic(scenario_path, scenario)
        completed_artifact_names.add("scenario.json")
        samples = load_resource_samples(run_dir / "resources.jsonl")
        summary = build_llm_summary(
            run_dir / "events.jsonl",
            condition=condition,
            spec=spec_record,
            report=report_record,
            resource_samples=samples,
            sampler_report=sampler_report.to_dict(),
            development_injection=bool(injected_components),
        )
        summary_path = run_dir / "summary.json"
        write_json_atomic(summary_path, summary)
        completed_artifact_names.add("summary.json")
        if summary.get("valid") is not True:
            raise LLMRunError("one or more LLM slice Gates failed")

        artifact_names = [
            "preflight.json",
            "events.jsonl",
            "resources.jsonl",
            "scenario.json",
            "summary.json",
        ]
        manifest["artifacts"] = {
            name: _artifact_identity(run_dir / name) for name in artifact_names
        }
        manifest["status"] = "completed"
        manifest["completed_at"] = utc_now_iso()
        write_json_atomic(manifest_path, manifest)

        from experiments.phase1.validate_llm_slice import validate_llm_slice_dir

        validation_errors = validate_llm_slice_dir(run_dir)
        if validation_errors:
            raise LLMRunError(
                "final LLM validation failed: " + "; ".join(validation_errors)
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
        choices=[condition.value for condition in LLMSliceCondition],
        required=True,
    )
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--session-id")
    parser.add_argument("--repetition", type=int, default=1)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--result-validity-s", type=float, default=180.0)
    parser.add_argument("--completion-timeout-s", type=float, default=150.0)
    parser.add_argument("--join-timeout-s", type=float, default=10.0)
    parser.add_argument("--probe-join-timeout-s", type=float, default=5.0)
    parser.add_argument("--prelude-s", type=float, default=1.0)
    parser.add_argument("--postlude-s", type=float, default=1.0)
    parser.add_argument("--stale-observation-s", type=float, default=0.5)
    parser.add_argument("--probe-period-ms", type=float, default=100.0)
    parser.add_argument("--probe-deadline-ms", type=float, default=100.0)
    parser.add_argument("--resource-interval-ms", type=int, default=200)
    parser.add_argument("--resource-first-sample-timeout-s", type=float, default=5.0)
    parser.add_argument("--adapter-request-timeout-s", type=float, default=120.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        run_dir = run_once(args)
    except Exception as exc:
        print(f"Phase 1 LLM run failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(run_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
