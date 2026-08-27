"""Descriptive summaries for one Phase 1 simulated-load run."""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Iterable, Mapping

from experiments.phase1.manifest import write_json_atomic
from experiments.phase1.replay_lifecycle import (
    TraceProfile,
    load_events,
    replay_events,
)
from experiments.phase1.simulation import SimulationCondition


SUMMARY_SCHEMA_VERSION = "0.1.0"


def nearest_rank(values: Iterable[int], percentile: int) -> int:
    ordered = sorted(values)
    if not ordered:
        raise ValueError("nearest-rank percentile requires at least one value")
    if isinstance(percentile, bool) or not isinstance(percentile, int):
        raise TypeError("percentile must be an integer")
    if not 1 <= percentile <= 100:
        raise ValueError("percentile must be between 1 and 100")
    rank = math.ceil(percentile / 100 * len(ordered))
    return ordered[rank - 1]


def _describe_ns(values: list[int]) -> dict[str, int | float] | None:
    if not values:
        return None
    return {
        "count": len(values),
        "min_ns": min(values),
        "mean_ns": statistics.fmean(values),
        "median_ns": statistics.median(values),
        "p50_ns": nearest_rank(values, 50),
        "p95_ns": nearest_rank(values, 95),
        "p99_ns": nearest_rank(values, 99),
        "max_ns": max(values),
    }


def _require_detail_int(event: Mapping[str, object], key: str) -> int:
    details = event.get("details")
    if not isinstance(details, Mapping):
        raise ValueError(f"event {event.get('seq')} has no details object")
    value = details.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"event {event.get('seq')} has no integer {key}")
    return value


def _probe_summary(events: list[dict[str, object]]) -> dict[str, object]:
    ticks = [event for event in events if event.get("event") == "probe.tick"]
    lateness = [_require_detail_int(event, "start_lateness_ns") for event in ticks]
    execution = [_require_detail_int(event, "execution_ns") for event in ticks]
    gaps = [
        value
        for event in ticks
        if (value := event["details"].get("actual_period_ns")) is not None
        and isinstance(value, int)
        and not isinstance(value, bool)
    ]
    absolute_period_error = [
        value
        for event in ticks
        if (value := event["details"].get("absolute_period_error_ns")) is not None
        and isinstance(value, int)
        and not isinstance(value, bool)
    ]
    misses = sum(event["details"].get("deadline_miss") is True for event in ticks)
    skipped = sum(
        _require_detail_int(event, "skipped_releases")
        for event in events
        if event.get("event") == "probe.skipped"
    )
    period_ns = next(
        (
            _require_detail_int(event, "period_ns")
            for event in events
            if event.get("event") == "probe.started"
        ),
        None,
    )
    return {
        "tick_count": len(ticks),
        "period_ns": period_ns,
        "skipped_releases": skipped,
        "deadline_miss_count": misses,
        "deadline_miss_rate": misses / len(ticks) if ticks else None,
        "start_lateness": _describe_ns(lateness),
        "execution": _describe_ns(execution),
        "actual_period": _describe_ns(gaps),
        "absolute_period_error": _describe_ns(absolute_period_error),
        "maximum_observed_gap_ns": max(gaps) if gaps else None,
        "percentile_method": "nearest-rank",
    }


def _task_timing_summary(events: list[dict[str, object]]) -> dict[str, object]:
    created: dict[str, int] = {}
    source: dict[str, int] = {}
    queue_wait: list[int] = []
    service: list[int] = []
    terminal_age: list[int] = []

    for event in events:
        task_id = event.get("task_id")
        if not isinstance(task_id, str):
            continue
        name = event.get("event")
        if name == "task.enqueued":
            created[task_id] = _require_detail_int(event, "created_monotonic_ns")
            source_value = event.get("source_monotonic_ns")
            if isinstance(source_value, int) and not isinstance(source_value, bool):
                source[task_id] = source_value
        elif name == "task.started" and task_id in created:
            started = _require_detail_int(event, "started_monotonic_ns")
            queue_wait.append(started - created[task_id])
        elif name == "task.finished":
            started = _require_detail_int(event, "started_monotonic_ns")
            finished = _require_detail_int(event, "finished_monotonic_ns")
            service.append(finished - started)

        details = event.get("details")
        if not isinstance(details, Mapping) or "disposition" not in details:
            continue
        transition = details.get("transition_monotonic_ns")
        if (
            task_id in source
            and isinstance(transition, int)
            and not isinstance(transition, bool)
        ):
            terminal_age.append(transition - source[task_id])

    return {
        "queue_wait": _describe_ns(queue_wait),
        "service_time": _describe_ns(service),
        "terminal_age": _describe_ns(terminal_age),
    }


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


def _condition_gates(
    condition: SimulationCondition,
    replay: dict[str, object],
    scenario: Mapping[str, object],
) -> list[dict[str, object]]:
    dispositions = dict(replay["disposition_counts"])
    gates = [
        _gate(
            "probe_ticks_present",
            replay["probe_tick_count"] > 0,
            observed=replay["probe_tick_count"],
            requirement="at least one recorded probe tick",
        ),
        _gate(
            "stale_consumed_zero",
            replay["stale_consumed_count"] == 0,
            observed=replay["stale_consumed_count"],
            requirement="zero stale results consumed",
        ),
    ]
    if condition is SimulationCondition.R1_INLINE_SYNC:
        direct = scenario.get("direct_workload")
        outcome = (
            direct.get("execution_outcome") if isinstance(direct, Mapping) else None
        )
        gates.append(
            _gate(
                "direct_workload_completed",
                outcome == "ok",
                observed=outcome,
                requirement="direct workload outcome is ok",
            )
        )
    elif condition is SimulationCondition.R2_THREADED_SYNC:
        direct = scenario.get("direct_workload")
        outcome = (
            direct.get("execution_outcome") if isinstance(direct, Mapping) else None
        )
        gates.append(
            _gate(
                "direct_workload_completed",
                outcome == "ok",
                observed=outcome,
                requirement="direct workload outcome is ok",
            )
        )
    elif condition is SimulationCondition.R3_ASYNC:
        gates.append(
            _gate(
                "nominal_result_consumed",
                dispositions.get("consumed", 0) == 1,
                observed=dispositions.get("consumed", 0),
                requirement="exactly one accepted result",
            )
        )
    elif condition is SimulationCondition.R4_STALE:
        gates.append(
            _gate(
                "old_generation_rejected",
                dispositions.get("rejected_state", 0) == 1
                and replay["accepted_result_count"] == 0,
                observed={
                    "rejected_state": dispositions.get("rejected_state", 0),
                    "accepted": replay["accepted_result_count"],
                },
                requirement="one state rejection and zero accepted results",
            )
        )
    elif condition is SimulationCondition.R4_OVERFLOW:
        runtime = scenario.get("runtime")
        final_snapshot = (
            runtime.get("final_snapshot") if isinstance(runtime, Mapping) else None
        )
        configured = scenario.get("spec")
        expected_drops = (
            configured.get("overflow_submissions")
            if isinstance(configured, Mapping)
            else None
        )
        max_depth = (
            final_snapshot.get("max_pending_depth")
            if isinstance(final_snapshot, Mapping)
            else None
        )
        capacity = (
            configured.get("pending_capacity")
            if isinstance(configured, Mapping)
            else None
        )
        gates.extend(
            [
                _gate(
                    "overflow_dispositions_close",
                    isinstance(expected_drops, int)
                    and dispositions.get("dropped_overflow", 0) == expected_drops,
                    observed=dispositions.get("dropped_overflow", 0),
                    requirement="one drop for each configured overflow submission",
                ),
                _gate(
                    "pending_capacity_respected",
                    isinstance(max_depth, int)
                    and isinstance(capacity, int)
                    and max_depth <= capacity,
                    observed={"max_depth": max_depth, "capacity": capacity},
                    requirement="maximum pending depth does not exceed capacity",
                ),
            ]
        )

    if condition.uses_runtime:
        runtime = scenario.get("runtime")
        shutdown = runtime.get("shutdown") if isinstance(runtime, Mapping) else None
        snapshot = (
            runtime.get("final_snapshot") if isinstance(runtime, Mapping) else None
        )
        gates.extend(
            [
                _gate(
                    "runtime_shutdown_complete",
                    isinstance(shutdown, Mapping) and shutdown.get("complete") is True,
                    observed=(
                        shutdown.get("complete")
                        if isinstance(shutdown, Mapping)
                        else None
                    ),
                    requirement="worker joined with a closed and empty broker",
                ),
                _gate(
                    "runtime_accounting_closed",
                    isinstance(snapshot, Mapping)
                    and snapshot.get("accounting_holds") is True,
                    observed=(
                        snapshot.get("accounting_holds")
                        if isinstance(snapshot, Mapping)
                        else None
                    ),
                    requirement="submission and terminal accounting close",
                ),
            ]
        )
    return gates


def build_summary(
    events_path: Path | str,
    *,
    condition: SimulationCondition,
    profile: TraceProfile,
    scenario_report: Mapping[str, object],
    spec: Mapping[str, object],
) -> dict[str, object]:
    if not isinstance(condition, SimulationCondition):
        raise TypeError("condition must be a SimulationCondition")
    if profile is not condition.trace_profile:
        raise ValueError("trace profile does not match the simulation condition")
    events = load_events(events_path)
    replay_summary = asdict(replay_events(events, profile=profile))
    scenario = dict(scenario_report)
    scenario["spec"] = dict(spec)
    gates = _condition_gates(condition, replay_summary, scenario)
    return {
        "summary_schema_version": SUMMARY_SCHEMA_VERSION,
        "run_id": replay_summary["run_id"],
        "condition": condition.value,
        "trace_profile": profile.value,
        "descriptive_only": True,
        "inference_claim_permitted": False,
        "probe": _probe_summary(events),
        "task_timing": _task_timing_summary(events),
        "lifecycle": replay_summary,
        "scenario": scenario_report,
        "gates": gates,
        "valid": all(gate["passed"] is True for gate in gates),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("events", type=Path)
    parser.add_argument("scenario", type=Path)
    parser.add_argument(
        "--condition",
        choices=[item.value for item in SimulationCondition],
        required=True,
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        scenario = json.loads(args.scenario.read_text(encoding="utf-8"))
        if not isinstance(scenario, dict):
            raise ValueError("scenario file must contain an object")
        condition = SimulationCondition(args.condition)
        spec = scenario.get("spec")
        report = scenario.get("report")
        if not isinstance(spec, dict) or not isinstance(report, dict):
            raise ValueError("scenario file must contain spec and report objects")
        summary = build_summary(
            args.events,
            condition=condition,
            profile=condition.trace_profile,
            scenario_report=report,
            spec=spec,
        )
    except (OSError, ValueError) as exc:
        print(f"INVALID: {exc}", file=sys.stderr)
        return 2
    if args.output is None:
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    else:
        write_json_atomic(args.output, summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
