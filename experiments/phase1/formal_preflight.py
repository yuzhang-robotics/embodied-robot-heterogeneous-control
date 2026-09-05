"""Fail-closed eligibility checks for a Phase 1 formal session."""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Callable, Mapping

from experiments.phase1.asr_adapter import fixed_asr_payload
from experiments.phase1.asr_preflight import (
    asr_preflight_errors,
    build_asr_preflight,
)
from experiments.phase1.formal_protocol import (
    DEFAULT_PROTOCOL_PATH,
    LLAMA_SOURCE_VERSION,
    VLM_MOONDREAM_DIGEST,
    VLM_OLLAMA_BINARY_SHA256,
    VLM_OLLAMA_VERSION,
    formal_protocol_errors,
    protocol_sha256,
)
from experiments.phase1.jetson_preflight import (
    build_jetson_preflight,
    preflight_errors,
)
from experiments.phase1.llm_adapter import (
    LLM_EXPECTED_SERVED_MODEL_ID,
    fixed_llm_payload,
)
from experiments.phase1.llm_preflight import (
    build_llm_preflight,
    llm_preflight_errors,
)
from experiments.phase1.manifest import command_snapshot, sha256_file, utc_now_iso
from experiments.phase1.vlm_adapter import fixed_c100_payload
from experiments.phase1.vlm_preflight import (
    build_vlm_preflight,
    vlm_preflight_errors,
)


FORMAL_PREFLIGHT_SCHEMA_VERSION = "0.1.0"
FROZEN_PROTOCOL_SHA256 = (
    "022df6af4bb3236a28b2e47f0edb9afbc6078131441a1c1f9e8730920c660761"
)
_REQUIRED_CHECKS = {
    "protocol_identity",
    "protocol_activated",
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
    name: str,
    passed: bool,
    *,
    observed: object,
    requirement: str,
) -> dict[str, object]:
    return {
        "name": name,
        "required": True,
        "passed": bool(passed),
        "observed": observed,
        "requirement": requirement,
    }


def _ollama_identity(
    *,
    snapshotter: Callable[..., Mapping[str, object]] = command_snapshot,
    hasher: Callable[[Path], str] = sha256_file,
) -> dict[str, object]:
    executable = shutil.which("ollama")
    version = snapshotter(["ollama", "--version"])
    active = snapshotter(["ollama", "ps"])
    digest: str | None = None
    error_code: str | None = None
    if executable is None:
        error_code = "ollama_missing"
    else:
        try:
            digest = hasher(Path(executable).resolve())
        except OSError as exc:
            error_code = "ollama_hash_" + type(exc).__name__.lower()
    active_output = active.get("output")
    active_lines = (
        [line for line in str(active_output).splitlines() if line.strip()]
        if active.get("returncode") == 0
        else []
    )
    return {
        "version_output": (
            version.get("output") if version.get("returncode") == 0 else None
        ),
        "binary_sha256": digest,
        "executable_path_recorded": False,
        "active_model_count": max(0, len(active_lines) - 1),
        "active_model_names_recorded": False,
        "error_code": error_code,
    }


def _service_identity(
    *,
    snapshotter: Callable[..., Mapping[str, object]] = command_snapshot,
) -> dict[str, object]:
    result: dict[str, object] = {}
    for name in ("llama-server", "ollama"):
        snapshot = snapshotter(
            ["ps", "-C", name, "-o", "pid=,lstart="],
        )
        lines = (
            sorted(
                line.strip()
                for line in str(snapshot.get("output", "")).splitlines()
                if line.strip()
            )
            if snapshot.get("returncode") == 0
            else []
        )
        result[name] = {
            "process_count": len(lines),
            "process_start_identities": lines,
            "arguments_recorded": False,
        }
    return result


def _package_present(output: object, package: str, version: str) -> bool:
    return any(
        line.split() == [package, version] for line in str(output or "").splitlines()
    )


def _service_identity_valid(value: Mapping[str, object]) -> bool:
    if set(value) != {"llama-server", "ollama"}:
        return False
    for name in ("llama-server", "ollama"):
        service = value.get(name)
        service_record = service if isinstance(service, Mapping) else {}
        identities = service_record.get("process_start_identities")
        if (
            service_record.get("process_count") != 1
            or not isinstance(identities, list)
            or len(identities) != 1
            or not isinstance(identities[0], str)
            or not identities[0]
            or service_record.get("arguments_recorded") is not False
        ):
            return False
    return True


def build_formal_preflight(
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
    """Collect and bind all formal eligibility evidence to the frozen protocol."""

    if not isinstance(services_restarted, bool):
        raise TypeError("services_restarted must be boolean")
    if not isinstance(dynamic_dvfs_confirmed, bool):
        raise TypeError("dynamic_dvfs_confirmed must be boolean")
    root = Path(repo_root).resolve()
    protocol_file = Path(protocol_path).resolve()
    expected_protocol_file = (
        root / "experiments" / "phase1" / "formal" / "phase1-g6-preregistration.json"
    ).resolve()
    base = dict(
        base_preflight
        if base_preflight is not None
        else build_jetson_preflight(root, expected_branch="main")
    )
    asr_payload = fixed_asr_payload(asr_input)
    llm_payload = fixed_llm_payload(llm_input)
    vlm_payload = fixed_c100_payload(vlm_input)
    asr = dict(
        asr_preflight
        if asr_preflight is not None
        else build_asr_preflight(
            root,
            input_payload=asr_payload,
            expected_branch="main",
            base_preflight=base,
        )
    )
    llm = dict(
        llm_preflight
        if llm_preflight is not None
        else build_llm_preflight(
            root,
            input_payload=llm_payload,
            expected_branch="main",
            base_preflight=base,
        )
    )
    vlm = dict(
        vlm_preflight
        if vlm_preflight is not None
        else build_vlm_preflight(
            root,
            input_payload=vlm_payload,
            expected_branch="main",
            base_preflight=base,
        )
    )
    ollama = dict(ollama_identity or _ollama_identity())
    services = dict(service_identity or _service_identity())
    environment = base.get("environment")
    environment_record = environment if isinstance(environment, Mapping) else {}
    git = environment_record.get("git")
    git_record = git if isinstance(git, Mapping) else {}
    packages = environment_record.get("jetpack_packages")
    package_record = packages if isinstance(packages, Mapping) else {}
    nvpmodel = environment_record.get("nvpmodel")
    power_record = nvpmodel if isinstance(nvpmodel, Mapping) else {}
    llm_runtime = llm.get("runtime")
    llm_runtime_record = llm_runtime if isinstance(llm_runtime, Mapping) else {}
    vlm_services = vlm.get("services")
    vlm_services_record = vlm_services if isinstance(vlm_services, Mapping) else {}
    observed_ollama = vlm_services_record.get("ollama")
    observed_ollama_record = (
        observed_ollama if isinstance(observed_ollama, Mapping) else {}
    )
    observed_qwen = vlm_services_record.get("qwen")
    observed_qwen_record = observed_qwen if isinstance(observed_qwen, Mapping) else {}
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
        if protocol_file == expected_protocol_file
        else {"returncode": 1, "output": "", "error_code": "protocol_path"}
    )
    protocol_errors = formal_protocol_errors(protocol)
    digest = protocol_sha256(protocol) if not protocol_errors else None
    version_output = str(ollama.get("version_output") or "")
    power_output = str(power_record.get("output") or "")
    package_output = package_record.get("output")
    workload_errors = {
        "asr": asr_preflight_errors(asr),
        "llm": llm_preflight_errors(llm),
        "vlm": vlm_preflight_errors(vlm),
    }
    checks = [
        _check(
            "protocol_identity",
            not protocol_errors
            and digest == FROZEN_PROTOCOL_SHA256
            and protocol_file == expected_protocol_file,
            observed={
                "sha256": digest,
                "errors": protocol_errors,
                "tracked_path": protocol_file == expected_protocol_file,
            },
            requirement="the tracked protocol matches the activated G6 identity",
        ),
        _check(
            "protocol_activated",
            protocol_commit.get("returncode") == 0
            and re.fullmatch(r"[0-9a-f]{40}", str(protocol_commit.get("output")))
            is not None
            and re.fullmatch(r"[0-9a-f]{40}", str(git_record.get("commit")))
            is not None,
            observed={
                "protocol_commit": protocol_commit.get("output"),
                "runner_commit": git_record.get("commit"),
            },
            requirement="the protocol and runner are committed on synchronized main",
        ),
        _check(
            "python_version",
            environment_record.get("python") == "3.10.12",
            observed=environment_record.get("python"),
            requirement="Python is exactly 3.10.12",
        ),
        _check(
            "jetpack_version",
            _package_present(package_output, "nvidia-jetpack", "6.2.2+b24"),
            observed=package_output,
            requirement="nvidia-jetpack is exactly 6.2.2+b24",
        ),
        _check(
            "l4t_core_version",
            _package_present(
                package_output,
                "nvidia-l4t-core",
                "36.5.0-20260115194252",
            ),
            observed=package_output,
            requirement="nvidia-l4t-core matches the preregistered version",
        ),
        _check(
            "power_mode",
            "MAXN_SUPER" in power_output
            and any(line.strip() == "2" for line in power_output.splitlines()),
            observed=power_output,
            requirement="nvpmodel reports MAXN_SUPER mode 2",
        ),
        _check(
            "dynamic_dvfs_confirmed",
            dynamic_dvfs_confirmed,
            observed=dynamic_dvfs_confirmed,
            requirement="the operator confirmed jetson_clocks is not enabled",
        ),
        _check(
            "services_restarted",
            services_restarted and _service_identity_valid(services),
            observed={"confirmed": services_restarted, "identity": services},
            requirement="model services were restarted before this session",
        ),
        _check(
            "ollama_version",
            version_output
            in {
                f"ollama version is {VLM_OLLAMA_VERSION}",
                f"ollama version {VLM_OLLAMA_VERSION}",
            },
            observed=version_output,
            requirement=f"Ollama is exactly {VLM_OLLAMA_VERSION}",
        ),
        _check(
            "ollama_binary_identity",
            ollama.get("binary_sha256") == VLM_OLLAMA_BINARY_SHA256
            and ollama.get("executable_path_recorded") is False,
            observed={
                "sha256": ollama.get("binary_sha256"),
                "path_recorded": ollama.get("executable_path_recorded"),
            },
            requirement="the Ollama executable matches the frozen hash",
        ),
        _check(
            "moondream_digest",
            observed_ollama_record.get("model_digest") == VLM_MOONDREAM_DIGEST,
            observed=observed_ollama_record.get("model_digest"),
            requirement="Moondream matches the frozen model digest",
        ),
        _check(
            "llama_source_version",
            llm_runtime_record.get("source_version") == LLAMA_SOURCE_VERSION,
            observed=llm_runtime_record.get("source_version"),
            requirement="llama.cpp matches the frozen source version",
        ),
        _check(
            "qwen_served_identity",
            llm_runtime_record.get("served_model_ids") == [LLM_EXPECTED_SERVED_MODEL_ID]
            and observed_qwen_record.get("served_model_ids")
            == [LLM_EXPECTED_SERVED_MODEL_ID],
            observed={
                "llm": llm_runtime_record.get("served_model_ids"),
                "vlm": observed_qwen_record.get("served_model_ids"),
            },
            requirement="both adapters resolve the frozen Qwen model",
        ),
        _check(
            "unrelated_inference_absent",
            ollama.get("active_model_count") == 0
            and ollama.get("active_model_names_recorded") is False
            and ollama.get("error_code") is None,
            observed={"ollama_active_model_count": ollama.get("active_model_count")},
            requirement="no unrelated model is loaded in Ollama",
        ),
        _check(
            "workload_preflights",
            all(not errors for errors in workload_errors.values()),
            observed=workload_errors,
            requirement="ASR, LLM and VLM preflights all pass",
        ),
    ]
    return {
        "formal_preflight_schema_version": FORMAL_PREFLIGHT_SCHEMA_VERSION,
        "captured_at": utc_now_iso(),
        "protocol": {
            "id": protocol.get("protocol_id"),
            "sha256": digest,
            "protocol_commit": protocol_commit.get("output"),
            "runner_commit": git_record.get("commit"),
            "path_recorded": False,
        },
        "base": base,
        "workloads": {"asr": asr, "llm": llm, "vlm": vlm},
        "ollama": ollama,
        "service_identity": services,
        "checks": checks,
        "eligible": all(check["passed"] is True for check in checks),
    }


def formal_preflight_errors(preflight: Mapping[str, object]) -> list[str]:
    errors: list[str] = []
    if (
        preflight.get("formal_preflight_schema_version")
        != FORMAL_PREFLIGHT_SCHEMA_VERSION
    ):
        errors.append("unsupported formal preflight schema version")
    base = preflight.get("base")
    if not isinstance(base, Mapping):
        errors.append("formal preflight base record is missing")
    else:
        errors.extend(f"base: {error}" for error in preflight_errors(base))
    checks = preflight.get("checks")
    by_name: dict[str, Mapping[str, object]] = {}
    if not isinstance(checks, list):
        errors.append("formal preflight checks are missing")
    else:
        for item in checks:
            if not isinstance(item, Mapping):
                errors.append("formal preflight check is not an object")
                continue
            name = item.get("name")
            if not isinstance(name, str) or name in by_name:
                errors.append(f"formal preflight check name is invalid: {name!r}")
                continue
            by_name[name] = item
            if item.get("required") is not True or item.get("passed") is not True:
                errors.append(f"formal preflight check failed: {name}")
    if set(by_name) != _REQUIRED_CHECKS:
        errors.append("formal preflight check set is incomplete or unsupported")
    protocol = preflight.get("protocol")
    if not isinstance(protocol, Mapping):
        errors.append("formal protocol identity is missing")
    elif protocol.get("sha256") != FROZEN_PROTOCOL_SHA256:
        errors.append("formal protocol hash does not match G6")
    if preflight.get("eligible") is not (not errors):
        errors.append("formal preflight eligibility is inconsistent")
    return errors
