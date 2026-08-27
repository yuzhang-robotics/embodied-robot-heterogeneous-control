from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from experiments.phase1.replay_lifecycle import (
    ReplayError,
    load_events,
    main,
    replay_events,
    replay_file,
)
from experiments.phase1.telemetry import EventRecorder
from jetson.phase1_runtime import (
    BoundedTaskBroker,
    LaneConfig,
    ObservableExecutor,
    OverflowPolicy,
    PayloadRef,
    PeriodicProbe,
    SimulatedAdapter,
    StateToken,
    TaskEnvelope,
    TaskKind,
)


RUN_ID = "20260827T010000Z_phase1_semantic_simulated_001"
PRIVATE_REF = "C:/private/research/input.bin"


def make_task(task_id: str) -> TaskEnvelope:
    now = time.monotonic_ns()
    return TaskEnvelope(
        task_id=task_id,
        task_kind=TaskKind.SIMULATED,
        source_monotonic_ns=now,
        created_monotonic_ns=now,
        deadline_monotonic_ns=now + 2_000_000_000,
        state_token=StateToken("trace", 0),
        payload=PayloadRef(
            ref=PRIVATE_REF,
            sha256="c" * 64,
            size_bytes=4,
            media_type="application/octet-stream",
        ),
    )


class TraceTests(unittest.TestCase):
    def wait_for(self, predicate, *, timeout_s: float = 1.0) -> None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if predicate():
                return
            threading.Event().wait(0.001)
        self.fail("condition did not become true before timeout")

    def create_complete_trace(self, root: Path) -> Path:
        recorder = EventRecorder(root, RUN_ID)
        broker = BoundedTaskBroker(
            LaneConfig(
                task_kind=TaskKind.SIMULATED,
                pending_capacity=2,
                result_capacity=1,
            )
        )
        executor = ObservableExecutor(
            broker,
            SimulatedAdapter(0),
            event_sink=recorder,
        )
        executor.start()
        executor.submit(make_task("trace-task"))
        self.wait_for(lambda: executor.snapshot().result_pending == 1)
        result = executor.consume_next()
        self.assertIsNotNone(result)
        self.assertTrue(
            executor.shutdown(cancel_live=False, join_timeout_s=1.0).complete
        )
        recorder.close()
        return recorder.path

    def test_jsonl_trace_replays_without_runtime_internals(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = self.create_complete_trace(Path(temp_dir))
            summary = replay_file(path)
            text = path.read_text(encoding="utf-8")

        self.assertEqual(summary.submission_attempts, 1)
        self.assertEqual(summary.admitted_total, 1)
        self.assertEqual(summary.terminal_admitted_total, 1)
        self.assertEqual(summary.accepted_result_count, 1)
        self.assertEqual(summary.stale_consumed_count, 0)
        self.assertTrue(summary.worker_joined)
        self.assertEqual(summary.final_broker_state, "closed")
        self.assertNotIn(PRIVATE_REF, text)
        self.assertTrue(text.endswith("\n"))

    def test_replay_rejects_capacity_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = self.create_complete_trace(Path(temp_dir))
            events = load_events(path)
        enqueued = next(event for event in events if event["event"] == "task.enqueued")
        enqueued["details"]["pending_depth"] = 99
        with self.assertRaisesRegex(ReplayError, "depths"):
            replay_events(events)

    def test_replay_rejects_a_consumed_result_after_its_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = self.create_complete_trace(Path(temp_dir))
            events = load_events(path)
        accepted = next(
            event for event in events if event["event"] == "result.accepted"
        )
        late_transition = accepted["deadline_monotonic_ns"] + 1
        accepted["details"]["transition_monotonic_ns"] = late_transition
        accepted_index = events.index(accepted)
        for event in events[accepted_index:]:
            event["monotonic_ns"] = max(event["monotonic_ns"], late_transition)
        with self.assertRaisesRegex(ReplayError, "stale or cancelled"):
            replay_events(events)

    def test_replay_rejects_duplicate_terminal_disposition(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = self.create_complete_trace(Path(temp_dir))
            events = load_events(path)
        accepted_index = next(
            index
            for index, event in enumerate(events)
            if event["event"] == "result.accepted"
        )
        duplicate = json.loads(json.dumps(events[accepted_index]))
        duplicate["event"] = "task.terminal"
        duplicate["details"]["previous_location"] = "terminal"
        duplicate["details"]["next_location"] = "terminal"
        duplicate["details"]["disposition"] = "rejected_state"
        events.insert(accepted_index + 1, duplicate)
        for index, event in enumerate(events):
            event["seq"] = index
        with self.assertRaisesRegex(ReplayError, "duplicate final"):
            replay_events(events)

    def test_recorder_refuses_invalid_id_and_existing_trace(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with self.assertRaises(ValueError):
                EventRecorder(root, "invalid")
            recorder = EventRecorder(root, RUN_ID)
            recorder.close()
            with self.assertRaises(FileExistsError):
                EventRecorder(root, RUN_ID)

    def test_replay_cli_prints_machine_readable_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = self.create_complete_trace(Path(temp_dir))
            output = StringIO()
            with redirect_stdout(output):
                return_code = main([str(path)])
            summary = json.loads(output.getvalue())

        self.assertEqual(return_code, 0)
        self.assertEqual(summary["accepted_result_count"], 1)
        self.assertEqual(summary["final_broker_state"], "closed")

    def test_probe_and_executor_share_one_ordered_trace(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            recorder = EventRecorder(Path(temp_dir), RUN_ID)
            broker = BoundedTaskBroker(
                LaneConfig(
                    task_kind=TaskKind.SIMULATED,
                    pending_capacity=2,
                    result_capacity=1,
                )
            )
            executor = ObservableExecutor(
                broker,
                SimulatedAdapter(0.01, poll_interval_s=0.001),
                event_sink=recorder,
            )
            probe = PeriodicProbe(period_ns=1_000_000, event_sink=recorder)
            executor.start()
            probe.start()
            executor.submit(make_task("interleaved"))
            self.wait_for(lambda: executor.snapshot().result_pending == 1)
            self.assertIsNotNone(executor.consume_next())
            probe_report = probe.stop(join_timeout_s=1.0)
            shutdown = executor.shutdown(
                cancel_live=False,
                join_timeout_s=1.0,
            )
            recorder.close()
            summary = replay_file(recorder.path)
            events = load_events(recorder.path)

        self.assertTrue(probe_report.joined)
        self.assertTrue(shutdown.complete)
        self.assertGreater(summary.probe_tick_count, 0)
        self.assertEqual(summary.accepted_result_count, 1)
        missing_join = [
            json.loads(json.dumps(event))
            for event in events
            if event["event"] != "probe.joined"
        ]
        for index, event in enumerate(missing_join):
            event["seq"] = index
        with self.assertRaisesRegex(ReplayError, "probe join"):
            replay_events(missing_join)
        tick = next(event for event in events if event["event"] == "probe.tick")
        tick["details"]["execution_ns"] += 1
        with self.assertRaisesRegex(ReplayError, "execution duration"):
            replay_events(events)

    def test_overflow_and_queued_cancellation_replay_to_one_terminal_each(self) -> None:
        gate = threading.Event()
        base_adapter = SimulatedAdapter(0)

        def gated_adapter(claimed):
            if not gate.wait(timeout=1.0):
                raise TimeoutError("test gate did not open")
            return base_adapter(claimed)

        with tempfile.TemporaryDirectory() as temp_dir:
            recorder = EventRecorder(Path(temp_dir), RUN_ID)
            broker = BoundedTaskBroker(
                LaneConfig(
                    task_kind=TaskKind.SIMULATED,
                    pending_capacity=1,
                    result_capacity=1,
                    overflow_policy=OverflowPolicy.DROP_OLDEST,
                )
            )
            executor = ObservableExecutor(
                broker,
                gated_adapter,
                event_sink=recorder,
            )
            executor.start()
            executor.submit(make_task("active"))
            self.wait_for(lambda: executor.snapshot().running == 1)
            executor.submit(make_task("replaced"))
            replacement = executor.submit(make_task("queued-cancel"))
            self.assertEqual(
                replacement.terminalized[0].task_id,
                "replaced",
            )
            executor.cancel("queued-cancel", reason="scenario_cancel")
            gate.set()
            self.wait_for(lambda: executor.snapshot().result_pending == 1)
            self.assertIsNotNone(executor.consume_next())
            self.assertTrue(
                executor.shutdown(cancel_live=False, join_timeout_s=1.0).complete
            )
            recorder.close()
            summary = replay_file(recorder.path)

        dispositions = dict(summary.disposition_counts)
        self.assertEqual(summary.admitted_total, 3)
        self.assertEqual(summary.terminal_admitted_total, 3)
        self.assertEqual(dispositions["dropped_overflow"], 1)
        self.assertEqual(dispositions["cancelled_queued"], 1)
        self.assertEqual(dispositions["consumed"], 1)

    def test_concurrent_producer_trace_replays_with_exact_depths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            recorder = EventRecorder(Path(temp_dir), RUN_ID)
            broker = BoundedTaskBroker(
                LaneConfig(
                    task_kind=TaskKind.SIMULATED,
                    pending_capacity=4,
                    result_capacity=1,
                )
            )
            executor = ObservableExecutor(
                broker,
                SimulatedAdapter(1.0, poll_interval_s=0.001),
                event_sink=recorder,
            )
            executor.start()
            barrier = threading.Barrier(17)
            threads = []

            def submit_one(value: int) -> None:
                barrier.wait()
                executor.submit(make_task(f"producer-{value}"))

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
            report = executor.shutdown(cancel_live=True, join_timeout_s=1.0)
            recorder.close()
            summary = replay_file(recorder.path)

        self.assertTrue(report.complete)
        self.assertEqual(summary.submission_attempts, 16)
        self.assertLessEqual(summary.max_pending_depth, 4)
        self.assertLessEqual(summary.max_result_depth, 1)
        self.assertEqual(summary.terminal_admitted_total, summary.admitted_total)


if __name__ == "__main__":
    unittest.main()
