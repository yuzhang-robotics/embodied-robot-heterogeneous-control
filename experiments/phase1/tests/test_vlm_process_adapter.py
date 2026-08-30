from __future__ import annotations

import hashlib
import json
import multiprocessing
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from experiments.phase1.vlm_process_adapter import ProcessIsolatedVLMAdapter
from experiments.phase1.telemetry import EventRecorder
from experiments.phase1.vlm_slice import (
    VLMSliceCondition,
    VLMSliceSpec,
    run_vlm_slice,
)
from jetson.phase1_runtime import (
    CancellationToken,
    ClaimedTask,
    ExecutionOutcome,
    PayloadRef,
    StateToken,
    TaskEnvelope,
    TaskKind,
)


FIXTURE_FACTORY = "experiments.phase1.tests.vlm_process_fixture:FixtureVLMAdapter"
ABRUPT_FACTORY = "experiments.phase1.tests.vlm_process_fixture:AbruptExitVLMAdapter"
UNRESPONSIVE_FACTORY = (
    "experiments.phase1.tests.vlm_process_fixture:UnresponsiveVLMAdapter"
)
ERROR_FACTORY = "experiments.phase1.tests.vlm_process_fixture:ErrorVLMAdapter"


class ProcessIsolatedVLMAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.input_path = Path(self.temporary.name) / "fixed.jpg"
        self.input_path.write_bytes(b"fixed process input")

    def claimed(self, token: CancellationToken | None = None) -> ClaimedTask:
        digest = hashlib.sha256(self.input_path.read_bytes()).hexdigest()
        now = time.monotonic_ns()
        task = TaskEnvelope(
            task_id="vlm-process-test",
            task_kind=TaskKind.VLM,
            source_monotonic_ns=now,
            created_monotonic_ns=now,
            deadline_monotonic_ns=now + 10_000_000_000,
            state_token=StateToken("vlm-process-test", 0),
            payload=PayloadRef(
                ref=str(self.input_path),
                sha256=digest,
                size_bytes=self.input_path.stat().st_size,
                media_type="image/jpeg",
            ),
        )
        return ClaimedTask(
            task=task,
            cancellation_token=token or CancellationToken(),
            started_monotonic_ns=now,
        )

    def test_module_import_does_not_start_or_load_device_paths(self) -> None:
        code = (
            "import sys\n"
            "import experiments.phase1.vlm_process_adapter\n"
            "assert 'jetson.vision_vlm' not in sys.modules\n"
            "assert 'jetson.app' not in sys.modules\n"
            "assert 'jetson.robot_comm' not in sys.modules\n"
            "assert not __import__('multiprocessing').active_children()\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=Path(__file__).resolve().parents[3],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=5,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_spawned_worker_returns_bounded_result_and_is_reaped(self) -> None:
        adapter = ProcessIsolatedVLMAdapter(
            factory_ref=FIXTURE_FACTORY,
            execution_timeout_s=5.0,
        )
        result = adapter(self.claimed())
        record = adapter.last_record
        report = adapter.last_process_report

        self.assertEqual(result.execution_outcome, ExecutionOutcome.OK)
        self.assertIsNotNone(record)
        self.assertIsNotNone(report)
        assert record is not None
        assert report is not None
        self.assertEqual(report.start_method, "spawn")
        self.assertTrue(report.protocol_complete)
        self.assertEqual(report.exit_code, 0)
        self.assertFalse(report.terminate_requested)
        self.assertFalse(report.terminate_confirmed)
        self.assertIsNone(report.error_code)
        self.assertIsNotNone(report.child_started_monotonic_ns)
        self.assertIsNotNone(report.inference_started_monotonic_ns)
        serialized = json.dumps(
            {
                "record": record.to_dict(),
                "process": report.to_dict(),
            }
        )
        self.assertNotIn(str(self.input_path), serialized)
        self.assertNotIn("bounded fixture output", serialized)

    def test_cancellation_is_forwarded_without_backend_stop_claim(self) -> None:
        token = CancellationToken()
        adapter = ProcessIsolatedVLMAdapter(
            factory_ref=FIXTURE_FACTORY,
            execution_timeout_s=5.0,
        )
        results = []
        worker = threading.Thread(
            target=lambda: results.append(adapter(self.claimed(token)))
        )
        worker.start()
        self.assertTrue(adapter.inference_started_event.wait(3.0))
        token.request("state_changed", time.monotonic_ns())
        worker.join(5.0)

        self.assertFalse(worker.is_alive())
        self.assertEqual(results[0].execution_outcome, ExecutionOutcome.CANCEL_OBSERVED)
        self.assertTrue(results[0].cancellation_report.worker_observed)
        self.assertIsNone(results[0].cancellation_report.backend_stop_confirmed)
        report = adapter.last_process_report
        self.assertIsNotNone(report)
        assert report is not None
        self.assertTrue(report.cancellation_forwarded)
        self.assertFalse(report.terminate_requested)
        self.assertEqual(report.exit_code, 0)

    def test_abrupt_child_exit_returns_error_and_records_exit_code(self) -> None:
        adapter = ProcessIsolatedVLMAdapter(
            factory_ref=ABRUPT_FACTORY,
            execution_timeout_s=5.0,
        )
        result = adapter(self.claimed())
        report = adapter.last_process_report

        self.assertEqual(result.execution_outcome, ExecutionOutcome.ERROR)
        self.assertEqual(result.error_code, "process_worker_exit")
        self.assertIsNotNone(report)
        assert report is not None
        self.assertEqual(report.exit_code, 17)
        self.assertFalse(report.protocol_complete)
        self.assertEqual(report.error_code, "process_worker_exit")

    def test_child_error_result_preserves_empty_output(self) -> None:
        adapter = ProcessIsolatedVLMAdapter(
            factory_ref=ERROR_FACTORY,
            execution_timeout_s=5.0,
        )
        result = adapter(self.claimed())
        record = adapter.last_record
        report = adapter.last_process_report

        self.assertEqual(result.execution_outcome, ExecutionOutcome.ERROR)
        self.assertEqual(result.error_code, "fixture_failure")
        self.assertIsNone(result.output_sha256)
        self.assertIsNotNone(record)
        self.assertIsNotNone(report)
        assert record is not None
        assert report is not None
        self.assertIsNone(record.output_sha256)
        self.assertTrue(report.protocol_complete)
        self.assertEqual(report.exit_code, 0)

    def test_unresponsive_child_is_terminated_and_reaped(self) -> None:
        adapter = ProcessIsolatedVLMAdapter(
            factory_ref=UNRESPONSIVE_FACTORY,
            execution_timeout_s=0.1,
            join_timeout_s=0.1,
            terminate_join_timeout_s=1.0,
        )
        result = adapter(self.claimed())
        report = adapter.last_process_report

        self.assertEqual(result.execution_outcome, ExecutionOutcome.TIMEOUT)
        self.assertEqual(result.error_code, "process_worker_timeout")
        self.assertIsNotNone(report)
        assert report is not None
        self.assertTrue(report.terminate_requested)
        self.assertTrue(report.terminate_confirmed)
        self.assertIsNotNone(report.exit_code)
        self.assertFalse(report.protocol_complete)

    def test_only_spawn_start_method_is_accepted(self) -> None:
        with self.assertRaisesRegex(ValueError, "start_method must be spawn"):
            ProcessIsolatedVLMAdapter(
                factory_ref=FIXTURE_FACTORY,
                start_method="fork",
            )

    def test_existing_slice_owns_lifecycle_around_process_adapter(self) -> None:
        baseline_children = {child.pid for child in multiprocessing.active_children()}
        adapter = ProcessIsolatedVLMAdapter(
            factory_ref=FIXTURE_FACTORY,
            execution_timeout_s=5.0,
        )
        claimed = self.claimed()
        spec = VLMSliceSpec(
            condition=VLMSliceCondition.STALE,
            result_validity_s=5.0,
            completion_timeout_s=3.0,
            join_timeout_s=3.0,
            probe_join_timeout_s=1.0,
            prelude_s=0.005,
            postlude_s=0.005,
            probe_period_ns=5_000_000,
            probe_deadline_ns=10_000_000,
        )
        run_id = "20260830T160000Z_phase1_vlm_process_vlm_001"
        with EventRecorder(Path(self.temporary.name) / "run", run_id) as sink:
            report = run_vlm_slice(
                spec,
                claimed.task.payload,
                sink,
                adapter=adapter,
                task_id="vlm-process-slice",
            )

        remaining_children = {child.pid for child in multiprocessing.active_children()}
        self.assertEqual(remaining_children, baseline_children)
        self.assertEqual(report.final_disposition, "rejected_state")
        self.assertFalse(report.consumed)
        self.assertTrue(report.adapter.worker_observed_cancellation)
        process_report = adapter.last_process_report
        self.assertIsNotNone(process_report)
        assert process_report is not None
        self.assertTrue(process_report.protocol_complete)
        self.assertEqual(process_report.exit_code, 0)


if __name__ == "__main__":
    unittest.main()
