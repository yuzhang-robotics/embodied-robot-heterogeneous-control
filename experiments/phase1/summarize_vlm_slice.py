"""Independent summary construction for one fixed-input VLM slice run."""

from __future__ import annotations

import re
from dataclasses import asdict
from pathlib import Path
from typing import Mapping, Sequence

from experiments.phase1.jetson_telemetry import summarize_resource_samples
from experiments.phase1.replay_lifecycle import TraceProfile, replay_file
from experiments.phase1.vlm_adapter import (
    C100_INPUT_SHA256,
    C100_INPUT_SIZE_BYTES,
)
from experiments.phase1.vlm_slice import VLMSliceCondition


VLM_SUMMARY_SCHEMA_VERSION = "0.1.0"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


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


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def build_vlm_summary(
    event_path: Path | str,
    *,
    condition: VLMSliceCondition,
    spec: Mapping[str, object],
    report: Mapping[str, object],
    resource_samples: Sequence[dict[str, object]],
    sampler_report: Mapping[str, object],
    development_injection: bool = False,
) -> dict[str, object]:
    """Rebuild lifecycle, adapter and resource Gates from serialized facts."""

    if not isinstance(condition, VLMSliceCondition):
        raise TypeError("condition must be a VLMSliceCondition")
    if not isinstance(development_injection, bool):
        raise TypeError("development_injection must be boolean")
    replay = replay_file(
        event_path,
        profile=TraceProfile.RUNTIME_THREADED_PROBE,
    )
    lifecycle = asdict(replay)
    disposition_counts = dict(replay.disposition_counts)
    adapter = _mapping(report.get("adapter"))
    adapter_input = _mapping(adapter.get("input"))
    adapter_output = _mapping(adapter.get("output"))
    cancellation = _mapping(adapter.get("cancellation"))
    model_residency = _mapping(adapter.get("model_residency"))
    stages = _mapping(adapter.get("stage_status"))
    stage_durations = _mapping(adapter.get("stage_durations_ns"))
    shutdown = _mapping(report.get("shutdown"))
    probe = _mapping(report.get("probe"))
    final_snapshot = _mapping(report.get("final_snapshot"))
    resource_summary = summarize_resource_samples(resource_samples)

    fixed_stages_ok = all(
        stages.get(name) == "ok"
        for name in (
            "input_verify_before",
            "module_import",
            "moondream_inference",
            "output_normalization",
            "model_unload",
            "input_verify_after",
        )
    )
    route = adapter.get("translation_route")
    route_ok = (route == "qwen" and stages.get("qwen_rewrite") == "ok") or (
        route == "argos"
        and stages.get("qwen_rewrite") == "error"
        and stages.get("argos_fallback") == "ok"
    )
    durations_ok = bool(stage_durations) and all(
        isinstance(value, int) and not isinstance(value, bool) and value >= 0
        for value in stage_durations.values()
    )
    output_sha256 = adapter_output.get("sha256")
    output_length = adapter_output.get("length")
    output_private = (
        isinstance(output_sha256, str)
        and _SHA256_RE.fullmatch(output_sha256) is not None
        and isinstance(output_length, int)
        and not isinstance(output_length, bool)
        and output_length > 0
        and adapter_output.get("raw_text_recorded") is False
    )
    resource_times = [sample.get("sample_monotonic_ns") for sample in resource_samples]
    started_ns = adapter.get("started_monotonic_ns")
    finished_ns = adapter.get("finished_monotonic_ns")
    covered_samples = sum(
        isinstance(value, int)
        and isinstance(started_ns, int)
        and isinstance(finished_ns, int)
        and started_ns <= value <= finished_ns
        for value in resource_times
    )

    expected_disposition = (
        "consumed" if condition is VLMSliceCondition.ASYNC else "rejected_state"
    )
    expected_accepted = int(condition is VLMSliceCondition.ASYNC)
    expected_outcome = (
        "ok" if condition is VLMSliceCondition.ASYNC else "cancel_observed"
    )
    gates = [
        _gate(
            "single_request",
            replay.submission_attempts == 1
            and replay.admitted_total == 1
            and replay.terminal_admitted_total == 1,
            observed={
                "submission_attempts": replay.submission_attempts,
                "admitted_total": replay.admitted_total,
                "terminal_admitted_total": replay.terminal_admitted_total,
            },
            requirement="exactly one VLM request is admitted and terminal",
        ),
        _gate(
            "bounded_lane",
            replay.max_pending_depth <= 1
            and replay.max_result_depth <= 1
            and final_snapshot.get("accounting_holds") is True,
            observed={
                "max_pending_depth": replay.max_pending_depth,
                "max_result_depth": replay.max_result_depth,
                "accounting_holds": final_snapshot.get("accounting_holds"),
            },
            requirement="pending and result depths remain at most one",
        ),
        _gate(
            "expected_disposition",
            disposition_counts == {expected_disposition: 1}
            and report.get("final_disposition") == expected_disposition
            and replay.accepted_result_count == expected_accepted
            and report.get("consumed") is bool(expected_accepted),
            observed={
                "disposition_counts": disposition_counts,
                "accepted_result_count": replay.accepted_result_count,
                "reported_disposition": report.get("final_disposition"),
                "reported_consumed": report.get("consumed"),
            },
            requirement=(
                "nominal output is consumed once"
                if condition is VLMSliceCondition.ASYNC
                else "old-generation output is rejected before consumption"
            ),
        ),
        _gate(
            "stale_zero_consumed",
            replay.stale_consumed_count == 0,
            observed=replay.stale_consumed_count,
            requirement="no stale result is consumed",
        ),
        _gate(
            "fixed_input_verified",
            adapter_input.get("sha256") == C100_INPUT_SHA256
            and adapter_input.get("size_bytes") == C100_INPUT_SIZE_BYTES
            and fixed_stages_ok,
            observed={
                "sha256": adapter_input.get("sha256"),
                "size_bytes": adapter_input.get("size_bytes"),
                "verification_before": stages.get("input_verify_before"),
                "verification_after": stages.get("input_verify_after"),
            },
            requirement="the Phase 0 C100 identity holds before and after inference",
        ),
        _gate(
            "pipeline_completed",
            adapter.get("execution_outcome") == expected_outcome
            and fixed_stages_ok
            and route_ok
            and durations_ok,
            observed={
                "execution_outcome": adapter.get("execution_outcome"),
                "translation_route": route,
                "stage_status": dict(stages),
            },
            requirement=(
                "Moondream, translation, normalization and the unload call complete"
            ),
        ),
        _gate(
            "model_unload_claim_bounded",
            model_residency.get("unload_requested") is True
            and model_residency.get("unload_confirmed") is None,
            observed=dict(model_residency),
            requirement=(
                "the unload request returns without claiming confirmed eviction"
            ),
        ),
        _gate(
            "output_private",
            output_private,
            observed={
                "sha256_present": isinstance(output_sha256, str),
                "length": output_length,
                "raw_text_recorded": adapter_output.get("raw_text_recorded"),
            },
            requirement="only output hash and length enter public artifacts",
        ),
        _gate(
            "cancellation_claim_bounded",
            (
                cancellation.get("requested") is False
                and cancellation.get("worker_observed") is False
                if condition is VLMSliceCondition.ASYNC
                else cancellation.get("requested") is True
                and cancellation.get("worker_observed") is True
                and cancellation.get("backend_stop_confirmed") is None
            ),
            observed=dict(cancellation),
            requirement=(
                "nominal execution has no cancellation request"
                if condition is VLMSliceCondition.ASYNC
                else "state invalidation does not claim backend preemption"
            ),
        ),
        _gate(
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
            requirement="worker and periodic probe terminate within their budgets",
        ),
        _gate(
            "resource_trace_valid",
            sampler_report.get("successful") is True
            and sampler_report.get("sample_count") == len(resource_samples)
            and covered_samples > 0,
            observed={
                "sampler_successful": sampler_report.get("successful"),
                "sample_count": len(resource_samples),
                "inference_interval_samples": covered_samples,
            },
            requirement="valid resource samples cover the VLM execution interval",
        ),
    ]
    return {
        "vlm_summary_schema_version": VLM_SUMMARY_SCHEMA_VERSION,
        "run_id": replay.run_id,
        "condition": condition.value,
        "trace_profile": TraceProfile.RUNTIME_THREADED_PROBE.value,
        "descriptive_only": True,
        "development_injection": development_injection,
        "real_vlm_path_executed": all(
            gate["passed"] is True
            for gate in gates
            if gate["name"]
            in {
                "fixed_input_verified",
                "pipeline_completed",
                "output_private",
            }
        )
        and not development_injection,
        "formal_performance_claim_permitted": False,
        "heterogeneous_inference_claim_permitted": False,
        "lifecycle": lifecycle,
        "spec": dict(spec),
        "adapter": dict(adapter),
        "resources": {
            "sampler_report": dict(sampler_report),
            "inference_interval_sample_count": covered_samples,
            "summary": resource_summary,
        },
        "gates": gates,
        "valid": all(gate["passed"] is True for gate in gates),
    }
