"""Reproducibility and safety helpers for Phase 1 simulation runs."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


MANIFEST_SCHEMA_VERSION = "0.1.0"
_MOTION_TRUE_VALUES = {"1", "true", "yes", "on"}
_MOTION_FALSE_VALUES = {"", "0", "false", "no", "off"}


class SafetyError(RuntimeError):
    """The process environment does not prove motion is disabled."""


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def motion_environment() -> dict[str, object]:
    raw = os.environ.get("ROBOT_ENABLE_MOTION")
    normalized = raw.strip().lower() if raw is not None else None
    valid = (
        normalized is None
        or normalized in _MOTION_TRUE_VALUES
        or normalized in _MOTION_FALSE_VALUES
    )
    enabled = normalized in _MOTION_TRUE_VALUES if normalized is not None else False
    return {
        "robot_enable_motion_raw": raw,
        "motion_enabled": enabled,
        "motion_value_valid": valid,
    }


def require_motion_disabled() -> dict[str, object]:
    """Return the captured setting or fail closed before creating a run."""

    safety = motion_environment()
    if safety["motion_value_valid"] is not True:
        raise SafetyError("ROBOT_ENABLE_MOTION contains an unrecognized value")
    if safety["motion_enabled"] is not False:
        raise SafetyError("Phase 1 simulation refuses to run with motion enabled")
    return safety


def sha256_file(path: Path | str) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json_atomic(path: Path | str, data: dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, target)


def command_snapshot(
    command: list[str],
    *,
    cwd: Path | str | None = None,
    timeout_s: float = 5.0,
) -> dict[str, object]:
    try:
        result = subprocess.run(
            command,
            cwd=str(cwd) if cwd is not None else None,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            timeout=timeout_s,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "returncode": None,
            "output": "",
            "error_code": type(exc).__name__.lower(),
        }
    return {
        "returncode": result.returncode,
        "output": result.stdout.strip(),
        "error_code": None,
    }


def git_snapshot(repo_root: Path | str) -> dict[str, object]:
    root = Path(repo_root)
    commit = command_snapshot(["git", "rev-parse", "HEAD"], cwd=root)
    branch = command_snapshot(["git", "branch", "--show-current"], cwd=root)
    status = command_snapshot(["git", "status", "--porcelain"], cwd=root)
    upstream = command_snapshot(
        ["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"],
        cwd=root,
    )
    upstream_commit = command_snapshot(["git", "rev-parse", "@{upstream}"], cwd=root)
    ahead_behind = command_snapshot(
        ["git", "rev-list", "--left-right", "--count", "HEAD...@{upstream}"],
        cwd=root,
    )
    status_output = status["output"] if isinstance(status["output"], str) else ""
    return {
        "commit": commit["output"],
        "branch": branch["output"],
        "dirty": bool(status_output),
        "status_porcelain": status_output.splitlines(),
        "upstream": upstream["output"] if upstream["returncode"] == 0 else "",
        "upstream_commit": (
            upstream_commit["output"] if upstream_commit["returncode"] == 0 else ""
        ),
        "ahead_behind": (
            ahead_behind["output"] if ahead_behind["returncode"] == 0 else ""
        ),
        "error_codes": [
            value
            for value in (
                commit["error_code"],
                branch["error_code"],
                status["error_code"],
            )
            if value is not None
        ],
    }


def collect_environment(repo_root: Path | str) -> dict[str, object]:
    l4t_path = Path("/etc/nv_tegra_release")
    l4t_release = ""
    if l4t_path.is_file():
        l4t_release = l4t_path.read_text(encoding="utf-8", errors="replace").strip()
    return {
        "captured_at": utc_now_iso(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "python": sys.version.split()[0],
        "l4t_release": l4t_release,
        "git": git_snapshot(repo_root),
    }
