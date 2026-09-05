from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from experiments.phase1.analyze_formal_failure import (
    analyze_failed_formal_attempt,
    main,
    render_markdown,
)
from experiments.phase1.analyze_formal_runs import analyze_formal_collection
from experiments.phase1.tests.test_formal_analysis import (
    build_collection,
    read_json,
    refresh_manifest,
    write_event_trace,
    write_json,
)


SOURCE_ARCHIVE_SHA256 = "3" * 64
LOG_ARCHIVE_SHA256 = "4" * 64


def _write_llama_log(path: Path) -> None:
    path.write_text(
        "\n".join(
            (
                "0.00.000.000 I slot launch_slot_: id 0 | task 9 | "
                "processing task, is_child = 0",
                "0.30.100.000 W srv          stop: cancel task, id_task = 9",
                "0.30.200.000 I slot      release: id 0 | task 9 | "
                "stop processing: n_tokens = 1, truncated = 0",
                "0.30.201.000 I srv  update_slots: all slots are idle",
            )
        )
        + "\n",
        encoding="utf-8",
    )


def _failed_collection(root: Path) -> tuple[Path, Path]:
    collection = build_collection(root)
    session_dir = collection / "session-01-attempt-01"
    for extra_session in sorted(collection.glob("session-0[2-5]-attempt-01")):
        shutil.rmtree(extra_session)

    ledger_path = session_dir / "ledger.jsonl"
    ledger = [
        json.loads(line)
        for line in ledger_path.read_text(encoding="utf-8").splitlines()
    ][:37]
    ledger_path.write_text(
        "".join(json.dumps(item, separators=(",", ":")) + "\n" for item in ledger),
        encoding="utf-8",
    )
    shutil.rmtree(session_dir / "idle" / "post_measurement")
    for run_dir in sorted((session_dir / "measured").iterdir()):
        if int(run_dir.name[:3]) > 18:
            shutil.rmtree(run_dir)

    failed_dir = next((session_dir / "measured").glob("018-vlm-formal_async"))
    failed = read_json(failed_dir / "run.json")
    durations = {
        "input_verify_before": 1_000_000,
        "module_import": 2_000_000,
        "moondream_inference": 60_000_000,
        "qwen_rewrite": 30_100_000_000,
        "argos_fallback": 20_000_000,
        "output_normalization": 1_000_000,
        "model_unload": 10_000_000,
        "input_verify_after": 1_000_000,
    }
    adapter = failed["adapter"]
    adapter["stage_durations_ns"] = durations
    adapter["stage_status"] = {
        name: "error" if name == "qwen_rewrite" else "ok" for name in durations
    }
    adapter["translation_route"] = "argos"
    adapter["finished_monotonic_ns"] = adapter["started_monotonic_ns"] + sum(
        durations.values()
    )
    adapter["duration_ns"] = (
        adapter["finished_monotonic_ns"] - adapter["started_monotonic_ns"]
    )
    result = failed["result"]
    result["finished_monotonic_ns"] = adapter["finished_monotonic_ns"]
    result["deadline_monotonic_ns"] = adapter["finished_monotonic_ns"] + 1_000_000
    failed["process"].update(
        {
            "protocol_version": "0.1.0",
            "joined_monotonic_ns": adapter["finished_monotonic_ns"],
            "terminate_requested": False,
        }
    )
    failed["finished_monotonic_ns"] = adapter["finished_monotonic_ns"] + 1_000_000
    failed["duration_ns"] = (
        failed["finished_monotonic_ns"] - failed["started_monotonic_ns"]
    )
    failed["status"] = "failed"
    failed["valid"] = False
    for gate in failed["gates"]:
        if gate["name"] == "translation_route_verified":
            gate["passed"] = False
            gate["observed"] = "argos"
    write_json(failed_dir / "run.json", failed)
    write_event_trace(failed_dir, failed)

    manifest_path = session_dir / "manifest.json"
    manifest = read_json(manifest_path)
    manifest.update(
        {
            "status": "aborted",
            "failure_class": "system_under_test",
            "failure_code": "formalsessionerror",
            "formal_evidence_eligible": False,
            "completed_entries": 17,
        }
    )
    write_json(manifest_path, manifest)
    refresh_manifest(session_dir)

    log_path = root / "llama.log"
    _write_llama_log(log_path)
    return collection, log_path


class FormalFailureAnalysisTests(unittest.TestCase):
    def test_failed_attempt_is_reconstructed_without_confirmatory_claim(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            collection, log_path = _failed_collection(Path(temp_dir))
            analysis = analyze_failed_formal_attempt(
                collection,
                llama_log=log_path,
                source_archive_sha256=SOURCE_ARCHIVE_SHA256,
                llama_log_archive_sha256=LOG_ARCHIVE_SHA256,
            )
            with self.assertRaisesRegex(ValueError, "attempt inventory is incomplete"):
                analyze_formal_collection(collection)

        self.assertEqual(analysis["integrity"]["completed_run_records_verified"], 17)
        self.assertEqual(analysis["integrity"]["failed_run_records_verified"], 1)
        self.assertEqual(
            analysis["failure"]["failed_gates"],
            ["translation_route_verified"],
        )
        self.assertTrue(analysis["failure"]["timeout_boundary_consistent"])
        self.assertTrue(analysis["interpretation"]["residency_order_confound_present"])
        self.assertFalse(
            analysis["interpretation"]["residency_order_causality_established"]
        )
        self.assertFalse(analysis["decision"]["formal_claim_permitted"])
        self.assertFalse(analysis["decision"]["v2_replacement_permitted"])

    def test_cli_outputs_are_deterministic_and_privacy_preserving(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            collection, log_path = _failed_collection(root)
            json_output = root / "published" / "analysis.json"
            markdown_output = root / "published" / "README.md"
            arguments = [
                str(collection),
                "--llama-log",
                str(log_path),
                "--source-archive-sha256",
                SOURCE_ARCHIVE_SHA256,
                "--llama-log-archive-sha256",
                LOG_ARCHIVE_SHA256,
                "--json-output",
                str(json_output),
                "--markdown-output",
                str(markdown_output),
            ]
            self.assertEqual(main(arguments), 0)
            first_json = json_output.read_text(encoding="utf-8")
            first_markdown = markdown_output.read_text(encoding="utf-8")
            self.assertEqual(main(arguments), 0)
            second_json = json_output.read_text(encoding="utf-8")
            second_markdown = markdown_output.read_text(encoding="utf-8")

        self.assertEqual(first_json, second_json)
        self.assertEqual(first_markdown, second_markdown)
        self.assertNotIn(str(collection), first_json)
        self.assertNotIn(str(log_path), first_json)
        self.assertNotIn("processing task", first_json)
        self.assertIn("# Phase 1 G6 v2 Failed Formal Attempt", first_markdown)
        self.assertIn("not confirmatory evidence", first_markdown)

    def test_tampered_failed_run_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            collection, log_path = _failed_collection(Path(temp_dir))
            run_path = next(collection.glob("session-*/measured/018-*/run.json"))
            run_path.write_text("{}\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "artifact identity mismatch"):
                analyze_failed_formal_attempt(
                    collection,
                    llama_log=log_path,
                    source_archive_sha256=SOURCE_ARCHIVE_SHA256,
                    llama_log_archive_sha256=LOG_ARCHIVE_SHA256,
                )

    def test_markdown_keeps_the_timeout_claim_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            collection, log_path = _failed_collection(Path(temp_dir))
            analysis = analyze_failed_formal_attempt(
                collection,
                llama_log=log_path,
                source_archive_sha256=SOURCE_ARCHIVE_SHA256,
                llama_log_archive_sha256=LOG_ARCHIVE_SHA256,
            )

        report = render_markdown(analysis)
        self.assertIn("does not by itself prove", report)
        self.assertIn("will not be rerun, replaced", report)


if __name__ == "__main__":
    unittest.main()
