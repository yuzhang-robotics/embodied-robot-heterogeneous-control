from __future__ import annotations

import threading
import time
import unittest

from jetson.phase1_runtime import (
    BoundedTaskBroker,
    ClaimedTask,
    ExecutionOutcome,
    FinalDisposition,
    LaneConfig,
    ObservableExecutor,
    OverflowPolicy,
    PayloadRef,
    ResultEnvelope,
    RuntimeEvent,
    SimulatedAdapter,
    StateToken,
    TaskEnvelope,
    TaskKind,
)


SHA_A = "a" * 64
SHA_B = "b" * 64


class RecordingSink:
    def __init__(self) -> None:
        self._events: list[RuntimeEvent] = []
        self._lock = threading.Lock()

    def emit(self, event: RuntimeEvent) -> None:
        with self._lock:
            self._events.append(event)

    def snapshot(self) -> tuple[RuntimeEvent, ...]:
        with self._lock:
            return tuple(self._events)


class FailOnEventSink(RecordingSink):
    def __init__(self, event_name: str) -> None:
        super().__init__()
        self._event_name = event_name

    def emit(self, event: RuntimeEvent) -> None:
        with self._lock:
            if event.event == self._event_name:
                raise OSError("simulated recorder failure")
            self._events.append(event)


def make_task(
    task_id: str,
    *,
    state_token: StateToken | None = None,
    lifetime_s: float = 2.0,
) -> TaskEnvelope:
    now = time.monotonic_ns()
    return TaskEnvelope(
        task_id=task_id,
        task_kind=TaskKind.SIMULATED,
        source_monotonic_ns=now,
        created_monotonic_ns=now,
        deadline_monotonic_ns=now + int(lifetime_s * 1_000_000_000),
        state_token=state_token or StateToken("test", 0),
        payload=PayloadRef(
            ref=f"private/{task_id}.bin",
            sha256=SHA_A,
            size_bytes=1,
            media_type="application/octet-stream",
        ),
    )


def make_executor(
    adapter,
    *,
    sink: RecordingSink | None = None,
    pending_capacity: int = 4,
    result_capacity: int = 4,
) -> ObservableExecutor:
    broker = BoundedTaskBroker(
        LaneConfig(
            task_kind=TaskKind.SIMULATED,
            pending_capacity=pending_capacity,
            result_capacity=result_capacity,
            terminal_record_capacity=128,
            overflow_policy=OverflowPolicy.REJECT_NEW,
        )
    )
    return ObservableExecutor(broker, adapter, event_sink=sink)


class ExecutorTests(unittest.TestCase):
    def wait_for(self, predicate, *, timeout_s: float = 1.0) -> None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if predicate():
                return
            threading.Event().wait(0.001)
        self.fail("condition did not become true before timeout")

    def stop_if_alive(self, executor: ObservableExecutor) -> None:
        if executor.is_alive:
            executor.shutdown(cancel_live=True, join_timeout_s=1.0)

    def test_valid_result_is_explicitly_consumed_and_worker_joins(self) -> None:
        sink = RecordingSink()
        executor = make_executor(SimulatedAdapter(0), sink=sink)
        executor.start()
        self.addCleanup(self.stop_if_alive, executor)

        submission = executor.submit(make_task("valid"))
        self.assertTrue(submission.admitted)
        self.wait_for(lambda: executor.snapshot().result_pending == 1)

        consumption = executor.consume_next()
        self.assertIsNotNone(consumption)
        assert consumption is not None
        self.assertTrue(consumption.consumed)
        self.assertEqual(consumption.disposition, FinalDisposition.CONSUMED)

        report = executor.shutdown(cancel_live=False, join_timeout_s=1.0)
        self.assertTrue(report.complete)
        self.assertIsNone(report.worker_error_code)
        self.assertTrue(executor.snapshot().accounting_holds)
        names = [event.event for event in sink.snapshot()]
        for expected in (
            "worker.started",
            "task.enqueued",
            "task.started",
            "task.finished",
            "result.accepted",
            "shutdown.requested",
            "worker.stopped",
            "worker.joined",
        ):
            self.assertIn(expected, names)

    def test_running_cancellation_is_observed_without_backend_overclaim(self) -> None:
        executor = make_executor(
            SimulatedAdapter(1.0, poll_interval_s=0.001),
            result_capacity=1,
        )
        executor.start()
        self.addCleanup(self.stop_if_alive, executor)
        executor.submit(make_task("cancel-running"))
        self.wait_for(lambda: executor.snapshot().running == 1)

        cancellation = executor.cancel("cancel-running", reason="superseded")
        self.assertTrue(cancellation.request_changed)
        self.wait_for(lambda: executor.snapshot().live == 0)

        snapshot = executor.snapshot()
        counts = dict(snapshot.disposition_counts)
        self.assertEqual(counts[FinalDisposition.REJECTED_CANCELLED], 1)
        report = executor.shutdown(cancel_live=False, join_timeout_s=1.0)
        self.assertTrue(report.complete)

    def test_adapter_exception_becomes_execution_error_and_worker_survives(
        self,
    ) -> None:
        def failing_adapter(claimed: ClaimedTask) -> ResultEnvelope:
            raise RuntimeError("private failure details must not escape")

        sink = RecordingSink()
        executor = make_executor(failing_adapter, sink=sink)
        executor.start()
        self.addCleanup(self.stop_if_alive, executor)

        executor.submit(make_task("failure-one"))
        self.wait_for(lambda: executor.snapshot().terminal_admitted_total == 1)
        executor.submit(make_task("failure-two"))
        self.wait_for(lambda: executor.snapshot().terminal_admitted_total == 2)

        snapshot = executor.snapshot()
        self.assertEqual(
            dict(snapshot.disposition_counts)[FinalDisposition.EXECUTION_ERROR],
            2,
        )
        self.assertTrue(executor.is_alive)
        self.assertIsNone(executor.worker_error_code)
        serialized_details = " ".join(
            str(dict(event.details)) for event in sink.snapshot()
        )
        self.assertNotIn("private failure", serialized_details)
        self.assertTrue(
            executor.shutdown(cancel_live=False, join_timeout_s=1.0).complete
        )

    def test_identity_mismatch_is_terminal_and_never_enters_mailbox(self) -> None:
        def mismatched_adapter(claimed: ClaimedTask) -> ResultEnvelope:
            task = claimed.task
            now = time.monotonic_ns()
            return ResultEnvelope(
                task_id=task.task_id,
                task_kind=task.task_kind,
                state_token=task.state_token,
                source_monotonic_ns=task.source_monotonic_ns,
                deadline_monotonic_ns=task.deadline_monotonic_ns,
                input_sha256=SHA_B,
                started_monotonic_ns=claimed.started_monotonic_ns,
                finished_monotonic_ns=max(claimed.started_monotonic_ns, now),
                execution_outcome=ExecutionOutcome.OK,
                output_sha256=SHA_A,
                output_length=1,
            )

        executor = make_executor(mismatched_adapter)
        executor.start()
        self.addCleanup(self.stop_if_alive, executor)
        executor.submit(make_task("mismatch"))
        self.wait_for(lambda: executor.snapshot().terminal_admitted_total == 1)

        snapshot = executor.snapshot()
        self.assertEqual(snapshot.result_pending, 0)
        self.assertEqual(
            dict(snapshot.disposition_counts)[FinalDisposition.REJECTED_IDENTITY],
            1,
        )
        self.assertTrue(
            executor.shutdown(cancel_live=False, join_timeout_s=1.0).complete
        )

    def test_shutdown_timeout_reports_live_noncooperative_adapter(self) -> None:
        executor = make_executor(
            SimulatedAdapter(
                0.05,
                observe_cancellation=False,
                poll_interval_s=0.001,
            )
        )
        executor.start()
        self.addCleanup(self.stop_if_alive, executor)
        executor.submit(make_task("slow-stop"))
        self.wait_for(lambda: executor.snapshot().running == 1)

        first = executor.shutdown(cancel_live=True, join_timeout_s=0)
        self.assertFalse(first.joined)
        self.assertEqual(first.active_id, "slow-stop")
        second = executor.shutdown(cancel_live=True, join_timeout_s=1.0)
        self.assertTrue(second.complete)
        self.assertIsNone(second.active_id)

    def test_concurrent_submissions_preserve_replayable_depth_sequence(self) -> None:
        sink = RecordingSink()
        executor = make_executor(
            SimulatedAdapter(0),
            sink=sink,
            pending_capacity=8,
            result_capacity=8,
        )
        executor.start()
        self.addCleanup(self.stop_if_alive, executor)

        barrier = threading.Barrier(17)
        threads = []

        def submit_one(value: int) -> None:
            barrier.wait()
            executor.submit(make_task(f"concurrent-{value}"))

        for index in range(16):
            thread = threading.Thread(
                target=submit_one,
                args=(index,),
            )
            threads.append(thread)
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=1.0)
            self.assertFalse(thread.is_alive())

        executor.shutdown(cancel_live=True, join_timeout_s=1.0)
        snapshot = executor.snapshot()
        self.assertEqual(snapshot.live, 0)
        self.assertTrue(snapshot.accounting_holds)
        for event in sink.snapshot():
            details = event.details
            if "pending_depth" in details:
                self.assertLessEqual(
                    details["pending_depth"],
                    details["pending_capacity"],
                )
                self.assertLessEqual(
                    details["result_depth"],
                    details["result_capacity"],
                )

    def test_event_sink_failure_stops_admission_and_is_not_success(self) -> None:
        sink = FailOnEventSink("task.enqueued")
        executor = make_executor(SimulatedAdapter(0), sink=sink)
        executor.start()
        self.addCleanup(self.stop_if_alive, executor)

        with self.assertRaisesRegex(RuntimeError, "event sink failed"):
            executor.submit(make_task("recorder-failure"))
        report = executor.shutdown(cancel_live=True, join_timeout_s=1.0)

        self.assertTrue(report.joined)
        self.assertEqual(report.broker_state.value, "closed")
        self.assertFalse(report.complete)
        self.assertEqual(report.event_error_code, "event_sink_oserror")
        self.assertEqual(executor.snapshot().live, 0)

    def test_state_advance_invalidates_running_result_in_observable_path(self) -> None:
        gate = threading.Event()
        base_adapter = SimulatedAdapter(0)

        def gated_adapter(claimed: ClaimedTask) -> ResultEnvelope:
            if not gate.wait(timeout=1.0):
                raise TimeoutError("test gate did not open")
            return base_adapter(claimed)

        sink = RecordingSink()
        executor = make_executor(gated_adapter, sink=sink)
        executor.start()
        self.addCleanup(self.stop_if_alive, executor)
        executor.submit(make_task("old-generation"))
        self.wait_for(lambda: executor.snapshot().running == 1)

        advanced = executor.advance_state("test", reason="new_supervisor_state")
        self.assertEqual(advanced.state_token.generation, 1)
        self.assertTrue(advanced.active_cancellation_requested)
        gate.set()
        self.wait_for(lambda: executor.snapshot().live == 0)

        counts = dict(executor.snapshot().disposition_counts)
        self.assertEqual(counts[FinalDisposition.REJECTED_STATE], 1)
        self.assertTrue(
            executor.shutdown(cancel_live=False, join_timeout_s=1.0).complete
        )
        names = [event.event for event in sink.snapshot()]
        self.assertIn("state.advanced", names)
        self.assertIn("task.finished", names)


if __name__ == "__main__":
    unittest.main()
