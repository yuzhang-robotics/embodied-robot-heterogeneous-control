from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from experiments.phase1.asr_adapter import (
    ASRRuntime,
    FixedInputASRAdapter,
    fixed_asr_payload,
)
from experiments.phase1.asr_slice import ASRSliceCondition, ASRSliceSpec, run_asr_slice
from experiments.phase1.replay_lifecycle import TraceProfile, load_events, replay_events
from experiments.phase1.telemetry import EventRecorder


class ASRSliceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.input_path = self.root / "fixed.wav"
        self.input_bytes = b"phase1-asr-slice-audio"
        self.input_path.write_bytes(self.input_bytes)
        self.model_path = self.root / "ggml-small.bin"
        self.model_bytes = b"phase1-asr-slice-model"
        self.model_path.write_bytes(self.model_bytes)
        self.transcript = "固定识别结果"
        self.transcript_hash = hashlib.sha256(
            self.transcript.encode("utf-8")
        ).hexdigest()
        self.runtime = ASRRuntime(
            whisper_dir=self.root,
            whisper_binary=Path(sys.executable).resolve(),
            whisper_model=self.model_path,
        )
        self.processes: list[subprocess.Popen[bytes]] = []
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

    def process_factory(self, command: list[str], _cwd: Path):
        output_base = Path(command[command.index("-of") + 1])
        output_txt = output_base.with_suffix(".txt")
        script = (
            "import time; from pathlib import Path; time.sleep(0.05); "
            f"Path({str(output_txt)!r}).write_text({self.transcript!r}, "
            "encoding='utf-8')"
        )
        process = subprocess.Popen(
            [sys.executable, "-c", script],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
        )
        self.processes.append(process)
        return process

    def run_condition(self, condition: ASRSliceCondition):
        run_id = f"20260831T010000Z_phase1_{condition.value}_asr_001"
        adapter = FixedInputASRAdapter(
            runtime_loader=lambda: self.runtime,
            process_factory=self.process_factory,
            execution_timeout_s=1.0,
            poll_interval_s=0.005,
            terminate_timeout_s=1.0,
            kill_timeout_s=1.0,
        )
        spec = ASRSliceSpec(
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
        payload = fixed_asr_payload(self.input_path)
        with EventRecorder(self.root / run_id, run_id) as recorder:
            report = run_asr_slice(spec, payload, recorder, adapter=adapter)
        replay = replay_events(
            load_events(self.root / run_id / "events.jsonl"),
            profile=TraceProfile.RUNTIME_THREADED_PROBE,
        )
        return report, replay

    def test_nominal_result_is_consumed_once(self) -> None:
        baseline_threads = {thread.name for thread in threading.enumerate()}
        report, replay = self.run_condition(ASRSliceCondition.ASYNC)
        self.assertTrue(report.consumed)
        self.assertEqual(report.final_disposition, "consumed")
        self.assertEqual(replay.accepted_result_count, 1)
        self.assertEqual(report.adapter.process_exit_code, 0)
        self.assertTrue(report.adapter.process_reaped)
        self.assertTrue(report.shutdown.complete)
        self.assertTrue(report.probe.joined)
        self.assertEqual(
            {thread.name for thread in threading.enumerate()}, baseline_threads
        )
        self.assertTrue(all(process.poll() is not None for process in self.processes))

    def test_state_change_stops_whisper_and_rejects_result(self) -> None:
        report, replay = self.run_condition(ASRSliceCondition.STALE)
        self.assertFalse(report.consumed)
        self.assertTrue(report.state_advanced)
        self.assertEqual(report.final_disposition, "rejected_state")
        self.assertEqual(replay.accepted_result_count, 0)
        self.assertEqual(replay.stale_consumed_count, 0)
        self.assertEqual(report.adapter.execution_outcome, "cancel_observed")
        self.assertTrue(report.adapter.terminate_requested)
        self.assertTrue(report.adapter.process_reaped)
        self.assertTrue(report.adapter.backend_stop_confirmed)
        self.assertTrue(all(process.poll() is not None for process in self.processes))

    def test_artifacts_do_not_contain_transcript_or_input_path(self) -> None:
        report, _replay = self.run_condition(ASRSliceCondition.ASYNC)
        serialized = json.dumps(report.to_dict(), ensure_ascii=False)
        self.assertNotIn(self.transcript, serialized)
        self.assertNotIn(str(self.input_path), serialized)

    def test_spec_requires_deadline_beyond_completion_budget(self) -> None:
        with self.assertRaisesRegex(ValueError, "result_validity_s"):
            ASRSliceSpec(
                condition=ASRSliceCondition.ASYNC,
                result_validity_s=1.0,
                completion_timeout_s=1.0,
            )


if __name__ == "__main__":
    unittest.main()
