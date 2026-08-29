from __future__ import annotations

import hashlib
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from experiments.phase1.replay_lifecycle import (
    TraceProfile,
    load_events,
    replay_events,
)
from experiments.phase1.telemetry import EventRecorder
from experiments.phase1.vlm_adapter import (
    FixedInputVLMAdapter,
    VLMPipeline,
    fixed_c100_payload,
)
from experiments.phase1.vlm_slice import (
    VLMSliceCondition,
    VLMSliceSpec,
    run_vlm_slice,
)


class VLMSliceTests(unittest.TestCase):
    def run_condition(self, condition: VLMSliceCondition):
        baseline_threads = {thread.name for thread in threading.enumerate()}
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            image_path = root / "fixed.jpg"
            content = b"phase1-vlm-slice-image"
            image_path.write_bytes(content)
            digest = hashlib.sha256(content).hexdigest()
            run_id = f"20260828T160000Z_phase1_{condition.value}_vlm_001"
            adapter = FixedInputVLMAdapter(
                pipeline_loader=lambda: VLMPipeline(
                    describe_english=self.describe,
                    rewrite_chinese=lambda _text: "固定输出",
                    translate_fallback=lambda _text: "备用输出",
                    normalize_output=lambda chinese, _english: chinese + "。",
                    unload_model=lambda: None,
                )
            )
            spec = VLMSliceSpec(
                condition=condition,
                result_validity_s=2.0,
                completion_timeout_s=1.0,
                join_timeout_s=1.0,
                probe_join_timeout_s=1.0,
                prelude_s=0.005,
                postlude_s=0.005,
                probe_period_ns=1_000_000,
                probe_deadline_ns=2_000_000,
            )
            with patch.multiple(
                "experiments.phase1.vlm_adapter",
                C100_INPUT_SHA256=digest,
                C100_INPUT_SIZE_BYTES=len(content),
            ):
                payload = fixed_c100_payload(image_path)
                with EventRecorder(root / "run", run_id) as recorder:
                    report = run_vlm_slice(
                        spec,
                        payload,
                        recorder,
                        adapter=adapter,
                    )
                replay = replay_events(
                    load_events(root / "run" / "events.jsonl"),
                    profile=TraceProfile.RUNTIME_THREADED_PROBE,
                )
                trace_text = (root / "run" / "events.jsonl").read_text(encoding="utf-8")
                report_text = json.dumps(report.to_dict(), ensure_ascii=False)

        remaining_threads = {thread.name for thread in threading.enumerate()}
        self.assertEqual(remaining_threads, baseline_threads)
        self.assertNotIn("private model output", trace_text)
        self.assertNotIn("private model output", report_text)
        self.assertEqual(replay.admitted_total, 1)
        self.assertEqual(replay.terminal_admitted_total, 1)
        self.assertEqual(replay.stale_consumed_count, 0)
        self.assertTrue(report.shutdown.complete)
        self.assertTrue(report.probe.joined)
        self.assertGreater(report.probe.tick_count, 0)
        return report, replay

    @staticmethod
    def describe(_path: Path) -> str:
        threading.Event().wait(0.03)
        return "private model output"

    def test_nominal_result_is_consumed_once(self) -> None:
        report, replay = self.run_condition(VLMSliceCondition.ASYNC)
        self.assertTrue(report.consumed)
        self.assertFalse(report.state_advanced)
        self.assertEqual(report.final_disposition, "consumed")
        self.assertEqual(replay.accepted_result_count, 1)

    def test_state_change_rejects_completed_backend_result(self) -> None:
        report, replay = self.run_condition(VLMSliceCondition.STALE)
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
            VLMSliceSpec(
                condition=VLMSliceCondition.ASYNC,
                result_validity_s=1.0,
                completion_timeout_s=1.0,
            )


if __name__ == "__main__":
    unittest.main()
