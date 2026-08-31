from __future__ import annotations

import hashlib
import json
import os
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
)
from experiments.phase1.asr_preflight import build_asr_preflight
from experiments.phase1.asr_slice import ASRSliceCondition
from experiments.phase1.jetson_preflight import build_jetson_preflight
from experiments.phase1.jetson_telemetry import TegrastatsSampler
from experiments.phase1.manifest import sha256_file
from experiments.phase1.run_asr_slice import build_parser, run_once
from experiments.phase1.tests.test_jetson_pilot import clean_environment
from experiments.phase1.tests.test_jetson_telemetry import sampler_command
from experiments.phase1.validate_asr_slice import validate_asr_slice_dir


SESSION_ID = "20260831T020000Z_phase1_asr_test"
REPO_ROOT = Path(__file__).resolve().parents[3]


def base_preflight() -> dict[str, object]:
    return build_jetson_preflight(
        REPO_ROOT,
        environment=clean_environment(),
        tegrastats_available=True,
        loaded_modules={"experiments.phase1.run_asr_slice"},
    )


class ASRRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.input_path = self.root / "fixed.wav"
        self.input_bytes = b"phase1-asr-runner-audio"
        self.input_path.write_bytes(self.input_bytes)
        self.model_path = self.root / "ggml-small.bin"
        self.model_bytes = b"phase1-asr-runner-model"
        self.model_path.write_bytes(self.model_bytes)
        self.input_hash = hashlib.sha256(self.input_bytes).hexdigest()
        self.model_hash = hashlib.sha256(self.model_bytes).hexdigest()
        self.transcript = "固定识别结果"
        self.transcript_hash = hashlib.sha256(
            self.transcript.encode("utf-8")
        ).hexdigest()
        self.runtime = ASRRuntime(
            whisper_dir=self.root,
            whisper_binary=Path(sys.executable).resolve(),
            whisper_model=self.model_path,
        )
        shared = {
            "ASR_INPUT_SHA256": self.input_hash,
            "ASR_INPUT_SIZE_BYTES": len(self.input_bytes),
            "ASR_MODEL_SHA256": self.model_hash,
            "ASR_MODEL_SIZE_BYTES": len(self.model_bytes),
            "ASR_EXPECTED_OUTPUT_SHA256": self.transcript_hash,
            "ASR_EXPECTED_OUTPUT_LENGTH": len(self.transcript),
        }
        self.constants = [
            patch.multiple("experiments.phase1.asr_adapter", **shared),
            patch.multiple(
                "experiments.phase1.asr_preflight",
                ASR_INPUT_SHA256=self.input_hash,
                ASR_INPUT_SIZE_BYTES=len(self.input_bytes),
                ASR_MODEL_SHA256=self.model_hash,
                ASR_MODEL_SIZE_BYTES=len(self.model_bytes),
            ),
            patch.multiple(
                "experiments.phase1.run_asr_slice",
                ASR_MODEL_SHA256=self.model_hash,
                ASR_MODEL_SIZE_BYTES=len(self.model_bytes),
                ASR_EXPECTED_OUTPUT_SHA256=self.transcript_hash,
                ASR_EXPECTED_OUTPUT_LENGTH=len(self.transcript),
            ),
            patch.multiple(
                "experiments.phase1.summarize_asr_slice",
                ASR_INPUT_SHA256=self.input_hash,
                ASR_INPUT_SIZE_BYTES=len(self.input_bytes),
                ASR_EXPECTED_OUTPUT_SHA256=self.transcript_hash,
                ASR_EXPECTED_OUTPUT_LENGTH=len(self.transcript),
            ),
            patch.multiple("experiments.phase1.validate_asr_slice", **shared),
        ]
        for value in self.constants:
            value.start()
            self.addCleanup(value.stop)

    def args(self, condition: ASRSliceCondition):
        return build_parser().parse_args(
            [
                "--condition",
                condition.value,
                "--input",
                str(self.input_path),
                "--session-id",
                SESSION_ID,
                "--output-root",
                str(self.root / "runs"),
                "--result-validity-s",
                "2",
                "--completion-timeout-s",
                "1",
                "--join-timeout-s",
                "1",
                "--probe-join-timeout-s",
                "1",
                "--prelude-s",
                "0.02",
                "--postlude-s",
                "0.02",
                "--stale-observation-s",
                "0.06",
                "--probe-period-ms",
                "5",
                "--probe-deadline-ms",
                "10",
                "--resource-interval-ms",
                "50",
                "--resource-first-sample-timeout-s",
                "2",
                "--adapter-execution-timeout-s",
                "0.8",
                "--adapter-poll-interval-s",
                "0.005",
                "--adapter-terminate-timeout-s",
                "1",
                "--adapter-kill-timeout-s",
                "1",
            ]
        )

    def preflight_builder(self, _root, *, input_payload, expected_branch):
        self.assertEqual(expected_branch, "main")
        return build_asr_preflight(
            REPO_ROOT,
            input_payload=input_payload,
            expected_branch=expected_branch,
            base_preflight=base_preflight(),
            runtime_status={
                "binary_available": True,
                "model_size_bytes": len(self.model_bytes),
                "model_sha256": self.model_hash,
                "source_version": "v1.8.4-326-gafa2ea54",
                "arguments": [
                    "-l",
                    "zh",
                    "-otxt",
                    "-nt",
                    "-np",
                    "-bs",
                    "1",
                    "-bo",
                    "1",
                ],
                "process_running": False,
                "error_code": None,
            },
        )

    @staticmethod
    def sampler_factory(run_dir, interval_ms):
        return TegrastatsSampler(
            run_dir,
            interval_ms,
            command=sampler_command(),
        )

    def process_factory(self, command: list[str], _cwd: Path):
        output_base = Path(command[command.index("-of") + 1])
        output_txt = output_base.with_suffix(".txt")
        script = (
            "import time; from pathlib import Path; time.sleep(0.12); "
            f"Path({str(output_txt)!r}).write_text({self.transcript!r}, "
            "encoding='utf-8')"
        )
        return subprocess.Popen(
            [sys.executable, "-c", script],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
        )

    def adapter_factory(self) -> FixedInputASRAdapter:
        return FixedInputASRAdapter(
            runtime_loader=lambda: self.runtime,
            process_factory=self.process_factory,
            execution_timeout_s=0.8,
            poll_interval_s=0.005,
            terminate_timeout_s=1.0,
            kill_timeout_s=1.0,
        )

    def test_both_conditions_create_independently_validated_artifacts(self) -> None:
        baseline_threads = {thread.name for thread in threading.enumerate()}
        with (
            patch.dict(os.environ, {"ROBOT_ENABLE_MOTION": "0"}),
            patch(
                "experiments.phase1.run_asr_slice.collect_environment",
                return_value=clean_environment(),
            ),
        ):
            run_dirs = [
                run_once(
                    self.args(condition),
                    repo_root=REPO_ROOT,
                    preflight_builder=self.preflight_builder,
                    sampler_factory=self.sampler_factory,
                    adapter_factory=self.adapter_factory,
                )
                for condition in ASRSliceCondition
            ]

        for run_dir, condition in zip(run_dirs, ASRSliceCondition):
            with self.subTest(condition=condition.value):
                self.assertEqual(validate_asr_slice_dir(run_dir), [])
                manifest = json.loads(
                    (run_dir / "manifest.json").read_text(encoding="utf-8")
                )
                summary = json.loads(
                    (run_dir / "summary.json").read_text(encoding="utf-8")
                )
                self.assertEqual(manifest["status"], "completed")
                self.assertEqual(manifest["adapter_isolation"], "whisper_subprocess")
                self.assertTrue(summary["valid"])
                self.assertTrue(summary["development_injection"])
                self.assertFalse(summary["real_asr_path_executed"])
                self.assertFalse(summary["formal_performance_claim_permitted"])
                combined = "\n".join(
                    (run_dir / name).read_text(encoding="utf-8")
                    for name in (
                        "manifest.json",
                        "preflight.json",
                        "events.jsonl",
                        "scenario.json",
                        "summary.json",
                    )
                )
                self.assertNotIn(self.transcript, combined)
                self.assertNotIn(str(self.input_path), combined)

        nominal = json.loads((run_dirs[0] / "summary.json").read_text())
        stale = json.loads((run_dirs[1] / "summary.json").read_text())
        self.assertEqual(
            dict(nominal["lifecycle"]["disposition_counts"]), {"consumed": 1}
        )
        self.assertEqual(
            dict(stale["lifecycle"]["disposition_counts"]),
            {"rejected_state": 1},
        )
        self.assertTrue(stale["adapter"]["process"]["reaped"])
        self.assertTrue(stale["adapter"]["cancellation"]["backend_stop_confirmed"])
        self.assertEqual(stale["spec"]["stale_observation_s"], 0.06)
        self.assertGreater(
            stale["resources"]["inference_interval_sample_count"],
            0,
        )
        self.assertTrue(
            next(
                gate
                for gate in stale["gates"]
                if gate["name"] == "stale_observation_window"
            )["passed"]
        )
        self.assertEqual(
            {thread.name for thread in threading.enumerate()}, baseline_threads
        )

    def test_failed_preflight_creates_no_run_directory(self) -> None:
        def failed_builder(_root, *, input_payload, expected_branch):
            preflight = self.preflight_builder(
                _root,
                input_payload=input_payload,
                expected_branch=expected_branch,
            )
            preflight["checks"][0]["passed"] = False
            preflight["eligible"] = False
            return preflight

        with patch.dict(os.environ, {"ROBOT_ENABLE_MOTION": "0"}):
            with self.assertRaisesRegex(RuntimeError, "preflight failed"):
                run_once(
                    self.args(ASRSliceCondition.ASYNC),
                    repo_root=REPO_ROOT,
                    preflight_builder=failed_builder,
                    sampler_factory=self.sampler_factory,
                    adapter_factory=self.adapter_factory,
                )
        self.assertFalse((self.root / "runs" / SESSION_ID).exists())

    def test_timeout_budget_is_rejected_before_output_creation(self) -> None:
        args = self.args(ASRSliceCondition.ASYNC)
        args.adapter_execution_timeout_s = args.completion_timeout_s
        with patch.dict(os.environ, {"ROBOT_ENABLE_MOTION": "0"}):
            with self.assertRaisesRegex(RuntimeError, "execution timeout"):
                run_once(
                    args,
                    repo_root=REPO_ROOT,
                    preflight_builder=self.preflight_builder,
                    sampler_factory=self.sampler_factory,
                    adapter_factory=self.adapter_factory,
                )
        self.assertFalse((self.root / "runs" / SESSION_ID).exists())

    def test_stale_observation_budget_is_rejected_before_output_creation(self) -> None:
        args = self.args(ASRSliceCondition.STALE)
        args.stale_observation_s = args.resource_interval_ms / 1000.0
        with patch.dict(os.environ, {"ROBOT_ENABLE_MOTION": "0"}):
            with self.assertRaisesRegex(RuntimeError, "resource interval"):
                run_once(
                    args,
                    repo_root=REPO_ROOT,
                    preflight_builder=self.preflight_builder,
                    sampler_factory=self.sampler_factory,
                    adapter_factory=self.adapter_factory,
                )
        self.assertFalse((self.root / "runs" / SESSION_ID).exists())

    def test_validator_rebuilds_summary_after_hash_consistent_tampering(self) -> None:
        with (
            patch.dict(os.environ, {"ROBOT_ENABLE_MOTION": "0"}),
            patch(
                "experiments.phase1.run_asr_slice.collect_environment",
                return_value=clean_environment(),
            ),
        ):
            run_dir = run_once(
                self.args(ASRSliceCondition.ASYNC),
                repo_root=REPO_ROOT,
                preflight_builder=self.preflight_builder,
                sampler_factory=self.sampler_factory,
                adapter_factory=self.adapter_factory,
            )
        summary_path = run_dir / "summary.json"
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        summary["resources"]["inference_interval_sample_count"] += 1
        summary_path.write_text(json.dumps(summary) + "\n", encoding="utf-8")
        manifest_path = run_dir / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["artifacts"]["summary.json"] = {
            "size_bytes": summary_path.stat().st_size,
            "sha256": sha256_file(summary_path),
        }
        manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")
        errors = validate_asr_slice_dir(run_dir)
        self.assertTrue(any("independently rebuilt" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
