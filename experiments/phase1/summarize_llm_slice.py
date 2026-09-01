"""Independent summary construction for one fixed-input LLM slice run."""

from __future__ import annotations

import re
from dataclasses import asdict
from pathlib import Path
from typing import Mapping, Sequence

from experiments.phase1.jetson_telemetry import summarize_resource_samples
from experiments.phase1.replay_lifecycle import TraceProfile, replay_file

from .llm_adapter import (
    LLM_EMPTY_HISTORY_SHA256,
    LLM_INPUT_MEDIA_TYPE,
    LLM_INPUT_SHA256,
    LLM_INPUT_SIZE_BYTES,
    frozen_llm_request_contract,
)
from .llm_slice import LLMSliceCondition


LLM_SUMMARY_SCHEMA_VERSION = "0.1.0"
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
    replay = replay_file(event_path, profile=TraceProfile.RUNTIME_THREADED_PROBE)
    lifecycle = asdict(replay)
    disposition_counts = dict(replay.disposition_counts)
    adapter = _mapping(report.get("adapter"))
    adapter_input = _mapping(adapter.get("input"))
    adapter_output = _mapping(adapter.get("output"))
    request = _mapping(adapter.get("request"))
    response = _mapping(adapter.get("response"))
    usage = _mapping(response.get("usage"))
    residency = _mapping(adapter.get("model_residency"))
    cancellation = _mapping(adapter.get("cancellation"))
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
        "consumed" if condition is LLMSliceCondition.ASYNC else "rejected_state"
    )
    expected_accepted = int(condition is LLMSliceCondition.ASYNC)
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
            requirement="exactly one LLM request is admitted and terminal",
        ),
        _gate(
            "bounded_conversation_lane",
            replay.max_pending_depth <= 1
            and replay.max_result_depth <= 1
            and final_snapshot.get("accounting_holds") is True,
            observed={
                "max_pending_depth": replay.max_pending_depth,
                "max_result_depth": replay.max_result_depth,
                "accounting_holds": final_snapshot.get("accounting_holds"),
            },
            requirement="LLM pending and result depths remain at most one",
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
                "nominal response identity is consumed once"
                if condition is LLMSliceCondition.ASYNC
                else "old-generation LLM response is rejected before consumption"
            ),
        ),
        _gate(
            "stale_zero_consumed",
            replay.stale_consumed_count == 0,
            observed=replay.stale_consumed_count,
            requirement="no stale LLM response is consumed",
        ),
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
            requirement="valid resource samples cover the LLM adapter interval",
        ),
    ]
    real_gate_names = {
        "fixed_prompt_verified",
        "request_contract_verified",
        "llama_request_completed",
        "token_usage_valid",
        "output_private",
    }
    return {
        "llm_summary_schema_version": LLM_SUMMARY_SCHEMA_VERSION,
        "run_id": replay.run_id,
        "condition": condition.value,
        "trace_profile": TraceProfile.RUNTIME_THREADED_PROBE.value,
        "descriptive_only": True,
        "development_injection": development_injection,
        "real_llm_path_executed": all(
            gate["passed"] is True for gate in gates if gate["name"] in real_gate_names
        )
        and not development_injection,
        "formal_performance_claim_permitted": False,
        "cancellation_latency_claim_permitted": False,
        "backend_cancellation_claim_permitted": False,
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
