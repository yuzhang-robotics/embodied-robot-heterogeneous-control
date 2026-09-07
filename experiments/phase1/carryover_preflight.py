"""Fail-closed eligibility checks for the ASR/VLM carryover diagnostic."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Mapping

from experiments.phase1.asr_adapter import fixed_asr_payload
from experiments.phase1.asr_preflight import asr_preflight_errors, build_asr_preflight
from experiments.phase1.carryover_protocol import (
    CARRYOVER_PROTOCOL_ID,
    CARRYOVER_PROTOCOL_SHA256,
    CARRYOVER_PROTOCOL_STATUS,
    DEFAULT_PROTOCOL_PATH,
    protocol_errors,
    protocol_sha256,
)
from experiments.phase1.formal_preflight import (
    _ollama_identity,
    _package_present,
    _service_identity,
    _service_identity_valid,
)
from experiments.phase1.formal_protocol import (
    LLAMA_SOURCE_VERSION,
    VLM_MOONDREAM_DIGEST,
    VLM_OLLAMA_BINARY_SHA256,
    VLM_OLLAMA_VERSION,
)
from experiments.phase1.jetson_preflight import (
    build_jetson_preflight,
    preflight_errors as base_preflight_errors,
)
from experiments.phase1.llm_adapter import (
    LLM_EXPECTED_SERVED_MODEL_ID,
    fixed_llm_payload,
)
from experiments.phase1.llm_preflight import build_llm_preflight, llm_preflight_errors
from experiments.phase1.manifest import command_snapshot, utc_now_iso
from experiments.phase1.vlm_adapter import fixed_c100_payload
from experiments.phase1.vlm_preflight import build_vlm_preflight, vlm_preflight_errors


CARRYOVER_PREFLIGHT_SCHEMA_VERSION = "0.1.0"
_REQUIRED_CHECKS = {
    "protocol_identity",
    "python_version",
    "jetpack_version",
    "l4t_core_version",
    "power_mode",
    "dynamic_dvfs_confirmed",
    "services_restarted",
    "ollama_version",
    "ollama_binary_identity",
    "moondream_digest",
    "llama_source_version",
    "qwen_served_identity",
    "unrelated_inference_absent",
    "workload_preflights",
}


def _check(
    name: str, passed: bool, observed: object, requirement: str
) -> dict[str, object]:
    return {
        "name": name,
        "required": True,
        "passed": bool(passed),
        "observed": observed,
        "requirement": requirement,
    }


def build_carryover_preflight(
    repo_root: Path | str,
    protocol: Mapping[str, object],
    *,
    asr_input: Path | str,
    llm_input: Path | str,
    vlm_input: Path | str,
    services_restarted: bool,
    dynamic_dvfs_confirmed: bool,
    protocol_path: Path | str = DEFAULT_PROTOCOL_PATH,
    base_preflight: Mapping[str, object] | None = None,
    asr_preflight: Mapping[str, object] | None = None,
    llm_preflight: Mapping[str, object] | None = None,
    vlm_preflight: Mapping[str, object] | None = None,
    ollama_identity: Mapping[str, object] | None = None,
    service_identity: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Bind the diagnostic to the same validated target environment as G6 v4."""

    if not isinstance(services_restarted, bool):
        raise TypeError("services_restarted must be boolean")
    if not isinstance(dynamic_dvfs_confirmed, bool):
        raise TypeError("dynamic_dvfs_confirmed must be boolean")
    root = Path(repo_root).resolve()
    protocol_file = Path(protocol_path).resolve()
    tracked_protocol = (
        root / "experiments" / "phase1" / "diagnostic" / DEFAULT_PROTOCOL_PATH.name
    ).resolve()
    base = dict(
        base_preflight
        if base_preflight is not None
        else build_jetson_preflight(root, expected_branch="main")
    )
    payloads = {
        "asr": fixed_asr_payload(asr_input),
        "llm": fixed_llm_payload(llm_input),
        "vlm": fixed_c100_payload(vlm_input),
    }
    workloads = {
        "asr": dict(
            asr_preflight
            if asr_preflight is not None
            else build_asr_preflight(
                root,
                input_payload=payloads["asr"],
                expected_branch="main",
                base_preflight=base,
            )
        ),
        "llm": dict(
            llm_preflight
            if llm_preflight is not None
            else build_llm_preflight(
                root,
                input_payload=payloads["llm"],
                expected_branch="main",
                base_preflight=base,
            )
        ),
        "vlm": dict(
            vlm_preflight
            if vlm_preflight is not None
            else build_vlm_preflight(
                root,
                input_payload=payloads["vlm"],
                expected_branch="main",
                base_preflight=base,
            )
        ),
    }
    ollama = dict(ollama_identity or _ollama_identity())
    services = dict(service_identity or _service_identity())
    environment = base.get("environment")
    environment_record = environment if isinstance(environment, Mapping) else {}
    git = environment_record.get("git")
    git_record = git if isinstance(git, Mapping) else {}
    packages = environment_record.get("jetpack_packages")
    package_record = packages if isinstance(packages, Mapping) else {}
    power = environment_record.get("nvpmodel")
    power_record = power if isinstance(power, Mapping) else {}
    llm_runtime = workloads["llm"].get("runtime")
    llm_runtime_record = llm_runtime if isinstance(llm_runtime, Mapping) else {}
    vlm_services = workloads["vlm"].get("services")
    vlm_service_record = vlm_services if isinstance(vlm_services, Mapping) else {}
    vlm_ollama = vlm_service_record.get("ollama")
    vlm_ollama_record = vlm_ollama if isinstance(vlm_ollama, Mapping) else {}
    vlm_qwen = vlm_service_record.get("qwen")
    vlm_qwen_record = vlm_qwen if isinstance(vlm_qwen, Mapping) else {}
    protocol_commit = (
        command_snapshot(
            [
                "git",
                "log",
                "-1",
                "--format=%H",
                "--",
                protocol_file.relative_to(root).as_posix(),
            ],
            cwd=root,
        )
        if protocol_file == tracked_protocol
        else {"returncode": 1, "output": "", "error_code": "protocol_path"}
    )
    errors = protocol_errors(protocol)
    digest = protocol_sha256(protocol) if not errors else None
    package_output = package_record.get("output")
    power_output = str(power_record.get("output") or "")
    workload_errors = {
        "asr": asr_preflight_errors(workloads["asr"]),
        "llm": llm_preflight_errors(workloads["llm"]),
        "vlm": vlm_preflight_errors(workloads["vlm"]),
    }
    checks = [
        _check(
            "protocol_identity",
            not errors
            and digest == CARRYOVER_PROTOCOL_SHA256
            and protocol_file == tracked_protocol
            and protocol.get("protocol_id") == CARRYOVER_PROTOCOL_ID
            and protocol.get("status") == CARRYOVER_PROTOCOL_STATUS
            and protocol_commit.get("returncode") == 0
            and re.fullmatch(r"[0-9a-f]{40}", str(protocol_commit.get("output")))
            is not None,
            {
                "sha256": digest,
                "errors": errors,
                "tracked_path": protocol_file == tracked_protocol,
                "protocol_commit": protocol_commit.get("output"),
                "runner_commit": git_record.get("commit"),
            },
            "the tracked diagnostic is active and committed on synchronized main",
        ),
        _check(
            "python_version",
            environment_record.get("python") == "3.10.12",
            environment_record.get("python"),
            "Python is exactly 3.10.12",
        ),
        _check(
            "jetpack_version",
            _package_present(package_output, "nvidia-jetpack", "6.2.2+b24"),
            package_output,
            "nvidia-jetpack is exactly 6.2.2+b24",
        ),
        _check(
            "l4t_core_version",
            _package_present(
                package_output,
                "nvidia-l4t-core",
                "36.5.0-20260115194252",
            ),
            package_output,
            "nvidia-l4t-core matches the G6 v4 target",
        ),
        _check(
            "power_mode",
            "MAXN_SUPER" in power_output
            and any(line.strip() == "2" for line in power_output.splitlines()),
            power_output,
            "nvpmodel reports MAXN_SUPER mode 2",
        ),
        _check(
            "dynamic_dvfs_confirmed",
            dynamic_dvfs_confirmed,
            dynamic_dvfs_confirmed,
            "the operator confirmed jetson_clocks is not enabled",
        ),
        _check(
            "services_restarted",
            services_restarted and _service_identity_valid(services),
            {"confirmed": services_restarted, "identity": services},
            "both expected model services were restarted before this session",
        ),
        _check(
            "ollama_version",
            str(ollama.get("version_output") or "")
            in {
                f"ollama version is {VLM_OLLAMA_VERSION}",
                f"ollama version {VLM_OLLAMA_VERSION}",
            },
            ollama.get("version_output"),
            f"Ollama is exactly {VLM_OLLAMA_VERSION}",
        ),
        _check(
            "ollama_binary_identity",
            ollama.get("binary_sha256") == VLM_OLLAMA_BINARY_SHA256
            and ollama.get("executable_path_recorded") is False,
            {
                "sha256": ollama.get("binary_sha256"),
                "path_recorded": ollama.get("executable_path_recorded"),
            },
            "the Ollama executable matches the G6 v4 target",
        ),
        _check(
            "moondream_digest",
            vlm_ollama_record.get("model_digest") == VLM_MOONDREAM_DIGEST,
            vlm_ollama_record.get("model_digest"),
            "Moondream matches the G6 v4 target",
        ),
        _check(
            "llama_source_version",
            llm_runtime_record.get("source_version") == LLAMA_SOURCE_VERSION,
            llm_runtime_record.get("source_version"),
            "llama.cpp matches the G6 v4 target",
        ),
        _check(
            "qwen_served_identity",
            llm_runtime_record.get("served_model_ids") == [LLM_EXPECTED_SERVED_MODEL_ID]
            and vlm_qwen_record.get("served_model_ids")
            == [LLM_EXPECTED_SERVED_MODEL_ID],
            {
                "llm": llm_runtime_record.get("served_model_ids"),
                "vlm": vlm_qwen_record.get("served_model_ids"),
            },
            "both adapters resolve the frozen Qwen model",
        ),
        _check(
            "unrelated_inference_absent",
            ollama.get("active_model_count") == 0
            and ollama.get("active_model_names_recorded") is False
            and ollama.get("error_code") is None,
            {"ollama_active_model_count": ollama.get("active_model_count")},
            "no unrelated model is loaded in Ollama",
        ),
        _check(
            "workload_preflights",
            all(not item for item in workload_errors.values()),
            workload_errors,
            "ASR, LLM and VLM preflights all pass",
        ),
    ]
    return {
        "carryover_preflight_schema_version": CARRYOVER_PREFLIGHT_SCHEMA_VERSION,
        "captured_at": utc_now_iso(),
        "protocol": {
            "id": protocol.get("protocol_id"),
            "sha256": digest,
            "protocol_commit": protocol_commit.get("output"),
            "runner_commit": git_record.get("commit"),
            "path_recorded": False,
        },
        "base": base,
        "workloads": workloads,
        "ollama": ollama,
        "service_identity": services,
        "checks": checks,
        "eligible": all(check["passed"] is True for check in checks),
        "formal_evidence": False,
    }


def carryover_preflight_errors(preflight: Mapping[str, object]) -> list[str]:
    errors: list[str] = []
    if (
        preflight.get("carryover_preflight_schema_version")
        != CARRYOVER_PREFLIGHT_SCHEMA_VERSION
    ):
        errors.append("unsupported carryover preflight schema version")
    base = preflight.get("base")
    if not isinstance(base, Mapping):
        errors.append("carryover preflight base record is missing")
    else:
        errors.extend(f"base: {error}" for error in base_preflight_errors(base))
    workloads = preflight.get("workloads")
    if not isinstance(workloads, Mapping) or set(workloads) != {"asr", "llm", "vlm"}:
        errors.append("carryover workload preflights are missing")
    checks = preflight.get("checks")
    by_name: dict[str, Mapping[str, object]] = {}
    if not isinstance(checks, list):
        errors.append("carryover preflight checks are missing")
    else:
        for item in checks:
            if not isinstance(item, Mapping):
                errors.append("carryover preflight check is not an object")
                continue
            name = item.get("name")
            if not isinstance(name, str) or name in by_name:
                errors.append(f"carryover preflight check name is invalid: {name!r}")
                continue
            by_name[name] = item
            if item.get("required") is not True or item.get("passed") is not True:
                errors.append(f"carryover preflight check failed: {name}")
    if set(by_name) != _REQUIRED_CHECKS:
        errors.append("carryover preflight check set is incomplete or unsupported")
    protocol = preflight.get("protocol")
    if not isinstance(protocol, Mapping) or protocol.get("id") != CARRYOVER_PROTOCOL_ID:
        errors.append("carryover protocol identity is invalid")
    elif protocol.get("sha256") != CARRYOVER_PROTOCOL_SHA256:
        errors.append("carryover protocol SHA-256 is invalid")
    if preflight.get("formal_evidence") is not False:
        errors.append("carryover diagnostic must not claim formal evidence")
    if preflight.get("eligible") is not (not errors):
        errors.append("carryover preflight eligibility is inconsistent")
    return errors
