"""Independent summary construction for one fixed-input LLM slice run."""

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

from experiments.phase1.workloads.llm.adapter import (
    LLM_EMPTY_HISTORY_SHA256,
    LLM_INPUT_MEDIA_TYPE,
    LLM_INPUT_SHA256,
    LLM_INPUT_SIZE_BYTES,
    frozen_llm_request_contract,
)
from experiments.phase1.workloads.llm.slice import LLMSliceCondition


LLM_SUMMARY_SCHEMA_VERSION = "0.1.0"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def build_llm_summary(
    event_path: Path | str,
    *,
    condition: LLMSliceCondition,
    spec: Mapping[str, object],
    report: Mapping[str, object],
    resource_samples: Sequence[dict[str, object]],
    sampler_report: Mapping[str, object],
    development_injection: bool = False,
) -> dict[str, object]:
    """Rebuild lifecycle, HTTP-boundary and resource Gates from serialized facts."""

    if not isinstance(condition, LLMSliceCondition):
        raise TypeError("condition must be an LLMSliceCondition")
    if not isinstance(development_injection, bool):
        raise TypeError("development_injection must be boolean")
    context = build_summary_context(event_path, report, resource_samples)
    adapter = context.adapter
    adapter_input = _mapping(adapter.get("input"))
    adapter_output = _mapping(adapter.get("output"))
    request = _mapping(adapter.get("request"))
    response = _mapping(adapter.get("response"))
    usage = _mapping(response.get("usage"))
    residency = _mapping(adapter.get("model_residency"))
    cancellation = _mapping(adapter.get("cancellation"))
    stages = _mapping(adapter.get("stage_status"))
    stage_durations = _mapping(adapter.get("stage_durations_ns"))

    fixed_stages_ok = all(
        stages.get(name) == "ok"
        for name in (
            "input_verify_before",
            "request_build",
            "llama_inference",
            "response_parse",
            "input_verify_after",
        )
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
        and response.get("raw_response_recorded") is False
    )
    expected_request = {
        **frozen_llm_request_contract(),
        "raw_prompt_recorded": False,
    }
    request_contract_ok = request == expected_request
    token_usage_ok = (
        set(usage) == {"prompt_tokens", "completion_tokens", "total_tokens"}
        and all(
            isinstance(usage.get(name), int)
            and not isinstance(usage.get(name), bool)
            and usage.get(name) >= 0
            for name in ("prompt_tokens", "completion_tokens", "total_tokens")
        )
        and usage.get("prompt_tokens", 0) > 0
        and usage.get("completion_tokens", 0) > 0
        and usage.get("total_tokens", 0)
        >= usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0)
    )
    residency_bounded = (
        residency.get("policy") == "external_llama_server_resident"
        and residency.get("server_preexisting") is True
        and residency.get("unload_requested") is False
        and residency.get("backend_stop_confirmed") is None
    )
    expected_outcome = (
        "ok" if condition is LLMSliceCondition.ASYNC else "cancel_observed"
    )
    cancellation_bounded = (
        cancellation.get("requested") is False
        and cancellation.get("worker_observed") is False
        and cancellation.get("client_wait_stopped") is False
        and cancellation.get("backend_stop_confirmed") is None
        if condition is LLMSliceCondition.ASYNC
        else cancellation.get("requested") is True
        and cancellation.get("worker_observed") is True
        and cancellation.get("client_wait_stopped") is False
        and cancellation.get("backend_stop_confirmed") is None
    )
    stale_observation_s = spec.get("stale_observation_s")
    stale_observation_ns = (
        int(stale_observation_s * 1_000_000_000)
        if isinstance(stale_observation_s, (int, float))
        and not isinstance(stale_observation_s, bool)
        else None
    )
    adapter_duration_ns = adapter.get("duration_ns")
    stale_observation_holds = condition is LLMSliceCondition.ASYNC or (
        isinstance(stale_observation_ns, int)
        and stale_observation_ns > 0
        and isinstance(adapter_duration_ns, int)
        and adapter_duration_ns >= stale_observation_ns
        and cancellation.get("worker_observed") is True
    )

    gates = common_lifecycle_gates(
        context,
        report,
        condition_is_async=condition is LLMSliceCondition.ASYNC,
        pending_capacity=1,
        result_capacity=1,
        lane_gate_name="bounded_conversation_lane",
        single_requirement="exactly one LLM request is admitted and terminal",
        lane_requirement="LLM pending and result depths remain at most one",
        nominal_disposition_requirement="nominal response identity is consumed once",
        stale_disposition_requirement=(
            "old-generation LLM response is rejected before consumption"
        ),
        stale_zero_requirement="no stale LLM response is consumed",
    ) + [
        _gate(
            "fixed_prompt_verified",
            adapter_input.get("sha256") == LLM_INPUT_SHA256
            and adapter_input.get("size_bytes") == LLM_INPUT_SIZE_BYTES
            and adapter_input.get("media_type") == LLM_INPUT_MEDIA_TYPE
            and adapter_input.get("raw_text_recorded") is False
            and spec.get("history_messages") == 0
            and spec.get("history_sha256") == LLM_EMPTY_HISTORY_SHA256
            and fixed_stages_ok,
            observed={
                "input": dict(adapter_input),
                "history_messages": spec.get("history_messages"),
                "history_sha256": spec.get("history_sha256"),
                "verification_before": stages.get("input_verify_before"),
                "verification_after": stages.get("input_verify_after"),
            },
            requirement="the fixed prompt and empty history identities hold through inference",
        ),
        _gate(
            "request_contract_verified",
            request_contract_ok,
            observed=dict(request),
            requirement="the Phase 0 text-free chat request contract is unchanged",
        ),
        _gate(
            "llama_request_completed",
            adapter.get("execution_outcome") == expected_outcome
            and fixed_stages_ok
            and durations_ok,
            observed={
                "execution_outcome": adapter.get("execution_outcome"),
                "stage_status": dict(stages),
            },
            requirement="the local llama.cpp request and response parsing complete",
        ),
        _gate(
            "token_usage_valid",
            token_usage_ok,
            observed=dict(usage),
            requirement="llama.cpp reports bounded prompt, completion and total token counts",
        ),
        _gate(
            "output_private",
            output_private,
            observed={
                "sha256_present": isinstance(output_sha256, str),
                "length": output_length,
                "raw_text_recorded": adapter_output.get("raw_text_recorded"),
                "raw_response_recorded": response.get("raw_response_recorded"),
            },
            requirement="only response identity and token counts enter artifacts",
        ),
        _gate(
            "server_residency_claim_bounded",
            residency_bounded,
            observed=dict(residency),
            requirement="the pre-existing server remains externally managed without a stop claim",
        ),
        _gate(
            "cancellation_claim_bounded",
            cancellation_bounded,
            observed=dict(cancellation),
            requirement=(
                "nominal execution has no cancellation request"
                if condition is LLMSliceCondition.ASYNC
                else "state invalidation is observed without claiming HTTP or backend preemption"
            ),
        ),
        _gate(
            "stale_observation_window",
            stale_observation_holds,
            observed={
                "condition": condition.value,
                "required_observation_ns": stale_observation_ns,
                "adapter_duration_ns": adapter_duration_ns,
            },
            requirement=(
                "the stale request remains active through the observation control"
                if condition is LLMSliceCondition.STALE
                else "the stale-only observation control is not applied"
            ),
        ),
    ] + closing_gates(
        context,
        sampler_report,
        resource_samples,
        threads_requirement="worker and periodic probe terminate within their budgets",
        resources_requirement="valid resource samples cover the LLM adapter interval",
    )
    real_gate_names = {
        "fixed_prompt_verified",
        "request_contract_verified",
        "llama_request_completed",
        "token_usage_valid",
        "output_private",
    }
    return summary_record(
        context,
        schema_name="llm_summary_schema_version",
        schema_version=LLM_SUMMARY_SCHEMA_VERSION,
        condition=condition.value,
        spec=spec,
        sampler_report=sampler_report,
        gates=gates,
        development_injection=development_injection,
        real_path_name="real_llm_path_executed",
        real_gate_names=real_gate_names,
        additional_claims={
            "cancellation_latency_claim_permitted": False,
            "backend_cancellation_claim_permitted": False,
        },
    )
