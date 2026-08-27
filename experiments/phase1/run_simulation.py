"""Run one safe and reproducible Phase 1 simulated-load condition."""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from experiments.phase1.manifest import (
    MANIFEST_SCHEMA_VERSION,
    collect_environment,
    require_motion_disabled,
    sha256_file,
    utc_now_iso,
    write_json_atomic,
)
from experiments.phase1.simulation import (
    ScenarioSpec,
    SimulationCondition,
    run_simulation,
)
from experiments.phase1.summarize_run import build_summary
from experiments.phase1.telemetry import EventRecorder, SCHEMA_VERSION
from experiments.phase1.validate_run import validate_run_dir


_SESSION_ID_RE = re.compile(r"^[0-9]{8}T[0-9]{6}Z_phase1_[a-z][a-z0-9_-]{0,47}$")


class RunError(RuntimeError):
    """The simulation could not produce a complete validated run."""


def make_run_id(
    condition: SimulationCondition,
    repetition: int,
    *,
    now: datetime | None = None,
) -> str:
    if not isinstance(condition, SimulationCondition):
        raise TypeError("condition must be a SimulationCondition")
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
    return f"{stamp}_phase1_{condition.value}_simulated_{repetition:03d}"


def make_session_id(now: datetime | None = None) -> str:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    stamp = current.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}_phase1_simulation"


def _validate_session_id(value: str) -> str:
    if not isinstance(value, str) or not _SESSION_ID_RE.fullmatch(value):
        raise ValueError(
            "session_id must use YYYYMMDDTHHMMSSZ_phase1_<lowercase-label>"
        )
    return value


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _validate_output_root(output_root: Path, repo_root: Path) -> Path:
    expanded = output_root.expanduser()
    resolved = (expanded if expanded.is_absolute() else repo_root / expanded).resolve()
    phase0_source = (repo_root / "experiments" / "phase0").resolve()
    if _is_relative_to(resolved, phase0_source):
        raise RunError("Phase 1 output root must not be inside experiments/phase0")
    return resolved


def _artifact_identity(path: Path) -> dict[str, object]:
    return {
        "size_bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def run_once(
    args: argparse.Namespace,
    *,
    repo_root: Path | str | None = None,
) -> Path:
    root = (
        Path(repo_root).resolve()
        if repo_root is not None
        else Path(__file__).resolve().parents[2]
    )
    condition = SimulationCondition(args.condition)
    session_id = _validate_session_id(args.session_id or make_session_id())
    safety = require_motion_disabled()
    environment = collect_environment(root)
    git = environment.get("git")
    if not args.allow_dirty:
        if (
            not isinstance(git, dict)
            or git.get("error_codes")
            or not git.get("commit")
            or not git.get("branch")
        ):
            raise RunError("a clean Git identity is required for a reproducibility run")
        if git.get("dirty") is True:
            raise RunError("refusing to record a reproducibility run from a dirty tree")
    git_identity_complete = (
        isinstance(git, dict)
        and not git.get("error_codes")
        and bool(git.get("commit"))
        and bool(git.get("branch"))
    )
    git_clean = git_identity_complete and git.get("dirty") is False

    spec = ScenarioSpec(
        condition=condition,
        service_time_s=args.service_time_s,
        prelude_s=args.prelude_s,
        postlude_s=args.postlude_s,
        probe_period_ns=int(args.probe_period_ms * 1_000_000),
        probe_deadline_ns=int(args.probe_deadline_ms * 1_000_000),
        pending_capacity=args.pending_capacity,
        result_capacity=args.result_capacity,
        overflow_submissions=args.overflow_submissions,
        adapter_poll_interval_s=args.adapter_poll_interval_s,
        join_timeout_s=args.join_timeout_s,
    )
    run_id = make_run_id(condition, args.repetition)
    output_root = _validate_output_root(Path(args.output_root), root)
    run_dir = output_root / session_id / condition.value / run_id
    if run_dir.exists():
        raise FileExistsError(f"refusing to overwrite existing run: {run_dir}")
    run_dir.mkdir(parents=True)

    manifest_path = run_dir / "manifest.json"
    manifest: dict[str, object] = {
        "manifest_schema_version": MANIFEST_SCHEMA_VERSION,
        "event_schema_version": SCHEMA_VERSION,
        "artifact_kind": "phase1_simulation_run",
        "run_id": run_id,
        "session_id": session_id,
        "condition": condition.value,
        "trace_profile": spec.trace_profile.value,
        "status": "running",
        "created_at": utc_now_iso(),
        "completed_at": None,
        "descriptive_only": True,
        "safety": safety,
        "environment": environment,
        "reproducibility": {
            "git_identity_complete": git_identity_complete,
            "git_clean": git_clean,
            "development_override": args.allow_dirty,
            "formal_evidence_eligible": (git_clean and not args.allow_dirty),
        },
        "spec": spec.to_dict(),
        "artifacts": {},
        "failure_code": None,
    }
    write_json_atomic(manifest_path, manifest)

    recorder: EventRecorder | None = None
    try:
        recorder = EventRecorder(run_dir, run_id)
        report = run_simulation(spec, recorder)
        recorder.close()
        recorder = None

        scenario = {"spec": spec.to_dict(), "report": report.to_dict()}
        scenario_path = run_dir / "scenario.json"
        write_json_atomic(scenario_path, scenario)
        summary = build_summary(
            run_dir / "events.jsonl",
            condition=condition,
            profile=spec.trace_profile,
            scenario_report=report.to_dict(),
            spec=spec.to_dict(),
        )
        summary_path = run_dir / "summary.json"
        write_json_atomic(summary_path, summary)
        if summary.get("valid") is not True:
            raise RunError("one or more condition Gates failed")

        manifest["artifacts"] = {
            name: _artifact_identity(run_dir / name)
            for name in ("events.jsonl", "scenario.json", "summary.json")
        }
        manifest["status"] = "completed"
        manifest["completed_at"] = utc_now_iso()
        write_json_atomic(manifest_path, manifest)

        validation_errors = validate_run_dir(run_dir)
        if validation_errors:
            raise RunError("final validation failed: " + "; ".join(validation_errors))
    except Exception as exc:
        if recorder is not None:
            recorder.close()
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
        choices=[condition.value for condition in SimulationCondition],
        required=True,
    )
    parser.add_argument("--service-time-s", type=float, required=True)
    parser.add_argument("--prelude-s", type=float, default=0.2)
    parser.add_argument("--postlude-s", type=float, default=0.2)
    parser.add_argument("--probe-period-ms", type=float, default=100.0)
    parser.add_argument("--probe-deadline-ms", type=float, default=100.0)
    parser.add_argument("--pending-capacity", type=int, default=1)
    parser.add_argument("--result-capacity", type=int, default=1)
    parser.add_argument("--overflow-submissions", type=int, default=2)
    parser.add_argument("--adapter-poll-interval-s", type=float, default=0.01)
    parser.add_argument("--join-timeout-s", type=float, default=5.0)
    parser.add_argument("--session-id")
    parser.add_argument("--repetition", type=int, default=1)
    parser.add_argument(
        "--output-root",
        type=Path,
        default=Path("experiments/runs/phase1-simulation"),
    )
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="permit development runs that are not formal reproducibility evidence",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        run_dir = run_once(args)
    except Exception as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 2
    print(run_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
