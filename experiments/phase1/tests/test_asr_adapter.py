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

from experiments.phase1.asr_adapter import (
    ASRRuntime,
    FixedInputASRAdapter,
    build_whisper_command,
    fixed_asr_payload,
)
from jetson.phase1_runtime import (
    CancellationToken,
    ClaimedTask,
    ExecutionOutcome,
    StateToken,
    TaskEnvelope,
    TaskKind,
)


class FixedInputASRAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.input_path = self.root / "fixed.wav"
        self.input_bytes = b"phase1-fixed-test-audio"
        self.input_path.write_bytes(self.input_bytes)
        self.model_path = self.root / "ggml-small.bin"
        self.model_bytes = b"phase1-test-model"
        self.model_path.write_bytes(self.model_bytes)
        self.binary_path = Path(sys.executable).resolve()
        self.transcript = "固定识别结果"
        self.transcript_hash = hashlib.sha256(
            self.transcript.encode("utf-8")
        ).hexdigest()
        self.runtime = ASRRuntime(
            whisper_dir=self.root,
            whisper_binary=self.binary_path,
            whisper_model=self.model_path,
        )
        self.constants = patch.multiple(
            "experiments.phase1.asr_adapter",
            ASR_INPUT_SHA256=hashlib.sha256(self.input_bytes).hexdigest(),
            ASR_INPUT_SIZE_BYTES=len(self.input_bytes),
            ASR_MODEL_SIZE_BYTES=len(self.model_bytes),
            ASR_EXPECTED_OUTPUT_SHA256=self.transcript_hash,
            ASR_EXPECTED_OUTPUT_LENGTH=len(self.transcript),
        )
        self.constants.start()
        self.addCleanup(self.constants.stop)

    def claimed_task(self, *, cancellation: CancellationToken | None = None):
        payload = fixed_asr_payload(self.input_path)
        now = time.monotonic_ns()
        task = TaskEnvelope(
            task_id="asr-test",
            task_kind=TaskKind.ASR,
            source_monotonic_ns=now,
            created_monotonic_ns=now,
            deadline_monotonic_ns=now + 10_000_000_000,
            state_token=StateToken("asr-test", 0),
            payload=payload,
        )
        return ClaimedTask(
            task=task,
            cancellation_token=cancellation or CancellationToken(),
            started_monotonic_ns=now,
        )

    def success_process(self, command: list[str], _cwd: Path):
        output_base = Path(command[command.index("-of") + 1])
        output_txt = output_base.with_suffix(".txt")
        script = (
            "from pathlib import Path; "
            f"Path({str(output_txt)!r}).write_text({self.transcript!r}, "
            "encoding='utf-8')"
        )
        return subprocess.Popen(
            [sys.executable, "-c", script],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
        )

    @staticmethod
    def sleeping_process(_command: list[str], _cwd: Path):
        return subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
        )

    def adapter(self, *, process_factory=None, **kwargs):
        return FixedInputASRAdapter(
            runtime_loader=lambda: self.runtime,
            process_factory=process_factory or self.success_process,
            poll_interval_s=0.005,
            terminate_timeout_s=1.0,
            kill_timeout_s=1.0,
            **kwargs,
        )

    def test_module_import_does_not_load_robot_application_or_config(self) -> None:
        code = (
            "import sys\n"
            "import experiments.phase1.asr_adapter\n"
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

    def test_command_preserves_phase0_whisper_arguments(self) -> None:
        command = build_whisper_command(
            self.runtime,
            self.input_path,
            self.root / "output",
        )
        self.assertEqual(
            command[3:],
            [
                "-f",
                str(self.input_path),
                "-l",
                "zh",
                "-otxt",
                "-of",
                str(self.root / "output"),
                "-nt",
                "-np",
                "-bs",
                "1",
                "-bo",
                "1",
            ],
        )

    def test_success_reaps_process_and_records_hash_only(self) -> None:
        adapter = self.adapter()
        result = adapter(self.claimed_task())
        self.assertEqual(result.execution_outcome, ExecutionOutcome.OK)
        self.assertEqual(result.output_sha256, self.transcript_hash)
        self.assertEqual(result.output_length, len(self.transcript))

        record = adapter.last_record
        self.assertIsNotNone(record)
        assert record is not None
        self.assertTrue(record.process_started)
        self.assertEqual(record.process_exit_code, 0)
        self.assertTrue(record.process_reaped)
        self.assertFalse(record.terminate_requested)
        serialized = json.dumps(record.to_dict(), ensure_ascii=False)
        self.assertNotIn(self.transcript, serialized)
        self.assertNotIn(str(self.input_path), serialized)
        self.assertFalse(record.to_dict()["output"]["raw_text_recorded"])

    def test_cancellation_terminates_and_reaps_whisper_process(self) -> None:
        token = CancellationToken()
        adapter = self.adapter(process_factory=self.sleeping_process)
        results = []
        worker = threading.Thread(
            target=lambda: results.append(
                adapter(self.claimed_task(cancellation=token))
            )
        )
        worker.start()
        self.assertTrue(adapter.inference_started_event.wait(1.0))
        token.request("state_changed", time.monotonic_ns())
        worker.join(2.0)

        self.assertFalse(worker.is_alive())
        result = results[0]
        self.assertEqual(result.execution_outcome, ExecutionOutcome.CANCEL_OBSERVED)
        self.assertTrue(result.cancellation_report.requested)
        self.assertTrue(result.cancellation_report.worker_observed)
        self.assertTrue(result.cancellation_report.client_wait_stopped)
        self.assertTrue(result.cancellation_report.backend_stop_confirmed)
        record = adapter.last_record
        assert record is not None
        self.assertTrue(record.terminate_requested)
        self.assertTrue(record.terminate_confirmed)
        self.assertTrue(record.process_reaped)
        self.assertIsNone(result.output_sha256)

    def test_timeout_terminates_and_reaps_whisper_process(self) -> None:
        adapter = self.adapter(
            process_factory=self.sleeping_process,
            execution_timeout_s=0.03,
        )
        result = adapter(self.claimed_task())
        self.assertEqual(result.execution_outcome, ExecutionOutcome.TIMEOUT)
        self.assertEqual(result.error_code, "whisper_timeout")
        record = adapter.last_record
        assert record is not None
        self.assertTrue(record.terminate_requested)
        self.assertTrue(record.process_reaped)

    def test_unexpected_transcript_fails_without_recording_text(self) -> None:
        self.transcript = "private unexpected transcript"
        adapter = self.adapter()
        result = adapter(self.claimed_task())
        self.assertEqual(result.execution_outcome, ExecutionOutcome.ERROR)
        self.assertEqual(result.error_code, "unexpected_transcript")
        self.assertIsNone(result.output_sha256)
        record = adapter.last_record
        assert record is not None
        self.assertNotIn(
            self.transcript,
            json.dumps(record.to_dict(), ensure_ascii=False),
        )

    def test_input_change_fails_before_process_start(self) -> None:
        claimed = self.claimed_task()
        self.input_path.write_bytes(b"changed")
        started = False

        def process_factory(command: list[str], cwd: Path):
            nonlocal started
            started = True
            return self.success_process(command, cwd)

        result = self.adapter(process_factory=process_factory)(claimed)
        self.assertEqual(result.execution_outcome, ExecutionOutcome.ERROR)
        self.assertEqual(result.error_code, "input_size_mismatch")
        self.assertFalse(started)


if __name__ == "__main__":
    unittest.main()
