"""Absolute-schedule periodic probe for Phase 1 responsiveness studies."""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from typing import Callable

from .events import EventStatus, NullEventSink, RuntimeEvent, RuntimeEventSink


@dataclass(frozen=True, slots=True)
class ProbeTick:
    index: int
    scheduled_monotonic_ns: int
    started_monotonic_ns: int
    finished_monotonic_ns: int
    start_lateness_ns: int
    execution_ns: int
    actual_period_ns: int | None
    signed_period_error_ns: int | None
    absolute_period_error_ns: int | None
    skipped_releases: int
    deadline_miss: bool


@dataclass(frozen=True, slots=True)
class ProbeStopReport:
    joined: bool
    tick_count: int
    skipped_releases: int
    deadline_miss_count: int
    max_lateness_ns: int
    max_gap_ns: int
    error_code: str | None


def select_release(
    origin_ns: int,
    index: int,
    started_ns: int,
    period_ns: int,
) -> tuple[int, int, int]:
    """Select the newest due release and report how many were skipped."""

    for value, name in (
        (origin_ns, "origin_ns"),
        (index, "index"),
        (started_ns, "started_ns"),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{name} must be a non-negative integer")
    if isinstance(period_ns, bool) or not isinstance(period_ns, int) or period_ns <= 0:
        raise ValueError("period_ns must be a positive integer")

    scheduled = origin_ns + index * period_ns
    if started_ns <= scheduled:
        return index, scheduled, 0
    skipped = (started_ns - scheduled) // period_ns
    return index + skipped, scheduled + skipped * period_ns, skipped


def build_probe_tick(
    *,
    index: int,
    scheduled_ns: int,
    started_ns: int,
    finished_ns: int,
    previous_started_ns: int | None,
    period_ns: int,
    deadline_ns: int,
    skipped_releases: int,
) -> ProbeTick:
    """Build one deterministic tick record from monotonic timestamps."""

    if started_ns < scheduled_ns:
        raise ValueError("started_ns must not be before scheduled_ns")
    if finished_ns < started_ns:
        raise ValueError("finished_ns must not be before started_ns")
    actual_period = (
        None if previous_started_ns is None else started_ns - previous_started_ns
    )
    signed_error = None if actual_period is None else actual_period - period_ns
    return ProbeTick(
        index=index,
        scheduled_monotonic_ns=scheduled_ns,
        started_monotonic_ns=started_ns,
        finished_monotonic_ns=finished_ns,
        start_lateness_ns=started_ns - scheduled_ns,
        execution_ns=finished_ns - started_ns,
        actual_period_ns=actual_period,
        signed_period_error_ns=signed_error,
        absolute_period_error_ns=(None if signed_error is None else abs(signed_error)),
        skipped_releases=skipped_releases,
        deadline_miss=finished_ns > scheduled_ns + deadline_ns,
    )


class PeriodicProbe:
    """Run a bounded callback on an interruptible absolute schedule."""

    def __init__(
        self,
        *,
        period_ns: int = 100_000_000,
        deadline_ns: int | None = None,
        work: Callable[[], None] | None = None,
        event_sink: RuntimeEventSink | None = None,
        clock_ns: Callable[[], int] = time.monotonic_ns,
        thread_name: str = "phase1-periodic-probe",
    ) -> None:
        if (
            isinstance(period_ns, bool)
            or not isinstance(period_ns, int)
            or period_ns <= 0
        ):
            raise ValueError("period_ns must be a positive integer")
        resolved_deadline = period_ns if deadline_ns is None else deadline_ns
        if (
            isinstance(resolved_deadline, bool)
            or not isinstance(resolved_deadline, int)
            or resolved_deadline <= 0
        ):
            raise ValueError("deadline_ns must be a positive integer")
        if work is not None and not callable(work):
            raise TypeError("work must be callable or None")
        if event_sink is not None and not callable(getattr(event_sink, "emit", None)):
            raise TypeError("event_sink must provide emit(event)")
        if not callable(clock_ns):
            raise TypeError("clock_ns must be callable")
        if (
            not isinstance(thread_name, str)
            or not 1 <= len(thread_name) <= 64
            or thread_name != thread_name.strip()
            or not thread_name.isprintable()
        ):
            raise ValueError("thread_name must be a bounded printable string")

        self.period_ns = period_ns
        self.deadline_ns = resolved_deadline
        self._work = work or (lambda: None)
        self._event_sink = event_sink or NullEventSink()
        self._clock_ns = clock_ns
        self._thread_name = thread_name
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._started = False
        self._tick_count = 0
        self._skipped_releases = 0
        self._deadline_miss_count = 0
        self._max_lateness_ns = 0
        self._max_gap_ns = 0
        self._error_code: str | None = None

    @property
    def is_alive(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        with self._lock:
            if self._started:
                raise RuntimeError("periodic probe has already been started")
            self._started = True
            self._thread = threading.Thread(
                target=self._run,
                name=self._thread_name,
                daemon=False,
            )
            self._thread.start()

    def _emit(
        self,
        event: str,
        status: EventStatus,
        details: dict[str, str | int | float | bool | None],
    ) -> None:
        self._event_sink.emit(
            RuntimeEvent(
                event=event,
                component="probe",
                status=status,
                details=details,
            )
        )

    def _wait_until(self, target_ns: int) -> bool:
        while True:
            remaining_ns = target_ns - self._clock_ns()
            if remaining_ns <= 0:
                return False
            if self._stop_event.wait(remaining_ns / 1_000_000_000):
                return True

    def _run(self) -> None:
        origin = self._clock_ns()
        index = 0
        previous_started: int | None = None
        try:
            self._emit(
                "probe.started",
                EventStatus.STARTED,
                {
                    "origin_monotonic_ns": origin,
                    "period_ns": self.period_ns,
                    "deadline_ns": self.deadline_ns,
                    "thread_name": self._thread_name,
                },
            )
            while not self._stop_event.is_set():
                scheduled = origin + index * self.period_ns
                if self._wait_until(scheduled):
                    break
                observed_start = self._clock_ns()
                selected_index, selected_release, skipped = select_release(
                    origin,
                    index,
                    observed_start,
                    self.period_ns,
                )
                if skipped:
                    self._emit(
                        "probe.skipped",
                        EventStatus.DROPPED,
                        {
                            "from_index": index,
                            "to_index": selected_index,
                            "skipped_releases": skipped,
                            "observed_monotonic_ns": observed_start,
                        },
                    )
                index = selected_index
                started = self._clock_ns()
                self._work()
                finished = self._clock_ns()
                tick = build_probe_tick(
                    index=index,
                    scheduled_ns=selected_release,
                    started_ns=started,
                    finished_ns=finished,
                    previous_started_ns=previous_started,
                    period_ns=self.period_ns,
                    deadline_ns=self.deadline_ns,
                    skipped_releases=skipped,
                )
                self._emit(
                    "probe.tick",
                    EventStatus.TIMEOUT if tick.deadline_miss else EventStatus.OK,
                    {
                        "tick_index": tick.index,
                        "scheduled_monotonic_ns": tick.scheduled_monotonic_ns,
                        "started_monotonic_ns": tick.started_monotonic_ns,
                        "finished_monotonic_ns": tick.finished_monotonic_ns,
                        "start_lateness_ns": tick.start_lateness_ns,
                        "execution_ns": tick.execution_ns,
                        "actual_period_ns": tick.actual_period_ns,
                        "signed_period_error_ns": tick.signed_period_error_ns,
                        "absolute_period_error_ns": tick.absolute_period_error_ns,
                        "skipped_releases": tick.skipped_releases,
                        "deadline_miss": tick.deadline_miss,
                    },
                )
                with self._lock:
                    self._tick_count += 1
                    self._skipped_releases += skipped
                    self._deadline_miss_count += int(tick.deadline_miss)
                    self._max_lateness_ns = max(
                        self._max_lateness_ns, tick.start_lateness_ns
                    )
                    if tick.actual_period_ns is not None:
                        self._max_gap_ns = max(self._max_gap_ns, tick.actual_period_ns)
                previous_started = started
                index += 1
        except Exception as exc:
            error_name = type(exc).__name__.lower()
            error_code = "probe_" + "".join(
                character if character.isalnum() else "_" for character in error_name
            )
            with self._lock:
                self._error_code = error_code[:64]
            try:
                self._emit(
                    "probe.failed",
                    EventStatus.ERROR,
                    {
                        "error_code": self._error_code,
                        "thread_name": self._thread_name,
                    },
                )
            except Exception:
                pass
        finally:
            try:
                with self._lock:
                    details: dict[str, str | int | float | bool | None] = {
                        "tick_count": self._tick_count,
                        "skipped_releases": self._skipped_releases,
                        "deadline_miss_count": self._deadline_miss_count,
                        "max_lateness_ns": self._max_lateness_ns,
                        "max_gap_ns": self._max_gap_ns,
                        "error_code": self._error_code,
                    }
                self._emit(
                    "probe.stopped",
                    EventStatus.ERROR if self._error_code else EventStatus.OK,
                    details,
                )
            except Exception:
                pass

    def stop(self, *, join_timeout_s: float) -> ProbeStopReport:
        if (
            isinstance(join_timeout_s, bool)
            or not isinstance(join_timeout_s, (int, float))
            or not math.isfinite(join_timeout_s)
            or join_timeout_s < 0
        ):
            raise ValueError("join_timeout_s must be a finite non-negative number")
        with self._lock:
            if not self._started or self._thread is None:
                raise RuntimeError("periodic probe has not been started")
            thread = self._thread
        self._stop_event.set()
        thread.join(timeout=float(join_timeout_s))
        joined = not thread.is_alive()
        with self._lock:
            report = ProbeStopReport(
                joined=joined,
                tick_count=self._tick_count,
                skipped_releases=self._skipped_releases,
                deadline_miss_count=self._deadline_miss_count,
                max_lateness_ns=self._max_lateness_ns,
                max_gap_ns=self._max_gap_ns,
                error_code=self._error_code,
            )
        self._emit(
            "probe.joined",
            EventStatus.OK if joined else EventStatus.TIMEOUT,
            {
                "joined": joined,
                "tick_count": report.tick_count,
                "skipped_releases": report.skipped_releases,
                "deadline_miss_count": report.deadline_miss_count,
                "error_code": report.error_code,
            },
        )
        return report
