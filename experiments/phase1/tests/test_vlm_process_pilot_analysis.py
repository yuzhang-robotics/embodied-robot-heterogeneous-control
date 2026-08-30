from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from experiments.phase1.analyze_vlm_pilot import (
    PROCESS_ANALYSIS_KIND,
    analyze_vlm_pilot_dir,
    main,
    render_markdown,
)
from experiments.phase1.tests.test_vlm_pilot_analysis import (
    ARCHIVE_SHA256,
    _write_run,
    make_session,
)


PROCESS_SESSION_ID = "20260830T122541Z_phase1_vlm_process_reaping"
PROCESS_ARCHIVE_SHA256 = "2" * 64


def _process_facts(condition: str) -> dict[str, object]:
    cancellation_forwarded = condition == "vlm_stale"
    return {
        "protocol_version": "0.1.0",
        "start_method": "spawn",
        "process_name": "phase1-vlm-process-worker",
        "process_id": 1234,
        "spawn_requested_monotonic_ns": 800_000_000,
        "child_started_monotonic_ns": 900_000_000,
        "inference_started_monotonic_ns": 1_300_000_000,
        "completion_received_monotonic_ns": 1_640_000_000,
        "joined_monotonic_ns": 1_650_000_000,
        "exit_code": 0,
        "cancellation_forwarded": cancellation_forwarded,
        "cancellation_forwarded_monotonic_ns": (
            1_400_000_000 if cancellation_forwarded else None
        ),
        "terminate_requested": False,
        "terminate_confirmed": False,
        "protocol_complete": True,
        "error_code": None,
    }


def _convert_run(run_dir: Path, condition: str) -> None:
    manifest_path = run_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["session_id"] = PROCESS_SESSION_ID
    manifest["adapter_isolation"] = "spawned_process"
    manifest["artifact_kind"] = "phase1_fixed_input_vlm_process_run"
    manifest["artifacts"]["process.json"] = {
        "sha256": "8" * 64,
        "size_bytes": 1,
    }
    manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")

    preflight_path = run_dir / "preflight.json"
    preflight = json.loads(preflight_path.read_text(encoding="utf-8"))
    preflight["vlm_preflight_schema_version"] = "0.2.0"
    for name in ("ollama", "qwen"):
        service = preflight["services"][name]
        service["listener_addresses"] = ["127.0.0.1"]
        service["listener_loopback_only"] = True
    preflight_path.write_text(json.dumps(preflight) + "\n", encoding="utf-8")

    facts = _process_facts(condition)
    scenario_path = run_dir / "scenario.json"
    scenario = json.loads(scenario_path.read_text(encoding="utf-8"))
    scenario["process"] = facts
    scenario["report"]["probe"].update(
        {
            "tick_count": 12,
            "skipped_releases": 0,
            "deadline_miss_count": 0,
            "max_lateness_ns": 500_000,
            "max_gap_ns": 100_500_000,
        }
    )
    scenario_path.write_text(json.dumps(scenario) + "\n", encoding="utf-8")

    events_path = run_dir / "events.jsonl"
    events = [
        json.loads(line)
        for line in events_path.read_text(encoding="utf-8").splitlines()
        if line
    ]
    events = [event for event in events if event["event"] != "probe.skipped"]
    events_path.write_text(
        "\n".join(json.dumps(event) for event in events) + "\n",
        encoding="utf-8",
    )

    process = {
        "vlm_process_summary_schema_version": "0.1.0",
        "adapter_isolation": "spawned_process",
        "condition": condition,
        "valid": True,
        "process": facts,
        "gates": [
            {"name": name, "passed": True}
            for name in (
                "spawned_process",
                "bounded_protocol",
                "process_reaped",
                "boundary_order",
                "cancellation_forwarding",
            )
        ],
    }
    (run_dir / "process.json").write_text(
        json.dumps(process) + "\n",
        encoding="utf-8",
    )


def make_process_session(root: Path) -> Path:
    session = root / PROCESS_SESSION_ID
    for condition in ("vlm_async", "vlm_stale"):
        run_dir = _write_run(session, condition)
        _convert_run(run_dir, condition)
        source = session / condition
        target = session / condition.replace("vlm_", "vlm_process_", 1)
        source.rename(target)
    return session


def write_thread_reference(root: Path) -> Path:
    thread_session = make_session(root / "thread")
    analysis = analyze_vlm_pilot_dir(
        thread_session,
        source_archive_sha256=ARCHIVE_SHA256,
    )
    path = root / "thread-analysis.json"
    path.write_text(json.dumps(analysis) + "\n", encoding="utf-8")
    return path


class VLMProcessPilotAnalysisTests(unittest.TestCase):
    def test_process_analysis_reconstructs_boundary_and_comparison(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with patch(
                "experiments.phase1.analyze_vlm_pilot.validate_vlm_slice_dir",
                return_value=[],
            ):
                reference = write_thread_reference(root)
                session = make_process_session(root)
                analysis = analyze_vlm_pilot_dir(
                    session,
                    source_archive_sha256=PROCESS_ARCHIVE_SHA256,
                    thread_reference_analysis=reference,
                )

        self.assertEqual(analysis["analysis_kind"], PROCESS_ANALYSIS_KIND)
        self.assertTrue(analysis["validation"]["all_process_gates_passed"])
        self.assertTrue(analysis["validation"]["process_boundary_correctness_observed"])
        self.assertTrue(analysis["validation"]["periodic_probe_continuity_observed"])
        self.assertTrue(
            analysis["thread_process_comparison"][
                "descriptive_mitigation_signal_observed"
            ]
        )
        self.assertEqual(
            analysis["thread_process_comparison"]["total_probe_skipped_releases"],
            {"thread": 4, "spawned_process": 0},
        )
        self.assertFalse(
            analysis["claim_boundary"]["timing_domain_isolation_claim_permitted"]
        )
        for run in analysis["runs"]:
            self.assertEqual(run["process"]["exit_code"], 0)
            self.assertFalse(run["process"]["terminate_requested"])
            self.assertTrue(all(run["process"]["gate_results"].values()))
            self.assertEqual(run["timing"]["probe"]["skipped_releases"], 0)

    def test_process_cli_is_deterministic_and_private(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with patch(
                "experiments.phase1.analyze_vlm_pilot.validate_vlm_slice_dir",
                return_value=[],
            ):
                reference = write_thread_reference(root)
                session = make_process_session(root / "private-source")
                json_path = root / "published" / "analysis.json"
                markdown_path = root / "published" / "README.md"
                arguments = [
                    str(session),
                    "--source-archive-sha256",
                    PROCESS_ARCHIVE_SHA256,
                    "--thread-reference-analysis",
                    str(reference),
                    "--json-output",
                    str(json_path),
                    "--markdown-output",
                    str(markdown_path),
                ]
                first_result = main(arguments)
                first_json = json_path.read_text(encoding="utf-8")
                first_markdown = markdown_path.read_text(encoding="utf-8")
                second_result = main(arguments)
                second_json = json_path.read_text(encoding="utf-8")
                second_markdown = markdown_path.read_text(encoding="utf-8")
                temporary_files = list(root.rglob("*.tmp"))

        self.assertEqual(first_result, 0)
        self.assertEqual(second_result, 0)
        self.assertEqual(first_json, second_json)
        self.assertEqual(first_markdown, second_markdown)
        self.assertNotIn(str(session), first_json)
        self.assertNotIn(str(reference), first_json)
        self.assertNotIn("private-source", first_json)
        self.assertIn("descriptive mitigation signal", first_markdown)
        self.assertIn("recorded 4 skipped releases", first_markdown)
        self.assertNotIn("performance improvement", first_markdown)
        self.assertFalse(temporary_files)

    def test_process_analysis_rejects_divergent_supervisor_facts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            session = make_process_session(Path(temp_dir))
            run_dir = next((session / "vlm_process_async").iterdir())
            process_path = run_dir / "process.json"
            process = json.loads(process_path.read_text(encoding="utf-8"))
            process["process"]["exit_code"] = -15
            process_path.write_text(json.dumps(process) + "\n", encoding="utf-8")
            with patch(
                "experiments.phase1.analyze_vlm_pilot.validate_vlm_slice_dir",
                return_value=[],
            ):
                with self.assertRaisesRegex(ValueError, "facts differ"):
                    analyze_vlm_pilot_dir(session)

    def test_process_analysis_rejects_reference_identity_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with patch(
                "experiments.phase1.analyze_vlm_pilot.validate_vlm_slice_dir",
                return_value=[],
            ):
                reference = write_thread_reference(root)
                value = json.loads(reference.read_text(encoding="utf-8"))
                value["identity"]["input"]["sha256"] = "9" * 64
                reference.write_text(json.dumps(value) + "\n", encoding="utf-8")
                session = make_process_session(root)
                with self.assertRaisesRegex(ValueError, "identity does not match"):
                    analyze_vlm_pilot_dir(
                        session,
                        thread_reference_analysis=reference,
                    )

    def test_process_markdown_preserves_claim_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            session = make_process_session(Path(temp_dir))
            with patch(
                "experiments.phase1.analyze_vlm_pilot.validate_vlm_slice_dir",
                return_value=[],
            ):
                analysis = analyze_vlm_pilot_dir(session)

        report = render_markdown(analysis)
        self.assertIn("Process-isolated Fixed-input VLM Pilot", report)
        self.assertIn("does not prove backend preemption", report)
        self.assertIn("timing_domain_isolation_claim_permitted=False", report)
        self.assertNotIn("hard real-time guarantee", report)


if __name__ == "__main__":
    unittest.main()
