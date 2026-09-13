"""Machine-readable contract for the Phase 2 target correctness pilot."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path

from experiments.phase2.evidence import CONTROL_CONDITION, PREFETCH_CONDITION


CORRECTNESS_PROTOCOL_SCHEMA_VERSION = "0.1.0"
CORRECTNESS_PROTOCOL_ID = "phase2_bounded_whisper_residency_correctness_pilot_v1"
CORRECTNESS_PROTOCOL_SHA256 = (
    "6cd017575d7d99edc4d96b9254a6a3e27a06dceeae30d84e208edbeaeac8748f"
)
DEFAULT_PROTOCOL_PATH = Path(__file__).with_name("correctness-pilot-v1.json")

_EXPECTED_CONDITIONS = [CONTROL_CONDITION, PREFETCH_CONDITION]
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
    "replacement_runs_allowed": False,
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
    "application_slice_authorized",
    "required_branch",
    "conditions",
    "unit_sequence",
    "parameters",
    "safety",
    "stopping",
}


class CorrectnessProtocolError(ValueError):
    """The pilot contract is missing, changed or internally inconsistent."""


def canonical_protocol_text(protocol: Mapping[str, object]) -> str:
    """Return the single canonical JSON representation used for identity."""

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


def protocol_sha256(protocol: Mapping[str, object]) -> str:
    return hashlib.sha256(canonical_protocol_text(protocol).encode("utf-8")).hexdigest()


def protocol_errors(protocol: Mapping[str, object]) -> list[str]:
    errors: list[str] = []
    if set(protocol) != _PROTOCOL_FIELDS:
        errors.append("protocol fields are invalid")
    if protocol.get("protocol_schema_version") != CORRECTNESS_PROTOCOL_SCHEMA_VERSION:
        errors.append("protocol schema version is invalid")
    if protocol.get("protocol_id") != CORRECTNESS_PROTOCOL_ID:
        errors.append("protocol id is invalid")
    if protocol.get("status") != "active_after_reviewed_merge":
        errors.append("protocol status is invalid")
    if protocol.get("evidence_namespace") != "phase2_nonformal_correctness_pilot":
        errors.append("evidence namespace is invalid")
    if protocol.get("formal_evidence") is not False:
        errors.append("correctness pilot cannot claim formal evidence")
    if protocol.get("application_slice_authorized") is not False:
        errors.append("correctness pilot cannot authorize application integration")
    if protocol.get("required_branch") != "main":
        errors.append("correctness pilot must run from main")
    if protocol.get("conditions") != _EXPECTED_CONDITIONS:
        errors.append("pilot must contain exactly one control and one treatment")
    if protocol.get("unit_sequence") != _EXPECTED_UNIT_SEQUENCE:
        errors.append("unit sequence is invalid")
    if protocol.get("parameters") != _EXPECTED_PARAMETERS:
        errors.append("pilot parameters are invalid")
    if protocol.get("safety") != _EXPECTED_SAFETY:
        errors.append("pilot safety boundary is invalid")
    if protocol.get("stopping") != _EXPECTED_STOPPING:
        errors.append("pilot stopping rules are invalid")
    return errors


def load_protocol(path: Path | str = DEFAULT_PROTOCOL_PATH) -> dict[str, object]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CorrectnessProtocolError(
            "correctness protocol could not be loaded"
        ) from exc
    if not isinstance(value, Mapping):
        raise CorrectnessProtocolError("correctness protocol must be an object")
    protocol = dict(value)
    errors = protocol_errors(protocol)
    if errors:
        raise CorrectnessProtocolError("; ".join(errors))
    return protocol
