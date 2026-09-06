from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from experiments.phase1.analyze_vlm_residency import (
    EXPECTED_STAGE_ORDER,
    QWEN_REQUEST_TIMEOUT_S,
    VLM_RESIDENCY_ANALYSIS_KIND,
    analyze_vlm_residency_diagnostic,
    main,
)
from experiments.phase1.tests.test_vlm_process_pilot_analysis import (
    make_process_session,
)
from experiments.phase1.vlm_process_adapter import PROCESS_PROTOCOL_VERSION


COLLECTION_ARCHIVE_SHA256 = "3" * 64
LOG_ARCHIVE_SHA256 = "4" * 64


def _write_llama_log(path: Path, *, cancelled: bool = False) -> None:
    cancellation = "0.02.500.000 I srv cancel task: id_task = 2\n" if cancelled else ""
    path.write_text(
        "".join(
            (
                "0.00.000.000 I srv update_slots: all slots are idle\n",
                "0.01.000.000 I slot launch_slot_: id 0 | task 1 | processing task\n",
                "0.02.000.000 I slot      release: id 0 | task 1 | stop processing\n",
                "0.02.001.000 I srv update_slots: all slots are idle\n",
                "0.02.100.000 I slot launch_slot_: id 0 | task 2 | processing task\n",
                cancellation,
                "0.03.000.000 I slot      release: id 0 | task 2 | stop processing\n",
                "0.03.001.000 I srv update_slots: all slots are idle\n",
            )
        ),
        encoding="utf-8",
    )


def _bind_residency_contract(session: Path) -> None:
    for manifest_path in session.glob("vlm_process_*/*/manifest.json"):
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["workload_contract"] = {
            "qwen_rewrite": {"request_timeout_s": QWEN_REQUEST_TIMEOUT_S},
            "unload_before_qwen": True,
            "cleanup_unload_on_failure": True,
        }
        manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")
        summary_path = manifest_path.with_name("summary.json")
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        durations = summary["adapter"]["stage_durations_ns"]
        statuses = summary["adapter"]["stage_status"]
        summary["adapter"]["stage_durations_ns"] = {
            name: durations[name] for name in EXPECTED_STAGE_ORDER
        }
        summary["adapter"]["stage_status"] = {
            name: statuses[name] for name in EXPECTED_STAGE_ORDER
        }
        summary["adapter"]["stage_error_codes"] = {}
        summary_path.write_text(json.dumps(summary) + "\n", encoding="utf-8")


class VLMResidencyAnalysisTests(unittest.TestCase):
    def test_analysis_binds_contract_log_and_conservative_decision(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            session = make_process_session(root)
            _bind_residency_contract(session)
            log = root / "llama.log"
            _write_llama_log(log)
            with patch(
                "experiments.phase1.analyze_vlm_pilot.validate_vlm_slice_dir",
                return_value=[],
            ):
                analysis = analyze_vlm_residency_diagnostic(
                    session,
                    llama_log=log,
                    source_archive_sha256=COLLECTION_ARCHIVE_SHA256,
                    llama_log_archive_sha256=LOG_ARCHIVE_SHA256,
                )

        self.assertEqual(analysis["analysis_kind"], VLM_RESIDENCY_ANALYSIS_KIND)
        self.assertEqual(
            analysis["contract"]["stage_order"], list(EXPECTED_STAGE_ORDER)
        )
        self.assertEqual(
            analysis["contract"]["process_protocol_version"],
            PROCESS_PROTOCOL_VERSION,
        )
        self.assertTrue(analysis["validation"]["residency_contract_verified"])
        self.assertTrue(analysis["validation"]["qwen_completed_within_request_timeout"])
        self.assertEqual(
            analysis["validation"]["llama_server"]["cancellation_record_count"],
            0,
        )
        self.assertEqual(analysis["decision"]["retain_qwen_request_timeout_s"], 30)
        self.assertFalse(
            analysis["claim_boundary"]["residency_order_causality_established"]
        )
        self.assertFalse(analysis["claim_boundary"]["performance_comparison_permitted"])

    def test_analysis_rejects_old_residency_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            session = make_process_session(root)
            log = root / "llama.log"
            _write_llama_log(log)
            with self.assertRaisesRegex(ValueError, "residency-order contract"):
                analyze_vlm_residency_diagnostic(
                    session,
                    llama_log=log,
                    source_archive_sha256=COLLECTION_ARCHIVE_SHA256,
                    llama_log_archive_sha256=LOG_ARCHIVE_SHA256,
                )

    def test_analysis_rejects_llama_cancellation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            session = make_process_session(root)
            _bind_residency_contract(session)
            log = root / "llama.log"
            _write_llama_log(log, cancelled=True)
            with patch(
                "experiments.phase1.analyze_vlm_pilot.validate_vlm_slice_dir",
                return_value=[],
            ):
                with self.assertRaisesRegex(ValueError, "unexpected cancellation"):
                    analyze_vlm_residency_diagnostic(
                        session,
                        llama_log=log,
                        source_archive_sha256=COLLECTION_ARCHIVE_SHA256,
                        llama_log_archive_sha256=LOG_ARCHIVE_SHA256,
                    )

    def test_cli_is_deterministic_private_and_claim_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            session = make_process_session(root / "private-source")
            _bind_residency_contract(session)
            log = root / "private-server.log"
            _write_llama_log(log)
            json_output = root / "published" / "analysis.json"
            markdown_output = root / "published" / "README.md"
            arguments = [
                str(session),
                "--llama-log",
                str(log),
                "--source-archive-sha256",
                COLLECTION_ARCHIVE_SHA256,
                "--llama-log-archive-sha256",
                LOG_ARCHIVE_SHA256,
                "--json-output",
                str(json_output),
                "--markdown-output",
                str(markdown_output),
            ]
            with patch(
                "experiments.phase1.analyze_vlm_pilot.validate_vlm_slice_dir",
                return_value=[],
            ):
                first_result = main(arguments)
                first_json = json_output.read_text(encoding="utf-8")
                first_markdown = markdown_output.read_text(encoding="utf-8")
                second_result = main(arguments)
                second_json = json_output.read_text(encoding="utf-8")
                second_markdown = markdown_output.read_text(encoding="utf-8")

        self.assertEqual(first_result, 0)
        self.assertEqual(second_result, 0)
        self.assertEqual(first_json, second_json)
        self.assertEqual(first_markdown, second_markdown)
        self.assertNotIn("private-source", first_json)
        self.assertNotIn("private-server.log", first_json)
        self.assertIn("Residency-order Diagnostic", first_markdown)
        self.assertIn("retaining the 30 s Qwen timeout", first_markdown)
        self.assertIn("residency_order_causality_established=False", first_markdown)
        self.assertNotIn("performance superiority was established", first_markdown)


if __name__ == "__main__":
    unittest.main()
