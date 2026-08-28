"""Run one motion-disabled Phase 1 simulated-load pilot on Jetson."""

from __future__ import annotations

import argparse
import sys
import threading
from pathlib import Path
from typing import Callable

from experiments.phase1.jetson_preflight import (
    build_jetson_preflight,
    preflight_errors,
)
from experiments.phase1.jetson_telemetry import (
    TegrastatsSampler,
    load_resource_samples,
)
from experiments.phase1.manifest import (
    sha256_file,
    utc_now_iso,
    write_json_atomic,
)
from experiments.phase1.pilot import (
    PILOT_MANIFEST_SCHEMA_VERSION,
    PilotError,
    build_pilot_plan,
    build_pilot_summary,
    make_pilot_session_id,
    validate_pilot_dir,
    validate_pilot_session_id,
)
from experiments.phase1.run_simulation import run_once


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
        raise PilotError("Phase 1 pilot output must not be inside experiments/phase0")
    if _is_relative_to(resolved, repo_root):
        ignored_root = (repo_root / "experiments" / "runs").resolve()
        if not _is_relative_to(resolved, ignored_root):
            raise PilotError(
                "repository-local pilot output must be inside experiments/runs"
            )
    return resolved


def _artifact_identity(path: Path) -> dict[str, object]:
    return {
        "size_bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def _run_args(
    entry: dict[str, object],
    *,
    plan: dict[str, object],
    output_root: Path,
) -> argparse.Namespace:
    scenario = plan["scenario"]
    if not isinstance(scenario, dict):
        raise PilotError("pilot scenario configuration is missing")
    return argparse.Namespace(
        condition=entry["condition"],
        service_time_s=entry["service_time_s"],
        prelude_s=scenario["prelude_s"],
        postlude_s=scenario["postlude_s"],
        probe_period_ms=scenario["probe_period_ms"],
        probe_deadline_ms=scenario["probe_deadline_ms"],
        pending_capacity=scenario["pending_capacity"],
        result_capacity=scenario["result_capacity"],
        overflow_submissions=scenario["overflow_submissions"],
        adapter_poll_interval_s=scenario["adapter_poll_interval_s"],
        join_timeout_s=scenario["join_timeout_s"],
        session_id=plan["session_id"],
        repetition=entry["sequence"],
        output_root=output_root,
        allow_dirty=False,
    )


def run_pilot_session(
    args: argparse.Namespace,
    *,
    repo_root: Path | str | None = None,
    preflight_builder: Callable[..., dict[str, object]] = build_jetson_preflight,
    sampler_factory: Callable[..., TegrastatsSampler] = TegrastatsSampler,
) -> Path:
    """Execute one predeclared pilot and preserve atomic session evidence."""

    root = (
        Path(repo_root).resolve()
        if repo_root is not None
        else Path(__file__).resolve().parents[2]
    )
    session_id = validate_pilot_session_id(args.session_id or make_pilot_session_id())
    plan = build_pilot_plan(
        session_id=session_id,
        service_times_s=args.service_times_s,
        repetitions=args.repetitions,
        correctness_service_time_s=args.correctness_service_time_s,
        prelude_s=args.prelude_s,
        postlude_s=args.postlude_s,
        probe_period_ms=args.probe_period_ms,
        probe_deadline_ms=args.probe_deadline_ms,
        pending_capacity=args.pending_capacity,
        result_capacity=args.result_capacity,
        overflow_submissions=args.overflow_submissions,
        adapter_poll_interval_s=args.adapter_poll_interval_s,
        join_timeout_s=args.join_timeout_s,
        resource_interval_ms=args.resource_interval_ms,
        resource_tail_s=args.resource_tail_s,
    )
    preflight = preflight_builder(root, expected_branch="main")
    failed_checks = preflight_errors(preflight)
    if failed_checks:
        raise PilotError("Jetson preflight failed: " + "; ".join(failed_checks))

    output_root = _resolve_output_root(Path(args.output_root), root)
    session_dir = output_root / session_id
    if session_dir.exists():
        raise FileExistsError(f"refusing to overwrite pilot session: {session_dir}")
    session_dir.mkdir(parents=True)
    plan_path = session_dir / "pilot_plan.json"
    preflight_path = session_dir / "preflight.json"
    manifest_path = session_dir / "session_manifest.json"
    write_json_atomic(plan_path, plan)
    write_json_atomic(preflight_path, preflight)

    manifest: dict[str, object] = {
        "pilot_manifest_schema_version": PILOT_MANIFEST_SCHEMA_VERSION,
        "artifact_kind": "phase1_jetson_simulation_pilot",
        "session_id": session_id,
        "status": "running",
        "created_at": utc_now_iso(),
        "completed_at": None,
        "descriptive_only": True,
        "inference_claim_permitted": False,
        "runs": [],
        "resource_sampler_report": None,
        "artifacts": {},
        "failure_code": None,
        "cleanup_error_code": None,
    }
    write_json_atomic(manifest_path, manifest)

    sampler: TegrastatsSampler | None = None
    run_records: list[dict[str, object]] = []
    try:
        resource_config = plan["resource_telemetry"]
        if not isinstance(resource_config, dict):
            raise PilotError("pilot resource configuration is missing")
        sampler = sampler_factory(
            session_dir,
            int(resource_config["interval_ms"]),
        )
        sampler.start(first_sample_timeout_s=args.resource_first_sample_timeout_s)
        planned_runs = plan["runs"]
        if not isinstance(planned_runs, list):
            raise PilotError("pilot plan contains no run matrix")
        for entry in planned_runs:
            if not isinstance(entry, dict):
                raise PilotError("pilot plan run is not an object")
            run_dir = run_once(
                _run_args(entry, plan=plan, output_root=output_root),
                repo_root=root,
            )
            run_manifest_path = run_dir / "manifest.json"
            record = dict(entry)
            record.update(
                {
                    "run_id": run_dir.name,
                    "relative_path": run_dir.relative_to(session_dir).as_posix(),
                    "manifest_identity": _artifact_identity(run_manifest_path),
                }
            )
            run_records.append(record)
            manifest["runs"] = run_records
            write_json_atomic(manifest_path, manifest)

        tail_s = float(resource_config["tail_s"])
        if tail_s > 0:
            threading.Event().wait(tail_s)
        sampler_report = sampler.stop()
        manifest["resource_sampler_report"] = sampler_report.to_dict()
        if not sampler_report.successful:
            raise PilotError("tegrastats did not produce a valid closed resource trace")

        samples = load_resource_samples(session_dir / "resources.jsonl")
        summary = build_pilot_summary(
            session_dir,
            plan=plan,
            preflight=preflight,
            run_records=run_records,
            sampler_report=sampler_report.to_dict(),
            samples=samples,
        )
        summary_path = session_dir / "pilot_summary.json"
        write_json_atomic(summary_path, summary)
        if summary.get("valid") is not True:
            raise PilotError("one or more Jetson pilot Gates failed")

        manifest["artifacts"] = {
            name: _artifact_identity(session_dir / name)
            for name in (
                "pilot_plan.json",
                "preflight.json",
                "resources.jsonl",
                "pilot_summary.json",
            )
        }
        manifest["status"] = "completed"
        manifest["completed_at"] = utc_now_iso()
        write_json_atomic(manifest_path, manifest)
        validation_errors = validate_pilot_dir(session_dir)
        if validation_errors:
            raise PilotError(
                "final pilot validation failed: " + "; ".join(validation_errors)
            )
    except Exception as exc:
        if sampler is not None and sampler.is_running:
            try:
                sampler.stop()
            except Exception as cleanup_exc:
                manifest["cleanup_error_code"] = type(cleanup_exc).__name__.lower()
        if sampler is not None and sampler.stop_report is not None:
            manifest["resource_sampler_report"] = sampler.stop_report.to_dict()
        manifest["status"] = "failed"
        manifest["completed_at"] = utc_now_iso()
        manifest["failure_code"] = type(exc).__name__.lower()
        write_json_atomic(manifest_path, manifest)
        raise
    return session_dir


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--service-times-s",
        type=float,
        nargs="+",
        required=True,
        help="predeclared simulated service durations for the R0--R3 blocks",
    )
    parser.add_argument("--correctness-service-time-s", type=float)
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--prelude-s", type=float, default=1.0)
    parser.add_argument("--postlude-s", type=float, default=1.0)
    parser.add_argument("--probe-period-ms", type=float, default=100.0)
    parser.add_argument("--probe-deadline-ms", type=float, default=100.0)
    parser.add_argument("--pending-capacity", type=int, default=1)
    parser.add_argument("--result-capacity", type=int, default=1)
    parser.add_argument("--overflow-submissions", type=int, default=2)
    parser.add_argument("--adapter-poll-interval-s", type=float, default=0.01)
    parser.add_argument("--join-timeout-s", type=float, default=5.0)
    parser.add_argument("--resource-interval-ms", type=int, default=200)
    parser.add_argument("--resource-tail-s", type=float, default=0.4)
    parser.add_argument("--resource-first-sample-timeout-s", type=float, default=3.0)
    parser.add_argument("--session-id")
    parser.add_argument(
        "--output-root",
        type=Path,
        default=Path("experiments/runs/phase1-jetson-pilot"),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        session_dir = run_pilot_session(args)
    except Exception as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 2
    print(session_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
