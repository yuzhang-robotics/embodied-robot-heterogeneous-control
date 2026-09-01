from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from experiments.phase1.jetson_preflight import build_jetson_preflight
from experiments.phase1.jetson_telemetry import TegrastatsSampler
from experiments.phase1.llm_adapter import FixedInputLLMAdapter, llm_request_contract
from experiments.phase1.llm_preflight import build_llm_preflight
from experiments.phase1.llm_slice import LLMSliceCondition
from experiments.phase1.manifest import sha256_file
from experiments.phase1.run_llm_slice import build_parser, run_once
from experiments.phase1.tests.test_jetson_pilot import clean_environment
from experiments.phase1.tests.test_jetson_telemetry import sampler_command
from experiments.phase1.validate_llm_slice import validate_llm_slice_dir


SESSION_ID = "20260901T020000Z_phase1_llm_test"
REPO_ROOT = Path(__file__).resolve().parents[3]


def base_preflight() -> dict[str, object]:
    return build_jetson_preflight(
        REPO_ROOT,
        environment=clean_environment(),
        tegrastats_available=True,
        loaded_modules={"experiments.phase1.run_llm_slice"},
    )


class LLMRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.input_path = self.root / "fixed.txt"
        self.prompt = "private phase1 runner prompt"
        self.input_bytes = self.prompt.encode("utf-8")
        self.input_path.write_bytes(self.input_bytes)
        self.model_bytes = b"phase1-llm-runner-model"
        self.input_hash = hashlib.sha256(self.input_bytes).hexdigest()
        self.model_hash = hashlib.sha256(self.model_bytes).hexdigest()
        self.output_text = "private phase1 runner response"
        shared = {
            "LLM_INPUT_SHA256": self.input_hash,
            "LLM_INPUT_SIZE_BYTES": len(self.input_bytes),
            "LLM_MODEL_SHA256": self.model_hash,
            "LLM_MODEL_SIZE_BYTES": len(self.model_bytes),
            "LLM_EXPECTED_SERVED_MODEL_ID": "qwen-test.gguf",
        }
        self.constants = [
            patch.multiple(
                "experiments.phase1.llm_adapter",
                LLM_INPUT_SHA256=self.input_hash,
                LLM_INPUT_SIZE_BYTES=len(self.input_bytes),
            ),
            patch.multiple("experiments.phase1.llm_preflight", **shared),
            patch.multiple(
                "experiments.phase1.run_llm_slice",
                LLM_MODEL_SHA256=self.model_hash,
                LLM_MODEL_SIZE_BYTES=len(self.model_bytes),
                LLM_EXPECTED_SERVED_MODEL_ID="qwen-test.gguf",
            ),
            patch.multiple(
                "experiments.phase1.summarize_llm_slice",
                LLM_INPUT_SHA256=self.input_hash,
                LLM_INPUT_SIZE_BYTES=len(self.input_bytes),
            ),
            patch.multiple("experiments.phase1.validate_llm_slice", **shared),
        ]
        for value in self.constants:
            value.start()
            self.addCleanup(value.stop)

    def args(self, condition: LLMSliceCondition):
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
                "--adapter-request-timeout-s",
                "0.8",
            ]
        )

    def preflight_builder(self, _root, *, input_payload, expected_branch):
        self.assertEqual(expected_branch, "main")
        return build_llm_preflight(
            REPO_ROOT,
            input_payload=input_payload,
            expected_branch=expected_branch,
            base_preflight=base_preflight(),
            runtime_status={
                "model_size_bytes": len(self.model_bytes),
                "model_sha256": self.model_hash,
                "source_version": "b123456",
                "source_clean": True,
                "server_process_count": 1,
                "server_arguments": {
                    "host": "127.0.0.1",
                    "port": 8080,
                    "n_gpu_layers": 10,
                    "ctx_size": 1024,
                    "threads": 4,
                    "parallel": 1,
                    "cache_ram": 0,
                },
                "server_arguments_match": True,
                "server_model_path_matches": True,
                "endpoint_local": True,
                "listener_addresses": ["127.0.0.1"],
                "listener_loopback_only": True,
                "listener_error_code": None,
                "service_reachable": True,
                "served_model_ids": ["qwen-test.gguf"],
                "expected_model_present": True,
                "request_contract": llm_request_contract(),
                "error_code": None,
            },
        )

    @staticmethod
    def sampler_factory(run_dir, interval_ms):
        return TegrastatsSampler(run_dir, interval_ms, command=sampler_command())

    def requester(self, _url: str, _payload: bytes, _timeout: float) -> bytes:
        threading.Event().wait(0.12)
        return json.dumps(
            {
                "model": "qwen-test.gguf",
                "choices": [
                    {"message": {"content": self.output_text, "role": "assistant"}}
                ],
                "usage": {
                    "prompt_tokens": 20,
                    "completion_tokens": 10,
                    "total_tokens": 30,
                },
            }
        ).encode("utf-8")

    def adapter_factory(self) -> FixedInputLLMAdapter:
        return FixedInputLLMAdapter(
            endpoint_loader=lambda: "http://127.0.0.1:8080/v1/chat/completions",
            requester=self.requester,
            request_timeout_s=0.8,
        )

    def test_both_conditions_create_independently_validated_artifacts(self) -> None:
        baseline_threads = {thread.name for thread in threading.enumerate()}
        with (
            patch.dict(os.environ, {"ROBOT_ENABLE_MOTION": "0"}),
            patch(
                "experiments.phase1.run_llm_slice.collect_environment",
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
                for condition in LLMSliceCondition
            ]

        for run_dir, condition in zip(run_dirs, LLMSliceCondition):
            with self.subTest(condition=condition.value):
                self.assertEqual(validate_llm_slice_dir(run_dir), [])
                manifest = json.loads(
                    (run_dir / "manifest.json").read_text(encoding="utf-8")
                )
                summary = json.loads(
                    (run_dir / "summary.json").read_text(encoding="utf-8")
                )
                self.assertEqual(manifest["status"], "completed")
                self.assertEqual(
                    manifest["adapter_isolation"], "llama_http_client_thread"
                )
                self.assertTrue(summary["valid"])
                self.assertTrue(summary["development_injection"])
                self.assertFalse(summary["real_llm_path_executed"])
                self.assertFalse(summary["backend_cancellation_claim_permitted"])
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
                self.assertNotIn(self.prompt, combined)
                self.assertNotIn(self.output_text, combined)
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
        self.assertTrue(stale["adapter"]["cancellation"]["worker_observed"])
        self.assertFalse(stale["adapter"]["cancellation"]["client_wait_stopped"])
        self.assertIsNone(stale["adapter"]["cancellation"]["backend_stop_confirmed"])
        self.assertGreater(stale["resources"]["inference_interval_sample_count"], 0)
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
                    self.args(LLMSliceCondition.ASYNC),
                    repo_root=REPO_ROOT,
                    preflight_builder=failed_builder,
                    sampler_factory=self.sampler_factory,
                    adapter_factory=self.adapter_factory,
                )
        self.assertFalse((self.root / "runs" / SESSION_ID).exists())

    def test_timeout_budget_is_rejected_before_output_creation(self) -> None:
        args = self.args(LLMSliceCondition.ASYNC)
        args.adapter_request_timeout_s = args.completion_timeout_s
        with patch.dict(os.environ, {"ROBOT_ENABLE_MOTION": "0"}):
            with self.assertRaisesRegex(RuntimeError, "request timeout"):
                run_once(
                    args,
                    repo_root=REPO_ROOT,
                    preflight_builder=self.preflight_builder,
                    sampler_factory=self.sampler_factory,
                    adapter_factory=self.adapter_factory,
                )
        self.assertFalse((self.root / "runs" / SESSION_ID).exists())

    def test_stale_observation_budget_is_rejected_before_output_creation(self) -> None:
        args = self.args(LLMSliceCondition.STALE)
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
                "experiments.phase1.run_llm_slice.collect_environment",
                return_value=clean_environment(),
            ),
        ):
            run_dir = run_once(
                self.args(LLMSliceCondition.ASYNC),
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
        errors = validate_llm_slice_dir(run_dir)
        self.assertTrue(any("independently rebuilt" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
