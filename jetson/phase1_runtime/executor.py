"""Observable single-worker execution for one bounded inference lane."""

from __future__ import annotations

import hashlib
import math
import threading
import time
from dataclasses import dataclass
from typing import Callable

from .broker import (
    BoundedTaskBroker,
    BrokerSnapshot,
    BrokerState,
    CancellationResult,
    ClaimedTask,
    CompletionResult,
    ConsumptionResult,
    ShutdownResult,
    StateAdvanceResult,
    SubmissionResult,
    TerminalTransition,
)
from .events import EventStatus, NullEventSink, RuntimeEvent, RuntimeEventSink
from .model import (
    CancellationReport,
    ExecutionOutcome,
    FinalDisposition,
    ResultEnvelope,
    TaskEnvelope,
    TaskLocation,
)


WorkloadAdapter = Callable[[ClaimedTask], ResultEnvelope]


@dataclass(frozen=True, slots=True)
class ExecutorShutdownReport:
    """Worker join and remaining broker state after one shutdown request."""

    broker_state: BrokerState
    joined: bool
    join_latency_ns: int
    queued_ids: tuple[str, ...]
    active_id: str | None
    result_pending_ids: tuple[str, ...]
    active_cancellation_requested: bool
    worker_error_code: str | None
    event_error_code: str | None

    @property
    def complete(self) -> bool:
        return (
            self.joined
            and self.broker_state is BrokerState.CLOSED
            and self.worker_error_code is None
            and self.event_error_code is None
        )


@dataclass(slots=True)
class _DepthCursor:
    pending: int
    active: int
    result: int

    @classmethod
    def from_snapshot(cls, snapshot: BrokerSnapshot) -> "_DepthCursor":
        return cls(
            pending=snapshot.queued,
            active=snapshot.running,
            result=snapshot.result_pending,
        )

    def leave(self, location: TaskLocation) -> None:
        if location is TaskLocation.QUEUED:
            self.pending -= 1
        elif location is TaskLocation.RUNNING:
            self.active -= 1
        elif location is TaskLocation.RESULT_PENDING:
            self.result -= 1
        else:
            raise RuntimeError(f"cannot leave lifecycle location: {location.value}")
        if min(self.pending, self.active, self.result) < 0:
            raise RuntimeError("observable depth accounting moved below zero")

    def enter(self, location: TaskLocation) -> None:
        if location is TaskLocation.QUEUED:
            self.pending += 1
        elif location is TaskLocation.RUNNING:
            self.active += 1
        elif location is TaskLocation.RESULT_PENDING:
            self.result += 1
        elif location is not TaskLocation.TERMINAL:
            raise RuntimeError(f"cannot enter lifecycle location: {location.value}")


class ObservableExecutor:
    """Run one adapter thread while emitting replayable lifecycle boundaries."""

    def __init__(
        self,
        broker: BoundedTaskBroker,
        adapter: WorkloadAdapter,
        *,
        event_sink: RuntimeEventSink | None = None,
        clock_ns: Callable[[], int] = time.monotonic_ns,
        worker_name: str = "phase1-worker",
    ) -> None:
        if not isinstance(broker, BoundedTaskBroker):
            raise TypeError("broker must be a BoundedTaskBroker")
        if not callable(adapter):
            raise TypeError("adapter must be callable")
        if event_sink is not None and not callable(getattr(event_sink, "emit", None)):
            raise TypeError("event_sink must provide emit(event)")
        if not callable(clock_ns):
            raise TypeError("clock_ns must be callable")
        if (
            not isinstance(worker_name, str)
            or not 1 <= len(worker_name) <= 64
            or worker_name != worker_name.strip()
            or not worker_name.isprintable()
        ):
            raise ValueError("worker_name must be a bounded printable string")

        self._broker = broker
        self._adapter = adapter
        self._event_sink = event_sink or NullEventSink()
        self._clock_ns = clock_ns
        self._worker_name = worker_name
        self._boundary_lock = threading.RLock()
        self._lifecycle_lock = threading.Lock()
        self._wake_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._started = False
        self._worker_error_code: str | None = None
        self._event_error_code: str | None = None

    @property
    def worker_name(self) -> str:
        return self._worker_name

    @property
    def worker_error_code(self) -> str | None:
        with self._lifecycle_lock:
            return self._worker_error_code

    @property
    def event_error_code(self) -> str | None:
        with self._lifecycle_lock:
            return self._event_error_code

    @property
    def is_alive(self) -> bool:
        with self._lifecycle_lock:
            return self._thread is not None and self._thread.is_alive()

    def _depth_details(
        self,
        cursor: _DepthCursor,
        **extra: str | int | float | bool | None,
    ) -> dict[str, str | int | float | bool | None]:
        details: dict[str, str | int | float | bool | None] = {
            "pending_depth": cursor.pending,
            "active_count": cursor.active,
            "result_depth": cursor.result,
            "pending_capacity": self._broker.config.pending_capacity,
            "result_capacity": self._broker.config.result_capacity,
            "overflow_policy": self._broker.config.overflow_policy.value,
        }
        details.update(extra)
        return details

    def _emit(
        self,
        event: str,
        status: EventStatus,
        cursor: _DepthCursor,
        *,
        component: str = "executor",
        task: TaskEnvelope | None = None,
        task_id: str | None = None,
        details: dict[str, str | int | float | bool | None] | None = None,
    ) -> None:
        payload = self._depth_details(cursor, **(details or {}))
        observation = RuntimeEvent(
            event=event,
            component=component,
            status=status,
            task_id=task.task_id if task is not None else task_id,
            task_kind=task.task_kind if task is not None else None,
            parent_task_id=task.parent_task_id if task is not None else None,
            source_monotonic_ns=(
                task.source_monotonic_ns if task is not None else None
            ),
            deadline_monotonic_ns=(
                task.deadline_monotonic_ns if task is not None else None
            ),
            state_token=task.state_token if task is not None else None,
            details=payload,
        )
        try:
            self._event_sink.emit(observation)
        except Exception as exc:
            error_name = type(exc).__name__.lower()
            error_code = "event_sink_" + "".join(
                character if character.isalnum() else "_" for character in error_name
            )
            with self._lifecycle_lock:
                if self._event_error_code is None:
                    self._event_error_code = error_code[:64]
            self._event_sink = NullEventSink()
            try:
                self._broker.begin_shutdown(cancel_live=True)
            finally:
                self._wake_event.set()
            raise RuntimeError("runtime event sink failed") from exc

    @staticmethod
    def _terminal_status(disposition: FinalDisposition) -> EventStatus:
        if disposition is FinalDisposition.DROPPED_OVERFLOW:
            return EventStatus.DROPPED
        if disposition in {
            FinalDisposition.CANCELLED_QUEUED,
            FinalDisposition.REJECTED_CANCELLED,
            FinalDisposition.SHUTDOWN_CANCELLED,
        }:
            return EventStatus.CANCELLED
        if disposition in {
            FinalDisposition.REJECTED_EXPIRED,
            FinalDisposition.REJECTED_STATE,
            FinalDisposition.REJECTED_IDENTITY,
        }:
            return EventStatus.STALE
        if disposition is FinalDisposition.EXECUTION_ERROR:
            return EventStatus.ERROR
        if disposition is FinalDisposition.CONSUMED:
            return EventStatus.OK
        return EventStatus.REJECTED

    def _emit_terminal(
        self,
        transition: TerminalTransition,
        cursor: _DepthCursor,
    ) -> None:
        cursor.leave(transition.previous_location)
        self._emit(
            "task.terminal",
            self._terminal_status(transition.disposition),
            cursor,
            task_id=transition.task_id,
            details={
                "previous_location": transition.previous_location.value,
                "next_location": TaskLocation.TERMINAL.value,
                "disposition": transition.disposition.value,
                "transition_monotonic_ns": transition.terminal_monotonic_ns,
            },
        )

    @staticmethod
    def _assert_depths(cursor: _DepthCursor, snapshot: BrokerSnapshot) -> None:
        observed = (cursor.pending, cursor.active, cursor.result)
        expected = (snapshot.queued, snapshot.running, snapshot.result_pending)
        if observed != expected:
            raise RuntimeError(
                f"observable depth accounting mismatch: {observed} != {expected}"
            )

    def start(self) -> None:
        """Start the non-daemon worker exactly once."""

        with self._lifecycle_lock:
            if self._started:
                raise RuntimeError("executor worker has already been started")
            self._started = True
            self._thread = threading.Thread(
                target=self._worker_loop,
                name=self._worker_name,
                daemon=False,
            )
            self._thread.start()

    def submit(self, task: TaskEnvelope) -> SubmissionResult:
        """Submit through the observable boundary without blocking the producer."""

        with self._lifecycle_lock:
            if not self._started:
                raise RuntimeError("executor worker has not been started")
        with self._boundary_lock:
            before = self._broker.snapshot()
            cursor = _DepthCursor.from_snapshot(before)
            result = self._broker.submit(task)
            after = self._broker.snapshot()
            for transition in result.terminalized:
                self._emit_terminal(transition, cursor)

            if result.admitted:
                cursor.enter(TaskLocation.QUEUED)
                self._emit(
                    "task.enqueued",
                    EventStatus.OK,
                    cursor,
                    task=task,
                    details={
                        "created_monotonic_ns": task.created_monotonic_ns,
                        "payload_sha256": task.payload.sha256,
                        "payload_size_bytes": task.payload.size_bytes,
                        "payload_media_type": task.payload.media_type,
                        "supersession_key_present": task.supersession_key is not None,
                    },
                )
            else:
                assert result.disposition is not None
                self._emit(
                    "task.rejected",
                    self._terminal_status(result.disposition),
                    cursor,
                    task=task,
                    details={
                        "created_monotonic_ns": task.created_monotonic_ns,
                        "payload_sha256": task.payload.sha256,
                        "disposition": result.disposition.value,
                    },
                )
            self._assert_depths(cursor, after)
        if result.admitted:
            self._wake_event.set()
        return result

    def consume_next(self) -> ConsumptionResult | None:
        """Revalidate and consume one result through the trace boundary."""

        with self._boundary_lock:
            before = self._broker.snapshot()
            cursor = _DepthCursor.from_snapshot(before)
            result = self._broker.consume_next()
            after = self._broker.snapshot()
            if result is None:
                self._assert_depths(cursor, after)
                return None

            cursor.leave(TaskLocation.RESULT_PENDING)
            event_name = "result.accepted" if result.consumed else "result.rejected"
            self._emit(
                event_name,
                self._terminal_status(result.disposition),
                cursor,
                task=result.task,
                details={
                    "previous_location": TaskLocation.RESULT_PENDING.value,
                    "next_location": TaskLocation.TERMINAL.value,
                    "disposition": result.disposition.value,
                    "transition_monotonic_ns": (
                        result.transition.terminal_monotonic_ns
                    ),
                    "result_input_sha256": result.result.input_sha256,
                    "result_task_kind": result.result.task_kind.value,
                    "result_source_monotonic_ns": (result.result.source_monotonic_ns),
                    "result_deadline_monotonic_ns": (
                        result.result.deadline_monotonic_ns
                    ),
                    "result_state_scope_id": result.result.state_token.scope_id,
                    "result_state_generation": (result.result.state_token.generation),
                },
            )
            self._assert_depths(cursor, after)
            return result

    def cancel(self, task_id: str, *, reason: str) -> CancellationResult:
        with self._boundary_lock:
            before = self._broker.snapshot()
            cursor = _DepthCursor.from_snapshot(before)
            result = self._broker.cancel(task_id, reason=reason)
            after = self._broker.snapshot()
            self._emit(
                "task.cancel_requested" if result.found else "task.cancel_missing",
                EventStatus.CANCELLED if result.found else EventStatus.INFO,
                cursor,
                task_id=task_id,
                details={
                    "found": result.found,
                    "request_changed": result.request_changed,
                    "already_terminal": result.already_terminal,
                    "reason": reason,
                },
            )
            if result.transition is not None:
                self._emit_terminal(result.transition, cursor)
            self._assert_depths(cursor, after)
        self._wake_event.set()
        return result

    def advance_state(self, scope_id: str, *, reason: str) -> StateAdvanceResult:
        with self._boundary_lock:
            before = self._broker.snapshot()
            cursor = _DepthCursor.from_snapshot(before)
            previous_generation = dict(before.state_generations).get(scope_id, 0)
            result = self._broker.advance_state(scope_id, reason=reason)
            after = self._broker.snapshot()
            self._emit(
                "state.advanced",
                EventStatus.OK,
                cursor,
                details={
                    "state_scope_id": result.state_token.scope_id,
                    "previous_generation": previous_generation,
                    "state_generation": result.state_token.generation,
                    "active_cancellation_requested": (
                        result.active_cancellation_requested
                    ),
                    "reason": reason,
                },
            )
            for transition in result.terminalized:
                self._emit_terminal(transition, cursor)
            self._assert_depths(cursor, after)
        self._wake_event.set()
        return result

    def snapshot(self) -> BrokerSnapshot:
        with self._boundary_lock:
            return self._broker.snapshot()

    def _claim_once(self) -> tuple[ClaimedTask | None, BrokerSnapshot]:
        with self._boundary_lock:
            before = self._broker.snapshot()
            cursor = _DepthCursor.from_snapshot(before)
            result = self._broker.claim_next()
            after = self._broker.snapshot()
            for transition in result.terminalized:
                self._emit_terminal(transition, cursor)
            if result.claimed is not None:
                cursor.leave(TaskLocation.QUEUED)
                cursor.enter(TaskLocation.RUNNING)
                self._emit(
                    "task.started",
                    EventStatus.STARTED,
                    cursor,
                    component="worker",
                    task=result.claimed.task,
                    details={
                        "previous_location": TaskLocation.QUEUED.value,
                        "next_location": TaskLocation.RUNNING.value,
                        "started_monotonic_ns": (result.claimed.started_monotonic_ns),
                        "worker_name": self._worker_name,
                    },
                )
            self._assert_depths(cursor, after)
            return result.claimed, after

    def _adapter_error_result(
        self,
        claimed: ClaimedTask,
        *,
        error_code: str,
        finished_ns: int,
    ) -> ResultEnvelope:
        task = claimed.task
        return ResultEnvelope(
            task_id=task.task_id,
            task_kind=task.task_kind,
            state_token=task.state_token,
            source_monotonic_ns=task.source_monotonic_ns,
            deadline_monotonic_ns=task.deadline_monotonic_ns,
            input_sha256=task.payload.sha256,
            started_monotonic_ns=claimed.started_monotonic_ns,
            finished_monotonic_ns=max(claimed.started_monotonic_ns, finished_ns),
            execution_outcome=ExecutionOutcome.ERROR,
            error_code=error_code,
            cancellation_report=CancellationReport(
                requested=claimed.cancellation_token.is_requested()
            ),
        )

    def _execute_adapter(self, claimed: ClaimedTask) -> ResultEnvelope:
        try:
            result = self._adapter(claimed)
        except Exception:
            return self._adapter_error_result(
                claimed,
                error_code="adapter_exception",
                finished_ns=self._clock_ns(),
            )
        if not isinstance(result, ResultEnvelope):
            return self._adapter_error_result(
                claimed,
                error_code="invalid_adapter_result",
                finished_ns=self._clock_ns(),
            )
        observed_now = self._clock_ns()
        if result.finished_monotonic_ns > observed_now:
            return self._adapter_error_result(
                claimed,
                error_code="invalid_adapter_timestamp",
                finished_ns=observed_now,
            )
        return result

    def _complete(self, result: ResultEnvelope) -> CompletionResult:
        with self._boundary_lock:
            before = self._broker.snapshot()
            cursor = _DepthCursor.from_snapshot(before)
            completion = self._broker.complete(result)
            after = self._broker.snapshot()
            cursor.leave(TaskLocation.RUNNING)
            next_location = (
                TaskLocation.RESULT_PENDING
                if completion.result_pending
                else TaskLocation.TERMINAL
            )
            cursor.enter(next_location)
            details: dict[str, str | int | float | bool | None] = {
                "previous_location": TaskLocation.RUNNING.value,
                "next_location": next_location.value,
                "started_monotonic_ns": result.started_monotonic_ns,
                "finished_monotonic_ns": result.finished_monotonic_ns,
                "execution_outcome": result.execution_outcome.value,
                "result_task_kind": result.task_kind.value,
                "error_code": result.error_code,
                "result_input_sha256": result.input_sha256,
                "result_source_monotonic_ns": result.source_monotonic_ns,
                "result_deadline_monotonic_ns": result.deadline_monotonic_ns,
                "result_state_scope_id": result.state_token.scope_id,
                "result_state_generation": result.state_token.generation,
                "cancel_requested": result.cancellation_report.requested,
                "cancel_worker_observed": (result.cancellation_report.worker_observed),
                "cancel_backend_stop_confirmed": (
                    result.cancellation_report.backend_stop_confirmed
                ),
            }
            if completion.disposition is not None:
                details["disposition"] = completion.disposition.value
            if completion.transition is not None:
                details["transition_monotonic_ns"] = (
                    completion.transition.terminal_monotonic_ns
                )
            self._emit(
                "task.finished",
                (
                    EventStatus.OK
                    if completion.result_pending
                    else self._terminal_status(completion.disposition)
                ),
                cursor,
                component="worker",
                task_id=result.task_id,
                details=details,
            )
            self._assert_depths(cursor, after)
            return completion

    def _worker_loop(self) -> None:
        try:
            with self._boundary_lock:
                snapshot = self._broker.snapshot()
                cursor = _DepthCursor.from_snapshot(snapshot)
                self._emit(
                    "worker.started",
                    EventStatus.STARTED,
                    cursor,
                    component="worker",
                    details={"worker_name": self._worker_name},
                )

            while True:
                self._wake_event.clear()
                claimed, snapshot = self._claim_once()
                if claimed is not None:
                    result = self._execute_adapter(claimed)
                    self._complete(result)
                    continue
                if (
                    snapshot.state is not BrokerState.OPEN
                    and snapshot.queued == 0
                    and snapshot.running == 0
                ):
                    break
                self._wake_event.wait()
        except Exception as exc:
            error_name = type(exc).__name__.lower()
            error_code = "worker_" + "".join(
                character if character.isalnum() else "_" for character in error_name
            )
            with self._lifecycle_lock:
                self._worker_error_code = error_code[:64]
            try:
                with self._boundary_lock:
                    snapshot = self._broker.snapshot()
                    self._emit(
                        "worker.failed",
                        EventStatus.ERROR,
                        _DepthCursor.from_snapshot(snapshot),
                        component="worker",
                        details={
                            "worker_name": self._worker_name,
                            "error_code": self._worker_error_code,
                            "event_error_code": self.event_error_code,
                        },
                    )
            except Exception:
                pass
        finally:
            try:
                with self._boundary_lock:
                    snapshot = self._broker.snapshot()
                    self._emit(
                        "worker.stopped",
                        (
                            EventStatus.ERROR
                            if (
                                self.worker_error_code is not None
                                or self.event_error_code is not None
                            )
                            else EventStatus.OK
                        ),
                        _DepthCursor.from_snapshot(snapshot),
                        component="worker",
                        details={
                            "worker_name": self._worker_name,
                            "error_code": self.worker_error_code,
                            "event_error_code": self.event_error_code,
                        },
                    )
            except Exception:
                pass

    def shutdown(
        self,
        *,
        cancel_live: bool,
        join_timeout_s: float,
    ) -> ExecutorShutdownReport:
        """Request broker shutdown, wake the worker, and join within a budget."""

        if (
            isinstance(join_timeout_s, bool)
            or not isinstance(join_timeout_s, (int, float))
            or not math.isfinite(join_timeout_s)
            or join_timeout_s < 0
        ):
            raise ValueError("join_timeout_s must be a finite non-negative number")
        with self._lifecycle_lock:
            if not self._started or self._thread is None:
                raise RuntimeError("executor worker has not been started")
            thread = self._thread

        with self._boundary_lock:
            before = self._broker.snapshot()
            cursor = _DepthCursor.from_snapshot(before)
            shutdown_result: ShutdownResult = self._broker.begin_shutdown(
                cancel_live=cancel_live
            )
            after = self._broker.snapshot()
            self._emit(
                "shutdown.requested",
                EventStatus.CANCELLED if cancel_live else EventStatus.INFO,
                cursor,
                details={
                    "cancel_live": cancel_live,
                    "broker_state": shutdown_result.state.value,
                    "active_cancellation_requested": (
                        shutdown_result.active_cancellation_requested
                    ),
                },
            )
            for transition in shutdown_result.terminalized:
                self._emit_terminal(transition, cursor)
            self._assert_depths(cursor, after)

        self._wake_event.set()
        join_started = time.monotonic_ns()
        thread.join(timeout=float(join_timeout_s))
        join_latency = time.monotonic_ns() - join_started
        joined = not thread.is_alive()

        with self._boundary_lock:
            snapshot = self._broker.snapshot()
            cursor = _DepthCursor.from_snapshot(snapshot)
            self._emit(
                "worker.joined",
                EventStatus.OK if joined else EventStatus.TIMEOUT,
                cursor,
                component="worker",
                details={
                    "worker_name": self._worker_name,
                    "joined": joined,
                    "join_latency_ns": join_latency,
                    "broker_state": snapshot.state.value,
                    "worker_error_code": self.worker_error_code,
                    "event_error_code": self.event_error_code,
                },
            )
            return ExecutorShutdownReport(
                broker_state=snapshot.state,
                joined=joined,
                join_latency_ns=join_latency,
                queued_ids=snapshot.queued_ids,
                active_id=snapshot.active_id,
                result_pending_ids=snapshot.result_pending_ids,
                active_cancellation_requested=(
                    shutdown_result.active_cancellation_requested
                ),
                worker_error_code=self.worker_error_code,
                event_error_code=self.event_error_code,
            )


class SimulatedAdapter:
    """Finite, cooperative slow workload used before Jetson integration."""

    def __init__(
        self,
        service_time_s: float,
        *,
        outcome: ExecutionOutcome = ExecutionOutcome.OK,
        error_code: str | None = None,
        observe_cancellation: bool = True,
        poll_interval_s: float = 0.01,
        clock_ns: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        for value, name, upper in (
            (service_time_s, "service_time_s", 3600.0),
            (poll_interval_s, "poll_interval_s", 1.0),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value < 0
                or value > upper
            ):
                raise ValueError(f"{name} must be finite and between 0 and {upper}")
        if poll_interval_s == 0:
            raise ValueError("poll_interval_s must be greater than zero")
        if not isinstance(outcome, ExecutionOutcome):
            raise TypeError("outcome must be an ExecutionOutcome")
        if outcome is ExecutionOutcome.CANCEL_OBSERVED:
            raise ValueError(
                "cancel_observed is produced only by cooperative cancellation"
            )
        if outcome in {ExecutionOutcome.ERROR, ExecutionOutcome.TIMEOUT}:
            if error_code is None:
                raise ValueError("error and timeout simulations require error_code")
        elif error_code is not None:
            raise ValueError("successful simulation must not include error_code")
        if not isinstance(observe_cancellation, bool):
            raise TypeError("observe_cancellation must be a boolean")
        if not callable(clock_ns):
            raise TypeError("clock_ns must be callable")

        self._service_time_ns = int(float(service_time_s) * 1_000_000_000)
        self._outcome = outcome
        self._error_code = error_code
        self._observe_cancellation = observe_cancellation
        self._poll_interval_s = float(poll_interval_s)
        self._clock_ns = clock_ns
        self._noncooperative_wait = threading.Event()

    def __call__(self, claimed: ClaimedTask) -> ResultEnvelope:
        deadline = claimed.started_monotonic_ns + self._service_time_ns
        cancellation_observed = False
        while True:
            now = self._clock_ns()
            if now >= deadline:
                break
            remaining_s = (deadline - now) / 1_000_000_000
            wait_s = min(self._poll_interval_s, remaining_s)
            if self._observe_cancellation:
                if claimed.cancellation_token.wait(timeout=wait_s):
                    cancellation_observed = True
                    break
            else:
                self._noncooperative_wait.wait(timeout=wait_s)

        finished = max(claimed.started_monotonic_ns, self._clock_ns())
        task = claimed.task
        if cancellation_observed:
            outcome = ExecutionOutcome.CANCEL_OBSERVED
            error_code = None
            output_sha256 = None
            output_length = None
            cancellation = CancellationReport(
                requested=True,
                client_wait_stopped=True,
                worker_observed=True,
                backend_stop_confirmed=True,
            )
        else:
            outcome = self._outcome
            error_code = self._error_code
            cancellation = CancellationReport(
                requested=claimed.cancellation_token.is_requested()
            )
            if outcome is ExecutionOutcome.OK:
                output = f"simulated:{task.payload.sha256}".encode("ascii")
                output_sha256 = hashlib.sha256(output).hexdigest()
                output_length = len(output)
            else:
                output_sha256 = None
                output_length = None

        return ResultEnvelope(
            task_id=task.task_id,
            task_kind=task.task_kind,
            state_token=task.state_token,
            source_monotonic_ns=task.source_monotonic_ns,
            deadline_monotonic_ns=task.deadline_monotonic_ns,
            input_sha256=task.payload.sha256,
            started_monotonic_ns=claimed.started_monotonic_ns,
            finished_monotonic_ns=finished,
            execution_outcome=outcome,
            output_sha256=output_sha256,
            output_length=output_length,
            error_code=error_code,
            cancellation_report=cancellation,
        )
