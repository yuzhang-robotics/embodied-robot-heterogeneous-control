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
from experiments.phase1.manifest import sha256_file
from experiments.phase1.run_vlm_slice import build_parser, run_once
from experiments.phase1.tests.test_jetson_pilot import clean_environment
from experiments.phase1.tests.test_jetson_telemetry import sampler_command
from experiments.phase1.validate_vlm_slice import validate_vlm_slice_dir
from experiments.phase1.vlm_adapter import FixedInputVLMAdapter, VLMPipeline
from experiments.phase1.vlm_preflight import (
    build_vlm_preflight,
    probe_vlm_services,
)
from experiments.phase1.vlm_slice import VLMSliceCondition


SESSION_ID = "20260828T170000Z_phase1_vlm_test"
REPO_ROOT = Path(__file__).resolve().parents[3]


def service_status() -> dict[str, object]:
    return {
        "ollama": {
            "endpoint_local": True,
            "reachable": True,
            "model": "moondream",
            "model_present": True,
            "model_digest": "a" * 64,
            "error_code": None,
        },
        "qwen": {
            "endpoint_local": True,
            "reachable": True,
            "model": "qwen",
            "model_present": True,
            "served_model_ids": ["qwen2.5-1.5b-instruct-q4_k_m.gguf"],
            "error_code": None,
        },
        "python_dependencies": {"argostranslate": True, "cv2": True},
        "ollama_cli_available": True,
    }


def base_preflight() -> dict[str, object]:
    return build_jetson_preflight(
        REPO_ROOT,
        environment=clean_environment(),
        tegrastats_available=True,
        loaded_modules={"experiments.phase1.run_vlm_slice"},
    )


class VLMRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.input_path = self.root / "fixed.jpg"
        self.input_bytes = b"phase1-vlm-runner-fixed-image"
        self.input_path.write_bytes(self.input_bytes)
        self.digest = hashlib.sha256(self.input_bytes).hexdigest()
        self.constants = [
            patch.multiple(
                module,
                C100_INPUT_SHA256=self.digest,
                C100_INPUT_SIZE_BYTES=len(self.input_bytes),
            )
            for module in (
                "experiments.phase1.vlm_adapter",
                "experiments.phase1.vlm_preflight",
                "experiments.phase1.summarize_vlm_slice",
                "experiments.phase1.validate_vlm_slice",
            )
        ]
        for constant_patch in self.constants:
            constant_patch.start()
            self.addCleanup(constant_patch.stop)

    def args(self, condition: VLMSliceCondition):
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
                "--probe-period-ms",
                "5",
                "--probe-deadline-ms",
                "10",
                "--resource-interval-ms",
                "50",
                "--resource-first-sample-timeout-s",
                "2",
            ]
        )

    def preflight_builder(self, _root, *, input_payload, expected_branch):
        self.assertEqual(expected_branch, "main")
        return build_vlm_preflight(
            REPO_ROOT,
            input_payload=input_payload,
            expected_branch=expected_branch,
            base_preflight=base_preflight(),
            services=service_status(),
        )

    @staticmethod
    def sampler_factory(run_dir, interval_ms):
        return TegrastatsSampler(
            run_dir,
            interval_ms,
            command=sampler_command(),
        )

    @staticmethod
    def adapter_factory() -> FixedInputVLMAdapter:
        def describe(_path: Path) -> str:
            threading.Event().wait(0.04)
            return "private runner model output"

        return FixedInputVLMAdapter(
            pipeline_loader=lambda: VLMPipeline(
                describe_english=describe,
                rewrite_chinese=lambda _text: "固定结果",
                translate_fallback=lambda _text: "备用结果",
                normalize_output=lambda chinese, _english: chinese + "。",
                unload_model=lambda: None,
            )
        )

    def test_local_service_probe_records_identity_without_endpoint_text(self) -> None:
        def query(url: str) -> dict[str, object]:
            if url.endswith("/tags"):
                return {"models": [{"name": "moondream:latest", "digest": "b" * 64}]}
            return {"data": [{"id": "qwen"}]}

        with patch.dict(
            os.environ,
            {
                "ROBOT_OLLAMA_CHAT_URL": "http://127.0.0.1:11434/api/chat",
                "ROBOT_LLAMA_API_URL": ("http://127.0.0.1:8080/v1/chat/completions"),
            },
        ):
            status = probe_vlm_services(query=query)
        self.assertTrue(status["ollama"]["endpoint_local"])
        self.assertTrue(status["ollama"]["model_present"])
        self.assertEqual(status["ollama"]["model_digest"], "b" * 64)
        self.assertTrue(status["qwen"]["model_present"])
        self.assertEqual(status["qwen"]["served_model_ids"], ["qwen"])
        self.assertNotIn("endpoint", status["ollama"])

    def test_nonlocal_service_configuration_is_not_contacted(self) -> None:
        requested_urls: list[str] = []

        def query(url: str) -> dict[str, object]:
            requested_urls.append(url)
            return {}

        with (
            patch.dict(
                os.environ,
                {
                    "ROBOT_OLLAMA_CHAT_URL": "http://192.0.2.1:11434/api/chat",
                    "ROBOT_LLAMA_API_URL": (
                        "http://192.0.2.2:8080/v1/chat/completions"
                    ),
                },
            ),
            patch(
                "jetson.config.OLLAMA_CHAT_URL",
                "http://192.0.2.1:11434/api/chat",
            ),
            patch(
                "jetson.config.LLAMA_API_URL",
                "http://192.0.2.2:8080/v1/chat/completions",
            ),
        ):
            status = probe_vlm_services(query=query)
        self.assertEqual(requested_urls, [])
        self.assertEqual(status["ollama"]["error_code"], "nonlocal_endpoint")
        self.assertEqual(status["qwen"]["error_code"], "nonlocal_endpoint")

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
                    self.args(VLMSliceCondition.ASYNC),
                    repo_root=REPO_ROOT,
                    preflight_builder=failed_builder,
                    sampler_factory=self.sampler_factory,
                    adapter_factory=self.adapter_factory,
                )
        self.assertFalse((self.root / "runs" / SESSION_ID).exists())

    def test_both_conditions_create_independently_validated_artifacts(self) -> None:
        baseline_threads = {thread.name for thread in threading.enumerate()}
        with (
            patch.dict(os.environ, {"ROBOT_ENABLE_MOTION": "0"}),
            patch(
                "experiments.phase1.run_vlm_slice.collect_environment",
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
                for condition in VLMSliceCondition
            ]

        for run_dir, condition in zip(run_dirs, VLMSliceCondition):
            with self.subTest(condition=condition.value):
                self.assertEqual(validate_vlm_slice_dir(run_dir), [])
                manifest = json.loads(
                    (run_dir / "manifest.json").read_text(encoding="utf-8")
                )
                summary = json.loads(
                    (run_dir / "summary.json").read_text(encoding="utf-8")
                )
                self.assertEqual(manifest["status"], "completed")
                self.assertTrue(summary["valid"])
                self.assertTrue(summary["development_injection"])
                self.assertFalse(summary["real_vlm_path_executed"])
                self.assertFalse(summary["formal_performance_claim_permitted"])
                self.assertFalse(summary["heterogeneous_inference_claim_permitted"])
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
                self.assertNotIn("private runner model output", combined)
                self.assertNotIn(str(self.input_path), combined)

        nominal_summary = json.loads(
            (run_dirs[0] / "summary.json").read_text(encoding="utf-8")
        )
        stale_summary = json.loads(
            (run_dirs[1] / "summary.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            dict(nominal_summary["lifecycle"]["disposition_counts"]),
            {"consumed": 1},
        )
        self.assertEqual(
            dict(stale_summary["lifecycle"]["disposition_counts"]),
            {"rejected_state": 1},
        )
        self.assertEqual(stale_summary["lifecycle"]["accepted_result_count"], 0)
        remaining_threads = {thread.name for thread in threading.enumerate()}
        self.assertEqual(remaining_threads, baseline_threads)

    def test_validator_rebuilds_summary_after_hash_consistent_tampering(self) -> None:
        with (
            patch.dict(os.environ, {"ROBOT_ENABLE_MOTION": "0"}),
            patch(
                "experiments.phase1.run_vlm_slice.collect_environment",
                return_value=clean_environment(),
            ),
        ):
            run_dir = run_once(
                self.args(VLMSliceCondition.ASYNC),
                repo_root=REPO_ROOT,
                preflight_builder=self.preflight_builder,
                sampler_factory=self.sampler_factory,
                adapter_factory=self.adapter_factory,
            )
        summary_path = run_dir / "summary.json"
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        summary["resources"]["inference_interval_sample_count"] += 1
        summary_path.write_text(
            json.dumps(summary, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        manifest_path = run_dir / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["artifacts"]["summary.json"] = {
            "size_bytes": summary_path.stat().st_size,
            "sha256": sha256_file(summary_path),
        }
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

        errors = validate_vlm_slice_dir(run_dir)
        self.assertTrue(any("independently rebuilt" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
