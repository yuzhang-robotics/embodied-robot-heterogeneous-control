"""Manifest helpers for Phase 0 experiment runs."""

from __future__ import annotations

import hashlib
import importlib.metadata
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


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def sha256_file(path: Path | str) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def file_identity(path: Path | str, *, calculate_hash: bool = True) -> dict[str, Any]:
    resolved = Path(path).expanduser().resolve()
    stat = resolved.stat()
    identity: dict[str, Any] = {
        "path": str(resolved),
        "size_bytes": stat.st_size,
    }
    if calculate_hash:
        identity["sha256"] = sha256_file(resolved)
    return identity


def command_snapshot(
    command: list[str], *, cwd: Path | str | None = None, timeout: float = 5
) -> dict[str, Any]:
    try:
        result = subprocess.run(
            command,
            cwd=str(cwd) if cwd is not None else None,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "command": command,
            "returncode": None,
            "output": "",
            "error": f"{type(exc).__name__}: {exc}",
        }

    return {
        "command": command,
        "returncode": result.returncode,
        "output": result.stdout.strip(),
        "error": "",
    }


def package_versions() -> dict[str, str | None]:
    names = [
        "argostranslate",
        "numpy",
        "onnxruntime",
        "piper-tts",
        "pyserial",
        "sherpa-onnx",
        "torch",
    ]
    versions: dict[str, str | None] = {}
    for name in names:
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    return versions


def git_snapshot(repo_root: Path | str) -> dict[str, Any]:
    root = Path(repo_root)
    commit = command_snapshot(["git", "rev-parse", "HEAD"], cwd=root)
    branch = command_snapshot(["git", "branch", "--show-current"], cwd=root)
    status = command_snapshot(["git", "status", "--porcelain"], cwd=root)
    return {
        "commit": commit["output"],
        "branch": branch["output"],
        "dirty": bool(status["output"]),
        "status_porcelain": status["output"].splitlines() if status["output"] else [],
        "errors": [
            item["error"]
            for item in (commit, branch, status)
            if item["error"]
        ],
    }


def collect_environment(repo_root: Path | str) -> dict[str, Any]:
    l4t_path = Path("/etc/nv_tegra_release")
    l4t_release = ""
    if l4t_path.exists():
        l4t_release = l4t_path.read_text(encoding="utf-8", errors="replace").strip()

    return {
        "captured_at": utc_now_iso(),
        "hostname": platform.node(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "python": sys.version.split()[0],
        "packages": package_versions(),
        "l4t_release": l4t_release,
        "jetpack_packages": command_snapshot(
            [
                "dpkg-query",
                "-W",
                "-f=${Package}\\t${Version}\\n",
                "nvidia-jetpack",
                "nvidia-l4t-core",
            ]
        ),
        "nvcc": command_snapshot(["nvcc", "--version"]),
        "nvpmodel": command_snapshot(["nvpmodel", "-q"]),
        "jetson_clocks": command_snapshot(["jetson_clocks", "--show"]),
        "ollama": command_snapshot(["ollama", "--version"]),
        "git": git_snapshot(repo_root),
    }


def motion_environment() -> dict[str, Any]:
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


def write_json_atomic(path: Path | str, data: dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, target)
