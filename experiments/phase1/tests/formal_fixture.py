from __future__ import annotations

from experiments.phase1.formal_preflight import FROZEN_PROTOCOL_SHA256
from experiments.phase1.formal_protocol import FORMAL_PROTOCOL_ID


BASE_CHECKS = (
    "motion_disabled",
    "linux_platform",
    "arm64_machine",
    "l4t_release_present",
    "tegrastats_available",
    "git_identity_complete",
    "git_tree_clean",
    "expected_branch",
    "git_upstream_synchronized",
    "forbidden_modules_absent",
)
FORMAL_CHECKS = (
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
)


def passing_base_preflight(commit: str = "1" * 40) -> dict[str, object]:
    safety = {
        "motion_environment_variable": "ROBOT_ENABLE_MOTION",
        "motion_environment_value": "0",
        "motion_enabled": False,
        "motion_value_valid": True,
    }
    environment = {
        "platform": "Linux-5.15-aarch64",
        "machine": "aarch64",
        "python": "3.10.12",
        "l4t_release": "R36",
        "git": {
            "commit": commit,
            "branch": "main",
            "dirty": False,
            "status_porcelain": [],
            "upstream": "origin/main",
            "upstream_commit": commit,
            "ahead_behind": "0 0",
            "error_codes": [],
        },
        "jetpack_packages": {
            "returncode": 0,
            "output": (
                "nvidia-jetpack\t6.2.2+b24\n" "nvidia-l4t-core\t36.5.0-20260115194252"
            ),
            "error_code": None,
        },
        "nvpmodel": {
            "returncode": 0,
            "output": "NV Power Mode: MAXN_SUPER\n2",
            "error_code": None,
        },
        "jetson_clocks": {"returncode": 0, "output": "", "error_code": None},
    }
    checks = []
    for name in BASE_CHECKS:
        observed: object = True
        if name == "forbidden_modules_absent":
            observed = []
        checks.append(
            {
                "name": name,
                "required": True,
                "passed": True,
                "observed": observed,
                "requirement": "fixture",
            }
        )
    return {
        "preflight_schema_version": "0.1.0",
        "captured_at": "2026-09-02T00:00:00Z",
        "expected_branch": "main",
        "safety": safety,
        "environment": environment,
        "checks": checks,
        "eligible": True,
    }


def passing_formal_preflight(
    *,
    commit: str = "1" * 40,
    service_suffix: str = "a",
    protocol_id: str = FORMAL_PROTOCOL_ID,
    protocol_sha256: str = FROZEN_PROTOCOL_SHA256,
) -> dict[str, object]:
    services = {
        "llama-server": {
            "process_count": 1,
            "process_start_identities": [f"1 Tue Sep 2 00:00:00 2026 {service_suffix}"],
            "arguments_recorded": False,
        },
        "ollama": {
            "process_count": 1,
            "process_start_identities": [f"2 Tue Sep 2 00:00:00 2026 {service_suffix}"],
            "arguments_recorded": False,
        },
    }
    return {
        "formal_preflight_schema_version": "0.1.0",
        "captured_at": "2026-09-02T00:00:00Z",
        "protocol": {
            "id": protocol_id,
            "sha256": protocol_sha256,
            "protocol_commit": "2" * 40,
            "runner_commit": commit,
            "path_recorded": False,
        },
        "base": passing_base_preflight(commit),
        "workloads": {"asr": {}, "llm": {}, "vlm": {}},
        "ollama": {
            "version_output": "ollama version is 0.24.0",
            "binary_sha256": (
                "6273a99e321b5e69741aa024cc22e0ce2803aa2bdf20185ea19627b4d891c87a"
            ),
            "executable_path_recorded": False,
            "active_model_count": 0,
            "active_model_names_recorded": False,
            "error_code": None,
        },
        "service_identity": services,
        "checks": [
            {
                "name": name,
                "required": True,
                "passed": True,
                "observed": True,
                "requirement": "fixture",
            }
            for name in FORMAL_CHECKS
        ],
        "eligible": True,
    }
