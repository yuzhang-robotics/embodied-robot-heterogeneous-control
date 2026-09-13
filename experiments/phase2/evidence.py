"""Injected post-VLM evidence path for the bounded Phase 2 comparison."""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from experiments.phase2.energy import (
    integrate_vdd_in_window,
    validate_energy_window_record,
)
from experiments.phase2.observations import (
    validate_boundary_observation,
    validate_process_observation,
)
from experiments.phase2.privacy import validate_privacy_boundary
from experiments.phase2.residency.prefetch import (
    PrefetchActionRecord,
    validate_prefetch_action_record,
)


UNIT_EVIDENCE_SCHEMA_VERSION = "0.1.0"
CONTROL_CONDITION = "control_vlm_then_asr"
PREFETCH_CONDITION = "prefetch_vlm_then_asr"
CONDITIONS = frozenset({CONTROL_CONDITION, PREFETCH_CONDITION})


class UnitEvidenceError(ValueError):
    """An injected Phase 2 unit cannot form complete, ordered evidence."""


_UNIT_FIELDS = {
    "unit_evidence_schema_version",
    "condition",
    "formal_evidence",
    "application_slice_authorized",
    "common_post_vlm_observation",
    "verified_post_action_observation",
    "prefetch_action",
    "prefetch_process_observation",
    "asr_started_monotonic_ns",
    "asr_result_available_monotonic_ns",
    "asr_process_observation",
    "fully_charged_duration_ms",
    "asr_duration_ms",
    "energy_window",
    "privacy_scan_passed",
}


def _positive_integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise UnitEvidenceError(f"{name} must be a positive integer")
    return value


def _mapping(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise UnitEvidenceError(f"{name} must be a record")
    return dict(value)


def _record_integer(value: Mapping[str, object], name: str) -> int:
    return _positive_integer(value.get(name), name)


def _positive_finite(value: object, name: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) <= 0
    ):
        raise UnitEvidenceError(f"{name} must be positive and finite")
    return float(value)


def _prefetch_dict(
    value: PrefetchActionRecord | Mapping[str, object],
) -> dict[str, object]:
    return value.to_dict() if isinstance(value, PrefetchActionRecord) else dict(value)


def build_unit_evidence(
    *,
    condition: str,
    common_post_vlm_observation: Mapping[str, object],
    verified_post_action_observation: Mapping[str, object] | None,
    prefetch_action: PrefetchActionRecord | Mapping[str, object] | None,
    prefetch_process_observation: Mapping[str, object] | None,
    asr_started_monotonic_ns: int,
    asr_result_available_monotonic_ns: int,
    asr_process_observation: Mapping[str, object],
    resource_samples: Sequence[Mapping[str, Any]],
    maximum_resource_gap_ns: int,
) -> dict[str, object]:
    """Build one complete, privacy-safe control or prefetch evidence record."""

    if condition not in CONDITIONS:
        raise UnitEvidenceError("unsupported Phase 2 condition")
    common = dict(common_post_vlm_observation)
    validate_boundary_observation(common)
    if common.get("boundary") != "post_vlm":
        raise UnitEvidenceError("common observation is not the post-VLM boundary")
    charged_start = _record_integer(common, "observation_finished_monotonic_ns")

    verified: dict[str, object] | None = None
    action: dict[str, object] | None = None
    action_process: dict[str, object] | None = None
    if condition == PREFETCH_CONDITION:
        if (
            verified_post_action_observation is None
            or prefetch_action is None
            or prefetch_process_observation is None
        ):
            raise UnitEvidenceError("prefetch treatment evidence is incomplete")
        verified = dict(verified_post_action_observation)
        action = _prefetch_dict(prefetch_action)
        action_process = dict(prefetch_process_observation)
        validate_boundary_observation(verified)
        validate_prefetch_action_record(action)
        validate_process_observation(action_process)
        if verified.get("boundary") != "post_action":
            raise UnitEvidenceError("treatment verification boundary is invalid")
        if verified.get("expected_size_bytes") != common.get(
            "expected_size_bytes"
        ) or action.get("expected_size_bytes") != common.get("expected_size_bytes"):
            raise UnitEvidenceError("treatment model-size identity changed")
        if _record_integer(action, "action_started_monotonic_ns") < charged_start:
            raise UnitEvidenceError("prefetch began before the charged boundary")
        if _record_integer(action, "action_finished_monotonic_ns") > _record_integer(
            verified, "observation_started_monotonic_ns"
        ):
            raise UnitEvidenceError("post-action verification overlaps prefetch")
        ready_boundary = _record_integer(verified, "observation_finished_monotonic_ns")
    else:
        if any(
            item is not None
            for item in (
                verified_post_action_observation,
                prefetch_action,
                prefetch_process_observation,
            )
        ):
            raise UnitEvidenceError("control must not contain a residency action")
        ready_boundary = charged_start

    asr_started = _positive_integer(
        asr_started_monotonic_ns, "asr_started_monotonic_ns"
    )
    result_available = _positive_integer(
        asr_result_available_monotonic_ns,
        "asr_result_available_monotonic_ns",
    )
    if asr_started < ready_boundary or result_available <= asr_started:
        raise UnitEvidenceError("ASR boundaries are invalid or overlap prior work")
    asr_process = dict(asr_process_observation)
    validate_process_observation(asr_process)

    energy = integrate_vdd_in_window(
        resource_samples,
        window_started_monotonic_ns=charged_start,
        window_finished_monotonic_ns=result_available,
        maximum_gap_ns=maximum_resource_gap_ns,
    ).to_dict()
    evidence: dict[str, object] = {
        "unit_evidence_schema_version": UNIT_EVIDENCE_SCHEMA_VERSION,
        "condition": condition,
        "formal_evidence": False,
        "application_slice_authorized": False,
        "common_post_vlm_observation": common,
        "verified_post_action_observation": verified,
        "prefetch_action": action,
        "prefetch_process_observation": action_process,
        "asr_started_monotonic_ns": asr_started,
        "asr_result_available_monotonic_ns": result_available,
        "asr_process_observation": asr_process,
        "fully_charged_duration_ms": (result_available - charged_start) / 1e6,
        "asr_duration_ms": (result_available - asr_started) / 1e6,
        "energy_window": energy,
        "privacy_scan_passed": True,
    }
    validate_unit_evidence(evidence)
    validate_privacy_boundary(evidence)
    return evidence


def validate_unit_evidence(value: Mapping[str, object]) -> None:
    """Validate a derived unit independently of private source artifacts."""

    if set(value) != _UNIT_FIELDS:
        raise UnitEvidenceError("unit evidence fields are invalid")
    condition = value.get("condition")
    if (
        value.get("unit_evidence_schema_version") != UNIT_EVIDENCE_SCHEMA_VERSION
        or condition not in CONDITIONS
        or value.get("formal_evidence") is not False
        or value.get("application_slice_authorized") is not False
        or value.get("privacy_scan_passed") is not True
    ):
        raise UnitEvidenceError("unit evidence identity or authority is invalid")

    common = _mapping(value.get("common_post_vlm_observation"), "common observation")
    validate_boundary_observation(common)
    if common.get("boundary") != "post_vlm":
        raise UnitEvidenceError("common observation boundary is invalid")
    charged_start = _record_integer(common, "observation_finished_monotonic_ns")

    verified_value = value.get("verified_post_action_observation")
    action_value = value.get("prefetch_action")
    action_process_value = value.get("prefetch_process_observation")
    if condition == PREFETCH_CONDITION:
        verified = _mapping(verified_value, "post-action observation")
        action = _mapping(action_value, "prefetch action")
        action_process = _mapping(action_process_value, "prefetch process observation")
        validate_boundary_observation(verified)
        validate_prefetch_action_record(action)
        validate_process_observation(action_process)
        if (
            verified.get("boundary") != "post_action"
            or verified.get("expected_size_bytes") != common.get("expected_size_bytes")
            or action.get("expected_size_bytes") != common.get("expected_size_bytes")
            or _record_integer(action, "action_started_monotonic_ns") < charged_start
            or _record_integer(action, "action_finished_monotonic_ns")
            > _record_integer(verified, "observation_started_monotonic_ns")
        ):
            raise UnitEvidenceError("prefetch evidence order is invalid")
        ready_boundary = _record_integer(verified, "observation_finished_monotonic_ns")
    else:
        if any(
            item is not None
            for item in (verified_value, action_value, action_process_value)
        ):
            raise UnitEvidenceError("control contains treatment-only evidence")
        ready_boundary = charged_start

    asr_started = _positive_integer(
        value.get("asr_started_monotonic_ns"), "asr_started_monotonic_ns"
    )
    result_available = _positive_integer(
        value.get("asr_result_available_monotonic_ns"),
        "asr_result_available_monotonic_ns",
    )
    if asr_started < ready_boundary or result_available <= asr_started:
        raise UnitEvidenceError("ASR evidence order is invalid")
    asr_process = _mapping(value.get("asr_process_observation"), "ASR process")
    validate_process_observation(asr_process)

    energy = _mapping(value.get("energy_window"), "energy window")
    validate_energy_window_record(energy)
    if (
        _record_integer(energy, "window_started_monotonic_ns") != charged_start
        or _record_integer(energy, "window_finished_monotonic_ns") != result_available
    ):
        raise UnitEvidenceError("energy and charged-time boundaries differ")

    charged_duration = _positive_finite(
        value.get("fully_charged_duration_ms"), "fully_charged_duration_ms"
    )
    asr_duration = _positive_finite(value.get("asr_duration_ms"), "asr_duration_ms")
    if (
        abs(charged_duration - (result_available - charged_start) / 1e6) > 1e-9
        or abs(asr_duration - (result_available - asr_started) / 1e6) > 1e-9
    ):
        raise UnitEvidenceError("unit duration does not match its boundaries")
    validate_privacy_boundary(value)


BoundaryReader = Callable[[str], Mapping[str, object]]
TreatmentRunner = Callable[
    [], tuple[PrefetchActionRecord | Mapping[str, object], Mapping[str, object]]
]
AsrRunner = Callable[[], tuple[int, int, Mapping[str, object]]]
ResourceReader = Callable[[], Sequence[Mapping[str, Any]]]


def run_injected_evidence_path(
    *,
    condition: str,
    boundary_reader: BoundaryReader,
    treatment_runner: TreatmentRunner,
    asr_runner: AsrRunner,
    resource_reader: ResourceReader,
    maximum_resource_gap_ns: int,
) -> dict[str, object]:
    """Exercise the post-VLM evidence sequence using injected safe callables."""

    if condition not in CONDITIONS:
        raise UnitEvidenceError("unsupported Phase 2 condition")
    for value, name in (
        (boundary_reader, "boundary_reader"),
        (treatment_runner, "treatment_runner"),
        (asr_runner, "asr_runner"),
        (resource_reader, "resource_reader"),
    ):
        if not callable(value):
            raise TypeError(f"{name} must be callable")

    common = dict(boundary_reader("post_vlm"))
    verified: Mapping[str, object] | None = None
    action: PrefetchActionRecord | Mapping[str, object] | None = None
    action_process: Mapping[str, object] | None = None
    if condition == PREFETCH_CONDITION:
        action, action_process = treatment_runner()
        verified = dict(boundary_reader("post_action"))
    asr_started, result_available, asr_process = asr_runner()
    samples = resource_reader()
    return build_unit_evidence(
        condition=condition,
        common_post_vlm_observation=common,
        verified_post_action_observation=verified,
        prefetch_action=action,
        prefetch_process_observation=action_process,
        asr_started_monotonic_ns=asr_started,
        asr_result_available_monotonic_ns=result_available,
        asr_process_observation=asr_process,
        resource_samples=samples,
        maximum_resource_gap_ns=maximum_resource_gap_ns,
    )
