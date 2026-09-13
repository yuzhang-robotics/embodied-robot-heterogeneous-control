"""Fail-closed eligibility checks for the Phase 2 correctness pilot."""

from __future__ import annotations

import hashlib
import os
import re
import stat
from collections.abc import Callable, Mapping
from pathlib import Path

from experiments.phase1.common.manifest import (
    command_snapshot,
    sha256_file,
    utc_now_iso,
)
from experiments.phase1.common.preflight import (
    build_jetson_preflight,
    preflight_errors as base_preflight_errors,
)
from experiments.phase1.formal.preflight import (
    _ollama_identity,
    _package_present,
    _service_identity,
    _service_identity_valid,
)
from experiments.phase1.formal.protocol import (
    LLAMA_SOURCE_VERSION,
    VLM_MOONDREAM_DIGEST,
    VLM_OLLAMA_BINARY_SHA256,
    VLM_OLLAMA_VERSION,
)
from experiments.phase1.workloads.asr.adapter import (
    ASR_MODEL_SHA256,
    ASR_MODEL_SIZE_BYTES,
    fixed_asr_payload,
    load_phase0_asr_runtime,
)
from experiments.phase1.workloads.asr.preflight import (
    asr_preflight_errors,
    build_asr_preflight,
)
from experiments.phase1.workloads.llm.adapter import (
    LLM_EXPECTED_SERVED_MODEL_ID,
    LLM_MODEL_SHA256,
    LLM_MODEL_SIZE_BYTES,
    LLM_SERVER_ARGUMENTS,
)
from experiments.phase1.workloads.llm.preflight import probe_llm_runtime
from experiments.phase1.workloads.vlm.adapter import fixed_c100_payload
from experiments.phase1.workloads.vlm.preflight import (
    build_vlm_preflight,
    vlm_preflight_errors,
)
from experiments.phase2.correctness import (
    CORRECTNESS_PROTOCOL_ID,
    CORRECTNESS_PROTOCOL_SHA256,
    DEFAULT_PROTOCOL_PATH,
    protocol_errors,
    protocol_sha256,
)


CORRECTNESS_PREFLIGHT_SCHEMA_VERSION = "0.1.0"
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
    "whisper_prefetch_file_identity",
    "vlm_model_identity",
    "qwen_runtime_identity",
    "unrelated_inference_absent",
    "workload_preflights",
    "resource_and_safety_prerequisites",
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


def _publish_service_identity(value: Mapping[str, object]) -> dict[str, object]:
    """Publish restart-comparable service identities without retaining PIDs."""

    published: dict[str, object] = {}
    for name in ("llama-server", "ollama"):
        service = value.get(name)
        record = service if isinstance(service, Mapping) else {}
        identities = record.get("process_start_identities")
        identity = (
            identities[0] if isinstance(identities, list) and identities else None
        )
        published[name] = {
            "process_count": record.get("process_count"),
            "process_start_identity_sha256": (
                hashlib.sha256(identity.encode("utf-8")).hexdigest()
                if isinstance(identity, str) and identity
                else None
            ),
            "arguments_recorded": False,
            "pid_recorded": False,
        }
    return published


def probe_whisper_file_identity(
    *,
    runtime_loader: Callable[[], object] = load_phase0_asr_runtime,
    hasher: Callable[[Path], str] = sha256_file,
) -> dict[str, object]:
    """Record the prefetch target identity without publishing its path."""

    try:
        runtime = runtime_loader()
        model_path = Path(getattr(runtime, "whisper_model"))
        file_stat = model_path.stat()
        filesystem = os.statvfs(model_path)
        page_size = int(os.sysconf("SC_PAGE_SIZE"))
        digest = hasher(model_path)
    except (AttributeError, OSError, TypeError, ValueError) as exc:
        return {
            "regular_file": False,
            "size_bytes": None,
            "sha256": None,
            "page_size_bytes": None,
            "filesystem_block_size_bytes": None,
            "device_major": None,
            "device_minor": None,
            "path_recorded": False,
            "error_code": "model_identity_" + type(exc).__name__.lower(),
        }
    return {
        "regular_file": stat.S_ISREG(file_stat.st_mode),
        "size_bytes": file_stat.st_size,
        "sha256": digest,
        "page_size_bytes": page_size,
        "filesystem_block_size_bytes": int(filesystem.f_frsize),
        "device_major": os.major(file_stat.st_dev),
        "device_minor": os.minor(file_stat.st_dev),
        "path_recorded": False,
        "error_code": None,
    }


def build_correctness_preflight(
    repo_root: Path | str,
    protocol: Mapping[str, object],
    *,
    asr_input: Path | str,
    vlm_input: Path | str,
    services_restarted: bool,
    dynamic_dvfs_confirmed: bool,
    protocol_path: Path | str = DEFAULT_PROTOCOL_PATH,
    base_preflight: Mapping[str, object] | None = None,
    asr_preflight: Mapping[str, object] | None = None,
    vlm_preflight: Mapping[str, object] | None = None,
    llm_runtime: Mapping[str, object] | None = None,
    ollama_identity: Mapping[str, object] | None = None,
    service_identity: Mapping[str, object] | None = None,
    whisper_file_identity: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Bind one nonformal pair to the reviewed environment and fixed identities."""

    if not isinstance(services_restarted, bool):
        raise TypeError("services_restarted must be boolean")
    if not isinstance(dynamic_dvfs_confirmed, bool):
        raise TypeError("dynamic_dvfs_confirmed must be boolean")
    root = Path(repo_root).resolve()
    protocol_file = Path(protocol_path).resolve()
    tracked_protocol = (
        root / "experiments" / "phase2" / DEFAULT_PROTOCOL_PATH.name
    ).resolve()
    base = dict(
        base_preflight
        if base_preflight is not None
        else build_jetson_preflight(root, expected_branch="main")
    )
    asr_payload = fixed_asr_payload(asr_input)
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
    qwen = dict(llm_runtime if llm_runtime is not None else probe_llm_runtime())
    ollama = dict(
        ollama_identity if ollama_identity is not None else _ollama_identity()
    )
    private_services = dict(
        service_identity if service_identity is not None else _service_identity()
    )
    services = _publish_service_identity(private_services)
    whisper = dict(
        whisper_file_identity
        if whisper_file_identity is not None
        else probe_whisper_file_identity()
    )

    environment = base.get("environment")
    environment_record = environment if isinstance(environment, Mapping) else {}
    safety = base.get("safety")
    safety_record = safety if isinstance(safety, Mapping) else {}
    git = environment_record.get("git")
    git_record = git if isinstance(git, Mapping) else {}
    packages = environment_record.get("jetpack_packages")
    package_record = packages if isinstance(packages, Mapping) else {}
    nvpmodel = environment_record.get("nvpmodel")
    power_record = nvpmodel if isinstance(nvpmodel, Mapping) else {}
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
        if protocol_file == tracked_protocol
        else {"returncode": 1, "output": "", "error_code": "protocol_path"}
    )

    contract_errors = protocol_errors(protocol)
    digest = protocol_sha256(protocol) if not contract_errors else None
    package_output = package_record.get("output")
    power_output = str(power_record.get("output") or "")
    workload_errors = {
        "asr": asr_preflight_errors(asr),
        "vlm": vlm_preflight_errors(vlm),
    }
    qwen_model_matches = (
        qwen.get("model_size_bytes") == LLM_MODEL_SIZE_BYTES
        and qwen.get("model_sha256") == LLM_MODEL_SHA256
        and qwen.get("source_version") == LLAMA_SOURCE_VERSION
        and qwen.get("source_clean") is True
        and qwen.get("server_process_count") == 1
        and qwen.get("server_arguments") == dict(LLM_SERVER_ARGUMENTS)
        and qwen.get("server_arguments_match") is True
        and qwen.get("server_model_path_matches") is True
        and qwen.get("endpoint_local") is True
        and qwen.get("listener_loopback_only") is True
        and qwen.get("service_reachable") is True
        and qwen.get("served_model_ids") == [LLM_EXPECTED_SERVED_MODEL_ID]
        and qwen.get("expected_model_present") is True
        and qwen.get("error_code") is None
    )
    whisper_matches = (
        whisper.get("regular_file") is True
        and whisper.get("size_bytes") == ASR_MODEL_SIZE_BYTES
        and whisper.get("sha256") == ASR_MODEL_SHA256
        and isinstance(whisper.get("page_size_bytes"), int)
        and whisper.get("page_size_bytes", 0) > 0
        and isinstance(whisper.get("filesystem_block_size_bytes"), int)
        and whisper.get("filesystem_block_size_bytes", 0) > 0
        and isinstance(whisper.get("device_major"), int)
        and whisper.get("device_major", -1) >= 0
        and isinstance(whisper.get("device_minor"), int)
        and whisper.get("device_minor", -1) >= 0
        and whisper.get("path_recorded") is False
        and whisper.get("error_code") is None
    )
    checks = [
        _check(
            "protocol_identity",
            not contract_errors
            and digest == CORRECTNESS_PROTOCOL_SHA256
            and protocol_file == tracked_protocol
            and protocol_commit.get("returncode") == 0
            and re.fullmatch(r"[0-9a-f]{40}", str(protocol_commit.get("output")))
            is not None,
            {
                "id": protocol.get("protocol_id"),
                "sha256": digest,
                "errors": contract_errors,
                "tracked_path": protocol_file == tracked_protocol,
                "protocol_commit": protocol_commit.get("output"),
                "runner_commit": git_record.get("commit"),
            },
            "the reviewed Phase 2 correctness contract is tracked and committed",
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
            "nvidia-l4t-core matches the frozen target",
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
            services_restarted and _service_identity_valid(private_services),
            {"confirmed": services_restarted, "identity": services},
            "the expected model services were restarted before the pilot",
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
            "the Ollama executable matches the frozen identity",
        ),
        _check(
            "whisper_prefetch_file_identity",
            whisper_matches,
            whisper,
            "the prefetch target is the frozen regular Whisper model file",
        ),
        _check(
            "vlm_model_identity",
            observed_ollama_record.get("model_digest") == VLM_MOONDREAM_DIGEST,
            observed_ollama_record.get("model_digest"),
            "Moondream matches the frozen model digest",
        ),
        _check(
            "qwen_runtime_identity",
            qwen_model_matches
            and observed_qwen_record.get("served_model_ids")
            == [LLM_EXPECTED_SERVED_MODEL_ID],
            {
                "runtime_matches": qwen_model_matches,
                "vlm_served_model_ids": observed_qwen_record.get("served_model_ids"),
            },
            "the local rewrite service and model match the frozen identity",
        ),
        _check(
            "unrelated_inference_absent",
            ollama.get("active_model_count") == 0
            and ollama.get("active_model_names_recorded") is False
            and ollama.get("error_code") is None,
            {"ollama_active_model_count": ollama.get("active_model_count")},
            "no unrelated Ollama model is active",
        ),
        _check(
            "workload_preflights",
            all(not errors for errors in workload_errors.values()),
            workload_errors,
            "the fixed-input ASR and VLM preflights both pass",
        ),
        _check(
            "resource_and_safety_prerequisites",
            not base_preflight_errors(base)
            and str(safety_record.get("robot_enable_motion_raw")) == "0",
            {
                "base_errors": base_preflight_errors(base),
                "motion_value_is_explicit_zero": str(
                    safety_record.get("robot_enable_motion_raw")
                )
                == "0",
            },
            "Linux ARM64, clean synchronized main, telemetry and explicit no-motion checks pass",
        ),
    ]
    return {
        "correctness_preflight_schema_version": CORRECTNESS_PREFLIGHT_SCHEMA_VERSION,
        "captured_at": utc_now_iso(),
        "protocol": {
            "id": protocol.get("protocol_id"),
            "sha256": digest,
            "protocol_commit": protocol_commit.get("output"),
            "runner_commit": git_record.get("commit"),
            "path_recorded": False,
        },
        "base": base,
        "workloads": {"asr": asr, "vlm": vlm},
        "qwen_runtime": qwen,
        "ollama": ollama,
        "service_identity": services,
        "whisper_file": whisper,
        "checks": checks,
        "eligible": all(check["passed"] is True for check in checks),
        "formal_evidence": False,
        "application_slice_authorized": False,
    }


def correctness_preflight_errors(preflight: Mapping[str, object]) -> list[str]:
    errors: list[str] = []
    if (
        preflight.get("correctness_preflight_schema_version")
        != CORRECTNESS_PREFLIGHT_SCHEMA_VERSION
    ):
        errors.append("unsupported correctness preflight schema version")
    base = preflight.get("base")
    if not isinstance(base, Mapping):
        errors.append("correctness preflight base record is missing")
    else:
        errors.extend(f"base: {item}" for item in base_preflight_errors(base))
    workloads = preflight.get("workloads")
    if not isinstance(workloads, Mapping) or set(workloads) != {"asr", "vlm"}:
        errors.append("correctness workload preflights are missing")
    else:
        asr = workloads.get("asr")
        vlm = workloads.get("vlm")
        if not isinstance(asr, Mapping):
            errors.append("ASR preflight is missing")
        else:
            errors.extend(f"asr: {item}" for item in asr_preflight_errors(asr))
        if not isinstance(vlm, Mapping):
            errors.append("VLM preflight is missing")
        else:
            errors.extend(f"vlm: {item}" for item in vlm_preflight_errors(vlm))

    checks = preflight.get("checks")
    by_name: dict[str, Mapping[str, object]] = {}
    if not isinstance(checks, list):
        errors.append("correctness preflight checks are missing")
    else:
        for item in checks:
            if not isinstance(item, Mapping):
                errors.append("correctness preflight check is not an object")
                continue
            name = item.get("name")
            if not isinstance(name, str) or name in by_name:
                errors.append(f"correctness preflight check name is invalid: {name!r}")
                continue
            by_name[name] = item
            if item.get("required") is not True or item.get("passed") is not True:
                errors.append(f"correctness preflight check failed: {name}")
    if set(by_name) != _REQUIRED_CHECKS:
        errors.append("correctness preflight check set is incomplete or unsupported")

    protocol = preflight.get("protocol")
    if not isinstance(protocol, Mapping):
        errors.append("correctness protocol identity is missing")
    elif (
        protocol.get("id") != CORRECTNESS_PROTOCOL_ID
        or protocol.get("sha256") != CORRECTNESS_PROTOCOL_SHA256
        or protocol.get("path_recorded") is not False
    ):
        errors.append("correctness protocol identity is invalid")
    whisper = preflight.get("whisper_file")
    if not isinstance(whisper, Mapping) or (
        whisper.get("size_bytes") != ASR_MODEL_SIZE_BYTES
        or whisper.get("sha256") != ASR_MODEL_SHA256
        or whisper.get("regular_file") is not True
        or whisper.get("path_recorded") is not False
    ):
        errors.append("correctness Whisper file identity is invalid")
    services = preflight.get("service_identity")
    if not isinstance(services, Mapping) or set(services) != {
        "llama-server",
        "ollama",
    }:
        errors.append("correctness service identity is invalid")
    else:
        for name in ("llama-server", "ollama"):
            service = services.get(name)
            record = service if isinstance(service, Mapping) else {}
            if (
                record.get("process_count") != 1
                or re.fullmatch(
                    r"[0-9a-f]{64}",
                    str(record.get("process_start_identity_sha256")),
                )
                is None
                or record.get("arguments_recorded") is not False
                or record.get("pid_recorded") is not False
            ):
                errors.append(f"correctness service identity is invalid: {name}")
    if preflight.get("formal_evidence") is not False:
        errors.append("correctness pilot cannot claim formal evidence")
    if preflight.get("application_slice_authorized") is not False:
        errors.append("correctness pilot cannot authorize application integration")
    if preflight.get("eligible") is not (not errors):
        errors.append("correctness preflight eligibility is inconsistent")
    return errors
