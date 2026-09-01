from __future__ import annotations

import hashlib
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from experiments.phase1.llm_adapter import FixedInputLLMAdapter, fixed_llm_payload
from experiments.phase1.llm_slice import (
    LLMSliceCondition,
    LLMSliceSpec,
    run_llm_slice,
)
from experiments.phase1.replay_lifecycle import (
    TraceProfile,
    load_events,
    replay_events,
)
from experiments.phase1.telemetry import EventRecorder


class LLMSliceTests(unittest.TestCase):
    def run_condition(self, condition: LLMSliceCondition):
        baseline_threads = {thread.name for thread in threading.enumerate()}
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            prompt_path = root / "fixed.txt"
            prompt = b"private phase1 llm prompt"
            prompt_path.write_bytes(prompt)
            digest = hashlib.sha256(prompt).hexdigest()
            private_output = "private phase1 llm response"

            def requester(_url: str, _payload: bytes, _timeout: float) -> bytes:
                threading.Event().wait(0.03)
                return json.dumps(
                    {
                        "model": "qwen-test.gguf",
                        "choices": [
                            {
                                "message": {
                                    "content": private_output,
                                    "role": "assistant",
                                }
                            }
                        ],
                        "usage": {
                            "prompt_tokens": 20,
                            "completion_tokens": 10,
                            "total_tokens": 30,
                        },
                    }
                ).encode("utf-8")

            run_id = f"20260901T000000Z_phase1_{condition.value}_llm_001"
            adapter = FixedInputLLMAdapter(
                endpoint_loader=lambda: "http://127.0.0.1:8080/v1/chat/completions",
                requester=requester,
            )
            spec = LLMSliceSpec(
                condition=condition,
                result_validity_s=1.0,
                completion_timeout_s=0.5,
                join_timeout_s=1.0,
                probe_join_timeout_s=1.0,
                prelude_s=0.005,
                postlude_s=0.005,
                stale_observation_s=0.005,
                probe_period_ns=1_000_000,
                probe_deadline_ns=2_000_000,
            )
            with patch.multiple(
                "experiments.phase1.llm_adapter",
                LLM_INPUT_SHA256=digest,
                LLM_INPUT_SIZE_BYTES=len(prompt),
            ):
                payload = fixed_llm_payload(prompt_path)
                with EventRecorder(root / "run", run_id) as recorder:
                    report = run_llm_slice(spec, payload, recorder, adapter=adapter)
                replay = replay_events(
                    load_events(root / "run" / "events.jsonl"),
                    profile=TraceProfile.RUNTIME_THREADED_PROBE,
                )
                trace_text = (root / "run" / "events.jsonl").read_text(encoding="utf-8")
                report_text = json.dumps(report.to_dict(), ensure_ascii=False)

        remaining_threads = {thread.name for thread in threading.enumerate()}
        self.assertEqual(remaining_threads, baseline_threads)
        self.assertNotIn(prompt.decode("utf-8"), trace_text)
        self.assertNotIn(prompt.decode("utf-8"), report_text)
        self.assertNotIn(private_output, trace_text)
        self.assertNotIn(private_output, report_text)
        self.assertEqual(replay.admitted_total, 1)
        self.assertEqual(replay.terminal_admitted_total, 1)
        self.assertEqual(replay.stale_consumed_count, 0)
        self.assertTrue(report.shutdown.complete)
        self.assertTrue(report.probe.joined)
        self.assertGreater(report.probe.tick_count, 0)
        return report, replay

    def test_nominal_result_is_consumed_once(self) -> None:
        report, replay = self.run_condition(LLMSliceCondition.ASYNC)
        self.assertTrue(report.consumed)
        self.assertFalse(report.state_advanced)
        self.assertEqual(report.final_disposition, "consumed")
        self.assertEqual(replay.accepted_result_count, 1)

    def test_state_change_rejects_result_without_backend_stop_claim(self) -> None:
        report, replay = self.run_condition(LLMSliceCondition.STALE)
        self.assertFalse(report.consumed)
        self.assertTrue(report.state_advanced)
        self.assertEqual(report.final_disposition, "rejected_state")
        self.assertEqual(replay.accepted_result_count, 0)
        self.assertTrue(report.adapter.cancellation_requested)
        self.assertTrue(report.adapter.worker_observed_cancellation)
        self.assertIsNone(report.adapter.backend_stop_confirmed)
        self.assertIsNotNone(report.adapter.output_sha256)

    def test_spec_requires_deadline_beyond_completion_budget(self) -> None:
        with self.assertRaisesRegex(ValueError, "result_validity_s"):
            LLMSliceSpec(
                condition=LLMSliceCondition.ASYNC,
                result_validity_s=1.0,
                completion_timeout_s=1.0,
            )


if __name__ == "__main__":
    unittest.main()
