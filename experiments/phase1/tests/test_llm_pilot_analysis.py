from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from experiments.phase1.analyze_llm_pilot import (
    ANALYSIS_KIND,
    _EXPECTED_GATES,
    analyze_llm_pilot_dir,
    main,
    render_markdown,
)
from experiments.phase1.llm_adapter import (
    LLM_EMPTY_HISTORY_SHA256,
    LLM_EXPECTED_SERVED_MODEL_ID,
    LLM_INPUT_MEDIA_TYPE,
    LLM_INPUT_SHA256,
    LLM_INPUT_SIZE_BYTES,
    LLM_MODEL_SHA256,
    LLM_MODEL_SIZE_BYTES,
    LLM_SERVER_ARGUMENTS,
    frozen_llm_request_contract,
)


SESSION_ID = "20260901T143315Z_phase1_llm_pilot"
ARCHIVE_SHA256 = "d" * 64
COMMIT = "b" * 40
SOURCE_VERSION = "b9246-2-g585080d31"


def _metric() -> dict[str, object]:
    return {
        "count": 1,
        "min": 1.0,
        "mean": 2.0,
        "median": 2.0,
        "p50": 2.0,
        "p95": 3.0,
        "p99": 3.0,
        "max": 4.0,
    }


def _write_run(session: Path, condition: str, *, commit: str = COMMIT) -> Path:
    run_id = f"20260901T143000Z_phase1_{condition}_llm_001"
    run_dir = session / condition / run_id
    run_dir.mkdir(parents=True)
    nominal = condition == "llm_async"
    disposition = "consumed" if nominal else "rejected_state"
    duration_ns = 1_500_000_000 if nominal else 620_000_000
    request_contract = frozen_llm_request_contract()
    input_identity = {
        "sha256": LLM_INPUT_SHA256,
        "size_bytes": LLM_INPUT_SIZE_BYTES,
        "media_type": LLM_INPUT_MEDIA_TYPE,
        "path_recorded": False,
        "raw_text_recorded": False,
    }
    model_identity = {
        "sha256": LLM_MODEL_SHA256,
        "size_bytes": LLM_MODEL_SIZE_BYTES,
        "served_model_id": LLM_EXPECTED_SERVED_MODEL_ID,
    }
    history_identity = {
        "sha256": LLM_EMPTY_HISTORY_SHA256,
        "messages": 0,
        "raw_history_recorded": False,
    }
    workload = {
        "source": "llama_cpp_openai_http",
        "model": model_identity,
        "source_version": SOURCE_VERSION,
        "server_arguments": dict(LLM_SERVER_ARGUMENTS),
        "request": request_contract,
        "history": history_identity,
        "residency_policy": "external_llama_server_resident",
        "raw_prompt_recorded": False,
        "raw_output_recorded": False,
    }
    manifest = {
        "run_id": run_id,
        "session_id": SESSION_ID,
        "condition": condition,
        "status": "completed",
        "resource_interval_ms": 200,
        "environment": {"git": {"commit": commit, "branch": "main"}},
        "input": input_identity,
        "workload_contract": workload,
        "artifacts": {"summary.json": {"sha256": "7" * 64, "size_bytes": 1}},
    }
    runtime = {
        "model_size_bytes": LLM_MODEL_SIZE_BYTES,
        "model_sha256": LLM_MODEL_SHA256,
        "source_version": SOURCE_VERSION,
        "source_clean": True,
        "server_process_count": 1,
        "server_arguments": dict(LLM_SERVER_ARGUMENTS),
        "server_arguments_match": True,
        "server_model_path_matches": True,
        "endpoint_local": True,
        "listener_addresses": ["127.0.0.1"],
        "listener_loopback_only": True,
        "service_reachable": True,
        "expected_model_present": True,
        "request_contract": request_contract,
    }
    preflight = {
        "llm_preflight_schema_version": "0.1.0",
        "runtime": runtime,
    }
    scenario = {
        "report": {
            "probe": {
                "tick_count": 20,
                "skipped_releases": 0,
                "deadline_miss_count": 0,
                "max_lateness_ns": 500_000,
                "max_gap_ns": 100_500_000,
                "joined": True,
                "error_code": None,
            }
        }
    }
    resource_summary = {
        "sample_count": 1,
        "parse_error_count": 0,
        "parse_warning_count": 1,
        "ram_used_mb": _metric(),
        "gr3d_usage_pct": _metric(),
        "temperatures_c": {"tj": _metric()},
        "power_instant_mw": {"VDD_IN": _metric()},
    }
    cancellation = {
        "requested": not nominal,
        "worker_observed": not nominal,
        "client_wait_stopped": False,
        "backend_stop_confirmed": None,
    }
    summary = {
        "llm_summary_schema_version": "0.1.0",
        "condition": condition,
        "valid": True,
        "real_llm_path_executed": True,
        "spec": {"stale_observation_s": 0.5},
        "adapter": {
            "started_monotonic_ns": 1_000_000_000,
            "finished_monotonic_ns": 1_000_000_000 + duration_ns,
            "execution_outcome": "ok" if nominal else "cancel_observed",
            "error_code": None,
            "output": {
                "sha256": ("8" if nominal else "9") * 64,
                "length": 43 if nominal else 40,
                "raw_text_recorded": False,
            },
            "response": {
                "model": LLM_EXPECTED_SERVED_MODEL_ID,
                "usage": {
                    "prompt_tokens": 103,
                    "completion_tokens": 26,
                    "total_tokens": 129,
                },
                "raw_response_recorded": False,
            },
            "model_residency": {
                "policy": "external_llama_server_resident",
                "server_preexisting": True,
                "unload_requested": False,
                "backend_stop_confirmed": None,
            },
            "cancellation": cancellation,
        },
        "lifecycle": {
            "disposition_counts": [[disposition, 1]],
            "accepted_result_count": int(nominal),
            "stale_consumed_count": 0,
        },
        "resources": {
            "inference_interval_sample_count": 1,
            "summary": resource_summary,
        },
        "gates": [{"name": name, "passed": True} for name in sorted(_EXPECTED_GATES)],
    }
    for name, value in (
        ("manifest.json", manifest),
        ("preflight.json", preflight),
        ("scenario.json", scenario),
        ("summary.json", summary),
    ):
        (run_dir / name).write_text(json.dumps(value) + "\n", encoding="utf-8")
    (run_dir / "events.jsonl").write_text("{}\n", encoding="utf-8")
    (run_dir / "resources.jsonl").write_text(
        json.dumps({"parse_warnings": ["emc_missing"]}) + "\n",
        encoding="utf-8",
    )
    return run_dir


def make_session(root: Path) -> Path:
    session = root / SESSION_ID
    for condition in ("llm_async", "llm_stale"):
        _write_run(session, condition)
    return session


class LLMPilotAnalysisTests(unittest.TestCase):
    def test_analysis_closes_llm_component_and_phase1_g5(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            session = make_session(Path(temp_dir))
            with patch(
                "experiments.phase1.analyze_llm_pilot.validate_llm_slice_dir",
                return_value=[],
            ):
                analysis = analyze_llm_pilot_dir(
                    session,
                    source_archive_sha256=ARCHIVE_SHA256,
                )

        self.assertEqual(analysis["analysis_kind"], ANALYSIS_KIND)
        self.assertTrue(analysis["validation"]["all_runs_valid"])
        self.assertTrue(analysis["validation"]["correctness_observed"])
        self.assertTrue(analysis["validation"]["probe_continuity_observed"])
        self.assertTrue(analysis["validation"]["resource_coverage_observed"])
        self.assertTrue(analysis["claim_boundary"]["llm_g5_component_satisfied"])
        self.assertTrue(analysis["claim_boundary"]["phase1_g5_complete"])
        self.assertEqual(analysis["claim_boundary"]["remaining_g5_workloads"], [])
        self.assertFalse(analysis["claim_boundary"]["g6_preregistration_complete"])
        self.assertFalse(
            analysis["claim_boundary"]["backend_cancellation_claim_permitted"]
        )
        self.assertEqual(analysis["controls"]["stale_observation_control_ms"], 500.0)

    def test_cli_writes_deterministic_private_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            session = make_session(root / "private-source")
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
                "experiments.phase1.analyze_llm_pilot.validate_llm_slice_dir",
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
        self.assertNotIn("private-source", first_json)
        self.assertNotIn("private prompt content", first_json)
        self.assertIn("# Phase 1 Fixed-input LLM Pilot", first_markdown)
        self.assertIn(
            "LLM correctness-pilot component of G5 is satisfied", first_markdown
        )
        self.assertIn("Phase 1 G5 is complete", first_markdown)
        self.assertNotIn("performance improvement", first_markdown)
        self.assertFalse(temporary_files)

    def test_analysis_rejects_mismatched_source_and_session_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            session = make_session(root)
            stale_dir = next((session / "llm_stale").iterdir())
            manifest_path = stale_dir / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["environment"]["git"]["commit"] = "c" * 40
            manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")
            with patch(
                "experiments.phase1.analyze_llm_pilot.validate_llm_slice_dir",
                return_value=[],
            ):
                with self.assertRaisesRegex(ValueError, "runs disagree"):
                    analyze_llm_pilot_dir(session)
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

    def test_analysis_rejects_extra_artifact_and_incomplete_gate_set(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            session = make_session(root / "extra")
            (session / "notes.txt").write_text("unexpected\n", encoding="utf-8")
            with patch(
                "experiments.phase1.analyze_llm_pilot.validate_llm_slice_dir",
                return_value=[],
            ):
                with self.assertRaisesRegex(ValueError, "exactly the two"):
                    analyze_llm_pilot_dir(session)

            session = make_session(root / "gates")
            run_dir = next((session / "llm_async").iterdir())
            summary_path = run_dir / "summary.json"
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            summary["gates"].pop()
            summary_path.write_text(json.dumps(summary) + "\n", encoding="utf-8")
            with patch(
                "experiments.phase1.analyze_llm_pilot.validate_llm_slice_dir",
                return_value=[],
            ):
                with self.assertRaisesRegex(ValueError, "gate set"):
                    analyze_llm_pilot_dir(session)

    def test_markdown_preserves_claim_boundaries(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            session = make_session(Path(temp_dir))
            with patch(
                "experiments.phase1.analyze_llm_pilot.validate_llm_slice_dir",
                return_value=[],
            ):
                analysis = analyze_llm_pilot_dir(session)

        report = render_markdown(analysis)
        self.assertIn("is not cancellation latency", report)
        self.assertIn("does not prove that backend inference stopped", report)
        self.assertIn("does not authorize a heterogeneous-inference claim", report)
        self.assertIn("Phase 1 G5 is complete", report)
        self.assertNotIn("hard real-time guarantee", report)


if __name__ == "__main__":
    unittest.main()
