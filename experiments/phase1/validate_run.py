"""Validate one complete Phase 1 simulated-load run directory."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from experiments.phase1.manifest import MANIFEST_SCHEMA_VERSION, sha256_file
from experiments.phase1.replay_lifecycle import ReplayError, TraceProfile, replay_file
from experiments.phase1.simulation import SimulationCondition
from experiments.phase1.summarize_run import SUMMARY_SCHEMA_VERSION, build_summary
from experiments.phase1.telemetry import SCHEMA_VERSION as EVENT_SCHEMA_VERSION


REQUIRED_FILES = ("manifest.json", "scenario.json", "events.jsonl", "summary.json")


def _read_object(path: Path, errors: list[str]) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        errors.append(f"{path.name}: {type(exc).__name__}: {exc}")
        return {}
    if not isinstance(value, dict):
        errors.append(f"{path.name}: root must be an object")
        return {}
    return value


def _json_value(value: object) -> object:
    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))


def validate_run_dir(run_dir: Path | str) -> list[str]:
    directory = Path(run_dir)
    errors: list[str] = []
    for name in REQUIRED_FILES:
        if not (directory / name).is_file():
            errors.append(f"missing file: {name}")
    if list(directory.glob("*.tmp")):
        errors.append("run directory contains an unfinished temporary file")
    if errors:
        return errors

    manifest = _read_object(directory / "manifest.json", errors)
    scenario = _read_object(directory / "scenario.json", errors)
    summary = _read_object(directory / "summary.json", errors)
    if errors:
        return errors

    run_id = manifest.get("run_id")
    if not isinstance(run_id, str) or run_id != directory.name:
        errors.append("manifest run_id does not match the run directory")
    if manifest.get("manifest_schema_version") != MANIFEST_SCHEMA_VERSION:
        errors.append("unsupported manifest schema version")
    if manifest.get("event_schema_version") != EVENT_SCHEMA_VERSION:
        errors.append("unsupported event schema version")
    if manifest.get("artifact_kind") != "phase1_simulation_run":
        errors.append("manifest artifact kind is not a Phase 1 simulation run")
    if manifest.get("status") != "completed":
        errors.append(f"manifest status is not completed: {manifest.get('status')!r}")

    safety = manifest.get("safety")
    if not isinstance(safety, dict):
        errors.append("manifest safety record is missing")
    else:
        if safety.get("motion_enabled") is not False:
            errors.append("manifest does not prove motion was disabled")
        if safety.get("motion_value_valid") is not True:
            errors.append("manifest contains an unrecognized motion setting")

    environment = manifest.get("environment")
    git = environment.get("git") if isinstance(environment, dict) else None
    reproducibility = manifest.get("reproducibility")
    if not isinstance(reproducibility, dict):
        errors.append("manifest reproducibility record is missing")
    else:
        identity_complete = (
            isinstance(git, dict)
            and not git.get("error_codes")
            and bool(git.get("commit"))
            and bool(git.get("branch"))
        )
        git_clean = identity_complete and git.get("dirty") is False
        development_override = reproducibility.get("development_override")
        expected_eligibility = git_clean and development_override is False
        if reproducibility.get("git_identity_complete") is not identity_complete:
            errors.append("reproducibility Git identity fact is inconsistent")
        if reproducibility.get("git_clean") is not git_clean:
            errors.append("reproducibility Git cleanliness fact is inconsistent")
        if not isinstance(development_override, bool):
            errors.append("reproducibility development override must be boolean")
        if reproducibility.get("formal_evidence_eligible") is not expected_eligibility:
            errors.append("formal-evidence eligibility is inconsistent")

    try:
        condition = SimulationCondition(manifest.get("condition"))
    except ValueError:
        errors.append("manifest condition is not supported")
        return errors
    if manifest.get("trace_profile") != condition.trace_profile.value:
        errors.append("manifest trace profile does not match its condition")
    spec = manifest.get("spec")
    if spec != scenario.get("spec"):
        errors.append("manifest and scenario specifications differ")
    if not isinstance(spec, dict):
        errors.append("manifest scenario specification is missing")
    else:
        if spec.get("condition") != condition.value:
            errors.append(
                "scenario specification condition does not match the manifest"
            )
        if spec.get("trace_profile") != condition.trace_profile.value:
            errors.append(
                "scenario specification trace profile does not match the manifest"
            )
    report = scenario.get("report")
    if not isinstance(report, dict):
        errors.append("scenario report is missing")
    else:
        if report.get("condition") != condition.value:
            errors.append("scenario report condition does not match the manifest")
        if report.get("trace_profile") != condition.trace_profile.value:
            errors.append("scenario report trace profile does not match the manifest")

    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict):
        errors.append("manifest artifact identities are missing")
    else:
        for name in ("events.jsonl", "scenario.json", "summary.json"):
            identity = artifacts.get(name)
            if not isinstance(identity, dict):
                errors.append(f"manifest identity is missing for {name}")
                continue
            path = directory / name
            if identity.get("size_bytes") != path.stat().st_size:
                errors.append(f"manifest size does not match {name}")
            if identity.get("sha256") != sha256_file(path):
                errors.append(f"manifest SHA-256 does not match {name}")

    replay_summary = None
    try:
        replay_summary = replay_file(
            directory / "events.jsonl",
            profile=TraceProfile(manifest["trace_profile"]),
        )
    except (OSError, ReplayError, TypeError, ValueError) as exc:
        errors.append(f"events.jsonl: {type(exc).__name__}: {exc}")

    if summary.get("summary_schema_version") != SUMMARY_SCHEMA_VERSION:
        errors.append("unsupported summary schema version")
    if summary.get("run_id") != run_id:
        errors.append("summary run_id does not match the manifest")
    if summary.get("condition") != condition.value:
        errors.append("summary condition does not match the manifest")
    if summary.get("trace_profile") != condition.trace_profile.value:
        errors.append("summary trace profile does not match the manifest")
    if summary.get("descriptive_only") is not True:
        errors.append("summary is not marked descriptive-only")
    if summary.get("inference_claim_permitted") is not False:
        errors.append("pilot summary incorrectly permits an inferential claim")
    if summary.get("valid") is not True:
        errors.append("summary Gates did not all pass")
    gates = summary.get("gates")
    if not isinstance(gates, list) or not gates:
        errors.append("summary contains no Gates")
    elif any(
        not isinstance(gate, dict) or gate.get("passed") is not True for gate in gates
    ):
        errors.append("one or more summary Gates failed")
    if replay_summary is not None and summary.get("lifecycle") != _json_value(
        asdict(replay_summary)
    ):
        errors.append("summary lifecycle facts do not match independent replay")
    if isinstance(spec, dict) and isinstance(report, dict):
        try:
            rebuilt_summary = build_summary(
                directory / "events.jsonl",
                condition=condition,
                profile=condition.trace_profile,
                scenario_report=report,
                spec=spec,
            )
        except (OSError, ReplayError, TypeError, ValueError) as exc:
            errors.append(f"summary rebuild failed: {type(exc).__name__}: {exc}")
        else:
            if summary != _json_value(rebuilt_summary):
                errors.append("summary does not match independently rebuilt metrics")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    args = parser.parse_args(argv)
    errors = validate_run_dir(args.run_dir)
    if errors:
        print("INVALID")
        for error in errors:
            print(f"- {error}")
        return 1
    print("VALID")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
