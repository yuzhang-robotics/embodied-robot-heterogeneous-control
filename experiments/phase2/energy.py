"""Exact-window device-wide energy integration for Phase 2 evidence."""

from __future__ import annotations

import bisect
import math
from dataclasses import asdict, dataclass
from typing import Any, Mapping, Sequence

from experiments.phase1.common.telemetry_jetson import validate_resource_samples


ENERGY_WINDOW_SCHEMA_VERSION = "0.1.0"


class EnergyIntegrationError(ValueError):
    """Required VDD_IN coverage cannot produce a valid exact-window integral."""


@dataclass(frozen=True, slots=True)
class EnergyWindowRecord:
    """Boundary-interpolated device input-energy result."""

    energy_window_schema_version: str
    method: str
    rail: str
    power_field: str
    window_started_monotonic_ns: int
    window_finished_monotonic_ns: int
    duration_ms: float
    source_first_monotonic_ns: int
    source_last_monotonic_ns: int
    source_sample_count: int
    integration_point_count: int
    maximum_allowed_gap_ms: float
    maximum_observed_gap_ms: float
    start_power_mw: float
    end_power_mw: float
    energy_mj: float
    device_scope: str
    process_attribution: bool
    raw_telemetry_recorded: bool

    def to_dict(self) -> dict[str, object]:
        value = asdict(self)
        validate_energy_window_record(value)
        return value


_ENERGY_FIELDS = set(EnergyWindowRecord.__dataclass_fields__)


def _positive_integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise EnergyIntegrationError(f"{name} must be a positive integer")
    return value


def _positive_finite(value: object, name: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) <= 0
    ):
        raise EnergyIntegrationError(f"{name} must be positive and finite")
    return float(value)


def _nonnegative_finite(value: object, name: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) < 0
    ):
        raise EnergyIntegrationError(f"{name} must be nonnegative and finite")
    return float(value)


def _power_at(
    timestamp_ns: int,
    timestamps: Sequence[int],
    powers_mw: Sequence[float],
) -> float:
    index = bisect.bisect_left(timestamps, timestamp_ns)
    if index < len(timestamps) and timestamps[index] == timestamp_ns:
        return powers_mw[index]
    if index == 0 or index == len(timestamps):
        raise EnergyIntegrationError("energy window lacks boundary coverage")
    lower_time = timestamps[index - 1]
    upper_time = timestamps[index]
    ratio = (timestamp_ns - lower_time) / (upper_time - lower_time)
    return powers_mw[index - 1] + ratio * (powers_mw[index] - powers_mw[index - 1])


def integrate_vdd_in_window(
    samples: Sequence[Mapping[str, Any]],
    *,
    window_started_monotonic_ns: int,
    window_finished_monotonic_ns: int,
    maximum_gap_ns: int,
) -> EnergyWindowRecord:
    """Integrate instantaneous VDD_IN with interpolated exact boundaries."""

    started = _positive_integer(
        window_started_monotonic_ns, "window_started_monotonic_ns"
    )
    finished = _positive_integer(
        window_finished_monotonic_ns, "window_finished_monotonic_ns"
    )
    allowed_gap = _positive_integer(maximum_gap_ns, "maximum_gap_ns")
    if finished <= started:
        raise EnergyIntegrationError("energy window must have positive duration")
    if not isinstance(samples, Sequence) or isinstance(samples, (str, bytes)):
        raise TypeError("samples must be a sequence of resource records")
    normalized = [dict(sample) for sample in samples]
    validation_errors = validate_resource_samples(normalized)
    if validation_errors:
        raise EnergyIntegrationError("resource samples failed validation")

    timestamps: list[int] = []
    powers_mw: list[float] = []
    previous = -1
    for sample in normalized:
        timestamp = int(sample["sample_monotonic_ns"])
        if timestamp <= previous:
            raise EnergyIntegrationError(
                "resource sample time is not strictly increasing"
            )
        previous = timestamp
        power = sample.get("power")
        rail = power.get("VDD_IN") if isinstance(power, Mapping) else None
        if not isinstance(rail, Mapping) or set(rail) != {"instant_mw", "average_mw"}:
            raise EnergyIntegrationError("VDD_IN rail is missing or invalid")
        instant = _nonnegative_finite(rail.get("instant_mw"), "VDD_IN instant_mw")
        timestamps.append(timestamp)
        powers_mw.append(instant)

    if timestamps[0] > started or timestamps[-1] < finished:
        raise EnergyIntegrationError("resource samples do not cover the energy window")
    left_index = bisect.bisect_right(timestamps, started) - 1
    right_index = bisect.bisect_left(timestamps, finished)
    source_times = timestamps[left_index : right_index + 1]
    if len(source_times) < 2:
        raise EnergyIntegrationError("energy window has insufficient source samples")
    gaps = [right - left for left, right in zip(source_times, source_times[1:])]
    maximum_observed_gap = max(gaps)
    if maximum_observed_gap > allowed_gap:
        raise EnergyIntegrationError("resource coverage gap exceeds the allowed bound")

    point_times = [started]
    point_times.extend(
        time_ns for time_ns in timestamps if started < time_ns < finished
    )
    point_times.append(finished)
    point_powers = [
        _power_at(time_ns, timestamps, powers_mw) for time_ns in point_times
    ]
    energy_mj = 0.0
    for left, right, left_power, right_power in zip(
        point_times,
        point_times[1:],
        point_powers,
        point_powers[1:],
    ):
        energy_mj += (left_power + right_power) * 0.5 * (right - left) / 1e9

    record = EnergyWindowRecord(
        energy_window_schema_version=ENERGY_WINDOW_SCHEMA_VERSION,
        method="linear_boundary_interpolation_trapezoidal",
        rail="VDD_IN",
        power_field="instant_mw",
        window_started_monotonic_ns=started,
        window_finished_monotonic_ns=finished,
        duration_ms=(finished - started) / 1e6,
        source_first_monotonic_ns=source_times[0],
        source_last_monotonic_ns=source_times[-1],
        source_sample_count=len(source_times),
        integration_point_count=len(point_times),
        maximum_allowed_gap_ms=allowed_gap / 1e6,
        maximum_observed_gap_ms=maximum_observed_gap / 1e6,
        start_power_mw=point_powers[0],
        end_power_mw=point_powers[-1],
        energy_mj=energy_mj,
        device_scope="device_wide_input_power",
        process_attribution=False,
        raw_telemetry_recorded=False,
    )
    validate_energy_window_record(record.to_dict())
    return record


def validate_energy_window_record(value: Mapping[str, object]) -> None:
    """Validate a derived energy record without requiring raw telemetry."""

    if set(value) != _ENERGY_FIELDS:
        raise EnergyIntegrationError("energy window fields are invalid")
    if (
        value.get("energy_window_schema_version") != ENERGY_WINDOW_SCHEMA_VERSION
        or value.get("method") != "linear_boundary_interpolation_trapezoidal"
        or value.get("rail") != "VDD_IN"
        or value.get("power_field") != "instant_mw"
        or value.get("device_scope") != "device_wide_input_power"
        or value.get("process_attribution") is not False
        or value.get("raw_telemetry_recorded") is not False
    ):
        raise EnergyIntegrationError("energy window identity or scope is invalid")

    started = _positive_integer(
        value.get("window_started_monotonic_ns"),
        "window_started_monotonic_ns",
    )
    finished = _positive_integer(
        value.get("window_finished_monotonic_ns"),
        "window_finished_monotonic_ns",
    )
    source_first = _positive_integer(
        value.get("source_first_monotonic_ns"), "source_first_monotonic_ns"
    )
    source_last = _positive_integer(
        value.get("source_last_monotonic_ns"), "source_last_monotonic_ns"
    )
    if not source_first <= started < finished <= source_last:
        raise EnergyIntegrationError("energy window coverage is invalid")

    duration_ms = _positive_finite(value.get("duration_ms"), "duration_ms")
    if abs(duration_ms - (finished - started) / 1e6) > 1e-9:
        raise EnergyIntegrationError("energy window duration is inconsistent")
    source_sample_count = _positive_integer(
        value.get("source_sample_count"), "source_sample_count"
    )
    if source_sample_count < 2:
        raise EnergyIntegrationError("energy window needs at least two source samples")
    integration_point_count = _positive_integer(
        value.get("integration_point_count"), "integration_point_count"
    )
    if integration_point_count < 2:
        raise EnergyIntegrationError(
            "energy window needs at least two integration points"
        )

    allowed_gap_ms = _positive_finite(
        value.get("maximum_allowed_gap_ms"), "maximum_allowed_gap_ms"
    )
    observed_gap_ms = _positive_finite(
        value.get("maximum_observed_gap_ms"), "maximum_observed_gap_ms"
    )
    if observed_gap_ms > allowed_gap_ms:
        raise EnergyIntegrationError("energy window gap exceeds the allowed bound")
    _nonnegative_finite(value.get("start_power_mw"), "start_power_mw")
    _nonnegative_finite(value.get("end_power_mw"), "end_power_mw")
    _nonnegative_finite(value.get("energy_mj"), "energy_mj")
