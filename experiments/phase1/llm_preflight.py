"""Fail-closed Jetson and llama.cpp checks for the Phase 1 LLM slice."""

from __future__ import annotations

import json
import os
import shlex
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

from experiments.phase1.jetson_preflight import (
    build_jetson_preflight,
    preflight_errors,
)
from experiments.phase1.manifest import command_snapshot, sha256_file
from experiments.phase1.vlm_preflight import probe_tcp_listener
from jetson.phase1_runtime import PayloadRef

from .llm_adapter import (
    LLM_EXPECTED_SERVED_MODEL_ID,
    LLM_INPUT_MEDIA_TYPE,
    LLM_INPUT_SHA256,
    LLM_INPUT_SIZE_BYTES,
    LLM_MODEL_SHA256,
    LLM_MODEL_SIZE_BYTES,
    LLM_SERVER_ARGUMENTS,
    frozen_llm_request_contract,
    llm_request_contract,
)


LLM_PREFLIGHT_SCHEMA_VERSION = "0.1.0"
_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
_REQUIRED_CHECKS = {
    "fixed_input_identity",
    "llama_model_identity",
    "llama_source_clean",
    "llama_server_process_unique",
    "llama_server_arguments_frozen",
    "llama_endpoint_local",
    "llama_listener_loopback_only",
    "llama_service_reachable",
    "llama_served_model_identity",
    "llm_request_contract_frozen",
}


@dataclass(frozen=True, slots=True)
class LLMRuntime:
    """Private local paths required only while checking the server identity."""

    model_path: Path
    llama_dir: Path
    api_url: str


def load_phase0_llm_runtime() -> LLMRuntime:
    """Resolve the same model and source defaults used by Phase 0."""

    from jetson.config import LLAMA_API_URL

    model_path = Path(
        os.environ.get(
            "PHASE0_QWEN_MODEL",
            str(Path.home() / "models/qwen2.5-1.5b/qwen2.5-1.5b-instruct-q4_k_m.gguf"),
        )
    ).expanduser()
    llama_dir = Path(
        os.environ.get("PHASE0_LLAMA_DIR", str(Path.home() / "llama.cpp"))
    ).expanduser()
    return LLMRuntime(
        model_path=model_path,
        llama_dir=llama_dir,
        api_url=LLAMA_API_URL,
    )


def _local_endpoint(url: str) -> bool:
    parts = urllib.parse.urlsplit(url)
    return parts.scheme == "http" and parts.hostname in _LOCAL_HOSTS


def _endpoint_port(url: str) -> int:
    parts = urllib.parse.urlsplit(url)
    if parts.port is not None:
        return parts.port
    return 443 if parts.scheme == "https" else 80


def _models_url(api_url: str) -> str:
    parts = urllib.parse.urlsplit(api_url)
    prefix = parts.path.split("/v1/", 1)[0]
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, f"{prefix}/v1/models", "", "")
    )


def _read_json(url: str, *, timeout_s: float = 5.0) -> dict[str, object]:
    request = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(request, timeout=timeout_s) as response:
        value = json.loads(response.read().decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("service response is not an object")
    return value


def _parse_process_command(output: object) -> list[list[str]]:
    if not isinstance(output, str):
        return []
    commands: list[list[str]] = []
    for line in output.splitlines():
        try:
            fields = shlex.split(line)
        except ValueError:
            continue
        if len(fields) >= 2 and fields[0].isdigit():
            commands.append(fields[1:])
    return commands


def _flag_value(command: list[str], *names: str) -> str | None:
    for name in names:
        try:
            index = command.index(name)
        except ValueError:
            continue
        if index + 1 < len(command):
            return command[index + 1]
    return None


def _safe_server_arguments(command: list[str]) -> dict[str, object]:
    names = {
        "host": ("--host",),
        "port": ("--port",),
        "n_gpu_layers": ("--n-gpu-layers", "-ngl"),
        "ctx_size": ("--ctx-size", "-c"),
        "threads": ("--threads", "-t"),
        "parallel": ("--parallel", "-np"),
        "cache_ram": ("--cache-ram",),
    }
    result: dict[str, object] = {}
    for key, aliases in names.items():
        value = _flag_value(command, *aliases)
        if key == "host":
            result[key] = value
        else:
            try:
                result[key] = int(value) if value is not None else None
            except ValueError:
                result[key] = None
    return result


def probe_llm_runtime(
    *,
    runtime_loader: Callable[[], LLMRuntime] = load_phase0_llm_runtime,
    snapshotter: Callable[..., Mapping[str, object]] = command_snapshot,
    hasher: Callable[[Path], str] = sha256_file,
    query: Callable[[str], dict[str, object]] = _read_json,
    listener_probe: Callable[[int], Mapping[str, object]] = probe_tcp_listener,
) -> dict[str, object]:
    """Collect model, process and endpoint identity without storing private paths."""

    try:
        runtime = runtime_loader()
    except Exception as exc:
        return {
            "model_size_bytes": None,
            "model_sha256": None,
            "source_version": None,
            "source_clean": False,
            "server_process_count": None,
            "server_arguments": {},
            "server_arguments_match": False,
            "server_model_path_matches": False,
            "endpoint_local": False,
            "listener_addresses": [],
            "listener_loopback_only": False,
            "listener_error_code": "not_probed",
            "service_reachable": False,
            "served_model_ids": [],
            "expected_model_present": False,
            "request_contract": llm_request_contract(),
            "error_code": "runtime_" + type(exc).__name__.lower(),
        }
    if not isinstance(runtime, LLMRuntime):
        raise TypeError("runtime_loader must return LLMRuntime")

    model_size: int | None = None
    model_sha256: str | None = None
    errors: list[str] = []
    if runtime.model_path.is_file():
        model_size = runtime.model_path.stat().st_size
        try:
            model_sha256 = hasher(runtime.model_path)
        except OSError as exc:
            errors.append("model_" + type(exc).__name__.lower())
    else:
        errors.append("model_missing")

    source = snapshotter(
        ["git", "describe", "--tags", "--always", "--dirty"],
        cwd=runtime.llama_dir,
    )
    source_version = source.get("output") if source.get("returncode") == 0 else None
    source_clean = (
        isinstance(source_version, str)
        and bool(source_version)
        and not source_version.endswith("-dirty")
    )
    if not source_clean:
        errors.append("source_identity")

    process = snapshotter(["pgrep", "-a", "llama-server"])
    commands = (
        _parse_process_command(process.get("output"))
        if process.get("returncode") == 0
        else []
    )
    server_arguments = _safe_server_arguments(commands[0]) if len(commands) == 1 else {}
    expected_arguments = dict(LLM_SERVER_ARGUMENTS)
    server_arguments_match = server_arguments == expected_arguments
    model_argument = (
        _flag_value(commands[0], "-m", "--model") if len(commands) == 1 else None
    )
    server_model_path_matches = False
    if model_argument is not None:
        try:
            server_model_path_matches = (
                Path(model_argument).expanduser().resolve()
                == runtime.model_path.resolve()
            )
        except OSError:
            server_model_path_matches = False
    if len(commands) != 1:
        errors.append("server_process_count")
    if not server_arguments_match or not server_model_path_matches:
        errors.append("server_arguments")

    endpoint_local = _local_endpoint(runtime.api_url)
    listener_addresses: list[object] = []
    listener_loopback_only = False
    listener_error_code: object = "not_probed"
    if endpoint_local:
        listener = listener_probe(_endpoint_port(runtime.api_url))
        listener_addresses = list(listener.get("addresses", []))
        listener_loopback_only = listener.get("loopback_only") is True
        listener_error_code = listener.get("error_code")
    else:
        errors.append("nonlocal_endpoint")

    service_reachable = False
    served_model_ids: list[str] = []
    try:
        models = query(_models_url(runtime.api_url)).get("data", [])
        if not isinstance(models, list):
            raise ValueError("llama.cpp models is not a list")
        service_reachable = True
        served_model_ids = sorted(
            {
                item.get("id")
                for item in models
                if isinstance(item, Mapping)
                and isinstance(item.get("id"), str)
                and item.get("id")
            }
        )[:4]
    except Exception as exc:
        errors.append("service_" + type(exc).__name__.lower())

    return {
        "model_size_bytes": model_size,
        "model_sha256": model_sha256,
        "source_version": source_version,
        "source_clean": source_clean,
        "server_process_count": len(commands),
        "server_arguments": server_arguments,
        "server_arguments_match": server_arguments_match,
        "server_model_path_matches": server_model_path_matches,
        "endpoint_local": endpoint_local,
        "listener_addresses": listener_addresses,
        "listener_loopback_only": listener_loopback_only,
        "listener_error_code": listener_error_code,
        "service_reachable": service_reachable,
        "served_model_ids": served_model_ids,
        "expected_model_present": LLM_EXPECTED_SERVED_MODEL_ID in served_model_ids,
        "request_contract": llm_request_contract(),
        "error_code": errors[0] if errors else None,
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


def build_llm_preflight(
    repo_root: Path | str,
    *,
    input_payload: PayloadRef,
    expected_branch: str = "main",
    base_preflight: Mapping[str, object] | None = None,
    runtime_status: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Build the complete platform, prompt and local-server eligibility record."""

    if not isinstance(input_payload, PayloadRef):
        raise TypeError("input_payload must be a PayloadRef")
    base = dict(
        base_preflight
        if base_preflight is not None
        else build_jetson_preflight(repo_root, expected_branch=expected_branch)
    )
    runtime = dict(
        runtime_status if runtime_status is not None else probe_llm_runtime()
    )
    input_identity = {
        "sha256": input_payload.sha256,
        "size_bytes": input_payload.size_bytes,
        "media_type": input_payload.media_type,
    }
    expected_input = {
        "sha256": LLM_INPUT_SHA256,
        "size_bytes": LLM_INPUT_SIZE_BYTES,
        "media_type": LLM_INPUT_MEDIA_TYPE,
    }
    observed_model = {
        "sha256": runtime.get("model_sha256"),
        "size_bytes": runtime.get("model_size_bytes"),
    }
    expected_model = {
        "sha256": LLM_MODEL_SHA256,
        "size_bytes": LLM_MODEL_SIZE_BYTES,
    }
    checks = [
        _check(
            "fixed_input_identity",
            input_identity == expected_input,
            observed=input_identity,
            requirement="input matches the frozen Phase 0 LLM prompt",
        ),
        _check(
            "llama_model_identity",
            observed_model == expected_model,
            observed=observed_model,
            requirement="the Qwen GGUF matches the Phase 0 identity",
        ),
        _check(
            "llama_source_clean",
            runtime.get("source_clean") is True,
            observed=runtime.get("source_version"),
            requirement="the recorded llama.cpp source checkout is clean",
        ),
        _check(
            "llama_server_process_unique",
            runtime.get("server_process_count") == 1,
            observed=runtime.get("server_process_count"),
            requirement="exactly one pre-existing llama-server process is active",
        ),
        _check(
            "llama_server_arguments_frozen",
            runtime.get("server_arguments_match") is True
            and runtime.get("server_model_path_matches") is True,
            observed={
                "arguments": runtime.get("server_arguments"),
                "model_path_matches": runtime.get("server_model_path_matches"),
            },
            requirement="the resident server uses the frozen Phase 0 launch contract",
        ),
        _check(
            "llama_endpoint_local",
            runtime.get("endpoint_local") is True,
            observed=runtime.get("endpoint_local"),
            requirement="the llama.cpp chat endpoint is loopback-only",
        ),
        _check(
            "llama_listener_loopback_only",
            runtime.get("listener_loopback_only") is True,
            observed={
                "addresses": runtime.get("listener_addresses"),
                "error_code": runtime.get("listener_error_code"),
            },
            requirement="the llama.cpp TCP listener binds only loopback addresses",
        ),
        _check(
            "llama_service_reachable",
            runtime.get("service_reachable") is True,
            observed={
                "reachable": runtime.get("service_reachable"),
                "error_code": runtime.get("error_code"),
            },
            requirement="the local llama.cpp service answers its model query",
        ),
        _check(
            "llama_served_model_identity",
            runtime.get("expected_model_present") is True,
            observed=runtime.get("served_model_ids"),
            requirement="the endpoint exposes the frozen Qwen model identity",
        ),
        _check(
            "llm_request_contract_frozen",
            runtime.get("request_contract") == frozen_llm_request_contract(),
            observed=runtime.get("request_contract"),
            requirement="the text-free Phase 0 chat request contract is unchanged",
        ),
    ]
    base_errors = preflight_errors(base)
    return {
        "llm_preflight_schema_version": LLM_PREFLIGHT_SCHEMA_VERSION,
        "expected_branch": expected_branch,
        "base": base,
        "input": input_identity,
        "runtime": runtime,
        "checks": checks,
        "eligible": not base_errors
        and all(check["passed"] is True for check in checks),
    }


def llm_preflight_errors(preflight: Mapping[str, object]) -> list[str]:
    errors: list[str] = []
    if preflight.get("llm_preflight_schema_version") != LLM_PREFLIGHT_SCHEMA_VERSION:
        errors.append("unsupported LLM preflight schema version")
    base = preflight.get("base")
    if not isinstance(base, Mapping):
        errors.append("LLM preflight base record is missing")
    else:
        errors.extend(f"base: {error}" for error in preflight_errors(base))
    checks = preflight.get("checks")
    by_name: dict[str, Mapping[str, object]] = {}
    if not isinstance(checks, list):
        errors.append("LLM preflight checks are missing")
    else:
        for item in checks:
            if not isinstance(item, Mapping):
                errors.append("LLM preflight check is not an object")
                continue
            name = item.get("name")
            if not isinstance(name, str) or name in by_name:
                errors.append(f"LLM preflight check name is invalid: {name!r}")
                continue
            by_name[name] = item
            if item.get("required") is not True:
                errors.append(f"LLM preflight check is not required: {name}")
        if set(by_name) != _REQUIRED_CHECKS:
            errors.append("LLM preflight check set is incomplete or unsupported")
        for name in sorted(_REQUIRED_CHECKS):
            check = by_name.get(name)
            if check is not None and check.get("passed") is not True:
                errors.append(f"LLM preflight check failed: {name}")
    if preflight.get("input") != {
        "sha256": LLM_INPUT_SHA256,
        "size_bytes": LLM_INPUT_SIZE_BYTES,
        "media_type": LLM_INPUT_MEDIA_TYPE,
    }:
        errors.append("LLM preflight input identity does not match Phase 0")
    expected_eligible = not errors
    if preflight.get("eligible") is not expected_eligible:
        errors.append("LLM preflight eligible flag is inconsistent")
    return errors
