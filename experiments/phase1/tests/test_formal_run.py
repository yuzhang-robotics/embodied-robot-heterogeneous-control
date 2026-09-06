from __future__ import annotations

import hashlib
import json
import threading
import time
import unittest
from dataclasses import dataclass
from pathlib import Path

from experiments.phase1.asr_adapter import (
    ASR_EXPECTED_OUTPUT_LENGTH,
    ASR_EXPECTED_OUTPUT_SHA256,
)
from experiments.phase1.formal_run import (
    FormalCondition,
    FormalRunSpec,
    run_formal_workload,
)
from experiments.phase1.llm_adapter import (
    FixedInputLLMAdapter,
    LLM_EXPECTED_SERVED_MODEL_ID,
    fixed_llm_payload,
    frozen_llm_request_contract,
)
from jetson.phase1_runtime import (
    CancellationReport,
    ClaimedTask,
    ExecutionOutcome,
    NullEventSink,
    PayloadRef,
    ResultEnvelope,
)


@dataclass(frozen=True)
class FakeRecord:
    task_id: str
    workload: str
    input_sha256: str
    worker_thread_id: int
    started_monotonic_ns: int
    finished_monotonic_ns: int

    def to_dict(self) -> dict[str, object]:
        residency = (
            {
                "unload_requested": True,
                "unload_confirmed": None,
            }
            if self.workload == "vlm"
            else {
                "policy": "external_llama_server_resident",
                "server_preexisting": True,
                "unload_requested": False,
                "backend_stop_confirmed": None,
            }
        )
        output_sha256 = (
            ASR_EXPECTED_OUTPUT_SHA256 if self.workload == "asr" else "b" * 64
        )
        output_length = ASR_EXPECTED_OUTPUT_LENGTH if self.workload == "asr" else 1
        stage_durations = {"llama_inference": 1_000_000}
        stage_status: dict[str, str] = {}
        if self.workload == "vlm":
            stage_durations = {
                name: 1
                for name in (
                    "input_verify_before",
                    "module_import",
                    "moondream_inference",
                    "model_unload",
                    "qwen_rewrite",
                    "output_normalization",
                    "input_verify_after",
                )
            }
            stage_status = {name: "ok" for name in stage_durations}
        return {
            "task_id": self.task_id,
            "worker_thread_id": self.worker_thread_id,
            "started_monotonic_ns": self.started_monotonic_ns,
            "finished_monotonic_ns": self.finished_monotonic_ns,
            "duration_ns": self.finished_monotonic_ns - self.started_monotonic_ns,
            "execution_outcome": "ok",
            "error_code": None,
            "input": {"sha256": self.input_sha256, "size_bytes": 1},
            "output": {
                "sha256": output_sha256,
                "length": output_length,
                "raw_text_recorded": False,
            },
            "process": {
                "started": True,
                "exit_code": 0,
                "terminate_requested": False,
                "terminate_confirmed": False,
                "kill_requested": False,
                "kill_confirmed": False,
                "reaped": True,
            },
            "response": {
                "model": LLM_EXPECTED_SERVED_MODEL_ID,
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
                "raw_response_recorded": False,
            },
            "request": {
                **frozen_llm_request_contract(),
                "raw_prompt_recorded": False,
            },
            "model_residency": residency,
            "translation_route": "qwen" if self.workload == "vlm" else None,
            "stage_durations_ns": stage_durations,
            "stage_status": stage_status,
            "stage_error_codes": {},
            "cancellation": {
                "requested": False,
                "worker_observed": False,
                "client_wait_stopped": False,
                "backend_stop_confirmed": None,
            },
        }


@dataclass(frozen=True)
class FakeProcessReport:
    def to_dict(self) -> dict[str, object]:
        return {
            "protocol_version": "0.2.0",
            "start_method": "spawn",
            "protocol_complete": True,
            "exit_code": 0,
            "error_code": None,
            "joined_monotonic_ns": time.monotonic_ns(),
        }


class FakeAdapter:
    def __init__(
        self, delay_s: float = 0.03, *, process_isolated: bool = False
    ) -> None:
        self.delay_s = delay_s
        self.last_record: FakeRecord | None = None
        self.last_process_report = FakeProcessReport() if process_isolated else None

    def __call__(self, claimed: ClaimedTask) -> ResultEnvelope:
        started = claimed.started_monotonic_ns
        time.sleep(self.delay_s)
        finished = time.monotonic_ns()
        self.last_record = FakeRecord(
            task_id=claimed.task.task_id,
            workload=claimed.task.task_kind.value,
            input_sha256=claimed.task.payload.sha256,
            worker_thread_id=threading.get_ident(),
            started_monotonic_ns=started,
            finished_monotonic_ns=finished,
        )
        output_sha256 = (
            ASR_EXPECTED_OUTPUT_SHA256
            if claimed.task.task_kind.value == "asr"
            else "b" * 64
        )
        output_length = (
            ASR_EXPECTED_OUTPUT_LENGTH if claimed.task.task_kind.value == "asr" else 1
        )
        return ResultEnvelope(
            task_id=claimed.task.task_id,
            task_kind=claimed.task.task_kind,
            state_token=claimed.task.state_token,
            source_monotonic_ns=claimed.task.source_monotonic_ns,
            deadline_monotonic_ns=claimed.task.deadline_monotonic_ns,
            input_sha256=claimed.task.payload.sha256,
            started_monotonic_ns=started,
            finished_monotonic_ns=finished,
            execution_outcome=ExecutionOutcome.OK,
            output_sha256=output_sha256,
            output_length=output_length,
            cancellation_report=CancellationReport(),
        )


def payload() -> PayloadRef:
    return PayloadRef(
        ref="fixture://formal-input",
        sha256=hashlib.sha256(b"x").hexdigest(),
        size_bytes=1,
        media_type="text/plain",
    )


class FormalRunTests(unittest.TestCase):
    def llm_spec(self, condition: FormalCondition) -> FormalRunSpec:
        return FormalRunSpec(
            workload="llm",
            condition=condition,
            role="measured",
            prelude_s=0.02,
            postlude_s=0.02,
            completion_timeout_s=2.0,
            join_timeout_s=2.0,
            probe_period_ns=5_000_000,
            probe_deadline_ns=5_000_000,
        )

    def run_condition(self, condition: FormalCondition) -> dict[str, object]:
        return run_formal_workload(
            self.llm_spec(condition),
            payload(),
            NullEventSink(),
            FakeAdapter(),
            task_id=f"formal-{condition.value}",
        )

    def test_llm_paths_bind_the_frozen_empty_history_identity(self) -> None:
        input_path = (
            Path(__file__).resolve().parents[2]
            / "phase0"
            / "inputs"
            / "llm_prompt_zh.txt"
        )
        llm_payload = fixed_llm_payload(input_path)

        def requester(_url: str, _payload: bytes, _timeout: float) -> bytes:
            return json.dumps(
                {
                    "model": LLM_EXPECTED_SERVED_MODEL_ID,
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "fixed response",
                            }
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 20,
                        "completion_tokens": 8,
                        "total_tokens": 28,
                    },
                }
            ).encode("utf-8")

        for condition in (FormalCondition.SYNC, FormalCondition.ASYNC):
            with self.subTest(condition=condition.value):
                adapter = FixedInputLLMAdapter(
                    endpoint_loader=lambda: (
                        "http://127.0.0.1:8080/v1/chat/completions"
                    ),
                    requester=requester,
                )
                report = run_formal_workload(
                    self.llm_spec(condition),
                    llm_payload,
                    NullEventSink(),
                    adapter,
                    task_id=f"formal-contract-{condition.value}",
                )

                self.assertTrue(report["valid"])
                self.assertEqual(report["adapter"]["execution_outcome"], "ok")
                self.assertIsNone(report["adapter"]["error_code"])

    def test_sync_uses_calling_thread_and_inline_probe(self) -> None:
        report = self.run_condition(FormalCondition.SYNC)

        self.assertTrue(report["valid"])
        self.assertEqual(report["probe"]["implementation"], "inline_same_thread")
        self.assertFalse(report["runtime"]["used"])
        self.assertEqual(report["adapter"]["worker_thread_id"], threading.get_ident())
        self.assertGreaterEqual(report["probe"]["max_gap_ns"], 20_000_000)

    def test_async_uses_bounded_worker_and_independent_probe(self) -> None:
        report = self.run_condition(FormalCondition.ASYNC)

        self.assertTrue(report["valid"])
        self.assertEqual(report["probe"]["implementation"], "independent_thread")
        self.assertTrue(report["runtime"]["used"])
        self.assertEqual(report["runtime"]["pending_capacity"], 1)
        self.assertEqual(
            report["runtime"]["final_snapshot"]["disposition_counts"]["consumed"],
            1,
        )
        self.assertNotEqual(
            report["adapter"]["worker_thread_id"], threading.get_ident()
        )

    def test_sync_vlm_keeps_process_isolation_without_the_runtime_worker(self) -> None:
        spec = FormalRunSpec(
            workload="vlm",
            condition=FormalCondition.SYNC,
            role="measured",
            prelude_s=0.02,
            postlude_s=0.02,
            completion_timeout_s=2.0,
            join_timeout_s=2.0,
            probe_period_ns=5_000_000,
            probe_deadline_ns=5_000_000,
        )

        report = run_formal_workload(
            spec,
            payload(),
            NullEventSink(),
            FakeAdapter(process_isolated=True),
            task_id="formal-sync-vlm",
        )

        self.assertTrue(report["valid"])
        self.assertFalse(report["runtime"]["used"])
        self.assertEqual(report["process"]["start_method"], "spawn")

    def test_sync_asr_requires_expected_transcript_and_reaped_process(self) -> None:
        spec = FormalRunSpec(
            workload="asr",
            condition=FormalCondition.SYNC,
            role="measured",
            prelude_s=0.02,
            postlude_s=0.02,
            completion_timeout_s=2.0,
            join_timeout_s=2.0,
            probe_period_ns=5_000_000,
            probe_deadline_ns=5_000_000,
        )

        report = run_formal_workload(
            spec,
            payload(),
            NullEventSink(),
            FakeAdapter(),
            task_id="formal-sync-asr",
        )

        self.assertTrue(report["valid"])
        gates = {gate["name"]: gate for gate in report["gates"]}
        self.assertTrue(gates["transcript_identity"]["passed"])
        self.assertTrue(gates["child_process_reaped"]["passed"])

    def test_invalid_condition_and_modified_probe_contract_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "formal workload runs"):
            FormalRunSpec(
                workload="llm",
                condition=FormalCondition.IDLE,
                role="measured",
            )
        with self.assertRaisesRegex(ValueError, "positive integer"):
            FormalRunSpec(
                workload="llm",
                condition=FormalCondition.SYNC,
                role="measured",
                probe_period_ns=0,
            )


if __name__ == "__main__":
    unittest.main()
