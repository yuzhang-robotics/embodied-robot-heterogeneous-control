"""Minimal event and Jetson resource recording for Phase 0 experiments.

This module is deliberately independent from ``jetson.app``. Importing it does
not open devices, start model services, or enable physical motion.
"""

from __future__ import annotations

import csv
import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, TextIO


SCHEMA_VERSION = "0.1.0"

_RUN_ID_RE = re.compile(
    r"^[0-9]{8}T[0-9]{6}Z_phase0_(?:asr|llm|vlm)_[0-9]{3}$"
)
_EVENT_RE = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$")
_COMPONENT_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_STATUSES = {
    "started",
    "ok",
    "error",
    "timeout",
    "cancelled",
    "dropped",
    "stale",
    "info",
}


class EventRecorder:
    """Append schema-shaped trace events to one buffered UTF-8 JSONL file."""

    def __init__(self, run_dir: Path | str, run_id: str) -> None:
        if not _RUN_ID_RE.fullmatch(run_id):
            raise ValueError(f"invalid Phase 0 run_id: {run_id!r}")

        self.run_dir = Path(run_dir)
        self.run_id = run_id
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.run_dir / "events.jsonl"
        if self.path.exists():
            raise FileExistsError(f"refusing to overwrite trace: {self.path}")
        self._stream: TextIO = self.path.open(
            "w", encoding="utf-8", buffering=64 * 1024, newline="\n"
        )
        self._seq = 0
        self._closed = False
        self._lock = threading.Lock()

    def emit(
        self,
        *,
        task_id: str,
        event: str,
        component: str,
        status: str,
        details: dict[str, Any] | None = None,
        parent_task_id: str | None = None,
        source_monotonic_ns: int | None = None,
        deadline_monotonic_ns: int | None = None,
        state_version: int | None = None,
    ) -> dict[str, Any]:
        """Record and return one event.

        ``monotonic_ns`` is captured while holding the sequence lock, so event
        sequence and primary timestamps have the same ordering within this
        process. The stream is buffered and never calls ``fsync`` per event.
        """

        if not task_id or len(task_id) > 128:
            raise ValueError("task_id must contain 1 to 128 characters")
        if parent_task_id is not None and not (1 <= len(parent_task_id) <= 128):
            raise ValueError("parent_task_id must contain 1 to 128 characters")
        if not _EVENT_RE.fullmatch(event):
            raise ValueError(f"invalid event name: {event!r}")
        if not _COMPONENT_RE.fullmatch(component):
            raise ValueError(f"invalid component name: {component!r}")
        if status not in _STATUSES:
            raise ValueError(f"invalid status: {status!r}")
        if details is not None and not isinstance(details, dict):
            raise TypeError("details must be a dictionary")

        with self._lock:
            if self._closed:
                raise RuntimeError("event recorder is closed")

            item: dict[str, Any] = {
                "schema_version": SCHEMA_VERSION,
                "run_id": self.run_id,
                "seq": self._seq,
                "task_id": task_id,
                "event": event,
                "component": component,
                "monotonic_ns": time.monotonic_ns(),
                "wall_time_ns": time.time_ns(),
                "status": status,
                "pid": os.getpid(),
                "thread_id": threading.get_ident(),
                "details": details or {},
            }

            optional = {
                "parent_task_id": parent_task_id,
                "source_monotonic_ns": source_monotonic_ns,
                "deadline_monotonic_ns": deadline_monotonic_ns,
                "state_version": state_version,
            }
            item.update({key: value for key, value in optional.items() if value is not None})

            line = json.dumps(
                item,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            )
            self._stream.write(line + "\n")
            self._seq += 1
            return item

    def flush(self) -> None:
        with self._lock:
            if not self._closed:
                self._stream.flush()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._stream.flush()
            self._stream.close()
            self._closed = True

    def __enter__(self) -> "EventRecorder":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()


_RAM_RE = re.compile(
    r"\bRAM\s+(?P<used>\d+)/(?P<total>\d+)MB\s+"
    r"\(lfb\s+(?P<count>\d+)x(?P<size>\d+)MB\)"
)
_SWAP_RE = re.compile(
    r"\bSWAP\s+(?P<used>\d+)/(?P<total>\d+)MB\s+"
    r"\(cached\s+(?P<cached>\d+)MB\)"
)
_CPU_RE = re.compile(r"\bCPU\s+\[(?P<cores>[^\]]+)\]")
_CORE_RE = re.compile(r"^(?P<usage>\d+)%@(?P<freq>\d+)$")
_GPU_RE = re.compile(r"\bGR3D_FREQ\s+(?P<usage>\d+)%")
_TEMP_RE = re.compile(r"\b(?P<name>cpu|gpu|tj|soc0|soc1|soc2)@(?P<value>[0-9.]+)C")
_POWER_RE = re.compile(
    r"\b(?P<name>VDD_IN|VDD_CPU_GPU_CV|VDD_SOC)\s+"
    r"(?P<instant>\d+)mW/(?P<average>\d+)mW"
)
_TEGRA_TIME_RE = re.compile(
    r"^(?P<date>[0-9]{2}-[0-9]{2}-[0-9]{4})\s+"
    r"(?P<time>[0-9]{2}:[0-9]{2}:[0-9]{2})"
)

RESOURCE_FIELDS = [
    "sample_monotonic_ns",
    "sample_wall_time_ns",
    "tegrastats_time",
    "ram_used_mb",
    "ram_total_mb",
    "ram_lfb_count",
    "ram_lfb_size_mb",
    "swap_used_mb",
    "swap_total_mb",
    "swap_cached_mb",
]
for _core_index in range(6):
    RESOURCE_FIELDS.extend(
        [f"cpu{_core_index}_usage_pct", f"cpu{_core_index}_freq_mhz"]
    )
RESOURCE_FIELDS.extend(
    [
        "gr3d_usage_pct",
        "temp_cpu_c",
        "temp_gpu_c",
        "temp_tj_c",
        "temp_soc0_c",
        "temp_soc1_c",
        "temp_soc2_c",
        "vdd_in_mw",
        "vdd_in_avg_mw",
        "vdd_cpu_gpu_cv_mw",
        "vdd_cpu_gpu_cv_avg_mw",
        "vdd_soc_mw",
        "vdd_soc_avg_mw",
        "parse_error",
        "raw_line",
    ]
)


def parse_tegrastats_line(line: str) -> dict[str, Any]:
    """Parse the fields emitted by the validated JetPack 6.2.2 tegrastats."""

    row: dict[str, Any] = {field: "" for field in RESOURCE_FIELDS}
    row["raw_line"] = line.rstrip("\r\n")
    errors: list[str] = []

    timestamp = _TEGRA_TIME_RE.search(line)
    if timestamp:
        row["tegrastats_time"] = f"{timestamp.group('date')} {timestamp.group('time')}"
    else:
        errors.append("timestamp")

    ram = _RAM_RE.search(line)
    if ram:
        row.update(
            {
                "ram_used_mb": int(ram.group("used")),
                "ram_total_mb": int(ram.group("total")),
                "ram_lfb_count": int(ram.group("count")),
                "ram_lfb_size_mb": int(ram.group("size")),
            }
        )
    else:
        errors.append("ram")

    swap = _SWAP_RE.search(line)
    if swap:
        row.update(
            {
                "swap_used_mb": int(swap.group("used")),
                "swap_total_mb": int(swap.group("total")),
                "swap_cached_mb": int(swap.group("cached")),
            }
        )
    else:
        errors.append("swap")

    cpu = _CPU_RE.search(line)
    if cpu:
        cores = [value.strip() for value in cpu.group("cores").split(",")]
        if len(cores) != 6:
            errors.append(f"cpu_count={len(cores)}")
        for index, value in enumerate(cores[:6]):
            core = _CORE_RE.fullmatch(value)
            if core:
                row[f"cpu{index}_usage_pct"] = int(core.group("usage"))
                row[f"cpu{index}_freq_mhz"] = int(core.group("freq"))
            elif value.lower() != "off":
                errors.append(f"cpu{index}")
    else:
        errors.append("cpu")

    gpu = _GPU_RE.search(line)
    if gpu:
        row["gr3d_usage_pct"] = int(gpu.group("usage"))
    else:
        errors.append("gr3d")

    temperature_names: set[str] = set()
    for match in _TEMP_RE.finditer(line):
        name = match.group("name")
        temperature_names.add(name)
        row[f"temp_{name}_c"] = float(match.group("value"))
    missing_temperatures = {"cpu", "gpu", "tj", "soc0", "soc1", "soc2"} - temperature_names
    if missing_temperatures:
        errors.append(f"temperature={','.join(sorted(missing_temperatures))}")

    power_keys = {
        "VDD_IN": ("vdd_in_mw", "vdd_in_avg_mw"),
        "VDD_CPU_GPU_CV": ("vdd_cpu_gpu_cv_mw", "vdd_cpu_gpu_cv_avg_mw"),
        "VDD_SOC": ("vdd_soc_mw", "vdd_soc_avg_mw"),
    }
    power_names: set[str] = set()
    for match in _POWER_RE.finditer(line):
        name = match.group("name")
        power_names.add(name)
        instant_key, average_key = power_keys[name]
        row[instant_key] = int(match.group("instant"))
        row[average_key] = int(match.group("average"))
    missing_power = power_keys.keys() - power_names
    if missing_power:
        errors.append(f"power={','.join(sorted(missing_power))}")

    row["parse_error"] = ";".join(errors)
    return row


class TegrastatsSampler:
    """Run tegrastats and convert each output line into resources.csv."""

    def __init__(self, run_dir: Path | str, interval_ms: int = 200) -> None:
        if interval_ms < 50:
            raise ValueError("tegrastats interval must be at least 50 ms")

        self.run_dir = Path(run_dir)
        self.interval_ms = interval_ms
        self.path = self.run_dir / "resources.csv"
        self._process: subprocess.Popen[str] | None = None
        self._thread: threading.Thread | None = None
        self._stream: TextIO | None = None
        self._writer: csv.DictWriter | None = None
        self.error: str | None = None

    def start(self) -> None:
        if self._process is not None:
            raise RuntimeError("tegrastats sampler is already running")

        binary = shutil.which("tegrastats")
        if binary is None:
            raise RuntimeError("tegrastats was not found in PATH")

        self.run_dir.mkdir(parents=True, exist_ok=True)
        self._stream = self.path.open("w", encoding="utf-8", newline="", buffering=1)
        self._writer = csv.DictWriter(self._stream, fieldnames=RESOURCE_FIELDS)
        self._writer.writeheader()

        self._process = subprocess.Popen(
            [binary, "--interval", str(self.interval_ms)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        self._thread = threading.Thread(
            target=self._reader_loop,
            name="phase0-tegrastats-reader",
            daemon=True,
        )
        self._thread.start()

    def _reader_loop(self) -> None:
        assert self._process is not None
        assert self._process.stdout is not None
        assert self._writer is not None

        try:
            for line in self._process.stdout:
                if not line.strip():
                    continue
                row = parse_tegrastats_line(line)
                row["sample_monotonic_ns"] = time.monotonic_ns()
                row["sample_wall_time_ns"] = time.time_ns()
                self._writer.writerow(row)
        except Exception as exc:  # captured for the experiment result
            self.error = f"{type(exc).__name__}: {exc}"

    def stop(self) -> None:
        process = self._process
        if process is None:
            return

        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)

        if self._thread is not None:
            self._thread.join(timeout=2)
        if process.stdout is not None:
            process.stdout.close()
        if self._stream is not None:
            self._stream.flush()
            self._stream.close()

        self._thread = None
        self._process = None
        self._writer = None
        self._stream = None

    def __enter__(self) -> "TegrastatsSampler":
        self.start()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.stop()
