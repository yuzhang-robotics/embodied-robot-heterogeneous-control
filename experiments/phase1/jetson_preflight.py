"""Fail-closed environment checks for the Phase 1 Jetson simulation pilot."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import Iterable, Mapping

from experiments.phase1.manifest import (
    collect_environment,
    command_snapshot,
    motion_environment,
    utc_now_iso,
)


PREFLIGHT_SCHEMA_VERSION = "0.1.0"
FORBIDDEN_MODULES = (
    "jetson.app",
    "jetson.motion_planner",
    "jetson.robot_comm",
)
_REQUIRED_CHECKS = {
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
}


def collect_jetson_environment(repo_root: Path | str) -> dict[str, object]:
    """Collect read-only platform facts without importing device modules."""

    environment = collect_environment(repo_root)
    environment["jetpack_packages"] = command_snapshot(
        [
            "dpkg-query",
            "-W",
            "-f=${Package}\\t${Version}\\n",
            "nvidia-jetpack",
            "nvidia-l4t-core",
        ]
    )
    environment["nvpmodel"] = command_snapshot(["nvpmodel", "-q"])
    environment["jetson_clocks"] = command_snapshot(["jetson_clocks", "--show"])
    return environment


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


def build_jetson_preflight(
    repo_root: Path | str,
    *,
    expected_branch: str = "main",
    environment: Mapping[str, object] | None = None,
    tegrastats_available: bool | None = None,
    loaded_modules: Iterable[str] | None = None,
) -> dict[str, object]:
    """Build one serializable pilot preflight record."""

    if not isinstance(expected_branch, str) or not expected_branch:
        raise ValueError("expected_branch must be a non-empty string")
    captured_environment = dict(
        environment
        if environment is not None
        else collect_jetson_environment(repo_root)
    )
    safety = motion_environment()
    git = captured_environment.get("git")
    git_record = git if isinstance(git, Mapping) else {}
    machine = str(captured_environment.get("machine", "")).lower()
    platform_name = str(captured_environment.get("platform", ""))
    l4t_release = str(captured_environment.get("l4t_release", ""))
    available = (
        bool(shutil.which("tegrastats"))
        if tegrastats_available is None
        else tegrastats_available
    )
    if not isinstance(available, bool):
        raise TypeError("tegrastats_available must be boolean")
    module_names = set(loaded_modules if loaded_modules is not None else sys.modules)
    forbidden_loaded = sorted(
        module
        for module in module_names
        if module in FORBIDDEN_MODULES
        or any(module.startswith(name + ".") for name in FORBIDDEN_MODULES)
    )
    git_identity_complete = (
        not git_record.get("error_codes")
        and bool(git_record.get("commit"))
        and bool(git_record.get("branch"))
    )

    checks = [
        _check(
            "motion_disabled",
            safety.get("motion_enabled") is False
            and safety.get("motion_value_valid") is True,
            observed=safety,
            requirement="ROBOT_ENABLE_MOTION is unset or explicitly false",
        ),
        _check(
            "linux_platform",
            platform_name.lower().startswith("linux"),
            observed=platform_name,
            requirement="the pilot runs on Linux",
        ),
        _check(
            "arm64_machine",
            machine in {"aarch64", "arm64"},
            observed=machine,
            requirement="the pilot runs on an ARM64 Jetson host",
        ),
        _check(
            "l4t_release_present",
            bool(l4t_release),
            observed=bool(l4t_release),
            requirement="the NVIDIA L4T release file is present",
        ),
        _check(
            "tegrastats_available",
            available,
            observed=available,
            requirement="tegrastats is available in PATH",
        ),
        _check(
            "git_identity_complete",
            git_identity_complete,
            observed={
                "commit_present": bool(git_record.get("commit")),
                "branch": git_record.get("branch"),
                "error_codes": git_record.get("error_codes"),
            },
            requirement="a commit and named branch identify the source tree",
        ),
        _check(
            "git_tree_clean",
            git_identity_complete and git_record.get("dirty") is False,
            observed=git_record.get("dirty"),
            requirement="the Git tree is clean before pilot artifacts are created",
        ),
        _check(
            "expected_branch",
            git_record.get("branch") == expected_branch,
            observed=git_record.get("branch"),
            requirement=f"the pilot runs from the reviewed {expected_branch} branch",
        ),
        _check(
            "git_upstream_synchronized",
            git_record.get("upstream") == f"origin/{expected_branch}"
            and git_record.get("upstream_commit") == git_record.get("commit")
            and str(git_record.get("ahead_behind", "")).split() == ["0", "0"],
            observed={
                "upstream": git_record.get("upstream"),
                "same_commit": git_record.get("upstream_commit")
                == git_record.get("commit"),
                "ahead_behind": git_record.get("ahead_behind"),
            },
            requirement=f"local {expected_branch} matches origin/{expected_branch}",
        ),
        _check(
            "forbidden_modules_absent",
            not forbidden_loaded,
            observed=forbidden_loaded,
            requirement="robot application, motion planner and UART modules are not loaded",
        ),
    ]
    return {
        "preflight_schema_version": PREFLIGHT_SCHEMA_VERSION,
        "captured_at": utc_now_iso(),
        "expected_branch": expected_branch,
        "safety": safety,
        "environment": captured_environment,
        "checks": checks,
        "eligible": all(check["passed"] is True for check in checks),
    }


def preflight_errors(preflight: Mapping[str, object]) -> list[str]:
    errors: list[str] = []
    if preflight.get("preflight_schema_version") != PREFLIGHT_SCHEMA_VERSION:
        errors.append("unsupported preflight schema version")
    checks = preflight.get("checks")
    if not isinstance(checks, list) or not checks:
        errors.append("preflight contains no checks")
        return errors
    check_by_name: dict[str, Mapping[str, object]] = {}
    for item in checks:
        if not isinstance(item, Mapping):
            errors.append("preflight check is not an object")
            continue
        name = item.get("name")
        if not isinstance(name, str) or name in check_by_name:
            errors.append(f"preflight check name is invalid or duplicated: {name!r}")
            continue
        check_by_name[name] = item
        if item.get("required") is not True or item.get("passed") is not True:
            errors.append(f"preflight check failed: {name}")
    if set(check_by_name) != _REQUIRED_CHECKS:
        errors.append("preflight check set is incomplete or unsupported")

    safety = preflight.get("safety")
    environment = preflight.get("environment")
    git = environment.get("git") if isinstance(environment, Mapping) else None
    expected_branch = preflight.get("expected_branch")
    if not isinstance(safety, Mapping):
        errors.append("preflight safety record is missing")
    elif (
        safety.get("motion_enabled") is not False
        or safety.get("motion_value_valid") is not True
    ):
        errors.append("preflight safety record does not prove motion is disabled")
    if not isinstance(environment, Mapping):
        errors.append("preflight environment record is missing")
    else:
        if not str(environment.get("platform", "")).lower().startswith("linux"):
            errors.append("preflight environment is not Linux")
        if str(environment.get("machine", "")).lower() not in {"aarch64", "arm64"}:
            errors.append("preflight environment is not ARM64")
        if not environment.get("l4t_release"):
            errors.append("preflight environment has no L4T release")
    if not isinstance(git, Mapping):
        errors.append("preflight Git record is missing")
    else:
        if git.get("error_codes") or not git.get("commit") or not git.get("branch"):
            errors.append("preflight Git identity is incomplete")
        if git.get("dirty") is not False:
            errors.append("preflight Git tree is not clean")
        if not isinstance(expected_branch, str) or git.get("branch") != expected_branch:
            errors.append("preflight Git branch does not match the expected branch")
        if (
            not isinstance(expected_branch, str)
            or git.get("upstream") != f"origin/{expected_branch}"
            or git.get("upstream_commit") != git.get("commit")
            or str(git.get("ahead_behind", "")).split() != ["0", "0"]
        ):
            errors.append("preflight Git branch is not synchronized with its upstream")
    forbidden_check = check_by_name.get("forbidden_modules_absent")
    if forbidden_check is None or forbidden_check.get("observed") != []:
        errors.append("preflight does not prove forbidden modules were absent")
    tegrastats_check = check_by_name.get("tegrastats_available")
    if tegrastats_check is None or tegrastats_check.get("observed") is not True:
        errors.append("preflight does not prove tegrastats was available")

    expected_eligibility = not errors
    if preflight.get("eligible") is not expected_eligibility:
        errors.append("preflight eligibility is inconsistent")
    return errors
