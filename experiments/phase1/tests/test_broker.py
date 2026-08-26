"""Deterministic lifecycle tests for the bounded Phase 1 broker."""

from __future__ import annotations

import threading
import unittest

from jetson.phase1_runtime.broker import BrokerState, BrokerStateError
from jetson.phase1_runtime.model import (
    ExecutionOutcome,
    FinalDisposition,
    PayloadRef,
    ResultEnvelope,
    StateToken,
    TaskEnvelope,
    TaskKind,
)
from jetson.phase1_runtime import BoundedTaskBroker, LaneConfig, OverflowPolicy


INPUT_SHA256 = "a" * 64
OTHER_SHA256 = "c" * 64
OUTPUT_SHA256 = "b" * 64


class ManualClock:
    def __init__(self, now_ns: int = 100) -> None:
        self.now_ns = now_ns

    def __call__(self) -> int:
        return self.now_ns

    def advance(self, delta_ns: int) -> int:
        self.now_ns += delta_ns
        return self.now_ns


def make_task(
    task_id: str,
    *,
    generation: int = 0,
    scope_id: str = "interaction-1",
    created_ns: int = 100,
    deadline_ns: int = 1000,
    supersession_key: str | None = None,
) -> TaskEnvelope:
    return TaskEnvelope(
        task_id=task_id,
        task_kind=TaskKind.SIMULATED,
        source_monotonic_ns=created_ns - 10,
        created_monotonic_ns=created_ns,
        deadline_monotonic_ns=deadline_ns,
        state_token=StateToken(scope_id, generation),
        payload=PayloadRef(
            ref=f"private/{task_id}.bin",
            sha256=INPUT_SHA256,
            size_bytes=16,
            media_type="application/octet-stream",
        ),
        supersession_key=supersession_key,
    )


def make_result(
    task: TaskEnvelope,
    *,
    started_ns: int = 110,
    finished_ns: int = 120,
    input_sha256: str = INPUT_SHA256,
    outcome: ExecutionOutcome = ExecutionOutcome.OK,
) -> ResultEnvelope:
    error_code = None
    output_sha256: str | None = OUTPUT_SHA256
    output_length: int | None = 4
    if outcome in {ExecutionOutcome.ERROR, ExecutionOutcome.TIMEOUT}:
        error_code = "simulated_failure"
        output_sha256 = None
        output_length = None
    return ResultEnvelope(
        task_id=task.task_id,
        task_kind=task.task_kind,
        state_token=task.state_token,
        source_monotonic_ns=task.source_monotonic_ns,
        deadline_monotonic_ns=task.deadline_monotonic_ns,
        input_sha256=input_sha256,
        started_monotonic_ns=started_ns,
        finished_monotonic_ns=finished_ns,
        execution_outcome=outcome,
        output_sha256=output_sha256,
        output_length=output_length,
        error_code=error_code,
    )


def make_broker(
    clock: ManualClock,
    *,
    pending_capacity: int = 2,
    result_capacity: int = 1,
    terminal_capacity: int = 16,
    state_scope_capacity: int = 8,
    policy: OverflowPolicy = OverflowPolicy.REJECT_NEW,
) -> BoundedTaskBroker:
    return BoundedTaskBroker(
        LaneConfig(
            task_kind=TaskKind.SIMULATED,
            pending_capacity=pending_capacity,
            result_capacity=result_capacity,
            terminal_record_capacity=terminal_capacity,
            state_scope_capacity=state_scope_capacity,
            overflow_policy=policy,
        ),
        clock_ns=clock,
    )


def run_to_result(
    broker: BoundedTaskBroker,
    task: TaskEnvelope,
    clock: ManualClock,
) -> None:
    self_submit = broker.submit(task)
    if not self_submit.admitted:
        raise AssertionError(self_submit)
    claim = broker.claim_next()
    if claim.claimed is None:
        raise AssertionError("task was not claimed")
    clock.advance(1)
    completion = broker.complete(
        make_result(
            task,
            started_ns=claim.claimed.started_monotonic_ns,
            finished_ns=clock.now_ns,
        )
    )
    if not completion.result_pending:
        raise AssertionError(completion)


class AdmissionPolicyTests(unittest.TestCase):
    def test_lane_capacity_has_a_finite_configuration_limit(self) -> None:
        with self.assertRaisesRegex(ValueError, "between 1"):
            LaneConfig(
                task_kind=TaskKind.SIMULATED,
                pending_capacity=100_001,
            )

    def test_reject_new_keeps_queue_bounded_and_accounting_closed(self) -> None:
        clock = ManualClock()
        broker = make_broker(clock, pending_capacity=1)

        first = broker.submit(make_task("task-1"))
        second = broker.submit(make_task("task-2"))

        self.assertTrue(first.admitted)
        self.assertFalse(second.admitted)
        self.assertEqual(second.disposition, FinalDisposition.REJECTED_BUSY)
        snapshot = broker.snapshot()
        self.assertEqual(snapshot.queued_ids, ("task-1",))
        self.assertEqual(snapshot.submission_attempts, 2)
        self.assertEqual(snapshot.rejected_at_ingress_total, 1)
        self.assertTrue(snapshot.accounting_holds)

    def test_drop_oldest_terminalizes_replaced_task(self) -> None:
        clock = ManualClock()
        broker = make_broker(
            clock,
            pending_capacity=1,
            policy=OverflowPolicy.DROP_OLDEST,
        )
        broker.submit(make_task("task-1"))
        result = broker.submit(make_task("task-2"))

        self.assertTrue(result.admitted)
        self.assertEqual(len(result.terminalized), 1)
        self.assertEqual(
            result.terminalized[0].disposition,
            FinalDisposition.DROPPED_OVERFLOW,
        )
        self.assertEqual(broker.snapshot().queued_ids, ("task-2",))

    def test_coalesce_replaces_only_a_matching_pending_key(self) -> None:
        clock = ManualClock()
        broker = make_broker(
            clock,
            pending_capacity=2,
            policy=OverflowPolicy.COALESCE_BY_KEY,
        )
        broker.submit(make_task("frame-1", supersession_key="camera-front"))
        replacement = broker.submit(
            make_task("frame-2", supersession_key="camera-front")
        )
        broker.submit(make_task("frame-side", supersession_key="camera-side"))
        rejected = broker.submit(
            make_task("frame-other", supersession_key="camera-other")
        )

        self.assertEqual(len(replacement.terminalized), 1)
        self.assertEqual(
            broker.snapshot().queued_ids,
            ("frame-2", "frame-side"),
        )
        self.assertFalse(rejected.admitted)
        self.assertEqual(rejected.disposition, FinalDisposition.REJECTED_BUSY)

    def test_duplicate_retained_task_id_is_rejected_as_contract_error(self) -> None:
        clock = ManualClock()
        broker = make_broker(clock)
        task = make_task("duplicate")
        broker.submit(task)
        with self.assertRaisesRegex(ValueError, "duplicate retained"):
            broker.submit(task)

    def test_expired_pending_task_is_pruned_before_claim(self) -> None:
        clock = ManualClock()
        broker = make_broker(clock)
        broker.submit(make_task("expired", deadline_ns=101))
        clock.advance(2)

        claim = broker.claim_next()

        self.assertIsNone(claim.claimed)
        self.assertEqual(len(claim.terminalized), 1)
        self.assertEqual(
            claim.terminalized[0].disposition,
            FinalDisposition.REJECTED_EXPIRED,
        )
        self.assertTrue(broker.snapshot().accounting_holds)

    def test_task_creation_time_cannot_be_in_the_future(self) -> None:
        clock = ManualClock(now_ns=100)
        broker = make_broker(clock)

        with self.assertRaisesRegex(ValueError, "creation time"):
            broker.submit(
                make_task(
                    "future",
                    created_ns=101,
                    deadline_ns=1000,
                )
            )

        snapshot = broker.snapshot()
        self.assertEqual(snapshot.submission_attempts, 0)
        self.assertTrue(snapshot.accounting_holds)

    def test_state_scope_table_is_bounded_without_forgetting_generations(self) -> None:
        clock = ManualClock()
        broker = make_broker(
            clock,
            pending_capacity=2,
            state_scope_capacity=1,
        )
        first = broker.submit(make_task("first", scope_id="scope-1"))
        second = broker.submit(make_task("second", scope_id="scope-2"))

        self.assertTrue(first.admitted)
        self.assertFalse(second.admitted)
        self.assertEqual(second.disposition, FinalDisposition.REJECTED_BUSY)
        self.assertEqual(broker.snapshot().state_generations, (("scope-1", 0),))
        with self.assertRaisesRegex(BrokerStateError, "scope capacity"):
            broker.advance_state("scope-2", reason="new_state")


class LifecycleTests(unittest.TestCase):
    def test_valid_result_is_revalidated_and_consumed(self) -> None:
        clock = ManualClock()
        broker = make_broker(clock)
        task = make_task("task-1")
        run_to_result(broker, task, clock)

        consumed = broker.consume_next()

        self.assertIsNotNone(consumed)
        assert consumed is not None
        self.assertTrue(consumed.consumed)
        self.assertEqual(consumed.disposition, FinalDisposition.CONSUMED)
        snapshot = broker.snapshot()
        self.assertEqual(snapshot.live, 0)
        self.assertEqual(snapshot.terminal_admitted_total, 1)
        self.assertTrue(snapshot.accounting_holds)

    def test_result_identity_mismatch_is_never_queued_for_consumption(self) -> None:
        clock = ManualClock()
        broker = make_broker(clock)
        task = make_task("task-1")
        broker.submit(task)
        claim = broker.claim_next().claimed
        assert claim is not None

        completion = broker.complete(
            make_result(
                task,
                started_ns=claim.started_monotonic_ns,
                finished_ns=clock.advance(1),
                input_sha256=OTHER_SHA256,
            )
        )

        self.assertFalse(completion.result_pending)
        self.assertEqual(
            completion.disposition,
            FinalDisposition.REJECTED_IDENTITY,
        )
        self.assertIsNone(broker.consume_next())

    def test_result_worker_start_must_match_the_claim(self) -> None:
        clock = ManualClock()
        broker = make_broker(clock)
        task = make_task("task-1")
        broker.submit(task)
        claim = broker.claim_next().claimed
        assert claim is not None
        clock.advance(2)

        completion = broker.complete(
            make_result(
                task,
                started_ns=claim.started_monotonic_ns + 1,
                finished_ns=clock.now_ns,
            )
        )

        self.assertEqual(
            completion.disposition,
            FinalDisposition.REJECTED_IDENTITY,
        )

    def test_result_can_expire_after_completion_before_consumption(self) -> None:
        clock = ManualClock()
        broker = make_broker(clock)
        task = make_task("task-1", deadline_ns=130)
        run_to_result(broker, task, clock)
        clock.now_ns = 131

        consumed = broker.consume_next()

        assert consumed is not None
        self.assertFalse(consumed.consumed)
        self.assertEqual(
            consumed.disposition,
            FinalDisposition.REJECTED_EXPIRED,
        )

    def test_result_mailbox_backpressure_is_a_terminal_disposition(self) -> None:
        clock = ManualClock()
        broker = make_broker(clock, result_capacity=1)
        first = make_task("task-1")
        second = make_task("task-2")
        run_to_result(broker, first, clock)
        broker.submit(second)
        claim = broker.claim_next().claimed
        assert claim is not None

        completion = broker.complete(
            make_result(
                second,
                started_ns=claim.started_monotonic_ns,
                finished_ns=clock.advance(1),
            )
        )

        self.assertEqual(
            completion.disposition,
            FinalDisposition.RESULT_BACKPRESSURE,
        )
        self.assertEqual(broker.snapshot().result_pending_ids, ("task-1",))

    def test_execution_error_is_separate_from_result_freshness(self) -> None:
        clock = ManualClock()
        broker = make_broker(clock)
        task = make_task("task-1")
        broker.submit(task)
        claim = broker.claim_next().claimed
        assert claim is not None

        completion = broker.complete(
            make_result(
                task,
                started_ns=claim.started_monotonic_ns,
                finished_ns=clock.advance(1),
                outcome=ExecutionOutcome.ERROR,
            )
        )

        self.assertEqual(
            completion.disposition,
            FinalDisposition.EXECUTION_ERROR,
        )
        self.assertIsNone(broker.consume_next())


class CancellationAndStateTests(unittest.TestCase):
    def test_cancel_and_state_change_validate_reason_without_live_tasks(self) -> None:
        clock = ManualClock()
        broker = make_broker(clock)

        with self.assertRaisesRegex(ValueError, "1 to 128"):
            broker.cancel("missing", reason="")
        with self.assertRaisesRegex(ValueError, "printable"):
            broker.advance_state("interaction-1", reason=" invalid ")

    def test_cancel_handles_queued_running_and_result_pending_locations(self) -> None:
        clock = ManualClock()
        broker = make_broker(clock, pending_capacity=3, result_capacity=2)

        queued = make_task("queued")
        broker.submit(queued)
        queued_cancel = broker.cancel("queued", reason="user_cancel")
        assert queued_cancel.transition is not None
        self.assertEqual(
            queued_cancel.transition.disposition,
            FinalDisposition.CANCELLED_QUEUED,
        )

        running = make_task("running")
        broker.submit(running)
        claim = broker.claim_next().claimed
        assert claim is not None
        running_cancel = broker.cancel("running", reason="user_cancel")
        self.assertTrue(running_cancel.request_changed)
        self.assertTrue(claim.cancellation_token.is_requested())
        completion = broker.complete(
            make_result(
                running,
                started_ns=claim.started_monotonic_ns,
                finished_ns=clock.advance(1),
            )
        )
        self.assertEqual(
            completion.disposition,
            FinalDisposition.REJECTED_CANCELLED,
        )

        pending_result = make_task("result")
        run_to_result(broker, pending_result, clock)
        result_cancel = broker.cancel("result", reason="user_cancel")
        assert result_cancel.transition is not None
        self.assertEqual(
            result_cancel.transition.disposition,
            FinalDisposition.REJECTED_CANCELLED,
        )
        self.assertEqual(broker.snapshot().live, 0)

    def test_state_advance_invalidates_only_the_matching_scope(self) -> None:
        clock = ManualClock()
        broker = make_broker(clock, pending_capacity=4, result_capacity=2)

        result_task = make_task("old-result")
        run_to_result(broker, result_task, clock)
        active_task = make_task("old-active")
        queued_task = make_task("old-queued")
        other_scope = make_task("other", scope_id="interaction-2")
        broker.submit(active_task)
        broker.submit(queued_task)
        broker.submit(other_scope)
        claim = broker.claim_next().claimed
        assert claim is not None
        self.assertEqual(claim.task.task_id, "old-active")

        advanced = broker.advance_state(
            "interaction-1",
            reason="new_interaction_state",
        )

        self.assertEqual(advanced.state_token.generation, 1)
        self.assertTrue(advanced.active_cancellation_requested)
        terminal_ids = {item.task_id for item in advanced.terminalized}
        self.assertEqual(terminal_ids, {"old-result", "old-queued"})
        self.assertEqual(broker.snapshot().queued_ids, ("other",))

        completion = broker.complete(
            make_result(
                active_task,
                started_ns=claim.started_monotonic_ns,
                finished_ns=clock.advance(1),
            )
        )
        self.assertEqual(
            completion.disposition,
            FinalDisposition.REJECTED_STATE,
        )

    def test_old_generation_is_rejected_at_ingress(self) -> None:
        clock = ManualClock()
        broker = make_broker(clock)
        broker.advance_state("interaction-1", reason="reset")

        rejected = broker.submit(make_task("old", generation=0))
        accepted = broker.submit(make_task("current", generation=1))

        self.assertFalse(rejected.admitted)
        self.assertEqual(rejected.disposition, FinalDisposition.REJECTED_STATE)
        self.assertTrue(accepted.admitted)


class BoundsAndShutdownTests(unittest.TestCase):
    def test_broker_rejects_monotonic_clock_regression(self) -> None:
        clock = ManualClock()
        broker = make_broker(clock)
        broker.submit(make_task("active"))
        clock.advance(10)
        claim = broker.claim_next().claimed
        assert claim is not None

        clock.now_ns = 109
        with self.assertRaisesRegex(ValueError, "moved backwards"):
            broker.cancel("active", reason="user_cancel")
        self.assertFalse(claim.cancellation_token.is_requested())

        clock.now_ns = 111
        cancelled = broker.cancel("active", reason="user_cancel")
        self.assertTrue(cancelled.request_changed)

    def test_terminal_record_retention_is_bounded_without_losing_accounting(
        self,
    ) -> None:
        clock = ManualClock()
        broker = make_broker(clock, terminal_capacity=2)

        for index in range(5):
            task = make_task(f"task-{index}")
            run_to_result(broker, task, clock)
            consumed = broker.consume_next()
            assert consumed is not None and consumed.consumed

        snapshot = broker.snapshot()
        self.assertEqual(snapshot.terminal_admitted_total, 5)
        self.assertEqual(len(snapshot.retained_terminal_ids), 2)
        self.assertEqual(snapshot.retained_terminal_ids, ("task-3", "task-4"))
        self.assertTrue(snapshot.accounting_holds)

    def test_cancel_shutdown_closes_after_active_task_finishes(self) -> None:
        clock = ManualClock()
        broker = make_broker(clock, pending_capacity=2)
        active = make_task("active")
        queued = make_task("queued")
        broker.submit(active)
        broker.submit(queued)
        claim = broker.claim_next().claimed
        assert claim is not None

        shutdown = broker.begin_shutdown(cancel_live=True)

        self.assertEqual(shutdown.state, BrokerState.CLOSING)
        self.assertTrue(shutdown.active_cancellation_requested)
        self.assertEqual(
            shutdown.terminalized[0].disposition,
            FinalDisposition.SHUTDOWN_CANCELLED,
        )
        with self.assertRaisesRegex(BrokerStateError, "does not match"):
            broker.complete(make_result(queued))

        completion = broker.complete(
            make_result(
                active,
                started_ns=claim.started_monotonic_ns,
                finished_ns=clock.advance(1),
            )
        )
        self.assertEqual(
            completion.disposition,
            FinalDisposition.REJECTED_CANCELLED,
        )
        self.assertEqual(broker.snapshot().state, BrokerState.CLOSED)

    def test_submissions_after_shutdown_are_rejected(self) -> None:
        clock = ManualClock()
        broker = make_broker(clock)
        broker.begin_shutdown(cancel_live=False)

        result = broker.submit(make_task("late"))

        self.assertFalse(result.admitted)
        self.assertEqual(
            result.disposition,
            FinalDisposition.SHUTDOWN_CANCELLED,
        )
        self.assertEqual(broker.snapshot().state, BrokerState.CLOSED)

    def test_concurrent_producers_preserve_capacity_and_accounting(self) -> None:
        clock = ManualClock()
        broker = make_broker(
            clock,
            pending_capacity=200,
            terminal_capacity=256,
        )
        errors: list[BaseException] = []

        def producer(worker_index: int) -> None:
            try:
                for item_index in range(50):
                    result = broker.submit(
                        make_task(f"task-{worker_index}-{item_index}")
                    )
                    if not result.admitted:
                        raise AssertionError(result)
            except BaseException as exc:  # reported after all threads join
                errors.append(exc)

        threads = [
            threading.Thread(target=producer, args=(index,)) for index in range(4)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2)

        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        snapshot = broker.snapshot()
        self.assertEqual(snapshot.queued, 200)
        self.assertEqual(snapshot.max_pending_depth, 200)
        self.assertTrue(snapshot.accounting_holds)


if __name__ == "__main__":
    unittest.main()
