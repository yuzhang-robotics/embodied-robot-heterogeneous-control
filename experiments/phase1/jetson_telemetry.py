"""Bounded tegrastats recording for the Phase 1 Jetson pilot."""

from __future__ import annotations

import json
import math
import re
import shutil
import statistics
import subprocess
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence, TextIO


RESOURCE_SCHEMA_VERSION = "0.1.0"
_MAX_LINE_LENGTH = 8192

_TEGRA_TIME_RE = re.compile(
    r"^(?P<date>[0-9]{2}-[0-9]{2}-[0-9]{4})\s+" r"(?P<time>[0-9]{2}:[0-9]{2}:[0-9]{2})"
)
_RAM_RE = re.compile(
    r"\bRAM\s+(?P<used>\d+)/(?P<total>\d+)MB\s+"
    r"\(lfb\s+(?P<count>\d+)x(?P<size>\d+)(?P<unit>MB|kB)\)"
)
_SWAP_RE = re.compile(
    r"\bSWAP\s+(?P<used>\d+)/(?P<total>\d+)MB\s+" r"\(cached\s+(?P<cached>\d+)MB\)"
)
_CPU_RE = re.compile(r"\bCPU\s+\[(?P<cores>[^\]]+)\]")
_CORE_RE = re.compile(r"^(?P<usage>\d+)%@(?P<freq>\d+)$")
_EMC_RE = re.compile(r"\bEMC_FREQ\s+(?P<usage>\d+)%" r"(?:@(?P<freq>\d+))?")
_GR3D_RE = re.compile(
    r"\bGR3D_FREQ\s+(?P<usage>\d+)%" r"(?:@(?P<freq>\[[0-9, ]+\]|\d+))?"
)
_TEMP_RE = re.compile(
    r"\b(?P<name>[A-Za-z][A-Za-z0-9_]*)@(?P<value>[0-9]+(?:\.[0-9]+)?)C"
)
_POWER_RE = re.compile(
    r"\b(?P<name>VDD_[A-Z0-9_]+)\s+" r"(?P<instant>\d+)mW/(?P<average>\d+)mW"
)
_SAMPLE_KEYS = {
    "resource_schema_version",
    "seq",
    "sample_monotonic_ns",
    "sample_wall_time_ns",
    "tegrastats_time",
    "ram",
    "swap",
    "cpu",
    "emc",
    "gr3d",
    "temperatures_c",
    "power",
    "parse_errors",
    "parse_warnings",
    "raw_line",
}


@dataclass(frozen=True, slots=True)
class TegrastatsStopReport:
    """Finite process and reader-thread shutdown evidence."""

    sample_count: int
    parse_error_count: int
    first_sample_monotonic_ns: int | None
    last_sample_monotonic_ns: int | None
    process_returncode: int | None
    stop_method: str
    reader_joined: bool
    reader_error_code: str | None

    @property
    def successful(self) -> bool:
        return (
            self.sample_count > 0
            and self.parse_error_count == 0
            and self.stop_method in {"terminated", "killed"}
            and self.reader_joined
            and self.reader_error_code is None
        )

    def to_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["successful"] = self.successful
        return value


def _percentage(value: str, field: str, errors: list[str]) -> int:
    parsed = int(value)
    if not 0 <= parsed <= 100:
        errors.append(f"{field}_range")
    return parsed


def _gr3d_frequencies(value: str | None) -> list[int]:
    if value is None:
        return []
    return [int(item) for item in re.findall(r"\d+", value)]


def parse_tegrastats_line(
    line: str,
    *,
    sequence: int,
    sample_monotonic_ns: int,
    sample_wall_time_ns: int,
) -> dict[str, Any]:
    """Parse one bounded JetPack tegrastats line without fixed sensor names."""

    for value, name in (
        (sequence, "sequence"),
        (sample_monotonic_ns, "sample_monotonic_ns"),
        (sample_wall_time_ns, "sample_wall_time_ns"),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{name} must be a non-negative integer")
    if not isinstance(line, str):
        raise TypeError("line must be a string")

    raw_line = line.rstrip("\r\n")
    errors: list[str] = []
    warnings: list[str] = []
    if len(raw_line) > _MAX_LINE_LENGTH:
        raw_line = raw_line[:_MAX_LINE_LENGTH]
        errors.append("line_too_long")

    timestamp_match = _TEGRA_TIME_RE.search(raw_line)
    timestamp = (
        f"{timestamp_match.group('date')} {timestamp_match.group('time')}"
        if timestamp_match is not None
        else None
    )
    if timestamp is None:
        warnings.append("tegrastats_timestamp_missing")

    ram: dict[str, int | float] | None = None
    ram_match = _RAM_RE.search(raw_line)
    if ram_match is None:
        errors.append("ram_missing")
    else:
        lfb_size_mb = int(ram_match.group("size"))
        if ram_match.group("unit") == "kB":
            lfb_size_mb /= 1024
        ram = {
            "used_mb": int(ram_match.group("used")),
            "total_mb": int(ram_match.group("total")),
            "lfb_count": int(ram_match.group("count")),
            "lfb_size_mb": lfb_size_mb,
        }

    swap: dict[str, int] | None = None
    swap_match = _SWAP_RE.search(raw_line)
    if swap_match is None:
        errors.append("swap_missing")
    else:
        swap = {
            "used_mb": int(swap_match.group("used")),
            "total_mb": int(swap_match.group("total")),
            "cached_mb": int(swap_match.group("cached")),
        }

    cpu: list[dict[str, int | bool | None]] = []
    cpu_match = _CPU_RE.search(raw_line)
    if cpu_match is None:
        errors.append("cpu_missing")
    else:
        tokens = [item.strip() for item in cpu_match.group("cores").split(",")]
        if not tokens or len(tokens) > 32:
            errors.append("cpu_count_invalid")
        for index, token in enumerate(tokens[:32]):
            if token.lower() == "off":
                cpu.append(
                    {
                        "index": index,
                        "online": False,
                        "usage_pct": None,
                        "frequency_mhz": None,
                    }
                )
                continue
            core_match = _CORE_RE.fullmatch(token)
            if core_match is None:
                errors.append(f"cpu{index}_invalid")
                cpu.append(
                    {
                        "index": index,
                        "online": True,
                        "usage_pct": None,
                        "frequency_mhz": None,
                    }
                )
                continue
            cpu.append(
                {
                    "index": index,
                    "online": True,
                    "usage_pct": _percentage(
                        core_match.group("usage"), f"cpu{index}_usage", errors
                    ),
                    "frequency_mhz": int(core_match.group("freq")),
                }
            )

    emc: dict[str, int | None] | None = None
    emc_match = _EMC_RE.search(raw_line)
    if emc_match is not None:
        emc = {
            "usage_pct": _percentage(emc_match.group("usage"), "emc_usage", errors),
            "frequency_mhz": (
                int(emc_match.group("freq"))
                if emc_match.group("freq") is not None
                else None
            ),
        }
    else:
        warnings.append("emc_missing")

    gr3d: dict[str, object] | None = None
    gr3d_match = _GR3D_RE.search(raw_line)
    if gr3d_match is None:
        errors.append("gr3d_missing")
    else:
        gr3d = {
            "usage_pct": _percentage(gr3d_match.group("usage"), "gr3d_usage", errors),
            "frequencies_mhz": _gr3d_frequencies(gr3d_match.group("freq")),
        }

    temperatures: dict[str, float] = {}
    for match in _TEMP_RE.finditer(raw_line):
        if len(temperatures) >= 64:
            errors.append("temperature_count_invalid")
            break
        temperatures[match.group("name").lower()] = float(match.group("value"))
    if not temperatures:
        errors.append("temperature_missing")

    power: dict[str, dict[str, int]] = {}
    for match in _POWER_RE.finditer(raw_line):
        if len(power) >= 64:
            errors.append("power_count_invalid")
            break
        power[match.group("name")] = {
            "instant_mw": int(match.group("instant")),
            "average_mw": int(match.group("average")),
        }
    if not power:
        errors.append("power_missing")

    return {
        "resource_schema_version": RESOURCE_SCHEMA_VERSION,
        "seq": sequence,
        "sample_monotonic_ns": sample_monotonic_ns,
        "sample_wall_time_ns": sample_wall_time_ns,
        "tegrastats_time": timestamp,
        "ram": ram,
        "swap": swap,
        "cpu": cpu,
        "emc": emc,
        "gr3d": gr3d,
        "temperatures_c": temperatures,
        "power": power,
        "parse_errors": errors,
        "parse_warnings": warnings,
        "raw_line": raw_line,
    }


def load_resource_samples(path: Path | str) -> list[dict[str, Any]]:
    samples: list[dict[str, Any]] = []
    with Path(path).open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"resources.jsonl line {line_number}: invalid JSON: {exc}"
                ) from exc
            if not isinstance(item, dict):
                raise ValueError(
                    f"resources.jsonl line {line_number}: root must be an object"
                )
            samples.append(item)
    return samples


def _nonnegative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _finite_nonnegative_number(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
    )


def _bounded_messages(value: object) -> bool:
    return (
        isinstance(value, list)
        and len(value) <= 32
        and all(isinstance(item, str) and 1 <= len(item) <= 128 for item in value)
    )


def validate_resource_samples(samples: Sequence[dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    if not samples:
        return ["resources.jsonl contains no samples"]
    previous_monotonic_ns = -1
    for expected_seq, sample in enumerate(samples):
        prefix = f"resources.jsonl line {expected_seq + 1}"
        if set(sample) != _SAMPLE_KEYS:
            errors.append(f"{prefix}: sample fields do not match the resource schema")
        if sample.get("resource_schema_version") != RESOURCE_SCHEMA_VERSION:
            errors.append(f"{prefix}: unsupported resource schema version")
        if sample.get("seq") != expected_seq:
            errors.append(f"{prefix}: sequence is not contiguous")

        monotonic_ns = sample.get("sample_monotonic_ns")
        if not _nonnegative_int(monotonic_ns) or monotonic_ns < previous_monotonic_ns:
            errors.append(f"{prefix}: monotonic timestamp moved backwards")
        if _nonnegative_int(monotonic_ns):
            previous_monotonic_ns = max(previous_monotonic_ns, monotonic_ns)
        wall_time_ns = sample.get("sample_wall_time_ns")
        if not _nonnegative_int(wall_time_ns) or wall_time_ns == 0:
            errors.append(f"{prefix}: invalid wall timestamp")
        tegrastats_time = sample.get("tegrastats_time")
        if tegrastats_time is not None and (
            not isinstance(tegrastats_time, str)
            or _TEGRA_TIME_RE.fullmatch(tegrastats_time) is None
        ):
            errors.append(f"{prefix}: invalid tegrastats timestamp")

        ram = sample.get("ram")
        if not isinstance(ram, dict) or set(ram) != {
            "used_mb",
            "total_mb",
            "lfb_count",
            "lfb_size_mb",
        }:
            errors.append(f"{prefix}: ram observation is invalid")
        elif (
            not _nonnegative_int(ram.get("used_mb"))
            or not _nonnegative_int(ram.get("total_mb"))
            or ram["used_mb"] > ram["total_mb"]
            or not _nonnegative_int(ram.get("lfb_count"))
            or not _finite_nonnegative_number(ram.get("lfb_size_mb"))
        ):
            errors.append(f"{prefix}: ram values are invalid")

        swap = sample.get("swap")
        if not isinstance(swap, dict) or set(swap) != {
            "used_mb",
            "total_mb",
            "cached_mb",
        }:
            errors.append(f"{prefix}: swap observation is invalid")
        elif (
            not _nonnegative_int(swap.get("used_mb"))
            or not _nonnegative_int(swap.get("total_mb"))
            or swap["used_mb"] > swap["total_mb"]
            or not _nonnegative_int(swap.get("cached_mb"))
        ):
            errors.append(f"{prefix}: swap values are invalid")

        parse_errors = sample.get("parse_errors")
        if not _bounded_messages(parse_errors):
            errors.append(f"{prefix}: parse_errors must be a bounded array")
        elif parse_errors:
            errors.append(f"{prefix}: unexplained parse errors: {parse_errors}")
        if not _bounded_messages(sample.get("parse_warnings")):
            errors.append(f"{prefix}: parse_warnings must be a bounded array")

        cpu = sample.get("cpu")
        if not isinstance(cpu, list) or not 1 <= len(cpu) <= 32:
            errors.append(f"{prefix}: CPU observations are missing")
        else:
            for index, core in enumerate(cpu):
                if not isinstance(core, dict) or set(core) != {
                    "index",
                    "online",
                    "usage_pct",
                    "frequency_mhz",
                }:
                    errors.append(f"{prefix}: CPU core {index} is invalid")
                    continue
                online = core.get("online")
                usage = core.get("usage_pct")
                frequency = core.get("frequency_mhz")
                if core.get("index") != index or not isinstance(online, bool):
                    errors.append(f"{prefix}: CPU core {index} identity is invalid")
                if online:
                    if (
                        not _nonnegative_int(usage)
                        or usage > 100
                        or not _nonnegative_int(frequency)
                    ):
                        errors.append(f"{prefix}: CPU core {index} values are invalid")
                elif usage is not None or frequency is not None:
                    errors.append(f"{prefix}: offline CPU core {index} has values")

        emc = sample.get("emc")
        if emc is not None:
            if not isinstance(emc, dict) or set(emc) != {
                "usage_pct",
                "frequency_mhz",
            }:
                errors.append(f"{prefix}: EMC observation is invalid")
            else:
                usage = emc.get("usage_pct")
                frequency = emc.get("frequency_mhz")
                if not _nonnegative_int(usage) or usage > 100:
                    errors.append(f"{prefix}: EMC usage is invalid")
                if frequency is not None and not _nonnegative_int(frequency):
                    errors.append(f"{prefix}: EMC frequency is invalid")

        gr3d = sample.get("gr3d")
        if not isinstance(gr3d, dict) or set(gr3d) != {
            "usage_pct",
            "frequencies_mhz",
        }:
            errors.append(f"{prefix}: GR3D observation is invalid")
        else:
            frequencies = gr3d.get("frequencies_mhz")
            if not _nonnegative_int(gr3d.get("usage_pct")) or gr3d["usage_pct"] > 100:
                errors.append(f"{prefix}: GR3D usage is invalid")
            if (
                not isinstance(frequencies, list)
                or len(frequencies) > 16
                or any(not _nonnegative_int(value) for value in frequencies)
            ):
                errors.append(f"{prefix}: GR3D frequencies are invalid")

        temperatures = sample.get("temperatures_c")
        if not isinstance(temperatures, dict) or not 1 <= len(temperatures) <= 64:
            errors.append(f"{prefix}: temperature observations are missing")
        elif any(
            re.fullmatch(r"[a-z][a-z0-9_]*", str(name)) is None
            or not _finite_nonnegative_number(value)
            for name, value in temperatures.items()
        ):
            errors.append(f"{prefix}: temperature observations are invalid")

        power = sample.get("power")
        if not isinstance(power, dict) or not 1 <= len(power) <= 64:
            errors.append(f"{prefix}: power observations are missing")
        else:
            for name, values in power.items():
                if re.fullmatch(r"VDD_[A-Z0-9_]+", str(name)) is None:
                    errors.append(f"{prefix}: power rail name is invalid")
                if not isinstance(values, dict) or set(values) != {
                    "instant_mw",
                    "average_mw",
                }:
                    errors.append(f"{prefix}: power rail {name} is invalid")
                elif not _nonnegative_int(
                    values.get("instant_mw")
                ) or not _nonnegative_int(values.get("average_mw")):
                    errors.append(f"{prefix}: power rail {name} values are invalid")

        raw_line = sample.get("raw_line")
        if (
            not isinstance(raw_line, str)
            or not raw_line
            or len(raw_line) > _MAX_LINE_LENGTH
        ):
            errors.append(f"{prefix}: raw_line is invalid")
        elif (
            _nonnegative_int(sample.get("seq"))
            and _nonnegative_int(monotonic_ns)
            and _nonnegative_int(wall_time_ns)
        ):
            rebuilt = parse_tegrastats_line(
                raw_line,
                sequence=sample["seq"],
                sample_monotonic_ns=monotonic_ns,
                sample_wall_time_ns=wall_time_ns,
            )
            if sample != rebuilt:
                errors.append(f"{prefix}: parsed fields do not match raw_line")
    return errors


def _nearest_rank(values: Sequence[float], percentile: int) -> float:
    ordered = sorted(values)
    rank = math.ceil(percentile / 100 * len(ordered))
    return ordered[rank - 1]


def _describe(values: Iterable[int | float]) -> dict[str, int | float] | None:
    items = [float(value) for value in values]
    if not items:
        return None
    return {
        "count": len(items),
        "min": min(items),
        "mean": statistics.fmean(items),
        "median": statistics.median(items),
        "p50": _nearest_rank(items, 50),
        "p95": _nearest_rank(items, 95),
        "p99": _nearest_rank(items, 99),
        "max": max(items),
    }


def summarize_resource_samples(
    samples: Sequence[dict[str, Any]],
) -> dict[str, object]:
    monotonic = [int(sample["sample_monotonic_ns"]) for sample in samples]
    intervals = [
        current - previous for previous, current in zip(monotonic, monotonic[1:])
    ]
    cpu_usage: dict[str, list[int]] = {}
    cpu_frequency: dict[str, list[int]] = {}
    temperatures: dict[str, list[float]] = {}
    power_instant: dict[str, list[int]] = {}
    power_average: dict[str, list[int]] = {}

    for sample in samples:
        for core in sample.get("cpu", []):
            if not isinstance(core, dict):
                continue
            key = str(core.get("index"))
            usage = core.get("usage_pct")
            frequency = core.get("frequency_mhz")
            if isinstance(usage, int) and not isinstance(usage, bool):
                cpu_usage.setdefault(key, []).append(usage)
            if isinstance(frequency, int) and not isinstance(frequency, bool):
                cpu_frequency.setdefault(key, []).append(frequency)
        for name, value in sample.get("temperatures_c", {}).items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                temperatures.setdefault(str(name), []).append(float(value))
        for name, value in sample.get("power", {}).items():
            if not isinstance(value, dict):
                continue
            instant = value.get("instant_mw")
            average = value.get("average_mw")
            if isinstance(instant, int) and not isinstance(instant, bool):
                power_instant.setdefault(str(name), []).append(instant)
            if isinstance(average, int) and not isinstance(average, bool):
                power_average.setdefault(str(name), []).append(average)

    ram_used = [sample["ram"]["used_mb"] for sample in samples]
    swap_used = [sample["swap"]["used_mb"] for sample in samples]
    gr3d_usage = [sample["gr3d"]["usage_pct"] for sample in samples]
    emc_usage = [
        sample["emc"]["usage_pct"]
        for sample in samples
        if isinstance(sample.get("emc"), dict)
    ]
    return {
        "resource_schema_version": RESOURCE_SCHEMA_VERSION,
        "sample_count": len(samples),
        "parse_error_count": sum(
            bool(sample.get("parse_errors")) for sample in samples
        ),
        "parse_warning_count": sum(
            bool(sample.get("parse_warnings")) for sample in samples
        ),
        "first_sample_monotonic_ns": monotonic[0] if monotonic else None,
        "last_sample_monotonic_ns": monotonic[-1] if monotonic else None,
        "sample_span_ns": monotonic[-1] - monotonic[0] if len(monotonic) > 1 else 0,
        "sample_interval_ns": _describe(intervals),
        "ram_used_mb": _describe(ram_used),
        "swap_used_mb": _describe(swap_used),
        "cpu_usage_pct": {
            key: _describe(values) for key, values in sorted(cpu_usage.items())
        },
        "cpu_frequency_mhz": {
            key: _describe(values) for key, values in sorted(cpu_frequency.items())
        },
        "gr3d_usage_pct": _describe(gr3d_usage),
        "emc_usage_pct": _describe(emc_usage),
        "temperatures_c": {
            key: _describe(values) for key, values in sorted(temperatures.items())
        },
        "power_instant_mw": {
            key: _describe(values) for key, values in sorted(power_instant.items())
        },
        "power_reported_average_mw": {
            key: _describe(values) for key, values in sorted(power_average.items())
        },
    }


class TegrastatsSampler:
    """Run one non-daemon tegrastats reader for an entire pilot session."""

    def __init__(
        self,
        session_dir: Path | str,
        interval_ms: int = 200,
        *,
        command: Sequence[str] | None = None,
        sample_callback: Callable[[Mapping[str, object]], None] | None = None,
    ) -> None:
        if isinstance(interval_ms, bool) or not isinstance(interval_ms, int):
            raise TypeError("interval_ms must be an integer")
        if interval_ms < 50 or interval_ms > 10_000:
            raise ValueError("interval_ms must be between 50 and 10000")
        if command is not None and (
            isinstance(command, (str, bytes))
            or not command
            or any(not isinstance(item, str) or not item for item in command)
        ):
            raise ValueError("command must be a non-empty sequence of strings")
        if sample_callback is not None and not callable(sample_callback):
            raise TypeError("sample_callback must be callable or None")
        self.session_dir = Path(session_dir)
        self.interval_ms = interval_ms
        self.path = self.session_dir / "resources.jsonl"
        self._configured_command = tuple(command) if command is not None else None
        self._sample_callback = sample_callback
        self._process: subprocess.Popen[str] | None = None
        self._thread: threading.Thread | None = None
        self._stream: TextIO | None = None
        self._ready = threading.Event()
        self._sample_condition = threading.Condition()
        self._sample_count = 0
        self._parse_error_count = 0
        self._first_sample_ns: int | None = None
        self._last_sample_ns: int | None = None
        self._reader_error_code: str | None = None
        self._stop_report: TegrastatsStopReport | None = None

    @property
    def is_running(self) -> bool:
        return self._process is not None and self._stop_report is None

    @property
    def stop_report(self) -> TegrastatsStopReport | None:
        return self._stop_report

    def _command(self) -> list[str]:
        if self._configured_command is not None:
            return list(self._configured_command)
        binary = shutil.which("tegrastats")
        if binary is None:
            raise RuntimeError("tegrastats was not found in PATH")
        return [binary, "--interval", str(self.interval_ms)]

    def start(self, *, first_sample_timeout_s: float = 3.0) -> None:
        if self._process is not None or self._stop_report is not None:
            raise RuntimeError("tegrastats sampler has already been started")
        if (
            isinstance(first_sample_timeout_s, bool)
            or not isinstance(first_sample_timeout_s, (int, float))
            or not math.isfinite(first_sample_timeout_s)
            or first_sample_timeout_s <= 0
        ):
            raise ValueError("first_sample_timeout_s must be positive and finite")
        self.session_dir.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            raise FileExistsError(f"refusing to overwrite telemetry: {self.path}")
        self._stream = self.path.open(
            "w", encoding="utf-8", buffering=64 * 1024, newline="\n"
        )
        try:
            self._process = subprocess.Popen(
                self._command(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except Exception:
            self._stream.close()
            self._stream = None
            raise
        self._thread = threading.Thread(
            target=self._reader_loop,
            name="phase1-tegrastats-reader",
            daemon=False,
        )
        try:
            self._thread.start()
        except Exception:
            if self._process.poll() is None:
                self._process.terminate()
                try:
                    self._process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self._process.kill()
                    self._process.wait(timeout=2)
            if self._process.stdout is not None:
                self._process.stdout.close()
            self._stream.close()
            self._process = None
            self._thread = None
            self._stream = None
            raise
        if not self._ready.wait(float(first_sample_timeout_s)):
            self.stop()
            raise TimeoutError("tegrastats did not produce a sample before the timeout")
        if self._sample_count == 0:
            error_code = self._reader_error_code or "tegrastats_exited_without_sample"
            self.stop()
            raise RuntimeError(f"tegrastats startup failed: {error_code}")
        if self._parse_error_count:
            self.stop()
            raise RuntimeError(
                "the first tegrastats sample did not satisfy the parser contract"
            )

    def _reader_loop(self) -> None:
        assert self._process is not None
        assert self._process.stdout is not None
        assert self._stream is not None
        try:
            for line in self._process.stdout:
                if not line.strip():
                    continue
                monotonic_ns = time.monotonic_ns()
                sample = parse_tegrastats_line(
                    line,
                    sequence=self._sample_count,
                    sample_monotonic_ns=monotonic_ns,
                    sample_wall_time_ns=time.time_ns(),
                )
                self._stream.write(
                    json.dumps(
                        sample,
                        ensure_ascii=False,
                        allow_nan=False,
                        separators=(",", ":"),
                    )
                    + "\n"
                )
                self._stream.flush()
                if self._sample_callback is not None:
                    self._sample_callback(sample)
                with self._sample_condition:
                    self._sample_count += 1
                    self._parse_error_count += int(bool(sample["parse_errors"]))
                    if self._first_sample_ns is None:
                        self._first_sample_ns = monotonic_ns
                    self._last_sample_ns = monotonic_ns
                    self._sample_condition.notify_all()
                self._ready.set()
        except Exception as exc:
            with self._sample_condition:
                self._reader_error_code = type(exc).__name__.lower()
                self._sample_condition.notify_all()
        finally:
            self._ready.set()
            with self._sample_condition:
                self._sample_condition.notify_all()

    def wait_for_sample_at_or_after(
        self,
        monotonic_ns: int,
        *,
        timeout_s: float = 2.0,
    ) -> int:
        """Wait until the resource trace covers a monotonic-time boundary."""

        if (
            isinstance(monotonic_ns, bool)
            or not isinstance(monotonic_ns, int)
            or monotonic_ns < 0
        ):
            raise ValueError("monotonic_ns must be a nonnegative integer")
        if (
            isinstance(timeout_s, bool)
            or not isinstance(timeout_s, (int, float))
            or not math.isfinite(timeout_s)
            or timeout_s <= 0
        ):
            raise ValueError("timeout_s must be positive and finite")
        if self._process is None or self._stop_report is not None:
            raise RuntimeError("tegrastats sampler is not running")

        deadline = time.monotonic() + float(timeout_s)
        with self._sample_condition:
            while self._last_sample_ns is None or self._last_sample_ns < monotonic_ns:
                if self._reader_error_code is not None:
                    raise RuntimeError(
                        "tegrastats reader failed before covering the boundary"
                    )
                if self._process.poll() is not None:
                    raise RuntimeError("tegrastats exited before covering the boundary")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        "tegrastats did not cover the boundary before the timeout"
                    )
                self._sample_condition.wait(remaining)
            return self._last_sample_ns

    def stop(
        self,
        *,
        terminate_timeout_s: float = 2.0,
        join_timeout_s: float = 2.0,
    ) -> TegrastatsStopReport:
        if self._stop_report is not None:
            return self._stop_report
        for value, name in (
            (terminate_timeout_s, "terminate_timeout_s"),
            (join_timeout_s, "join_timeout_s"),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ValueError(f"{name} must be positive and finite")
        process = self._process
        if process is None:
            raise RuntimeError("tegrastats sampler has not been started")
        stop_method = "exited"
        if process.poll() is None:
            stop_method = "terminated"
            process.terminate()
            try:
                process.wait(timeout=terminate_timeout_s)
            except subprocess.TimeoutExpired:
                stop_method = "killed"
                process.kill()
                process.wait(timeout=terminate_timeout_s)

        thread = self._thread
        if thread is not None:
            thread.join(timeout=join_timeout_s)
        reader_joined = thread is None or not thread.is_alive()
        if not reader_joined and process.stdout is not None:
            process.stdout.close()
            thread.join(timeout=join_timeout_s)
            reader_joined = not thread.is_alive()
        if not reader_joined and self._reader_error_code is None:
            self._reader_error_code = "reader_join_timeout"
        if process.stdout is not None and not process.stdout.closed:
            process.stdout.close()
        if self._stream is not None:
            self._stream.flush()
            self._stream.close()

        self._stop_report = TegrastatsStopReport(
            sample_count=self._sample_count,
            parse_error_count=self._parse_error_count,
            first_sample_monotonic_ns=self._first_sample_ns,
            last_sample_monotonic_ns=self._last_sample_ns,
            process_returncode=process.returncode,
            stop_method=stop_method,
            reader_joined=reader_joined,
            reader_error_code=self._reader_error_code,
        )
        return self._stop_report

    def __enter__(self) -> "TegrastatsSampler":
        self.start()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.stop()
