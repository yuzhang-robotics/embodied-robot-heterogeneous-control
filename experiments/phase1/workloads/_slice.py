"""Shared single-request lifecycle for fixed-input workload slices."""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from typing import Callable, Generic, Mapping, Protocol, TypeVar

from jetson.phase1_runtime import (
    BoundedTaskBroker,
    BrokerSnapshot,
    ClaimedTask,
    ExecutorShutdownReport,
    FinalDisposition,
    LaneConfig,
    ObservableExecutor,
    OverflowPolicy,
    PayloadRef,
    PeriodicProbe,
    ProbeStopReport,
    ResultEnvelope,
    RuntimeEventSink,
    StateToken,
    TaskEnvelope,
    TaskKind,
)


RecordT = TypeVar("RecordT")


class SliceTiming(Protocol):
    result_validity_s: float
    completion_timeout_s: float
    join_timeout_s: float
    probe_join_timeout_s: float
    prelude_s: float
    postlude_s: float
    probe_period_ns: int
    probe_deadline_ns: int


class SingleRequestAdapter(Protocol[RecordT]):
    inference_started_event: threading.Event

    @property
    def last_record(self) -> RecordT | None: ...

    def __call__(self, claimed: ClaimedTask) -> ResultEnvelope: ...


@dataclass(frozen=True, slots=True)
class SingleRequestOutcome(Generic[RecordT]):
    task_id: str
    state_advanced: bool
    consumed: bool
    final_disposition: str
    adapter_record: RecordT
    shutdown: ExecutorShutdownReport
    probe: ProbeStopReport
    final_snapshot: BrokerSnapshot


def finite_seconds(
    value: object,
    name: str,
    *,
    positive: bool = False,
) -> float:
    """Return a validated finite duration."""

    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
        or (positive and value <= 0)
    ):
        qualifier = "positive" if positive else "non-negative"
        raise ValueError(f"{name} must be a finite {qualifier} number")
    return float(value)


def validate_slice_timing(
    spec: SliceTiming,
    *,
    stale_observation_s: float | None = None,
) -> None:
    """Validate timing fields shared by every fixed-input slice."""

    for field_name in (
        "result_validity_s",
        "completion_timeout_s",
        "join_timeout_s",
        "probe_join_timeout_s",
    ):
        finite_seconds(getattr(spec, field_name), field_name, positive=True)
    for field_name in ("prelude_s", "postlude_s"):
        finite_seconds(getattr(spec, field_name), field_name)
    if stale_observation_s is not None:
        finite_seconds(
            stale_observation_s,
            "stale_observation_s",
            positive=True,
        )
    for field_name in ("probe_period_ns", "probe_deadline_ns"):
        value = getattr(spec, field_name)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{field_name} must be a positive integer")
    if spec.result_validity_s <= spec.completion_timeout_s:
        raise ValueError("result_validity_s must exceed completion_timeout_s")


def make_single_request_task(
    payload: PayloadRef,
    *,
    state_token: StateToken,
    task_id: str,
    task_kind: TaskKind,
    result_validity_s: float,
    metadata: Mapping[str, object],
    supersession_key: str | None = None,
    clock_ns: Callable[[], int] = time.monotonic_ns,
) -> TaskEnvelope:
    """Build one fixed-input task with a monotonic validity deadline."""

    if not isinstance(payload, PayloadRef):
        raise TypeError("payload must be a PayloadRef")
    validity = finite_seconds(
        result_validity_s,
        "result_validity_s",
        positive=True,
    )
    now = clock_ns()
    return TaskEnvelope(
        task_id=task_id,
        task_kind=task_kind,
        source_monotonic_ns=now,
        created_monotonic_ns=now,
        deadline_monotonic_ns=now + int(validity * 1_000_000_000),
        state_token=state_token,
        payload=payload,
        supersession_key=supersession_key,
        metadata=metadata,
    )


def slice_report_dict(
    *,
    condition: str,
    outcome: SingleRequestOutcome[object],
    adapter: Mapping[str, object],
) -> dict[str, object]:
    """Serialize the lifecycle fields shared by workload reports."""

    snapshot = outcome.final_snapshot
    shutdown = outcome.shutdown
    probe = outcome.probe
    return {
        "condition": condition,
        "task_id": outcome.task_id,
        "state_advanced": outcome.state_advanced,
        "consumed": outcome.consumed,
        "final_disposition": outcome.final_disposition,
        "adapter": dict(adapter),
        "shutdown": {
            "complete": shutdown.complete,
            "broker_state": shutdown.broker_state.value,
            "joined": shutdown.joined,
            "join_latency_ns": shutdown.join_latency_ns,
            "active_cancellation_requested": (
                shutdown.active_cancellation_requested
            ),
            "worker_error_code": shutdown.worker_error_code,
            "event_error_code": shutdown.event_error_code,
        },
        "probe": {
            "joined": probe.joined,
            "tick_count": probe.tick_count,
            "skipped_releases": probe.skipped_releases,
            "deadline_miss_count": probe.deadline_miss_count,
            "max_lateness_ns": probe.max_lateness_ns,
            "max_gap_ns": probe.max_gap_ns,
            "error_code": probe.error_code,
        },
        "final_snapshot": {
            "state": snapshot.state.value,
            "submission_attempts": snapshot.submission_attempts,
            "admitted_total": snapshot.admitted_total,
            "terminal_admitted_total": snapshot.terminal_admitted_total,
            "queued": snapshot.queued,
            "running": snapshot.running,
            "result_pending": snapshot.result_pending,
            "max_pending_depth": snapshot.max_pending_depth,
            "max_result_depth": snapshot.max_result_depth,
            "disposition_counts": {
                key.value: value for key, value in snapshot.disposition_counts
            },
            "accounting_holds": snapshot.accounting_holds,
        },
    }


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
        threading.Event().wait(0.01)
    raise TimeoutError(f"timed out waiting for {description}")


def _sleep(seconds: float) -> None:
    if seconds > 0:
        threading.Event().wait(seconds)


def _final_disposition(
    snapshot: BrokerSnapshot,
    *,
    workload_label: str,
) -> FinalDisposition:
    nonzero = [
        disposition for disposition, count in snapshot.disposition_counts if count > 0
    ]
    if len(nonzero) != 1 or snapshot.terminal_admitted_total != 1:
        raise RuntimeError(
            f"{workload_label} slice did not close with one final disposition"
        )
    return nonzero[0]


def run_single_request_slice(
    *,
    workload_label: str,
    task_kind: TaskKind,
    state_scope: str,
    pending_capacity: int,
    result_capacity: int,
    worker_name: str,
    probe_name: str,
    is_stale: bool,
    timing: SliceTiming,
    stale_observation_s: float,
    task_factory: Callable[[StateToken], TaskEnvelope],
    adapter: SingleRequestAdapter[RecordT],
    event_sink: RuntimeEventSink,
) -> SingleRequestOutcome[RecordT]:
    """Execute the common nominal/stale lifecycle and close local threads."""

    broker = BoundedTaskBroker(
        LaneConfig(
            task_kind=task_kind,
            pending_capacity=pending_capacity,
            result_capacity=result_capacity,
            overflow_policy=OverflowPolicy.REJECT_NEW,
        )
    )
    executor = ObservableExecutor(
        broker,
        adapter,
        event_sink=event_sink,
        worker_name=worker_name,
    )
    probe = PeriodicProbe(
        period_ns=timing.probe_period_ns,
        deadline_ns=timing.probe_deadline_ns,
        event_sink=event_sink,
        thread_name=probe_name,
    )
    task = task_factory(broker.current_state_token(state_scope))

    executor.start()
    probe.start()
    shutdown: ExecutorShutdownReport | None = None
    probe_report: ProbeStopReport | None = None
    state_advanced = False
    consumed = False
    try:
        _sleep(timing.prelude_s)
        submission = executor.submit(task)
        if not submission.admitted:
            raise RuntimeError(
                f"the single {workload_label} request was not admitted"
            )

        if is_stale:
            if not adapter.inference_started_event.wait(timing.completion_timeout_s):
                raise TimeoutError(
                    f"{workload_label} inference did not reach its start boundary"
                )
            _sleep(stale_observation_s)
            executor.advance_state(
                state_scope,
                reason="fixed_input_state_change",
            )
            state_advanced = True

        _wait_for(
            lambda: (
                executor.snapshot().result_pending == 1
                or executor.snapshot().terminal_admitted_total == 1
            ),
            timeout_s=timing.completion_timeout_s,
            description=f"the {workload_label} result decision",
        )

        if not is_stale:
            decision = executor.consume_next()
            if decision is None or not decision.consumed:
                raise RuntimeError(
                    f"nominal {workload_label} result was not consumed"
                )
            consumed = True
        elif executor.consume_next() is not None:
            raise RuntimeError(
                f"invalidated {workload_label} result entered the result mailbox"
            )

        _sleep(timing.postlude_s)
        shutdown = executor.shutdown(
            cancel_live=False,
            join_timeout_s=timing.join_timeout_s,
        )
    finally:
        if shutdown is None and executor.is_alive:
            shutdown = executor.shutdown(
                cancel_live=True,
                join_timeout_s=timing.join_timeout_s,
            )
        probe_report = probe.stop(join_timeout_s=timing.probe_join_timeout_s)

    if shutdown is None or not shutdown.complete:
        raise RuntimeError(f"{workload_label} executor did not shut down cleanly")
    if probe_report is None or not probe_report.joined:
        raise RuntimeError(f"{workload_label} periodic probe did not join")
    adapter_record = adapter.last_record
    if (
        adapter_record is None
        or getattr(adapter_record, "task_id", None) != task.task_id
    ):
        raise RuntimeError(
            f"{workload_label} adapter did not publish a matching execution record"
        )

    final_snapshot = executor.snapshot()
    disposition = _final_disposition(
        final_snapshot,
        workload_label=workload_label,
    )
    if not is_stale:
        if disposition is not FinalDisposition.CONSUMED or not consumed:
            raise RuntimeError(
                f"nominal {workload_label} task did not finish as consumed"
            )
    elif disposition is not FinalDisposition.REJECTED_STATE or consumed:
        raise RuntimeError(f"stale {workload_label} task escaped state rejection")

    return SingleRequestOutcome(
        task_id=task.task_id,
        state_advanced=state_advanced,
        consumed=consumed,
        final_disposition=disposition.value,
        adapter_record=adapter_record,
        shutdown=shutdown,
        probe=probe_report,
        final_snapshot=final_snapshot,
    )
