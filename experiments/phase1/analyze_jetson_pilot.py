"""Build a deterministic descriptive report from one validated Jetson pilot."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Mapping, Sequence

from experiments.phase1.jetson_telemetry import load_resource_samples
from experiments.phase1.pilot import validate_pilot_dir


ANALYSIS_SCHEMA_VERSION = "0.1.0"
DEFAULT_CPU_ACTIVITY_THRESHOLD_PCT = 80.0
DEFAULT_CPU_ACTIVITY_MIN_SAMPLES = 5
DEFAULT_CPU_ACTIVITY_MERGE_GAP_MS = 500.0
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    return value


def _finite_positive(value: object, name: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError(f"{name} must be positive and finite")
    return float(value)


def _positive_integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _milliseconds(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("nanosecond metric must be numeric or null")
    return round(float(value) / 1_000_000.0, 6)


def _metric_value(
    description: object,
    field: str,
    *,
    divisor: float = 1.0,
) -> float | None:
    if description is None:
        return None
    if not isinstance(description, Mapping):
        raise ValueError("descriptive metric must be an object or null")
    value = description.get(field)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"descriptive metric {field} must be numeric")
    return round(float(value) / divisor, 6)


def _snapshot_status(value: object) -> dict[str, object]:
    snapshot = value if isinstance(value, Mapping) else {}
    return {
        "available": snapshot.get("returncode") == 0
        and snapshot.get("error_code") is None,
        "returncode": snapshot.get("returncode"),
        "error_code": snapshot.get("error_code"),
        "output": snapshot.get("output", ""),
    }


def _condition_rows(summary: Mapping[str, object]) -> list[dict[str, object]]:
    runs = summary.get("runs")
    if not isinstance(runs, list):
        raise ValueError("pilot summary has no run list")
    rows: list[dict[str, object]] = []
    for run in runs:
        if not isinstance(run, Mapping):
            raise ValueError("pilot summary run is not an object")
        probe = run.get("probe")
        if not isinstance(probe, Mapping):
            raise ValueError("pilot summary run has no probe object")
        rows.append(
            {
                "sequence": run.get("sequence"),
                "condition": run.get("condition"),
                "role": run.get("role"),
                "service_time_s": run.get("service_time_s"),
                "tick_count": probe.get("tick_count"),
                "skipped_releases": probe.get("skipped_releases"),
                "deadline_miss_count": probe.get("deadline_miss_count"),
                "deadline_miss_rate": probe.get("deadline_miss_rate"),
                "start_lateness_p95_ms": _milliseconds(
                    _mapping_value(probe.get("start_lateness"), "p95_ns")
                ),
                "start_lateness_p99_ms": _milliseconds(
                    _mapping_value(probe.get("start_lateness"), "p99_ns")
                ),
                "start_lateness_max_ms": _milliseconds(
                    _mapping_value(probe.get("start_lateness"), "max_ns")
                ),
                "actual_period_p99_ms": _milliseconds(
                    _mapping_value(probe.get("actual_period"), "p99_ns")
                ),
                "maximum_observed_gap_ms": _milliseconds(
                    probe.get("maximum_observed_gap_ns")
                ),
                "run_valid": run.get("run_valid") is True,
            }
        )
    return rows


def _mapping_value(value: object, key: str) -> object:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError(f"metric containing {key} must be an object")
    return value.get(key)


def _runtime_rows(summary: Mapping[str, object]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for run in _run_objects(summary):
        if run.get("condition") != "r3_async":
            continue
        timing = run.get("task_timing")
        if not isinstance(timing, Mapping):
            raise ValueError("R3 run has no task timing object")
        service_time_s = run.get("service_time_s")
        if isinstance(service_time_s, bool) or not isinstance(
            service_time_s, (int, float)
        ):
            raise ValueError("R3 service time is not numeric")
        terminal_age_ms = _milliseconds(
            _mapping_value(timing.get("terminal_age"), "mean_ns")
        )
        configured_ms = round(float(service_time_s) * 1000.0, 6)
        rows.append(
            {
                "sequence": run.get("sequence"),
                "service_time_s": float(service_time_s),
                "queue_wait_ms": _milliseconds(
                    _mapping_value(timing.get("queue_wait"), "mean_ns")
                ),
                "measured_service_time_ms": _milliseconds(
                    _mapping_value(timing.get("service_time"), "mean_ns")
                ),
                "terminal_age_ms": terminal_age_ms,
                "terminal_age_excess_over_configured_ms": (
                    round(terminal_age_ms - configured_ms, 6)
                    if terminal_age_ms is not None
                    else None
                ),
            }
        )
    return rows


def _run_objects(summary: Mapping[str, object]) -> list[Mapping[str, object]]:
    runs = summary.get("runs")
    if not isinstance(runs, list):
        raise ValueError("pilot summary has no run list")
    if not all(isinstance(run, Mapping) for run in runs):
        raise ValueError("pilot summary run is not an object")
    return runs


def _lifecycle_rows(summary: Mapping[str, object]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for run in _run_objects(summary):
        if run.get("condition") not in {
            "r3_async",
            "r4_stale",
            "r4_overflow",
        }:
            continue
        lifecycle = run.get("lifecycle")
        if not isinstance(lifecycle, Mapping):
            raise ValueError("runtime run has no lifecycle object")
        disposition_items = lifecycle.get("disposition_counts")
        if not isinstance(disposition_items, list):
            raise ValueError("runtime run has no disposition counts")
        dispositions: dict[str, int] = {}
        for item in disposition_items:
            if (
                not isinstance(item, list)
                or len(item) != 2
                or not isinstance(item[0], str)
                or isinstance(item[1], bool)
                or not isinstance(item[1], int)
            ):
                raise ValueError("runtime disposition count is invalid")
            dispositions[item[0]] = item[1]
        rows.append(
            {
                "sequence": run.get("sequence"),
                "condition": run.get("condition"),
                "service_time_s": run.get("service_time_s"),
                "submission_attempts": lifecycle.get("submission_attempts"),
                "admitted_total": lifecycle.get("admitted_total"),
                "accepted_result_count": lifecycle.get("accepted_result_count"),
                "stale_consumed_count": lifecycle.get("stale_consumed_count"),
                "max_pending_depth": lifecycle.get("max_pending_depth"),
                "max_result_depth": lifecycle.get("max_result_depth"),
                "worker_joined": lifecycle.get("worker_joined"),
                "probe_joined": lifecycle.get("probe_joined"),
                "disposition_counts": dispositions,
            }
        )
    return rows


def _resource_rows(summary: Mapping[str, object]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for run in _run_objects(summary):
        resources = run.get("resources")
        if not isinstance(resources, Mapping):
            raise ValueError("pilot run has no resource summary")
        cpu = resources.get("cpu_usage_pct")
        if not isinstance(cpu, Mapping):
            raise ValueError("pilot run has no CPU summary")
        cpu_mean = sum(
            value
            for description in cpu.values()
            if (value := _metric_value(description, "mean")) is not None
        )
        gr3d = resources.get("gr3d_usage_pct")
        ram = resources.get("ram_used_mb")
        temperatures = resources.get("temperatures_c")
        power = resources.get("power_instant_mw")
        temperature_map = temperatures if isinstance(temperatures, Mapping) else {}
        power_map = power if isinstance(power, Mapping) else {}
        rows.append(
            {
                "sequence": run.get("sequence"),
                "condition": run.get("condition"),
                "service_time_s": run.get("service_time_s"),
                "sample_count": resources.get("sample_count"),
                "aggregate_cpu_mean_pct": round(cpu_mean, 6),
                "gr3d_mean_pct": _metric_value(gr3d, "mean"),
                "gr3d_p95_pct": _metric_value(gr3d, "p95"),
                "gr3d_max_pct": _metric_value(gr3d, "max"),
                "ram_mean_mb": _metric_value(ram, "mean"),
                "ram_max_mb": _metric_value(ram, "max"),
                "tj_mean_c": _metric_value(temperature_map.get("tj"), "mean"),
                "tj_max_c": _metric_value(temperature_map.get("tj"), "max"),
                "vdd_in_mean_mw": _metric_value(power_map.get("VDD_IN"), "mean"),
                "vdd_in_p95_mw": _metric_value(power_map.get("VDD_IN"), "p95"),
                "vdd_in_max_mw": _metric_value(power_map.get("VDD_IN"), "max"),
            }
        )
    return rows


def _cpu_total(sample: Mapping[str, object]) -> float:
    cores = sample.get("cpu")
    if not isinstance(cores, list):
        raise ValueError("resource sample has no CPU list")
    total = 0.0
    for core in cores:
        if not isinstance(core, Mapping):
            raise ValueError("resource CPU entry is not an object")
        usage = core.get("usage_pct")
        if usage is None and core.get("online") is False:
            continue
        if isinstance(usage, bool) or not isinstance(usage, (int, float)):
            raise ValueError("resource CPU usage is not numeric")
        total += float(usage)
    return total


def _detect_cpu_activity(
    samples: Sequence[Mapping[str, object]],
    runs: Sequence[Mapping[str, object]],
    *,
    threshold_pct: float,
    minimum_samples: int,
    merge_gap_ms: float,
) -> list[dict[str, object]]:
    threshold = _finite_positive(threshold_pct, "cpu_activity_threshold_pct")
    minimum = _positive_integer(minimum_samples, "cpu_activity_min_samples")
    merge_gap_ns = int(
        _finite_positive(merge_gap_ms, "cpu_activity_merge_gap_ms") * 1_000_000
    )
    selected = [sample for sample in samples if _cpu_total(sample) >= threshold]
    groups: list[list[Mapping[str, object]]] = []
    for sample in selected:
        monotonic_ns = sample.get("sample_monotonic_ns")
        if isinstance(monotonic_ns, bool) or not isinstance(monotonic_ns, int):
            raise ValueError("resource sample has an invalid monotonic timestamp")
        if not groups:
            groups.append([sample])
            continue
        previous_ns = groups[-1][-1].get("sample_monotonic_ns")
        if (
            not isinstance(previous_ns, int)
            or monotonic_ns - previous_ns > merge_gap_ns
        ):
            groups.append([sample])
        else:
            groups[-1].append(sample)

    episodes: list[dict[str, object]] = []
    first_sample_ns = int(samples[0]["sample_monotonic_ns"]) if samples else 0
    for group in groups:
        if len(group) < minimum:
            continue
        start_ns = int(group[0]["sample_monotonic_ns"])
        end_ns = int(group[-1]["sample_monotonic_ns"])
        totals = [_cpu_total(sample) for sample in group]
        overlapping: list[dict[str, object]] = []
        for run in runs:
            run_start = run.get("started_monotonic_ns")
            run_finish = run.get("finished_monotonic_ns")
            if not isinstance(run_start, int) or not isinstance(run_finish, int):
                raise ValueError("pilot run has an invalid monotonic interval")
            if run_start <= end_ns and run_finish >= start_ns:
                overlapping.append(
                    {
                        "sequence": run.get("sequence"),
                        "condition": run.get("condition"),
                        "service_time_s": run.get("service_time_s"),
                    }
                )
        episodes.append(
            {
                "start_offset_s": round((start_ns - first_sample_ns) / 1e9, 6),
                "end_offset_s": round((end_ns - first_sample_ns) / 1e9, 6),
                "duration_s": round((end_ns - start_ns) / 1e9, 6),
                "sample_count": len(group),
                "aggregate_cpu_mean_pct": round(sum(totals) / len(totals), 6),
                "aggregate_cpu_max_pct": round(max(totals), 6),
                "overlapping_runs": overlapping,
            }
        )
    return episodes


def _resource_capabilities(
    samples: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    sample_count = len(samples)
    emc_available = sum(isinstance(sample.get("emc"), Mapping) for sample in samples)
    warning_counts = Counter(
        warning
        for sample in samples
        for warning in sample.get("parse_warnings", [])
        if isinstance(warning, str)
    )
    error_counts = Counter(
        error
        for sample in samples
        for error in sample.get("parse_errors", [])
        if isinstance(error, str)
    )
    return {
        "sample_count": sample_count,
        "emc": {
            "available_sample_count": emc_available,
            "missing_sample_count": sample_count - emc_available,
            "coverage_rate": emc_available / sample_count if sample_count else None,
        },
        "parse_warning_counts": dict(sorted(warning_counts.items())),
        "parse_error_counts": dict(sorted(error_counts.items())),
    }


def _limitations(
    plan: Mapping[str, object],
    capabilities: Mapping[str, object],
    clocks: Mapping[str, object],
    activity: Sequence[Mapping[str, object]],
) -> list[str]:
    limitations = ["simulated_workload_only", "fixed_condition_order"]
    if plan.get("repetitions") == 1:
        limitations.append("single_repetition")
    emc = capabilities.get("emc")
    if isinstance(emc, Mapping) and emc.get("available_sample_count") == 0:
        limitations.append("emc_unavailable")
    if clocks.get("available") is not True:
        limitations.append("jetson_clocks_snapshot_unavailable")
    if activity:
        limitations.append("unattributed_cpu_activity_detected")
    return limitations


def analyze_pilot_dir(
    session_dir: Path | str,
    *,
    source_archive_sha256: str | None = None,
    cpu_activity_threshold_pct: float = DEFAULT_CPU_ACTIVITY_THRESHOLD_PCT,
    cpu_activity_min_samples: int = DEFAULT_CPU_ACTIVITY_MIN_SAMPLES,
    cpu_activity_merge_gap_ms: float = DEFAULT_CPU_ACTIVITY_MERGE_GAP_MS,
) -> dict[str, object]:
    """Validate and reconstruct one non-inferential Jetson pilot analysis."""

    directory = Path(session_dir).resolve()
    errors = validate_pilot_dir(directory)
    if errors:
        raise ValueError("invalid Jetson pilot: " + "; ".join(errors))
    if source_archive_sha256 is not None:
        normalized_hash = source_archive_sha256.lower()
        if not _SHA256_RE.fullmatch(normalized_hash):
            raise ValueError("source_archive_sha256 must contain 64 hexadecimal digits")
    else:
        normalized_hash = None

    manifest = _read_object(directory / "session_manifest.json")
    plan = _read_object(directory / "pilot_plan.json")
    preflight = _read_object(directory / "preflight.json")
    summary = _read_object(directory / "pilot_summary.json")
    samples = load_resource_samples(directory / "resources.jsonl")
    runs = _run_objects(summary)

    environment = preflight.get("environment")
    if not isinstance(environment, Mapping):
        raise ValueError("preflight has no environment object")
    git = environment.get("git")
    git_record = git if isinstance(git, Mapping) else {}
    clocks = _snapshot_status(environment.get("jetson_clocks"))
    capabilities = _resource_capabilities(samples)
    activity = _detect_cpu_activity(
        samples,
        runs,
        threshold_pct=cpu_activity_threshold_pct,
        minimum_samples=cpu_activity_min_samples,
        merge_gap_ms=cpu_activity_merge_gap_ms,
    )
    gates = summary.get("gates")
    if not isinstance(gates, list):
        raise ValueError("pilot summary has no Gate list")

    return {
        "analysis_schema_version": ANALYSIS_SCHEMA_VERSION,
        "analysis_kind": "phase1_jetson_simulation_pilot",
        "session_id": summary.get("session_id"),
        "source": {
            "git_commit": git_record.get("commit"),
            "git_branch": git_record.get("branch"),
            "source_archive_sha256": normalized_hash,
            "session_artifacts": manifest.get("artifacts"),
        },
        "claim_boundary": {
            "design_role": "descriptive_pilot",
            "descriptive_only": True,
            "inference_claim_permitted": False,
            "asynchronous_performance_superiority_claim_permitted": False,
            "hard_real_time_claim_permitted": False,
            "heterogeneous_inference_claim_permitted": False,
            "condition_resource_attribution_permitted": False,
        },
        "analysis_parameters": {
            "percentile_method": "nearest-rank",
            "cpu_activity_screen": {
                "metric": "sum of per-core tegrastats usage percentages",
                "threshold_pct": float(cpu_activity_threshold_pct),
                "minimum_samples": cpu_activity_min_samples,
                "merge_gap_ms": float(cpu_activity_merge_gap_ms),
                "role": "descriptive contamination screen; no samples excluded",
            },
        },
        "environment": {
            "platform": environment.get("platform"),
            "machine": environment.get("machine"),
            "python": environment.get("python"),
            "l4t_release": environment.get("l4t_release"),
            "jetpack_packages": _snapshot_status(environment.get("jetpack_packages")),
            "nvpmodel": _snapshot_status(environment.get("nvpmodel")),
            "jetson_clocks": clocks,
            "motion_disabled": (
                preflight.get("safety", {}).get("motion_enabled") is False
                if isinstance(preflight.get("safety"), Mapping)
                else False
            ),
        },
        "validation": {
            "session_status": manifest.get("status"),
            "session_valid": summary.get("valid") is True,
            "run_count": summary.get("run_count"),
            "gates": gates,
        },
        "responsiveness": _condition_rows(summary),
        "runtime_overhead": _runtime_rows(summary),
        "lifecycle": _lifecycle_rows(summary),
        "resources": {
            "session": summary.get("resources"),
            "per_run": _resource_rows(summary),
            "capabilities": capabilities,
        },
        "data_quality": {
            "unattributed_cpu_activity": activity,
            "limitations": _limitations(plan, capabilities, clocks, activity),
        },
    }


def _format_number(value: object, digits: int = 3) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value).replace("|", "\\|")


def _snapshot_output(environment: Mapping[str, object], name: str) -> str:
    snapshot = environment.get(name)
    if not isinstance(snapshot, Mapping):
        return "unavailable"
    output = str(snapshot.get("output", "")).replace("\n", "; ").replace("\t", " ")
    return output or "unavailable"


def render_markdown(analysis: Mapping[str, object]) -> str:
    """Render the public, descriptive result report."""

    source = analysis["source"]
    environment = analysis["environment"]
    validation = analysis["validation"]
    resources = analysis["resources"]
    quality = analysis["data_quality"]
    parameters = analysis["analysis_parameters"]
    if not all(
        isinstance(item, Mapping)
        for item in (
            source,
            environment,
            validation,
            resources,
            quality,
            parameters,
        )
    ):
        raise ValueError("analysis contains an invalid report section")

    lines = [
        "# Phase 1 Jetson Simulation Pilot",
        "",
        (
            "This report records one descriptive, motion-disabled pilot on the "
            "Jetson Orin Nano. It validates the measurement protocol and runtime "
            "semantics; it is not a formal performance comparison."
        ),
        "",
        "## Evidence boundary",
        "",
        "- Workload: deterministic simulated service time; no model inference.",
        "- Repetitions: one fixed-order R0--R4 matrix.",
        "- Physical motion and UART access: disabled.",
        "- Permitted interpretation: descriptive timing and correctness evidence only.",
        "- Not permitted: asynchronous superiority, hard-real-time or heterogeneous-inference claims.",
        "",
        "## Provenance",
        "",
        "| Field | Value |",
        "| --- | --- |",
        f"| Session | `{analysis.get('session_id')}` |",
        f"| Source commit | `{source.get('git_commit')}` |",
        f"| Source branch | `{source.get('git_branch')}` |",
        f"| Transfer archive SHA-256 | `{source.get('source_archive_sha256') or 'not recorded'}` |",
        f"| Session status | `{validation.get('session_status')}` |",
        f"| Independently valid | {_format_number(validation.get('session_valid'))} |",
        "",
        "Machine-readable derived data: [`analysis.json`](analysis.json).",
        "",
        "## Device context",
        "",
        "| Field | Value |",
        "| --- | --- |",
        f"| Platform | {_format_number(environment.get('platform'))} |",
        f"| Architecture | {_format_number(environment.get('machine'))} |",
        f"| Python | {_format_number(environment.get('python'))} |",
        f"| JetPack packages | {_snapshot_output(environment, 'jetpack_packages')} |",
        f"| Power mode | {_snapshot_output(environment, 'nvpmodel')} |",
        f"| `jetson_clocks` snapshot | {'available' if environment.get('jetson_clocks', {}).get('available') else 'unavailable'} |",
        "",
        "## Validation Gates",
        "",
        "| Gate | Passed |",
        "| --- | --- |",
    ]
    gates = validation.get("gates")
    if not isinstance(gates, list):
        raise ValueError("analysis validation Gates are missing")
    for gate in gates:
        if not isinstance(gate, Mapping):
            raise ValueError("analysis validation Gate is invalid")
        lines.append(f"| `{gate.get('name')}` | {_format_number(gate.get('passed'))} |")

    lines.extend(
        [
            "",
            "## Responsiveness decomposition",
            "",
            "| Seq. | Condition | Service (s) | Ticks | Skipped | Deadline misses | Lateness p99 (ms) | Maximum gap (ms) |",
            "| ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    responsiveness = analysis.get("responsiveness")
    if not isinstance(responsiveness, list):
        raise ValueError("analysis responsiveness rows are missing")
    for row in responsiveness:
        if not isinstance(row, Mapping):
            raise ValueError("analysis responsiveness row is invalid")
        lines.append(
            "| {sequence} | `{condition}` | {service} | {ticks} | {skipped} | "
            "{misses} | {p99} | {gap} |".format(
                sequence=row.get("sequence"),
                condition=row.get("condition"),
                service=_format_number(row.get("service_time_s")),
                ticks=_format_number(row.get("tick_count")),
                skipped=_format_number(row.get("skipped_releases")),
                misses=_format_number(row.get("deadline_miss_count")),
                p99=_format_number(row.get("start_lateness_p99_ms")),
                gap=_format_number(row.get("maximum_observed_gap_ms")),
            )
        )

    lines.extend(
        [
            "",
            (
                "The inline condition recorded skipped releases rather than executed "
                "ticks that missed their deadline. R2 and R3 both isolate the periodic "
                "probe from the slow call; R3 additionally supplies bounded ownership, "
                "freshness and cancellation semantics."
            ),
            "",
            "## R3 runtime timing",
            "",
            "| Service (s) | Queue wait (ms) | Measured service (ms) | Terminal age (ms) | Excess over configured (ms) |",
            "| ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    overhead = analysis.get("runtime_overhead")
    if not isinstance(overhead, list):
        raise ValueError("analysis runtime overhead rows are missing")
    for row in overhead:
        if not isinstance(row, Mapping):
            raise ValueError("analysis runtime overhead row is invalid")
        lines.append(
            "| {service} | {queue} | {measured} | {terminal} | {excess} |".format(
                service=_format_number(row.get("service_time_s")),
                queue=_format_number(row.get("queue_wait_ms")),
                measured=_format_number(row.get("measured_service_time_ms")),
                terminal=_format_number(row.get("terminal_age_ms")),
                excess=_format_number(
                    row.get("terminal_age_excess_over_configured_ms")
                ),
            )
        )

    lines.extend(
        [
            "",
            "## Runtime correctness",
            "",
            "| Condition | Service (s) | Submissions | Accepted | Stale consumed | Max pending | Max result | Dispositions |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
        ]
    )
    lifecycle = analysis.get("lifecycle")
    if not isinstance(lifecycle, list):
        raise ValueError("analysis lifecycle rows are missing")
    for row in lifecycle:
        if not isinstance(row, Mapping):
            raise ValueError("analysis lifecycle row is invalid")
        disposition = row.get("disposition_counts")
        rendered = (
            ", ".join(f"{key}={value}" for key, value in disposition.items())
            if isinstance(disposition, Mapping)
            else "n/a"
        )
        lines.append(
            "| `{condition}` | {service} | {submitted} | {accepted} | {stale} | "
            "{pending} | {result} | {disposition} |".format(
                condition=row.get("condition"),
                service=_format_number(row.get("service_time_s")),
                submitted=_format_number(row.get("submission_attempts")),
                accepted=_format_number(row.get("accepted_result_count")),
                stale=_format_number(row.get("stale_consumed_count")),
                pending=_format_number(row.get("max_pending_depth")),
                result=_format_number(row.get("max_result_depth")),
                disposition=rendered,
            )
        )

    session_resources = resources.get("session")
    capabilities = resources.get("capabilities")
    if not isinstance(session_resources, Mapping) or not isinstance(
        capabilities, Mapping
    ):
        raise ValueError("analysis resource sections are invalid")
    temperature = session_resources.get("temperatures_c")
    power = session_resources.get("power_instant_mw")
    temperature_map = temperature if isinstance(temperature, Mapping) else {}
    power_map = power if isinstance(power, Mapping) else {}
    sample_interval = session_resources.get("sample_interval_ns")
    sample_span_s = _metric_value(
        {"value": session_resources.get("sample_span_ns")},
        "value",
        divisor=1_000_000_000.0,
    )
    lines.extend(
        [
            "",
            "## Session resources",
            "",
            "| Metric | Mean | p95 | Maximum |",
            "| --- | ---: | ---: | ---: |",
            "| RAM used (MB) | {mean} | {p95} | {maximum} |".format(
                mean=_format_number(
                    _metric_value(session_resources.get("ram_used_mb"), "mean")
                ),
                p95=_format_number(
                    _metric_value(session_resources.get("ram_used_mb"), "p95")
                ),
                maximum=_format_number(
                    _metric_value(session_resources.get("ram_used_mb"), "max")
                ),
            ),
            "| GR3D usage (%) | {mean} | {p95} | {maximum} |".format(
                mean=_format_number(
                    _metric_value(session_resources.get("gr3d_usage_pct"), "mean")
                ),
                p95=_format_number(
                    _metric_value(session_resources.get("gr3d_usage_pct"), "p95")
                ),
                maximum=_format_number(
                    _metric_value(session_resources.get("gr3d_usage_pct"), "max")
                ),
            ),
            "| Junction temperature (C) | {mean} | {p95} | {maximum} |".format(
                mean=_format_number(_metric_value(temperature_map.get("tj"), "mean")),
                p95=_format_number(_metric_value(temperature_map.get("tj"), "p95")),
                maximum=_format_number(_metric_value(temperature_map.get("tj"), "max")),
            ),
            "| VDD_IN instantaneous power (mW) | {mean} | {p95} | {maximum} |".format(
                mean=_format_number(_metric_value(power_map.get("VDD_IN"), "mean")),
                p95=_format_number(_metric_value(power_map.get("VDD_IN"), "p95")),
                maximum=_format_number(_metric_value(power_map.get("VDD_IN"), "max")),
            ),
            "",
            (
                "Telemetry coverage: {count} samples across {span} s; mean interval "
                "{mean} ms, p99 interval {p99} ms, parse errors {errors}."
            ).format(
                count=_format_number(session_resources.get("sample_count")),
                span=_format_number(sample_span_s),
                mean=_format_number(
                    _metric_value(sample_interval, "mean", divisor=1_000_000.0)
                ),
                p99=_format_number(
                    _metric_value(sample_interval, "p99", divisor=1_000_000.0)
                ),
                errors=_format_number(session_resources.get("parse_error_count")),
            ),
            "",
            "## Data-quality observations",
            "",
        ]
    )
    limitations = quality.get("limitations")
    if not isinstance(limitations, list):
        raise ValueError("analysis limitations are missing")
    for item in limitations:
        lines.append(f"- `{item}`")

    emc = capabilities.get("emc")
    if isinstance(emc, Mapping):
        lines.append(
            "- EMC coverage: {available}/{total} samples; missing values are not "
            "interpreted as zero.".format(
                available=emc.get("available_sample_count"),
                total=capabilities.get("sample_count"),
            )
        )
    warnings = capabilities.get("parse_warning_counts")
    if isinstance(warnings, Mapping):
        lines.append(
            "- Resource parse warnings: "
            + (", ".join(f"{key}={value}" for key, value in warnings.items()) or "none")
            + "."
        )

    activity_screen = parameters.get("cpu_activity_screen")
    if not isinstance(activity_screen, Mapping):
        raise ValueError("analysis CPU activity parameters are missing")
    lines.extend(
        [
            "",
            "### Unattributed CPU activity screen",
            "",
            (
                "The screen sums per-core usage percentages and marks sustained "
                "intervals at or above {threshold}%. It does not identify a process "
                "and does not exclude any sample."
            ).format(threshold=_format_number(activity_screen.get("threshold_pct"), 1)),
            "",
            "| Start offset (s) | End offset (s) | Samples | CPU mean (%) | CPU max (%) | Overlapping runs |",
            "| ---: | ---: | ---: | ---: | ---: | --- |",
        ]
    )
    episodes = quality.get("unattributed_cpu_activity")
    if not isinstance(episodes, list):
        raise ValueError("analysis activity episodes are missing")
    for episode in episodes:
        if not isinstance(episode, Mapping):
            raise ValueError("analysis activity episode is invalid")
        overlapping = episode.get("overlapping_runs")
        rendered_runs = ""
        if isinstance(overlapping, list):
            rendered_runs = ", ".join(
                f"{item.get('sequence')}:{item.get('condition')}"
                for item in overlapping
                if isinstance(item, Mapping)
            )
        lines.append(
            "| {start} | {end} | {count} | {mean} | {maximum} | {runs} |".format(
                start=_format_number(episode.get("start_offset_s")),
                end=_format_number(episode.get("end_offset_s")),
                count=_format_number(episode.get("sample_count")),
                mean=_format_number(episode.get("aggregate_cpu_mean_pct")),
                maximum=_format_number(episode.get("aggregate_cpu_max_pct")),
                runs=rendered_runs or "none",
            )
        )

    lines.extend(
        [
            "",
            "## Interpretation",
            "",
            (
                "The pilot shows that an inline slow call creates a control-proxy "
                "gap on the same scale as the configured service time. For this "
                "simulated delay, a separate thread preserved the probe schedule, "
                "while the bounded runtime added explicit queue, cancellation and "
                "freshness behavior. This result does not establish isolation for "
                "real adapters that hold the Python GIL."
            ),
            "",
            (
                "Resource differences between conditions are not attributed to the "
                "runtime because the matrix has one fixed-order repetition and the "
                "CPU screen crosses condition boundaries. The simulated adapter also "
                "does not exercise a heterogeneous inference workload. A fixed-input "
                "VLM slice and a balanced repeated protocol are required before those "
                "questions can be tested."
            ),
            "",
        ]
    )
    return "\n".join(lines)


def _refuse_output_inside_session(output: Path | None, session_dir: Path) -> None:
    if output is None:
        return
    resolved = output.resolve()
    try:
        resolved.relative_to(session_dir.resolve())
    except ValueError:
        return
    raise ValueError("analysis output must not be written inside the source session")


def _require_distinct_outputs(
    json_output: Path | None,
    markdown_output: Path | None,
) -> None:
    if (
        json_output is not None
        and markdown_output is not None
        and json_output.resolve() == markdown_output.resolve()
    ):
        raise ValueError("JSON and Markdown outputs must use distinct paths")


def _write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8", newline="\n")
    os.replace(temporary, path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session_dir", type=Path)
    parser.add_argument("--source-archive-sha256")
    parser.add_argument("--json-output", type=Path)
    parser.add_argument("--markdown-output", type=Path)
    parser.add_argument(
        "--cpu-activity-threshold-pct",
        type=float,
        default=DEFAULT_CPU_ACTIVITY_THRESHOLD_PCT,
    )
    parser.add_argument(
        "--cpu-activity-min-samples",
        type=int,
        default=DEFAULT_CPU_ACTIVITY_MIN_SAMPLES,
    )
    parser.add_argument(
        "--cpu-activity-merge-gap-ms",
        type=float,
        default=DEFAULT_CPU_ACTIVITY_MERGE_GAP_MS,
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    session_dir = args.session_dir.resolve()
    try:
        _refuse_output_inside_session(args.json_output, session_dir)
        _refuse_output_inside_session(args.markdown_output, session_dir)
        _require_distinct_outputs(args.json_output, args.markdown_output)
        analysis = analyze_pilot_dir(
            session_dir,
            source_archive_sha256=args.source_archive_sha256,
            cpu_activity_threshold_pct=args.cpu_activity_threshold_pct,
            cpu_activity_min_samples=args.cpu_activity_min_samples,
            cpu_activity_merge_gap_ms=args.cpu_activity_merge_gap_ms,
        )
        markdown_text = (
            render_markdown(analysis) if args.markdown_output is not None else None
        )
        json_text = json.dumps(
            analysis,
            ensure_ascii=False,
            indent=2,
            allow_nan=False,
        )
        if args.json_output is not None:
            _write_text_atomic(args.json_output, json_text + "\n")
        else:
            print(json_text)
        if args.markdown_output is not None and markdown_text is not None:
            _write_text_atomic(args.markdown_output, markdown_text)
    except (OSError, TypeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
