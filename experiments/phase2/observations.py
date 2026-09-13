"""Validated Phase 2 memory, residency and process observations."""

from __future__ import annotations

import math
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Mapping

from experiments.phase1.carryover.observation import (
    ObservationError as Phase1ObservationError,
    observe_file_residency as _observe_file_residency,
    read_meminfo as _read_meminfo,
    validate_process_observation as _validate_phase1_process_observation,
)


BOUNDARY_OBSERVATION_SCHEMA_VERSION = "0.1.0"
BOUNDARY_NAMES = frozenset(
    {"pre_unit", "post_primer", "post_vlm", "post_action", "post_asr"}
)


class ObservationValidationError(RuntimeError):
    """A required Phase 2 observation is unavailable or inconsistent."""


@dataclass(frozen=True, slots=True)
class BoundaryObservationRecord:
    """Privacy-preserving memory and file-residency boundary evidence."""

    boundary_observation_schema_version: str
    boundary: str
    observation_started_monotonic_ns: int
    observation_finished_monotonic_ns: int
    expected_size_bytes: int
    mem_available_bytes: int
    cached_bytes: int
    sreclaimable_bytes: int
    residency_method: str
    file_size_bytes: int
    page_size_bytes: int
    total_pages: int
    resident_pages: int
    resident_fraction: float
    path_recorded: bool
    contents_recorded: bool

    def to_dict(self) -> dict[str, object]:
        value = asdict(self)
        validate_boundary_observation(value)
        return value


_BOUNDARY_FIELDS = set(BoundaryObservationRecord.__dataclass_fields__)
_RESIDENCY_FIELDS = {
    "method",
    "file_size_bytes",
    "page_size_bytes",
    "total_pages",
    "resident_pages",
    "resident_fraction",
    "path_recorded",
}
_MEMORY_FIELDS = {
    "MemAvailable_bytes",
    "Cached_bytes",
    "SReclaimable_bytes",
}
_PROCESS_FIELDS = {
    "observation_schema_version",
    "method",
    "sample_interval_ms",
    "sample_count",
    "read_error_count",
    "process_exit_observed",
    "user_time_s",
    "system_time_s",
    "minor_faults",
    "major_faults",
    "maximum_rss_bytes",
    "voluntary_context_switches",
    "involuntary_context_switches",
    "pid_recorded",
    "command_recorded",
}


def _positive_integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ObservationValidationError(f"{name} must be a positive integer")
    return value


def _nonnegative_integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ObservationValidationError(f"{name} must be a nonnegative integer")
    return value


def capture_boundary_observation(
    path: Path | str,
    *,
    boundary: str,
    expected_size_bytes: int,
    memory_reader: Callable[[], Mapping[str, int]] = _read_meminfo,
    residency_reader: Callable[[Path | str], Mapping[str, object]] = (
        _observe_file_residency
    ),
    clock_ns: Callable[[], int] = time.monotonic_ns,
) -> BoundaryObservationRecord:
    """Capture one non-touching residency boundary without publishing identity."""

    expected = _positive_integer(expected_size_bytes, "expected_size_bytes")
    if boundary not in BOUNDARY_NAMES:
        raise ObservationValidationError("unsupported Phase 2 observation boundary")
    if not callable(memory_reader) or not callable(residency_reader):
        raise TypeError("observation readers must be callable")
    if not callable(clock_ns):
        raise TypeError("clock_ns must be callable")

    started_ns = clock_ns()
    try:
        memory = dict(memory_reader())
        residency = dict(residency_reader(path))
    except (Phase1ObservationError, OSError, ValueError, TypeError) as exc:
        raise ObservationValidationError("Phase 2 boundary observation failed") from exc
    finished_ns = clock_ns()

    if set(memory) != _MEMORY_FIELDS:
        raise ObservationValidationError("memory observation fields are invalid")
    if set(residency) != _RESIDENCY_FIELDS:
        raise ObservationValidationError("residency observation fields are invalid")
    if residency.get("path_recorded") is not False:
        raise ObservationValidationError("residency privacy boundary is invalid")
    fraction = residency.get("resident_fraction")
    if (
        isinstance(fraction, bool)
        or not isinstance(fraction, (int, float))
        or not math.isfinite(float(fraction))
    ):
        raise ObservationValidationError("resident_fraction is invalid")

    record = BoundaryObservationRecord(
        boundary_observation_schema_version=BOUNDARY_OBSERVATION_SCHEMA_VERSION,
        boundary=boundary,
        observation_started_monotonic_ns=started_ns,
        observation_finished_monotonic_ns=finished_ns,
        expected_size_bytes=expected,
        mem_available_bytes=memory["MemAvailable_bytes"],
        cached_bytes=memory["Cached_bytes"],
        sreclaimable_bytes=memory["SReclaimable_bytes"],
        residency_method=str(residency["method"]),
        file_size_bytes=_positive_integer(
            residency["file_size_bytes"], "file_size_bytes"
        ),
        page_size_bytes=_positive_integer(
            residency["page_size_bytes"], "page_size_bytes"
        ),
        total_pages=_positive_integer(residency["total_pages"], "total_pages"),
        resident_pages=_nonnegative_integer(
            residency["resident_pages"], "resident_pages"
        ),
        resident_fraction=float(fraction),
        path_recorded=False,
        contents_recorded=False,
    )
    validate_boundary_observation(record.to_dict())
    return record


def validate_boundary_observation(value: Mapping[str, object]) -> None:
    """Fail closed unless a boundary observation satisfies the Phase 2 contract."""

    if set(value) != _BOUNDARY_FIELDS:
        raise ObservationValidationError("boundary observation fields are invalid")
    if (
        value.get("boundary_observation_schema_version")
        != BOUNDARY_OBSERVATION_SCHEMA_VERSION
        or value.get("boundary") not in BOUNDARY_NAMES
    ):
        raise ObservationValidationError("boundary observation identity is invalid")

    started = _positive_integer(
        value.get("observation_started_monotonic_ns"),
        "observation_started_monotonic_ns",
    )
    finished = _positive_integer(
        value.get("observation_finished_monotonic_ns"),
        "observation_finished_monotonic_ns",
    )
    if finished < started:
        raise ObservationValidationError("boundary observation time moved backwards")

    expected = _positive_integer(
        value.get("expected_size_bytes"), "expected_size_bytes"
    )
    file_size = _positive_integer(value.get("file_size_bytes"), "file_size_bytes")
    page_size = _positive_integer(value.get("page_size_bytes"), "page_size_bytes")
    total_pages = _positive_integer(value.get("total_pages"), "total_pages")
    resident_pages = _nonnegative_integer(value.get("resident_pages"), "resident_pages")
    if file_size != expected or total_pages != math.ceil(file_size / page_size):
        raise ObservationValidationError("residency size or page count is inconsistent")
    if resident_pages > total_pages:
        raise ObservationValidationError("resident page count exceeds total pages")

    fraction = value.get("resident_fraction")
    if (
        isinstance(fraction, bool)
        or not isinstance(fraction, (int, float))
        or not math.isfinite(float(fraction))
        or abs(float(fraction) - resident_pages / total_pages) > 1e-12
        or value.get("residency_method") != "non_touching_mmap_mincore"
    ):
        raise ObservationValidationError("residency values are invalid")

    for name in ("mem_available_bytes", "cached_bytes", "sreclaimable_bytes"):
        _nonnegative_integer(value.get(name), name)
    if (
        value.get("path_recorded") is not False
        or value.get("contents_recorded") is not False
    ):
        raise ObservationValidationError(
            "boundary observation privacy flags are invalid"
        )


def validate_process_observation(value: Mapping[str, object]) -> None:
    """Validate sampled Linux process evidence without accepting identity fields."""

    if set(value) != _PROCESS_FIELDS:
        raise ObservationValidationError("process observation fields are invalid")
    interval = value.get("sample_interval_ms")
    if (
        isinstance(interval, bool)
        or not isinstance(interval, (int, float))
        or not math.isfinite(float(interval))
        or float(interval) <= 0
    ):
        raise ObservationValidationError("process observation interval is invalid")
    try:
        _validate_phase1_process_observation(value)
    except Phase1ObservationError as exc:
        raise ObservationValidationError("process observation is invalid") from exc
