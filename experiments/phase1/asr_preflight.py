"""Fail-closed Jetson and whisper.cpp checks for the Phase 1 ASR slice."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable, Mapping

from experiments.phase1.asr_adapter import (
    ASR_INPUT_MEDIA_TYPE,
    ASR_INPUT_SHA256,
    ASR_INPUT_SIZE_BYTES,
    ASR_MODEL_SHA256,
    ASR_MODEL_SIZE_BYTES,
    ASR_WHISPER_ARGUMENTS,
    ASR_WHISPER_SOURCE_VERSION,
    ASRRuntime,
    load_phase0_asr_runtime,
)
from experiments.phase1.jetson_preflight import (
    build_jetson_preflight,
    preflight_errors,
)
from experiments.phase1.manifest import command_snapshot, sha256_file
from jetson.phase1_runtime import PayloadRef


ASR_PREFLIGHT_SCHEMA_VERSION = "0.1.0"
_REQUIRED_CHECKS = {
    "fixed_input_identity",
    "whisper_binary_available",
    "whisper_model_identity",
    "whisper_source_version",
    "whisper_arguments_frozen",
    "whisper_process_absent",
}


def _check(
    name: str,
    passed: bool,
    *,
    observed: object,
    requirement: str,
) -> dict[str, object]:
    return {
        "name": name,
        "required": True,
        "passed": passed,
        "observed": observed,
        "requirement": requirement,
    }


def probe_asr_runtime(
    *,
    runtime_loader: Callable[[], ASRRuntime] = load_phase0_asr_runtime,
    snapshotter: Callable[..., Mapping[str, object]] = command_snapshot,
    hasher: Callable[[Path], str] = sha256_file,
) -> dict[str, object]:
    """Collect bounded runtime identity without recording private paths."""

    try:
        runtime = runtime_loader()
    except Exception as exc:
        return {
            "binary_available": False,
            "model_size_bytes": None,
            "model_sha256": None,
            "source_version": None,
            "arguments": list(ASR_WHISPER_ARGUMENTS),
            "process_running": None,
            "error_code": "runtime_" + type(exc).__name__.lower(),
        }
    if not isinstance(runtime, ASRRuntime):
        raise TypeError("runtime_loader must return ASRRuntime")

    binary_available = runtime.whisper_binary.is_file() and os.access(
        runtime.whisper_binary, os.X_OK
    )
    model_size: int | None = None
    model_sha256: str | None = None
    error_code: str | None = None
    if runtime.whisper_model.is_file():
        model_size = runtime.whisper_model.stat().st_size
        try:
            model_sha256 = hasher(runtime.whisper_model)
        except OSError as exc:
            error_code = "model_" + type(exc).__name__.lower()
    else:
        error_code = "model_missing"

    source = snapshotter(
        ["git", "describe", "--tags", "--always", "--dirty"],
        cwd=runtime.whisper_dir,
    )
    process = snapshotter(["pgrep", "-x", "whisper-cli"])
    process_returncode = process.get("returncode")
    process_running = (
        True if process_returncode == 0 else False if process_returncode == 1 else None
    )
    if process_running is None and error_code is None:
        error_code = "process_probe_failed"

    return {
        "binary_available": binary_available,
        "model_size_bytes": model_size,
        "model_sha256": model_sha256,
        "source_version": (
            source.get("output") if source.get("returncode") == 0 else None
        ),
        "arguments": list(ASR_WHISPER_ARGUMENTS),
        "process_running": process_running,
        "error_code": error_code,
    }


def build_asr_preflight(
    repo_root: Path | str,
    *,
    input_payload: PayloadRef,
    expected_branch: str = "main",
    base_preflight: Mapping[str, object] | None = None,
    runtime_status: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Build the complete platform, input and whisper.cpp eligibility record."""

    if not isinstance(input_payload, PayloadRef):
        raise TypeError("input_payload must be a PayloadRef")
    base = dict(
        base_preflight
        if base_preflight is not None
        else build_jetson_preflight(repo_root, expected_branch=expected_branch)
    )
    runtime = dict(
        runtime_status if runtime_status is not None else probe_asr_runtime()
    )
    input_identity = {
        "sha256": input_payload.sha256,
        "size_bytes": input_payload.size_bytes,
        "media_type": input_payload.media_type,
    }
    expected_input = {
        "sha256": ASR_INPUT_SHA256,
        "size_bytes": ASR_INPUT_SIZE_BYTES,
        "media_type": ASR_INPUT_MEDIA_TYPE,
    }
    expected_model = {
        "sha256": ASR_MODEL_SHA256,
        "size_bytes": ASR_MODEL_SIZE_BYTES,
    }
    observed_model = {
        "sha256": runtime.get("model_sha256"),
        "size_bytes": runtime.get("model_size_bytes"),
    }
    checks = [
        _check(
            "fixed_input_identity",
            input_identity == expected_input,
            observed=input_identity,
            requirement="input matches the frozen Phase 0 ASR WAV",
        ),
        _check(
            "whisper_binary_available",
            runtime.get("binary_available") is True,
            observed=runtime.get("binary_available"),
            requirement="the configured whisper-cli binary exists and is executable",
        ),
        _check(
            "whisper_model_identity",
            observed_model == expected_model,
            observed=observed_model,
            requirement="the Whisper model matches the Phase 0 identity",
        ),
        _check(
            "whisper_source_version",
            runtime.get("source_version") == ASR_WHISPER_SOURCE_VERSION,
            observed=runtime.get("source_version"),
            requirement="whisper.cpp matches the Phase 0 source version",
        ),
        _check(
            "whisper_arguments_frozen",
            runtime.get("arguments") == list(ASR_WHISPER_ARGUMENTS),
            observed=runtime.get("arguments"),
            requirement="Whisper inference arguments match the Phase 0 baseline",
        ),
        _check(
            "whisper_process_absent",
            runtime.get("process_running") is False,
            observed={
                "running": runtime.get("process_running"),
                "error_code": runtime.get("error_code"),
            },
            requirement="no pre-existing whisper-cli process can contaminate the pilot",
        ),
    ]
    base_errors = preflight_errors(base)
    return {
        "asr_preflight_schema_version": ASR_PREFLIGHT_SCHEMA_VERSION,
        "expected_branch": expected_branch,
        "base": base,
        "input": input_identity,
        "runtime": runtime,
        "checks": checks,
        "eligible": not base_errors
        and all(check["passed"] is True for check in checks),
    }


def asr_preflight_errors(preflight: Mapping[str, object]) -> list[str]:
    errors: list[str] = []
    if preflight.get("asr_preflight_schema_version") != ASR_PREFLIGHT_SCHEMA_VERSION:
        errors.append("unsupported ASR preflight schema version")
    base = preflight.get("base")
    if not isinstance(base, Mapping):
        errors.append("ASR preflight base record is missing")
    else:
        errors.extend(f"base: {error}" for error in preflight_errors(base))

    checks = preflight.get("checks")
    check_by_name: dict[str, Mapping[str, object]] = {}
    if not isinstance(checks, list):
        errors.append("ASR preflight checks are missing")
    else:
        for item in checks:
            if not isinstance(item, Mapping):
                errors.append("ASR preflight check is not an object")
                continue
            name = item.get("name")
            if not isinstance(name, str) or name in check_by_name:
                errors.append(f"ASR preflight check name is invalid: {name!r}")
                continue
            check_by_name[name] = item
            if item.get("required") is not True or item.get("passed") is not True:
                errors.append(f"ASR preflight check failed: {name}")
    if set(check_by_name) != _REQUIRED_CHECKS:
        errors.append("ASR preflight check set is incomplete or unsupported")

    if preflight.get("input") != {
        "sha256": ASR_INPUT_SHA256,
        "size_bytes": ASR_INPUT_SIZE_BYTES,
        "media_type": ASR_INPUT_MEDIA_TYPE,
    }:
        errors.append("ASR preflight input identity does not match Phase 0")
    if preflight.get("eligible") is not (not errors):
        errors.append("ASR preflight eligibility is inconsistent")
    return errors
