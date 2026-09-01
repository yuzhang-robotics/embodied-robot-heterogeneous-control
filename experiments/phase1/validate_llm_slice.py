"""Validate one complete fixed-input Phase 1 LLM run directory."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any, Mapping

from experiments.phase1.jetson_telemetry import (
    load_resource_samples,
    validate_resource_samples,
)
from experiments.phase1.manifest import MANIFEST_SCHEMA_VERSION, sha256_file
from experiments.phase1.telemetry import SCHEMA_VERSION as EVENT_SCHEMA_VERSION

from .llm_adapter import (
    LLM_EMPTY_HISTORY_SHA256,
    LLM_EXPECTED_SERVED_MODEL_ID,
    LLM_INPUT_MEDIA_TYPE,
    LLM_INPUT_SHA256,
    LLM_INPUT_SIZE_BYTES,
    LLM_MODEL_SHA256,
    LLM_MODEL_SIZE_BYTES,
    LLM_SERVER_ARGUMENTS,
    frozen_llm_request_contract,
)
from .llm_preflight import llm_preflight_errors
from .llm_slice import LLMSliceCondition
from .summarize_llm_slice import LLM_SUMMARY_SCHEMA_VERSION, build_llm_summary


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
    "request",
    "response",
    "model_residency",
    "stage_durations_ns",
    "stage_status",
    "cancellation",
}
_EXPECTED_GATES = {
    "single_request",
    "bounded_conversation_lane",
    "expected_disposition",
    "stale_zero_consumed",
    "fixed_prompt_verified",
    "request_contract_verified",
    "llama_request_completed",
    "token_usage_valid",
    "output_private",
    "server_residency_claim_bounded",
    "cancellation_claim_bounded",
    "stale_observation_window",
    "threads_closed",
    "resource_trace_valid",
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


def _validate_report_schema(report: object, errors: list[str]) -> None:
    if not isinstance(report, Mapping):
        errors.append("scenario LLM report is missing")
        return
    if set(report) != _REPORT_KEYS:
        errors.append("scenario LLM report fields are incomplete or unsupported")
    adapter = report.get("adapter")
    if not isinstance(adapter, Mapping) or set(adapter) != _ADAPTER_KEYS:
        errors.append("scenario LLM adapter fields are incomplete or unsupported")
        return
    adapter_input = adapter.get("input")
    adapter_output = adapter.get("output")
    request = adapter.get("request")
    response = adapter.get("response")
    residency = adapter.get("model_residency")
    cancellation = adapter.get("cancellation")
    if not isinstance(adapter_input, Mapping) or set(adapter_input) != {
        "sha256",
        "size_bytes",
        "media_type",
        "raw_text_recorded",
    }:
        errors.append("scenario adapter input fields are unsupported")
    if adapter_output is not None and (
        not isinstance(adapter_output, Mapping)
        or set(adapter_output) != {"sha256", "length", "raw_text_recorded"}
    ):
        errors.append("scenario adapter output fields are unsupported")
    if not isinstance(request, Mapping) or set(request) != {
        "model",
        "temperature",
        "max_tokens",
        "stream",
        "system_prompt",
        "raw_prompt_recorded",
    }:
        errors.append("scenario adapter request fields are unsupported")
    if not isinstance(response, Mapping) or set(response) != {
        "model",
        "usage",
        "raw_response_recorded",
    }:
        errors.append("scenario adapter response fields are unsupported")
    if not isinstance(residency, Mapping) or set(residency) != {
        "policy",
        "server_preexisting",
        "unload_requested",
        "backend_stop_confirmed",
    }:
        errors.append("scenario adapter residency fields are unsupported")
    if not isinstance(cancellation, Mapping) or set(cancellation) != {
        "requested",
        "worker_observed",
        "client_wait_stopped",
        "backend_stop_confirmed",
    }:
        errors.append("scenario adapter cancellation fields are unsupported")


def validate_llm_slice_dir(run_dir: Path | str) -> list[str]:
    directory = Path(run_dir)
    errors: list[str] = []
    if not directory.is_dir():
        return ["run directory does not exist"]
    observed = {path.name for path in directory.iterdir() if path.is_file()}
    if observed != set(REQUIRED_FILES) or any(
        not path.is_file() for path in directory.iterdir()
    ):
        errors.append("run directory file set is incomplete or unsupported")
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
    if manifest.get("artifact_kind") != "phase1_fixed_input_llm_run":
        errors.append("manifest artifact kind is not a fixed-input LLM run")
    if manifest.get("adapter_isolation") != "llama_http_client_thread":
        errors.append("manifest LLM adapter isolation is inconsistent")
    if manifest.get("trace_profile") != "runtime_threaded_probe":
        errors.append("manifest trace profile is not runtime_threaded_probe")
    if manifest.get("status") != "completed":
        errors.append(f"manifest status is not completed: {manifest.get('status')!r}")
    if manifest.get("descriptive_only") is not True:
        errors.append("manifest is not marked descriptive-only")
    for field in (
        "formal_performance_claim_permitted",
        "cancellation_latency_claim_permitted",
        "backend_cancellation_claim_permitted",
        "heterogeneous_inference_claim_permitted",
    ):
        if manifest.get(field) is not False:
            errors.append(f"manifest incorrectly permits {field}")

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
        allowed_injections = {
            "preflight_builder",
            "sampler_factory",
            "adapter_factory",
        }
        if not isinstance(development_injection, bool):
            errors.append("development injection fact must be boolean")
        if not isinstance(injected_components, list) or any(
            item not in allowed_injections for item in injected_components
        ):
            errors.append("injected component list is invalid")
        elif development_injection is not bool(injected_components):
            errors.append("development injection facts are inconsistent")
        if reproducibility.get("formal_evidence_eligible") is not False:
            errors.append("LLM pilot must remain ineligible for a formal claim")

    safety = manifest.get("safety")
    if not isinstance(safety, Mapping):
        errors.append("manifest safety record is missing")
    elif (
        safety.get("motion_enabled") is not False
        or safety.get("motion_value_valid") is not True
    ):
        errors.append("manifest does not prove physical motion was disabled")

    if manifest.get("input") != {
        "sha256": LLM_INPUT_SHA256,
        "size_bytes": LLM_INPUT_SIZE_BYTES,
        "media_type": LLM_INPUT_MEDIA_TYPE,
        "path_recorded": False,
        "raw_text_recorded": False,
    }:
        errors.append("manifest input identity does not match the fixed LLM prompt")

    errors.extend(f"preflight: {error}" for error in llm_preflight_errors(preflight))
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

    runtime = preflight.get("runtime")
    runtime_record = runtime if isinstance(runtime, Mapping) else {}
    expected_contract = {
        "source": "llama_cpp_openai_http",
        "model": {
            "sha256": LLM_MODEL_SHA256,
            "size_bytes": LLM_MODEL_SIZE_BYTES,
            "served_model_id": LLM_EXPECTED_SERVED_MODEL_ID,
        },
        "source_version": runtime_record.get("source_version"),
        "server_arguments": dict(LLM_SERVER_ARGUMENTS),
        "request": frozen_llm_request_contract(),
        "history": {
            "sha256": LLM_EMPTY_HISTORY_SHA256,
            "messages": 0,
            "raw_history_recorded": False,
        },
        "residency_policy": "external_llama_server_resident",
        "raw_prompt_recorded": False,
        "raw_output_recorded": False,
    }
    if manifest.get("workload_contract") != expected_contract:
        errors.append("manifest workload contract differs from Phase 0")

    try:
        condition = LLMSliceCondition(manifest.get("condition"))
    except ValueError:
        errors.append("manifest LLM condition is not supported")
        return errors
    spec = manifest.get("spec")
    report = scenario.get("report")
    if spec != scenario.get("spec"):
        errors.append("manifest and scenario specifications differ")
    if not isinstance(spec, Mapping):
        errors.append("manifest LLM specification is missing")
    elif (
        spec.get("condition") != condition.value
        or spec.get("task_kind") != "llm"
        or spec.get("request_count") != 1
        or spec.get("pending_capacity") != 1
        or spec.get("result_capacity") != 1
        or spec.get("queue_semantics") != "conversation_fifo"
        or spec.get("history_messages") != 0
        or spec.get("history_sha256") != LLM_EMPTY_HISTORY_SHA256
    ):
        errors.append("manifest LLM specification is inconsistent")
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
            condition is LLMSliceCondition.STALE
            and isinstance(stale_observation_s, (int, float))
            and not isinstance(stale_observation_s, bool)
            and stale_observation_s <= resource_interval_ms / 1000.0
        ):
            errors.append("stale observation window does not exceed resource interval")

    _validate_report_schema(report, errors)
    if isinstance(report, Mapping) and report.get("condition") != condition.value:
        errors.append("scenario condition does not match the manifest")

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

    if summary.get("llm_summary_schema_version") != LLM_SUMMARY_SCHEMA_VERSION:
        errors.append("unsupported LLM summary schema version")
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
        and summary.get("real_llm_path_executed") is not False
    ):
        errors.append("development injection cannot claim real LLM execution")
    for field in (
        "formal_performance_claim_permitted",
        "cancellation_latency_claim_permitted",
        "backend_cancellation_claim_permitted",
        "heterogeneous_inference_claim_permitted",
    ):
        if summary.get(field) is not False:
            errors.append(f"summary incorrectly permits {field}")
    if summary.get("valid") is not True:
        errors.append("summary Gates did not all pass")
    gates = summary.get("gates")
    gate_names: set[object] = set()
    if not isinstance(gates, list) or not gates:
        errors.append("summary contains no Gates")
    else:
        for gate in gates:
            if not isinstance(gate, Mapping) or gate.get("passed") is not True:
                errors.append("one or more LLM summary Gates failed")
                break
            gate_names.add(gate.get("name"))
        if gate_names != _EXPECTED_GATES:
            errors.append("summary Gate set is incomplete or unsupported")

    if isinstance(spec, Mapping) and isinstance(report, Mapping) and samples:
        try:
            rebuilt = build_llm_summary(
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
    errors = validate_llm_slice_dir(args.run_dir)
    if errors:
        print("INVALID")
        for error in errors:
            print(f"- {error}")
        return 1
    print("VALID")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
