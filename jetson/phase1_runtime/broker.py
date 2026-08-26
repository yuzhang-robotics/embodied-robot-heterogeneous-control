"""Thread-safe bounded task ownership for the Phase 1 runtime kernel."""

from __future__ import annotations

import threading
import time
from collections import Counter, deque
from dataclasses import dataclass
from enum import Enum
from typing import Callable

from .model import (
    CancellationToken,
    ExecutionOutcome,
    FinalDisposition,
    ResultEnvelope,
    StateToken,
    TaskEnvelope,
    TaskLocation,
)
from .policies import LaneConfig, OverflowPolicy


class BrokerState(str, Enum):
    """Admission and shutdown state of one inference lane."""

    OPEN = "open"
    CLOSING = "closing"
    CLOSED = "closed"


class BrokerStateError(RuntimeError):
    """A caller attempted an impossible lifecycle operation."""


@dataclass(frozen=True, slots=True)
class TerminalTransition:
    task_id: str
    previous_location: TaskLocation
    disposition: FinalDisposition
    terminal_monotonic_ns: int


@dataclass(frozen=True, slots=True)
class SubmissionResult:
    task_id: str
    admitted: bool
    disposition: FinalDisposition | None
    terminalized: tuple[TerminalTransition, ...] = ()


@dataclass(frozen=True, slots=True)
class ClaimedTask:
    task: TaskEnvelope
    cancellation_token: CancellationToken
    started_monotonic_ns: int


@dataclass(frozen=True, slots=True)
class ClaimResult:
    claimed: ClaimedTask | None
    terminalized: tuple[TerminalTransition, ...] = ()


@dataclass(frozen=True, slots=True)
class CompletionResult:
    task_id: str
    result_pending: bool
    disposition: FinalDisposition | None
    transition: TerminalTransition | None = None


@dataclass(frozen=True, slots=True)
class ConsumptionResult:
    task: TaskEnvelope
    result: ResultEnvelope
    consumed: bool
    disposition: FinalDisposition
    transition: TerminalTransition


@dataclass(frozen=True, slots=True)
class CancellationResult:
    task_id: str
    found: bool
    request_changed: bool = False
    already_terminal: bool = False
    transition: TerminalTransition | None = None


@dataclass(frozen=True, slots=True)
class StateAdvanceResult:
    state_token: StateToken
    terminalized: tuple[TerminalTransition, ...]
    active_cancellation_requested: bool


@dataclass(frozen=True, slots=True)
class ShutdownResult:
    state: BrokerState
    terminalized: tuple[TerminalTransition, ...]
    active_cancellation_requested: bool


@dataclass(frozen=True, slots=True)
class BrokerSnapshot:
    state: BrokerState
    submission_attempts: int
    admitted_total: int
    rejected_at_ingress_total: int
    queued_ids: tuple[str, ...]
    active_id: str | None
    result_pending_ids: tuple[str, ...]
    terminal_admitted_total: int
    retained_terminal_ids: tuple[str, ...]
    max_pending_depth: int
    max_result_depth: int
    state_generations: tuple[tuple[str, int], ...]
    disposition_counts: tuple[tuple[FinalDisposition, int], ...]

    @property
    def queued(self) -> int:
        return len(self.queued_ids)

    @property
    def running(self) -> int:
        return int(self.active_id is not None)

    @property
    def result_pending(self) -> int:
        return len(self.result_pending_ids)

    @property
    def live(self) -> int:
        return self.queued + self.running + self.result_pending

    @property
    def accounting_holds(self) -> bool:
        attempts_close = self.submission_attempts == (
            self.admitted_total + self.rejected_at_ingress_total
        )
        admitted_close = self.admitted_total == (
            self.live + self.terminal_admitted_total
        )
        dispositions_close = sum(count for _, count in self.disposition_counts) == (
            self.rejected_at_ingress_total + self.terminal_admitted_total
        )
        return attempts_close and admitted_close and dispositions_close


@dataclass(slots=True)
class _TaskRecord:
    task: TaskEnvelope
    location: TaskLocation
    admitted_monotonic_ns: int
    cancellation_token: CancellationToken
    started_monotonic_ns: int | None = None
    result: ResultEnvelope | None = None
    disposition: FinalDisposition | None = None
    terminal_monotonic_ns: int | None = None


class BoundedTaskBroker:
    """Own all live locations for one bounded, single-consumer lane."""

    def __init__(
        self,
        config: LaneConfig,
        *,
        clock_ns: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        if not isinstance(config, LaneConfig):
            raise TypeError("config must be a LaneConfig")
        if not callable(clock_ns):
            raise TypeError("clock_ns must be callable")

        self.config = config
        self._clock_ns = clock_ns
        self._condition = threading.Condition(threading.RLock())
        self._state = BrokerState.OPEN
        self._pending: deque[str] = deque()
        self._active_id: str | None = None
        self._result_pending: deque[str] = deque()
        self._records: dict[str, _TaskRecord] = {}
        self._terminal_order: deque[str] = deque()
        self._state_generations: dict[str, int] = {}
        self._submission_attempts = 0
        self._admitted_total = 0
        self._rejected_at_ingress_total = 0
        self._terminal_admitted_total = 0
        self._disposition_counts: Counter[FinalDisposition] = Counter()
        self._max_pending_depth = 0
        self._max_result_depth = 0
        self._last_monotonic_ns = 0

    def _now_locked(self, supplied: int | None) -> int:
        value = self._clock_ns() if supplied is None else supplied
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("monotonic timestamp must be a non-negative integer")
        if value < self._last_monotonic_ns:
            raise ValueError("broker monotonic time moved backwards")
        self._last_monotonic_ns = value
        return value

    @staticmethod
    def _validate_reason(reason: object) -> str:
        if not isinstance(reason, str):
            raise TypeError("reason must be a string")
        if not 1 <= len(reason) <= 128:
            raise ValueError("reason must contain 1 to 128 characters")
        if reason != reason.strip() or not reason.isprintable():
            raise ValueError("reason must be printable without outer whitespace")
        return reason

    def current_state_token(self, scope_id: str) -> StateToken:
        probe = StateToken(scope_id=scope_id, generation=0)
        with self._condition:
            return StateToken(
                scope_id=probe.scope_id,
                generation=self._state_generations.get(probe.scope_id, 0),
            )

    def _state_matches(self, task: TaskEnvelope) -> bool:
        current = self._state_generations.get(task.state_token.scope_id, 0)
        return task.state_token.generation == current

    def _reject_ingress(
        self,
        task_id: str,
        disposition: FinalDisposition,
        terminalized: tuple[TerminalTransition, ...] = (),
    ) -> SubmissionResult:
        self._rejected_at_ingress_total += 1
        self._disposition_counts[disposition] += 1
        return SubmissionResult(
            task_id=task_id,
            admitted=False,
            disposition=disposition,
            terminalized=terminalized,
        )

    def _terminalize(
        self,
        record: _TaskRecord,
        disposition: FinalDisposition,
        now_ns: int,
    ) -> TerminalTransition:
        if record.location is TaskLocation.TERMINAL:
            raise BrokerStateError(f"task is already terminal: {record.task.task_id}")
        previous = record.location
        record.location = TaskLocation.TERMINAL
        record.disposition = disposition
        record.terminal_monotonic_ns = now_ns
        self._terminal_admitted_total += 1
        self._disposition_counts[disposition] += 1
        self._terminal_order.append(record.task.task_id)

        while len(self._terminal_order) > self.config.terminal_record_capacity:
            evicted_id = self._terminal_order.popleft()
            evicted = self._records.get(evicted_id)
            if evicted is not None and evicted.location is TaskLocation.TERMINAL:
                del self._records[evicted_id]

        return TerminalTransition(
            task_id=record.task.task_id,
            previous_location=previous,
            disposition=disposition,
            terminal_monotonic_ns=now_ns,
        )

    def _remove_pending(self, task_id: str) -> None:
        try:
            self._pending.remove(task_id)
        except ValueError as exc:
            raise BrokerStateError(
                f"task is not in the pending queue: {task_id}"
            ) from exc

    def _remove_result_pending(self, task_id: str) -> None:
        try:
            self._result_pending.remove(task_id)
        except ValueError as exc:
            raise BrokerStateError(
                f"task is not in the result mailbox: {task_id}"
            ) from exc

    def _prune_pending(self, now_ns: int) -> tuple[TerminalTransition, ...]:
        transitions: list[TerminalTransition] = []
        for task_id in tuple(self._pending):
            record = self._records[task_id]
            disposition: FinalDisposition | None = None
            if record.cancellation_token.is_requested():
                disposition = FinalDisposition.REJECTED_CANCELLED
            elif not self._state_matches(record.task):
                disposition = FinalDisposition.REJECTED_STATE
            elif now_ns > record.task.deadline_monotonic_ns:
                disposition = FinalDisposition.REJECTED_EXPIRED
            if disposition is None:
                continue
            self._remove_pending(task_id)
            transitions.append(self._terminalize(record, disposition, now_ns))
        return tuple(transitions)

    def submit(
        self,
        task: TaskEnvelope,
        *,
        now_ns: int | None = None,
    ) -> SubmissionResult:
        """Attempt to admit one task without blocking the producer."""

        if not isinstance(task, TaskEnvelope):
            raise TypeError("task must be a TaskEnvelope")
        if task.task_kind is not self.config.task_kind:
            raise ValueError(
                f"lane accepts {self.config.task_kind.value}, not {task.task_kind.value}"
            )

        with self._condition:
            now = self._now_locked(now_ns)
            if now < task.created_monotonic_ns:
                raise ValueError("task creation time must not be in the future")
            if task.task_id in self._records:
                raise ValueError(f"duplicate retained task_id: {task.task_id}")

            self._submission_attempts += 1
            terminalized = list(self._prune_pending(now))

            if self._state is not BrokerState.OPEN:
                return self._reject_ingress(
                    task.task_id,
                    FinalDisposition.SHUTDOWN_CANCELLED,
                    tuple(terminalized),
                )
            scope_id = task.state_token.scope_id
            if scope_id not in self._state_generations:
                if len(self._state_generations) >= self.config.state_scope_capacity:
                    return self._reject_ingress(
                        task.task_id,
                        FinalDisposition.REJECTED_BUSY,
                        tuple(terminalized),
                    )
                self._state_generations[scope_id] = 0
            if not self._state_matches(task):
                return self._reject_ingress(
                    task.task_id,
                    FinalDisposition.REJECTED_STATE,
                    tuple(terminalized),
                )
            if now > task.deadline_monotonic_ns:
                return self._reject_ingress(
                    task.task_id,
                    FinalDisposition.REJECTED_EXPIRED,
                    tuple(terminalized),
                )

            if (
                self.config.overflow_policy is OverflowPolicy.COALESCE_BY_KEY
                and task.supersession_key is not None
            ):
                replaced_id = next(
                    (
                        task_id
                        for task_id in self._pending
                        if self._records[task_id].task.supersession_key
                        == task.supersession_key
                    ),
                    None,
                )
                if replaced_id is not None:
                    self._remove_pending(replaced_id)
                    terminalized.append(
                        self._terminalize(
                            self._records[replaced_id],
                            FinalDisposition.DROPPED_OVERFLOW,
                            now,
                        )
                    )

            if len(self._pending) >= self.config.pending_capacity:
                if self.config.overflow_policy is OverflowPolicy.DROP_OLDEST:
                    replaced_id = self._pending.popleft()
                    terminalized.append(
                        self._terminalize(
                            self._records[replaced_id],
                            FinalDisposition.DROPPED_OVERFLOW,
                            now,
                        )
                    )
                else:
                    return self._reject_ingress(
                        task.task_id,
                        FinalDisposition.REJECTED_BUSY,
                        tuple(terminalized),
                    )

            self._records[task.task_id] = _TaskRecord(
                task=task,
                location=TaskLocation.QUEUED,
                admitted_monotonic_ns=now,
                cancellation_token=CancellationToken(),
            )
            self._pending.append(task.task_id)
            self._admitted_total += 1
            self._max_pending_depth = max(
                self._max_pending_depth,
                len(self._pending),
            )
            self._condition.notify_all()
            return SubmissionResult(
                task_id=task.task_id,
                admitted=True,
                disposition=None,
                terminalized=tuple(terminalized),
            )

    def claim_next(self, *, now_ns: int | None = None) -> ClaimResult:
        """Claim the next valid pending task for the single worker."""

        with self._condition:
            now = self._now_locked(now_ns)
            terminalized = self._prune_pending(now)
            if self._active_id is not None:
                return ClaimResult(claimed=None, terminalized=terminalized)
            if not self._pending:
                self._maybe_close()
                return ClaimResult(claimed=None, terminalized=terminalized)

            task_id = self._pending.popleft()
            record = self._records[task_id]
            if record.location is not TaskLocation.QUEUED:
                raise BrokerStateError(f"pending task has wrong location: {task_id}")
            record.location = TaskLocation.RUNNING
            record.started_monotonic_ns = now
            self._active_id = task_id
            self._condition.notify_all()
            return ClaimResult(
                claimed=ClaimedTask(
                    task=record.task,
                    cancellation_token=record.cancellation_token,
                    started_monotonic_ns=now,
                ),
                terminalized=terminalized,
            )

    @staticmethod
    def _identity_matches(record: _TaskRecord, result: ResultEnvelope) -> bool:
        task = record.task
        return all(
            (
                task.task_id == result.task_id,
                task.task_kind is result.task_kind,
                task.state_token == result.state_token,
                task.source_monotonic_ns == result.source_monotonic_ns,
                task.deadline_monotonic_ns == result.deadline_monotonic_ns,
                task.payload.sha256 == result.input_sha256,
                record.started_monotonic_ns == result.started_monotonic_ns,
            )
        )

    def complete(
        self,
        result: ResultEnvelope,
        *,
        now_ns: int | None = None,
    ) -> CompletionResult:
        """Finish the active task and either reject or enqueue its result."""

        if not isinstance(result, ResultEnvelope):
            raise TypeError("result must be a ResultEnvelope")
        with self._condition:
            now = self._now_locked(now_ns)
            if self._active_id is None:
                raise BrokerStateError("no active task to complete")
            if result.task_id != self._active_id:
                raise BrokerStateError(
                    f"result task_id {result.task_id!r} does not match active "
                    f"task {self._active_id!r}"
                )
            if now < result.finished_monotonic_ns:
                raise ValueError("result finish time must not be in the future")

            record = self._records[self._active_id]
            if record.location is not TaskLocation.RUNNING:
                raise BrokerStateError("active task is not in running state")

            self._active_id = None
            disposition: FinalDisposition | None = None
            if not self._identity_matches(record, result):
                disposition = FinalDisposition.REJECTED_IDENTITY
            elif not self._state_matches(record.task):
                disposition = FinalDisposition.REJECTED_STATE
            elif record.cancellation_token.is_requested():
                disposition = FinalDisposition.REJECTED_CANCELLED
            elif (
                result.finished_monotonic_ns > result.deadline_monotonic_ns
                or now > result.deadline_monotonic_ns
            ):
                disposition = FinalDisposition.REJECTED_EXPIRED
            elif result.execution_outcome is ExecutionOutcome.CANCEL_OBSERVED:
                disposition = FinalDisposition.REJECTED_CANCELLED
            elif result.execution_outcome is not ExecutionOutcome.OK:
                disposition = FinalDisposition.EXECUTION_ERROR
            elif len(self._result_pending) >= self.config.result_capacity:
                disposition = FinalDisposition.RESULT_BACKPRESSURE

            if disposition is not None:
                transition = self._terminalize(record, disposition, now)
                self._maybe_close()
                self._condition.notify_all()
                return CompletionResult(
                    task_id=record.task.task_id,
                    result_pending=False,
                    disposition=disposition,
                    transition=transition,
                )

            record.result = result
            record.location = TaskLocation.RESULT_PENDING
            self._result_pending.append(record.task.task_id)
            self._max_result_depth = max(
                self._max_result_depth,
                len(self._result_pending),
            )
            self._condition.notify_all()
            return CompletionResult(
                task_id=record.task.task_id,
                result_pending=True,
                disposition=None,
            )

    def consume_next(
        self,
        *,
        now_ns: int | None = None,
    ) -> ConsumptionResult | None:
        """Revalidate and consume or reject the oldest completed result."""

        with self._condition:
            now = self._now_locked(now_ns)
            if not self._result_pending:
                self._maybe_close()
                return None

            task_id = self._result_pending.popleft()
            record = self._records[task_id]
            if record.location is not TaskLocation.RESULT_PENDING:
                raise BrokerStateError(f"result task has wrong location: {task_id}")
            if record.result is None:
                raise BrokerStateError(f"result task has no envelope: {task_id}")

            disposition = FinalDisposition.CONSUMED
            if not self._state_matches(record.task):
                disposition = FinalDisposition.REJECTED_STATE
            elif record.cancellation_token.is_requested():
                disposition = FinalDisposition.REJECTED_CANCELLED
            elif now > record.task.deadline_monotonic_ns:
                disposition = FinalDisposition.REJECTED_EXPIRED
            elif not self._identity_matches(record, record.result):
                disposition = FinalDisposition.REJECTED_IDENTITY

            transition = self._terminalize(record, disposition, now)
            self._maybe_close()
            self._condition.notify_all()
            return ConsumptionResult(
                task=record.task,
                result=record.result,
                consumed=disposition is FinalDisposition.CONSUMED,
                disposition=disposition,
                transition=transition,
            )

    def cancel(
        self,
        task_id: str,
        *,
        reason: str,
        now_ns: int | None = None,
    ) -> CancellationResult:
        """Cancel queued work, signal running work, or reject a pending result."""

        checked_reason = self._validate_reason(reason)
        with self._condition:
            now = self._now_locked(now_ns)
            record = self._records.get(task_id)
            if record is None:
                return CancellationResult(task_id=task_id, found=False)
            if record.location is TaskLocation.TERMINAL:
                return CancellationResult(
                    task_id=task_id,
                    found=True,
                    already_terminal=True,
                )

            changed = record.cancellation_token.request(checked_reason, now)
            transition: TerminalTransition | None = None
            if record.location is TaskLocation.QUEUED:
                self._remove_pending(task_id)
                transition = self._terminalize(
                    record,
                    FinalDisposition.CANCELLED_QUEUED,
                    now,
                )
            elif record.location is TaskLocation.RESULT_PENDING:
                self._remove_result_pending(task_id)
                transition = self._terminalize(
                    record,
                    FinalDisposition.REJECTED_CANCELLED,
                    now,
                )
            elif record.location is not TaskLocation.RUNNING:
                raise BrokerStateError(f"unsupported task location: {record.location}")

            self._maybe_close()
            self._condition.notify_all()
            return CancellationResult(
                task_id=task_id,
                found=True,
                request_changed=changed,
                transition=transition,
            )

    def advance_state(
        self,
        scope_id: str,
        *,
        reason: str,
        now_ns: int | None = None,
    ) -> StateAdvanceResult:
        """Atomically invalidate old tasks in one independent state scope."""

        checked_reason = self._validate_reason(reason)
        checked_scope = StateToken(scope_id=scope_id, generation=0).scope_id
        with self._condition:
            now = self._now_locked(now_ns)
            if checked_scope not in self._state_generations:
                if len(self._state_generations) >= self.config.state_scope_capacity:
                    raise BrokerStateError("state scope capacity exceeded")
                self._state_generations[checked_scope] = 0
            current = self._state_generations[checked_scope]
            next_token = StateToken(
                scope_id=checked_scope,
                generation=current + 1,
            )
            self._state_generations[checked_scope] = next_token.generation
            transitions: list[TerminalTransition] = []
            active_requested = False

            for task_id in tuple(self._pending):
                record = self._records[task_id]
                if record.task.state_token.scope_id != checked_scope:
                    continue
                self._remove_pending(task_id)
                record.cancellation_token.request(checked_reason, now)
                transitions.append(
                    self._terminalize(
                        record,
                        FinalDisposition.REJECTED_STATE,
                        now,
                    )
                )

            if self._active_id is not None:
                active = self._records[self._active_id]
                if active.task.state_token.scope_id == checked_scope:
                    active_requested = active.cancellation_token.request(
                        checked_reason, now
                    )

            for task_id in tuple(self._result_pending):
                record = self._records[task_id]
                if record.task.state_token.scope_id != checked_scope:
                    continue
                self._remove_result_pending(task_id)
                record.cancellation_token.request(checked_reason, now)
                transitions.append(
                    self._terminalize(
                        record,
                        FinalDisposition.REJECTED_STATE,
                        now,
                    )
                )

            self._maybe_close()
            self._condition.notify_all()
            return StateAdvanceResult(
                state_token=next_token,
                terminalized=tuple(transitions),
                active_cancellation_requested=active_requested,
            )

    def begin_shutdown(
        self,
        *,
        cancel_live: bool,
        now_ns: int | None = None,
    ) -> ShutdownResult:
        """Stop admission and optionally cancel every live lifecycle location."""

        with self._condition:
            now = self._now_locked(now_ns)
            if self._state is BrokerState.CLOSED:
                return ShutdownResult(
                    state=self._state,
                    terminalized=(),
                    active_cancellation_requested=False,
                )
            self._state = BrokerState.CLOSING
            transitions: list[TerminalTransition] = []
            active_requested = False

            if cancel_live:
                for task_id in tuple(self._pending):
                    self._remove_pending(task_id)
                    record = self._records[task_id]
                    record.cancellation_token.request("shutdown", now)
                    transitions.append(
                        self._terminalize(
                            record,
                            FinalDisposition.SHUTDOWN_CANCELLED,
                            now,
                        )
                    )

                if self._active_id is not None:
                    active_requested = self._records[
                        self._active_id
                    ].cancellation_token.request("shutdown", now)

                for task_id in tuple(self._result_pending):
                    self._remove_result_pending(task_id)
                    record = self._records[task_id]
                    record.cancellation_token.request("shutdown", now)
                    transitions.append(
                        self._terminalize(
                            record,
                            FinalDisposition.SHUTDOWN_CANCELLED,
                            now,
                        )
                    )

            self._maybe_close()
            self._condition.notify_all()
            return ShutdownResult(
                state=self._state,
                terminalized=tuple(transitions),
                active_cancellation_requested=active_requested,
            )

    def _maybe_close(self) -> None:
        if (
            self._state is BrokerState.CLOSING
            and not self._pending
            and self._active_id is None
            and not self._result_pending
        ):
            self._state = BrokerState.CLOSED

    def snapshot(self) -> BrokerSnapshot:
        """Return one lock-consistent accounting snapshot."""

        with self._condition:
            snapshot = BrokerSnapshot(
                state=self._state,
                submission_attempts=self._submission_attempts,
                admitted_total=self._admitted_total,
                rejected_at_ingress_total=self._rejected_at_ingress_total,
                queued_ids=tuple(self._pending),
                active_id=self._active_id,
                result_pending_ids=tuple(self._result_pending),
                terminal_admitted_total=self._terminal_admitted_total,
                retained_terminal_ids=tuple(self._terminal_order),
                max_pending_depth=self._max_pending_depth,
                max_result_depth=self._max_result_depth,
                state_generations=tuple(sorted(self._state_generations.items())),
                disposition_counts=tuple(
                    sorted(
                        self._disposition_counts.items(),
                        key=lambda item: item[0].value,
                    )
                ),
            )
            if not snapshot.accounting_holds:
                raise BrokerStateError("broker accounting invariant failed")
            if snapshot.queued > self.config.pending_capacity:
                raise BrokerStateError("pending capacity invariant failed")
            if snapshot.result_pending > self.config.result_capacity:
                raise BrokerStateError("result capacity invariant failed")
            if (
                len(snapshot.retained_terminal_ids)
                > self.config.terminal_record_capacity
            ):
                raise BrokerStateError("terminal retention invariant failed")
            if len(snapshot.state_generations) > self.config.state_scope_capacity:
                raise BrokerStateError("state scope capacity invariant failed")
            return snapshot
