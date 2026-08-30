from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from experiments.phase1.analyze_vlm_pilot import (
    analyze_vlm_pilot_dir,
    main,
    render_markdown,
)


SESSION_ID = "20260830T073825Z_phase1_vlm_pilot"
ARCHIVE_SHA256 = "1" * 64


def _resource_description() -> dict[str, object]:
    metric = {
        "count": 1,
        "min": 1.0,
        "mean": 2.0,
        "median": 2.0,
        "p50": 2.0,
        "p95": 3.0,
        "p99": 3.0,
        "max": 4.0,
    }
    return {
        "sample_count": 1,
        "parse_error_count": 0,
        "sample_interval_ns": None,
        "ram_used_mb": metric,
        "gr3d_usage_pct": metric,
        "temperatures_c": {"tj": metric},
        "power_instant_mw": {"VDD_IN": metric},
    }


def _write_run(session: Path, condition: str, commit: str = "a" * 40) -> Path:
    run_id = f"20260830T000000Z_phase1_{condition}_vlm_001"
    run_dir = session / condition / run_id
    run_dir.mkdir(parents=True)
    disposition = "consumed" if condition == "vlm_async" else "rejected_state"
    outcome = "ok" if condition == "vlm_async" else "cancel_observed"
    accepted = int(condition == "vlm_async")
    durations = {
        "input_verify_before": 10_000_000,
        "module_import": 300_000_000,
        "moondream_inference": 200_000_000,
        "qwen_rewrite": 100_000_000,
        "output_normalization": 10_000_000,
        "model_unload": 10_000_000,
        "input_verify_after": 10_000_000,
    }
    status = {name: "ok" for name in durations}
    started_ns = 1_000_000_000
    finished_ns = started_ns + sum(durations.values())
    manifest = {
        "run_id": run_id,
        "session_id": SESSION_ID,
        "condition": condition,
        "status": "completed",
        "environment": {
            "git": {"commit": commit, "branch": "main"},
        },
        "input": {
            "sha256": "6" * 64,
            "size_bytes": 9009,
            "media_type": "image/jpeg",
            "path_recorded": False,
        },
        "artifacts": {"summary.json": {"sha256": "7" * 64, "size_bytes": 1}},
    }
    preflight = {
        "vlm_preflight_schema_version": "0.1.0",
        "services": {
            "ollama": {"model": "moondream", "model_digest": "5" * 64},
            "qwen": {"served_model_ids": ["qwen.gguf"]},
        },
    }
    scenario = {
        "spec": {"probe_period_ns": 100_000_000, "probe_deadline_ns": 100_000_000},
        "report": {
            "probe": {
                "tick_count": 10,
                "skipped_releases": 2,
                "deadline_miss_count": 0,
                "max_lateness_ns": 5_000_000,
                "max_gap_ns": 300_000_000,
                "joined": True,
                "error_code": None,
            }
        },
    }
    summary = {
        "condition": condition,
        "valid": True,
        "real_vlm_path_executed": True,
        "adapter": {
            "started_monotonic_ns": started_ns,
            "finished_monotonic_ns": finished_ns,
            "execution_outcome": outcome,
            "translation_route": "qwen",
            "stage_durations_ns": durations,
            "stage_status": status,
            "output": {"raw_text_recorded": False},
            "model_residency": {"unload_requested": True, "unload_confirmed": None},
            "cancellation": {
                "requested": condition == "vlm_stale",
                "worker_observed": condition == "vlm_stale",
                "backend_stop_confirmed": None,
            },
        },
        "lifecycle": {
            "disposition_counts": [[disposition, 1]],
            "accepted_result_count": accepted,
            "stale_consumed_count": 0,
        },
        "resources": {
            "inference_interval_sample_count": 1,
            "summary": _resource_description(),
        },
        "gates": [{"name": "test", "passed": True}],
    }
    events = [
        {
            "event": "probe.started",
            "details": {
                "origin_monotonic_ns": 900_000_000,
                "period_ns": 100_000_000,
            },
        },
        {
            "event": "probe.skipped",
            "details": {
                "from_index": 2,
                "to_index": 4,
                "skipped_releases": 2,
            },
        },
    ]
    resource = {"parse_warnings": ["emc_missing"]}
    for name, value in (
        ("manifest.json", manifest),
        ("preflight.json", preflight),
        ("scenario.json", scenario),
        ("summary.json", summary),
    ):
        (run_dir / name).write_text(json.dumps(value) + "\n", encoding="utf-8")
    (run_dir / "events.jsonl").write_text(
        "\n".join(json.dumps(event) for event in events) + "\n",
        encoding="utf-8",
    )
    (run_dir / "resources.jsonl").write_text(
        json.dumps(resource) + "\n",
        encoding="utf-8",
    )
    return run_dir


def make_session(root: Path) -> Path:
    session = root / SESSION_ID
    _write_run(session, "vlm_async")
    _write_run(session, "vlm_stale")
    return session


class VLMPilotAnalysisTests(unittest.TestCase):
    def test_analysis_separates_correctness_from_timing_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            session = make_session(Path(temp_dir))
            with patch(
                "experiments.phase1.analyze_vlm_pilot.validate_vlm_slice_dir",
                return_value=[],
            ):
                analysis = analyze_vlm_pilot_dir(
                    session,
                    source_archive_sha256=ARCHIVE_SHA256,
                )

        self.assertTrue(analysis["validation"]["all_runs_valid"])
        self.assertTrue(analysis["validation"]["correctness_observed"])
        self.assertFalse(analysis["validation"]["listener_binding_evidence_complete"])
        self.assertFalse(
            analysis["claim_boundary"]["timing_domain_isolation_claim_permitted"]
        )
        self.assertEqual(analysis["data_quality"]["total_probe_skipped_releases"], 4)
        for run in analysis["runs"]:
            self.assertEqual(
                run["timing"]["probe"]["skipped_releases_by_adapter_stage"],
                {"module_import": 2},
            )

    def test_cli_writes_deterministic_outputs_outside_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            session = make_session(root / "raw")
            json_path = root / "published" / "analysis.json"
            markdown_path = root / "published" / "README.md"
            arguments = [
                str(session),
                "--source-archive-sha256",
                ARCHIVE_SHA256,
                "--json-output",
                str(json_path),
                "--markdown-output",
                str(markdown_path),
            ]
            with patch(
                "experiments.phase1.analyze_vlm_pilot.validate_vlm_slice_dir",
                return_value=[],
            ):
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
        self.assertIn("# Phase 1 Fixed-input VLM Pilot", first_markdown)
        self.assertIn(
            "missed scheduled releases during lazy module import", first_markdown
        )
        self.assertNotIn("performance improvement", first_markdown)
        self.assertFalse(temporary_files)

    def test_analysis_rejects_mismatched_source_and_session_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            session = make_session(root)
            stale_dir = next((session / "vlm_stale").iterdir())
            manifest_path = stale_dir / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["environment"]["git"]["commit"] = "b" * 40
            manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")
            with patch(
                "experiments.phase1.analyze_vlm_pilot.validate_vlm_slice_dir",
                return_value=[],
            ):
                with self.assertRaisesRegex(ValueError, "runs disagree"):
                    analyze_vlm_pilot_dir(session)
                stderr = StringIO()
                with redirect_stderr(stderr):
                    result = main(
                        [
                            str(session),
                            "--json-output",
                            str(session / "analysis.json"),
                        ]
                    )

        self.assertEqual(result, 1)
        self.assertIn("must not be written inside", stderr.getvalue())

    def test_analysis_rejects_extra_source_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            session = make_session(Path(temp_dir))
            (session / "notes.txt").write_text("unexpected\n", encoding="utf-8")
            with patch(
                "experiments.phase1.analyze_vlm_pilot.validate_vlm_slice_dir",
                return_value=[],
            ):
                with self.assertRaisesRegex(ValueError, "must contain exactly"):
                    analyze_vlm_pilot_dir(session)

    def test_markdown_preserves_claim_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            session = make_session(Path(temp_dir))
            with patch(
                "experiments.phase1.analyze_vlm_pilot.validate_vlm_slice_dir",
                return_value=[],
            ):
                analysis = analyze_vlm_pilot_dir(session)

        report = render_markdown(analysis)
        self.assertIn("not a synchronous/asynchronous performance comparison", report)
        self.assertIn("does not authorize a heterogeneous-inference claim", report)
        self.assertNotIn("hard real-time guarantee", report)


if __name__ == "__main__":
    unittest.main()
