"""Independent summary construction for one fixed-input VLM slice run."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Mapping, Sequence

from experiments.phase1.workloads._summary import (
    build_summary_context,
    closing_gates,
    common_lifecycle_gates,
    gate as _gate,
    mapping as _mapping,
    summary_record,
)
from experiments.phase1.workloads.vlm.adapter import (
    C100_INPUT_SHA256,
    C100_INPUT_SIZE_BYTES,
)
from experiments.phase1.workloads.vlm.slice import VLMSliceCondition


VLM_SUMMARY_SCHEMA_VERSION = "0.2.0"
LEGACY_VLM_SUMMARY_SCHEMA_VERSION = "0.1.0"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def build_vlm_summary(
    event_path: Path | str,
    *,
    condition: VLMSliceCondition,
    spec: Mapping[str, object],
    report: Mapping[str, object],
    resource_samples: Sequence[dict[str, object]],
    sampler_report: Mapping[str, object],
    development_injection: bool = False,
    schema_version: str = VLM_SUMMARY_SCHEMA_VERSION,
) -> dict[str, object]:
    """Rebuild lifecycle, adapter and resource Gates from serialized facts."""

    if not isinstance(condition, VLMSliceCondition):
        raise TypeError("condition must be a VLMSliceCondition")
    if not isinstance(development_injection, bool):
        raise TypeError("development_injection must be boolean")
    if schema_version not in {
        LEGACY_VLM_SUMMARY_SCHEMA_VERSION,
        VLM_SUMMARY_SCHEMA_VERSION,
    }:
        raise ValueError("unsupported VLM summary schema version")
    context = build_summary_context(event_path, report, resource_samples)
    adapter = context.adapter
    adapter_input = _mapping(adapter.get("input"))
    adapter_output = _mapping(adapter.get("output"))
    cancellation = _mapping(adapter.get("cancellation"))
    model_residency = _mapping(adapter.get("model_residency"))
    stages = _mapping(adapter.get("stage_status"))
    stage_error_codes = _mapping(adapter.get("stage_error_codes"))
    stage_durations = _mapping(adapter.get("stage_durations_ns"))

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
    stage_error_codes_ok = (
        "stage_error_codes" not in adapter
        if schema_version == LEGACY_VLM_SUMMARY_SCHEMA_VERSION
        else set(stage_error_codes)
        == {name for name, status in stages.items() if status == "error"}
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
    expected_outcome = (
        "ok" if condition is VLMSliceCondition.ASYNC else "cancel_observed"
    )
    gates = common_lifecycle_gates(
        context,
        report,
        condition_is_async=condition is VLMSliceCondition.ASYNC,
        pending_capacity=1,
        result_capacity=1,
        lane_gate_name="bounded_lane",
        single_requirement="exactly one VLM request is admitted and terminal",
        lane_requirement="pending and result depths remain at most one",
        nominal_disposition_requirement="nominal output is consumed once",
        stale_disposition_requirement=(
            "old-generation output is rejected before consumption"
        ),
        stale_zero_requirement="no stale result is consumed",
    ) + [
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
            and durations_ok
            and stage_error_codes_ok,
            observed={
                "execution_outcome": adapter.get("execution_outcome"),
                "translation_route": route,
                "stage_status": dict(stages),
                "stage_error_codes": dict(stage_error_codes),
            },
            requirement=(
                "Moondream, translation, normalization and the unload call complete"
            ),
        ),
        _gate(
            "model_unload_claim_bounded",
            model_residency.get("unload_requested") is True
            and (
                model_residency.get("unload_confirmed") is None
                or model_residency.get("unload_confirmed") is True
            ),
            observed=dict(model_residency),
            requirement=(
                "the unload request returns without claiming confirmed eviction"
                if model_residency.get("unload_confirmed") is None
                else "the unload request records positive process-list confirmation"
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
    ] + closing_gates(
        context,
        sampler_report,
        resource_samples,
        threads_requirement="worker and periodic probe terminate within their budgets",
        resources_requirement=(
            "valid resource samples cover the VLM execution interval"
        ),
    )
    return summary_record(
        context,
        schema_name="vlm_summary_schema_version",
        schema_version=schema_version,
        condition=condition.value,
        spec=spec,
        sampler_report=sampler_report,
        gates=gates,
        development_injection=development_injection,
        real_path_name="real_vlm_path_executed",
        real_gate_names={
            "fixed_input_verified",
            "pipeline_completed",
            "output_private",
        },
    )
