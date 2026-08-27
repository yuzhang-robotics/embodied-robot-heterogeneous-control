"""Condition orchestration for the Phase 1 simulated-load protocol."""

from __future__ import annotations

import hashlib
import math
import threading
import time
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Callable

from experiments.phase1.replay_lifecycle import TraceProfile
from jetson.phase1_runtime import (
    BoundedTaskBroker,
    BrokerSnapshot,
    CancellationToken,
    ClaimedTask,
    EventStatus,
    ExecutorShutdownReport,
    LaneConfig,
    ObservableExecutor,
    OverflowPolicy,
    PayloadRef,
    PeriodicProbe,
    ProbeStopReport,
    ResultEnvelope,
    RuntimeEvent,
    RuntimeEventSink,
    SimulatedAdapter,
    StateToken,
    TaskEnvelope,
    TaskKind,
    build_probe_tick,
    select_release,
)


_SIMULATED_PAYLOAD_SHA256 = hashlib.sha256(
    b"phase1-simulated-bounded-payload-v1"
).hexdigest()


class SimulationCondition(str, Enum):
    """Conditions that separate timing isolation from runtime semantics."""

    R0_IDLE = "r0_idle"
    R1_INLINE_SYNC = "r1_inline_sync"
    R2_THREADED_SYNC = "r2_threaded_sync"
    R3_ASYNC = "r3_async"
    R4_STALE = "r4_stale"
    R4_OVERFLOW = "r4_overflow"

    @property
    def trace_profile(self) -> TraceProfile:
        if self is SimulationCondition.R1_INLINE_SYNC:
            return TraceProfile.INLINE_PROBE
        if self in {
            SimulationCondition.R0_IDLE,
            SimulationCondition.R2_THREADED_SYNC,
        }:
            return TraceProfile.THREADED_PROBE
        return TraceProfile.RUNTIME_THREADED_PROBE

    @property
    def uses_runtime(self) -> bool:
        return self in {
            SimulationCondition.R3_ASYNC,
            SimulationCondition.R4_STALE,
            SimulationCondition.R4_OVERFLOW,
        }


def _finite_seconds(value: object, name: str, *, positive: bool = False) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
        or (positive and value == 0)
    ):
        qualifier = "positive" if positive else "non-negative"
        raise ValueError(f"{name} must be a finite {qualifier} number")
    return float(value)


@dataclass(frozen=True, slots=True)
class ScenarioSpec:
    """One immutable simulated condition with an explicit timing budget."""

    condition: SimulationCondition
    service_time_s: float
    prelude_s: float = 0.2
    postlude_s: float = 0.2
    probe_period_ns: int = 100_000_000
    probe_deadline_ns: int = 100_000_000
    pending_capacity: int = 1
    result_capacity: int = 1
    overflow_submissions: int = 2
    adapter_poll_interval_s: float = 0.01
    join_timeout_s: float = 5.0

    def __post_init__(self) -> None:
        if not isinstance(self.condition, SimulationCondition):
            raise TypeError("condition must be a SimulationCondition")
        service = _finite_seconds(self.service_time_s, "service_time_s")
        prelude = _finite_seconds(self.prelude_s, "prelude_s")
        postlude = _finite_seconds(self.postlude_s, "postlude_s")
        poll_interval = _finite_seconds(
            self.adapter_poll_interval_s,
            "adapter_poll_interval_s",
            positive=True,
        )
        join_timeout = _finite_seconds(
            self.join_timeout_s, "join_timeout_s", positive=True
        )
        for value, name, upper in (
            (service, "service_time_s", 3600.0),
            (prelude, "prelude_s", 3600.0),
            (postlude, "postlude_s", 3600.0),
            (poll_interval, "adapter_poll_interval_s", 1.0),
            (join_timeout, "join_timeout_s", 3600.0),
        ):
            if value > upper:
                raise ValueError(f"{name} must not exceed {upper}")
        for value, name in (
            (self.probe_period_ns, "probe_period_ns"),
            (self.probe_deadline_ns, "probe_deadline_ns"),
            (self.pending_capacity, "pending_capacity"),
            (self.result_capacity, "result_capacity"),
            (self.overflow_submissions, "overflow_submissions"),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if (
            self.condition
            in {
                SimulationCondition.R4_STALE,
                SimulationCondition.R4_OVERFLOW,
            }
            and service == 0
        ):
            raise ValueError("R4 conditions require a positive service time")

    @property
    def trace_profile(self) -> TraceProfile:
        return self.condition.trace_profile

    def to_dict(self) -> dict[str, object]:
        return {
            "condition": self.condition.value,
            "trace_profile": self.trace_profile.value,
            "service_time_s": self.service_time_s,
            "prelude_s": self.prelude_s,
            "postlude_s": self.postlude_s,
            "probe_period_ns": self.probe_period_ns,
            "probe_deadline_ns": self.probe_deadline_ns,
            "pending_capacity": self.pending_capacity,
            "result_capacity": self.result_capacity,
            "overflow_submissions": self.overflow_submissions,
            "adapter_poll_interval_s": self.adapter_poll_interval_s,
            "join_timeout_s": self.join_timeout_s,
        }


@dataclass(frozen=True, slots=True)
class InlineProbeReport:
    tick_count: int
    skipped_releases: int
    deadline_miss_count: int
    max_lateness_ns: int
    max_gap_ns: int
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class DirectWorkloadReport:
    task_id: str
    started_monotonic_ns: int
    finished_monotonic_ns: int
    execution_outcome: str
    cancellation_requested: bool


@dataclass(frozen=True, slots=True)
class SimulationReport:
    condition: str
    trace_profile: str
    started_monotonic_ns: int
    finished_monotonic_ns: int
    direct_workload: DirectWorkloadReport | None
    probe: InlineProbeReport | ProbeStopReport
    runtime: dict[str, object] | None

    def to_dict(self) -> dict[str, object]:
        return {
            "condition": self.condition,
            "trace_profile": self.trace_profile,
            "started_monotonic_ns": self.started_monotonic_ns,
            "finished_monotonic_ns": self.finished_monotonic_ns,
            "duration_ns": self.finished_monotonic_ns - self.started_monotonic_ns,
            "direct_workload": (
                None if self.direct_workload is None else asdict(self.direct_workload)
            ),
            "probe": asdict(self.probe),
            "runtime": self.runtime,
        }


class InlineProbe:
    """Advance an absolute probe schedule on the calling thread."""

    def __init__(
        self,
        *,
        period_ns: int,
        deadline_ns: int,
        event_sink: RuntimeEventSink,
        work: Callable[[], None] | None = None,
        clock_ns: Callable[[], int] = time.monotonic_ns,
        wait: Callable[[float], None] = time.sleep,
    ) -> None:
        for value, name in (
            (period_ns, "period_ns"),
            (deadline_ns, "deadline_ns"),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if not callable(getattr(event_sink, "emit", None)):
            raise TypeError("event_sink must provide emit(event)")
        if not callable(clock_ns) or not callable(wait):
            raise TypeError("clock_ns and wait must be callable")
        self.period_ns = period_ns
        self.deadline_ns = deadline_ns
        self._event_sink = event_sink
        self._work = work or (lambda: None)
        self._clock_ns = clock_ns
        self._wait = wait
        self._origin_ns: int | None = None
        self._next_index = 0
        self._previous_started_ns: int | None = None
        self._tick_count = 0
        self._skipped_releases = 0
        self._deadline_miss_count = 0
        self._max_lateness_ns = 0
        self._max_gap_ns = 0
        self._stopped = False

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

    def start(self) -> None:
        if self._origin_ns is not None:
            raise RuntimeError("inline probe has already been started")
        self._origin_ns = self._clock_ns()
        self._emit(
            "probe.started",
            EventStatus.STARTED,
            {
                "origin_monotonic_ns": self._origin_ns,
                "period_ns": self.period_ns,
                "deadline_ns": self.deadline_ns,
                "thread_name": "phase1-inline-probe",
            },
        )

    def _wait_until(self, target_ns: int) -> None:
        while True:
            remaining_ns = target_ns - self._clock_ns()
            if remaining_ns <= 0:
                return
            self._wait(remaining_ns / 1_000_000_000)

    def run_until(self, end_monotonic_ns: int) -> None:
        if self._origin_ns is None or self._stopped:
            raise RuntimeError("inline probe is not running")
        if (
            isinstance(end_monotonic_ns, bool)
            or not isinstance(end_monotonic_ns, int)
            or end_monotonic_ns < self._origin_ns
        ):
            raise ValueError("end_monotonic_ns must not precede probe start")

        while True:
            scheduled = self._origin_ns + self._next_index * self.period_ns
            if scheduled > end_monotonic_ns:
                return
            self._wait_until(scheduled)
            observed_start = self._clock_ns()
            selected_index, selected_release, skipped = select_release(
                self._origin_ns,
                self._next_index,
                observed_start,
                self.period_ns,
            )
            if selected_release > end_monotonic_ns:
                return
            if skipped:
                self._emit(
                    "probe.skipped",
                    EventStatus.DROPPED,
                    {
                        "from_index": self._next_index,
                        "to_index": selected_index,
                        "skipped_releases": skipped,
                        "observed_monotonic_ns": observed_start,
                    },
                )
            started = self._clock_ns()
            self._work()
            finished = self._clock_ns()
            tick = build_probe_tick(
                index=selected_index,
                scheduled_ns=selected_release,
                started_ns=started,
                finished_ns=finished,
                previous_started_ns=self._previous_started_ns,
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
            self._tick_count += 1
            self._skipped_releases += skipped
            self._deadline_miss_count += int(tick.deadline_miss)
            self._max_lateness_ns = max(self._max_lateness_ns, tick.start_lateness_ns)
            if tick.actual_period_ns is not None:
                self._max_gap_ns = max(self._max_gap_ns, tick.actual_period_ns)
            self._previous_started_ns = started
            self._next_index = selected_index + 1

    def stop(self) -> InlineProbeReport:
        if self._origin_ns is None or self._stopped:
            raise RuntimeError("inline probe is not running")
        self._stopped = True
        report = InlineProbeReport(
            tick_count=self._tick_count,
            skipped_releases=self._skipped_releases,
            deadline_miss_count=self._deadline_miss_count,
            max_lateness_ns=self._max_lateness_ns,
            max_gap_ns=self._max_gap_ns,
        )
        self._emit(
            "probe.stopped",
            EventStatus.OK,
            {
                "tick_count": report.tick_count,
                "skipped_releases": report.skipped_releases,
                "deadline_miss_count": report.deadline_miss_count,
                "max_lateness_ns": report.max_lateness_ns,
                "max_gap_ns": report.max_gap_ns,
                "error_code": report.error_code,
            },
        )
        return report


def _make_task(
    task_id: str,
    *,
    generation: int,
    service_time_s: float,
    postlude_s: float,
) -> TaskEnvelope:
    now = time.monotonic_ns()
    validity_s = max(1.0, service_time_s * 2 + postlude_s + 1.0)
    return TaskEnvelope(
        task_id=task_id,
        task_kind=TaskKind.SIMULATED,
        source_monotonic_ns=now,
        created_monotonic_ns=now,
        deadline_monotonic_ns=now + int(validity_s * 1_000_000_000),
        state_token=StateToken("simulation", generation),
        payload=PayloadRef(
            ref="simulation://bounded-payload-v1",
            sha256=_SIMULATED_PAYLOAD_SHA256,
            size_bytes=34,
            media_type="application/octet-stream",
        ),
        supersession_key="simulation-latest",
        metadata={"protocol": "phase1b1"},
    )


def _run_direct(
    spec: ScenarioSpec,
    adapter: SimulatedAdapter,
) -> DirectWorkloadReport:
    task = _make_task(
        "direct-001",
        generation=0,
        service_time_s=spec.service_time_s,
        postlude_s=spec.postlude_s,
    )
    started = time.monotonic_ns()
    result: ResultEnvelope = adapter(
        ClaimedTask(
            task=task,
            cancellation_token=CancellationToken(),
            started_monotonic_ns=started,
        )
    )
    return DirectWorkloadReport(
        task_id=task.task_id,
        started_monotonic_ns=result.started_monotonic_ns,
        finished_monotonic_ns=result.finished_monotonic_ns,
        execution_outcome=result.execution_outcome.value,
        cancellation_requested=result.cancellation_report.requested,
    )


def _sleep(seconds: float) -> None:
    if seconds > 0:
        threading.Event().wait(seconds)


def _wait_for(
    predicate: Callable[[], bool],
    *,
    timeout_s: float,
    description: str,
) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        threading.Event().wait(0.001)
    raise TimeoutError(f"timed out waiting for {description}")


def _snapshot_dict(snapshot: BrokerSnapshot) -> dict[str, object]:
    return {
        "state": snapshot.state.value,
        "submission_attempts": snapshot.submission_attempts,
        "admitted_total": snapshot.admitted_total,
        "rejected_at_ingress_total": snapshot.rejected_at_ingress_total,
        "queued": snapshot.queued,
        "running": snapshot.running,
        "result_pending": snapshot.result_pending,
        "terminal_admitted_total": snapshot.terminal_admitted_total,
        "max_pending_depth": snapshot.max_pending_depth,
        "max_result_depth": snapshot.max_result_depth,
        "accounting_holds": snapshot.accounting_holds,
        "disposition_counts": {
            disposition.value: count
            for disposition, count in snapshot.disposition_counts
        },
    }


def _shutdown_dict(report: ExecutorShutdownReport) -> dict[str, object]:
    return {
        "complete": report.complete,
        "broker_state": report.broker_state.value,
        "joined": report.joined,
        "join_latency_ns": report.join_latency_ns,
        "queued_ids": list(report.queued_ids),
        "active_id": report.active_id,
        "result_pending_ids": list(report.result_pending_ids),
        "active_cancellation_requested": report.active_cancellation_requested,
        "worker_error_code": report.worker_error_code,
        "event_error_code": report.event_error_code,
    }


def _make_runtime(
    spec: ScenarioSpec,
    event_sink: RuntimeEventSink,
    *,
    observe_cancellation: bool,
    overflow_policy: OverflowPolicy = OverflowPolicy.REJECT_NEW,
    start_gate: threading.Event | None = None,
) -> tuple[ObservableExecutor, PeriodicProbe, threading.Event]:
    broker = BoundedTaskBroker(
        LaneConfig(
            task_kind=TaskKind.SIMULATED,
            pending_capacity=spec.pending_capacity,
            result_capacity=spec.result_capacity,
            overflow_policy=overflow_policy,
        )
    )
    adapter = SimulatedAdapter(
        spec.service_time_s,
        observe_cancellation=observe_cancellation,
        poll_interval_s=spec.adapter_poll_interval_s,
    )
    adapter_started = threading.Event()

    def observed_adapter(claimed: ClaimedTask) -> ResultEnvelope:
        adapter_started.set()
        if start_gate is not None and not start_gate.wait(spec.join_timeout_s):
            raise TimeoutError("simulation start gate was not released")
        return adapter(claimed)

    executor = ObservableExecutor(broker, observed_adapter, event_sink=event_sink)
    probe = PeriodicProbe(
        period_ns=spec.probe_period_ns,
        deadline_ns=spec.probe_deadline_ns,
        event_sink=event_sink,
    )
    return executor, probe, adapter_started


def _runtime_report(
    executor: ObservableExecutor,
    shutdown: ExecutorShutdownReport,
) -> dict[str, object]:
    return {
        "shutdown": _shutdown_dict(shutdown),
        "final_snapshot": _snapshot_dict(executor.snapshot()),
    }


def _run_idle(
    spec: ScenarioSpec,
    event_sink: RuntimeEventSink,
) -> tuple[None, ProbeStopReport, None]:
    probe = PeriodicProbe(
        period_ns=spec.probe_period_ns,
        deadline_ns=spec.probe_deadline_ns,
        event_sink=event_sink,
    )
    probe.start()
    try:
        _sleep(spec.prelude_s + spec.service_time_s + spec.postlude_s)
    finally:
        report = probe.stop(join_timeout_s=spec.join_timeout_s)
    if not report.joined:
        raise RuntimeError("periodic probe did not join")
    return None, report, None


def _run_inline_sync(
    spec: ScenarioSpec,
    event_sink: RuntimeEventSink,
) -> tuple[DirectWorkloadReport, InlineProbeReport, None]:
    adapter = SimulatedAdapter(
        spec.service_time_s,
        poll_interval_s=spec.adapter_poll_interval_s,
    )
    probe = InlineProbe(
        period_ns=spec.probe_period_ns,
        deadline_ns=spec.probe_deadline_ns,
        event_sink=event_sink,
    )
    probe.start()
    prelude_end = time.monotonic_ns() + int(spec.prelude_s * 1_000_000_000)
    probe.run_until(prelude_end)
    direct = _run_direct(spec, adapter)
    postlude_end = time.monotonic_ns() + int(spec.postlude_s * 1_000_000_000)
    probe.run_until(postlude_end)
    return direct, probe.stop(), None


def _run_threaded_sync(
    spec: ScenarioSpec,
    event_sink: RuntimeEventSink,
) -> tuple[DirectWorkloadReport, ProbeStopReport, None]:
    adapter = SimulatedAdapter(
        spec.service_time_s,
        poll_interval_s=spec.adapter_poll_interval_s,
    )
    probe = PeriodicProbe(
        period_ns=spec.probe_period_ns,
        deadline_ns=spec.probe_deadline_ns,
        event_sink=event_sink,
    )
    probe.start()
    try:
        _sleep(spec.prelude_s)
        direct = _run_direct(spec, adapter)
        _sleep(spec.postlude_s)
    finally:
        probe_report = probe.stop(join_timeout_s=spec.join_timeout_s)
    if not probe_report.joined:
        raise RuntimeError("periodic probe did not join")
    return direct, probe_report, None


def _run_async_nominal(
    spec: ScenarioSpec,
    event_sink: RuntimeEventSink,
) -> tuple[None, ProbeStopReport, dict[str, object]]:
    executor, probe, _ = _make_runtime(
        spec,
        event_sink,
        observe_cancellation=True,
    )
    executor.start()
    probe.start()
    shutdown: ExecutorShutdownReport | None = None
    try:
        _sleep(spec.prelude_s)
        executor.submit(
            _make_task(
                "async-001",
                generation=0,
                service_time_s=spec.service_time_s,
                postlude_s=spec.postlude_s,
            )
        )
        _wait_for(
            lambda: executor.snapshot().result_pending == 1,
            timeout_s=spec.service_time_s + spec.join_timeout_s,
            description="the asynchronous result",
        )
        consumed = executor.consume_next()
        if consumed is None or not consumed.consumed:
            raise RuntimeError("nominal asynchronous result was not consumed")
        _sleep(spec.postlude_s)
        shutdown = executor.shutdown(
            cancel_live=False,
            join_timeout_s=spec.join_timeout_s,
        )
    finally:
        if shutdown is None and executor.is_alive:
            shutdown = executor.shutdown(
                cancel_live=True,
                join_timeout_s=spec.join_timeout_s,
            )
        probe_report = probe.stop(join_timeout_s=spec.join_timeout_s)
    if shutdown is None or not shutdown.complete:
        raise RuntimeError("asynchronous executor did not shut down cleanly")
    if not probe_report.joined:
        raise RuntimeError("periodic probe did not join")
    return None, probe_report, _runtime_report(executor, shutdown)


def _run_stale(
    spec: ScenarioSpec,
    event_sink: RuntimeEventSink,
) -> tuple[None, ProbeStopReport, dict[str, object]]:
    start_gate = threading.Event()
    executor, probe, adapter_started = _make_runtime(
        spec,
        event_sink,
        observe_cancellation=False,
        start_gate=start_gate,
    )
    executor.start()
    probe.start()
    shutdown: ExecutorShutdownReport | None = None
    try:
        _sleep(spec.prelude_s)
        executor.submit(
            _make_task(
                "stale-001",
                generation=0,
                service_time_s=spec.service_time_s,
                postlude_s=spec.postlude_s,
            )
        )
        if not adapter_started.wait(spec.join_timeout_s):
            raise TimeoutError("timed out waiting for the stale scenario worker claim")
        executor.advance_state("simulation", reason="scenario_state_change")
        start_gate.set()
        _wait_for(
            lambda: executor.snapshot().terminal_admitted_total == 1,
            timeout_s=spec.service_time_s + spec.join_timeout_s,
            description="the invalidated result terminal",
        )
        if executor.consume_next() is not None:
            raise RuntimeError("stale scenario unexpectedly exposed a result")
        _sleep(spec.postlude_s)
        shutdown = executor.shutdown(
            cancel_live=False,
            join_timeout_s=spec.join_timeout_s,
        )
    finally:
        start_gate.set()
        if shutdown is None and executor.is_alive:
            shutdown = executor.shutdown(
                cancel_live=True,
                join_timeout_s=spec.join_timeout_s,
            )
        probe_report = probe.stop(join_timeout_s=spec.join_timeout_s)
    if shutdown is None or not shutdown.complete:
        raise RuntimeError("stale scenario executor did not shut down cleanly")
    if not probe_report.joined:
        raise RuntimeError("periodic probe did not join")
    return None, probe_report, _runtime_report(executor, shutdown)


def _run_overflow(
    spec: ScenarioSpec,
    event_sink: RuntimeEventSink,
) -> tuple[None, ProbeStopReport, dict[str, object]]:
    start_gate = threading.Event()
    executor, probe, adapter_started = _make_runtime(
        spec,
        event_sink,
        observe_cancellation=True,
        overflow_policy=OverflowPolicy.DROP_OLDEST,
        start_gate=start_gate,
    )
    executor.start()
    probe.start()
    shutdown: ExecutorShutdownReport | None = None
    try:
        _sleep(spec.prelude_s)
        executor.submit(
            _make_task(
                "overflow-active",
                generation=0,
                service_time_s=spec.service_time_s,
                postlude_s=spec.postlude_s,
            )
        )
        if not adapter_started.wait(spec.join_timeout_s):
            raise TimeoutError(
                "timed out waiting for the overflow scenario worker claim"
            )
        queued_submissions = spec.pending_capacity + spec.overflow_submissions
        for index in range(queued_submissions):
            executor.submit(
                _make_task(
                    f"overflow-queued-{index:03d}",
                    generation=0,
                    service_time_s=spec.service_time_s,
                    postlude_s=spec.postlude_s,
                )
            )
        start_gate.set()
        _wait_for(
            lambda: executor.snapshot().result_pending == 1,
            timeout_s=spec.service_time_s + spec.join_timeout_s,
            description="the first overflow scenario result",
        )
        first_result = executor.consume_next()
        if first_result is None or not first_result.consumed:
            raise RuntimeError("overflow scenario did not consume its first result")
        shutdown = executor.shutdown(
            cancel_live=True,
            join_timeout_s=spec.join_timeout_s,
        )
        _sleep(spec.postlude_s)
    finally:
        start_gate.set()
        if shutdown is None and executor.is_alive:
            shutdown = executor.shutdown(
                cancel_live=True,
                join_timeout_s=spec.join_timeout_s,
            )
        probe_report = probe.stop(join_timeout_s=spec.join_timeout_s)
    if shutdown is None or not shutdown.complete:
        raise RuntimeError("overflow scenario executor did not shut down cleanly")
    if not probe_report.joined:
        raise RuntimeError("periodic probe did not join")
    return None, probe_report, _runtime_report(executor, shutdown)


def run_simulation(
    spec: ScenarioSpec,
    event_sink: RuntimeEventSink,
) -> SimulationReport:
    """Execute one condition and return bounded manifest-ready facts."""

    if not isinstance(spec, ScenarioSpec):
        raise TypeError("spec must be a ScenarioSpec")
    if not callable(getattr(event_sink, "emit", None)):
        raise TypeError("event_sink must provide emit(event)")

    handlers = {
        SimulationCondition.R0_IDLE: _run_idle,
        SimulationCondition.R1_INLINE_SYNC: _run_inline_sync,
        SimulationCondition.R2_THREADED_SYNC: _run_threaded_sync,
        SimulationCondition.R3_ASYNC: _run_async_nominal,
        SimulationCondition.R4_STALE: _run_stale,
        SimulationCondition.R4_OVERFLOW: _run_overflow,
    }
    started = time.monotonic_ns()
    direct, probe, runtime = handlers[spec.condition](spec, event_sink)
    finished = time.monotonic_ns()
    return SimulationReport(
        condition=spec.condition.value,
        trace_profile=spec.trace_profile.value,
        started_monotonic_ns=started,
        finished_monotonic_ns=finished,
        direct_workload=direct,
        probe=probe,
        runtime=runtime,
    )
