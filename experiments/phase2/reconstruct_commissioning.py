"""Deterministically reconstruct a complete Phase 2 commissioning collection."""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import NoReturn

from experiments.phase1.common.manifest import sha256_file, write_json_atomic
from experiments.phase1.common.telemetry_jetson import (
    load_resource_samples,
    validate_resource_samples,
)
from experiments.phase2.commissioning import (
    COMMISSIONING_ATTEMPT,
    COMMISSIONING_PROTOCOL_ID,
    COMMISSIONING_PROTOCOL_SHA256,
    COMMISSIONING_SESSION_COUNT,
    canonical_commissioning_protocol_text,
    commissioning_conditions,
    commissioning_protocol_sha256,
    load_commissioning_protocol,
)
from experiments.phase2.commissioning_preflight import (
    commissioning_preflight_errors,
)
from experiments.phase2.evidence import (
    CONTROL_CONDITION,
    PREFETCH_CONDITION,
    validate_unit_evidence,
)
from experiments.phase2.observations import (
    validate_boundary_observation,
    validate_process_observation,
)
from experiments.phase2.privacy import validate_privacy_boundary
from experiments.phase2.run_commissioning_session import (
    COMMISSIONING_SESSION_SCHEMA_VERSION,
)


COMMISSIONING_RECONSTRUCTION_SCHEMA_VERSION = "0.1.0"
_COLLECTION_RE = re.compile(r"^[0-9]{8}T[0-9]{6}Z_phase2_commissioning_v1$")


class CommissioningReconstructionError(ValueError):
    """Commissioning artifacts do not support deterministic reconstruction."""


def _fail(message: str) -> NoReturn:
    raise CommissioningReconstructionError(message)


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        _fail(f"{name} is not a record")
    return value


def _list(value: object, name: str) -> list[object]:
    if not isinstance(value, list):
        _fail(f"{name} is not a list")
    return value


def _positive_number(value: object, name: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) <= 0
    ):
        _fail(f"{name} is not positive and finite")
    return float(value)


def _positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        _fail(f"{name} is not a positive integer")
    return value


def _parse_utc(value: object, name: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        _fail(f"{name} is not a UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise CommissioningReconstructionError(
            f"{name} is not a UTC timestamp"
        ) from exc
    if parsed.tzinfo is None:
        _fail(f"{name} has no timezone")
    return parsed.astimezone(timezone.utc)


def _load_json(path: Path, name: str) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CommissioningReconstructionError(f"{name} cannot be loaded") from exc
    if not isinstance(value, Mapping):
        _fail(f"{name} is not an object")
    return dict(value)


def _load_jsonl(path: Path, name: str) -> list[dict[str, object]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise CommissioningReconstructionError(f"{name} cannot be loaded") from exc
    records: list[dict[str, object]] = []
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            _fail(f"{name} line {line_number} is empty")
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise CommissioningReconstructionError(
                f"{name} line {line_number} is invalid JSON"
            ) from exc
        if not isinstance(value, Mapping):
            _fail(f"{name} line {line_number} is not an object")
        records.append(dict(value))
    return records


def _validate_artifact_inventory(
    session_dir: Path, manifest: Mapping[str, object]
) -> int:
    actual = sorted(
        path
        for path in session_dir.rglob("*")
        if path.is_file()
        and path.name != "manifest.json"
        and not path.name.endswith(".tmp")
    )
    inventory = _mapping(manifest.get("artifacts"), "artifact inventory")
    items = _list(inventory.get("files"), "artifact inventory files")
    declared: dict[str, Mapping[str, object]] = {}
    for item in items:
        record = _mapping(item, "artifact inventory item")
        name = record.get("relative_name")
        if not isinstance(name, str) or not name or name in declared:
            _fail("artifact inventory contains an invalid or duplicate name")
        declared[name] = record
    actual_names = {path.relative_to(session_dir).as_posix() for path in actual}
    if inventory.get("count") != len(actual) or set(declared) != actual_names:
        _fail("artifact inventory does not match the session files")
    for path in actual:
        name = path.relative_to(session_dir).as_posix()
        record = declared[name]
        if record.get("size_bytes") != path.stat().st_size:
            _fail(f"artifact size differs from manifest: {name}")
        if record.get("sha256") != sha256_file(path):
            _fail(f"artifact SHA-256 differs from manifest: {name}")
    return len(actual)


def _validate_events(path: Path, label: str) -> int:
    records = _load_jsonl(path, label)
    if not records:
        _fail(f"{label} contains no events")
    previous_time = -1
    for expected_sequence, record in enumerate(records):
        validate_privacy_boundary(record)
        if record.get("seq") != expected_sequence:
            _fail(f"{label} sequence is not contiguous")
        timestamp = record.get("monotonic_ns")
        if (
            isinstance(timestamp, bool)
            or not isinstance(timestamp, int)
            or timestamp < previous_time
        ):
            _fail(f"{label} monotonic time moved backwards")
        previous_time = timestamp
    return len(records)


def _validate_invocation(path: Path) -> tuple[dict[str, object], int]:
    run = _load_json(path / "run.json", f"invocation {path.name}")
    validate_privacy_boundary(run)
    if (
        run.get("artifact_kind") != "phase2_correctness_pilot_invocation"
        or run.get("status") == "failed"
        or run.get("valid") is not True
        or run.get("formal_evidence") is not False
        or run.get("application_slice_authorized") is not False
        or run.get("raw_input_recorded") is not False
        or run.get("raw_output_recorded") is not False
    ):
        _fail(f"invocation is invalid or exceeds authority: {path.name}")
    gates = _list(run.get("gates"), f"invocation gates {path.name}")
    if not gates or any(
        not isinstance(gate, Mapping) or gate.get("passed") is not True
        for gate in gates
    ):
        _fail(f"invocation contains a failed Gate: {path.name}")
    process_observation = run.get("process_observation")
    if process_observation is not None:
        validate_process_observation(
            _mapping(process_observation, f"process observation {path.name}")
        )
    if run.get("workload") == "vlm":
        by_name = {
            gate.get("name"): gate
            for gate in gates
            if isinstance(gate, Mapping) and isinstance(gate.get("name"), str)
        }
        for name in (
            "child_process_reaped",
            "model_unload_claim_bounded",
            "residency_contract_verified",
        ):
            if by_name.get(name, {}).get("passed") is not True:
                _fail(f"VLM lifecycle Gate is missing: {path.name}: {name}")
    event_count = _validate_events(
        path / "events.jsonl", f"invocation events {path.name}"
    )
    return run, event_count


def _unit_label(condition: str) -> str:
    if condition == CONTROL_CONDITION:
        return "control"
    if condition == PREFETCH_CONDITION:
        return "prefetch"
    _fail("unit condition is unsupported")


def _validate_unit(
    session_dir: Path,
    *,
    unit_index: int,
    condition: str,
    protocol: Mapping[str, object],
) -> tuple[dict[str, object], dict[str, object], int, int]:
    unit_dir = session_dir / "units" / f"unit-{unit_index:02d}-{_unit_label(condition)}"
    unit = _load_json(unit_dir / "unit.json", f"unit {unit_index}")
    evidence = _load_json(unit_dir / "evidence.json", f"unit evidence {unit_index}")
    validate_privacy_boundary(unit)
    validate_unit_evidence(evidence)
    if (
        unit.get("artifact_kind") != "phase2_correctness_pilot_unit"
        or unit.get("unit_index") != unit_index
        or unit.get("condition") != condition
        or unit.get("status") != "completed"
        or unit.get("completed_steps") != protocol.get("unit_sequence")
        or unit.get("formal_evidence") is not False
        or unit.get("application_slice_authorized") is not False
        or evidence.get("condition") != condition
    ):
        _fail(f"unit identity, schedule or authority is invalid: {unit_index}")
    observations = _mapping(unit.get("observations"), f"unit observations {unit_index}")
    for observation in observations.values():
        validate_boundary_observation(
            _mapping(observation, f"unit boundary observation {unit_index}")
        )
    expected_observations = (
        {"pre_unit", "post_primer", "post_vlm", "post_asr"}
        if condition == CONTROL_CONDITION
        else {"pre_unit", "post_primer", "post_vlm", "post_action", "post_asr"}
    )
    if set(observations) != expected_observations:
        _fail(f"unit boundary set is invalid: {unit_index}")
    probe = _mapping(unit.get("probe"), f"unit probe {unit_index}")
    tick_count = probe.get("tick_count")
    if (
        probe.get("implementation") != "independent_thread"
        or probe.get("joined") is not True
        or probe.get("error_code") is not None
        or isinstance(tick_count, bool)
        or not isinstance(tick_count, int)
        or tick_count < 2
    ):
        _fail(f"unit probe did not close: {unit_index}")

    invocations = _mapping(unit.get("invocations"), f"unit invocations {unit_index}")
    expected_roles = {"primer_1", "primer_2", "vlm", "measured_asr", "recovery_asr"}
    if set(invocations) != expected_roles:
        _fail(f"unit invocation roles are incomplete: {unit_index}")
    invocation_count = 0
    event_count = _validate_events(
        unit_dir / "events.jsonl", f"unit events {unit_index}"
    )
    for role in sorted(expected_roles):
        relative = invocations.get(role)
        if not isinstance(relative, str) or not relative.startswith("runs/"):
            _fail(f"unit invocation reference is invalid: {unit_index}: {role}")
        invocation_dir = (session_dir / relative).resolve()
        try:
            invocation_dir.relative_to(session_dir.resolve())
        except ValueError:
            _fail(f"unit invocation escapes the session: {unit_index}: {role}")
        run, run_events = _validate_invocation(invocation_dir)
        if (
            run.get("condition") != condition
            or run.get("pilot_role") != role
            or run.get("workload") != ("vlm" if role == "vlm" else "asr")
        ):
            _fail(f"unit invocation binding is invalid: {unit_index}: {role}")
        invocation_count += 1
        event_count += run_events
    return unit, evidence, invocation_count, event_count


def _pair_summary(
    session_index: int,
    conditions: Sequence[str],
    records: Sequence[tuple[dict[str, object], dict[str, object]]],
) -> dict[str, object]:
    by_condition = {
        condition: (unit, evidence)
        for condition, (unit, evidence) in zip(conditions, records)
    }
    if set(by_condition) != {CONTROL_CONDITION, PREFETCH_CONDITION}:
        _fail("commissioning pair does not contain one unit per condition")
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
    control_probe = _mapping(control_unit.get("probe"), "control probe")
    treatment_probe = _mapping(treatment_unit.get("probe"), "treatment probe")
    return {
        "session_index": session_index,
        "pair_index_within_session": 1,
        "condition_order": list(conditions),
        "control": {
            "fully_charged_duration_ms": control_charged,
            "asr_duration_ms": _positive_number(
                control.get("asr_duration_ms"), "control ASR duration"
            ),
            "energy_mj": control_energy,
            "post_vlm_resident_fraction": control_common.get("resident_fraction"),
            "responsiveness_reference_met": control_probe.get(
                "responsiveness_reference_met"
            ),
            "probe_max_gap_ms": _positive_number(
                control_probe.get("max_gap_ns"), "control probe maximum gap"
            )
            / 1e6,
        },
        "treatment": {
            "fully_charged_duration_ms": treatment_charged,
            "asr_duration_ms": _positive_number(
                treatment.get("asr_duration_ms"), "treatment ASR duration"
            ),
            "energy_mj": treatment_energy,
            "post_vlm_resident_fraction": treatment_common.get("resident_fraction"),
            "post_action_resident_fraction": treatment_verified.get(
                "resident_fraction"
            ),
            "prefetch_status": action.get("status"),
            "prefetch_bytes_read": action.get("bytes_read"),
            "responsiveness_reference_met": treatment_probe.get(
                "responsiveness_reference_met"
            ),
            "probe_max_gap_ms": _positive_number(
                treatment_probe.get("max_gap_ns"),
                "treatment probe maximum gap",
            )
            / 1e6,
        },
        "charged_duration_ratio_treatment_over_control": (
            treatment_charged / control_charged
        ),
        "charged_duration_delta_ms": treatment_charged - control_charged,
        "energy_ratio_treatment_over_control": treatment_energy / control_energy,
        "energy_delta_mj": treatment_energy - control_energy,
        "formal_interpretation_permitted": False,
    }


def _reconstruct_session(
    session_dir: Path,
    *,
    collection_id: str,
    session_index: int,
    protocol: Mapping[str, object],
) -> tuple[dict[str, object], dict[str, object]]:
    manifest = _load_json(session_dir / "manifest.json", "session manifest")
    validate_privacy_boundary(manifest)
    if (
        manifest.get("commissioning_session_schema_version")
        != COMMISSIONING_SESSION_SCHEMA_VERSION
        or manifest.get("artifact_kind") != "phase2_commissioning_session"
        or manifest.get("collection_id") != collection_id
        or manifest.get("session_id") != session_dir.name
        or manifest.get("session_index") != session_index
        or manifest.get("attempt") != COMMISSIONING_ATTEMPT
        or manifest.get("protocol_id") != COMMISSIONING_PROTOCOL_ID
        or manifest.get("protocol_sha256") != COMMISSIONING_PROTOCOL_SHA256
        or manifest.get("conditions")
        != list(commissioning_conditions(protocol, session_index))
        or manifest.get("status") != "completed"
        or manifest.get("failure_class") is not None
        or manifest.get("failure_code") is not None
        or manifest.get("formal_evidence") is not False
        or manifest.get("confirmatory_data") is not False
        or manifest.get("application_slice_authorized") is not False
        or manifest.get("development_injection") is not False
        or manifest.get("completed_units") != 2
    ):
        _fail(f"commissioning session manifest is invalid: {session_index}")
    created_at = _parse_utc(manifest.get("created_at"), "session created_at")
    completed_at = _parse_utc(manifest.get("completed_at"), "session completed_at")
    if completed_at < created_at:
        _fail(f"commissioning session completion moved backwards: {session_index}")

    source_protocol = _load_json(
        session_dir / "protocol.json", f"session protocol {session_index}"
    )
    if (
        source_protocol != protocol
        or commissioning_protocol_sha256(source_protocol)
        != COMMISSIONING_PROTOCOL_SHA256
        or (session_dir / "protocol.json").read_text(encoding="utf-8")
        != canonical_commissioning_protocol_text(protocol)
    ):
        _fail(f"commissioning protocol copy differs: {session_index}")
    preflight = _load_json(
        session_dir / "preflight.json", f"session preflight {session_index}"
    )
    validate_privacy_boundary(preflight)
    preflight_errors = commissioning_preflight_errors(preflight)
    if preflight_errors:
        _fail(
            f"commissioning preflight is invalid: {session_index}: "
            + "; ".join(preflight_errors)
        )
    manifest_preflight = _mapping(
        manifest.get("preflight"), f"manifest preflight {session_index}"
    )
    preflight_protocol = _mapping(
        preflight.get("protocol"), f"preflight protocol {session_index}"
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
        or manifest_preflight.get("service_identity")
        != preflight.get("service_identity")
        or manifest_preflight.get("prior_session_index")
        != preflight_prior.get("session_index")
        or manifest_preflight.get("separation_s")
        != preflight_separation.get("observed_s")
    ):
        _fail(f"manifest and preflight bindings differ: {session_index}")

    parameters = _mapping(protocol.get("parameters"), "commissioning parameters")
    thermal_start_maximum_tj_c = _positive_number(
        parameters.get("thermal_start_maximum_tj_c"),
        "thermal start maximum",
    )
    thermal_start_consecutive_samples = _positive_int(
        parameters.get("thermal_start_consecutive_samples"),
        "thermal start sample count",
    )
    thermal_stop_tj_c = _positive_number(
        parameters.get("thermal_stop_tj_c"), "thermal stop threshold"
    )
    thermal = _mapping(manifest.get("thermal"), f"thermal record {session_index}")
    readiness = _mapping(thermal.get("readiness"), f"thermal readiness {session_index}")
    if (
        thermal.get("stop_requested") is not False
        or thermal.get("stop_tj_c") != thermal_stop_tj_c
        or readiness.get("maximum_tj_c") != thermal_start_maximum_tj_c
        or readiness.get("consecutive_samples") != thermal_start_consecutive_samples
    ):
        _fail(f"thermal contract is invalid: {session_index}")
    observed_tj = readiness.get("observed_tj_c")
    first_sequence = readiness.get("first_sequence")
    last_sequence = readiness.get("last_sequence")
    if (
        not isinstance(observed_tj, list)
        or len(observed_tj) != thermal_start_consecutive_samples
        or any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or value > thermal_start_maximum_tj_c
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
            or tj >= thermal_stop_tj_c
        ):
            _fail(f"resource trace crossed the thermal stop: {session_index}")
    artifact_count = _validate_artifact_inventory(session_dir, manifest)

    conditions = commissioning_conditions(protocol, session_index)
    expected_ledger: list[tuple[object, object, object, object]] = []
    for unit_index, condition in enumerate(conditions, start=1):
        expected_ledger.extend(
            [
                ("unit_started", unit_index, condition, None),
                ("unit_completed", unit_index, condition, "completed"),
            ]
        )
    ledger = _load_jsonl(session_dir / "ledger.jsonl", "commissioning ledger")
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
        _fail(f"commissioning ledger is incomplete or reordered: {session_index}")

    expected_unit_names = {
        f"unit-{index:02d}-{_unit_label(condition)}"
        for index, condition in enumerate(conditions, start=1)
    }
    unit_root = session_dir / "units"
    actual_unit_names = {path.name for path in unit_root.iterdir() if path.is_dir()}
    if actual_unit_names != expected_unit_names:
        _fail(f"commissioning unit directories differ: {session_index}")
    records: list[tuple[dict[str, object], dict[str, object]]] = []
    invocation_count = 0
    event_count = 0
    for unit_index, condition in enumerate(conditions, start=1):
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
    if run_directories != referenced_directories or invocation_count != 10:
        _fail(f"commissioning invocation inventory differs: {session_index}")

    pair = _pair_summary(session_index, conditions, records)
    separation_s = preflight_separation.get("observed_s")
    minimum_session_separation_s = _positive_number(
        protocol.get("minimum_session_separation_s"),
        "minimum session separation",
    )
    minimum_separation_verified = session_index == 1 or (
        not isinstance(separation_s, bool)
        and isinstance(separation_s, (int, float))
        and separation_s >= minimum_session_separation_s
    )
    session_summary = {
        "session_index": session_index,
        "session_id": session_dir.name,
        "condition_order": list(conditions),
        "artifact_count": artifact_count,
        "resource_sample_count": len(resources),
        "invocation_count": invocation_count,
        "event_count": event_count,
        "service_restart_verified": True,
        "minimum_separation_verified": minimum_separation_verified,
        "thermal_stop_requested": False,
        "formal_evidence": False,
    }
    return session_summary, pair


def reconstruct_collection(collection_dir: Path | str) -> dict[str, object]:
    """Validate and derive a complete two-session commissioning record."""

    root = Path(collection_dir).resolve()
    if not root.is_dir():
        _fail("commissioning collection directory does not exist")
    collection_id = root.name
    if _COLLECTION_RE.fullmatch(collection_id) is None:
        _fail("commissioning collection id is invalid")
    protocol = load_commissioning_protocol()
    if commissioning_protocol_sha256(protocol) != COMMISSIONING_PROTOCOL_SHA256:
        _fail("tracked commissioning protocol identity failed")
    expected_directories = {
        f"session-{index:02d}-attempt-01"
        for index in range(1, COMMISSIONING_SESSION_COUNT + 1)
    }
    actual_directories = {path.name for path in root.iterdir() if path.is_dir()}
    if actual_directories != expected_directories:
        _fail("commissioning collection does not contain exactly two sessions")

    sessions: list[dict[str, object]] = []
    pairs: list[dict[str, object]] = []
    for session_index in range(1, COMMISSIONING_SESSION_COUNT + 1):
        session, pair = _reconstruct_session(
            root / f"session-{session_index:02d}-attempt-01",
            collection_id=collection_id,
            session_index=session_index,
            protocol=protocol,
        )
        sessions.append(session)
        pairs.append(pair)
    result: dict[str, object] = {
        "commissioning_reconstruction_schema_version": (
            COMMISSIONING_RECONSTRUCTION_SCHEMA_VERSION
        ),
        "artifact_kind": "phase2_commissioning_reconstruction",
        "collection_id": collection_id,
        "protocol_id": COMMISSIONING_PROTOCOL_ID,
        "protocol_sha256": COMMISSIONING_PROTOCOL_SHA256,
        "session_count": len(sessions),
        "pair_count": len(pairs),
        "sessions": sessions,
        "pairs": pairs,
        "commissioning_valid": True,
        "evidence_path_executable": True,
        "formal_protocol_parameters_may_be_frozen": True,
        "formal_evidence": False,
        "confirmatory_data": False,
        "confirmatory_collection_authorized": False,
        "operational_benefit_interpretation_permitted": False,
        "application_slice_authorized": False,
        "raw_input_recorded": False,
        "raw_model_text_recorded": False,
        "private_paths_recorded": False,
    }
    validate_privacy_boundary(result)
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("collection_dir", type=Path)
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = reconstruct_collection(args.collection_dir)
        if args.output is None:
            print(
                json.dumps(
                    result,
                    ensure_ascii=False,
                    allow_nan=False,
                    indent=2,
                    sort_keys=True,
                )
            )
        else:
            output = args.output.expanduser().resolve()
            collection = args.collection_dir.expanduser().resolve()
            try:
                output.relative_to(collection)
            except ValueError:
                pass
            else:
                if any(
                    output == session or session in output.parents
                    for session in collection.glob("session-*-attempt-*")
                ):
                    raise CommissioningReconstructionError(
                        "reconstruction output cannot alter a session inventory"
                    )
            if output.exists():
                raise FileExistsError(
                    f"refusing to overwrite commissioning reconstruction: {output}"
                )
            write_json_atomic(output, result)
            print(output)
    except BaseException as exc:
        print(
            f"Phase 2 commissioning reconstruction failed: "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
