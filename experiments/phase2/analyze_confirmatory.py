"""Reconstruct and analyze one complete Phase 2 confirmatory collection."""

from __future__ import annotations

import argparse
import json
import math
import re
import statistics
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import NoReturn

import numpy as np

from experiments.phase1.common.manifest import write_json_atomic
from experiments.phase1.common.telemetry_jetson import (
    load_resource_samples,
    summarize_resource_samples,
    validate_resource_samples,
)
from experiments.phase2.confirmatory import (
    BOOTSTRAP_RESAMPLES,
    BOOTSTRAP_SEED,
    CONFIRMATORY_ATTEMPT,
    CONFIRMATORY_PAIRS_PER_SESSION,
    CONFIRMATORY_PROTOCOL_ID,
    CONFIRMATORY_PROTOCOL_SHA256,
    CONFIRMATORY_SESSION_COUNT,
    canonical_confirmatory_protocol_text,
    confirmatory_conditions,
    confirmatory_protocol_sha256,
    load_confirmatory_protocol,
)
from experiments.phase2.confirmatory_preflight import confirmatory_preflight_errors
from experiments.phase2.evidence import CONTROL_CONDITION, PREFETCH_CONDITION
from experiments.phase2.privacy import validate_privacy_boundary
from experiments.phase2.reconstruct_commissioning import (
    CommissioningReconstructionError,
    _list,
    _load_json,
    _load_jsonl,
    _mapping,
    _parse_utc,
    _positive_int,
    _positive_number,
    _unit_label,
    _validate_artifact_inventory,
    _validate_unit,
)
from experiments.phase2.run_confirmatory_session import (
    CONFIRMATORY_SESSION_SCHEMA_VERSION,
)


CONFIRMATORY_ANALYSIS_SCHEMA_VERSION = "0.1.0"
_COLLECTION_RE = re.compile(r"^[0-9]{8}T[0-9]{6}Z_phase2_confirmatory_v1$")


class ConfirmatoryAnalysisError(ValueError):
    """Artifacts cannot support the frozen confirmatory analysis."""


def _fail(message: str) -> NoReturn:
    raise ConfirmatoryAnalysisError(message)


def _nonnegative_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        _fail(f"{name} is not a nonnegative integer")
    return value


def _finite_number(value: object, name: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        _fail(f"{name} is not finite")
    return float(value)


def _process_summary(value: Mapping[str, object], name: str) -> dict[str, object]:
    return {
        "user_time_s": _finite_number(value.get("user_time_s"), f"{name} user time"),
        "system_time_s": _finite_number(
            value.get("system_time_s"), f"{name} system time"
        ),
        "minor_faults": _nonnegative_int(
            value.get("minor_faults"), f"{name} minor faults"
        ),
        "major_faults": _nonnegative_int(
            value.get("major_faults"), f"{name} major faults"
        ),
        "maximum_rss_bytes": _nonnegative_int(
            value.get("maximum_rss_bytes"), f"{name} maximum RSS"
        ),
        "voluntary_context_switches": _nonnegative_int(
            value.get("voluntary_context_switches"),
            f"{name} voluntary context switches",
        ),
        "involuntary_context_switches": _nonnegative_int(
            value.get("involuntary_context_switches"),
            f"{name} involuntary context switches",
        ),
    }


def _boundary_summary(value: Mapping[str, object], name: str) -> dict[str, object]:
    return {
        "resident_pages": _nonnegative_int(
            value.get("resident_pages"), f"{name} resident pages"
        ),
        "resident_fraction": _finite_number(
            value.get("resident_fraction"), f"{name} resident fraction"
        ),
        "mem_available_bytes": _nonnegative_int(
            value.get("mem_available_bytes"), f"{name} MemAvailable"
        ),
        "cached_bytes": _nonnegative_int(value.get("cached_bytes"), f"{name} Cached"),
        "sreclaimable_bytes": _nonnegative_int(
            value.get("sreclaimable_bytes"), f"{name} SReclaimable"
        ),
    }


def _probe_summary(value: Mapping[str, object], name: str) -> dict[str, object]:
    return {
        "max_gap_ms": _positive_number(value.get("max_gap_ns"), f"{name} maximum gap")
        / 1e6,
        "max_lateness_ms": _finite_number(
            value.get("max_lateness_ns"), f"{name} maximum lateness"
        )
        / 1e6,
        "deadline_miss_count": _nonnegative_int(
            value.get("deadline_miss_count"), f"{name} deadline misses"
        ),
        "skipped_releases": _nonnegative_int(
            value.get("skipped_releases"), f"{name} skipped releases"
        ),
    }


def _resource_summary(
    samples: Sequence[dict[str, object]],
) -> dict[str, object]:
    summary = summarize_resource_samples(samples)
    ram = [_mapping(sample.get("ram"), "RAM sample") for sample in samples]
    swap = [_mapping(sample.get("swap"), "swap sample") for sample in samples]
    emc = [_mapping(sample.get("emc"), "EMC sample") for sample in samples]
    gr3d = [_mapping(sample.get("gr3d"), "GR3D sample") for sample in samples]
    summary.update(
        {
            "ram_total_mb": _descriptive(
                [_finite_number(item.get("total_mb"), "RAM total") for item in ram]
            ),
            "largest_free_block_count": _descriptive(
                [_finite_number(item.get("lfb_count"), "LFB count") for item in ram]
            ),
            "largest_free_block_size_mb": _descriptive(
                [_finite_number(item.get("lfb_size_mb"), "LFB size") for item in ram]
            ),
            "swap_total_mb": _descriptive(
                [_finite_number(item.get("total_mb"), "swap total") for item in swap]
            ),
            "swap_cached_mb": _descriptive(
                [_finite_number(item.get("cached_mb"), "swap cached") for item in swap]
            ),
            "emc_frequency_mhz": _descriptive(
                [
                    _finite_number(item.get("frequency_mhz"), "EMC frequency")
                    for item in emc
                ]
            ),
            "gr3d_frequency_mhz": _descriptive(
                [
                    _finite_number(frequency, "GR3D frequency")
                    for item in gr3d
                    for frequency in _list(
                        item.get("frequencies_mhz"), "GR3D frequencies"
                    )
                ]
            ),
        }
    )
    return summary


def nearest_rank(values: Sequence[float], percentile: float) -> float:
    ordered = sorted(float(value) for value in values)
    if not ordered or not 0 < percentile <= 100:
        raise ValueError("nearest-rank inputs are invalid")
    return ordered[math.ceil(percentile / 100 * len(ordered)) - 1]


def _descriptive(values: Sequence[float]) -> dict[str, float | int]:
    items = [float(value) for value in values]
    if not items or not all(math.isfinite(value) for value in items):
        raise ValueError("descriptive values must be finite")
    return {
        "count": len(items),
        "mean": statistics.fmean(items),
        "median": statistics.median(items),
        "sample_stddev": statistics.stdev(items) if len(items) > 1 else 0.0,
        "min": min(items),
        "max": max(items),
    }


def _interval(values: np.ndarray) -> dict[str, float]:
    low, high = np.quantile(values, (0.025, 0.975))
    return {"low": float(low), "high": float(high)}


def paired_hierarchical_bootstrap(
    charged_log_ratios: Sequence[Sequence[float]],
    residency_differences: Sequence[Sequence[float]],
    major_fault_differences: Sequence[Sequence[float]],
    *,
    resamples: int = BOOTSTRAP_RESAMPLES,
    seed: int = BOOTSTRAP_SEED,
) -> dict[str, dict[str, object]]:
    """Resample sessions first, then pairs within each selected session."""

    if isinstance(resamples, bool) or not isinstance(resamples, int) or resamples <= 0:
        raise ValueError("resamples must be a positive integer")
    arrays = {
        "charged": np.asarray(charged_log_ratios, dtype=float),
        "residency": np.asarray(residency_differences, dtype=float),
        "major_faults": np.asarray(major_fault_differences, dtype=float),
    }
    expected_shape = (CONFIRMATORY_SESSION_COUNT, CONFIRMATORY_PAIRS_PER_SESSION)
    if any(array.shape != expected_shape for array in arrays.values()):
        raise ValueError("bootstrap inputs do not match the frozen 6-by-4 design")
    if any(not np.all(np.isfinite(array)) for array in arrays.values()):
        raise ValueError("bootstrap inputs must be finite")

    rng = np.random.default_rng(seed)
    samples: dict[str, list[np.ndarray]] = {name: [] for name in arrays}
    remaining = resamples
    chunk_size = min(10_000, resamples)
    while remaining:
        current = min(chunk_size, remaining)
        selected_sessions = rng.integers(
            0,
            CONFIRMATORY_SESSION_COUNT,
            size=(current, CONFIRMATORY_SESSION_COUNT),
        )
        selected_pairs = rng.integers(
            0,
            CONFIRMATORY_PAIRS_PER_SESSION,
            size=(
                current,
                CONFIRMATORY_SESSION_COUNT,
                CONFIRMATORY_PAIRS_PER_SESSION,
            ),
        )
        for name, array in arrays.items():
            selected: list[np.ndarray] = []
            for slot in range(CONFIRMATORY_SESSION_COUNT):
                session_indices = selected_sessions[:, slot, None]
                pair_indices = selected_pairs[:, slot, :]
                selected.append(array[session_indices, pair_indices])
            sample_means = np.concatenate(selected, axis=1).mean(axis=1)
            samples[name].append(
                np.exp(sample_means) if name == "charged" else sample_means
            )
        remaining -= current

    charged = arrays["charged"]
    residency = arrays["residency"]
    faults = arrays["major_faults"]
    return {
        "operational_benefit": {
            "estimate": float(np.exp(charged.mean())),
            "ci95": _interval(np.concatenate(samples["charged"])),
        },
        "residency_difference": {
            "estimate": float(residency.mean()),
            "ci95": _interval(np.concatenate(samples["residency"])),
        },
        "major_fault_difference": {
            "estimate": float(faults.mean()),
            "ci95": _interval(np.concatenate(samples["major_faults"])),
        },
    }


def _directional_decision(
    interval: Mapping[str, object], *, favorable: str, null: float
) -> str:
    low = _finite_number(interval.get("low"), "interval lower bound")
    high = _finite_number(interval.get("high"), "interval upper bound")
    if low > high:
        _fail("confidence interval is reversed")
    if favorable == "below":
        if high < null:
            return "positive"
        if low > null:
            return "negative"
    elif favorable == "above":
        if low > null:
            return "positive"
        if high < null:
            return "negative"
    else:
        raise ValueError("unsupported directional decision")
    return "inconclusive"


def classify_estimands(
    bootstrap: Mapping[str, Mapping[str, object]],
    *,
    responsiveness_p95_ms: float,
    responsiveness_reference_ms: float = 300.0,
) -> dict[str, object]:
    operational = _mapping(bootstrap.get("operational_benefit"), "operational result")
    residency = _mapping(bootstrap.get("residency_difference"), "residency result")
    faults = _mapping(bootstrap.get("major_fault_difference"), "major-fault result")
    operational_decision = _directional_decision(
        _mapping(operational.get("ci95"), "operational interval"),
        favorable="below",
        null=1.0,
    )
    residency_decision = _directional_decision(
        _mapping(residency.get("ci95"), "residency interval"),
        favorable="above",
        null=0.0,
    )
    fault_decision = _directional_decision(
        _mapping(faults.get("ci95"), "major-fault interval"),
        favorable="below",
        null=0.0,
    )
    mechanism_supported = (
        residency_decision == "positive" and fault_decision == "positive"
    )
    if mechanism_supported:
        mechanism_decision = "positive"
    elif residency_decision == "negative" or fault_decision == "negative":
        mechanism_decision = "negative"
    else:
        mechanism_decision = "inconclusive"
    responsiveness_preserved = (
        math.isfinite(responsiveness_p95_ms)
        and responsiveness_p95_ms <= responsiveness_reference_ms
    )
    return {
        "operational_benefit_decision": operational_decision,
        "operational_benefit_supported": operational_decision == "positive",
        "residency_component_decision": residency_decision,
        "major_fault_component_decision": fault_decision,
        "residency_mechanism_decision": mechanism_decision,
        "residency_mechanism_supported": mechanism_supported,
        "responsiveness_decision": (
            "preserved" if responsiveness_preserved else "not_preserved"
        ),
        "responsiveness_preserved": responsiveness_preserved,
    }


def _pair_summary(
    session_index: int,
    pair_index: int,
    conditions: Sequence[str],
    records: Sequence[tuple[dict[str, object], dict[str, object]]],
) -> dict[str, object]:
    by_condition = {
        condition: (unit, evidence)
        for condition, (unit, evidence) in zip(conditions, records)
    }
    if set(by_condition) != {CONTROL_CONDITION, PREFETCH_CONDITION}:
        _fail("confirmatory pair does not contain one unit per condition")
    control_unit, control = by_condition[CONTROL_CONDITION]
    treatment_unit, treatment = by_condition[PREFETCH_CONDITION]
    control_charged = _positive_number(
        control.get("fully_charged_duration_ms"), "control charged duration"
    )
    treatment_charged = _positive_number(
        treatment.get("fully_charged_duration_ms"), "treatment charged duration"
    )
    control_energy = _positive_number(
        _mapping(control.get("energy_window"), "control energy").get("energy_mj"),
        "control energy",
    )
    treatment_energy = _positive_number(
        _mapping(treatment.get("energy_window"), "treatment energy").get("energy_mj"),
        "treatment energy",
    )
    action = _mapping(treatment.get("prefetch_action"), "prefetch action")
    control_common = _mapping(
        control.get("common_post_vlm_observation"), "control post-VLM observation"
    )
    treatment_common = _mapping(
        treatment.get("common_post_vlm_observation"),
        "treatment post-VLM observation",
    )
    treatment_verified = _mapping(
        treatment.get("verified_post_action_observation"),
        "treatment post-action observation",
    )
    control_process = _mapping(
        control.get("asr_process_observation"), "control measured-ASR process"
    )
    treatment_process = _mapping(
        treatment.get("asr_process_observation"), "treatment measured-ASR process"
    )
    control_faults = _nonnegative_int(
        control_process.get("major_faults"), "control measured-ASR major faults"
    )
    treatment_faults = _nonnegative_int(
        treatment_process.get("major_faults"), "treatment measured-ASR major faults"
    )
    control_fraction = _finite_number(
        control_common.get("resident_fraction"), "control post-VLM residency"
    )
    treatment_post_vlm = _finite_number(
        treatment_common.get("resident_fraction"), "treatment post-VLM residency"
    )
    treatment_post_action = _finite_number(
        treatment_verified.get("resident_fraction"), "treatment post-action residency"
    )
    control_probe = _mapping(control_unit.get("probe"), "control probe")
    treatment_probe = _mapping(treatment_unit.get("probe"), "treatment probe")
    control_probe_summary = _probe_summary(control_probe, "control probe")
    treatment_probe_summary = _probe_summary(treatment_probe, "treatment probe")
    control_gap = float(control_probe_summary["max_gap_ms"])
    treatment_gap = float(treatment_probe_summary["max_gap_ms"])
    control_primer = _positive_number(
        control_unit.get("primer_2_duration_ms"), "control primer-2 duration"
    )
    treatment_primer = _positive_number(
        treatment_unit.get("primer_2_duration_ms"), "treatment primer-2 duration"
    )
    control_recovery = _positive_number(
        control_unit.get("recovery_asr_duration_ms"), "control recovery-ASR duration"
    )
    treatment_recovery = _positive_number(
        treatment_unit.get("recovery_asr_duration_ms"),
        "treatment recovery-ASR duration",
    )
    control_asr = _positive_number(
        control.get("asr_duration_ms"), "control ASR duration"
    )
    treatment_asr = _positive_number(
        treatment.get("asr_duration_ms"), "treatment ASR duration"
    )
    action_started = _positive_number(
        action.get("action_started_monotonic_ns"), "prefetch action start"
    )
    action_finished = _positive_number(
        action.get("action_finished_monotonic_ns"), "prefetch action finish"
    )
    action_duration_ms = (action_finished - action_started) / 1e6
    if action_duration_ms <= 0:
        _fail("prefetch action duration is not positive")
    bytes_read = action.get("bytes_read")
    if bytes_read is not None:
        bytes_read = _nonnegative_int(bytes_read, "prefetch bytes read")
    action_process = _mapping(
        treatment.get("prefetch_process_observation"),
        "prefetch process observation",
    )
    return {
        "session_index": session_index,
        "pair_index_within_session": pair_index,
        "condition_order": list(conditions),
        "control": {
            "fully_charged_duration_ms": control_charged,
            "asr_duration_ms": control_asr,
            "primer_2_duration_ms": control_primer,
            "asr_to_primer_2_ratio": control_asr / control_primer,
            "recovery_asr_duration_ms": control_recovery,
            "energy_mj": control_energy,
            "post_vlm_observation": _boundary_summary(
                control_common, "control post-VLM"
            ),
            "post_vlm_resident_fraction": control_fraction,
            "measured_asr_major_faults": control_faults,
            "measured_asr_process": _process_summary(
                control_process, "control measured ASR"
            ),
            "probe": control_probe_summary,
            "probe_max_gap_ms": control_gap,
        },
        "treatment": {
            "fully_charged_duration_ms": treatment_charged,
            "asr_duration_ms": treatment_asr,
            "primer_2_duration_ms": treatment_primer,
            "asr_to_primer_2_ratio": treatment_asr / treatment_primer,
            "recovery_asr_duration_ms": treatment_recovery,
            "energy_mj": treatment_energy,
            "post_vlm_observation": _boundary_summary(
                treatment_common, "treatment post-VLM"
            ),
            "post_vlm_resident_fraction": treatment_post_vlm,
            "post_action_observation": _boundary_summary(
                treatment_verified, "treatment post-action"
            ),
            "post_action_resident_fraction": treatment_post_action,
            "measured_asr_major_faults": treatment_faults,
            "measured_asr_process": _process_summary(
                treatment_process, "treatment measured ASR"
            ),
            "prefetch_status": action.get("status"),
            "prefetch_bytes_read": bytes_read,
            "prefetch_duration_ms": action_duration_ms,
            "prefetch_throughput_bytes_per_s": (
                bytes_read / (action_duration_ms / 1000.0)
                if bytes_read is not None
                else None
            ),
            "prefetch_process": _process_summary(action_process, "prefetch process"),
            "probe": treatment_probe_summary,
            "probe_max_gap_ms": treatment_gap,
        },
        "charged_log_ratio_treatment_over_control": math.log(
            treatment_charged / control_charged
        ),
        "charged_duration_ratio_treatment_over_control": (
            treatment_charged / control_charged
        ),
        "residency_difference_treatment_post_action_minus_control": (
            treatment_post_action - control_fraction
        ),
        "major_fault_difference_treatment_minus_control": (
            treatment_faults - control_faults
        ),
        "energy_ratio_treatment_over_control": treatment_energy / control_energy,
        "formal_interpretation_permitted": True,
    }


def _reconstruct_session(
    session_dir: Path,
    *,
    collection_id: str,
    session_index: int,
    protocol: Mapping[str, object],
) -> tuple[dict[str, object], list[dict[str, object]]]:
    manifest = _load_json(session_dir / "manifest.json", "session manifest")
    validate_privacy_boundary(manifest)
    expected_conditions = list(confirmatory_conditions(protocol, session_index))
    if (
        manifest.get("confirmatory_session_schema_version")
        != CONFIRMATORY_SESSION_SCHEMA_VERSION
        or manifest.get("artifact_kind") != "phase2_confirmatory_session"
        or manifest.get("collection_id") != collection_id
        or manifest.get("session_id") != session_dir.name
        or manifest.get("session_index") != session_index
        or manifest.get("attempt") != CONFIRMATORY_ATTEMPT
        or manifest.get("protocol_id") != CONFIRMATORY_PROTOCOL_ID
        or manifest.get("protocol_sha256") != CONFIRMATORY_PROTOCOL_SHA256
        or manifest.get("conditions") != expected_conditions
        or manifest.get("status") != "completed"
        or manifest.get("failure_class") is not None
        or manifest.get("failure_code") is not None
        or manifest.get("formal_evidence") is not True
        or manifest.get("confirmatory_data") is not True
        or manifest.get("application_slice_authorized") is not False
        or manifest.get("development_injection") is not False
        or manifest.get("completed_units") != 8
    ):
        _fail(f"confirmatory session manifest is invalid: {session_index}")
    created_at = _parse_utc(manifest.get("created_at"), "session created_at")
    completed_at = _parse_utc(manifest.get("completed_at"), "session completed_at")
    if completed_at < created_at:
        _fail(f"confirmatory session completion moved backwards: {session_index}")

    source_protocol = _load_json(
        session_dir / "protocol.json", f"session protocol {session_index}"
    )
    if (
        source_protocol != protocol
        or confirmatory_protocol_sha256(source_protocol) != CONFIRMATORY_PROTOCOL_SHA256
        or (session_dir / "protocol.json").read_text(encoding="utf-8")
        != canonical_confirmatory_protocol_text(protocol)
    ):
        _fail(f"confirmatory protocol copy differs: {session_index}")
    preflight = _load_json(
        session_dir / "preflight.json", f"session preflight {session_index}"
    )
    validate_privacy_boundary(preflight)
    preflight_errors = confirmatory_preflight_errors(preflight)
    if preflight_errors:
        _fail(
            f"confirmatory preflight is invalid: {session_index}: "
            + "; ".join(preflight_errors)
        )
    manifest_preflight = _mapping(
        manifest.get("preflight"), f"manifest preflight {session_index}"
    )
    preflight_protocol = _mapping(
        preflight.get("protocol"), f"preflight protocol {session_index}"
    )
    preflight_commissioning = _mapping(
        preflight.get("commissioning_result"),
        f"preflight commissioning result {session_index}",
    )
    preflight_separation = _mapping(
        preflight.get("separation"), f"preflight separation {session_index}"
    )
    preflight_prior = _mapping(
        preflight.get("prior_session"), f"preflight prior session {session_index}"
    )
    if (
        manifest_preflight.get("captured_at") != preflight.get("captured_at")
        or manifest_preflight.get("protocol_commit")
        != preflight_protocol.get("protocol_commit")
        or manifest_preflight.get("runner_commit")
        != preflight_protocol.get("runner_commit")
        or manifest_preflight.get("commissioning_result_sha256")
        != preflight_commissioning.get("sha256")
        or manifest_preflight.get("commissioning_result_commit")
        != preflight_commissioning.get("result_commit")
        or manifest_preflight.get("service_identity")
        != preflight.get("service_identity")
        or manifest_preflight.get("prior_session_index")
        != preflight_prior.get("session_index")
        or manifest_preflight.get("separation_s")
        != preflight_separation.get("observed_s")
    ):
        _fail(f"manifest and preflight bindings differ: {session_index}")

    parameters = _mapping(protocol.get("parameters"), "confirmatory parameters")
    thermal_start_maximum = _positive_number(
        parameters.get("thermal_start_maximum_tj_c"), "thermal start maximum"
    )
    thermal_start_samples = _positive_int(
        parameters.get("thermal_start_consecutive_samples"),
        "thermal start sample count",
    )
    thermal_stop = _positive_number(
        parameters.get("thermal_stop_tj_c"), "thermal stop threshold"
    )
    thermal = _mapping(manifest.get("thermal"), f"thermal record {session_index}")
    readiness = _mapping(thermal.get("readiness"), f"thermal readiness {session_index}")
    if (
        thermal.get("stop_requested") is not False
        or thermal.get("stop_tj_c") != thermal_stop
        or readiness.get("maximum_tj_c") != thermal_start_maximum
        or readiness.get("consecutive_samples") != thermal_start_samples
    ):
        _fail(f"thermal contract is invalid: {session_index}")
    observed_tj = readiness.get("observed_tj_c")
    first_sequence = readiness.get("first_sequence")
    last_sequence = readiness.get("last_sequence")
    if (
        not isinstance(observed_tj, list)
        or len(observed_tj) != thermal_start_samples
        or any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or value > thermal_start_maximum
            for value in observed_tj
        )
        or isinstance(first_sequence, bool)
        or not isinstance(first_sequence, int)
        or isinstance(last_sequence, bool)
        or not isinstance(last_sequence, int)
        or last_sequence - first_sequence != len(observed_tj) - 1
    ):
        _fail(f"thermal readiness samples are invalid: {session_index}")
    sampler = _mapping(
        manifest.get("resource_sampler_report"),
        f"resource sampler report {session_index}",
    )
    if (
        sampler.get("successful") is not True
        or sampler.get("parse_error_count") != 0
        or sampler.get("reader_joined") is not True
    ):
        _fail(f"resource sampler did not close: {session_index}")
    resources = load_resource_samples(session_dir / "resources.jsonl")
    resource_errors = validate_resource_samples(resources)
    if resource_errors:
        _fail(
            f"resource samples are invalid: {session_index}: "
            + "; ".join(resource_errors[:4])
        )
    if sampler.get("sample_count") != len(resources):
        _fail(f"resource sample count differs: {session_index}")
    for sample in resources:
        temperatures = sample.get("temperatures_c")
        temperature_record = temperatures if isinstance(temperatures, Mapping) else {}
        tj = temperature_record.get("tj")
        if (
            isinstance(tj, bool)
            or not isinstance(tj, (int, float))
            or not math.isfinite(float(tj))
            or tj >= thermal_stop
        ):
            _fail(f"resource trace crossed the thermal stop: {session_index}")
    artifact_count = _validate_artifact_inventory(session_dir, manifest)

    expected_ledger: list[tuple[object, object, object, object]] = []
    for unit_index, condition in enumerate(expected_conditions, start=1):
        expected_ledger.extend(
            [
                ("unit_started", unit_index, condition, None),
                ("unit_completed", unit_index, condition, "completed"),
            ]
        )
    ledger = _load_jsonl(session_dir / "ledger.jsonl", "confirmatory ledger")
    for record in ledger:
        validate_privacy_boundary(record)
    observed_ledger = [
        (
            record.get("event"),
            record.get("unit_index"),
            record.get("condition"),
            record.get("status"),
        )
        for record in ledger
    ]
    if observed_ledger != expected_ledger:
        _fail(f"confirmatory ledger is incomplete or reordered: {session_index}")

    expected_unit_names = {
        f"unit-{index:02d}-{_unit_label(condition)}"
        for index, condition in enumerate(expected_conditions, start=1)
    }
    unit_root = session_dir / "units"
    actual_unit_names = {path.name for path in unit_root.iterdir() if path.is_dir()}
    if actual_unit_names != expected_unit_names:
        _fail(f"confirmatory unit directories differ: {session_index}")
    records: list[tuple[dict[str, object], dict[str, object]]] = []
    invocation_count = 0
    event_count = 0
    for unit_index, condition in enumerate(expected_conditions, start=1):
        unit, evidence, invocations, events = _validate_unit(
            session_dir,
            unit_index=unit_index,
            condition=condition,
            protocol=protocol,
        )
        records.append((unit, evidence))
        invocation_count += invocations
        event_count += events
    run_directories = {
        path.resolve() for path in (session_dir / "runs").iterdir() if path.is_dir()
    }
    referenced_directories = {
        (session_dir / relative).resolve()
        for unit, _evidence in records
        for relative in _mapping(unit.get("invocations"), "unit invocations").values()
        if isinstance(relative, str)
    }
    if run_directories != referenced_directories or invocation_count != 40:
        _fail(f"confirmatory invocation inventory differs: {session_index}")

    pairs = [
        _pair_summary(
            session_index,
            pair_index,
            expected_conditions[offset : offset + 2],
            records[offset : offset + 2],
        )
        for pair_index, offset in enumerate(range(0, 8, 2), start=1)
    ]
    session_summary = {
        "session_index": session_index,
        "session_id": session_dir.name,
        "condition_order": expected_conditions,
        "artifact_count": artifact_count,
        "resource_sample_count": len(resources),
        "resource_summary": _resource_summary(resources),
        "invocation_count": invocation_count,
        "event_count": event_count,
        "service_restart_verified": True,
        "minimum_separation_verified": True,
        "thermal_stop_requested": False,
        "protocol_commit": preflight_protocol.get("protocol_commit"),
        "runner_commit": preflight_protocol.get("runner_commit"),
        "commissioning_result_commit": preflight_commissioning.get("result_commit"),
        "formal_evidence": True,
        "confirmatory_data": True,
    }
    return session_summary, pairs


def _matrix(pairs: Sequence[Mapping[str, object]], name: str) -> list[list[float]]:
    values = [
        [0.0] * CONFIRMATORY_PAIRS_PER_SESSION
        for _ in range(CONFIRMATORY_SESSION_COUNT)
    ]
    seen: set[tuple[int, int]] = set()
    for pair in pairs:
        session_index = pair.get("session_index")
        pair_index = pair.get("pair_index_within_session")
        if (
            isinstance(session_index, bool)
            or not isinstance(session_index, int)
            or isinstance(pair_index, bool)
            or not isinstance(pair_index, int)
            or not 1 <= session_index <= CONFIRMATORY_SESSION_COUNT
            or not 1 <= pair_index <= CONFIRMATORY_PAIRS_PER_SESSION
            or (session_index, pair_index) in seen
        ):
            _fail("confirmatory pair identity is invalid or duplicated")
        seen.add((session_index, pair_index))
        values[session_index - 1][pair_index - 1] = _finite_number(pair.get(name), name)
    if len(seen) != CONFIRMATORY_SESSION_COUNT * CONFIRMATORY_PAIRS_PER_SESSION:
        _fail("confirmatory pair inventory is incomplete")
    return values


def analyze_confirmatory_collection(
    collection_dir: Path | str,
    *,
    bootstrap_resamples: int = BOOTSTRAP_RESAMPLES,
    bootstrap_seed: int = BOOTSTRAP_SEED,
) -> dict[str, object]:
    """Validate all artifacts, reconstruct 24 pairs and apply frozen decisions."""

    try:
        if (
            bootstrap_resamples != BOOTSTRAP_RESAMPLES
            or bootstrap_seed != BOOTSTRAP_SEED
        ):
            _fail("confirmatory bootstrap parameters differ from the frozen protocol")
        root = Path(collection_dir).resolve()
        if not root.is_dir():
            _fail("confirmatory collection directory does not exist")
        collection_id = root.name
        if _COLLECTION_RE.fullmatch(collection_id) is None:
            _fail("confirmatory collection id is invalid")
        protocol = load_confirmatory_protocol()
        if confirmatory_protocol_sha256(protocol) != CONFIRMATORY_PROTOCOL_SHA256:
            _fail("tracked confirmatory protocol identity failed")
        expected_directories = {
            f"session-{index:02d}-attempt-01"
            for index in range(1, CONFIRMATORY_SESSION_COUNT + 1)
        }
        actual_directories = {path.name for path in root.iterdir() if path.is_dir()}
        if actual_directories != expected_directories:
            _fail("confirmatory collection does not contain exactly six sessions")

        sessions: list[dict[str, object]] = []
        pairs: list[dict[str, object]] = []
        for session_index in range(1, CONFIRMATORY_SESSION_COUNT + 1):
            session, session_pairs = _reconstruct_session(
                root / f"session-{session_index:02d}-attempt-01",
                collection_id=collection_id,
                session_index=session_index,
                protocol=protocol,
            )
            sessions.append(session)
            pairs.extend(session_pairs)
        if len(pairs) != 24:
            _fail("confirmatory reconstruction did not produce exactly 24 pairs")
        for field in (
            "protocol_commit",
            "runner_commit",
            "commissioning_result_commit",
        ):
            if len({session[field] for session in sessions}) != 1:
                _fail(f"confirmatory {field} changed between sessions")

        charged = _matrix(pairs, "charged_log_ratio_treatment_over_control")
        residency = _matrix(
            pairs, "residency_difference_treatment_post_action_minus_control"
        )
        major_faults = _matrix(pairs, "major_fault_difference_treatment_minus_control")
        bootstrap = paired_hierarchical_bootstrap(
            charged,
            residency,
            major_faults,
            resamples=bootstrap_resamples,
            seed=bootstrap_seed,
        )
        gaps = [
            _finite_number(
                _mapping(pair.get(condition), f"{condition} pair record").get(
                    "probe_max_gap_ms"
                ),
                "probe maximum gap",
            )
            for pair in pairs
            for condition in ("control", "treatment")
        ]
        responsiveness_p95 = nearest_rank(gaps, 95)
        parameters = _mapping(protocol.get("parameters"), "confirmatory parameters")
        decisions = classify_estimands(
            bootstrap,
            responsiveness_p95_ms=responsiveness_p95,
            responsiveness_reference_ms=_positive_number(
                parameters.get("responsiveness_p95_reference_ms"),
                "responsiveness reference",
            ),
        )
        energy_ratios = [
            _positive_number(
                pair.get("energy_ratio_treatment_over_control"), "energy ratio"
            )
            for pair in pairs
        ]
        charged_ratios = [
            _positive_number(
                pair.get("charged_duration_ratio_treatment_over_control"),
                "charged ratio",
            )
            for pair in pairs
        ]
        prefetch_status_counts: dict[str, int] = {}
        for pair in pairs:
            treatment = _mapping(pair.get("treatment"), "treatment pair record")
            status = treatment.get("prefetch_status")
            if not isinstance(status, str):
                _fail("prefetch status is invalid")
            prefetch_status_counts[status] = prefetch_status_counts.get(status, 0) + 1

        result: dict[str, object] = {
            "confirmatory_analysis_schema_version": CONFIRMATORY_ANALYSIS_SCHEMA_VERSION,
            "artifact_kind": "phase2_confirmatory_analysis",
            "collection_id": collection_id,
            "protocol_id": CONFIRMATORY_PROTOCOL_ID,
            "protocol_sha256": CONFIRMATORY_PROTOCOL_SHA256,
            "bootstrap": {
                "method": "paired_hierarchical_percentile",
                "resamples": bootstrap_resamples,
                "seed": bootstrap_seed,
                "confidence_level": 0.95,
                "session_resampling_first": True,
                "pair_resampling_within_selected_session": True,
            },
            "session_count": len(sessions),
            "pair_count": len(pairs),
            "unit_count": len(gaps),
            "sessions": sessions,
            "pairs": pairs,
            "estimands": {
                **bootstrap,
                "responsiveness": {
                    "p95_nearest_rank_unit_probe_max_gap_ms": responsiveness_p95,
                    "reference_ms": parameters["responsiveness_p95_reference_ms"],
                },
            },
            "decisions": decisions,
            "secondary": {
                "charged_duration_ratio": _descriptive(charged_ratios),
                "energy_ratio": _descriptive(energy_ratios),
                "probe_max_gap_ms": _descriptive(gaps),
                "prefetch_status_counts": dict(sorted(prefetch_status_counts.items())),
            },
            "study_valid": True,
            "operational_benefit_supported": decisions["operational_benefit_supported"],
            "residency_mechanism_supported": decisions["residency_mechanism_supported"],
            "responsiveness_preserved": decisions["responsiveness_preserved"],
            "confirmatory_outcome": decisions["operational_benefit_decision"],
            "formal_evidence": True,
            "confirmatory_data": True,
            "confirmatory_collection_closed": True,
            "application_slice_authorized": False,
            "outlier_exclusion_applied": False,
            "missing_value_imputation_applied": False,
            "replacement_applied": False,
            "raw_input_recorded": False,
            "raw_model_text_recorded": False,
            "private_paths_recorded": False,
        }
        validate_privacy_boundary(result)
        return result
    except CommissioningReconstructionError as exc:
        raise ConfirmatoryAnalysisError(str(exc)) from exc


def _refuse_output_inside_collection(output: Path | None, root: Path) -> None:
    if output is None:
        return
    resolved = output.resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError:
        return
    raise ConfirmatoryAnalysisError(
        "analysis output must remain outside the immutable collection"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("collection_dir", type=Path)
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        _refuse_output_inside_collection(args.output, args.collection_dir)
        result = analyze_confirmatory_collection(args.collection_dir)
        text = json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2) + "\n"
        if args.output is None:
            print(text, end="")
        else:
            write_json_atomic(args.output, result)
    except (ConfirmatoryAnalysisError, ValueError) as exc:
        print(f"Phase 2 confirmatory analysis failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
