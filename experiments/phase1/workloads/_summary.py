"""Shared evidence primitives for fixed-input workload summaries."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Mapping, Sequence

from experiments.phase1.common.replay import ReplaySummary, TraceProfile, replay_file
from experiments.phase1.common.telemetry_jetson import summarize_resource_samples


@dataclass(frozen=True, slots=True)
class SliceSummaryContext:
    replay: ReplaySummary
    lifecycle: dict[str, object]
    disposition_counts: dict[str, int]
    adapter: Mapping[str, object]
    shutdown: Mapping[str, object]
    probe: Mapping[str, object]
    final_snapshot: Mapping[str, object]
    resource_summary: dict[str, object]
    covered_samples: int


def gate(
    name: str,
    passed: bool,
    *,
    observed: object,
    requirement: str,
) -> dict[str, object]:
    """Build one independently checkable summary gate."""

    return {
        "name": name,
        "passed": passed,
        "observed": observed,
        "requirement": requirement,
    }


def mapping(value: object) -> Mapping[str, object]:
    """Normalize an optional nested record without mutating it."""

    return value if isinstance(value, Mapping) else {}


def build_summary_context(
    event_path: Path | str,
    report: Mapping[str, object],
    resource_samples: Sequence[dict[str, object]],
) -> SliceSummaryContext:
    """Reconstruct the lifecycle and common runtime/resource views."""

    replay = replay_file(event_path, profile=TraceProfile.RUNTIME_THREADED_PROBE)
    adapter = mapping(report.get("adapter"))
    started_ns = adapter.get("started_monotonic_ns")
    finished_ns = adapter.get("finished_monotonic_ns")
    covered_samples = sum(
        isinstance(value, int)
        and isinstance(started_ns, int)
        and isinstance(finished_ns, int)
        and started_ns <= value <= finished_ns
        for value in (
            sample.get("sample_monotonic_ns") for sample in resource_samples
        )
    )
    return SliceSummaryContext(
        replay=replay,
        lifecycle=asdict(replay),
        disposition_counts=dict(replay.disposition_counts),
        adapter=adapter,
        shutdown=mapping(report.get("shutdown")),
        probe=mapping(report.get("probe")),
        final_snapshot=mapping(report.get("final_snapshot")),
        resource_summary=summarize_resource_samples(resource_samples),
        covered_samples=covered_samples,
    )


def common_lifecycle_gates(
    context: SliceSummaryContext,
    report: Mapping[str, object],
    *,
    condition_is_async: bool,
    pending_capacity: int,
    result_capacity: int,
    lane_gate_name: str,
    single_requirement: str,
    lane_requirement: str,
    nominal_disposition_requirement: str,
    stale_disposition_requirement: str,
    stale_zero_requirement: str,
) -> list[dict[str, object]]:
    """Build the four lifecycle gates shared by all workload slices."""

    replay = context.replay
    expected_disposition = "consumed" if condition_is_async else "rejected_state"
    expected_accepted = int(condition_is_async)
    return [
        gate(
            "single_request",
            replay.submission_attempts == 1
            and replay.admitted_total == 1
            and replay.terminal_admitted_total == 1,
            observed={
                "submission_attempts": replay.submission_attempts,
                "admitted_total": replay.admitted_total,
                "terminal_admitted_total": replay.terminal_admitted_total,
            },
            requirement=single_requirement,
        ),
        gate(
            lane_gate_name,
            replay.max_pending_depth <= pending_capacity
            and replay.max_result_depth <= result_capacity
            and context.final_snapshot.get("accounting_holds") is True,
            observed={
                "max_pending_depth": replay.max_pending_depth,
                "max_result_depth": replay.max_result_depth,
                "accounting_holds": context.final_snapshot.get("accounting_holds"),
            },
            requirement=lane_requirement,
        ),
        gate(
            "expected_disposition",
            context.disposition_counts == {expected_disposition: 1}
            and report.get("final_disposition") == expected_disposition
            and replay.accepted_result_count == expected_accepted
            and report.get("consumed") is bool(expected_accepted),
            observed={
                "disposition_counts": context.disposition_counts,
                "accepted_result_count": replay.accepted_result_count,
                "reported_disposition": report.get("final_disposition"),
                "reported_consumed": report.get("consumed"),
            },
            requirement=(
                nominal_disposition_requirement
                if condition_is_async
                else stale_disposition_requirement
            ),
        ),
        gate(
            "stale_zero_consumed",
            replay.stale_consumed_count == 0,
            observed=replay.stale_consumed_count,
            requirement=stale_zero_requirement,
        ),
    ]


def closing_gates(
    context: SliceSummaryContext,
    sampler_report: Mapping[str, object],
    resource_samples: Sequence[dict[str, object]],
    *,
    threads_requirement: str,
    resources_requirement: str,
) -> list[dict[str, object]]:
    """Build common thread-closure and resource-coverage gates."""

    shutdown = context.shutdown
    probe = context.probe
    return [
        gate(
            "threads_closed",
            shutdown.get("complete") is True
            and shutdown.get("joined") is True
            and probe.get("joined") is True
            and probe.get("error_code") is None,
            observed={
                "shutdown_complete": shutdown.get("complete"),
                "worker_joined": shutdown.get("joined"),
                "probe_joined": probe.get("joined"),
                "probe_error_code": probe.get("error_code"),
            },
            requirement=threads_requirement,
        ),
        gate(
            "resource_trace_valid",
            sampler_report.get("successful") is True
            and sampler_report.get("sample_count") == len(resource_samples)
            and context.covered_samples > 0,
            observed={
                "sampler_successful": sampler_report.get("successful"),
                "sample_count": len(resource_samples),
                "inference_interval_samples": context.covered_samples,
            },
            requirement=resources_requirement,
        ),
    ]


def summary_record(
    context: SliceSummaryContext,
    *,
    schema_name: str,
    schema_version: str,
    condition: str,
    spec: Mapping[str, object],
    sampler_report: Mapping[str, object],
    gates: list[dict[str, object]],
    development_injection: bool,
    real_path_name: str,
    real_gate_names: set[str],
    additional_claims: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Serialize the common workload-summary envelope."""

    record: dict[str, object] = {
        schema_name: schema_version,
        "run_id": context.replay.run_id,
        "condition": condition,
        "trace_profile": TraceProfile.RUNTIME_THREADED_PROBE.value,
        "descriptive_only": True,
        "development_injection": development_injection,
        real_path_name: all(
            item["passed"] is True
            for item in gates
            if item["name"] in real_gate_names
        )
        and not development_injection,
        "formal_performance_claim_permitted": False,
    }
    if additional_claims is not None:
        record.update(additional_claims)
    record.update(
        {
            "heterogeneous_inference_claim_permitted": False,
            "lifecycle": context.lifecycle,
            "spec": dict(spec),
            "adapter": dict(context.adapter),
            "resources": {
                "sampler_report": dict(sampler_report),
                "inference_interval_sample_count": context.covered_samples,
                "summary": context.resource_summary,
            },
            "gates": gates,
            "valid": all(item["passed"] is True for item in gates),
        }
    )
    return record
