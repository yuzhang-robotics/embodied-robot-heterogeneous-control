from __future__ import annotations

import tempfile
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from experiments.phase1.analyze_jetson_pilot import (
    _detect_cpu_activity,
    analyze_pilot_dir,
    main,
    render_markdown,
)
from experiments.phase1.jetson_telemetry import TegrastatsSampler
from experiments.phase1.run_jetson_pilot import run_pilot_session
from experiments.phase1.tests.test_jetson_pilot import (
    REPO_ROOT,
    clean_environment,
    passing_preflight,
    pilot_args,
)
from experiments.phase1.tests.test_jetson_telemetry import (
    TEGRASTATS_SAMPLE,
    sampler_command,
)


ARCHIVE_SHA256 = "b" * 64


def make_session(output_root: Path, line: str = TEGRASTATS_SAMPLE) -> Path:
    def sampler_factory(session_dir: Path, interval_ms: int):
        return TegrastatsSampler(
            session_dir,
            interval_ms,
            command=sampler_command(line),
        )

    with patch(
        "experiments.phase1.run_simulation.collect_environment",
        return_value=clean_environment(),
    ):
        return run_pilot_session(
            pilot_args(output_root),
            repo_root=REPO_ROOT,
            preflight_builder=lambda *args, **kwargs: passing_preflight(),
            sampler_factory=sampler_factory,
        )


class JetsonPilotAnalysisTests(unittest.TestCase):
    def test_analysis_preserves_claim_boundary_and_runtime_facts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            session_dir = make_session(Path(temp_dir))
            analysis = analyze_pilot_dir(
                session_dir,
                source_archive_sha256=ARCHIVE_SHA256,
            )

        self.assertEqual(analysis["analysis_schema_version"], "0.1.0")
        self.assertEqual(analysis["source"]["source_archive_sha256"], ARCHIVE_SHA256)
        self.assertTrue(analysis["validation"]["session_valid"])
        self.assertEqual(analysis["validation"]["run_count"], 6)
        self.assertFalse(analysis["claim_boundary"]["inference_claim_permitted"])
        self.assertFalse(
            analysis["claim_boundary"]["condition_resource_attribution_permitted"]
        )
        self.assertEqual(len(analysis["responsiveness"]), 6)
        self.assertGreater(
            next(
                row
                for row in analysis["responsiveness"]
                if row["condition"] == "r1_inline_sync"
            )["skipped_releases"],
            0,
        )
        self.assertEqual(len(analysis["runtime_overhead"]), 1)
        lifecycle = {row["condition"]: row for row in analysis["lifecycle"]}
        self.assertEqual(
            lifecycle["r4_stale"]["disposition_counts"],
            {"rejected_state": 1},
        )
        self.assertEqual(
            lifecycle["r4_overflow"]["disposition_counts"],
            {
                "consumed": 1,
                "dropped_overflow": 2,
                "rejected_cancelled": 1,
            },
        )
        self.assertIn(
            "simulated_workload_only", analysis["data_quality"]["limitations"]
        )

    def test_missing_emc_is_reported_as_unavailable_not_zero(self) -> None:
        sample_without_emc = TEGRASTATS_SAMPLE.replace("EMC_FREQ 2%@2133 ", "")
        with tempfile.TemporaryDirectory() as temp_dir:
            session_dir = make_session(Path(temp_dir), sample_without_emc)
            analysis = analyze_pilot_dir(session_dir)

        capabilities = analysis["resources"]["capabilities"]
        self.assertEqual(capabilities["emc"]["available_sample_count"], 0)
        self.assertEqual(
            capabilities["emc"]["missing_sample_count"],
            capabilities["sample_count"],
        )
        self.assertEqual(
            capabilities["parse_warning_counts"],
            {"emc_missing": capabilities["sample_count"]},
        )
        self.assertIn("emc_unavailable", analysis["data_quality"]["limitations"])

    def test_activity_screen_groups_sustained_samples_and_maps_runs(self) -> None:
        samples = []
        for sequence, usage in enumerate((10, 80, 90, 100, 95, 85, 10)):
            samples.append(
                {
                    "sample_monotonic_ns": sequence * 200_000_000,
                    "cpu": [{"usage_pct": usage}],
                }
            )
        runs = [
            {
                "sequence": 1,
                "condition": "r2_threaded_sync",
                "service_time_s": 2.0,
                "started_monotonic_ns": 0,
                "finished_monotonic_ns": 700_000_000,
            },
            {
                "sequence": 2,
                "condition": "r3_async",
                "service_time_s": 2.0,
                "started_monotonic_ns": 700_000_001,
                "finished_monotonic_ns": 1_400_000_000,
            },
        ]

        episodes = _detect_cpu_activity(
            samples,
            runs,
            threshold_pct=80.0,
            minimum_samples=5,
            merge_gap_ms=500.0,
        )

        self.assertEqual(len(episodes), 1)
        self.assertEqual(episodes[0]["sample_count"], 5)
        self.assertEqual(episodes[0]["aggregate_cpu_max_pct"], 100.0)
        self.assertEqual(
            [row["condition"] for row in episodes[0]["overlapping_runs"]],
            ["r2_threaded_sync", "r3_async"],
        )

    def test_cli_writes_deterministic_outputs_outside_the_session(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            session_dir = make_session(root / "runs")
            json_path = root / "published" / "analysis.json"
            markdown_path = root / "published" / "report.md"

            result = main(
                [
                    str(session_dir),
                    "--source-archive-sha256",
                    ARCHIVE_SHA256,
                    "--json-output",
                    str(json_path),
                    "--markdown-output",
                    str(markdown_path),
                ]
            )
            first_json = json_path.read_text(encoding="utf-8")
            first_markdown = markdown_path.read_text(encoding="utf-8")
            result_again = main(
                [
                    str(session_dir),
                    "--source-archive-sha256",
                    ARCHIVE_SHA256,
                    "--json-output",
                    str(json_path),
                    "--markdown-output",
                    str(markdown_path),
                ]
            )

            self.assertEqual(result, 0)
            self.assertEqual(result_again, 0)
            self.assertEqual(first_json, json_path.read_text(encoding="utf-8"))
            self.assertEqual(
                first_markdown,
                markdown_path.read_text(encoding="utf-8"),
            )
            self.assertNotIn(str(session_dir), first_json)
            self.assertIn("# Phase 1 Jetson Simulation Pilot", first_markdown)
            self.assertFalse(list(root.rglob("*.tmp")))

    def test_analysis_rejects_bad_archive_hash_and_session_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            session_dir = make_session(root)
            with self.assertRaisesRegex(ValueError, "64 hexadecimal"):
                analyze_pilot_dir(
                    session_dir,
                    source_archive_sha256="not-a-digest",
                )
            stderr = StringIO()
            with redirect_stderr(stderr):
                result = main(
                    [
                        str(session_dir),
                        "--json-output",
                        str(session_dir / "analysis.json"),
                    ]
                )
            duplicate_output = root / "duplicate.out"
            with redirect_stderr(stderr):
                duplicate_result = main(
                    [
                        str(session_dir),
                        "--json-output",
                        str(duplicate_output),
                        "--markdown-output",
                        str(duplicate_output),
                    ]
                )
            self.assertFalse(duplicate_output.exists())

        self.assertEqual(result, 1)
        self.assertIn("must not be written inside", stderr.getvalue())
        self.assertEqual(duplicate_result, 1)
        self.assertIn("must use distinct paths", stderr.getvalue())

    def test_markdown_renderer_contains_no_inferential_claim(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            analysis = analyze_pilot_dir(make_session(Path(temp_dir)))

        report = render_markdown(analysis)
        self.assertIn("not a formal performance comparison", report)
        self.assertIn("does not exercise a heterogeneous inference workload", report)
        self.assertNotIn("statistically significant", report)


if __name__ == "__main__":
    unittest.main()
