"""Shared evidence helpers for fixed-input workload runners."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from experiments.phase1.common.manifest import sha256_file


def make_run_id(
    condition: str,
    workload: str,
    repetition: int,
    *,
    now: datetime | None = None,
) -> str:
    """Build a timestamped workload run identifier."""

    validate_repetition(repetition)
    stamp = _utc_stamp(now)
    return f"{stamp}_phase1_{condition}_{workload}_{repetition:03d}"


def validate_repetition(repetition: int) -> int:
    """Validate the bounded repetition index used by public run IDs."""

    if (
        isinstance(repetition, bool)
        or not isinstance(repetition, int)
        or not 0 <= repetition <= 999
    ):
        raise ValueError("repetition must be an integer from 0 to 999")
    return repetition


def make_session_id(
    workload: str,
    *,
    now: datetime | None = None,
) -> str:
    """Build a timestamped workload session identifier."""

    return f"{_utc_stamp(now)}_phase1_{workload}_slice"


def validate_session_id(value: str, workload: str) -> str:
    """Validate the public session identifier accepted by a runner."""

    pattern = rf"^[0-9]{{8}}T[0-9]{{6}}Z_phase1_{workload}_[a-z][a-z0-9_-]{{0,31}}$"
    if not isinstance(value, str) or re.fullmatch(pattern, value) is None:
        raise ValueError(
            "session_id must use "
            f"YYYYMMDDTHHMMSSZ_phase1_{workload}_<lowercase-label>"
        )
    return value


def resolve_output_root(
    output_root: Path,
    repo_root: Path,
    *,
    workload: str,
    error_type: type[RuntimeError],
) -> Path:
    """Resolve an output root without allowing writes into source trees."""

    expanded = output_root.expanduser()
    resolved = (expanded if expanded.is_absolute() else repo_root / expanded).resolve()
    phase0_source = (repo_root / "experiments" / "phase0").resolve()
    label = workload.upper()
    if _is_relative_to(resolved, phase0_source):
        raise error_type(
            f"Phase 1 {label} output must not be inside experiments/phase0"
        )
    if _is_relative_to(resolved, repo_root):
        ignored_root = (repo_root / "experiments" / "runs").resolve()
        if not _is_relative_to(resolved, ignored_root):
            raise error_type(
                f"repository-local {label} output must be inside experiments/runs"
            )
    return resolved


def artifact_identity(path: Path) -> dict[str, object]:
    """Return the stable identity recorded for one evidence artifact."""

    return {
        "size_bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def completed_artifact_identities(
    run_dir: Path,
    completed_names: set[str],
    artifact_names: Iterable[str],
) -> dict[str, object]:
    """Describe only artifacts known to have closed before a failure."""

    return {
        name: artifact_identity(run_dir / name)
        for name in artifact_names
        if name in completed_names
    }


def reproducibility_record(
    environment: dict[str, object],
    *,
    injected_components: list[str],
) -> dict[str, object]:
    """Summarize source identity and development injection state."""

    git = environment.get("git")
    record = git if isinstance(git, dict) else {}
    identity_complete = (
        not record.get("error_codes")
        and bool(record.get("commit"))
        and bool(record.get("branch"))
    )
    git_clean = identity_complete and record.get("dirty") is False
    synchronized_main = (
        git_clean
        and record.get("branch") == "main"
        and record.get("upstream") == "origin/main"
        and record.get("upstream_commit") == record.get("commit")
        and str(record.get("ahead_behind", "")).split() == ["0", "0"]
    )
    return {
        "git_identity_complete": identity_complete,
        "git_clean": git_clean,
        "synchronized_main": synchronized_main,
        "development_injection": bool(injected_components),
        "injected_components": injected_components,
        "formal_evidence_eligible": False,
    }


def _utc_stamp(now: datetime | None) -> str:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return current.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True
