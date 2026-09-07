from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from experiments.phase1.analyze_formal_runs import analyze_formal_collection
from experiments.phase1.analyze_formal_v3_failure import (
    analyze_v3_failed_formal_attempt,
    main,
    render_markdown,
)
from experiments.phase1.formal_protocol import (
    FORMAL_V3_PROTOCOL_ID,
    FORMAL_V3_PROTOCOL_PATH,
    FORMAL_V3_PROTOCOL_SHA256,
    load_formal_protocol,
)
from experiments.phase1.tests.test_formal_analysis import (
    build_collection,
    read_json,
    refresh_manifest,
    write_event_trace,
    write_json,
)


SOURCE_ARCHIVE_SHA256 = "5" * 64
LOG_ARCHIVE_SHA256 = "6" * 64


def _stamp(milliseconds: float) -> str:
    minutes = int(milliseconds // 60_000)
    remaining = milliseconds - minutes * 60_000
    seconds = int(remaining // 1_000)
    remaining -= seconds * 1_000
    millis = int(remaining)
    micros = round((remaining - millis) * 1_000)
    return f"{minutes}.{seconds:02d}.{millis:03d}.{micros:03d}"


def _write_llama_log(path: Path) -> None:
    requests = (
        (0, 103, 1_417.43, 26, 1_886.28, 129, 3_303.70),
        (27, 161, 23_342.77, 32, 2_483.55, 193, 25_826.32),
        (60, 100, 629.20, 28, 1_871.62, 128, 2_500.82),
        (89, 1, 75.73, 26, 1_835.24, 27, 1_910.96),
        (116, 171, 27_244.57, 37, 2_872.56, 208, 30_117.12),
    )
    cursor = 0.0
    lines: list[str] = []
    for (
        task,
        prompt_tokens,
        prompt_ms,
        generation_tokens,
        generation_ms,
        total_tokens,
        total_ms,
    ) in requests:
        lines.extend(
            (
                f"{_stamp(cursor)} I slot launch_slot_: id 0 | task {task} | "
                "processing task, is_child = 0",
                f"{_stamp(cursor + 1)} I slot print_timing: prompt eval time = "
                f"{prompt_ms:.2f} ms / {prompt_tokens} tokens",
                f"{_stamp(cursor + 2)} I slot print_timing: eval time = "
                f"{generation_ms:.2f} ms / {generation_tokens} runs",
                f"{_stamp(cursor + total_ms)} I slot print_timing: total time = "
                f"{total_ms:.2f} ms / {total_tokens} tokens",
                f"{_stamp(cursor + total_ms + 1)} I slot      release: id 0 | "
                f"task {task} | stop processing: n_tokens = 1, truncated = 0",
                f"{_stamp(cursor + total_ms + 2)} I srv update_slots: "
                "all slots are idle",
            )
        )
        cursor += total_ms + 100
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _failed_collection(root: Path) -> tuple[Path, Path]:
    collection = build_collection(
        root,
        protocol_override=load_formal_protocol(FORMAL_V3_PROTOCOL_PATH),
        protocol_id=FORMAL_V3_PROTOCOL_ID,
        protocol_sha256_value=FORMAL_V3_PROTOCOL_SHA256,
    )
    session_dir = collection / "session-01-attempt-01"
    for extra_session in sorted(collection.glob("session-0[2-5]-attempt-01")):
        shutil.rmtree(extra_session)

    ledger_path = session_dir / "ledger.jsonl"
    ledger = [
        json.loads(line)
        for line in ledger_path.read_text(encoding="utf-8").splitlines()
    ][:21]
    ledger_path.write_text(
        "".join(json.dumps(item, separators=(",", ":")) + "\n" for item in ledger),
        encoding="utf-8",
    )
    shutil.rmtree(session_dir / "idle" / "post_measurement")
    for run_dir in sorted((session_dir / "measured").iterdir()):
        if int(run_dir.name[:3]) > 10:
            shutil.rmtree(run_dir)

    failed_dir = next((session_dir / "measured").glob("010-vlm-formal_sync"))
    failed = read_json(failed_dir / "run.json")
    durations = {
        "input_verify_before": 1_000_000,
        "module_import": 16_000_000_000,
        "moondream_inference": 55_000_000_000,
        "model_unload": 300_000_000,
        "qwen_rewrite": 30_031_008_000,
        "argos_fallback": 53_000_000_000,
        "output_normalization": 1_000_000,
        "input_verify_after": 1_000_000,
    }
    adapter = failed["adapter"]
    adapter["stage_durations_ns"] = durations
    adapter["stage_status"] = {
        name: "error" if name == "qwen_rewrite" else "ok" for name in durations
    }
    adapter["stage_error_codes"] = {"qwen_rewrite": "timeouterror"}
    adapter["translation_route"] = "argos"
    adapter["model_residency"] = {
        "unload_requested": True,
        "unload_confirmed": None,
    }
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
            "protocol_version": "0.2.0",
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
        elif gate["name"] == "residency_contract_verified":
            gate["passed"] = False
            gate["observed"] = {
                "process_protocol_version": "0.2.0",
                "completed_stages": sorted(durations),
                "required_stage_order": [
                    "input_verify_before",
                    "module_import",
                    "moondream_inference",
                    "model_unload",
                    "qwen_rewrite",
                    "output_normalization",
                    "input_verify_after",
                ],
                "stage_error_codes": {"qwen_rewrite": "timeouterror"},
            }
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
            "completed_entries": 9,
        }
    )
    write_json(manifest_path, manifest)
    refresh_manifest(session_dir)

    log_path = root / "llama.log"
    _write_llama_log(log_path)
    return collection, log_path


class FormalV3FailureAnalysisTests(unittest.TestCase):
    def test_failed_attempt_is_reconstructed_as_negative_result(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            collection, log_path = _failed_collection(Path(temp_dir))
            analysis = analyze_v3_failed_formal_attempt(
                collection,
                llama_log=log_path,
                source_archive_sha256=SOURCE_ARCHIVE_SHA256,
                llama_log_archive_sha256=LOG_ARCHIVE_SHA256,
            )
            with self.assertRaisesRegex(
                ValueError, "formal collection protocol does not match activated G6"
            ):
                analyze_formal_collection(collection)

        self.assertEqual(analysis["integrity"]["completed_run_records_verified"], 9)
        self.assertEqual(analysis["integrity"]["passed_gate_count"], 97)
        self.assertEqual(
            analysis["failure"]["failed_gates"],
            ["residency_contract_verified", "translation_route_verified"],
        )
        self.assertEqual(analysis["failure"]["qwen_requests"]["prompt_token_delta"], 10)
        self.assertEqual(
            analysis["failure"]["qwen_requests"]["server_total_delta_ms"],
            4_290.8,
        )
        self.assertEqual(
            analysis["failure"]["qwen_requests"]["failed_server_over_timeout_ms"],
            117.12,
        )
        self.assertEqual(
            analysis["failure"]["qwen_requests"]["failed_server_minus_client_ms"],
            86.112,
        )
        self.assertTrue(analysis["interpretation"]["timeout_mechanism_established"])
        self.assertFalse(analysis["decision"]["formal_claim_permitted"])
        self.assertFalse(analysis["decision"]["v3_replacement_permitted"])

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
        self.assertIn("# Phase 1 G6 v3 Failed Formal Attempt", first_markdown)
        self.assertIn("negative Phase 1 result", first_markdown)

    def test_tampered_failed_run_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            collection, log_path = _failed_collection(Path(temp_dir))
            run_path = next(collection.glob("session-*/measured/010-*/run.json"))
            run_path.write_text("{}\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "artifact identity mismatch"):
                analyze_v3_failed_formal_attempt(
                    collection,
                    llama_log=log_path,
                    source_archive_sha256=SOURCE_ARCHIVE_SHA256,
                    llama_log_archive_sha256=LOG_ARCHIVE_SHA256,
                )

    def test_cancelled_llama_request_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            collection, log_path = _failed_collection(Path(temp_dir))
            lines = log_path.read_text(encoding="utf-8").splitlines()
            release_index = next(
                index
                for index, line in enumerate(lines)
                if "task 116 | stop processing" in line
            )
            lines.insert(
                release_index,
                "2.00.000.000 W srv stop: cancel task, id_task = 116",
            )
            log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "unexpected cancellation"):
                analyze_v3_failed_formal_attempt(
                    collection,
                    llama_log=log_path,
                    source_archive_sha256=SOURCE_ARCHIVE_SHA256,
                    llama_log_archive_sha256=LOG_ARCHIVE_SHA256,
                )

    def test_nonincreasing_failed_request_token_counts_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            collection, log_path = _failed_collection(Path(temp_dir))
            log = log_path.read_text(encoding="utf-8")
            log_path.write_text(
                log.replace("2872.56 ms / 37 runs", "2872.56 ms / 32 runs"),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "do not exceed the warm-up"):
                analyze_v3_failed_formal_attempt(
                    collection,
                    llama_log=log_path,
                    source_archive_sha256=SOURCE_ARCHIVE_SHA256,
                    llama_log_archive_sha256=LOG_ARCHIVE_SHA256,
                )

    def test_markdown_keeps_causal_claims_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            collection, log_path = _failed_collection(Path(temp_dir))
            analysis = analyze_v3_failed_formal_attempt(
                collection,
                llama_log=log_path,
                source_archive_sha256=SOURCE_ARCHIVE_SHA256,
                llama_log_archive_sha256=LOG_ARCHIVE_SHA256,
            )

        report = render_markdown(analysis)
        self.assertIn("do not establish", report)
        self.assertIn("not be rerun, replaced", report)
        self.assertNotIn(str(collection), report)


if __name__ == "__main__":
    unittest.main()
