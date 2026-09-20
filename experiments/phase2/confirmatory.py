"""Frozen contract helpers for the Phase 2 confirmatory comparison."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path

from experiments.phase2.evidence import CONTROL_CONDITION, PREFETCH_CONDITION


CONFIRMATORY_PROTOCOL_SCHEMA_VERSION = "0.1.0"
CONFIRMATORY_PROTOCOL_ID = "phase2_bounded_whisper_residency_confirmatory_v1"
CONFIRMATORY_PROTOCOL_SHA256 = (
    "528334f0e8bc9bf54b4b6450cc5cd2fa62a567cf7da5b45708da00ba91c06a42"
)
DEFAULT_CONFIRMATORY_PROTOCOL_PATH = Path(__file__).with_name("confirmatory-v1.json")
DEFAULT_COMMISSIONING_RESULT_PATH = Path(__file__).with_name(
    "commissioning-result-v1.json"
)
COMMISSIONING_RESULT_SHA256 = (
    "2fa7ae0a1012eafeea68016cfcd85bc9ec7d676d5906dda9e1bbb1f2e333f125"
)
CONFIRMATORY_SESSION_COUNT = 6
CONFIRMATORY_PAIRS_PER_SESSION = 4
CONFIRMATORY_ATTEMPT = 1
MINIMUM_SESSION_SEPARATION_S = 1800
BOOTSTRAP_RESAMPLES = 100_000
BOOTSTRAP_SEED = 20_260_920
CONFIDENCE_LEVEL = 0.95


def _expected_schedules() -> list[dict[str, object]]:
    schedules: list[dict[str, object]] = []
    for session_index in range(1, CONFIRMATORY_SESSION_COUNT + 1):
        order = (
            [PREFETCH_CONDITION, CONTROL_CONDITION]
            if session_index % 2
            else [CONTROL_CONDITION, PREFETCH_CONDITION]
        )
        schedules.append(
            {
                "session_index": session_index,
                "pair_conditions": [list(order) for _ in range(4)],
            }
        )
    return schedules


_EXPECTED_SCHEDULES = _expected_schedules()
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
_EXPECTED_COMMISSIONING_RESULT = {
    "path": "experiments/phase2/commissioning-result-v1.json",
    "sha256": COMMISSIONING_RESULT_SHA256,
    "collection_id": "20260920T123241Z_phase2_commissioning_v1",
    "commissioning_valid": True,
    "formal_protocol_parameters_may_be_frozen": True,
}
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
_EXPECTED_ANALYSIS = {
    "bootstrap_method": "paired_hierarchical_percentile",
    "bootstrap_resamples": BOOTSTRAP_RESAMPLES,
    "bootstrap_seed": BOOTSTRAP_SEED,
    "confidence_level": CONFIDENCE_LEVEL,
    "session_resampling_first": True,
    "pair_resampling_within_selected_session": True,
    "primary_estimand": (
        "fully_charged_duration_geometric_mean_ratio_treatment_over_control"
    ),
    "primary_support_rule": "two_sided_ci_upper_below_1",
    "residency_estimand": (
        "post_action_treatment_minus_post_vlm_control_resident_fraction"
    ),
    "residency_support_rule": "two_sided_ci_lower_above_0",
    "major_fault_estimand": "measured_asr_treatment_minus_control_major_faults",
    "major_fault_support_rule": "two_sided_ci_upper_below_0",
    "responsiveness_estimand": "p95_nearest_rank_unit_probe_max_gap_ms",
    "responsiveness_support_rule": "point_estimate_at_or_below_300_ms",
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
    "outlier_exclusion_allowed": False,
    "missing_value_imputation_allowed": False,
    "outcome_driven_extension_allowed": False,
    "threshold_changes_allowed": False,
    "alternative_mitigation_allowed": False,
    "first_complete_valid_collection_closes_v1": True,
}
_EXPECTED_FAILURE_POLICY = {
    "valid_treatment_timeout": "retain_as_system_under_test_outcome",
    "valid_system_under_test_failure": "retain_as_observed_outcome",
    "infrastructure_failure": "close_collection_without_replacement",
    "operator_interruption": "close_collection_without_replacement",
    "service_failure": "close_collection_without_replacement",
    "incomplete_collection": "invalid_no_smaller_design",
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
    "commissioning_result",
    "session_schedules",
    "unit_sequence",
    "parameters",
    "analysis",
    "safety",
    "stopping",
    "failure_policy",
}


class ConfirmatoryProtocolError(ValueError):
    """The confirmatory contract is unavailable or has changed."""


def canonical_confirmatory_protocol_text(protocol: Mapping[str, object]) -> str:
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


def confirmatory_protocol_sha256(protocol: Mapping[str, object]) -> str:
    return hashlib.sha256(
        canonical_confirmatory_protocol_text(protocol).encode("utf-8")
    ).hexdigest()


def confirmatory_protocol_errors(protocol: Mapping[str, object]) -> list[str]:
    errors: list[str] = []
    if set(protocol) != _PROTOCOL_FIELDS:
        errors.append("confirmatory protocol fields are invalid")
    expected_scalars = {
        "protocol_schema_version": CONFIRMATORY_PROTOCOL_SCHEMA_VERSION,
        "protocol_id": CONFIRMATORY_PROTOCOL_ID,
        "status": "active_after_reviewed_merge_and_explicit_collection_authorization",
        "evidence_namespace": "phase2_confirmatory_v1",
        "formal_evidence": True,
        "confirmatory_data": True,
        "application_slice_authorized": False,
        "required_branch": "main",
        "session_count": CONFIRMATORY_SESSION_COUNT,
        "pairs_per_session": CONFIRMATORY_PAIRS_PER_SESSION,
        "attempt": CONFIRMATORY_ATTEMPT,
        "minimum_session_separation_s": MINIMUM_SESSION_SEPARATION_S,
        "restart_model_services_each_session": True,
    }
    for name, expected in expected_scalars.items():
        if protocol.get(name) != expected:
            errors.append(f"confirmatory protocol {name} is invalid")
    expected_records = {
        "commissioning_result": _EXPECTED_COMMISSIONING_RESULT,
        "session_schedules": _EXPECTED_SCHEDULES,
        "unit_sequence": _EXPECTED_UNIT_SEQUENCE,
        "parameters": _EXPECTED_PARAMETERS,
        "analysis": _EXPECTED_ANALYSIS,
        "safety": _EXPECTED_SAFETY,
        "stopping": _EXPECTED_STOPPING,
        "failure_policy": _EXPECTED_FAILURE_POLICY,
    }
    for name, expected in expected_records.items():
        if protocol.get(name) != expected:
            errors.append(f"confirmatory protocol {name} is invalid")
    return errors


def load_confirmatory_protocol(
    path: Path | str = DEFAULT_CONFIRMATORY_PROTOCOL_PATH,
) -> dict[str, object]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConfirmatoryProtocolError(
            "confirmatory protocol could not be loaded"
        ) from exc
    if not isinstance(value, Mapping):
        raise ConfirmatoryProtocolError("confirmatory protocol must be an object")
    protocol = dict(value)
    errors = confirmatory_protocol_errors(protocol)
    if errors:
        raise ConfirmatoryProtocolError("; ".join(errors))
    return protocol


def confirmatory_pair_conditions(
    protocol: Mapping[str, object], session_index: int
) -> tuple[tuple[str, str], ...]:
    errors = confirmatory_protocol_errors(protocol)
    if errors:
        raise ConfirmatoryProtocolError("; ".join(errors))
    if (
        isinstance(session_index, bool)
        or not isinstance(session_index, int)
        or not 1 <= session_index <= CONFIRMATORY_SESSION_COUNT
    ):
        raise ConfirmatoryProtocolError("confirmatory session index is invalid")
    schedules = protocol["session_schedules"]
    assert isinstance(schedules, list)
    schedule = schedules[session_index - 1]
    assert isinstance(schedule, Mapping)
    pairs = schedule["pair_conditions"]
    assert isinstance(pairs, list)
    return tuple((str(pair[0]), str(pair[1])) for pair in pairs)


def confirmatory_conditions(
    protocol: Mapping[str, object], session_index: int
) -> tuple[str, ...]:
    return tuple(
        condition
        for pair in confirmatory_pair_conditions(protocol, session_index)
        for condition in pair
    )
