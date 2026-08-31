"""Independent summary construction for one fixed-input ASR slice run."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Mapping, Sequence

from experiments.phase1.asr_adapter import (
    ASR_EXPECTED_OUTPUT_LENGTH,
    ASR_EXPECTED_OUTPUT_SHA256,
    ASR_INPUT_SHA256,
    ASR_INPUT_SIZE_BYTES,
)
from experiments.phase1.asr_slice import ASRSliceCondition
from experiments.phase1.jetson_telemetry import summarize_resource_samples
from experiments.phase1.replay_lifecycle import TraceProfile, replay_file


ASR_SUMMARY_SCHEMA_VERSION = "0.2.0"


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
    replay = replay_file(
        event_path,
        profile=TraceProfile.RUNTIME_THREADED_PROBE,
    )
    lifecycle = asdict(replay)
    disposition_counts = dict(replay.disposition_counts)
    adapter = _mapping(report.get("adapter"))
    adapter_input = _mapping(adapter.get("input"))
    adapter_output = _mapping(adapter.get("output"))
    process = _mapping(adapter.get("process"))
    cancellation = _mapping(adapter.get("cancellation"))
    shutdown = _mapping(report.get("shutdown"))
    probe = _mapping(report.get("probe"))
    final_snapshot = _mapping(report.get("final_snapshot"))
    resource_summary = summarize_resource_samples(resource_samples)

    expected_disposition = (
        "consumed" if condition is ASRSliceCondition.ASYNC else "rejected_state"
    )
    expected_accepted = int(condition is ASRSliceCondition.ASYNC)
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
            requirement="exactly one ASR utterance is admitted and terminal",
        ),
        _gate(
            "bounded_fifo_lane",
            replay.max_pending_depth <= 2
            and replay.max_result_depth <= 2
            and final_snapshot.get("accounting_holds") is True,
            observed={
                "max_pending_depth": replay.max_pending_depth,
                "max_result_depth": replay.max_result_depth,
                "accounting_holds": final_snapshot.get("accounting_holds"),
            },
            requirement="ASR pending and result depths remain at most two",
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
                "nominal transcript identity is consumed once"
                if condition is ASRSliceCondition.ASYNC
                else "old-generation ASR result is rejected before consumption"
            ),
        ),
        _gate(
            "stale_zero_consumed",
            replay.stale_consumed_count == 0,
            observed=replay.stale_consumed_count,
            requirement="no stale ASR result is consumed",
        ),
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
            requirement="valid resource samples cover the ASR execution interval",
        ),
    ]
    return {
        "asr_summary_schema_version": ASR_SUMMARY_SCHEMA_VERSION,
        "run_id": replay.run_id,
        "condition": condition.value,
        "trace_profile": TraceProfile.RUNTIME_THREADED_PROBE.value,
        "descriptive_only": True,
        "development_injection": development_injection,
        "real_asr_path_executed": all(
            gate["passed"] is True
            for gate in gates
            if gate["name"]
            in {
                "fixed_input_verified",
                "whisper_process_completed",
                "transcript_private_and_expected",
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
