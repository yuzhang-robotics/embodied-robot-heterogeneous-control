"""Spawn-safe fixture adapters for process-boundary tests."""

from __future__ import annotations

import hashlib
import os
import threading
import time

from experiments.phase1.vlm_adapter import VLMExecutionRecord
from jetson.phase1_runtime import (
    CancellationReport,
    ClaimedTask,
    ExecutionOutcome,
    ResultEnvelope,
)


class FixtureVLMAdapter:
    def __init__(self) -> None:
        self.inference_started_event = threading.Event()
        self.last_record: VLMExecutionRecord | None = None

    def __call__(self, claimed: ClaimedTask) -> ResultEnvelope:
        self.inference_started_event.set()
        deadline = time.monotonic() + 0.25
        while time.monotonic() < deadline:
            if claimed.cancellation_token.is_requested():
                break
            time.sleep(0.005)
        requested = claimed.cancellation_token.is_requested()
        outcome = ExecutionOutcome.CANCEL_OBSERVED if requested else ExecutionOutcome.OK
        output = hashlib.sha256(b"bounded fixture output").hexdigest()
        finished = time.monotonic_ns()
        cancellation = CancellationReport(
            requested=requested,
            worker_observed=requested,
            backend_stop_confirmed=None,
        )
        result = ResultEnvelope(
            task_id=claimed.task.task_id,
            task_kind=claimed.task.task_kind,
            state_token=claimed.task.state_token,
            source_monotonic_ns=claimed.task.source_monotonic_ns,
            deadline_monotonic_ns=claimed.task.deadline_monotonic_ns,
            input_sha256=claimed.task.payload.sha256,
            started_monotonic_ns=claimed.started_monotonic_ns,
            finished_monotonic_ns=finished,
            execution_outcome=outcome,
            output_sha256=output,
            output_length=22,
            cancellation_report=cancellation,
        )
        self.last_record = VLMExecutionRecord(
            task_id=claimed.task.task_id,
            worker_thread_id=threading.get_ident(),
            started_monotonic_ns=claimed.started_monotonic_ns,
            finished_monotonic_ns=finished,
            execution_outcome=outcome.value,
            error_code=None,
            input_sha256=claimed.task.payload.sha256,
            input_size_bytes=claimed.task.payload.size_bytes,
            output_sha256=output,
            output_length=22,
            translation_route="qwen",
            model_unload_requested=True,
            model_unload_confirmed=None,
            stage_durations_ns={
                "input_verify_before": 1,
                "module_import": 1,
                "moondream_inference": 1,
                "qwen_rewrite": 1,
                "output_normalization": 1,
                "model_unload": 1,
                "input_verify_after": 1,
            },
            stage_status={
                "input_verify_before": "ok",
                "module_import": "ok",
                "moondream_inference": "ok",
                "qwen_rewrite": "ok",
                "output_normalization": "ok",
                "model_unload": "ok",
                "input_verify_after": "ok",
            },
            stage_error_codes={},
            cancellation_requested=requested,
            worker_observed_cancellation=requested,
            backend_stop_confirmed=None,
        )
        return result


class AbruptExitVLMAdapter:
    def __init__(self) -> None:
        self.inference_started_event = threading.Event()
        self.last_record = None

    def __call__(self, _claimed: ClaimedTask) -> ResultEnvelope:
        self.inference_started_event.set()
        os._exit(17)


class LingeringThreadVLMAdapter(FixtureVLMAdapter):
    def __call__(self, claimed: ClaimedTask) -> ResultEnvelope:
        blocker = threading.Event()
        threading.Thread(
            target=blocker.wait,
            args=(60.0,),
            name="phase1-vlm-lingering-runtime",
        ).start()
        return super().__call__(claimed)


class ErrorVLMAdapter(FixtureVLMAdapter):
    def __call__(self, claimed: ClaimedTask) -> ResultEnvelope:
        self.inference_started_event.set()
        finished = time.monotonic_ns()
        result = ResultEnvelope(
            task_id=claimed.task.task_id,
            task_kind=claimed.task.task_kind,
            state_token=claimed.task.state_token,
            source_monotonic_ns=claimed.task.source_monotonic_ns,
            deadline_monotonic_ns=claimed.task.deadline_monotonic_ns,
            input_sha256=claimed.task.payload.sha256,
            started_monotonic_ns=claimed.started_monotonic_ns,
            finished_monotonic_ns=finished,
            execution_outcome=ExecutionOutcome.ERROR,
            error_code="fixture_failure",
        )
        self.last_record = VLMExecutionRecord(
            task_id=claimed.task.task_id,
            worker_thread_id=threading.get_ident(),
            started_monotonic_ns=claimed.started_monotonic_ns,
            finished_monotonic_ns=finished,
            execution_outcome=ExecutionOutcome.ERROR.value,
            error_code="fixture_failure",
            input_sha256=claimed.task.payload.sha256,
            input_size_bytes=claimed.task.payload.size_bytes,
            output_sha256=None,
            output_length=None,
            translation_route=None,
            model_unload_requested=False,
            model_unload_confirmed=None,
            stage_durations_ns={"module_import": 1},
            stage_status={"module_import": "error"},
            stage_error_codes={"module_import": "runtimeerror"},
            cancellation_requested=False,
            worker_observed_cancellation=False,
            backend_stop_confirmed=None,
        )
        return result


class UnresponsiveVLMAdapter:
    def __init__(self) -> None:
        self.inference_started_event = threading.Event()
        self.last_record = None

    def __call__(self, _claimed: ClaimedTask) -> ResultEnvelope:
        self.inference_started_event.set()
        while True:
            time.sleep(1.0)
