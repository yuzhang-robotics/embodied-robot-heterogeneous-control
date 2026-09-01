from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from experiments.phase1.jetson_preflight import build_jetson_preflight
from experiments.phase1.llm_adapter import (
    fixed_llm_payload,
    frozen_llm_request_contract,
    llm_request_contract,
)
from experiments.phase1.llm_preflight import (
    LLMRuntime,
    build_llm_preflight,
    llm_preflight_errors,
    probe_llm_runtime,
)
from experiments.phase1.tests.test_jetson_pilot import clean_environment


REPO_ROOT = Path(__file__).resolve().parents[3]


def base_preflight() -> dict[str, object]:
    return build_jetson_preflight(
        REPO_ROOT,
        environment=clean_environment(),
        tegrastats_available=True,
        loaded_modules={"experiments.phase1.llm_preflight"},
    )


class LLMPreflightTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.input_path = self.root / "fixed.txt"
        self.input_bytes = b"phase1-llm-preflight-prompt"
        self.input_path.write_bytes(self.input_bytes)
        self.model_path = self.root / "qwen-test.gguf"
        self.model_bytes = b"phase1-llm-preflight-model"
        self.model_path.write_bytes(self.model_bytes)
        self.input_hash = hashlib.sha256(self.input_bytes).hexdigest()
        self.model_hash = hashlib.sha256(self.model_bytes).hexdigest()
        self.runtime = LLMRuntime(
            model_path=self.model_path,
            llama_dir=self.root,
            api_url="http://127.0.0.1:8080/v1/chat/completions",
        )
        self.patches = [
            patch.multiple(
                "experiments.phase1.llm_adapter",
                LLM_INPUT_SHA256=self.input_hash,
                LLM_INPUT_SIZE_BYTES=len(self.input_bytes),
            ),
            patch.multiple(
                "experiments.phase1.llm_preflight",
                LLM_INPUT_SHA256=self.input_hash,
                LLM_INPUT_SIZE_BYTES=len(self.input_bytes),
                LLM_MODEL_SHA256=self.model_hash,
                LLM_MODEL_SIZE_BYTES=len(self.model_bytes),
                LLM_EXPECTED_SERVED_MODEL_ID="qwen-test.gguf",
            ),
        ]
        for value in self.patches:
            value.start()
            self.addCleanup(value.stop)

    def runtime_status(self) -> dict[str, object]:
        return {
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
        }

    def test_runtime_probe_records_identity_without_private_paths(self) -> None:
        model_path = self.model_path.resolve().as_posix()

        def snapshotter(command, **_kwargs):
            if command[0] == "git":
                return {"returncode": 0, "output": "b123456", "error_code": None}
            return {
                "returncode": 0,
                "output": (
                    "123 ./llama-server "
                    f"-m {model_path} --host 127.0.0.1 --port 8080 "
                    "--n-gpu-layers 10 --ctx-size 1024 --threads 4 "
                    "--parallel 1 --cache-ram 0"
                ),
                "error_code": None,
            }

        status = probe_llm_runtime(
            runtime_loader=lambda: self.runtime,
            snapshotter=snapshotter,
            hasher=lambda _path: self.model_hash,
            query=lambda _url: {"data": [{"id": "qwen-test.gguf"}]},
            listener_probe=lambda _port: {
                "addresses": ["127.0.0.1"],
                "loopback_only": True,
                "error_code": None,
            },
        )
        self.assertEqual(status["model_sha256"], self.model_hash)
        self.assertTrue(status["server_arguments_match"])
        self.assertTrue(status["server_model_path_matches"])
        self.assertTrue(status["expected_model_present"])
        serialized = json.dumps(status)
        self.assertNotIn(str(self.root), serialized)
        self.assertNotIn(model_path, serialized)

    def test_complete_preflight_is_eligible_and_independently_valid(self) -> None:
        self.assertEqual(llm_request_contract(), frozen_llm_request_contract())
        preflight = build_llm_preflight(
            REPO_ROOT,
            input_payload=fixed_llm_payload(self.input_path),
            base_preflight=base_preflight(),
            runtime_status=self.runtime_status(),
        )
        self.assertTrue(preflight["eligible"])
        self.assertEqual(llm_preflight_errors(preflight), [])
        self.assertTrue(all(check["passed"] for check in preflight["checks"]))

    def test_preflight_rejects_phase0_request_contract_drift(self) -> None:
        runtime = self.runtime_status()
        runtime["request_contract"] = {
            **llm_request_contract(),
            "temperature": 0.5,
        }
        preflight = build_llm_preflight(
            REPO_ROOT,
            input_payload=fixed_llm_payload(self.input_path),
            base_preflight=base_preflight(),
            runtime_status=runtime,
        )
        failed = {check["name"] for check in preflight["checks"] if not check["passed"]}
        self.assertEqual(failed, {"llm_request_contract_frozen"})

    def test_validator_rejects_required_or_input_identity_tampering(self) -> None:
        preflight = build_llm_preflight(
            REPO_ROOT,
            input_payload=fixed_llm_payload(self.input_path),
            base_preflight=base_preflight(),
            runtime_status=self.runtime_status(),
        )
        preflight["checks"][0]["required"] = False
        preflight["input"]["sha256"] = "0" * 64
        preflight["eligible"] = False
        errors = llm_preflight_errors(preflight)
        self.assertTrue(any("not required" in error for error in errors))
        self.assertTrue(any("input identity" in error for error in errors))

    def test_preflight_rejects_nonlocal_or_nonloopback_service(self) -> None:
        runtime = self.runtime_status()
        runtime["endpoint_local"] = False
        runtime["listener_loopback_only"] = False
        preflight = build_llm_preflight(
            REPO_ROOT,
            input_payload=fixed_llm_payload(self.input_path),
            base_preflight=base_preflight(),
            runtime_status=runtime,
        )
        failed = {check["name"] for check in preflight["checks"] if not check["passed"]}
        self.assertEqual(
            failed,
            {"llama_endpoint_local", "llama_listener_loopback_only"},
        )

    def test_preflight_rejects_model_process_or_argument_mismatch(self) -> None:
        runtime = self.runtime_status()
        runtime["model_sha256"] = "0" * 64
        runtime["server_process_count"] = 2
        runtime["server_arguments_match"] = False
        preflight = build_llm_preflight(
            REPO_ROOT,
            input_payload=fixed_llm_payload(self.input_path),
            base_preflight=base_preflight(),
            runtime_status=runtime,
        )
        failed = {check["name"] for check in preflight["checks"] if not check["passed"]}
        self.assertEqual(
            failed,
            {
                "llama_model_identity",
                "llama_server_process_unique",
                "llama_server_arguments_frozen",
            },
        )

    def test_validator_rejects_eligibility_tampering(self) -> None:
        runtime = self.runtime_status()
        runtime["expected_model_present"] = False
        preflight = build_llm_preflight(
            REPO_ROOT,
            input_payload=fixed_llm_payload(self.input_path),
            base_preflight=base_preflight(),
            runtime_status=runtime,
        )
        preflight["eligible"] = True
        errors = llm_preflight_errors(preflight)
        self.assertTrue(
            any("eligible flag is inconsistent" in error for error in errors)
        )


if __name__ == "__main__":
    unittest.main()
