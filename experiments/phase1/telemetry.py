"""UTF-8 JSONL recording for Phase 1 runtime and probe observations."""

from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path
from typing import TextIO

from jetson.phase1_runtime.events import RuntimeEvent


SCHEMA_VERSION = "0.2.0"

_RUN_ID_RE = re.compile(
    r"^[0-9]{8}T[0-9]{6}Z_phase1_[a-z][a-z0-9_]{0,31}_"
    r"(?:simulated|vlm|asr|llm)_[0-9]{3}$"
)


class EventRecorder:
    """Append ordered Phase 1 observations to one buffered trace file."""

    def __init__(self, run_dir: Path | str, run_id: str) -> None:
        if not isinstance(run_id, str) or not _RUN_ID_RE.fullmatch(run_id):
            raise ValueError(f"invalid Phase 1 run_id: {run_id!r}")
        self.run_dir = Path(run_dir)
        self.run_id = run_id
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.run_dir / "events.jsonl"
        if self.path.exists():
            raise FileExistsError(f"refusing to overwrite trace: {self.path}")
        self._stream: TextIO = self.path.open(
            "w", encoding="utf-8", buffering=64 * 1024, newline="\n"
        )
        self._sequence = 0
        self._last_monotonic_ns = 0
        self._closed = False
        self._lock = threading.Lock()

    def emit(self, event: RuntimeEvent) -> None:
        """Record one event with sequence and correlation timestamps."""

        if not isinstance(event, RuntimeEvent):
            raise TypeError("event must be a RuntimeEvent")
        with self._lock:
            if self._closed:
                raise RuntimeError("event recorder is closed")
            monotonic_ns = time.monotonic_ns()
            if monotonic_ns < self._last_monotonic_ns:
                raise RuntimeError("event recorder monotonic time moved backwards")

            item: dict[str, object] = {
                "schema_version": SCHEMA_VERSION,
                "run_id": self.run_id,
                "seq": self._sequence,
                "event": event.event,
                "component": event.component,
                "status": event.status.value,
                "monotonic_ns": monotonic_ns,
                "wall_time_ns": time.time_ns(),
                "pid": os.getpid(),
                "thread_id": threading.get_ident(),
                "details": dict(event.details),
            }
            optional: dict[str, object | None] = {
                "task_id": event.task_id,
                "task_kind": (
                    event.task_kind.value if event.task_kind is not None else None
                ),
                "parent_task_id": event.parent_task_id,
                "source_monotonic_ns": event.source_monotonic_ns,
                "deadline_monotonic_ns": event.deadline_monotonic_ns,
                "state_scope_id": (
                    event.state_token.scope_id
                    if event.state_token is not None
                    else None
                ),
                "state_generation": (
                    event.state_token.generation
                    if event.state_token is not None
                    else None
                ),
            }
            item.update(
                {key: value for key, value in optional.items() if value is not None}
            )
            line = json.dumps(
                item,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            )
            self._stream.write(line + "\n")
            self._sequence += 1
            self._last_monotonic_ns = monotonic_ns

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
