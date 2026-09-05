from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from experiments.phase1.vlm_adapter import (
    FixedInputVLMAdapter,
    VLMPipeline,
    fixed_c100_payload,
)
from jetson.phase1_runtime import (
    CancellationToken,
    ClaimedTask,
    ExecutionOutcome,
    StateToken,
    TaskEnvelope,
    TaskKind,
)


def make_pipeline(
    *,
    qwen_error: bool = False,
    unload_error: bool = False,
    description_delay_s: float = 0.0,
    calls: list[str] | None = None,
) -> VLMPipeline:
    observed_calls = calls if calls is not None else []

    def describe(_path: Path) -> str:
        observed_calls.append("moondream_inference")
        print("private English model output")
        if description_delay_s:
            threading.Event().wait(description_delay_s)
        return "A camera is centered in the image."

    def rewrite(_english: str) -> str:
        observed_calls.append("qwen_rewrite")
        if qwen_error:
            raise OSError("private Qwen error")
        return "画面中间是一台摄像机"

    def unload() -> None:
        observed_calls.append("model_unload")
        if unload_error:
            raise RuntimeError("private unload failure")
        print("private unload message")

    return VLMPipeline(
        describe_english=describe,
        rewrite_chinese=rewrite,
        translate_fallback=lambda _english: "图像中有一台摄像机",
        normalize_output=lambda chinese, _english: chinese + "。",
        unload_model=unload,
    )


class FixedInputVLMAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.input_path = Path(self.temporary.name) / "fixed.jpg"
        self.input_bytes = b"phase1-fixed-test-image"
        self.input_path.write_bytes(self.input_bytes)
        self.digest = hashlib.sha256(self.input_bytes).hexdigest()
        self.constants = patch.multiple(
            "experiments.phase1.vlm_adapter",
            C100_INPUT_SHA256=self.digest,
            C100_INPUT_SIZE_BYTES=len(self.input_bytes),
        )
        self.constants.start()
        self.addCleanup(self.constants.stop)

    def claimed_task(self, *, cancellation: CancellationToken | None = None):
        payload = fixed_c100_payload(self.input_path)
        now = time.monotonic_ns()
        task = TaskEnvelope(
            task_id="vlm-test",
            task_kind=TaskKind.VLM,
            source_monotonic_ns=now,
            created_monotonic_ns=now,
            deadline_monotonic_ns=now + 10_000_000_000,
            state_token=StateToken("vlm-test", 0),
            payload=payload,
        )
        return ClaimedTask(
            task=task,
            cancellation_token=cancellation or CancellationToken(),
            started_monotonic_ns=now,
        )

    def test_module_import_does_not_load_the_device_pipeline(self) -> None:
        code = (
            "import sys\n"
            "import experiments.phase1.vlm_adapter\n"
            "assert 'jetson.vision_vlm' not in sys.modules\n"
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

    def test_pipeline_loading_is_lazy_and_output_is_hash_only(self) -> None:
        load_count = 0
        calls: list[str] = []

        def load_pipeline() -> VLMPipeline:
            nonlocal load_count
            load_count += 1
            return make_pipeline(calls=calls)

        adapter = FixedInputVLMAdapter(pipeline_loader=load_pipeline)
        self.assertEqual(load_count, 0)
        result = adapter(self.claimed_task())
        self.assertEqual(load_count, 1)
        self.assertEqual(result.execution_outcome, ExecutionOutcome.OK)
        self.assertIsNotNone(result.output_sha256)
        self.assertEqual(result.output_length, len("画面中间是一台摄像机。"))

        record = adapter.last_record
        self.assertIsNotNone(record)
        assert record is not None
        serialized = json.dumps(record.to_dict(), ensure_ascii=False)
        self.assertNotIn("A camera is centered", serialized)
        self.assertNotIn("画面中间是一台摄像机", serialized)
        self.assertNotIn("private unload message", serialized)
        self.assertFalse(record.to_dict()["output"]["raw_text_recorded"])
        self.assertEqual(record.translation_route, "qwen")
        self.assertTrue(record.model_unload_requested)
        self.assertIsNone(record.model_unload_confirmed)
        self.assertEqual(record.stage_error_codes, {})
        self.assertEqual(
            calls,
            ["moondream_inference", "model_unload", "qwen_rewrite"],
        )
        for stage in (
            "input_verify_before",
            "module_import",
            "moondream_inference",
            "qwen_rewrite",
            "output_normalization",
            "model_unload",
            "input_verify_after",
        ):
            self.assertEqual(record.stage_status[stage], "ok")

    def test_qwen_failure_uses_argos_without_recording_error_text(self) -> None:
        adapter = FixedInputVLMAdapter(
            pipeline_loader=lambda: make_pipeline(qwen_error=True)
        )
        result = adapter(self.claimed_task())
        record = adapter.last_record
        self.assertEqual(result.execution_outcome, ExecutionOutcome.OK)
        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record.translation_route, "argos")
        self.assertEqual(record.stage_status["qwen_rewrite"], "error")
        self.assertEqual(record.stage_error_codes["qwen_rewrite"], "oserror")
        self.assertEqual(record.stage_status["argos_fallback"], "ok")
        self.assertNotIn("private Qwen error", json.dumps(record.to_dict()))

    def test_unload_failure_prevents_qwen_from_starting(self) -> None:
        calls: list[str] = []
        adapter = FixedInputVLMAdapter(
            pipeline_loader=lambda: make_pipeline(
                unload_error=True,
                calls=calls,
            )
        )

        result = adapter(self.claimed_task())
        record = adapter.last_record

        self.assertEqual(result.execution_outcome, ExecutionOutcome.ERROR)
        self.assertEqual(result.error_code, "model_unload_failed")
        self.assertEqual(calls, ["moondream_inference", "model_unload"])
        self.assertIsNotNone(record)
        assert record is not None
        self.assertIsNone(record.translation_route)
        self.assertEqual(record.stage_status["model_unload"], "error")
        self.assertEqual(
            record.stage_error_codes["model_unload"],
            "runtimeerror",
        )
        self.assertNotIn("private unload failure", json.dumps(record.to_dict()))

    def test_input_mismatch_fails_before_loading_the_pipeline(self) -> None:
        claimed = self.claimed_task()
        self.input_path.write_bytes(b"changed")
        loaded = False

        def load_pipeline() -> VLMPipeline:
            nonlocal loaded
            loaded = True
            return make_pipeline()

        adapter = FixedInputVLMAdapter(pipeline_loader=load_pipeline)
        result = adapter(claimed)
        self.assertEqual(result.execution_outcome, ExecutionOutcome.ERROR)
        self.assertEqual(result.error_code, "input_size_mismatch")
        self.assertFalse(loaded)
        self.assertIsNone(result.output_sha256)

    def test_post_backend_cancellation_is_observed_without_stop_claim(self) -> None:
        token = CancellationToken()
        claimed = self.claimed_task(cancellation=token)
        adapter = FixedInputVLMAdapter(
            pipeline_loader=lambda: make_pipeline(description_delay_s=0.03)
        )
        result_holder = []
        worker = threading.Thread(
            target=lambda: result_holder.append(adapter(claimed)),
            name="vlm-adapter-test",
        )
        worker.start()
        self.assertTrue(adapter.inference_started_event.wait(1.0))
        token.request("state_changed", time.monotonic_ns())
        worker.join(1.0)

        self.assertFalse(worker.is_alive())
        result = result_holder[0]
        self.assertEqual(result.execution_outcome, ExecutionOutcome.CANCEL_OBSERVED)
        self.assertTrue(result.cancellation_report.requested)
        self.assertTrue(result.cancellation_report.worker_observed)
        self.assertIsNone(result.cancellation_report.backend_stop_confirmed)
        self.assertIsNotNone(result.output_sha256)


if __name__ == "__main__":
    unittest.main()
