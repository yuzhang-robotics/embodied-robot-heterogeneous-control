"""Linux-only memory and child-process observations for carryover diagnostics."""

from __future__ import annotations

import ctypes
import math
import mmap
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Mapping, cast


OBSERVATION_SCHEMA_VERSION = "0.1.0"
_MEMINFO_FIELDS = ("MemAvailable", "Cached", "SReclaimable")


class ObservationError(RuntimeError):
    """A required diagnostic observation could not be completed."""


def _positive_integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ObservationError(f"{name} must be a positive integer")
    return value


def _nonnegative_integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ObservationError(f"{name} must be a nonnegative integer")
    return value


def _sysconf_integer(name: str) -> int:
    getter = getattr(os, "sysconf", None)
    if not callable(getter):
        raise ObservationError("required POSIX sysconf is unavailable")
    return _positive_integer(int(getter(name)), name)


def parse_meminfo(text: str) -> dict[str, int]:
    """Return the three required `/proc/meminfo` values in bytes."""

    if not isinstance(text, str):
        raise TypeError("meminfo text must be a string")
    values: dict[str, int] = {}
    for line in text.splitlines():
        name, separator, raw = line.partition(":")
        if not separator or name not in _MEMINFO_FIELDS:
            continue
        fields = raw.split()
        if len(fields) != 2 or fields[1] != "kB" or not fields[0].isdigit():
            raise ObservationError(f"invalid /proc/meminfo field: {name}")
        values[name] = int(fields[0]) * 1024
    if set(values) != set(_MEMINFO_FIELDS):
        raise ObservationError("required /proc/meminfo fields are missing")
    return {f"{name}_bytes": values[name] for name in _MEMINFO_FIELDS}


def read_meminfo(path: Path | str = "/proc/meminfo") -> dict[str, int]:
    try:
        text = Path(path).read_text(encoding="ascii", errors="strict")
    except (OSError, UnicodeError) as exc:
        raise ObservationError("could not read /proc/meminfo") from exc
    return parse_meminfo(text)


MincoreProbe = Callable[[int, int, int], bytes]


def _system_mincore(address: int, length: int, page_count: int) -> bytes:
    vector = (ctypes.c_ubyte * page_count)()
    libc = ctypes.CDLL(None, use_errno=True)
    mincore = libc.mincore
    mincore.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p]
    mincore.restype = ctypes.c_int
    if mincore(ctypes.c_void_p(address), ctypes.c_size_t(length), vector) != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))
    return bytes(vector)


def observe_file_residency(
    path: Path | str,
    *,
    platform_name: str = sys.platform,
    page_size: int | None = None,
    mincore_probe: MincoreProbe = _system_mincore,
) -> dict[str, object]:
    """Inspect file-backed resident pages without reading file contents."""

    if not isinstance(platform_name, str) or not platform_name.startswith("linux"):
        raise ObservationError("file residency observation requires Linux")
    if not callable(mincore_probe):
        raise TypeError("mincore_probe must be callable")
    resolved = Path(path).expanduser().resolve()
    if not resolved.is_file():
        raise ObservationError("residency target is not a regular file")
    file_size = _positive_integer(resolved.stat().st_size, "file size")
    resolved_page_size = _positive_integer(
        int(page_size if page_size is not None else _sysconf_integer("SC_PAGE_SIZE")),
        "page size",
    )
    page_count = math.ceil(file_size / resolved_page_size)
    try:
        with resolved.open("rb") as stream:
            with mmap.mmap(
                stream.fileno(), file_size, access=mmap.ACCESS_COPY
            ) as mapped:
                first_byte = ctypes.c_char.from_buffer(mapped)
                address = ctypes.addressof(first_byte)
                del first_byte
                vector = mincore_probe(address, file_size, page_count)
    except (OSError, ValueError, BufferError) as exc:
        raise ObservationError("mincore residency observation failed") from exc
    if not isinstance(vector, bytes) or len(vector) != page_count:
        raise ObservationError("mincore returned an invalid residency vector")
    resident_pages = sum(1 for value in vector if value & 1)
    return {
        "method": "non_touching_mmap_mincore",
        "file_size_bytes": file_size,
        "page_size_bytes": resolved_page_size,
        "total_pages": page_count,
        "resident_pages": resident_pages,
        "resident_fraction": resident_pages / page_count,
        "path_recorded": False,
    }


def memory_observation(
    model_path: Path | str,
    *,
    meminfo_reader: Callable[[], Mapping[str, int]] = read_meminfo,
    residency_reader: Callable[[Path | str], Mapping[str, object]] = (
        observe_file_residency
    ),
    clock_ns: Callable[[], int] = time.monotonic_ns,
) -> dict[str, object]:
    """Capture one fail-closed memory/residency boundary observation."""

    memory = dict(meminfo_reader())
    residency = dict(residency_reader(model_path))
    observation = {
        "observation_schema_version": OBSERVATION_SCHEMA_VERSION,
        "observed_monotonic_ns": clock_ns(),
        "memory": memory,
        "whisper_model_residency": residency,
    }
    validate_memory_observation(observation)
    return observation


def validate_memory_observation(value: Mapping[str, object]) -> None:
    if value.get("observation_schema_version") != OBSERVATION_SCHEMA_VERSION:
        raise ObservationError("unsupported observation schema version")
    observed = value.get("observed_monotonic_ns")
    _positive_integer(observed, "observation monotonic time")
    memory = value.get("memory")
    if not isinstance(memory, Mapping) or set(memory) != {
        "MemAvailable_bytes",
        "Cached_bytes",
        "SReclaimable_bytes",
    }:
        raise ObservationError("memory observation fields are invalid")
    for name, item in memory.items():
        _nonnegative_integer(item, str(name))
    residency = value.get("whisper_model_residency")
    if not isinstance(residency, Mapping):
        raise ObservationError("Whisper residency observation is missing")
    expected = {
        "method",
        "file_size_bytes",
        "page_size_bytes",
        "total_pages",
        "resident_pages",
        "resident_fraction",
        "path_recorded",
    }
    if set(residency) != expected:
        raise ObservationError("Whisper residency fields are invalid")
    total = _positive_integer(residency.get("total_pages"), "total pages")
    resident = residency.get("resident_pages")
    fraction = residency.get("resident_fraction")
    if (
        isinstance(resident, bool)
        or not isinstance(resident, int)
        or not 0 <= resident <= total
        or isinstance(fraction, bool)
        or not isinstance(fraction, (int, float))
        or not math.isfinite(float(fraction))
        or abs(float(fraction) - resident / total) > 1e-12
        or residency.get("method") != "non_touching_mmap_mincore"
        or residency.get("path_recorded") is not False
    ):
        raise ObservationError("Whisper residency values are invalid")


def parse_proc_stat(text: str, *, clock_ticks_per_second: int) -> dict[str, object]:
    """Parse Linux `/proc/<pid>/stat` counters needed by the diagnostic."""

    ticks = _positive_integer(clock_ticks_per_second, "clock ticks per second")
    closing = text.rfind(")")
    if closing < 2:
        raise ObservationError("process stat record is malformed")
    fields = text[closing + 1 :].split()
    if len(fields) < 22:
        raise ObservationError("process stat record is incomplete")
    try:
        minflt = int(fields[7])
        majflt = int(fields[9])
        user_ticks = int(fields[11])
        system_ticks = int(fields[12])
        rss_pages = int(fields[21])
    except ValueError as exc:
        raise ObservationError("process stat counters are invalid") from exc
    if min(minflt, majflt, user_ticks, system_ticks, rss_pages) < 0:
        raise ObservationError("process stat counters are negative")
    return {
        "user_time_s": user_ticks / ticks,
        "system_time_s": system_ticks / ticks,
        "minor_faults": minflt,
        "major_faults": majflt,
        "rss_pages": rss_pages,
    }


def _named_integer(text: str, name: str, *, suffix: str = "") -> int:
    for line in text.splitlines():
        key, separator, raw = line.partition(":")
        if key != name or not separator:
            continue
        fields = raw.split()
        if suffix:
            if len(fields) != 2 or fields[1] != suffix:
                break
        elif len(fields) != 1:
            break
        if fields[0].isdigit():
            return int(fields[0])
        break
    raise ObservationError(f"process field is missing or invalid: {name}")


def read_process_counters(
    pid: int,
    *,
    proc_root: Path | str = "/proc",
    page_size: int | None = None,
    clock_ticks_per_second: int | None = None,
) -> dict[str, object]:
    """Read one live process without recording its command or filesystem path."""

    _positive_integer(pid, "pid")
    root = Path(proc_root) / str(pid)
    try:
        stat = parse_proc_stat(
            (root / "stat").read_text(encoding="ascii", errors="strict"),
            clock_ticks_per_second=int(
                clock_ticks_per_second
                if clock_ticks_per_second is not None
                else _sysconf_integer("SC_CLK_TCK")
            ),
        )
        status = (root / "status").read_text(encoding="ascii", errors="strict")
        io_text = (root / "io").read_text(encoding="ascii", errors="strict")
    except (OSError, UnicodeError) as exc:
        raise ObservationError("could not read live process counters") from exc
    resolved_page_size = _positive_integer(
        int(page_size if page_size is not None else _sysconf_integer("SC_PAGE_SIZE")),
        "page size",
    )
    return {
        "user_time_s": stat["user_time_s"],
        "system_time_s": stat["system_time_s"],
        "minor_faults": stat["minor_faults"],
        "major_faults": stat["major_faults"],
        "rss_bytes": cast(int, stat["rss_pages"]) * resolved_page_size,
        "high_water_rss_bytes": _named_integer(status, "VmHWM", suffix="kB") * 1024,
        "filesystem_read_bytes": _named_integer(io_text, "read_bytes"),
        "voluntary_context_switches": _named_integer(status, "voluntary_ctxt_switches"),
        "involuntary_context_switches": _named_integer(
            status, "nonvoluntary_ctxt_switches"
        ),
    }


ProcessSpawner = Callable[[list[str], Path], subprocess.Popen[bytes]]
ProcessReader = Callable[[int], Mapping[str, object]]


def _spawn_process(command: list[str], cwd: Path) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        command,
        cwd=str(cwd),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
    )


class ObservedProcessFactory:
    """Spawn one ASR child and sample its Linux `/proc` counters until exit."""

    def __init__(
        self,
        *,
        sample_interval_s: float = 0.01,
        spawner: ProcessSpawner = _spawn_process,
        reader: ProcessReader = read_process_counters,
    ) -> None:
        if (
            isinstance(sample_interval_s, bool)
            or not isinstance(sample_interval_s, (int, float))
            or not math.isfinite(float(sample_interval_s))
            or sample_interval_s <= 0
        ):
            raise ValueError("sample_interval_s must be positive and finite")
        if not callable(spawner) or not callable(reader):
            raise TypeError("spawner and reader must be callable")
        self._interval = float(sample_interval_s)
        self._spawner = spawner
        self._reader = reader
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._process: subprocess.Popen[bytes] | None = None
        self._samples: list[dict[str, object]] = []
        self._read_error_count = 0
        self._thread_error: str | None = None

    def __call__(self, command: list[str], cwd: Path) -> subprocess.Popen[bytes]:
        if self._process is not None:
            raise ObservationError("observed process factory can spawn only once")
        process = self._spawner(command, cwd)
        if not isinstance(process, subprocess.Popen):
            raise ObservationError("spawner did not return subprocess.Popen")
        self._process = process
        self._thread = threading.Thread(
            target=self._sample,
            name="phase1-carryover-asr-process-observer",
            daemon=False,
        )
        self._thread.start()
        return process

    def _sample(self) -> None:
        assert self._process is not None
        while not self._stop.is_set():
            try:
                sample = dict(self._reader(self._process.pid))
            except ObservationError:
                self._read_error_count += 1
            except Exception as exc:
                self._thread_error = type(exc).__name__.lower()
                return
            else:
                sample["observed_monotonic_ns"] = time.monotonic_ns()
                self._samples.append(sample)
            if self._process.poll() is not None:
                return
            self._stop.wait(self._interval)

    def finish(self, *, join_timeout_s: float = 2.0) -> dict[str, object]:
        process = self._process
        thread = self._thread
        if process is None or thread is None:
            raise ObservationError("observed process was not started")
        self._stop.set()
        thread.join(join_timeout_s)
        if thread.is_alive():
            raise ObservationError("process observer thread did not join")
        if self._thread_error is not None:
            raise ObservationError(f"process observer failed: {self._thread_error}")
        if process.poll() is None:
            raise ObservationError("observed process is still running")
        if not self._samples:
            raise ObservationError("process observer collected no valid samples")
        last = self._samples[-1]
        report = {
            "observation_schema_version": OBSERVATION_SCHEMA_VERSION,
            "method": "sampled_linux_proc",
            "sample_interval_ms": self._interval * 1000,
            "sample_count": len(self._samples),
            "read_error_count": self._read_error_count,
            "process_exit_observed": True,
            "user_time_s": last.get("user_time_s"),
            "system_time_s": last.get("system_time_s"),
            "minor_faults": last.get("minor_faults"),
            "major_faults": last.get("major_faults"),
            "maximum_rss_bytes": max(
                cast(int, item.get("high_water_rss_bytes", 0)) for item in self._samples
            ),
            "filesystem_read_bytes": last.get("filesystem_read_bytes"),
            "voluntary_context_switches": last.get("voluntary_context_switches"),
            "involuntary_context_switches": last.get("involuntary_context_switches"),
            "pid_recorded": False,
            "command_recorded": False,
        }
        validate_process_observation(report)
        return report


def validate_process_observation(value: Mapping[str, object]) -> None:
    required_nonnegative = (
        "user_time_s",
        "system_time_s",
        "minor_faults",
        "major_faults",
        "maximum_rss_bytes",
        "filesystem_read_bytes",
        "voluntary_context_switches",
        "involuntary_context_switches",
    )
    if (
        value.get("observation_schema_version") != OBSERVATION_SCHEMA_VERSION
        or value.get("method") != "sampled_linux_proc"
        or value.get("process_exit_observed") is not True
        or value.get("pid_recorded") is not False
        or value.get("command_recorded") is not False
    ):
        raise ObservationError("process observation identity is invalid")
    _positive_integer(value.get("sample_count"), "process sample count")
    read_errors = value.get("read_error_count")
    if (
        isinstance(read_errors, bool)
        or not isinstance(read_errors, int)
        or read_errors < 0
    ):
        raise ObservationError("process read error count is invalid")
    for name in required_nonnegative:
        item = value.get(name)
        if (
            isinstance(item, bool)
            or not isinstance(item, (int, float))
            or not math.isfinite(float(item))
            or item < 0
        ):
            raise ObservationError(f"process observation is invalid: {name}")
