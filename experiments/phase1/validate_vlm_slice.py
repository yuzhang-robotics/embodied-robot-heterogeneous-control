"""Validate one complete fixed-input Phase 1 VLM run directory."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any, Mapping

from experiments.phase1.jetson_telemetry import (
    load_resource_samples,
    validate_resource_samples,
)
from experiments.phase1.manifest import MANIFEST_SCHEMA_VERSION, sha256_file
from experiments.phase1.summarize_vlm_slice import (
    LEGACY_VLM_SUMMARY_SCHEMA_VERSION,
    VLM_SUMMARY_SCHEMA_VERSION,
    build_vlm_summary,
)
from experiments.phase1.summarize_vlm_process_slice import (
    LEGACY_VLM_PROCESS_SUMMARY_SCHEMA_VERSION,
    VLM_PROCESS_ISOLATION,
    VLM_PROCESS_SUMMARY_SCHEMA_VERSION,
    build_vlm_process_summary,
)
from experiments.phase1.telemetry import SCHEMA_VERSION as EVENT_SCHEMA_VERSION
from experiments.phase1.vlm_adapter import (
    C100_INPUT_MEDIA_TYPE,
    C100_INPUT_SHA256,
    C100_INPUT_SIZE_BYTES,
)
from experiments.phase1.vlm_preflight import vlm_preflight_errors
from experiments.phase1.vlm_process_adapter import PROCESS_PROTOCOL_VERSION
from experiments.phase1.vlm_slice import VLMSliceCondition
from jetson.vlm_request_contract import current_vlm_workload_contract


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
    "translation_route",
    "model_residency",
    "stage_durations_ns",
    "stage_status",
    "stage_error_codes",
    "cancellation",
}
_LEGACY_ADAPTER_KEYS = _ADAPTER_KEYS - {"stage_error_codes"}
_PROCESS_REPORT_KEYS = {
    "protocol_version",
    "start_method",
    "process_name",
    "process_id",
    "spawn_requested_monotonic_ns",
    "child_started_monotonic_ns",
    "inference_started_monotonic_ns",
    "completion_received_monotonic_ns",
    "joined_monotonic_ns",
    "exit_code",
    "cancellation_forwarded",
    "cancellation_forwarded_monotonic_ns",
    "terminate_requested",
    "terminate_confirmed",
    "protocol_complete",
    "error_code",
}


def _legacy_workload_contract_valid(
    contract: Mapping[str, object],
    *,
    legacy_schema: bool,
) -> bool:
    common_valid = (
        contract.get("request_contract_version") is None
        and contract.get("source") == "jetson.vision_vlm"
        and contract.get("translation_fallback") == "argos_en_zh"
        and contract.get("unload_confirmation") == "not_available"
        and contract.get("raw_output_recorded") is False
        and contract.get("moondream")
        == {
            "temperature": 0.1,
            "num_predict": 100,
            "request_timeout_s": 180,
        }
        and contract.get("qwen_rewrite")
        == {
            "temperature": 0.2,
            "max_tokens": 96,
            "request_timeout_s": 30,
        }
    )
    residency_valid = (
        contract.get("unload_after_request") is True
        and "unload_before_qwen" not in contract
        and "cleanup_unload_on_failure" not in contract
        if legacy_schema
        else contract.get("unload_before_qwen") is True
        and contract.get("cleanup_unload_on_failure") is True
        and "unload_after_request" not in contract
    )
    return common_valid and residency_valid


_ERROR_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


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
    *,
    required_files: tuple[str, ...] = REQUIRED_FILES,
) -> None:
    if not isinstance(artifacts, Mapping):
        errors.append("manifest artifact identities are missing")
        return
    expected = set(required_files) - {"manifest.json"}
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


def validate_vlm_slice_dir(run_dir: Path | str) -> list[str]:
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

    summary_schema = summary.get("vlm_summary_schema_version")
    legacy_schema = summary_schema == LEGACY_VLM_SUMMARY_SCHEMA_VERSION
    if summary_schema not in {
        LEGACY_VLM_SUMMARY_SCHEMA_VERSION,
        VLM_SUMMARY_SCHEMA_VERSION,
    }:
        errors.append("unsupported VLM summary schema version")

    artifact_kind = manifest.get("artifact_kind")
    process_isolated = artifact_kind == "phase1_fixed_input_vlm_process_run"
    required_files = (
        REQUIRED_FILES + ("process.json",) if process_isolated else REQUIRED_FILES
    )
    process_summary: dict[str, Any] = {}
    if process_isolated:
        process_path = directory / "process.json"
        if not process_path.is_file():
            errors.append("missing file: process.json")
        else:
            process_summary = _read_object(process_path, errors)
    if errors:
        return errors

    run_id = manifest.get("run_id")
    if not isinstance(run_id, str) or run_id != directory.name:
        errors.append("manifest run_id does not match the run directory")
    if manifest.get("manifest_schema_version") != MANIFEST_SCHEMA_VERSION:
        errors.append("unsupported manifest schema version")
    if manifest.get("event_schema_version") != EVENT_SCHEMA_VERSION:
        errors.append("unsupported event schema version")
    if artifact_kind not in {
        "phase1_fixed_input_vlm_run",
        "phase1_fixed_input_vlm_process_run",
    }:
        errors.append("manifest artifact kind is not a fixed-input VLM run")
    adapter_isolation = manifest.get("adapter_isolation")
    if process_isolated:
        if adapter_isolation != VLM_PROCESS_ISOLATION:
            errors.append("manifest process adapter isolation is inconsistent")
    elif adapter_isolation not in {None, "thread"}:
        errors.append("manifest thread adapter isolation is inconsistent")
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
            errors.append("VLM pilot must remain ineligible for a formal claim")

    safety = manifest.get("safety")
    if not isinstance(safety, Mapping):
        errors.append("manifest safety record is missing")
    elif (
        safety.get("motion_enabled") is not False
        or safety.get("motion_value_valid") is not True
    ):
        errors.append("manifest does not prove physical motion was disabled")

    input_identity = manifest.get("input")
    if input_identity != {
        "sha256": C100_INPUT_SHA256,
        "size_bytes": C100_INPUT_SIZE_BYTES,
        "media_type": C100_INPUT_MEDIA_TYPE,
        "path_recorded": False,
    }:
        errors.append("manifest input identity does not match the fixed C100 image")

    errors.extend(f"preflight: {error}" for error in vlm_preflight_errors(preflight))
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

    workload_contract = manifest.get("workload_contract")
    unload_confirmation_required = False
    if not isinstance(workload_contract, Mapping):
        errors.append("manifest workload contract is missing")
    else:
        unload_confirmation_required = (
            workload_contract.get("request_contract_version") is not None
        )
        contract_valid = (
            _legacy_workload_contract_valid(
                workload_contract,
                legacy_schema=legacy_schema,
            )
            if workload_contract.get("request_contract_version") is None
            else dict(workload_contract) == current_vlm_workload_contract()
        )
        if not contract_valid:
            errors.append("manifest VLM workload contract is unsupported")
    try:
        condition = VLMSliceCondition(manifest.get("condition"))
    except ValueError:
        errors.append("manifest VLM condition is not supported")
        return errors
    spec = manifest.get("spec")
    report = scenario.get("report")
    process_report = scenario.get("process")
    if spec != scenario.get("spec"):
        errors.append("manifest and scenario specifications differ")
    if not isinstance(spec, Mapping):
        errors.append("manifest VLM specification is missing")
    elif (
        spec.get("condition") != condition.value
        or spec.get("task_kind") != "vlm"
        or spec.get("request_count") != 1
    ):
        errors.append("manifest VLM specification is inconsistent")
    elif process_isolated and spec.get("adapter_isolation") != VLM_PROCESS_ISOLATION:
        errors.append("process specification isolation is inconsistent")
    elif not process_isolated and spec.get("adapter_isolation") not in {
        None,
        "thread",
    }:
        errors.append("thread specification isolation is inconsistent")
    if process_isolated:
        if not isinstance(process_report, Mapping):
            errors.append("scenario process report is missing")
        elif set(process_report) != _PROCESS_REPORT_KEYS:
            errors.append("scenario process report fields are unsupported")
    elif process_report is not None:
        errors.append("thread scenario unexpectedly contains a process report")
    if not isinstance(report, Mapping):
        errors.append("scenario VLM report is missing")
    elif report.get("condition") != condition.value:
        errors.append("scenario condition does not match the manifest")
    else:
        if set(report) != _REPORT_KEYS:
            errors.append("scenario VLM report fields are incomplete or unsupported")
        adapter = report.get("adapter")
        expected_adapter_keys = _LEGACY_ADAPTER_KEYS if legacy_schema else _ADAPTER_KEYS
        if not isinstance(adapter, Mapping) or set(adapter) != expected_adapter_keys:
            errors.append("scenario adapter fields are incomplete or unsupported")
        else:
            adapter_input = adapter.get("input")
            adapter_output = adapter.get("output")
            cancellation = adapter.get("cancellation")
            model_residency = adapter.get("model_residency")
            stage_durations = adapter.get("stage_durations_ns")
            stage_status = adapter.get("stage_status")
            stage_error_codes = adapter.get("stage_error_codes")
            if not isinstance(adapter_input, Mapping) or set(adapter_input) != {
                "sha256",
                "size_bytes",
                "media_type",
            }:
                errors.append("scenario adapter input fields are unsupported")
            if not isinstance(adapter_output, Mapping) or set(adapter_output) != {
                "sha256",
                "length",
                "raw_text_recorded",
            }:
                errors.append("scenario adapter output fields are unsupported")
            if not isinstance(cancellation, Mapping) or set(cancellation) != {
                "requested",
                "worker_observed",
                "client_wait_stopped",
                "backend_stop_confirmed",
            }:
                errors.append("scenario cancellation fields are unsupported")
            if not isinstance(model_residency, Mapping) or set(model_residency) != {
                "unload_requested",
                "unload_confirmed",
            }:
                errors.append("scenario model-residency fields are unsupported")
            elif unload_confirmation_required and (
                model_residency.get("unload_requested") is not True
                or model_residency.get("unload_confirmed") is not True
            ):
                errors.append("scenario does not confirm VLM model unload")
            if not isinstance(stage_durations, Mapping) or any(
                not isinstance(name, str)
                or isinstance(duration, bool)
                or not isinstance(duration, int)
                or duration < 0
                for name, duration in stage_durations.items()
            ):
                errors.append("scenario stage durations are unsupported")
            if not isinstance(stage_status, Mapping) or any(
                not isinstance(name, str) or status not in {"ok", "error"}
                for name, status in stage_status.items()
            ):
                errors.append("scenario stage statuses are unsupported")
            if not legacy_schema:
                if not isinstance(stage_error_codes, Mapping) or any(
                    not isinstance(name, str)
                    or not isinstance(code, str)
                    or _ERROR_CODE_RE.fullmatch(code) is None
                    for name, code in stage_error_codes.items()
                ):
                    errors.append("scenario stage error codes are unsupported")
                elif isinstance(stage_status, Mapping) and set(stage_error_codes) != {
                    name for name, status in stage_status.items() if status == "error"
                }:
                    errors.append("scenario stage error codes are inconsistent")

    _check_artifacts(
        directory,
        manifest.get("artifacts"),
        errors,
        required_files=required_files,
    )
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
        and summary.get("real_vlm_path_executed") is not False
    ):
        errors.append("development injection cannot claim real VLM execution")
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
        errors.append("one or more VLM summary Gates failed")

    if process_isolated:
        expected_process_summary_schema = (
            LEGACY_VLM_PROCESS_SUMMARY_SCHEMA_VERSION
            if legacy_schema
            else VLM_PROCESS_SUMMARY_SCHEMA_VERSION
        )
        expected_process_protocol = (
            "0.1.0" if legacy_schema else PROCESS_PROTOCOL_VERSION
        )
        if (
            process_summary.get("vlm_process_summary_schema_version")
            != expected_process_summary_schema
        ):
            errors.append("unsupported VLM process summary schema version")
        if process_summary.get("adapter_isolation") != VLM_PROCESS_ISOLATION:
            errors.append("process summary isolation is inconsistent")
        if process_summary.get("condition") != condition.value:
            errors.append("process summary condition is inconsistent")
        if process_summary.get("valid") is not True:
            errors.append("process summary Gates did not all pass")
        process_gates = process_summary.get("gates")
        if not isinstance(process_gates, list) or not process_gates:
            errors.append("process summary contains no Gates")
        elif any(
            not isinstance(gate, Mapping) or gate.get("passed") is not True
            for gate in process_gates
        ):
            errors.append("one or more process summary Gates failed")
        if isinstance(process_report, Mapping):
            try:
                rebuilt_process = build_vlm_process_summary(
                    process_report,
                    condition=condition,
                    protocol_version=expected_process_protocol,
                    schema_version=expected_process_summary_schema,
                )
            except (TypeError, ValueError) as exc:
                errors.append(
                    "process summary rebuild failed: " f"{type(exc).__name__}: {exc}"
                )
            else:
                if process_summary != _json_value(rebuilt_process):
                    errors.append(
                        "process summary does not match independently rebuilt Gates"
                    )

    if isinstance(spec, Mapping) and isinstance(report, Mapping) and samples:
        try:
            rebuilt = build_vlm_summary(
                directory / "events.jsonl",
                condition=condition,
                spec=spec,
                report=report,
                resource_samples=samples,
                sampler_report=sampler_report,
                development_injection=bool(expected_injection),
                schema_version=str(summary_schema),
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
    errors = validate_vlm_slice_dir(args.run_dir)
    if errors:
        print("INVALID")
        for error in errors:
            print(f"- {error}")
        return 1
    print("VALID")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
