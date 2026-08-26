"""Unit tests for immutable Phase 1 task and result contracts."""

from __future__ import annotations

import math
import sys
import unittest

from jetson.phase1_runtime.model import (
    CancellationReport,
    CancellationToken,
    ExecutionOutcome,
    PayloadRef,
    ResultEnvelope,
    StateToken,
    TaskEnvelope,
    TaskKind,
)


INPUT_SHA256 = "a" * 64
OUTPUT_SHA256 = "b" * 64


def make_payload() -> PayloadRef:
    return PayloadRef(
        ref="private/input.bin",
        sha256=INPUT_SHA256,
        size_bytes=32,
        media_type="application/octet-stream",
    )


def make_task(**overrides: object) -> TaskEnvelope:
    values: dict[str, object] = {
        "task_id": "task-001",
        "task_kind": TaskKind.SIMULATED,
        "source_monotonic_ns": 90,
        "created_monotonic_ns": 100,
        "deadline_monotonic_ns": 1000,
        "state_token": StateToken("interaction-1", 0),
        "payload": make_payload(),
        "metadata": {"duration_ms": 10, "cooperative": True},
    }
    values.update(overrides)
    return TaskEnvelope(**values)  # type: ignore[arg-type]


def make_result(**overrides: object) -> ResultEnvelope:
    values: dict[str, object] = {
        "task_id": "task-001",
        "task_kind": TaskKind.SIMULATED,
        "state_token": StateToken("interaction-1", 0),
        "source_monotonic_ns": 90,
        "deadline_monotonic_ns": 1000,
        "input_sha256": INPUT_SHA256,
        "started_monotonic_ns": 110,
        "finished_monotonic_ns": 120,
        "execution_outcome": ExecutionOutcome.OK,
        "output_sha256": OUTPUT_SHA256,
        "output_length": 4,
    }
    values.update(overrides)
    return ResultEnvelope(**values)  # type: ignore[arg-type]


class TaskModelTests(unittest.TestCase):
    def test_runtime_import_does_not_cross_the_motion_boundary(self) -> None:
        self.assertNotIn("jetson.robot_comm", sys.modules)
        self.assertNotIn("jetson.motion_planner", sys.modules)
        self.assertNotIn("serial", sys.modules)

    def test_valid_task_freezes_metadata_and_hides_payload_reference(self) -> None:
        metadata = {"duration_ms": 10}
        task = make_task(metadata=metadata)
        metadata["duration_ms"] = 99

        self.assertEqual(task.metadata["duration_ms"], 10)
        with self.assertRaises(TypeError):
            task.metadata["duration_ms"] = 20  # type: ignore[index]
        self.assertNotIn("private/input.bin", repr(task.payload))

    def test_task_rejects_invalid_timestamp_order(self) -> None:
        with self.assertRaisesRegex(ValueError, "after creation"):
            make_task(source_monotonic_ns=101)
        with self.assertRaisesRegex(ValueError, "before creation"):
            make_task(deadline_monotonic_ns=99)

    def test_payload_requires_lowercase_sha256_and_media_type(self) -> None:
        with self.assertRaisesRegex(ValueError, "lowercase SHA-256"):
            PayloadRef("input", "A" * 64, 1, "application/octet-stream")
        with self.assertRaisesRegex(ValueError, "MIME"):
            PayloadRef("input", INPUT_SHA256, 1, "Application/JSON")

    def test_metadata_rejects_nested_oversized_and_non_finite_values(self) -> None:
        with self.assertRaisesRegex(TypeError, "JSON scalar"):
            make_task(metadata={"nested": {"value": 1}})
        with self.assertRaisesRegex(ValueError, "must not exceed"):
            make_task(metadata={"message": "x" * 257})
        with self.assertRaisesRegex(ValueError, "finite"):
            make_task(metadata={"value": math.inf})

    def test_result_requires_consistent_output_descriptor(self) -> None:
        with self.assertRaisesRegex(ValueError, "present together"):
            make_result(output_sha256=None)
        with self.assertRaisesRegex(ValueError, "present together"):
            make_result(output_length=None)

    def test_error_and_timeout_results_require_bounded_error_code(self) -> None:
        with self.assertRaisesRegex(ValueError, "require error_code"):
            make_result(
                execution_outcome=ExecutionOutcome.ERROR,
                output_sha256=None,
                output_length=None,
            )
        result = make_result(
            execution_outcome=ExecutionOutcome.TIMEOUT,
            output_sha256=None,
            output_length=None,
            error_code="adapter_timeout",
        )
        self.assertEqual(result.error_code, "adapter_timeout")

    def test_cancel_observed_requires_matching_report(self) -> None:
        with self.assertRaisesRegex(ValueError, "worker_observed"):
            make_result(
                execution_outcome=ExecutionOutcome.CANCEL_OBSERVED,
                output_sha256=None,
                output_length=None,
            )
        result = make_result(
            execution_outcome=ExecutionOutcome.CANCEL_OBSERVED,
            output_sha256=None,
            output_length=None,
            cancellation_report=CancellationReport(
                requested=True,
                worker_observed=True,
                backend_stop_confirmed=True,
            ),
        )
        self.assertTrue(result.cancellation_report.worker_observed)

    def test_cancellation_report_does_not_infer_backend_stop(self) -> None:
        report = CancellationReport(requested=True)
        self.assertIsNone(report.backend_stop_confirmed)
        with self.assertRaisesRegex(ValueError, "requested=True"):
            CancellationReport(requested=False, client_wait_stopped=True)
        with self.assertRaisesRegex(ValueError, "requested=True"):
            CancellationReport(
                requested=False,
                backend_stop_confirmed=False,
            )

    def test_cancellation_token_is_idempotent(self) -> None:
        token = CancellationToken()
        self.assertTrue(token.request("user_cancel", 200))
        self.assertFalse(token.request("new_reason", 300))
        self.assertTrue(token.is_requested())
        self.assertEqual(token.reason, "user_cancel")
        self.assertEqual(token.requested_at_ns, 200)


if __name__ == "__main__":
    unittest.main()
