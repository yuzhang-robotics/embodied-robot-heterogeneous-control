"""Single-request orchestration for the fixed-input Phase 1 ASR slice."""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Protocol

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

from .asr_adapter import ASRExecutionRecord, FixedInputASRAdapter


class ASRAdapter(Protocol):
    """Minimum parent-side contract for one supervised Whisper invocation."""

    inference_started_event: threading.Event

    @property
    def last_record(self) -> ASRExecutionRecord | None: ...

    def __call__(self, claimed: ClaimedTask) -> ResultEnvelope: ...


class ASRSliceCondition(str, Enum):
    """The first two real ASR correctness conditions."""

    ASYNC = "asr_async"
    STALE = "asr_stale"


def _finite_seconds(value: object, name: str, *, positive: bool = False) -> float:
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


@dataclass(frozen=True, slots=True)
class ASRSliceSpec:
    """Frozen controls for one fixed-input ASR request."""

    condition: ASRSliceCondition
    result_validity_s: float = 180.0
    completion_timeout_s: float = 150.0
    join_timeout_s: float = 10.0
    probe_join_timeout_s: float = 5.0
    prelude_s: float = 1.0
    postlude_s: float = 1.0
    probe_period_ns: int = 100_000_000
    probe_deadline_ns: int = 100_000_000

    def __post_init__(self) -> None:
        if not isinstance(self.condition, ASRSliceCondition):
            raise TypeError("condition must be an ASRSliceCondition")
        for field_name in (
            "result_validity_s",
            "completion_timeout_s",
            "join_timeout_s",
            "probe_join_timeout_s",
        ):
            _finite_seconds(getattr(self, field_name), field_name, positive=True)
        for field_name in ("prelude_s", "postlude_s"):
            _finite_seconds(getattr(self, field_name), field_name)
        for field_name in ("probe_period_ns", "probe_deadline_ns"):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{field_name} must be a positive integer")
        if self.result_validity_s <= self.completion_timeout_s:
            raise ValueError("result_validity_s must exceed completion_timeout_s")

    def to_dict(self) -> dict[str, object]:
        return {
            "condition": self.condition.value,
            "task_kind": TaskKind.ASR.value,
            "request_count": 1,
            "pending_capacity": 2,
            "result_capacity": 2,
            "overflow_policy": OverflowPolicy.REJECT_NEW.value,
            "queue_semantics": "utterance_fifo",
            "result_validity_s": self.result_validity_s,
            "completion_timeout_s": self.completion_timeout_s,
            "join_timeout_s": self.join_timeout_s,
            "probe_join_timeout_s": self.probe_join_timeout_s,
            "prelude_s": self.prelude_s,
            "postlude_s": self.postlude_s,
            "probe_period_ns": self.probe_period_ns,
            "probe_deadline_ns": self.probe_deadline_ns,
            "state_advance_after_inference_start": (
                self.condition is ASRSliceCondition.STALE
            ),
        }


@dataclass(frozen=True, slots=True)
class ASRSliceReport:
    """Closed runtime, probe and adapter facts for one request."""

    condition: ASRSliceCondition
    task_id: str
    state_advanced: bool
    consumed: bool
    final_disposition: str
    adapter: ASRExecutionRecord
    shutdown: ExecutorShutdownReport
    probe: ProbeStopReport
    final_snapshot: BrokerSnapshot

    def to_dict(self) -> dict[str, object]:
        snapshot = self.final_snapshot
        return {
            "condition": self.condition.value,
            "task_id": self.task_id,
            "state_advanced": self.state_advanced,
            "consumed": self.consumed,
            "final_disposition": self.final_disposition,
            "adapter": self.adapter.to_dict(),
            "shutdown": {
                "complete": self.shutdown.complete,
                "broker_state": self.shutdown.broker_state.value,
                "joined": self.shutdown.joined,
                "join_latency_ns": self.shutdown.join_latency_ns,
                "active_cancellation_requested": (
                    self.shutdown.active_cancellation_requested
                ),
                "worker_error_code": self.shutdown.worker_error_code,
                "event_error_code": self.shutdown.event_error_code,
            },
            "probe": {
                "joined": self.probe.joined,
                "tick_count": self.probe.tick_count,
                "skipped_releases": self.probe.skipped_releases,
                "deadline_miss_count": self.probe.deadline_miss_count,
                "max_lateness_ns": self.probe.max_lateness_ns,
                "max_gap_ns": self.probe.max_gap_ns,
                "error_code": self.probe.error_code,
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


def make_asr_task(
    payload: PayloadRef,
    *,
    state_token: StateToken,
    task_id: str,
    result_validity_s: float,
    clock_ns: Callable[[], int] = time.monotonic_ns,
) -> TaskEnvelope:
    """Create the only utterance admitted by one Phase 1D correctness run."""

    if not isinstance(payload, PayloadRef):
        raise TypeError("payload must be a PayloadRef")
    validity = _finite_seconds(
        result_validity_s,
        "result_validity_s",
        positive=True,
    )
    now = clock_ns()
    return TaskEnvelope(
        task_id=task_id,
        task_kind=TaskKind.ASR,
        source_monotonic_ns=now,
        created_monotonic_ns=now,
        deadline_monotonic_ns=now + int(validity * 1_000_000_000),
        state_token=state_token,
        payload=payload,
        metadata={
            "protocol": "phase1d_asr",
            "fixed_input": True,
            "queue_semantics": "utterance_fifo",
            "raw_output_recorded": False,
        },
    )


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


def _final_disposition(snapshot: BrokerSnapshot) -> FinalDisposition:
    nonzero = [
        disposition for disposition, count in snapshot.disposition_counts if count > 0
    ]
    if len(nonzero) != 1 or snapshot.terminal_admitted_total != 1:
        raise RuntimeError("ASR slice did not close with one final disposition")
    return nonzero[0]


def run_asr_slice(
    spec: ASRSliceSpec,
    payload: PayloadRef,
    event_sink: RuntimeEventSink,
    *,
    adapter: ASRAdapter | None = None,
    task_id: str = "asr-001",
) -> ASRSliceReport:
    """Run one nominal or invalidated ASR request and close all processes."""

    if not isinstance(spec, ASRSliceSpec):
        raise TypeError("spec must be an ASRSliceSpec")
    if not isinstance(payload, PayloadRef):
        raise TypeError("payload must be a PayloadRef")
    if not callable(getattr(event_sink, "emit", None)):
        raise TypeError("event_sink must provide emit(event)")
    resolved_adapter = adapter or FixedInputASRAdapter()
    if not callable(resolved_adapter):
        raise TypeError("adapter must be callable")

    broker = BoundedTaskBroker(
        LaneConfig(
            task_kind=TaskKind.ASR,
            pending_capacity=2,
            result_capacity=2,
            overflow_policy=OverflowPolicy.REJECT_NEW,
        )
    )
    executor = ObservableExecutor(
        broker,
        resolved_adapter,
        event_sink=event_sink,
        worker_name="phase1-asr-worker",
    )
    probe = PeriodicProbe(
        period_ns=spec.probe_period_ns,
        deadline_ns=spec.probe_deadline_ns,
        event_sink=event_sink,
        thread_name="phase1-asr-probe",
    )
    task = make_asr_task(
        payload,
        state_token=broker.current_state_token("asr-slice"),
        task_id=task_id,
        result_validity_s=spec.result_validity_s,
    )

    executor.start()
    probe.start()
    shutdown: ExecutorShutdownReport | None = None
    probe_report: ProbeStopReport | None = None
    state_advanced = False
    consumed = False
    try:
        _sleep(spec.prelude_s)
        submission = executor.submit(task)
        if not submission.admitted:
            raise RuntimeError("the single ASR request was not admitted")

        if spec.condition is ASRSliceCondition.STALE:
            if not resolved_adapter.inference_started_event.wait(
                spec.completion_timeout_s
            ):
                raise TimeoutError("ASR inference did not reach its start boundary")
            executor.advance_state(
                "asr-slice",
                reason="fixed_input_state_change",
            )
            state_advanced = True

        _wait_for(
            lambda: (
                executor.snapshot().result_pending == 1
                or executor.snapshot().terminal_admitted_total == 1
            ),
            timeout_s=spec.completion_timeout_s,
            description="the ASR result decision",
        )

        if spec.condition is ASRSliceCondition.ASYNC:
            decision = executor.consume_next()
            if decision is None or not decision.consumed:
                raise RuntimeError("nominal ASR result was not consumed")
            consumed = True
        elif executor.consume_next() is not None:
            raise RuntimeError("invalidated ASR result entered the result mailbox")

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
        probe_report = probe.stop(join_timeout_s=spec.probe_join_timeout_s)

    if shutdown is None or not shutdown.complete:
        raise RuntimeError("ASR executor did not shut down cleanly")
    if probe_report is None or not probe_report.joined:
        raise RuntimeError("ASR periodic probe did not join")
    adapter_record = resolved_adapter.last_record
    if adapter_record is None or adapter_record.task_id != task.task_id:
        raise RuntimeError("ASR adapter did not publish a matching execution record")

    final_snapshot = executor.snapshot()
    disposition = _final_disposition(final_snapshot)
    if spec.condition is ASRSliceCondition.ASYNC:
        if disposition is not FinalDisposition.CONSUMED or not consumed:
            raise RuntimeError("nominal ASR task did not finish as consumed")
    else:
        if disposition is not FinalDisposition.REJECTED_STATE or consumed:
            raise RuntimeError("stale ASR task escaped state rejection")

    return ASRSliceReport(
        condition=spec.condition,
        task_id=task.task_id,
        state_advanced=state_advanced,
        consumed=consumed,
        final_disposition=disposition.value,
        adapter=adapter_record,
        shutdown=shutdown,
        probe=probe_report,
        final_snapshot=final_snapshot,
    )
