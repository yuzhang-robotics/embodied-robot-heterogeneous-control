from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from experiments.phase1.analyze_vlm_timeout_diagnostic import (
    DIAGNOSTIC_DESIGN_ROLE,
    EXPECTED_REQUEST_CONTRACT,
    VLM_TIMEOUT_DIAGNOSTIC_ANALYSIS_KIND,
    _refuse_output_inside_diagnostic,
    analyze_vlm_timeout_diagnostic,
    render_markdown,
)
from experiments.phase1.vlm_adapter import (
    C100_INPUT_SHA256,
    C100_INPUT_SIZE_BYTES,
)


ARCHIVE_SHA256 = "a" * 64
DESCRIPTION_SHA256 = "b" * 64


def _record(repetition: int, qwen_ms: float, output_sha: str) -> dict[str, object]:
    minute = repetition - 1
    return {
        "completed_at": f"2026-09-06T08:{minute:02d}:01Z",
        "description": {
            "bytes": 208,
            "characters": 208,
            "raw_text_recorded": False,
            "sha256": DESCRIPTION_SHA256,
        },
        "error_code": None,
        "error_stage": None,
        "model_unload": {
            "confirmed": True,
            "duration_ms": 500.0 + repetition,
            "requested": True,
        },
        "moondream": {
            "client_ms": 20_000.0,
            "eval_count": 44,
            "generation_ms": 1_000.0,
            "load_ms": 18_000.0,
            "prompt_eval_count": 742,
            "prompt_eval_ms": 500.0,
            "total_ms": 19_900.0,
        },
        "qwen": {
            "client_ms": qwen_ms,
            "output": {
                "bytes": 144,
                "characters": 48,
                "raw_text_recorded": False,
                "sha256": output_sha,
            },
            "usage": {
                "completion_tokens": 32,
                "prompt_tokens": 164,
                "total_tokens": 196,
            },
        },
        "repetition": repetition,
        "started_at": f"2026-09-06T08:{minute:02d}:00Z",
        "status": "completed",
    }


def _tegrastats_line(second: int) -> str:
    return (
        f"09-06-2026 16:30:{second:02d} RAM 3000/7607MB (lfb 40x1MB) "
        "SWAP 600/3804MB (cached 3MB) CPU [0%@729,1%@729] "
        "GR3D_FREQ 20% cpu@49C gpu@50C tj@51C "
        "VDD_IN 5000mW/5000mW VDD_SOC 1000mW/1000mW"
    )


def _llama_log() -> str:
    lines: list[str] = []
    for index, task in enumerate((0, 33, 66)):
        prompt_tokens = 164 if index == 0 else 1
        lines.extend(
            [
                f"1.00.000.000 I slot launch_slot_: id 0 | task {task} | processing task",
                f"1.01.000.000 I slot print_timing: id 0 | task {task} | prompt eval time = 100.00 ms / {prompt_tokens} tokens",
                f"1.02.000.000 I slot print_timing: id 0 | task {task} |        eval time = 100.00 ms / 32 tokens",
                f"1.03.000.000 I slot print_timing: id 0 | task {task} |       total time = 200.00 ms / 196 tokens",
                f"1.04.000.000 I slot      release: id 0 | task {task} | stop processing",
                "1.05.000.000 I srv update_slots: all slots are idle",
            ]
        )
    return "\n".join(lines) + "\n"


class VLMTimeoutDiagnosticAnalysisTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "20260906T082627Z_phase1_vlm_timeout_diag"
        self.root.mkdir()
        records = [
            _record(1, 21_000.0, "c" * 64),
            _record(2, 11_000.0, "d" * 64),
            _record(3, 10_000.0, "d" * 64),
        ]
        result = {
            "created_at": "2026-09-06T08:03:01Z",
            "design_role": DIAGNOSTIC_DESIGN_ROLE,
            "diagnostic_schema_version": "0.1.0",
            "formal_evidence_eligible": False,
            "input": {
                "path_recorded": False,
                "sha256": C100_INPUT_SHA256,
                "size_bytes": C100_INPUT_SIZE_BYTES,
            },
            "raw_model_text_recorded": False,
            "raw_prompt_recorded": False,
            "records": records,
            "request_contract": EXPECTED_REQUEST_CONTRACT,
            "safety": {
                "motion_enabled": False,
                "motion_environment_value": "0",
                "uart_accessed": False,
            },
            "summary": {
                "all_completed": True,
                "all_unloads_confirmed": True,
                "completed_repetitions": 3,
                "description_identity_count": 1,
                "qwen_client_max_ms": 21_000.0,
                "qwen_client_min_ms": 10_000.0,
                "qwen_output_identity_count": 2,
                "qwen_requests_over_legacy_timeout": 0,
                "requested_repetitions": 3,
            },
        }
        (self.root / "results.json").write_text(
            json.dumps(result), encoding="utf-8"
        )
        (self.root / "llama-server.log").write_text(
            _llama_log(), encoding="utf-8"
        )
        (self.root / "tegrastats.log").write_text(
            "\n".join(_tegrastats_line(index) for index in range(3)) + "\n",
            encoding="utf-8",
        )
        (self.root / "llama-server.pid").write_text("123\n", encoding="utf-8")
        (self.root / "ollama-tags.json").write_text(
            json.dumps(
                {
                    "models": [
                        {"name": "moondream:latest", "digest": "e" * 64},
                        {"name": "qwen2.5vl:3b", "digest": "f" * 64},
                    ]
                }
            ),
            encoding="utf-8",
        )
        (self.root / "llama-models.json").write_text(
            json.dumps(
                {
                    "models": [
                        {"name": "qwen2.5-1.5b-instruct-q4_k_m.gguf"}
                    ]
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def analyze(self) -> dict[str, object]:
        return analyze_vlm_timeout_diagnostic(
            self.root,
            source_archive_sha256=ARCHIVE_SHA256,
        )

    def test_reconstructs_privacy_safe_diagnostic(self) -> None:
        analysis = self.analyze()

        self.assertEqual(analysis["analysis_kind"], VLM_TIMEOUT_DIAGNOSTIC_ANALYSIS_KIND)
        self.assertEqual(analysis["integrity"]["file_count"], 6)
        self.assertEqual(analysis["llama_server"]["request_count"], 3)
        self.assertEqual(analysis["resources"]["sample_count"], 3)
        self.assertTrue(analysis["decision"]["repair_contract_supported"])
        self.assertTrue(
            analysis["decision"]["actual_repaired_path_validation_required"]
        )
        self.assertFalse(analysis["decision"]["formal_collection_authorized"])
        serialized = json.dumps(analysis)
        self.assertNotIn(str(self.root), serialized)
        self.assertNotIn("private English description", serialized)

    def test_rendered_report_preserves_evidence_boundary(self) -> None:
        report = render_markdown(self.analyze())

        self.assertIn("descriptive repair evidence", report)
        self.assertIn("did not execute the modified repository adapter", report)
        self.assertIn("G6 v3 remains permanently closed", report)
        self.assertNotIn(str(self.root), report)

    def test_rejects_unconfirmed_unload(self) -> None:
        path = self.root / "results.json"
        result = json.loads(path.read_text(encoding="utf-8"))
        result["records"][1]["model_unload"]["confirmed"] = False
        path.write_text(json.dumps(result), encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "did not confirm unload"):
            self.analyze()

    def test_rejects_changed_qwen_request_size(self) -> None:
        path = self.root / "results.json"
        result = json.loads(path.read_text(encoding="utf-8"))
        result["records"][0]["qwen"]["usage"]["prompt_tokens"] = 165
        path.write_text(json.dumps(result), encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "request size differs"):
            self.analyze()

    def test_rejects_service_cancellation(self) -> None:
        path = self.root / "llama-server.log"
        path.write_text(path.read_text(encoding="utf-8") + "cancel task\n")

        with self.assertRaisesRegex(ValueError, "cancellation, timeout or error"):
            self.analyze()

    def test_rejects_telemetry_parse_error(self) -> None:
        (self.root / "tegrastats.log").write_text("invalid\n", encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "parse errors"):
            self.analyze()

    def test_refuses_output_inside_raw_diagnostic(self) -> None:
        with self.assertRaisesRegex(ValueError, "outside the raw diagnostic"):
            _refuse_output_inside_diagnostic(
                self.root / "analysis.json",
                self.root.resolve(),
            )


if __name__ == "__main__":
    unittest.main()
