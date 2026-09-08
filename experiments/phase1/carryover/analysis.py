"""Validate and summarize the Phase 1 ASR/VLM carryover diagnostic."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import statistics
import sys
from pathlib import Path
from typing import Mapping, Sequence

from experiments.phase1.carryover.observation import (
    validate_memory_observation,
    validate_process_observation,
)
from experiments.phase1.carryover.preflight import carryover_preflight_errors
from experiments.phase1.carryover.protocol import (
    CARRYOVER_PROTOCOL_ID,
    CARRYOVER_PROTOCOL_SHA256,
    diagnostic_orders,
    load_protocol,
    protocol_sha256,
)
from experiments.phase1.common.telemetry_jetson import (
    load_resource_samples,
    validate_resource_samples,
)
from experiments.phase1.common.manifest import sha256_file
from experiments.phase1.run_carryover_session import (
    CARRYOVER_RUN_SCHEMA_VERSION,
    CARRYOVER_SESSION_SCHEMA_VERSION,
)


CARRYOVER_ANALYSIS_SCHEMA_VERSION = "0.2.0"
CARRYOVER_ANALYSIS_KIND = "phase1_asr_vlm_carryover_diagnostic"
_CONDITIONS = ("idle", "llm", "vlm")


def _read_object(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    return value


def _artifact_inventory(session_dir: Path) -> list[dict[str, object]]:
    return [
        {
            "path": path.relative_to(session_dir).as_posix(),
            "size_bytes": path.stat().st_size,
            "sha256": sha256_file(path),
        }
        for path in sorted(
            item
            for item in session_dir.rglob("*")
            if item.is_file()
            and item.name != "manifest.json"
            and not item.name.endswith(".tmp")
        )
    ]


def _validate_inventory(session_dir: Path, manifest: Mapping[str, object]) -> None:
    inventory = manifest.get("artifacts")
    if not isinstance(inventory, Mapping):
        raise ValueError("session artifact inventory is missing")
    files = inventory.get("files")
    actual = _artifact_inventory(session_dir)
    if inventory.get("count") != len(actual) or files != actual:
        raise ValueError("session artifact inventory does not match files on disk")


def _number(value: object, name: str, *, positive: bool = False) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or (positive and value <= 0)
    ):
        raise ValueError(f"{name} is invalid")
    return float(value)


def _load_run(session_dir: Path, relative: object, *, role: str) -> dict[str, object]:
    if not isinstance(relative, str) or not relative.startswith("runs/"):
        raise ValueError(f"{role} run reference is invalid")
    path = (session_dir / relative).resolve()
    try:
        path.relative_to(session_dir.resolve())
    except ValueError as exc:
        raise ValueError(f"{role} run escapes the session") from exc
    run = _read_object(path / "run.json")
    if (
        run.get("carryover_run_schema_version") != CARRYOVER_RUN_SCHEMA_VERSION
        or run.get("artifact_kind") != "phase1_carryover_diagnostic_invocation"
        or run.get("diagnostic_role") != role
        or run.get("valid") is not True
        or run.get("formal_evidence") is not False
        or "formal_run_schema_version" in run
        or "formal_claim_permitted" in run
    ):
        raise ValueError(f"{role} diagnostic invocation is invalid")
    gates = run.get("gates")
    if (
        not isinstance(gates, list)
        or not gates
        or any(
            not isinstance(gate, Mapping) or gate.get("passed") is not True
            for gate in gates
        )
    ):
        raise ValueError(f"{role} invocation Gates did not all pass")
    if run.get("workload") == "asr":
        process = run.get("asr_process_observation")
        if not isinstance(process, Mapping):
            raise ValueError(f"{role} ASR process observation is missing")
        validate_process_observation(process)
    elif run.get("asr_process_observation") is not None:
        raise ValueError(f"{role} non-ASR run has an ASR process observation")
    return run


def _adapter_duration_ms(run: Mapping[str, object]) -> float:
    adapter = run.get("adapter")
    if not isinstance(adapter, Mapping):
        raise ValueError("adapter record is missing")
    return (
        _number(adapter.get("duration_ns"), "adapter duration", positive=True)
        / 1_000_000
    )


def _residency(observations: Mapping[str, object], boundary: str) -> dict[str, object]:
    value = observations.get(boundary)
    if not isinstance(value, Mapping):
        raise ValueError(f"memory observation is missing: {boundary}")
    validate_memory_observation(value)
    residency = value.get("whisper_model_residency")
    assert isinstance(residency, Mapping)
    memory = value.get("memory")
    assert isinstance(memory, Mapping)
    return {
        "resident_pages": int(residency["resident_pages"]),
        "total_pages": int(residency["total_pages"]),
        "resident_fraction": float(residency["resident_fraction"]),
        "MemAvailable_bytes": int(memory["MemAvailable_bytes"]),
        "Cached_bytes": int(memory["Cached_bytes"]),
        "SReclaimable_bytes": int(memory["SReclaimable_bytes"]),
    }


def _analyze_unit(session_dir: Path, path: Path, condition: str) -> dict[str, object]:
    unit = _read_object(path)
    if (
        unit.get("unit_schema_version") != "0.1.0"
        or unit.get("interposer") != condition
        or unit.get("valid") is not True
        or unit.get("formal_evidence") is not False
        or "pass" in unit
        or "decision" in unit
    ):
        raise ValueError(f"{condition} unit identity is invalid")
    timing = unit.get("timing")
    if not isinstance(timing, Mapping):
        raise ValueError(f"{condition} timing record is missing")
    if (
        timing.get("target_interval_s") != 150.0
        or timing.get("interposer_overrun") is not False
        or not 150_000.0
        <= _number(timing.get("observed_interval_ms"), "observed interval")
        <= 150_250.0
        or not 0.0
        <= _number(timing.get("start_lateness_ms"), "start lateness")
        <= 250.0
    ):
        raise ValueError(f"{condition} fixed interval is invalid")
    if not 0 < _number(unit.get("primer_2_duration_ms"), "primer duration") <= 5_000:
        raise ValueError(f"{condition} primer 2 did not prove warm state")
    observations = unit.get("observations")
    if not isinstance(observations, Mapping) or set(observations) != {
        "before_primers",
        "after_primer_2",
        "after_interposer",
        "after_measured_asr",
    }:
        raise ValueError(f"{condition} observations are incomplete")
    observation_summary = {
        boundary: _residency(observations, boundary)
        for boundary in (
            "before_primers",
            "after_primer_2",
            "after_interposer",
            "after_measured_asr",
        )
    }
    references = unit.get("runs")
    if not isinstance(references, Mapping):
        raise ValueError(f"{condition} run references are missing")
    required_roles = {"primer_1", "primer_2", "measured_asr", "recovery_asr"}
    if condition != "idle":
        required_roles.add("interposer")
    if set(references) != required_roles:
        raise ValueError(f"{condition} run references are incomplete")
    runs = {
        role: _load_run(session_dir, references[role], role=role)
        for role in sorted(required_roles)
    }
    for role in ("primer_1", "primer_2", "measured_asr", "recovery_asr"):
        if runs[role].get("workload") != "asr":
            raise ValueError(f"{condition} {role} workload is invalid")
    if condition != "idle" and runs["interposer"].get("workload") != condition:
        raise ValueError(f"{condition} interposer workload is invalid")
    measured_process = runs["measured_asr"]["asr_process_observation"]
    assert isinstance(measured_process, Mapping)
    measured_ms = _adapter_duration_ms(runs["measured_asr"])
    recovery_ms = _adapter_duration_ms(runs["recovery_asr"])
    if (
        abs(
            measured_ms
            - _number(unit.get("measured_asr_duration_ms"), "measured duration")
        )
        > 1e-9
    ):
        raise ValueError(f"{condition} measured duration is inconsistent")
    if (
        abs(
            recovery_ms
            - _number(unit.get("recovery_asr_duration_ms"), "recovery duration")
        )
        > 1e-9
    ):
        raise ValueError(f"{condition} recovery duration is inconsistent")
    return {
        "condition": condition,
        "unit_index": unit.get("unit_index"),
        "primer_2_duration_ms": unit.get("primer_2_duration_ms"),
        "measured_asr_duration_ms": measured_ms,
        "recovery_asr_duration_ms": recovery_ms,
        "timing": dict(timing),
        "residency": observation_summary,
        "measured_asr_process": {
            name: measured_process[name]
            for name in (
                "user_time_s",
                "system_time_s",
                "minor_faults",
                "major_faults",
                "maximum_rss_bytes",
                "voluntary_context_switches",
                "involuntary_context_switches",
                "sample_count",
                "read_error_count",
            )
        },
    }


def _geometric_mean(values: Sequence[float]) -> float:
    if not values or any(value <= 0 or not math.isfinite(value) for value in values):
        raise ValueError("geometric mean requires positive finite observations")
    return math.exp(statistics.fmean(math.log(value) for value in values))


def _contrast(
    sessions: Sequence[Mapping[str, object]], condition: str
) -> dict[str, object]:
    duration_ratios: list[float] = []
    residency_differences: list[float] = []
    minor_fault_differences: list[int] = []
    major_fault_differences: list[int] = []
    per_session: list[dict[str, object]] = []
    for session in sessions:
        units = session["units"]
        assert isinstance(units, Mapping)
        selected = units[condition]
        idle = units["idle"]
        assert isinstance(selected, Mapping) and isinstance(idle, Mapping)
        selected_duration = float(selected["measured_asr_duration_ms"])
        idle_duration = float(idle["measured_asr_duration_ms"])
        selected_residency = selected["residency"]
        idle_residency = idle["residency"]
        selected_process = selected["measured_asr_process"]
        idle_process = idle["measured_asr_process"]
        assert isinstance(selected_residency, Mapping)
        assert isinstance(idle_residency, Mapping)
        assert isinstance(selected_process, Mapping)
        assert isinstance(idle_process, Mapping)
        selected_post = selected_residency["after_interposer"]
        idle_post = idle_residency["after_interposer"]
        assert isinstance(selected_post, Mapping) and isinstance(idle_post, Mapping)
        ratio = selected_duration / idle_duration
        residency_difference = float(selected_post["resident_fraction"]) - float(
            idle_post["resident_fraction"]
        )
        minor_difference = int(selected_process["minor_faults"]) - int(
            idle_process["minor_faults"]
        )
        major_difference = int(selected_process["major_faults"]) - int(
            idle_process["major_faults"]
        )
        duration_ratios.append(ratio)
        residency_differences.append(residency_difference)
        minor_fault_differences.append(minor_difference)
        major_fault_differences.append(major_difference)
        per_session.append(
            {
                "session": session["session"],
                "duration_ratio": ratio,
                "resident_fraction_difference": residency_difference,
                "minor_fault_difference": minor_difference,
                "major_fault_difference": major_difference,
            }
        )
    return {
        "condition_vs_idle": condition,
        "session_count": len(per_session),
        "per_session": per_session,
        "duration_ratio": {
            "geometric_mean": _geometric_mean(duration_ratios),
            "median": statistics.median(duration_ratios),
            "minimum": min(duration_ratios),
            "maximum": max(duration_ratios),
        },
        "resident_fraction_difference": {
            "mean": statistics.fmean(residency_differences),
            "median": statistics.median(residency_differences),
            "minimum": min(residency_differences),
            "maximum": max(residency_differences),
        },
        "minor_fault_difference": {
            "mean": statistics.fmean(minor_fault_differences),
            "median": statistics.median(minor_fault_differences),
        },
        "major_fault_difference": {
            "mean": statistics.fmean(major_fault_differences),
            "median": statistics.median(major_fault_differences),
        },
    }


def analyze_collection(collection_dir: Path | str) -> dict[str, object]:
    """Validate all six sessions and produce descriptive paired contrasts."""

    directory = Path(collection_dir).resolve()
    protocol = load_protocol()
    if protocol_sha256(protocol) != CARRYOVER_PROTOCOL_SHA256:
        raise ValueError("tracked carryover protocol SHA-256 is invalid")
    sessions: list[dict[str, object]] = []
    service_identities: list[Mapping[str, object]] = []
    resource_sample_count = 0
    maximum_tj_c = -math.inf
    for session_index, expected_order in enumerate(diagnostic_orders(), start=1):
        session_dir = directory / f"session-{session_index:02d}-attempt-01"
        manifest = _read_object(session_dir / "manifest.json")
        if (
            manifest.get("carryover_session_schema_version")
            != CARRYOVER_SESSION_SCHEMA_VERSION
            or manifest.get("artifact_kind")
            != "phase1_asr_vlm_carryover_diagnostic_session"
            or manifest.get("status") != "completed"
            or manifest.get("formal_evidence") is not False
            or manifest.get("protocol_id") != CARRYOVER_PROTOCOL_ID
            or manifest.get("protocol_sha256") != protocol_sha256(protocol)
            or manifest.get("interposer_order") != list(expected_order)
            or manifest.get("completed_units") != 3
            or "formal_claim_permitted" in manifest
        ):
            raise ValueError(f"session {session_index} manifest is invalid")
        _validate_inventory(session_dir, manifest)
        protocol_copy = _read_object(session_dir / "protocol.json")
        if protocol_copy != protocol:
            raise ValueError(f"session {session_index} protocol copy is invalid")
        preflight = _read_object(session_dir / "preflight.json")
        if carryover_preflight_errors(preflight):
            raise ValueError(f"session {session_index} preflight is invalid")
        preflight_summary = manifest.get("preflight")
        if not isinstance(preflight_summary, Mapping):
            raise ValueError(f"session {session_index} preflight summary is missing")
        preflight_protocol = preflight.get("protocol")
        if not isinstance(preflight_protocol, Mapping):
            raise ValueError(f"session {session_index} protocol preflight is missing")
        identity = preflight_summary.get("service_identity")
        if not isinstance(identity, Mapping):
            raise ValueError(f"session {session_index} service identity is missing")
        if (
            preflight_summary.get("protocol_commit")
            != preflight_protocol.get("protocol_commit")
            or preflight_summary.get("runner_commit")
            != preflight_protocol.get("runner_commit")
            or identity != preflight.get("service_identity")
            or re.fullmatch(
                r"[0-9a-f]{40}", str(preflight_summary.get("runner_commit"))
            )
            is None
            or re.fullmatch(
                r"[0-9a-f]{40}", str(preflight_summary.get("protocol_commit"))
            )
            is None
        ):
            raise ValueError(
                f"session {session_index} preflight summary is inconsistent"
            )
        if service_identities and any(
            service_identities[-1].get(name) == identity.get(name)
            for name in ("llama-server", "ollama")
        ):
            raise ValueError("model services did not both change between sessions")
        service_identities.append(identity)
        resources = load_resource_samples(session_dir / "resources.jsonl")
        resource_errors = validate_resource_samples(resources)
        if resource_errors:
            raise ValueError(
                f"session {session_index} resource trace is invalid: "
                + "; ".join(resource_errors)
            )
        resource_sample_count += len(resources)
        for sample in resources:
            temperatures = sample.get("temperatures_c")
            if isinstance(temperatures, Mapping):
                maximum_tj_c = max(maximum_tj_c, float(temperatures["tj"]))
        unit_files = sorted((session_dir / "units").glob("*/unit.json"))
        if len(unit_files) != 3:
            raise ValueError(f"session {session_index} does not contain three units")
        units = {
            condition: _analyze_unit(
                session_dir,
                session_dir
                / "units"
                / f"unit-{position:02d}-{condition}"
                / "unit.json",
                condition,
            )
            for position, condition in enumerate(expected_order, start=1)
        }
        run_starts: list[int] = []
        run_finishes: list[int] = []
        for unit in units.values():
            unit_index_value = _number(
                unit.get("unit_index"), "unit index", positive=True
            )
            condition_value = unit.get("condition")
            if not isinstance(condition_value, str):
                raise ValueError("unit condition is invalid")
            references = _read_object(
                session_dir
                / "units"
                / f"unit-{int(unit_index_value):02d}-{condition_value}"
                / "unit.json"
            )["runs"]
            assert isinstance(references, Mapping)
            for relative in references.values():
                run = _read_object(session_dir / str(relative) / "run.json")
                adapter = run.get("adapter")
                assert isinstance(adapter, Mapping)
                run_starts.append(int(adapter["started_monotonic_ns"]))
                run_finishes.append(int(adapter["finished_monotonic_ns"]))
        sample_times = [int(sample["sample_monotonic_ns"]) for sample in resources]
        if min(sample_times) > min(run_starts) or max(sample_times) < max(run_finishes):
            raise ValueError(
                f"session {session_index} resource trace lacks run coverage"
            )
        sessions.append(
            {
                "session": f"session-{session_index:02d}",
                "interposer_order": list(expected_order),
                "runner_commit": preflight_summary.get("runner_commit"),
                "units": units,
            }
        )
    commits = {session["runner_commit"] for session in sessions}
    if len(commits) != 1:
        raise ValueError("diagnostic sessions do not share one runner commit")
    return {
        "carryover_analysis_schema_version": CARRYOVER_ANALYSIS_SCHEMA_VERSION,
        "analysis_kind": CARRYOVER_ANALYSIS_KIND,
        "protocol": {
            "id": CARRYOVER_PROTOCOL_ID,
            "sha256": protocol_sha256(protocol),
        },
        "source": {
            "collection_id": directory.name,
            "runner_commit": next(iter(commits)),
            "source_paths_recorded": False,
        },
        "validation": {
            "session_count": len(sessions),
            "unit_count": len(sessions) * 3,
            "all_units_valid": True,
            "all_invocation_gates_pass": True,
            "fixed_intervals_valid": True,
            "service_restarts_valid": True,
            "resource_sample_count": resource_sample_count,
            "maximum_tj_c": maximum_tj_c,
        },
        "sessions": sessions,
        "contrasts": {
            "vlm_vs_idle": _contrast(sessions, "vlm"),
            "llm_vs_idle": _contrast(sessions, "llm"),
        },
        "claim_boundary": {
            "exploratory_only": True,
            "formal_evidence": False,
            "g6_v4_reopened": False,
            "formal_pass_fail_emitted": False,
            "phase2_authorized": False,
        },
    }


def render_markdown(analysis: Mapping[str, object]) -> str:
    validation = analysis["validation"]
    contrasts = analysis["contrasts"]
    source = analysis["source"]
    protocol = analysis["protocol"]
    assert isinstance(validation, Mapping)
    assert isinstance(contrasts, Mapping)
    assert isinstance(source, Mapping)
    assert isinstance(protocol, Mapping)
    rows: list[str] = []
    for name in ("vlm_vs_idle", "llm_vs_idle"):
        contrast = contrasts[name]
        assert isinstance(contrast, Mapping)
        duration = contrast["duration_ratio"]
        residency = contrast["resident_fraction_difference"]
        minor = contrast["minor_fault_difference"]
        major = contrast["major_fault_difference"]
        assert all(
            isinstance(item, Mapping)
            for item in (duration, residency, minor, major)
        )
        rows.append(
            "| `{}` | {:.4f} | {:+.6f} | {:+.1f} | {:+.1f} |".format(
                name.replace("_vs_idle", ""),
                float(duration["geometric_mean"]),
                float(residency["mean"]),
                float(minor["mean"]),
                float(major["mean"]),
            )
        )
    return "\n".join(
        [
            "# Phase 1 ASR/VLM Carryover Diagnostic",
            "",
            "This report validates a motion-disabled exploratory diagnostic. It is not a formal G6 comparison.",
            "",
            "## Provenance and validation",
            "",
            f"- Collection: `{source.get('collection_id')}`",
            f"- Runner commit: `{source.get('runner_commit')}`",
            f"- Protocol: `{protocol.get('id')}` (`{protocol.get('sha256')}`)",
            f"- Sessions / units: {validation.get('session_count')} / {validation.get('unit_count')}",
            f"- Resource samples: {validation.get('resource_sample_count')}",
            f"- Maximum Tj: {_number(validation.get('maximum_tj_c'), 'maximum Tj'):.3f} C",
            "",
            "## Paired descriptive contrasts",
            "",
            "| Interposer | ASR duration ratio vs idle | Post-interposer resident fraction difference | Minor-fault difference | Major-fault difference |",
            "| --- | ---: | ---: | ---: | ---: |",
            *rows,
            "",
            "The duration ratio is the geometric mean of the six within-session ratios. Other columns are means of within-session differences.",
            "",
            "## Claim boundary",
            "",
            "These contrasts isolate carryover under the fixed inputs and target configuration. They do not reopen or reclassify G6 v4, do not emit a formal pass/fail decision, and do not authorize Phase 2.",
            "",
        ]
    )


def _write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8", newline="\n")
    os.replace(temporary, path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("collection_dir", type=Path)
    parser.add_argument("--json-output", type=Path)
    parser.add_argument("--markdown-output", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        analysis = analyze_collection(args.collection_dir)
        json_text = (
            json.dumps(
                analysis,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                indent=2,
            )
            + "\n"
        )
        markdown = render_markdown(analysis)
        if args.json_output is not None:
            _write_text_atomic(args.json_output, json_text)
        if args.markdown_output is not None:
            _write_text_atomic(args.markdown_output, markdown)
        if args.json_output is None and args.markdown_output is None:
            print(json_text, end="")
    except BaseException as exc:
        print(
            f"Phase 1 carryover analysis failed: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
