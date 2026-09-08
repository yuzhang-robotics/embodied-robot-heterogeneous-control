"""Shared parsing and artifact checks for workload validators."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from experiments.phase1.common.manifest import sha256_file


REQUIRED_SLICE_FILES = (
    "manifest.json",
    "preflight.json",
    "events.jsonl",
    "resources.jsonl",
    "scenario.json",
    "summary.json",
)


def read_object(path: Path, errors: list[str]) -> dict[str, Any]:
    """Read one JSON object while accumulating validation errors."""

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        errors.append(f"{path.name}: {type(exc).__name__}: {exc}")
        return {}
    if not isinstance(value, dict):
        errors.append(f"{path.name}: root must be an object")
        return {}
    return value


def json_value(value: object) -> object:
    """Normalize a value through the strict JSON data model."""

    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))


def check_artifacts(
    directory: Path,
    artifacts: object,
    errors: list[str],
    *,
    required_files: tuple[str, ...] = REQUIRED_SLICE_FILES,
) -> None:
    """Verify the complete manifest inventory against files on disk."""

    if not isinstance(artifacts, Mapping):
        errors.append("manifest artifact identities are missing")
        return
    expected = set(required_files) - {"manifest.json"}
    if set(artifacts) != expected:
        errors.append("manifest artifact identity set is incomplete or unsupported")
    for name in sorted(expected):
        identity = artifacts.get(name)
        path = directory / name
        if not isinstance(identity, Mapping):
            errors.append(f"manifest identity is missing for {name}")
            continue
        if identity.get("size_bytes") != path.stat().st_size:
            errors.append(f"manifest size does not match {name}")
        if identity.get("sha256") != sha256_file(path):
            errors.append(f"manifest SHA-256 does not match {name}")
