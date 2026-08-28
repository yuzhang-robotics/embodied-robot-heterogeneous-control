"""Plan, summarize and validate the Phase 1 Jetson simulation pilot."""

from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from experiments.phase1.jetson_preflight import (
    PREFLIGHT_SCHEMA_VERSION,
    preflight_errors,
)
from experiments.phase1.jetson_telemetry import (
    RESOURCE_SCHEMA_VERSION,
    load_resource_samples,
    summarize_resource_samples,
    validate_resource_samples,
)
from experiments.phase1.manifest import sha256_file
from experiments.phase1.simulation import SimulationCondition
from experiments.phase1.validate_run import (
    REQUIRED_FILES as RUN_REQUIRED_FILES,
    validate_run_dir,
)


PILOT_MANIFEST_SCHEMA_VERSION = "0.1.0"
PILOT_PLAN_SCHEMA_VERSION = "0.1.0"
PILOT_SUMMARY_SCHEMA_VERSION = "0.1.0"
PILOT_REQUIRED_FILES = (
    "session_manifest.json",
    "pilot_plan.json",
    "preflight.json",
    "resources.jsonl",
    "pilot_summary.json",
)
_SESSION_ID_RE = re.compile(
    r"^[0-9]{8}T[0-9]{6}Z_phase1_jetson_pilot(?:_[a-z][a-z0-9_-]{0,31})?$"
)
_RESPONSIVENESS_CONDITIONS = (
    SimulationCondition.R0_IDLE,
    SimulationCondition.R1_INLINE_SYNC,
    SimulationCondition.R2_THREADED_SYNC,
    SimulationCondition.R3_ASYNC,
)
_CORRECTNESS_CONDITIONS = (
    SimulationCondition.R4_STALE,
    SimulationCondition.R4_OVERFLOW,
)


class PilotError(RuntimeError):
    """The Jetson pilot contract could not be satisfied."""


def make_pilot_session_id(
    now: datetime | None = None,
    *,
    label: str | None = None,
) -> str:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    suffix = ""
    if label is not None:
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", label):
            raise ValueError("label must be a bounded lowercase identifier")
        suffix = "_" + label
    stamp = current.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}_phase1_jetson_pilot{suffix}"


def validate_pilot_session_id(value: str) -> str:
    if not isinstance(value, str) or not _SESSION_ID_RE.fullmatch(value):
        raise ValueError(
            "session_id must use YYYYMMDDTHHMMSSZ_phase1_jetson_pilot[_label]"
        )
    return value


def _finite_seconds(value: object, name: str, *, positive: bool = False) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
        or (positive and value == 0)
        or value > 3600
    ):
        qualifier = "positive" if positive else "non-negative"
        raise ValueError(f"{name} must be a finite {qualifier} value up to 3600")
    return float(value)


def build_pilot_plan(
    *,
    session_id: str,
    service_times_s: Sequence[float],
    repetitions: int = 1,
    correctness_service_time_s: float | None = None,
    prelude_s: float = 1.0,
    postlude_s: float = 1.0,
    probe_period_ms: float = 100.0,
    probe_deadline_ms: float = 100.0,
    pending_capacity: int = 1,
    result_capacity: int = 1,
    overflow_submissions: int = 2,
    adapter_poll_interval_s: float = 0.01,
    join_timeout_s: float = 5.0,
    resource_interval_ms: int = 200,
    resource_tail_s: float = 0.4,
) -> dict[str, object]:
    """Freeze one deterministic descriptive pilot matrix before execution."""

    validate_pilot_session_id(session_id)
    if isinstance(service_times_s, (str, bytes)) or not service_times_s:
        raise ValueError("service_times_s must contain at least one duration")
    durations = [
        _finite_seconds(value, "service_times_s", positive=True)
        for value in service_times_s
    ]
    if len(set(durations)) != len(durations):
        raise ValueError("service_times_s must not contain duplicates")
    if isinstance(repetitions, bool) or not isinstance(repetitions, int):
        raise TypeError("repetitions must be an integer")
    if repetitions < 1 or repetitions > 30:
        raise ValueError("repetitions must be between 1 and 30")
    correctness_duration = _finite_seconds(
        (
            correctness_service_time_s
            if correctness_service_time_s is not None
            else min(durations)
        ),
        "correctness_service_time_s",
        positive=True,
    )
    prelude = _finite_seconds(prelude_s, "prelude_s")
    postlude = _finite_seconds(postlude_s, "postlude_s")
    poll = _finite_seconds(
        adapter_poll_interval_s, "adapter_poll_interval_s", positive=True
    )
    join = _finite_seconds(join_timeout_s, "join_timeout_s", positive=True)
    tail = _finite_seconds(resource_tail_s, "resource_tail_s")
    for value, name in (
        (probe_period_ms, "probe_period_ms"),
        (probe_deadline_ms, "probe_deadline_ms"),
    ):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError(f"{name} must be numeric")
    probe_period = (
        _finite_seconds(probe_period_ms / 1000, "probe_period_ms", positive=True) * 1000
    )
    probe_deadline = (
        _finite_seconds(probe_deadline_ms / 1000, "probe_deadline_ms", positive=True)
        * 1000
    )
    for value, name in (
        (pending_capacity, "pending_capacity"),
        (result_capacity, "result_capacity"),
        (overflow_submissions, "overflow_submissions"),
        (resource_interval_ms, "resource_interval_ms"),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{name} must be a positive integer")
    if resource_interval_ms < 50 or resource_interval_ms > 10_000:
        raise ValueError("resource_interval_ms must be between 50 and 10000")

    runs: list[dict[str, object]] = []
    sequence = 0
    for repetition in range(1, repetitions + 1):
        for duration in durations:
            for condition in _RESPONSIVENESS_CONDITIONS:
                sequence += 1
                runs.append(
                    {
                        "sequence": sequence,
                        "block_repetition": repetition,
                        "condition": condition.value,
                        "service_time_s": duration,
                        "role": "responsiveness",
                    }
                )
        for condition in _CORRECTNESS_CONDITIONS:
            sequence += 1
            runs.append(
                {
                    "sequence": sequence,
                    "block_repetition": repetition,
                    "condition": condition.value,
                    "service_time_s": correctness_duration,
                    "role": "correctness",
                }
            )
    if len(runs) > 999:
        raise ValueError("pilot plan must not exceed 999 runs")

    return {
        "pilot_plan_schema_version": PILOT_PLAN_SCHEMA_VERSION,
        "session_id": session_id,
        "design_role": "descriptive_pilot",
        "descriptive_only": True,
        "inference_claim_permitted": False,
        "service_times_s": durations,
        "correctness_service_time_s": correctness_duration,
        "repetitions": repetitions,
        "condition_order": [
            condition.value
            for condition in _RESPONSIVENESS_CONDITIONS + _CORRECTNESS_CONDITIONS
        ],
        "scenario": {
            "prelude_s": prelude,
            "postlude_s": postlude,
            "probe_period_ms": probe_period,
            "probe_deadline_ms": probe_deadline,
            "pending_capacity": pending_capacity,
            "result_capacity": result_capacity,
            "overflow_submissions": overflow_submissions,
            "adapter_poll_interval_s": poll,
            "join_timeout_s": join,
        },
        "resource_telemetry": {
            "backend": "tegrastats",
            "resource_schema_version": RESOURCE_SCHEMA_VERSION,
            "interval_ms": resource_interval_ms,
            "tail_s": tail,
            "scope": "continuous_session",
        },
        "runs": runs,
    }


def _read_object(path: Path, errors: list[str]) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        errors.append(f"{path.name}: {type(exc).__name__}: {exc}")
        return {}
    if not isinstance(value, dict):
        errors.append(f"{path.name}: root must be an object")
        return {}
    return value


def _gate(
    name: str,
    passed: bool,
    *,
    observed: object,
    requirement: str,
) -> dict[str, object]:
    return {
        "name": name,
        "passed": passed,
        "observed": observed,
        "requirement": requirement,
    }


def _samples_for_interval(
    samples: Sequence[dict[str, Any]],
    started_monotonic_ns: int,
    finished_monotonic_ns: int,
) -> list[dict[str, Any]]:
    return [
        sample
        for sample in samples
        if started_monotonic_ns
        <= sample["sample_monotonic_ns"]
        <= finished_monotonic_ns
    ]


def build_pilot_summary(
    session_dir: Path | str,
    *,
    plan: Mapping[str, object],
    preflight: Mapping[str, object],
    run_records: Sequence[Mapping[str, object]],
    sampler_report: Mapping[str, object],
    samples: Sequence[dict[str, Any]],
) -> dict[str, object]:
    """Reconstruct descriptive run and resource facts from session artifacts."""

    directory = Path(session_dir)
    per_run: list[dict[str, object]] = []
    invalid_run_count = 0
    uncovered_run_count = 0
    stale_consumed_count = 0

    for record in run_records:
        relative_path = record.get("relative_path")
        if not isinstance(relative_path, str):
            raise ValueError("run record has no relative_path")
        run_dir = directory / relative_path
        scenario = json.loads((run_dir / "scenario.json").read_text(encoding="utf-8"))
        run_summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
        if not isinstance(scenario, dict) or not isinstance(run_summary, dict):
            raise ValueError("run artifacts must contain JSON objects")
        report = scenario.get("report")
        if not isinstance(report, dict):
            raise ValueError("scenario report is missing")
        started = report.get("started_monotonic_ns")
        finished = report.get("finished_monotonic_ns")
        if (
            isinstance(started, bool)
            or not isinstance(started, int)
            or isinstance(finished, bool)
            or not isinstance(finished, int)
            or finished < started
        ):
            raise ValueError("scenario report contains an invalid monotonic interval")
        selected = _samples_for_interval(samples, started, finished)
        uncovered_run_count += int(not selected)
        invalid_run_count += int(run_summary.get("valid") is not True)
        lifecycle = run_summary.get("lifecycle")
        if isinstance(lifecycle, dict):
            count = lifecycle.get("stale_consumed_count")
            if isinstance(count, int) and not isinstance(count, bool):
                stale_consumed_count += count
        per_run.append(
            {
                "sequence": record.get("sequence"),
                "block_repetition": record.get("block_repetition"),
                "condition": record.get("condition"),
                "service_time_s": record.get("service_time_s"),
                "role": record.get("role"),
                "run_id": record.get("run_id"),
                "relative_path": relative_path,
                "started_monotonic_ns": started,
                "finished_monotonic_ns": finished,
                "probe": run_summary.get("probe"),
                "task_timing": run_summary.get("task_timing"),
                "lifecycle": lifecycle,
                "resources": summarize_resource_samples(selected),
                "run_valid": run_summary.get("valid") is True,
            }
        )

    resource_errors = validate_resource_samples(samples)
    gates = [
        _gate(
            "preflight_passed",
            preflight.get("eligible") is True and not preflight_errors(preflight),
            observed=preflight.get("eligible"),
            requirement="all Jetson, source-identity and motion-safety checks pass",
        ),
        _gate(
            "run_matrix_complete",
            len(run_records) == len(plan.get("runs", [])),
            observed={
                "completed": len(run_records),
                "planned": len(plan.get("runs", [])),
            },
            requirement="every predeclared pilot run completes",
        ),
        _gate(
            "all_run_gates_passed",
            invalid_run_count == 0,
            observed=invalid_run_count,
            requirement="every individual R0--R4 summary is valid",
        ),
        _gate(
            "resource_stream_valid",
            not resource_errors,
            observed=resource_errors,
            requirement="resource samples are contiguous and have zero parse errors",
        ),
        _gate(
            "resource_sampler_stopped",
            sampler_report.get("successful") is True,
            observed=dict(sampler_report),
            requirement="tegrastats and its non-daemon reader stop cleanly",
        ),
        _gate(
            "every_run_has_resource_coverage",
            uncovered_run_count == 0,
            observed=uncovered_run_count,
            requirement="at least one resource sample falls inside every run interval",
        ),
        _gate(
            "stale_consumed_zero",
            stale_consumed_count == 0,
            observed=stale_consumed_count,
            requirement="no stale result is consumed in any pilot condition",
        ),
    ]
    return {
        "pilot_summary_schema_version": PILOT_SUMMARY_SCHEMA_VERSION,
        "session_id": plan.get("session_id"),
        "design_role": "descriptive_pilot",
        "descriptive_only": True,
        "inference_claim_permitted": False,
        "run_count": len(per_run),
        "resources": summarize_resource_samples(samples),
        "runs": per_run,
        "gates": gates,
        "valid": all(gate["passed"] is True for gate in gates),
    }


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _json_value(value: object) -> object:
    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))


def validate_pilot_dir(session_dir: Path | str) -> list[str]:
    """Independently validate one completed Jetson pilot session directory."""

    directory = Path(session_dir)
    errors: list[str] = []
    for name in PILOT_REQUIRED_FILES:
        if not (directory / name).is_file():
            errors.append(f"missing file: {name}")
    if list(directory.rglob("*.tmp")):
        errors.append("pilot directory contains an unfinished temporary file")
    if errors:
        return errors

    manifest = _read_object(directory / "session_manifest.json", errors)
    plan = _read_object(directory / "pilot_plan.json", errors)
    preflight = _read_object(directory / "preflight.json", errors)
    summary = _read_object(directory / "pilot_summary.json", errors)
    if errors:
        return errors

    session_id = manifest.get("session_id")
    if not isinstance(session_id, str) or session_id != directory.name:
        errors.append("manifest session_id does not match the directory")
    else:
        try:
            validate_pilot_session_id(session_id)
        except ValueError as exc:
            errors.append(str(exc))
    if manifest.get("pilot_manifest_schema_version") != PILOT_MANIFEST_SCHEMA_VERSION:
        errors.append("unsupported pilot manifest schema version")
    if manifest.get("artifact_kind") != "phase1_jetson_simulation_pilot":
        errors.append("manifest artifact kind is not a Jetson simulation pilot")
    if manifest.get("status") != "completed":
        errors.append(
            f"pilot manifest status is not completed: {manifest.get('status')!r}"
        )
    if manifest.get("failure_code") is not None:
        errors.append("completed pilot manifest contains a failure code")
    if manifest.get("cleanup_error_code") is not None:
        errors.append("completed pilot manifest contains a cleanup error code")
    if manifest.get("descriptive_only") is not True:
        errors.append("pilot manifest is not marked descriptive-only")
    if manifest.get("inference_claim_permitted") is not False:
        errors.append("pilot manifest incorrectly permits an inferential claim")

    if plan.get("pilot_plan_schema_version") != PILOT_PLAN_SCHEMA_VERSION:
        errors.append("unsupported pilot plan schema version")
    if plan.get("session_id") != session_id:
        errors.append("pilot plan session_id does not match the manifest")
    if (
        plan.get("descriptive_only") is not True
        or plan.get("inference_claim_permitted") is not False
    ):
        errors.append("pilot plan claim boundary is invalid")
    scenario_config = plan.get("scenario")
    resource_config = plan.get("resource_telemetry")
    if isinstance(scenario_config, dict) and isinstance(resource_config, dict):
        try:
            rebuilt_plan = build_pilot_plan(
                session_id=str(plan.get("session_id")),
                service_times_s=plan.get("service_times_s", []),
                repetitions=plan.get("repetitions"),
                correctness_service_time_s=plan.get("correctness_service_time_s"),
                prelude_s=scenario_config.get("prelude_s"),
                postlude_s=scenario_config.get("postlude_s"),
                probe_period_ms=scenario_config.get("probe_period_ms"),
                probe_deadline_ms=scenario_config.get("probe_deadline_ms"),
                pending_capacity=scenario_config.get("pending_capacity"),
                result_capacity=scenario_config.get("result_capacity"),
                overflow_submissions=scenario_config.get("overflow_submissions"),
                adapter_poll_interval_s=scenario_config.get("adapter_poll_interval_s"),
                join_timeout_s=scenario_config.get("join_timeout_s"),
                resource_interval_ms=resource_config.get("interval_ms"),
                resource_tail_s=resource_config.get("tail_s"),
            )
        except (TypeError, ValueError) as exc:
            errors.append(f"pilot plan reconstruction failed: {exc}")
        else:
            if plan != _json_value(rebuilt_plan):
                errors.append("pilot plan does not match its declared matrix")
    else:
        errors.append("pilot plan scenario or resource configuration is missing")
    if preflight.get("preflight_schema_version") != PREFLIGHT_SCHEMA_VERSION:
        errors.append("unsupported preflight schema version")
    errors.extend(preflight_errors(preflight))

    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict):
        errors.append("pilot artifact identities are missing")
    else:
        for name in (
            "pilot_plan.json",
            "preflight.json",
            "resources.jsonl",
            "pilot_summary.json",
        ):
            identity = artifacts.get(name)
            path = directory / name
            if not isinstance(identity, dict):
                errors.append(f"pilot identity is missing for {name}")
                continue
            if identity.get("size_bytes") != path.stat().st_size:
                errors.append(f"pilot size does not match {name}")
            if identity.get("sha256") != sha256_file(path):
                errors.append(f"pilot SHA-256 does not match {name}")

    planned_runs = plan.get("runs")
    run_records = manifest.get("runs")
    if not isinstance(planned_runs, list) or not planned_runs:
        errors.append("pilot plan contains no runs")
        return errors
    if not isinstance(run_records, list):
        errors.append("pilot manifest run records are missing")
        return errors
    if len(run_records) != len(planned_runs):
        errors.append("pilot manifest does not contain every planned run")

    allowed_top_level = set(PILOT_REQUIRED_FILES) | {
        str(item.get("condition")) for item in planned_runs if isinstance(item, dict)
    }
    for child in directory.iterdir():
        if child.name not in allowed_top_level:
            errors.append(f"unexpected pilot artifact: {child.name}")

    for index, record in enumerate(run_records):
        if not isinstance(record, dict):
            errors.append(f"run record {index + 1} is not an object")
            continue
        planned = planned_runs[index] if index < len(planned_runs) else None
        if not isinstance(planned, dict):
            errors.append(f"planned run {index + 1} is not an object")
            continue
        for field in (
            "sequence",
            "block_repetition",
            "condition",
            "service_time_s",
            "role",
        ):
            if record.get(field) != planned.get(field):
                errors.append(f"run record {index + 1} differs from the plan: {field}")
        relative_path = record.get("relative_path")
        if not isinstance(relative_path, str):
            errors.append(f"run record {index + 1} has no relative path")
            continue
        if Path(relative_path).is_absolute():
            errors.append(f"run record {index + 1} path is not relative")
            continue
        run_dir = (directory / relative_path).resolve()
        if not _is_relative_to(run_dir, directory.resolve()):
            errors.append(f"run record {index + 1} escapes the session directory")
            continue
        expected_parent = (directory / str(record.get("condition"))).resolve()
        if run_dir.parent != expected_parent or run_dir.name != record.get("run_id"):
            errors.append(f"run record {index + 1} path does not match its identity")
        if run_dir.is_dir():
            unexpected_run_files = {child.name for child in run_dir.iterdir()} - set(
                RUN_REQUIRED_FILES
            )
            if unexpected_run_files:
                errors.append(
                    f"run record {index + 1} contains unexpected artifacts: "
                    + ", ".join(sorted(unexpected_run_files))
                )
        run_errors = validate_run_dir(run_dir)
        errors.extend(f"run {index + 1}: {error}" for error in run_errors)
        run_manifest = _read_object(run_dir / "manifest.json", errors)
        if run_manifest:
            if run_manifest.get("run_id") != record.get("run_id"):
                errors.append(f"run record {index + 1} has a mismatched run_id")
            if run_manifest.get("session_id") != session_id:
                errors.append(f"run record {index + 1} has a mismatched session_id")
            if run_manifest.get("condition") != record.get("condition"):
                errors.append(f"run record {index + 1} has a mismatched condition")
            preflight_environment = preflight.get("environment")
            preflight_git = (
                preflight_environment.get("git")
                if isinstance(preflight_environment, dict)
                else None
            )
            run_environment = run_manifest.get("environment")
            run_git = (
                run_environment.get("git")
                if isinstance(run_environment, dict)
                else None
            )
            if not isinstance(preflight_git, dict) or not isinstance(run_git, dict):
                errors.append(f"run record {index + 1} has no comparable Git identity")
            else:
                for field in (
                    "commit",
                    "branch",
                    "dirty",
                    "upstream",
                    "upstream_commit",
                    "ahead_behind",
                ):
                    if run_git.get(field) != preflight_git.get(field):
                        errors.append(
                            f"run record {index + 1} Git identity differs: {field}"
                        )
            reproducibility = run_manifest.get("reproducibility")
            if (
                not isinstance(reproducibility, dict)
                or reproducibility.get("formal_evidence_eligible") is not True
            ):
                errors.append(
                    f"run record {index + 1} is not clean reproducibility evidence"
                )
            spec = run_manifest.get("spec")
            if not isinstance(spec, dict) or spec.get("service_time_s") != record.get(
                "service_time_s"
            ):
                errors.append(f"run record {index + 1} has a mismatched service time")
        identity = record.get("manifest_identity")
        manifest_path = run_dir / "manifest.json"
        if not isinstance(identity, dict):
            errors.append(f"run record {index + 1} has no manifest identity")
        elif manifest_path.is_file():
            if identity.get("size_bytes") != manifest_path.stat().st_size:
                errors.append(f"run record {index + 1} manifest size differs")
            if identity.get("sha256") != sha256_file(manifest_path):
                errors.append(f"run record {index + 1} manifest hash differs")

    expected_run_dirs = {
        (directory / record["relative_path"]).resolve()
        for record in run_records
        if isinstance(record, dict) and isinstance(record.get("relative_path"), str)
    }
    condition_names = {
        str(item.get("condition")) for item in planned_runs if isinstance(item, dict)
    }
    for condition_name in condition_names:
        condition_dir = directory / condition_name
        if not condition_dir.is_dir():
            continue
        for child in condition_dir.iterdir():
            if child.resolve() not in expected_run_dirs:
                errors.append(
                    f"unexpected run directory in {condition_name}: {child.name}"
                )

    try:
        samples = load_resource_samples(directory / "resources.jsonl")
    except (OSError, UnicodeError, ValueError) as exc:
        errors.append(f"resources.jsonl: {type(exc).__name__}: {exc}")
        samples = []
    resource_errors = validate_resource_samples(samples)
    errors.extend(resource_errors)
    sampler_report = manifest.get("resource_sampler_report")
    if not isinstance(sampler_report, dict):
        errors.append("resource sampler report is missing")
    else:
        if sampler_report.get("sample_count") != len(samples):
            errors.append("resource sampler sample count differs from resources.jsonl")
        parse_error_count = sum(bool(sample.get("parse_errors")) for sample in samples)
        if sampler_report.get("parse_error_count") != parse_error_count:
            errors.append(
                "resource sampler parse-error count differs from resources.jsonl"
            )
        first_sample_ns = samples[0].get("sample_monotonic_ns") if samples else None
        last_sample_ns = samples[-1].get("sample_monotonic_ns") if samples else None
        if sampler_report.get("first_sample_monotonic_ns") != first_sample_ns:
            errors.append(
                "resource sampler first timestamp differs from resources.jsonl"
            )
        if sampler_report.get("last_sample_monotonic_ns") != last_sample_ns:
            errors.append(
                "resource sampler last timestamp differs from resources.jsonl"
            )
        expected_sampler_success = (
            len(samples) > 0
            and parse_error_count == 0
            and sampler_report.get("stop_method") in {"terminated", "killed"}
            and sampler_report.get("reader_joined") is True
            and sampler_report.get("reader_error_code") is None
        )
        if sampler_report.get("successful") is not expected_sampler_success:
            errors.append("resource sampler success fact is inconsistent")
        if sampler_report.get("successful") is not True:
            errors.append("resource sampler did not stop successfully")

    if summary.get("pilot_summary_schema_version") != PILOT_SUMMARY_SCHEMA_VERSION:
        errors.append("unsupported pilot summary schema version")
    if summary.get("session_id") != session_id:
        errors.append("pilot summary session_id does not match the manifest")
    if summary.get("descriptive_only") is not True:
        errors.append("pilot summary is not marked descriptive-only")
    if summary.get("inference_claim_permitted") is not False:
        errors.append("pilot summary incorrectly permits an inferential claim")
    if summary.get("valid") is not True:
        errors.append("pilot summary Gates did not all pass")
    if isinstance(sampler_report, dict) and samples and not resource_errors:
        try:
            rebuilt = build_pilot_summary(
                directory,
                plan=plan,
                preflight=preflight,
                run_records=run_records,
                sampler_report=sampler_report,
                samples=samples,
            )
        except (
            OSError,
            UnicodeError,
            ValueError,
            TypeError,
            KeyError,
            json.JSONDecodeError,
        ) as exc:
            errors.append(f"pilot summary rebuild failed: {type(exc).__name__}: {exc}")
        else:
            if summary != _json_value(rebuilt):
                errors.append(
                    "pilot summary does not match independently rebuilt metrics"
                )
    return errors
