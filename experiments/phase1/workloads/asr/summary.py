"""Independent summary construction for one fixed-input ASR slice run."""

from __future__ import annotations

from pathlib import Path
from typing import Mapping, Sequence

from experiments.phase1.workloads.asr.adapter import (
    ASR_EXPECTED_OUTPUT_LENGTH,
    ASR_EXPECTED_OUTPUT_SHA256,
    ASR_INPUT_SHA256,
    ASR_INPUT_SIZE_BYTES,
)
from experiments.phase1.workloads.asr.slice import ASRSliceCondition
from experiments.phase1.workloads._summary import (
    build_summary_context,
    closing_gates,
    common_lifecycle_gates,
    gate as _gate,
    mapping as _mapping,
    summary_record,
)


ASR_SUMMARY_SCHEMA_VERSION = "0.2.0"


def build_asr_summary(
    event_path: Path | str,
    *,
    condition: ASRSliceCondition,
    spec: Mapping[str, object],
    report: Mapping[str, object],
    resource_samples: Sequence[dict[str, object]],
    sampler_report: Mapping[str, object],
    development_injection: bool = False,
) -> dict[str, object]:
    """Rebuild lifecycle, process and resource Gates from serialized facts."""

    if not isinstance(condition, ASRSliceCondition):
        raise TypeError("condition must be an ASRSliceCondition")
    if not isinstance(development_injection, bool):
        raise TypeError("development_injection must be boolean")
    context = build_summary_context(event_path, report, resource_samples)
    adapter = context.adapter
    adapter_input = _mapping(adapter.get("input"))
    adapter_output = _mapping(adapter.get("output"))
    process = _mapping(adapter.get("process"))
    cancellation = _mapping(adapter.get("cancellation"))
    expected_outcome = (
        "ok" if condition is ASRSliceCondition.ASYNC else "cancel_observed"
    )
    output_matches = (
        adapter_output.get("sha256") == ASR_EXPECTED_OUTPUT_SHA256
        and adapter_output.get("length") == ASR_EXPECTED_OUTPUT_LENGTH
        and adapter_output.get("raw_text_recorded") is False
        if condition is ASRSliceCondition.ASYNC
        else adapter.get("output") is None
    )
    process_closed = (
        process.get("started") is True
        and process.get("reaped") is True
        and (
            process.get("exit_code") == 0
            and process.get("terminate_requested") is False
            and process.get("kill_requested") is False
            if condition is ASRSliceCondition.ASYNC
            else process.get("terminate_requested") is True
            and (
                process.get("terminate_confirmed") is True
                or process.get("kill_confirmed") is True
            )
        )
    )
    cancellation_bounded = (
        cancellation.get("requested") is False
        and cancellation.get("worker_observed") is False
        and cancellation.get("client_wait_stopped") is False
        and cancellation.get("backend_stop_confirmed") is None
        if condition is ASRSliceCondition.ASYNC
        else cancellation.get("requested") is True
        and cancellation.get("worker_observed") is True
        and cancellation.get("client_wait_stopped") is True
        and cancellation.get("backend_stop_confirmed") is True
    )
    stale_observation_s = spec.get("stale_observation_s")
    stale_observation_ns = (
        int(stale_observation_s * 1_000_000_000)
        if isinstance(stale_observation_s, (int, float))
        and not isinstance(stale_observation_s, bool)
        else None
    )
    adapter_duration_ns = adapter.get("duration_ns")
    stale_observation_holds = condition is ASRSliceCondition.ASYNC or (
        isinstance(stale_observation_ns, int)
        and stale_observation_ns > 0
        and isinstance(adapter_duration_ns, int)
        and adapter_duration_ns >= stale_observation_ns
    )

    gates = common_lifecycle_gates(
        context,
        report,
        condition_is_async=condition is ASRSliceCondition.ASYNC,
        pending_capacity=2,
        result_capacity=2,
        lane_gate_name="bounded_fifo_lane",
        single_requirement="exactly one ASR utterance is admitted and terminal",
        lane_requirement="ASR pending and result depths remain at most two",
        nominal_disposition_requirement="nominal transcript identity is consumed once",
        stale_disposition_requirement=(
            "old-generation ASR result is rejected before consumption"
        ),
        stale_zero_requirement="no stale ASR result is consumed",
    ) + [
        _gate(
            "fixed_input_verified",
            adapter_input.get("sha256") == ASR_INPUT_SHA256
            and adapter_input.get("size_bytes") == ASR_INPUT_SIZE_BYTES
            and adapter_input.get("media_type") == "audio/wav",
            observed=dict(adapter_input),
            requirement="the Phase 0 fixed WAV identity holds through inference",
        ),
        _gate(
            "whisper_process_completed",
            adapter.get("execution_outcome") == expected_outcome and process_closed,
            observed={
                "execution_outcome": adapter.get("execution_outcome"),
                "process": dict(process),
            },
            requirement=(
                "Whisper exits with code zero and is reaped"
                if condition is ASRSliceCondition.ASYNC
                else "state invalidation stops and reaps the Whisper process"
            ),
        ),
        _gate(
            "transcript_private_and_expected",
            output_matches,
            observed={
                "sha256": adapter_output.get("sha256"),
                "length": adapter_output.get("length"),
                "raw_text_recorded": adapter_output.get("raw_text_recorded"),
                "output_absent": adapter.get("output") is None,
            },
            requirement=(
                "the nominal transcript matches Phase 0 and only identity is recorded"
                if condition is ASRSliceCondition.ASYNC
                else "cancelled ASR output is absent from the result envelope"
            ),
        ),
        _gate(
            "cancellation_claim_bounded",
            cancellation_bounded,
            observed=dict(cancellation),
            requirement=(
                "nominal execution has no cancellation request"
                if condition is ASRSliceCondition.ASYNC
                else "state invalidation is confirmed at the Whisper process boundary"
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
                "the stale condition observes active Whisper long enough for telemetry"
                if condition is ASRSliceCondition.STALE
                else "the stale-only observation control is not applied"
            ),
        ),
    ] + closing_gates(
        context,
        sampler_report,
        resource_samples,
        threads_requirement="worker and periodic probe terminate within their budgets",
        resources_requirement=(
            "valid resource samples cover the ASR execution interval"
        ),
    )
    return summary_record(
        context,
        schema_name="asr_summary_schema_version",
        schema_version=ASR_SUMMARY_SCHEMA_VERSION,
        condition=condition.value,
        spec=spec,
        sampler_report=sampler_report,
        gates=gates,
        development_injection=development_injection,
        real_path_name="real_asr_path_executed",
        real_gate_names={
            "fixed_input_verified",
            "whisper_process_completed",
            "transcript_private_and_expected",
        },
    )
