from __future__ import annotations

import hashlib
import json
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from experiments.phase1.llm_adapter import (
    LLM_EMPTY_HISTORY_SHA256,
    FixedInputLLMAdapter,
    fixed_llm_payload,
)
from jetson.phase1_runtime import (
    CancellationToken,
    ClaimedTask,
    ExecutionOutcome,
    StateToken,
    TaskEnvelope,
    TaskKind,
)


class FixedInputLLMAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.input_path = Path(self.temporary.name) / "fixed.txt"
        self.prompt = "private fixed prompt"
        self.input_bytes = self.prompt.encode("utf-8")
        self.input_path.write_bytes(self.input_bytes)
        self.digest = hashlib.sha256(self.input_bytes).hexdigest()
        self.response_text = "private model response"
        self.constants = patch.multiple(
            "experiments.phase1.llm_adapter",
            LLM_INPUT_SHA256=self.digest,
            LLM_INPUT_SIZE_BYTES=len(self.input_bytes),
        )
        self.constants.start()
        self.addCleanup(self.constants.stop)

    def response(self, *, usage: object | None = None) -> bytes:
        return json.dumps(
            {
                "model": "qwen-test.gguf",
                "choices": [
                    {"message": {"role": "assistant", "content": self.response_text}}
                ],
                "usage": (
                    usage
                    if usage is not None
                    else {
                        "prompt_tokens": 20,
                        "completion_tokens": 8,
                        "total_tokens": 28,
                    }
                ),
            }
        ).encode("utf-8")

    def claimed_task(
        self,
        *,
        cancellation: CancellationToken | None = None,
        history_sha256: str = LLM_EMPTY_HISTORY_SHA256,
    ):
        payload = fixed_llm_payload(self.input_path)
        now = time.monotonic_ns()
        task = TaskEnvelope(
            task_id="llm-test",
            task_kind=TaskKind.LLM,
            source_monotonic_ns=now,
            created_monotonic_ns=now,
            deadline_monotonic_ns=now + 10_000_000_000,
            state_token=StateToken("llm-test", 0),
            payload=payload,
            metadata={
                "history_sha256": history_sha256,
                "history_messages": 0,
            },
        )
        return ClaimedTask(
            task=task,
            cancellation_token=cancellation or CancellationToken(),
            started_monotonic_ns=now,
        )

    def test_module_import_does_not_load_robot_application_or_config(self) -> None:
        code = (
            "import sys\n"
            "import experiments.phase1.llm_adapter\n"
            "assert 'jetson.config' not in sys.modules\n"
            "assert 'jetson.app' not in sys.modules\n"
            "assert 'jetson.robot_comm' not in sys.modules\n"
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

    def test_success_records_identity_and_usage_without_private_text(self) -> None:
        observed_payloads: list[dict[str, object]] = []

        def requester(_url: str, encoded: bytes, _timeout: float) -> bytes:
            observed_payloads.append(json.loads(encoded.decode("utf-8")))
            return self.response()

        adapter = FixedInputLLMAdapter(
            endpoint_loader=lambda: "http://127.0.0.1:8080/v1/chat/completions",
            requester=requester,
        )
        result = adapter(self.claimed_task())

        self.assertEqual(result.execution_outcome, ExecutionOutcome.OK)
        self.assertEqual(
            result.output_sha256,
            hashlib.sha256(self.response_text.encode("utf-8")).hexdigest(),
        )
        self.assertEqual(result.output_length, len(self.response_text))
        self.assertEqual(observed_payloads[0]["messages"][1]["content"], self.prompt)
        record = adapter.last_record
        self.assertIsNotNone(record)
        assert record is not None
        serialized = json.dumps(record.to_dict(), ensure_ascii=False)
        self.assertNotIn(self.prompt, serialized)
        self.assertNotIn(self.response_text, serialized)
        self.assertNotIn(str(self.input_path), serialized)
        self.assertEqual(record.token_usage["completion_tokens"], 8)
        for stage in (
            "input_verify_before",
            "request_build",
            "llama_inference",
            "response_parse",
            "input_verify_after",
        ):
            self.assertEqual(record.stage_status[stage], "ok")
        self.assertFalse(record.to_dict()["output"]["raw_text_recorded"])
        self.assertFalse(record.to_dict()["response"]["raw_response_recorded"])

    def test_state_cancellation_waits_for_http_without_backend_stop_claim(self) -> None:
        token = CancellationToken()
        release = threading.Event()

        def requester(_url: str, _encoded: bytes, _timeout: float) -> bytes:
            release.wait(1.0)
            return self.response()

        adapter = FixedInputLLMAdapter(
            endpoint_loader=lambda: "http://127.0.0.1:8080/v1/chat/completions",
            requester=requester,
        )
        results = []
        worker = threading.Thread(
            target=lambda: results.append(
                adapter(self.claimed_task(cancellation=token))
            )
        )
        worker.start()
        self.assertTrue(adapter.inference_started_event.wait(1.0))
        token.request("state_changed", time.monotonic_ns())
        self.assertTrue(worker.is_alive())
        release.set()
        worker.join(1.0)

        self.assertFalse(worker.is_alive())
        result = results[0]
        self.assertEqual(result.execution_outcome, ExecutionOutcome.CANCEL_OBSERVED)
        self.assertTrue(result.cancellation_report.requested)
        self.assertTrue(result.cancellation_report.worker_observed)
        self.assertFalse(result.cancellation_report.client_wait_stopped)
        self.assertIsNone(result.cancellation_report.backend_stop_confirmed)
        self.assertIsNotNone(result.output_sha256)

    def test_timeout_is_bounded_without_serializing_exception_text(self) -> None:
        def requester(_url: str, _encoded: bytes, _timeout: float) -> bytes:
            raise socket.timeout("private endpoint detail")

        adapter = FixedInputLLMAdapter(
            endpoint_loader=lambda: "http://127.0.0.1:8080/v1/chat/completions",
            requester=requester,
        )
        result = adapter(self.claimed_task())
        self.assertEqual(result.execution_outcome, ExecutionOutcome.TIMEOUT)
        self.assertEqual(result.error_code, "llm_request_timeout")
        self.assertIsNone(result.output_sha256)
        record = adapter.last_record
        assert record is not None
        self.assertNotIn("private endpoint detail", json.dumps(record.to_dict()))

    def test_invalid_usage_fails_closed_and_drops_output_identity(self) -> None:
        adapter = FixedInputLLMAdapter(
            endpoint_loader=lambda: "http://127.0.0.1:8080/v1/chat/completions",
            requester=lambda _url, _encoded, _timeout: self.response(
                usage={"prompt_tokens": 20}
            ),
        )
        result = adapter(self.claimed_task())
        self.assertEqual(result.execution_outcome, ExecutionOutcome.ERROR)
        self.assertEqual(result.error_code, "invalid_llm_usage")
        self.assertIsNone(result.output_sha256)

    def test_input_change_fails_before_request(self) -> None:
        claimed = self.claimed_task()
        self.input_path.write_bytes(b"changed")
        calls = 0

        def requester(_url: str, _encoded: bytes, _timeout: float) -> bytes:
            nonlocal calls
            calls += 1
            return self.response()

        adapter = FixedInputLLMAdapter(
            endpoint_loader=lambda: "http://127.0.0.1:8080/v1/chat/completions",
            requester=requester,
        )
        result = adapter(claimed)
        self.assertEqual(result.execution_outcome, ExecutionOutcome.ERROR)
        self.assertEqual(result.error_code, "input_size_mismatch")
        self.assertEqual(calls, 0)

    def test_nonempty_history_identity_fails_before_request(self) -> None:
        calls = 0

        def requester(_url: str, _encoded: bytes, _timeout: float) -> bytes:
            nonlocal calls
            calls += 1
            return self.response()

        adapter = FixedInputLLMAdapter(
            endpoint_loader=lambda: "http://127.0.0.1:8080/v1/chat/completions",
            requester=requester,
        )
        result = adapter(self.claimed_task(history_sha256="0" * 64))
        self.assertEqual(result.execution_outcome, ExecutionOutcome.ERROR)
        self.assertEqual(result.error_code, "unsupported_history_snapshot")
        self.assertEqual(calls, 0)


if __name__ == "__main__":
    unittest.main()
