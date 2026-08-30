"""Fail-closed Jetson and service checks for the Phase 1 VLM slice."""

from __future__ import annotations

import importlib.util
import ipaddress
import json
import shutil
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable, Mapping

from experiments.phase1.jetson_preflight import (
    build_jetson_preflight,
    preflight_errors,
)
from experiments.phase1.manifest import command_snapshot
from experiments.phase1.vlm_adapter import (
    C100_INPUT_MEDIA_TYPE,
    C100_INPUT_SHA256,
    C100_INPUT_SIZE_BYTES,
)
from jetson.phase1_runtime import PayloadRef


VLM_PREFLIGHT_SCHEMA_VERSION = "0.2.0"
_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
_REQUIRED_CHECKS_V0_1 = {
    "fixed_input_identity",
    "python_dependencies_available",
    "ollama_endpoint_local",
    "ollama_cli_available",
    "ollama_service_reachable",
    "moondream_model_present",
    "qwen_endpoint_local",
    "qwen_service_reachable",
    "qwen_model_present",
}
_REQUIRED_CHECKS_BY_VERSION = {
    "0.1.0": _REQUIRED_CHECKS_V0_1,
    "0.2.0": _REQUIRED_CHECKS_V0_1
    | {
        "ollama_listener_loopback_only",
        "qwen_listener_loopback_only",
    },
}


def _local_endpoint(url: str) -> bool:
    parts = urllib.parse.urlsplit(url)
    return parts.scheme == "http" and parts.hostname in _LOCAL_HOSTS


def _read_json(url: str, *, timeout_s: float = 5.0) -> dict[str, object]:
    request = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(request, timeout=timeout_s) as response:
        value = json.loads(response.read().decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("service response is not an object")
    return value


def _llama_models_url(api_url: str) -> str:
    parts = urllib.parse.urlsplit(api_url)
    prefix = parts.path.split("/v1/", 1)[0]
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, f"{prefix}/v1/models", "", "")
    )


def _endpoint_port(url: str) -> int:
    parts = urllib.parse.urlsplit(url)
    if parts.port is not None:
        return parts.port
    return 443 if parts.scheme == "https" else 80


def _listener_host(local_address: str, port: int) -> str | None:
    token = local_address.strip()
    if token.startswith("["):
        closing = token.rfind("]")
        if closing < 0 or token[closing + 1 :] != f":{port}":
            return None
        return token[1:closing].split("%", 1)[0]
    try:
        host, service = token.rsplit(":", 1)
    except ValueError:
        return None
    if service != str(port):
        return None
    return host.split("%", 1)[0]


def _is_loopback_address(address: str) -> bool:
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False
    if parsed.is_loopback:
        return True
    mapped = getattr(parsed, "ipv4_mapped", None)
    return mapped is not None and mapped.is_loopback


def probe_tcp_listener(
    port: int,
    *,
    snapshotter: Callable[[list[str]], Mapping[str, object]] = command_snapshot,
) -> dict[str, object]:
    """Record the bound addresses for one TCP port without process details."""

    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("port must be an integer from 1 to 65535")
    snapshot = snapshotter(["ss", "-H", "-ltn"])
    returncode = snapshot.get("returncode")
    error_code = snapshot.get("error_code")
    output = snapshot.get("output")
    if returncode != 0 or error_code is not None or not isinstance(output, str):
        return {
            "addresses": [],
            "loopback_only": False,
            "error_code": str(error_code or "listener_probe_failed"),
        }

    addresses: set[str] = set()
    unparsed_match = False
    for line in output.splitlines():
        fields = line.split()
        if len(fields) < 4:
            continue
        local_address = fields[3]
        if not local_address.endswith(f":{port}"):
            continue
        host = _listener_host(local_address, port)
        if host is None:
            unparsed_match = True
        else:
            addresses.add(host)
    ordered = sorted(addresses)
    listener_found = bool(ordered) or unparsed_match
    loopback_only = (
        bool(ordered)
        and not unparsed_match
        and all(_is_loopback_address(address) for address in ordered)
    )
    return {
        "addresses": ordered,
        "loopback_only": loopback_only,
        "error_code": None if listener_found else "listener_not_found",
    }


def probe_vlm_services(
    *,
    query: Callable[[str], dict[str, object]] = _read_json,
    listener_probe: Callable[[int], Mapping[str, object]] = probe_tcp_listener,
) -> dict[str, object]:
    """Probe local model services without importing the VLM implementation."""

    from jetson.config import LLAMA_API_URL, OLLAMA_CHAT_URL, VLM_MODEL

    ollama: dict[str, object] = {
        "endpoint_local": _local_endpoint(OLLAMA_CHAT_URL),
        "reachable": False,
        "model": VLM_MODEL,
        "model_present": False,
        "model_digest": None,
        "listener_addresses": [],
        "listener_loopback_only": False,
        "listener_error_code": "not_probed",
        "error_code": None,
    }
    if ollama["endpoint_local"] is not True:
        ollama["error_code"] = "nonlocal_endpoint"
    else:
        listener = listener_probe(_endpoint_port(OLLAMA_CHAT_URL))
        ollama["listener_addresses"] = list(listener.get("addresses", []))
        ollama["listener_loopback_only"] = listener.get("loopback_only") is True
        ollama["listener_error_code"] = listener.get("error_code")
        try:
            models = query(OLLAMA_CHAT_URL.rsplit("/", 1)[0] + "/tags").get(
                "models", []
            )
            if not isinstance(models, list):
                raise ValueError("Ollama models is not a list")
            ollama["reachable"] = True
            selected = next(
                (
                    item
                    for item in models
                    if isinstance(item, Mapping)
                    and item.get("name") in {VLM_MODEL, f"{VLM_MODEL}:latest"}
                ),
                None,
            )
            if selected is not None:
                ollama["model_present"] = True
                digest = selected.get("digest")
                if isinstance(digest, str) and digest:
                    ollama["model_digest"] = digest
        except Exception as exc:
            ollama["error_code"] = type(exc).__name__.lower()

    qwen: dict[str, object] = {
        "endpoint_local": _local_endpoint(LLAMA_API_URL),
        "reachable": False,
        "model": "qwen",
        "model_present": False,
        "served_model_ids": [],
        "listener_addresses": [],
        "listener_loopback_only": False,
        "listener_error_code": "not_probed",
        "error_code": None,
    }
    if qwen["endpoint_local"] is not True:
        qwen["error_code"] = "nonlocal_endpoint"
    else:
        listener = listener_probe(_endpoint_port(LLAMA_API_URL))
        qwen["listener_addresses"] = list(listener.get("addresses", []))
        qwen["listener_loopback_only"] = listener.get("loopback_only") is True
        qwen["listener_error_code"] = listener.get("error_code")
        try:
            models = query(_llama_models_url(LLAMA_API_URL)).get("data", [])
            if not isinstance(models, list):
                raise ValueError("llama.cpp models is not a list")
            qwen["reachable"] = True
            identifiers = sorted(
                {
                    item.get("id")
                    for item in models
                    if isinstance(item, Mapping)
                    and isinstance(item.get("id"), str)
                    and item.get("id")
                }
            )
            qwen["served_model_ids"] = identifiers[:4]
            qwen["model_present"] = bool(identifiers)
        except Exception as exc:
            qwen["error_code"] = type(exc).__name__.lower()

    dependencies = {
        name: importlib.util.find_spec(name) is not None
        for name in ("argostranslate", "cv2")
    }
    return {
        "ollama": ollama,
        "qwen": qwen,
        "python_dependencies": dependencies,
        "ollama_cli_available": shutil.which("ollama") is not None,
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


def build_vlm_preflight(
    repo_root: Path | str,
    *,
    input_payload: PayloadRef,
    expected_branch: str = "main",
    base_preflight: Mapping[str, object] | None = None,
    services: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Build the complete platform, input and local-service eligibility record."""

    if not isinstance(input_payload, PayloadRef):
        raise TypeError("input_payload must be a PayloadRef")
    base = dict(
        base_preflight
        if base_preflight is not None
        else build_jetson_preflight(repo_root, expected_branch=expected_branch)
    )
    observed_services = dict(services or probe_vlm_services())
    ollama = observed_services.get("ollama")
    qwen = observed_services.get("qwen")
    dependencies = observed_services.get("python_dependencies")
    ollama_cli_available = observed_services.get("ollama_cli_available")
    ollama_record = ollama if isinstance(ollama, Mapping) else {}
    qwen_record = qwen if isinstance(qwen, Mapping) else {}
    dependency_record = dependencies if isinstance(dependencies, Mapping) else {}
    input_identity = {
        "sha256": input_payload.sha256,
        "size_bytes": input_payload.size_bytes,
        "media_type": input_payload.media_type,
    }
    input_matches = input_identity == {
        "sha256": C100_INPUT_SHA256,
        "size_bytes": C100_INPUT_SIZE_BYTES,
        "media_type": C100_INPUT_MEDIA_TYPE,
    }
    checks = [
        _check(
            "fixed_input_identity",
            input_matches,
            observed=input_identity,
            requirement="input matches the frozen Phase 0 C100 image",
        ),
        _check(
            "python_dependencies_available",
            dependency_record.get("argostranslate") is True
            and dependency_record.get("cv2") is True,
            observed=dict(dependency_record),
            requirement="Argos Translate and OpenCV packages are installed",
        ),
        _check(
            "ollama_endpoint_local",
            ollama_record.get("endpoint_local") is True,
            observed=ollama_record.get("endpoint_local"),
            requirement="the Ollama endpoint is loopback-only",
        ),
        _check(
            "ollama_listener_loopback_only",
            ollama_record.get("listener_loopback_only") is True,
            observed={
                "addresses": ollama_record.get("listener_addresses"),
                "error_code": ollama_record.get("listener_error_code"),
            },
            requirement="the Ollama TCP listener binds only loopback addresses",
        ),
        _check(
            "ollama_cli_available",
            ollama_cli_available is True,
            observed=ollama_cli_available,
            requirement="the Ollama CLI is available for the unload request",
        ),
        _check(
            "ollama_service_reachable",
            ollama_record.get("reachable") is True,
            observed={
                "reachable": ollama_record.get("reachable"),
                "error_code": ollama_record.get("error_code"),
            },
            requirement="the local Ollama service answers its model query",
        ),
        _check(
            "moondream_model_present",
            ollama_record.get("model_present") is True,
            observed={
                "model": ollama_record.get("model"),
                "model_present": ollama_record.get("model_present"),
                "model_digest": ollama_record.get("model_digest"),
            },
            requirement="the configured Moondream model is installed",
        ),
        _check(
            "qwen_endpoint_local",
            qwen_record.get("endpoint_local") is True,
            observed=qwen_record.get("endpoint_local"),
            requirement="the Qwen rewrite endpoint is loopback-only",
        ),
        _check(
            "qwen_listener_loopback_only",
            qwen_record.get("listener_loopback_only") is True,
            observed={
                "addresses": qwen_record.get("listener_addresses"),
                "error_code": qwen_record.get("listener_error_code"),
            },
            requirement="the Qwen TCP listener binds only loopback addresses",
        ),
        _check(
            "qwen_service_reachable",
            qwen_record.get("reachable") is True,
            observed={
                "reachable": qwen_record.get("reachable"),
                "error_code": qwen_record.get("error_code"),
            },
            requirement="the local llama.cpp service answers its model query",
        ),
        _check(
            "qwen_model_present",
            qwen_record.get("model_present") is True,
            observed={
                "model": qwen_record.get("model"),
                "model_present": qwen_record.get("model_present"),
                "served_model_ids": qwen_record.get("served_model_ids"),
            },
            requirement="the local rewrite endpoint exposes a model identity",
        ),
    ]
    base_errors = preflight_errors(base)
    return {
        "vlm_preflight_schema_version": VLM_PREFLIGHT_SCHEMA_VERSION,
        "expected_branch": expected_branch,
        "base": base,
        "input": input_identity,
        "services": observed_services,
        "checks": checks,
        "eligible": not base_errors
        and all(check["passed"] is True for check in checks),
    }


def vlm_preflight_errors(preflight: Mapping[str, object]) -> list[str]:
    errors: list[str] = []
    schema_version = preflight.get("vlm_preflight_schema_version")
    required_checks = _REQUIRED_CHECKS_BY_VERSION.get(str(schema_version))
    if required_checks is None:
        errors.append("unsupported VLM preflight schema version")
    base = preflight.get("base")
    if not isinstance(base, Mapping):
        errors.append("VLM preflight base record is missing")
    else:
        errors.extend(f"base: {error}" for error in preflight_errors(base))
    checks = preflight.get("checks")
    check_by_name: dict[str, Mapping[str, object]] = {}
    if not isinstance(checks, list):
        errors.append("VLM preflight checks are missing")
    else:
        for item in checks:
            if not isinstance(item, Mapping):
                errors.append("VLM preflight check is not an object")
                continue
            name = item.get("name")
            if not isinstance(name, str) or name in check_by_name:
                errors.append(f"VLM preflight check name is invalid: {name!r}")
                continue
            check_by_name[name] = item
            if item.get("required") is not True or item.get("passed") is not True:
                errors.append(f"VLM preflight check failed: {name}")
    if required_checks is not None and set(check_by_name) != required_checks:
        errors.append("VLM preflight check set is incomplete or unsupported")

    input_identity = preflight.get("input")
    if input_identity != {
        "sha256": C100_INPUT_SHA256,
        "size_bytes": C100_INPUT_SIZE_BYTES,
        "media_type": C100_INPUT_MEDIA_TYPE,
    }:
        errors.append("VLM preflight input identity does not match Phase 0")
    if preflight.get("eligible") is not (not errors):
        errors.append("VLM preflight eligibility is inconsistent")
    return errors
