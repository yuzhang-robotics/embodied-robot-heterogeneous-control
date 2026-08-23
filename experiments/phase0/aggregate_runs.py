"""Aggregate multiple valid Phase 0 runs without inferential claims."""

from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path
from typing import Any

from .summarize_run import summarize_run_dir


def descriptive_stats(values: list[float]) -> dict[str, float | int]:
    if not values:
        raise ValueError("values must not be empty")
    mean = statistics.fmean(values)
    sample_stddev = statistics.stdev(values) if len(values) > 1 else 0.0
    return {
        "count": len(values),
        "min": min(values),
        "mean": mean,
        "median": statistics.median(values),
        "max": max(values),
        "range": max(values) - min(values),
        "sample_stddev": sample_stddev,
        "cv_pct": (sample_stddev / mean * 100) if mean else 0.0,
    }


def aggregate_run_dirs(run_dirs: list[Path | str]) -> dict[str, Any]:
    if not run_dirs:
        raise ValueError("at least one run directory is required")

    summaries = [summarize_run_dir(path) for path in run_dirs]
    measured = [item for item in summaries if item["sample_role"] == "measured"]
    if not measured:
        raise ValueError("no measured runs were provided")

    workloads = {item["workload"] for item in measured}
    input_hashes = {item["input_sha256"] for item in measured}
    residency_policies = {item["residency_policy"] for item in measured}
    baseline_commits = {item["baseline_commit"] for item in measured}
    runner_commits = {
        item.get("runner_git", {}).get("commit") for item in measured
    }
    stage_signatures = {
        tuple(
            (stage["component"], stage["stage"])
            for stage in item.get("timing", {}).get("stages", [])
        )
        for item in measured
    }
    translation_routes = {
        item["result"].get("translation_route") for item in measured
    }
    consistency_errors: list[str] = []
    if len(workloads) != 1:
        consistency_errors.append("workload mismatch")
    if len(input_hashes) != 1:
        consistency_errors.append("input hash mismatch")
    if len(residency_policies) != 1:
        consistency_errors.append("residency policy mismatch")
    if len(baseline_commits) != 1:
        consistency_errors.append("baseline commit mismatch")
    if None in runner_commits or "" in runner_commits or len(runner_commits) != 1:
        consistency_errors.append("runner commit mismatch or missing runner commit")
    if len(stage_signatures) != 1:
        consistency_errors.append("timing stage structure mismatch")
    if workloads == {"vlm"} and (
        None in translation_routes or len(translation_routes) != 1
    ):
        consistency_errors.append("VLM translation route mismatch or missing route")
    if consistency_errors:
        raise ValueError("; ".join(consistency_errors))

    stage_values: dict[str, list[float]] = {}
    per_run: list[dict[str, Any]] = []
    output_hashes: set[str] = set()
    output_chars: list[float] = []
    token_fields = (
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "request_completion_tokens_per_second",
        "request_ms_per_completion_token",
    )
    token_values: dict[str, list[float]] = {field: [] for field in token_fields}
    translation_route_counts: dict[str, int] = {}

    for item in measured:
        for stage in item["timing"]["stages"]:
            key = f"{stage['component']}/{stage['stage']}"
            stage_values.setdefault(key, []).append(stage["duration_ms"])

        result = item["result"]
        if result["text_sha256"]:
            output_hashes.add(result["text_sha256"])
        if result["output_chars"] is not None:
            output_chars.append(float(result["output_chars"]))
        token_usage = result.get("token_usage")
        if token_usage is not None:
            for field in token_fields:
                value = token_usage.get(field)
                if value is not None:
                    token_values[field].append(float(value))
        translation_route = result.get("translation_route")
        if translation_route is not None:
            translation_route_counts[translation_route] = (
                translation_route_counts.get(translation_route, 0) + 1
            )

        per_run.append(
            {
                "run_id": item["run_id"],
                "experiment_duration_ms": item["timing"]["experiment_duration_ms"],
                "result_text_sha256": result["text_sha256"],
                "output_chars": result["output_chars"],
                "token_usage": token_usage,
                "translation_route": translation_route,
                "resources": item["resources"],
            }
        )

    experiment_values = [
        float(item["timing"]["experiment_duration_ms"])
        for item in measured
        if item["timing"]["experiment_duration_ms"] is not None
    ]

    resource_fields = [
        "cpu_mean_across_cores_pct",
        "gr3d_usage_pct",
        "ram_used_mb",
        "temperature_all_sensors_c",
        "vdd_in_mw",
    ]
    resources: dict[str, Any] = {}
    for field in resource_fields:
        run_means = [
            float(item["resources"][field]["mean"])
            for item in measured
            if item["resources"][field] is not None
        ]
        observed_peaks = [
            float(item["resources"][field]["max"])
            for item in measured
            if item["resources"][field] is not None
        ]
        resources[field] = {
            "run_mean_stats": descriptive_stats(run_means) if run_means else None,
            "observed_peak": max(observed_peaks) if observed_peaks else None,
        }

    return {
        "analysis_kind": "descriptive_pilot",
        "interpretation_limit": (
            "These runs validate the measurement method and provide descriptive "
            "statistics only; they do not establish inferential conclusions."
        ),
        "workload": next(iter(workloads)),
        "measured_run_count": len(measured),
        "excluded_non_measured_count": len(summaries) - len(measured),
        "input_sha256": next(iter(input_hashes)),
        "residency_policy": next(iter(residency_policies)),
        "baseline_commit": next(iter(baseline_commits)),
        "runner_commit": next(iter(runner_commits)),
        "runner_dirty_any": any(item["runner_git"].get("dirty") for item in measured),
        "timing": {
            "experiment_duration_ms": descriptive_stats(experiment_values),
            "stages": {
                key: descriptive_stats(values) for key, values in stage_values.items()
            },
        },
        "outputs": {
            "unique_text_hashes": len(output_hashes),
            "output_chars": descriptive_stats(output_chars) if output_chars else None,
            "translation_route_counts": translation_route_counts or None,
            "token_usage": (
                {
                    field: descriptive_stats(values) if values else None
                    for field, values in token_values.items()
                }
                if any(token_values.values())
                else None
            ),
        },
        "resources": resources,
        "runs": per_run,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dirs", nargs="+", type=Path)
    args = parser.parse_args(argv)
    try:
        aggregate = aggregate_run_dirs(args.run_dirs)
    except ValueError as exc:
        print(f"ERROR: {exc}")
        return 1
    print(json.dumps(aggregate, ensure_ascii=False, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
