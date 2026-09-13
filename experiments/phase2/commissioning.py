"""Frozen contract helpers for Phase 2 nonformal commissioning."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path

from experiments.phase2.evidence import CONTROL_CONDITION, PREFETCH_CONDITION


COMMISSIONING_PROTOCOL_SCHEMA_VERSION = "0.1.0"
COMMISSIONING_PROTOCOL_ID = "phase2_bounded_whisper_residency_commissioning_v1"
COMMISSIONING_PROTOCOL_SHA256 = (
    "49a4efdee5a0ba3f231c1b3149a431768acd080db0d2697a33139d34c536ee45"
)
DEFAULT_COMMISSIONING_PROTOCOL_PATH = Path(__file__).with_name("commissioning-v1.json")
COMMISSIONING_SESSION_COUNT = 2
COMMISSIONING_PAIRS_PER_SESSION = 1
COMMISSIONING_ATTEMPT = 1
MINIMUM_SESSION_SEPARATION_S = 1800

_EXPECTED_SCHEDULES = [
    {
        "session_index": 1,
        "conditions": [PREFETCH_CONDITION, CONTROL_CONDITION],
    },
    {
        "session_index": 2,
        "conditions": [CONTROL_CONDITION, PREFETCH_CONDITION],
    },
]
_EXPECTED_UNIT_SEQUENCE = [
    "pre_unit_observation",
    "asr_primer_1",
    "asr_primer_2",
    "post_primer_observation",
    "frozen_vlm",
    "confirmed_vlm_unload_and_child_reap",
    "common_post_vlm_observation",
    "condition_action",
    "measured_asr",
    "post_asr_observation",
    "recovery_asr",
]
_EXPECTED_PARAMETERS = {
    "prefetch_buffer_size_bytes": 4 * 1024 * 1024,
    "prefetch_timeout_s": 20.0,
    "resource_interval_ms": 200,
    "maximum_resource_gap_ms": 400,
    "probe_period_ms": 100,
    "probe_deadline_ms": 100,
    "probe_join_timeout_s": 5.0,
    "responsiveness_p95_reference_ms": 300,
    "thermal_start_maximum_tj_c": 55.0,
    "thermal_start_consecutive_samples": 10,
    "thermal_stop_tj_c": 85.0,
}
_EXPECTED_SAFETY = {
    "robot_enable_motion_value": "0",
    "motion_enabled": False,
    "uart_access": False,
    "application_modules_loaded": False,
    "live_inputs": False,
}
_EXPECTED_STOPPING = {
    "replacement_sessions_allowed": False,
    "replacement_units_allowed": False,
    "outcome_driven_extension_allowed": False,
    "threshold_changes_allowed": False,
    "alternative_mitigation_allowed": False,
}
_PROTOCOL_FIELDS = {
    "protocol_schema_version",
    "protocol_id",
    "status",
    "evidence_namespace",
    "formal_evidence",
    "confirmatory_data",
    "application_slice_authorized",
    "required_branch",
    "session_count",
    "pairs_per_session",
    "attempt",
    "minimum_session_separation_s",
    "restart_model_services_each_session",
    "session_schedules",
    "unit_sequence",
    "parameters",
    "safety",
    "stopping",
}


class CommissioningProtocolError(ValueError):
    """The commissioning contract is unavailable or has changed."""


def canonical_commissioning_protocol_text(protocol: Mapping[str, object]) -> str:
    """Return the sole canonical representation used for protocol identity."""

    return (
        json.dumps(
            dict(protocol),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )


def commissioning_protocol_sha256(protocol: Mapping[str, object]) -> str:
    return hashlib.sha256(
        canonical_commissioning_protocol_text(protocol).encode("utf-8")
    ).hexdigest()


def commissioning_protocol_errors(protocol: Mapping[str, object]) -> list[str]:
    """Return exact-contract errors without weakening the correctness treatment."""

    errors: list[str] = []
    if set(protocol) != _PROTOCOL_FIELDS:
        errors.append("commissioning protocol fields are invalid")
    expected_scalars = {
        "protocol_schema_version": COMMISSIONING_PROTOCOL_SCHEMA_VERSION,
        "protocol_id": COMMISSIONING_PROTOCOL_ID,
        "status": "active_after_reviewed_merge",
        "evidence_namespace": "phase2_nonformal_commissioning",
        "formal_evidence": False,
        "confirmatory_data": False,
        "application_slice_authorized": False,
        "required_branch": "main",
        "session_count": COMMISSIONING_SESSION_COUNT,
        "pairs_per_session": COMMISSIONING_PAIRS_PER_SESSION,
        "attempt": COMMISSIONING_ATTEMPT,
        "minimum_session_separation_s": MINIMUM_SESSION_SEPARATION_S,
        "restart_model_services_each_session": True,
    }
    for name, expected in expected_scalars.items():
        if protocol.get(name) != expected:
            errors.append(f"commissioning protocol {name} is invalid")
    if protocol.get("session_schedules") != _EXPECTED_SCHEDULES:
        errors.append("commissioning session schedules are invalid")
    if protocol.get("unit_sequence") != _EXPECTED_UNIT_SEQUENCE:
        errors.append("commissioning unit sequence is invalid")
    if protocol.get("parameters") != _EXPECTED_PARAMETERS:
        errors.append("commissioning parameters changed from the tested treatment")
    if protocol.get("safety") != _EXPECTED_SAFETY:
        errors.append("commissioning safety boundary is invalid")
    if protocol.get("stopping") != _EXPECTED_STOPPING:
        errors.append("commissioning stopping rules are invalid")
    return errors


def load_commissioning_protocol(
    path: Path | str = DEFAULT_COMMISSIONING_PROTOCOL_PATH,
) -> dict[str, object]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CommissioningProtocolError(
            "commissioning protocol could not be loaded"
        ) from exc
    if not isinstance(value, Mapping):
        raise CommissioningProtocolError("commissioning protocol must be an object")
    protocol = dict(value)
    errors = commissioning_protocol_errors(protocol)
    if errors:
        raise CommissioningProtocolError("; ".join(errors))
    return protocol


def commissioning_conditions(
    protocol: Mapping[str, object], session_index: int
) -> tuple[str, str]:
    """Return the exact complementary order for one commissioning session."""

    errors = commissioning_protocol_errors(protocol)
    if errors:
        raise CommissioningProtocolError("; ".join(errors))
    if (
        isinstance(session_index, bool)
        or not isinstance(session_index, int)
        or not 1 <= session_index <= COMMISSIONING_SESSION_COUNT
    ):
        raise CommissioningProtocolError("commissioning session index is invalid")
    schedules = protocol["session_schedules"]
    if not isinstance(schedules, list):
        raise CommissioningProtocolError("commissioning schedules are unavailable")
    schedule = schedules[session_index - 1]
    if not isinstance(schedule, Mapping):
        raise CommissioningProtocolError("commissioning schedule is invalid")
    conditions = schedule.get("conditions")
    if not isinstance(conditions, list) or len(conditions) != 2:
        raise CommissioningProtocolError("commissioning pair is invalid")
    return str(conditions[0]), str(conditions[1])
