"""Analyze the balanced, three-session Phase 0 formal baseline dataset."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

import numpy as np

from .summarize_run import summarize_run_dir
from .validate_run import validate_run_dir


WORKLOADS = ("asr", "llm", "vlm")
EXPECTED_WARMUPS = {"asr": 3, "llm": 1, "vlm": 1}
EXPECTED_RESOURCE_INTERVAL_MS = 200
RESOURCE_FIELDS = (
    "cpu_mean_across_cores_pct",
    "gr3d_usage_pct",
    "ram_used_mb",
    "temperature_all_sensors_c",
    "vdd_in_mw",
)
TOKEN_FIELDS = (
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "request_completion_tokens_per_second",
    "request_ms_per_completion_token",
)


@dataclass(frozen=True)
class RunRecord:
    session: str
    session_index: int
    repetition: int
    role: str
    run_dir: Path
    manifest: dict[str, Any]
    summary: dict[str, Any]


def nearest_rank(values: Iterable[float], percentile: float) -> float:
    """Return the nearest-rank percentile used by the recorder benchmark."""

    ordered = sorted(float(value) for value in values)
    if not ordered:
        raise ValueError("values must not be empty")
    if not 0 < percentile <= 100:
        raise ValueError("percentile must be in (0, 100]")
    rank = math.ceil(percentile / 100 * len(ordered))
    return ordered[rank - 1]


def _metric_seed(base_seed: int, metric_name: str) -> int:
    digest = hashlib.sha256(metric_name.encode("utf-8")).digest()
    return base_seed ^ int.from_bytes(digest[:8], "big")


def _bootstrap_mean_median_ci(
    groups: list[np.ndarray],
    *,
    resamples: int,
    seed: int,
    confidence_level: float,
) -> tuple[tuple[float, float], tuple[float, float]]:
    if resamples <= 0:
        raise ValueError("bootstrap resamples must be positive")
    if not 0 < confidence_level < 1:
        raise ValueError("confidence level must be in (0, 1)")
    if not groups or any(group.size == 0 for group in groups):
        raise ValueError("each bootstrap session group must contain values")

    rng = np.random.default_rng(seed)
    mean_samples: list[np.ndarray] = []
    median_samples: list[np.ndarray] = []
    remaining = resamples
    chunk_size = min(10_000, resamples)

    while remaining:
        current = min(chunk_size, remaining)
        sampled_groups = []
        for group in groups:
            indices = rng.integers(0, group.size, size=(current, group.size))
            sampled_groups.append(group[indices])
        sampled = np.concatenate(sampled_groups, axis=1)
        mean_samples.append(sampled.mean(axis=1))
        median_samples.append(np.median(sampled, axis=1))
        remaining -= current

    means = np.concatenate(mean_samples)
    medians = np.concatenate(median_samples)
    alpha = (1 - confidence_level) / 2
    quantiles = (alpha, 1 - alpha)
    mean_ci = tuple(float(value) for value in np.quantile(means, quantiles))
    median_ci = tuple(float(value) for value in np.quantile(medians, quantiles))
    return mean_ci, median_ci


def formal_stats(
    values_by_session: dict[str, list[float]],
    *,
    resamples: int,
    seed: int,
    confidence_level: float = 0.95,
) -> dict[str, Any]:
    """Calculate formal descriptive statistics and stratified bootstrap CIs."""

    if not values_by_session:
        raise ValueError("values_by_session must not be empty")
    if confidence_level != 0.95:
        raise ValueError("formal analysis currently supports only 95% confidence")
    groups = [
        np.asarray(values_by_session[name], dtype=float)
        for name in sorted(values_by_session)
    ]
    values = np.concatenate(groups)
    if not np.all(np.isfinite(values)):
        raise ValueError("formal statistics require finite values")

    mean = float(values.mean())
    median = float(np.median(values))
    sample_stddev = float(values.std(ddof=1)) if values.size > 1 else 0.0
    mean_ci, median_ci = _bootstrap_mean_median_ci(
        groups,
        resamples=resamples,
        seed=seed,
        confidence_level=confidence_level,
    )
    return {
        "count": int(values.size),
        "min": float(values.min()),
        "mean": mean,
        "mean_ci95": {"low": mean_ci[0], "high": mean_ci[1]},
        "median": median,
        "median_ci95": {"low": median_ci[0], "high": median_ci[1]},
        "max": float(values.max()),
        "range": float(values.max() - values.min()),
        "sample_stddev": sample_stddev,
        "cv_pct": sample_stddev / mean * 100 if mean else 0.0,
        "p95_nearest_rank": nearest_rank(values, 95),
    }


def _descriptive_stats(values: list[float]) -> dict[str, Any]:
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
        "sample_stddev": sample_stddev,
        "cv_pct": sample_stddev / mean * 100 if mean else 0.0,
        "p95_nearest_rank": nearest_rank(values, 95),
    }


def linear_trend(values: list[float]) -> dict[str, float | int]:
    """Describe a within-session linear trend without claiming causality."""

    if len(values) < 2:
        raise ValueError("at least two values are required for a trend")
    numeric = np.asarray(values, dtype=float)
    positions = np.arange(1, numeric.size + 1, dtype=float)
    slope, intercept = np.polyfit(positions, numeric, 1)
    split = numeric.size // 2
    first = float(numeric[:split].mean())
    second = float(numeric[split:].mean())
    correlation = float(np.corrcoef(positions, numeric)[0, 1])
    return {
        "count": int(numeric.size),
        "slope_per_run": float(slope),
        "intercept": float(intercept),
        "position_pearson_r": correlation,
        "first_half_mean": first,
        "second_half_mean": second,
        "second_vs_first_pct": (second / first - 1) * 100 if first else 0.0,
    }


def _parse_repetition(run_dir: Path) -> int:
    try:
        return int(run_dir.name.rsplit("_", 1)[-1])
    except ValueError as exc:
        raise ValueError(f"cannot parse repetition from {run_dir.name}") from exc


def _workload_config_identity(manifest: dict[str, Any]) -> dict[str, Any]:
    workload = manifest["workload"]
    config = manifest["workload_config"]
    if workload in {"asr", "llm"}:
        model = config["model"]
        model_identity = {
            "size_bytes": model["size_bytes"],
            "expected_sha256": model["expected_sha256"],
            "hash_verified_during_environment_setup": model[
                "hash_verified_during_environment_setup"
            ],
        }
    else:
        model = config["model"]
        model_identity = {
            "name": model["name"],
            "size": model["size"],
            "digest": model["digest"],
            "format": model["details"]["format"],
            "parameter_size": model["details"]["parameter_size"],
            "quantization_level": model["details"]["quantization_level"],
        }

    identity: dict[str, Any] = {"model": model_identity}
    for key in (
        "binary",
        "endpoint",
        "source_version",
        "arguments",
        "server_arguments",
        "request",
    ):
        if key in config:
            identity[key] = config[key]
    return identity


def _environment_identity(manifest: dict[str, Any]) -> dict[str, Any]:
    environment = manifest["environment"]
    return {
        "hostname": environment["hostname"],
        "platform": environment["platform"],
        "machine": environment["machine"],
        "python": environment["python"],
        "packages": environment["packages"],
        "jetpack_packages": environment["jetpack_packages"]["output"],
        "nvcc": environment["nvcc"]["output"],
        "nvpmodel": environment["nvpmodel"]["output"],
        "ollama": environment["ollama"]["output"],
    }


def _validate_resource_intervals(intervals: Iterable[Any]) -> int:
    observed = set(intervals)
    if observed != {EXPECTED_RESOURCE_INTERVAL_MS}:
        rendered = sorted(repr(value) for value in observed)
        raise ValueError(
            "resource interval mismatch: expected "
            f"{EXPECTED_RESOURCE_INTERVAL_MS} ms, got {rendered}"
        )
    return EXPECTED_RESOURCE_INTERVAL_MS


def _discover_records(
    formal_root: Path,
    *,
    expected_sessions: int,
    runs_per_session: int,
) -> tuple[list[RunRecord], dict[str, Any]]:
    expected_names = [
        f"session-{index:02d}" for index in range(1, expected_sessions + 1)
    ]
    session_dirs = sorted(
        path for path in formal_root.glob("session-*") if path.is_dir()
    )
    session_names = [path.name for path in session_dirs]
    if session_names != expected_names:
        raise ValueError(
            f"session layout mismatch: expected {expected_names}, got {session_names}"
        )

    records: list[RunRecord] = []
    inventory: dict[str, Any] = {}
    for session_index, session_dir in enumerate(session_dirs, start=1):
        expected_repetitions = list(
            range(
                (session_index - 1) * runs_per_session + 1,
                session_index * runs_per_session + 1,
            )
        )
        inventory[session_dir.name] = {}
        for workload in WORKLOADS:
            warmups = sorted(
                path
                for path in (session_dir / workload / "warmup").glob("*")
                if path.is_dir()
            )
            measured = sorted(
                path
                for path in (session_dir / workload / "measured").glob("*")
                if path.is_dir()
            )
            repetitions = [_parse_repetition(path) for path in measured]
            warmup_repetitions = [_parse_repetition(path) for path in warmups]
            if len(warmups) != EXPECTED_WARMUPS[workload]:
                raise ValueError(
                    f"{session_dir.name}/{workload}: expected "
                    f"{EXPECTED_WARMUPS[workload]} warmups, got {len(warmups)}"
                )
            if any(repetition != 0 for repetition in warmup_repetitions):
                raise ValueError(
                    f"{session_dir.name}/{workload}: warmups must use repetition 0"
                )
            if repetitions != expected_repetitions:
                raise ValueError(
                    f"{session_dir.name}/{workload}: expected repetitions "
                    f"{expected_repetitions}, got {repetitions}"
                )

            inventory[session_dir.name][workload] = {
                "warmup_count": len(warmups),
                "measured_count": len(measured),
                "measured_repetitions": repetitions,
            }
            for role, run_dirs in (("warmup", warmups), ("measured", measured)):
                for run_dir in run_dirs:
                    errors = validate_run_dir(run_dir)
                    if errors:
                        raise ValueError(f"{run_dir}: {'; '.join(errors)}")
                    manifest = json.loads(
                        (run_dir / "manifest.json").read_text(encoding="utf-8")
                    )
                    summary = summarize_run_dir(run_dir)
                    if summary["workload"] != workload:
                        raise ValueError(f"{run_dir}: workload does not match layout")
                    if summary["sample_role"] != role:
                        raise ValueError(
                            f"{run_dir}: sample role does not match layout"
                        )
                    if manifest["repetition"] != _parse_repetition(run_dir):
                        raise ValueError(f"{run_dir}: repetition does not match run_id")
                    safety = manifest["safety"]
                    if safety["motion_enabled"] or not safety["motion_value_valid"]:
                        raise ValueError(f"{run_dir}: unsafe motion setting")
                    records.append(
                        RunRecord(
                            session=session_dir.name,
                            session_index=session_index,
                            repetition=_parse_repetition(run_dir),
                            role=role,
                            run_dir=run_dir,
                            manifest=manifest,
                            summary=summary,
                        )
                    )

    runner_commits = {record.summary["runner_git"]["commit"] for record in records}
    baseline_commits = {record.summary["baseline_commit"] for record in records}
    statuses = {record.summary["result"]["status"] for record in records}
    resource_intervals = {record.manifest["resource_interval_ms"] for record in records}
    environment_identities = {
        json.dumps(
            _environment_identity(record.manifest),
            sort_keys=True,
            ensure_ascii=False,
        )
        for record in records
    }
    if len(runner_commits) != 1:
        raise ValueError(f"runner commit mismatch: {sorted(runner_commits)}")
    if len(baseline_commits) != 1:
        raise ValueError(f"baseline commit mismatch: {sorted(baseline_commits)}")
    if any(record.summary["runner_git"]["dirty"] for record in records):
        raise ValueError("formal dataset contains a dirty runner")
    if statuses != {"ok"}:
        raise ValueError(f"formal dataset contains non-ok statuses: {sorted(statuses)}")
    resource_interval_ms = _validate_resource_intervals(resource_intervals)
    if len(environment_identities) != 1:
        raise ValueError("formal dataset contains mixed environment identities")

    workload_configs: dict[str, Any] = {}
    for workload in WORKLOADS:
        identities = {
            json.dumps(
                _workload_config_identity(record.manifest),
                sort_keys=True,
                ensure_ascii=False,
            )
            for record in records
            if record.summary["workload"] == workload
        }
        if len(identities) != 1:
            raise ValueError(f"{workload}: workload configuration mismatch")
        workload_configs[workload] = json.loads(next(iter(identities)))

    metadata = {
        "inventory": inventory,
        "runner_commit": next(iter(runner_commits)),
        "baseline_commit": next(iter(baseline_commits)),
        "result_statuses": sorted(statuses),
        "resource_interval_ms": resource_interval_ms,
        "environment": json.loads(next(iter(environment_identities))),
        "workload_configs": workload_configs,
    }
    return records, metadata


def _values_by_session(
    records: list[RunRecord], getter: Callable[[RunRecord], float]
) -> dict[str, list[float]]:
    result: dict[str, list[float]] = {}
    for record in records:
        result.setdefault(record.session, []).append(float(getter(record)))
    return result


def _analyze_metric(
    records: list[RunRecord],
    getter: Callable[[RunRecord], float],
    *,
    metric_name: str,
    resamples: int,
    base_seed: int,
) -> dict[str, Any]:
    return formal_stats(
        _values_by_session(records, getter),
        resamples=resamples,
        seed=_metric_seed(base_seed, metric_name),
    )


def _stage_duration(record: RunRecord, key: str) -> float:
    for stage in record.summary["timing"]["stages"]:
        if f"{stage['component']}/{stage['stage']}" == key:
            return float(stage["duration_ms"])
    raise ValueError(f"{record.run_dir}: missing timing stage {key}")


def _session_summaries(records: list[RunRecord]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for session in sorted({record.session for record in records}):
        selected = [record for record in records if record.session == session]
        result[session] = {
            "repetitions": [record.repetition for record in selected],
            "experiment_duration_ms": _descriptive_stats(
                [
                    record.summary["timing"]["experiment_duration_ms"]
                    for record in selected
                ]
            ),
        }
    return result


def _analyze_workload(
    workload: str,
    records: list[RunRecord],
    *,
    resamples: int,
    seed: int,
) -> dict[str, Any]:
    input_hashes = {record.summary["input_sha256"] for record in records}
    residency_policies = {record.summary["residency_policy"] for record in records}
    if len(input_hashes) != 1 or len(residency_policies) != 1:
        raise ValueError(f"{workload}: input or residency policy mismatch")

    timing: dict[str, Any] = {
        "experiment_duration_ms": _analyze_metric(
            records,
            lambda record: record.summary["timing"]["experiment_duration_ms"],
            metric_name=f"{workload}/experiment_duration_ms",
            resamples=resamples,
            base_seed=seed,
        ),
        "stages": {},
    }
    stage_signatures = [
        [
            f"{stage['component']}/{stage['stage']}"
            for stage in record.summary["timing"]["stages"]
        ]
        for record in records
    ]
    common_stages = [
        key
        for key in stage_signatures[0]
        if all(key in signature for signature in stage_signatures)
    ]
    for key in common_stages:
        timing["stages"][key] = _analyze_metric(
            records,
            lambda record, stage_key=key: _stage_duration(record, stage_key),
            metric_name=f"{workload}/stage/{key}",
            resamples=resamples,
            base_seed=seed,
        )

    outputs: dict[str, Any] = {
        "unique_text_hashes": len(
            {record.summary["result"]["text_sha256"] for record in records}
        ),
        "output_chars": _analyze_metric(
            records,
            lambda record: record.summary["result"]["output_chars"],
            metric_name=f"{workload}/output_chars",
            resamples=resamples,
            base_seed=seed,
        ),
        "translation_route_counts": dict(
            Counter(
                record.summary["result"]["translation_route"]
                for record in records
                if record.summary["result"]["translation_route"] is not None
            )
        )
        or None,
        "token_usage": None,
    }
    if all(record.summary["result"].get("token_usage") for record in records):
        outputs["token_usage"] = {
            field: _analyze_metric(
                records,
                lambda record, token_field=field: record.summary["result"][
                    "token_usage"
                ][token_field],
                metric_name=f"{workload}/token/{field}",
                resamples=resamples,
                base_seed=seed,
            )
            for field in TOKEN_FIELDS
        }

    resources: dict[str, Any] = {
        "sample_count": _analyze_metric(
            records,
            lambda record: record.summary["resources"]["sample_count"],
            metric_name=f"{workload}/resource/sample_count",
            resamples=resamples,
            base_seed=seed,
        )
    }
    for field in RESOURCE_FIELDS:
        resources[field] = {
            "run_mean": _analyze_metric(
                records,
                lambda record, resource_field=field: record.summary["resources"][
                    resource_field
                ]["mean"],
                metric_name=f"{workload}/resource/{field}/run_mean",
                resamples=resamples,
                base_seed=seed,
            ),
            "observed_peak": max(
                record.summary["resources"][field]["max"] for record in records
            ),
        }

    route_timing: dict[str, Any] | None = None
    if workload == "vlm":
        route_timing = {}
        routes = sorted(outputs["translation_route_counts"] or {})
        for route in routes:
            selected = [
                record
                for record in records
                if record.summary["result"]["translation_route"] == route
            ]
            route_timing[route] = {
                "count": len(selected),
                "experiment_duration_ms": _analyze_metric(
                    selected,
                    lambda record: record.summary["timing"]["experiment_duration_ms"],
                    metric_name=f"vlm/route/{route}/experiment_duration_ms",
                    resamples=resamples,
                    base_seed=seed,
                ),
            }

    return {
        "measured_run_count": len(records),
        "input_sha256": next(iter(input_hashes)),
        "residency_policy": next(iter(residency_policies)),
        "sessions": _session_summaries(records),
        "timing": timing,
        "outputs": outputs,
        "resources": resources,
        "translation_route_timing": route_timing,
    }


def _diagnostics(measured: dict[str, list[RunRecord]]) -> dict[str, Any]:
    llm_records = measured["llm"]
    completion_tokens = np.asarray(
        [
            record.summary["result"]["token_usage"]["completion_tokens"]
            for record in llm_records
        ],
        dtype=float,
    )
    llama_duration = np.asarray(
        [_stage_duration(record, "llama/inference") for record in llm_records],
        dtype=float,
    )

    vlm_trends: dict[str, Any] = {}
    for session in sorted({record.session for record in measured["vlm"]}):
        records = [record for record in measured["vlm"] if record.session == session]
        vlm_trends[session] = {
            "experiment_duration_ms": linear_trend(
                [
                    record.summary["timing"]["experiment_duration_ms"]
                    for record in records
                ]
            ),
            "moondream_inference_ms": linear_trend(
                [_stage_duration(record, "moondream/inference") for record in records]
            ),
        }

    return {
        "llm_completion_tokens_vs_llama_inference_ms_pearson_r": float(
            np.corrcoef(completion_tokens, llama_duration)[0, 1]
        ),
        "vlm_within_session_trends": vlm_trends,
        "interpretation": (
            "The LLM correlation diagnoses output-length-driven request latency. "
            "VLM slopes and half-session contrasts describe serial warm-state trends; "
            "they are not causal estimates of workload order."
        ),
    }


def analyze_formal_root(
    formal_root: Path | str,
    *,
    bootstrap_resamples: int = 100_000,
    bootstrap_seed: int = 20_260_824,
    expected_sessions: int = 3,
    runs_per_session: int = 10,
) -> dict[str, Any]:
    root = Path(formal_root)
    records, metadata = _discover_records(
        root,
        expected_sessions=expected_sessions,
        runs_per_session=runs_per_session,
    )
    measured = {
        workload: [
            record
            for record in records
            if record.role == "measured" and record.summary["workload"] == workload
        ]
        for workload in WORKLOADS
    }
    workloads = {
        workload: _analyze_workload(
            workload,
            measured[workload],
            resamples=bootstrap_resamples,
            seed=bootstrap_seed,
        )
        for workload in WORKLOADS
    }
    role_counts = Counter(record.role for record in records)
    planned_measured = expected_sessions * runs_per_session * len(WORKLOADS)

    return {
        "schema_version": "0.1.0",
        "analysis_kind": "formal_baseline",
        "interpretation_limit": (
            "Results describe fixed inputs on one validated Jetson configuration. "
            "They do not establish VLM accuracy in live robot scenes or isolate "
            "causal workload-order effects."
        ),
        "formal_root": root.as_posix(),
        "analysis_parameters": {
            "p95_method": "nearest-rank",
            "bootstrap": {
                "method": "session-stratified percentile bootstrap",
                "confidence_level": 0.95,
                "resamples": bootstrap_resamples,
                "base_seed": bootstrap_seed,
                "stratification": ("resample within each session at its observed size"),
            },
            "outlier_policy": "no post-hoc exclusions",
        },
        "dataset": {
            "session_count": expected_sessions,
            "runs_per_session_per_workload": runs_per_session,
            "planned_measured_count": planned_measured,
            "successful_measured_count": role_counts["measured"],
            "measured_completion_rate": role_counts["measured"] / planned_measured,
            "warmup_count": role_counts["warmup"],
            "total_run_count": len(records),
            "runner_commit": metadata["runner_commit"],
            "baseline_commit": metadata["baseline_commit"],
            "runner_dirty_any": False,
            "result_statuses": metadata["result_statuses"],
            "resource_interval_ms": metadata["resource_interval_ms"],
            "environment": metadata["environment"],
            "workload_configs": metadata["workload_configs"],
            "inventory": metadata["inventory"],
        },
        "workloads": workloads,
        "diagnostics": _diagnostics(measured),
    }


def _format_ci(stats: dict[str, Any], key: str) -> str:
    interval = stats[f"{key}_ci95"]
    return f"{stats[key]:.3f} " f"[{interval['low']:.3f}, {interval['high']:.3f}]"


def render_markdown(analysis: dict[str, Any]) -> str:
    parameters = analysis["analysis_parameters"]
    dataset = analysis["dataset"]
    workloads = analysis["workloads"]
    lines = [
        "# Phase 0 Formal Baseline Results",
        "",
        (
            "> 中文简介：本报告汇总固定输入、固定 Jetson 配置下的 30 次 ASR、"
            "30 次 LLM 与 30 次 VLM 正式同步基线测量。结果仅描述该受控配置，"
            "不代表真实机器人场景中的视觉准确率。"
        ),
        "",
        "## Dataset and method",
        "",
        f"- Measured completion: {dataset['successful_measured_count']}/"
        f"{dataset['planned_measured_count']} "
        f"({dataset['measured_completion_rate'] * 100:.1f}%).",
        f"- Warm-ups retained but excluded: {dataset['warmup_count']}.",
        f"- Runner: `{dataset['runner_commit']}`; functional baseline: "
        f"`{dataset['baseline_commit']}`; dirty runs: none.",
        f"- p95: {parameters['p95_method']}.",
        (
            f"- Bootstrap: {parameters['bootstrap']['method']}, "
            f"{parameters['bootstrap']['resamples']:,} resamples, "
            f"base seed {parameters['bootstrap']['base_seed']}."
        ),
        f"- Outliers: {parameters['outlier_policy']}.",
        "",
        "## Headline timing results",
        "",
        "| Workload | n | End-to-end mean ms (95% CI) | Median ms (95% CI) | p95 ms | CV |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for workload in WORKLOADS:
        stats = workloads[workload]["timing"]["experiment_duration_ms"]
        lines.append(
            f"| {workload.upper()} | {stats['count']} | "
            f"{_format_ci(stats, 'mean')} | {_format_ci(stats, 'median')} | "
            f"{stats['p95_nearest_rank']:.3f} | {stats['cv_pct']:.3f}% |"
        )

    for workload in WORKLOADS:
        result = workloads[workload]
        lines.extend(["", f"## {workload.upper()}", ""])
        lines.extend(
            [
                "| Timing metric | Mean ms (95% CI) | Median ms (95% CI) | SD ms | CV | p95 ms | Min–max ms |",
                "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
            ]
        )
        metrics = {"end-to-end": result["timing"]["experiment_duration_ms"]}
        metrics.update(
            {
                key: value
                for key, value in result["timing"]["stages"].items()
                if key != "runner/experiment"
            }
        )
        for name, stats in metrics.items():
            lines.append(
                f"| {name} | {_format_ci(stats, 'mean')} | "
                f"{_format_ci(stats, 'median')} | "
                f"{stats['sample_stddev']:.3f} | {stats['cv_pct']:.3f}% | "
                f"{stats['p95_nearest_rank']:.3f} | "
                f"{stats['min']:.3f}–{stats['max']:.3f} |"
            )

        lines.extend(
            [
                "",
                "| Session | Repetitions | End-to-end mean ms | Median ms | CV |",
                "| --- | --- | ---: | ---: | ---: |",
            ]
        )
        for session, session_result in result["sessions"].items():
            stats = session_result["experiment_duration_ms"]
            repetitions = session_result["repetitions"]
            lines.append(
                f"| {session} | {repetitions[0]}–{repetitions[-1]} | "
                f"{stats['mean']:.3f} | {stats['median']:.3f} | "
                f"{stats['cv_pct']:.3f}% |"
            )

        if workload == "llm":
            token_usage = result["outputs"]["token_usage"]
            lines.extend(["", "### Length-normalized LLM metrics", ""])
            lines.extend(
                [
                    "| Metric | Mean (95% CI) | Median (95% CI) | CV | p95 | Min–max |",
                    "| --- | ---: | ---: | ---: | ---: | ---: |",
                ]
            )
            for name in (
                "completion_tokens",
                "request_completion_tokens_per_second",
                "request_ms_per_completion_token",
            ):
                stats = token_usage[name]
                lines.append(
                    f"| {name} | {_format_ci(stats, 'mean')} | "
                    f"{_format_ci(stats, 'median')} | {stats['cv_pct']:.3f}% | "
                    f"{stats['p95_nearest_rank']:.3f} | "
                    f"{stats['min']:.3f}–{stats['max']:.3f} |"
                )

        lines.extend(["", "### Resource summary", ""])
        lines.extend(
            [
                "| Resource | Mean of run means (95% CI) | CV | Observed peak |",
                "| --- | ---: | ---: | ---: |",
            ]
        )
        for field in RESOURCE_FIELDS:
            resource = result["resources"][field]
            stats = resource["run_mean"]
            lines.append(
                f"| {field} | {_format_ci(stats, 'mean')} | "
                f"{stats['cv_pct']:.3f}% | {resource['observed_peak']:.3f} |"
            )

    diagnostics = analysis["diagnostics"]
    lines.extend(
        [
            "",
            "## Diagnostics and interpretation",
            "",
            (
                "LLM completion-token count and llama request duration had Pearson "
                f"r = {diagnostics['llm_completion_tokens_vs_llama_inference_ms_pearson_r']:.4f}. "
                "End-to-end variability is therefore interpreted together with "
                "tokens/s, not as hardware instability alone."
            ),
            "",
            "VLM showed a repeated within-session warm-state trend:",
            "",
            "| Session | E2E slope ms/run | E2E second vs first half | Moondream slope ms/run | Moondream second vs first half |",
            "| --- | ---: | ---: | ---: | ---: |",
        ]
    )
    for session, trends in diagnostics["vlm_within_session_trends"].items():
        e2e = trends["experiment_duration_ms"]
        moondream = trends["moondream_inference_ms"]
        lines.append(
            f"| {session} | {e2e['slope_per_run']:.3f} | "
            f"{e2e['second_vs_first_pct']:.3f}% | "
            f"{moondream['slope_per_run']:.3f} | "
            f"{moondream['second_vs_first_pct']:.3f}% |"
        )

    lines.extend(
        [
            "",
            "The primary VLM distribution retains all predefined measured runs and "
            "therefore represents the observed mixture of colder and warmer states. "
            "The trend is reported rather than removed post hoc.",
            "",
            "## Limits",
            "",
            "- Confidence intervals are conditional on the three observed sessions and "
            "preserve their equal weights; they are not population-wide hardware claims.",
            "- The balanced workload order helps reveal order sensitivity, but session, "
            "day and order remain confounded with only three sessions.",
            "- LLM and VLM outputs remain stochastic because sampling was not newly fixed "
            "for this measurement phase.",
            "- The C100 product image is a fixed performance input, not a live-scene "
            "visual-accuracy benchmark.",
            "",
        ]
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("formal_root", type=Path)
    parser.add_argument("--bootstrap-resamples", type=int, default=100_000)
    parser.add_argument("--bootstrap-seed", type=int, default=20_260_824)
    parser.add_argument("--json-output", type=Path)
    parser.add_argument("--markdown-output", type=Path)
    args = parser.parse_args(argv)

    try:
        analysis = analyze_formal_root(
            args.formal_root,
            bootstrap_resamples=args.bootstrap_resamples,
            bootstrap_seed=args.bootstrap_seed,
        )
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}")
        return 1

    json_text = json.dumps(analysis, ensure_ascii=False, indent=2, allow_nan=False)
    if args.json_output:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(json_text + "\n", encoding="utf-8")
    else:
        print(json_text)

    if args.markdown_output:
        args.markdown_output.parent.mkdir(parents=True, exist_ok=True)
        args.markdown_output.write_text(render_markdown(analysis), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
