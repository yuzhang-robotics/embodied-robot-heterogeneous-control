"""Summarize timing and resource data from one valid Phase 0 run."""

from __future__ import annotations

import argparse
import csv
import json
import statistics
from pathlib import Path
from typing import Any

from .validate_run import validate_run_dir


def _number(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _stats(values: list[float]) -> dict[str, float] | None:
    if not values:
        return None
    return {
        "min": min(values),
        "mean": statistics.fmean(values),
        "max": max(values),
    }


def _token_count(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if number < 0 or number != value:
        return None
    return number


def _llm_token_usage(
    workload: str,
    result_details: dict[str, Any],
    durations: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if workload != "llm":
        return None

    raw_usage = result_details.get("usage")
    if not isinstance(raw_usage, dict):
        return None

    usage = {
        field: _token_count(raw_usage.get(field))
        for field in ("prompt_tokens", "completion_tokens", "total_tokens")
    }
    if all(value is None for value in usage.values()):
        return None

    inference_stage = next(
        (
            item
            for item in durations
            if item["component"] == "llama" and item["stage"] == "inference"
        ),
        None,
    )
    duration_ms = inference_stage["duration_ms"] if inference_stage else None
    completion_tokens = usage["completion_tokens"]
    if completion_tokens and duration_ms is not None and duration_ms > 0:
        usage["request_completion_tokens_per_second"] = (
            completion_tokens * 1000 / duration_ms
        )
        usage["request_ms_per_completion_token"] = duration_ms / completion_tokens
    else:
        usage["request_completion_tokens_per_second"] = None
        usage["request_ms_per_completion_token"] = None
    usage["rate_basis"] = (
        "completion_tokens divided by llama/inference wall duration; includes "
        "HTTP handling and prompt evaluation, so it is not decode-only throughput"
    )
    return usage


def _event_durations(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    open_events: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    durations: list[dict[str, Any]] = []

    for event in events:
        name = event["event"]
        if name.endswith(".start"):
            base = name.rsplit(".", 1)[0]
            key = (event["task_id"], event["component"], base)
            open_events.setdefault(key, []).append(event)
            continue
        if not name.endswith(".end"):
            continue

        base = name.rsplit(".", 1)[0]
        key = (event["task_id"], event["component"], base)
        starts = open_events.get(key)
        if not starts:
            continue
        start = starts.pop()
        duration = {
            "task_id": event["task_id"],
            "component": event["component"],
            "stage": base,
            "start_seq": start["seq"],
            "end_seq": event["seq"],
            "duration_ms": (
                event["monotonic_ns"] - start["monotonic_ns"]
            )
            / 1_000_000,
            "status": event["status"],
        }
        if event.get("details"):
            duration["end_details"] = event["details"]
        durations.append(duration)

    return sorted(durations, key=lambda item: item["start_seq"])


def summarize_run_dir(run_dir: Path | str) -> dict[str, Any]:
    directory = Path(run_dir)
    validation_errors = validate_run_dir(directory)
    if validation_errors:
        joined = "; ".join(validation_errors)
        raise ValueError(f"run directory is invalid: {joined}")

    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    result = json.loads((directory / "result.json").read_text(encoding="utf-8"))
    events = [
        json.loads(line)
        for line in (directory / "events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    durations = _event_durations(events)

    with (directory / "resources.csv").open(
        "r", encoding="utf-8", newline=""
    ) as stream:
        resources = list(csv.DictReader(stream))

    cpu_sample_means: list[float] = []
    for row in resources:
        core_values = [
            value
            for index in range(6)
            if (value := _number(row.get(f"cpu{index}_usage_pct"))) is not None
        ]
        if core_values:
            cpu_sample_means.append(statistics.fmean(core_values))

    ram = [
        value
        for row in resources
        if (value := _number(row.get("ram_used_mb"))) is not None
    ]
    gpu = [
        value
        for row in resources
        if (value := _number(row.get("gr3d_usage_pct"))) is not None
    ]
    temperature = [
        value
        for row in resources
        for field in (
            "temp_cpu_c",
            "temp_gpu_c",
            "temp_tj_c",
            "temp_soc0_c",
            "temp_soc1_c",
            "temp_soc2_c",
        )
        if (value := _number(row.get(field))) is not None
    ]
    vdd_in = [
        value
        for row in resources
        if (value := _number(row.get("vdd_in_mw"))) is not None
    ]

    experiment_stage = next(
        (
            item
            for item in durations
            if item["component"] == "runner" and item["stage"] == "experiment"
        ),
        None,
    )

    result_details = result.get("result", {})
    workload = manifest["workload"]
    return {
        "schema_version": manifest["schema_version"],
        "run_id": manifest["run_id"],
        "workload": workload,
        "sample_role": manifest["sample_role"],
        "residency_policy": manifest["residency_policy"],
        "baseline_commit": manifest.get("baseline_commit"),
        "runner_git": manifest.get("environment", {}).get("git", {}),
        "input_sha256": manifest["input"]["sha256"],
        "result": {
            "status": result["status"],
            "text_sha256": result.get("text_sha256"),
            "output_chars": result_details.get("output_chars"),
            "translation_route": result_details.get("translation_route"),
            "token_usage": _llm_token_usage(workload, result_details, durations),
        },
        "timing": {
            "experiment_duration_ms": (
                experiment_stage["duration_ms"] if experiment_stage else None
            ),
            "stages": durations,
        },
        "resources": {
            "sample_count": len(resources),
            "cpu_mean_across_cores_pct": _stats(cpu_sample_means),
            "gr3d_usage_pct": _stats(gpu),
            "ram_used_mb": _stats(ram),
            "temperature_all_sensors_c": _stats(temperature),
            "vdd_in_mw": _stats(vdd_in),
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    args = parser.parse_args(argv)
    try:
        summary = summarize_run_dir(args.run_dir)
    except ValueError as exc:
        print(f"ERROR: {exc}")
        return 1
    print(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
