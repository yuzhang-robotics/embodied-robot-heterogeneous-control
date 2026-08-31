"""Validate one complete fixed-input Phase 1 ASR run directory."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any, Mapping

from experiments.phase1.asr_adapter import (
    ASR_EXPECTED_OUTPUT_LENGTH,
    ASR_EXPECTED_OUTPUT_SHA256,
    ASR_INPUT_MEDIA_TYPE,
    ASR_INPUT_SHA256,
    ASR_INPUT_SIZE_BYTES,
    ASR_MODEL_SHA256,
    ASR_MODEL_SIZE_BYTES,
    ASR_WHISPER_ARGUMENTS,
    ASR_WHISPER_SOURCE_VERSION,
)
from experiments.phase1.asr_preflight import asr_preflight_errors
from experiments.phase1.asr_slice import ASRSliceCondition
from experiments.phase1.jetson_telemetry import (
    load_resource_samples,
    validate_resource_samples,
)
from experiments.phase1.manifest import MANIFEST_SCHEMA_VERSION, sha256_file
from experiments.phase1.summarize_asr_slice import (
    ASR_SUMMARY_SCHEMA_VERSION,
    build_asr_summary,
)
from experiments.phase1.telemetry import SCHEMA_VERSION as EVENT_SCHEMA_VERSION


REQUIRED_FILES = (
    "manifest.json",
    "preflight.json",
    "events.jsonl",
    "resources.jsonl",
    "scenario.json",
    "summary.json",
)
_REPORT_KEYS = {
    "condition",
    "task_id",
    "state_advanced",
    "consumed",
    "final_disposition",
    "adapter",
    "shutdown",
    "probe",
    "final_snapshot",
}
_ADAPTER_KEYS = {
    "task_id",
    "worker_thread_id",
    "started_monotonic_ns",
    "finished_monotonic_ns",
    "duration_ns",
    "execution_outcome",
    "error_code",
    "input",
    "output",
    "process",
    "cancellation",
}
_PROCESS_KEYS = {
    "started",
    "exit_code",
    "terminate_requested",
    "terminate_confirmed",
    "kill_requested",
    "kill_confirmed",
    "reaped",
}


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


def _check_artifacts(
    directory: Path,
    artifacts: object,
    errors: list[str],
) -> None:
    if not isinstance(artifacts, Mapping):
        errors.append("manifest artifact identities are missing")
        return
    expected = set(REQUIRED_FILES) - {"manifest.json"}
    if set(artifacts) != expected:
        errors.append("manifest artifact identity set is incomplete or unsupported")
    for name in sorted(expected):
        identity = artifacts.get(name)
        path = directory / name
        if not isinstance(identity, Mapping):
            errors.append(f"manifest identity is missing for {name}")
            continue
        if identity.get("size_bytes") != path.stat().st_size:
            errors.append(f"manifest size does not match {name}")
        if identity.get("sha256") != sha256_file(path):
            errors.append(f"manifest SHA-256 does not match {name}")


def validate_asr_slice_dir(run_dir: Path | str) -> list[str]:
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
    preflight = _read_object(directory / "preflight.json", errors)
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
    if manifest.get("artifact_kind") != "phase1_fixed_input_asr_run":
        errors.append("manifest artifact kind is not a fixed-input ASR run")
    if manifest.get("adapter_isolation") != "whisper_subprocess":
        errors.append("manifest ASR adapter isolation is inconsistent")
    if manifest.get("trace_profile") != "runtime_threaded_probe":
        errors.append("manifest trace profile is not runtime_threaded_probe")
    if manifest.get("status") != "completed":
        errors.append(f"manifest status is not completed: {manifest.get('status')!r}")
    if manifest.get("descriptive_only") is not True:
        errors.append("manifest is not marked descriptive-only")
    if manifest.get("formal_performance_claim_permitted") is not False:
        errors.append("manifest incorrectly permits a formal performance claim")
    if manifest.get("heterogeneous_inference_claim_permitted") is not False:
        errors.append("manifest incorrectly permits a heterogeneous inference claim")

    environment = manifest.get("environment")
    git = environment.get("git") if isinstance(environment, Mapping) else None
    reproducibility = manifest.get("reproducibility")
    if not isinstance(reproducibility, Mapping):
        errors.append("manifest reproducibility record is missing")
    else:
        synchronized_main = (
            isinstance(git, Mapping)
            and not git.get("error_codes")
            and git.get("dirty") is False
            and git.get("branch") == "main"
            and git.get("upstream") == "origin/main"
            and git.get("upstream_commit") == git.get("commit")
            and str(git.get("ahead_behind", "")).split() == ["0", "0"]
        )
        if reproducibility.get("synchronized_main") is not synchronized_main:
            errors.append("manifest synchronized-main fact is inconsistent")
        development_injection = reproducibility.get("development_injection")
        injected_components = reproducibility.get("injected_components")
        if not isinstance(development_injection, bool):
            errors.append("development injection fact must be boolean")
        if not isinstance(injected_components, list) or any(
            item
            not in {
                "preflight_builder",
                "sampler_factory",
                "adapter_factory",
            }
            for item in injected_components
        ):
            errors.append("injected component list is invalid")
        elif development_injection is not bool(injected_components):
            errors.append("development injection facts are inconsistent")
        if reproducibility.get("formal_evidence_eligible") is not False:
            errors.append("ASR pilot must remain ineligible for a formal claim")

    safety = manifest.get("safety")
    if not isinstance(safety, Mapping):
        errors.append("manifest safety record is missing")
    elif (
        safety.get("motion_enabled") is not False
        or safety.get("motion_value_valid") is not True
    ):
        errors.append("manifest does not prove physical motion was disabled")

    if manifest.get("input") != {
        "sha256": ASR_INPUT_SHA256,
        "size_bytes": ASR_INPUT_SIZE_BYTES,
        "media_type": ASR_INPUT_MEDIA_TYPE,
        "path_recorded": False,
    }:
        errors.append("manifest input identity does not match the fixed ASR WAV")

    errors.extend(f"preflight: {error}" for error in asr_preflight_errors(preflight))
    base = preflight.get("base")
    base_environment = base.get("environment") if isinstance(base, Mapping) else None
    base_git = (
        base_environment.get("git") if isinstance(base_environment, Mapping) else None
    )
    if isinstance(git, Mapping) and isinstance(base_git, Mapping):
        for key in (
            "commit",
            "branch",
            "dirty",
            "upstream",
            "upstream_commit",
            "ahead_behind",
        ):
            if git.get(key) != base_git.get(key):
                errors.append(f"manifest and preflight Git {key} differ")

    expected_contract = {
        "source": "whisper_cli_subprocess",
        "model": {
            "sha256": ASR_MODEL_SHA256,
            "size_bytes": ASR_MODEL_SIZE_BYTES,
        },
        "source_version": ASR_WHISPER_SOURCE_VERSION,
        "arguments": list(ASR_WHISPER_ARGUMENTS),
        "residency_policy": "loads_model_per_invocation",
        "expected_output": {
            "sha256": ASR_EXPECTED_OUTPUT_SHA256,
            "length": ASR_EXPECTED_OUTPUT_LENGTH,
        },
        "raw_output_recorded": False,
    }
    if manifest.get("workload_contract") != expected_contract:
        errors.append("manifest workload contract differs from Phase 0")

    try:
        condition = ASRSliceCondition(manifest.get("condition"))
    except ValueError:
        errors.append("manifest ASR condition is not supported")
        return errors
    spec = manifest.get("spec")
    report = scenario.get("report")
    if spec != scenario.get("spec"):
        errors.append("manifest and scenario specifications differ")
    if not isinstance(spec, Mapping):
        errors.append("manifest ASR specification is missing")
    elif (
        spec.get("condition") != condition.value
        or spec.get("task_kind") != "asr"
        or spec.get("request_count") != 1
        or spec.get("pending_capacity") != 2
        or spec.get("result_capacity") != 2
        or spec.get("queue_semantics") != "utterance_fifo"
    ):
        errors.append("manifest ASR specification is inconsistent")
    else:
        stale_observation_s = spec.get("stale_observation_s")
        if (
            isinstance(stale_observation_s, bool)
            or not isinstance(stale_observation_s, (int, float))
            or not math.isfinite(stale_observation_s)
            or stale_observation_s <= 0
        ):
            errors.append("manifest stale observation window is invalid")
        resource_interval_ms = manifest.get("resource_interval_ms")
        if (
            isinstance(resource_interval_ms, bool)
            or not isinstance(resource_interval_ms, int)
            or resource_interval_ms < 50
            or resource_interval_ms > 10_000
        ):
            errors.append("manifest resource interval is invalid")
        elif (
            condition is ASRSliceCondition.STALE
            and isinstance(stale_observation_s, (int, float))
            and not isinstance(stale_observation_s, bool)
            and stale_observation_s <= resource_interval_ms / 1000.0
        ):
            errors.append("stale observation window does not exceed resource interval")

    if not isinstance(report, Mapping):
        errors.append("scenario ASR report is missing")
    elif report.get("condition") != condition.value:
        errors.append("scenario condition does not match the manifest")
    else:
        if set(report) != _REPORT_KEYS:
            errors.append("scenario ASR report fields are incomplete or unsupported")
        adapter = report.get("adapter")
        if not isinstance(adapter, Mapping) or set(adapter) != _ADAPTER_KEYS:
            errors.append("scenario ASR adapter fields are incomplete or unsupported")
        else:
            adapter_input = adapter.get("input")
            adapter_output = adapter.get("output")
            process = adapter.get("process")
            cancellation = adapter.get("cancellation")
            if not isinstance(adapter_input, Mapping) or set(adapter_input) != {
                "sha256",
                "size_bytes",
                "media_type",
            }:
                errors.append("scenario adapter input fields are unsupported")
            if adapter_output is not None and (
                not isinstance(adapter_output, Mapping)
                or set(adapter_output) != {"sha256", "length", "raw_text_recorded"}
            ):
                errors.append("scenario adapter output fields are unsupported")
            if not isinstance(process, Mapping) or set(process) != _PROCESS_KEYS:
                errors.append("scenario process fields are unsupported")
            if not isinstance(cancellation, Mapping) or set(cancellation) != {
                "requested",
                "worker_observed",
                "client_wait_stopped",
                "backend_stop_confirmed",
            }:
                errors.append("scenario cancellation fields are unsupported")

    _check_artifacts(directory, manifest.get("artifacts"), errors)
    try:
        samples = load_resource_samples(directory / "resources.jsonl")
    except (OSError, UnicodeError, ValueError) as exc:
        errors.append(f"resources.jsonl: {type(exc).__name__}: {exc}")
        samples = []
    else:
        errors.extend(validate_resource_samples(samples))

    sampler_report = manifest.get("resource_sampler_report")
    if not isinstance(sampler_report, Mapping):
        errors.append("manifest resource sampler report is missing")
        sampler_report = {}
    elif sampler_report.get("successful") is not True:
        errors.append("manifest resource sampler did not close successfully")

    if summary.get("asr_summary_schema_version") != ASR_SUMMARY_SCHEMA_VERSION:
        errors.append("unsupported ASR summary schema version")
    if summary.get("run_id") != run_id:
        errors.append("summary run_id does not match the manifest")
    if summary.get("condition") != condition.value:
        errors.append("summary condition does not match the manifest")
    if summary.get("descriptive_only") is not True:
        errors.append("summary is not marked descriptive-only")
    expected_injection = (
        reproducibility.get("development_injection")
        if isinstance(reproducibility, Mapping)
        else None
    )
    if summary.get("development_injection") is not expected_injection:
        errors.append("summary development injection fact is inconsistent")
    if (
        expected_injection is True
        and summary.get("real_asr_path_executed") is not False
    ):
        errors.append("development injection cannot claim real ASR execution")
    if summary.get("formal_performance_claim_permitted") is not False:
        errors.append("summary incorrectly permits a formal performance claim")
    if summary.get("heterogeneous_inference_claim_permitted") is not False:
        errors.append("summary incorrectly permits a heterogeneous inference claim")
    if summary.get("valid") is not True:
        errors.append("summary Gates did not all pass")
    gates = summary.get("gates")
    if not isinstance(gates, list) or not gates:
        errors.append("summary contains no Gates")
    elif any(
        not isinstance(gate, Mapping) or gate.get("passed") is not True
        for gate in gates
    ):
        errors.append("one or more ASR summary Gates failed")

    if isinstance(spec, Mapping) and isinstance(report, Mapping) and samples:
        try:
            rebuilt = build_asr_summary(
                directory / "events.jsonl",
                condition=condition,
                spec=spec,
                report=report,
                resource_samples=samples,
                sampler_report=sampler_report,
                development_injection=bool(expected_injection),
            )
        except (OSError, TypeError, ValueError) as exc:
            errors.append(f"summary rebuild failed: {type(exc).__name__}: {exc}")
        else:
            if summary != _json_value(rebuilt):
                errors.append("summary does not match independently rebuilt metrics")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    args = parser.parse_args(argv)
    errors = validate_asr_slice_dir(args.run_dir)
    if errors:
        print("INVALID")
        for error in errors:
            print(f"- {error}")
        return 1
    print("VALID")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
