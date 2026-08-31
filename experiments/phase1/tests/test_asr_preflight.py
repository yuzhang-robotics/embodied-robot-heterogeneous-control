from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from experiments.phase1.asr_adapter import ASRRuntime, fixed_asr_payload
from experiments.phase1.asr_preflight import (
    asr_preflight_errors,
    build_asr_preflight,
    probe_asr_runtime,
)
from experiments.phase1.jetson_preflight import build_jetson_preflight
from experiments.phase1.tests.test_jetson_pilot import clean_environment


REPO_ROOT = Path(__file__).resolve().parents[3]


def base_preflight() -> dict[str, object]:
    return build_jetson_preflight(
        REPO_ROOT,
        environment=clean_environment(),
        tegrastats_available=True,
        loaded_modules={"experiments.phase1.asr_preflight"},
    )


class ASRPreflightTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.input_path = self.root / "fixed.wav"
        self.input_bytes = b"phase1-asr-preflight-audio"
        self.input_path.write_bytes(self.input_bytes)
        self.model_path = self.root / "ggml-small.bin"
        self.model_bytes = b"phase1-asr-preflight-model"
        self.model_path.write_bytes(self.model_bytes)
        self.input_hash = hashlib.sha256(self.input_bytes).hexdigest()
        self.model_hash = hashlib.sha256(self.model_bytes).hexdigest()
        self.runtime = ASRRuntime(
            whisper_dir=self.root,
            whisper_binary=Path(sys.executable).resolve(),
            whisper_model=self.model_path,
        )
        self.patches = [
            patch.multiple(
                "experiments.phase1.asr_adapter",
                ASR_INPUT_SHA256=self.input_hash,
                ASR_INPUT_SIZE_BYTES=len(self.input_bytes),
            ),
            patch.multiple(
                "experiments.phase1.asr_preflight",
                ASR_INPUT_SHA256=self.input_hash,
                ASR_INPUT_SIZE_BYTES=len(self.input_bytes),
                ASR_MODEL_SHA256=self.model_hash,
                ASR_MODEL_SIZE_BYTES=len(self.model_bytes),
            ),
        ]
        for value in self.patches:
            value.start()
            self.addCleanup(value.stop)

    def runtime_status(self) -> dict[str, object]:
        return {
            "binary_available": True,
            "model_size_bytes": len(self.model_bytes),
            "model_sha256": self.model_hash,
            "source_version": "v1.8.4-326-gafa2ea54",
            "arguments": ["-l", "zh", "-otxt", "-nt", "-np", "-bs", "1", "-bo", "1"],
            "process_running": False,
            "error_code": None,
        }

    def test_runtime_probe_records_identity_without_private_paths(self) -> None:
        def snapshotter(command, **_kwargs):
            if command[0] == "git":
                return {
                    "returncode": 0,
                    "output": "v1.8.4-326-gafa2ea54",
                    "error_code": None,
                }
            return {"returncode": 1, "output": "", "error_code": None}

        status = probe_asr_runtime(
            runtime_loader=lambda: self.runtime,
            snapshotter=snapshotter,
            hasher=lambda _path: self.model_hash,
        )
        self.assertTrue(status["binary_available"])
        self.assertEqual(status["model_sha256"], self.model_hash)
        self.assertFalse(status["process_running"])
        serialized = json.dumps(status)
        self.assertNotIn(str(self.root), serialized)
        self.assertNotIn(str(self.input_path), serialized)

    def test_complete_preflight_is_eligible_and_independently_valid(self) -> None:
        preflight = build_asr_preflight(
            REPO_ROOT,
            input_payload=fixed_asr_payload(self.input_path),
            base_preflight=base_preflight(),
            runtime_status=self.runtime_status(),
        )
        self.assertTrue(preflight["eligible"])
        self.assertEqual(asr_preflight_errors(preflight), [])
        self.assertTrue(all(check["passed"] for check in preflight["checks"]))

    def test_preflight_rejects_existing_whisper_process(self) -> None:
        runtime = self.runtime_status()
        runtime["process_running"] = True
        preflight = build_asr_preflight(
            REPO_ROOT,
            input_payload=fixed_asr_payload(self.input_path),
            base_preflight=base_preflight(),
            runtime_status=runtime,
        )
        errors = asr_preflight_errors(preflight)
        self.assertFalse(preflight["eligible"])
        self.assertTrue(any("whisper_process_absent" in error for error in errors))

    def test_preflight_rejects_model_or_source_identity_mismatch(self) -> None:
        runtime = self.runtime_status()
        runtime["model_sha256"] = "0" * 64
        runtime["source_version"] = "different"
        preflight = build_asr_preflight(
            REPO_ROOT,
            input_payload=fixed_asr_payload(self.input_path),
            base_preflight=base_preflight(),
            runtime_status=runtime,
        )
        failed = {check["name"] for check in preflight["checks"] if not check["passed"]}
        self.assertEqual(
            failed,
            {"whisper_model_identity", "whisper_source_version"},
        )

    def test_validator_rejects_eligibility_tampering(self) -> None:
        runtime = self.runtime_status()
        runtime["process_running"] = True
        preflight = build_asr_preflight(
            REPO_ROOT,
            input_payload=fixed_asr_payload(self.input_path),
            base_preflight=base_preflight(),
            runtime_status=runtime,
        )
        preflight["eligible"] = True
        errors = asr_preflight_errors(preflight)
        self.assertTrue(any("eligibility is inconsistent" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
