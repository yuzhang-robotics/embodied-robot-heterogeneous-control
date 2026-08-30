"""Deterministic process-boundary Gates for one fixed-input VLM run."""

from __future__ import annotations

from typing import Mapping

from experiments.phase1.vlm_process_adapter import PROCESS_PROTOCOL_VERSION
from experiments.phase1.vlm_slice import VLMSliceCondition


VLM_PROCESS_SUMMARY_SCHEMA_VERSION = "0.1.0"
VLM_PROCESS_ISOLATION = "spawned_process"


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


def _integer(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def build_vlm_process_summary(
    process_report: Mapping[str, object],
    *,
    condition: VLMSliceCondition,
) -> dict[str, object]:
    """Rebuild process ownership and cleanup Gates from serialized facts."""

    if not isinstance(process_report, Mapping):
        raise TypeError("process_report must be a mapping")
    if not isinstance(condition, VLMSliceCondition):
        raise TypeError("condition must be a VLMSliceCondition")

    process_id = _integer(process_report.get("process_id"))
    spawn_ns = _integer(process_report.get("spawn_requested_monotonic_ns"))
    child_started_ns = _integer(process_report.get("child_started_monotonic_ns"))
    inference_started_ns = _integer(
        process_report.get("inference_started_monotonic_ns")
    )
    completion_ns = _integer(process_report.get("completion_received_monotonic_ns"))
    joined_ns = _integer(process_report.get("joined_monotonic_ns"))
    expected_cancellation = condition is VLMSliceCondition.STALE
    cancellation_forwarded = process_report.get("cancellation_forwarded")
    cancellation_ns = _integer(
        process_report.get("cancellation_forwarded_monotonic_ns")
    )

    gates = [
        _gate(
            "spawned_process",
            process_report.get("start_method") == "spawn"
            and process_id is not None
            and process_id > 0,
            observed={
                "start_method": process_report.get("start_method"),
                "process_id_present": process_id is not None,
            },
            requirement="the VLM adapter executes in one spawned child process",
        ),
        _gate(
            "bounded_protocol",
            process_report.get("protocol_version") == PROCESS_PROTOCOL_VERSION
            and process_report.get("protocol_complete") is True
            and process_report.get("error_code") is None,
            observed={
                "protocol_version": process_report.get("protocol_version"),
                "protocol_complete": process_report.get("protocol_complete"),
                "error_code": process_report.get("error_code"),
            },
            requirement="the bounded child protocol closes without an error",
        ),
        _gate(
            "process_reaped",
            process_report.get("exit_code") == 0
            and process_report.get("terminate_requested") is False
            and process_report.get("terminate_confirmed") is False,
            observed={
                "exit_code": process_report.get("exit_code"),
                "terminate_requested": process_report.get("terminate_requested"),
                "terminate_confirmed": process_report.get("terminate_confirmed"),
            },
            requirement="the child exits normally and is reaped without termination",
        ),
        _gate(
            "boundary_order",
            all(
                value is not None
                for value in (
                    spawn_ns,
                    child_started_ns,
                    inference_started_ns,
                    completion_ns,
                    joined_ns,
                )
            )
            and spawn_ns <= child_started_ns <= inference_started_ns
            and inference_started_ns <= completion_ns <= joined_ns,
            observed={
                "spawn_requested_monotonic_ns": spawn_ns,
                "child_started_monotonic_ns": child_started_ns,
                "inference_started_monotonic_ns": inference_started_ns,
                "completion_received_monotonic_ns": completion_ns,
                "joined_monotonic_ns": joined_ns,
            },
            requirement="spawn, inference, completion and join boundaries are ordered",
        ),
        _gate(
            "cancellation_forwarding",
            cancellation_forwarded is expected_cancellation
            and (
                cancellation_ns is not None
                if expected_cancellation
                else cancellation_ns is None
            )
            and (
                not expected_cancellation
                or (
                    inference_started_ns is not None
                    and completion_ns is not None
                    and inference_started_ns <= cancellation_ns <= completion_ns
                )
            ),
            observed={
                "expected": expected_cancellation,
                "forwarded": cancellation_forwarded,
                "forwarded_monotonic_ns": cancellation_ns,
            },
            requirement=(
                "state invalidation is forwarded to the child"
                if expected_cancellation
                else "the nominal request forwards no cancellation"
            ),
        ),
    ]
    return {
        "vlm_process_summary_schema_version": (VLM_PROCESS_SUMMARY_SCHEMA_VERSION),
        "adapter_isolation": VLM_PROCESS_ISOLATION,
        "condition": condition.value,
        "valid": all(gate["passed"] for gate in gates),
        "process": dict(process_report),
        "gates": gates,
    }
