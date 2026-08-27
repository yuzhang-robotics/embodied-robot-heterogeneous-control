from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from experiments.phase1.manifest import (
    SafetyError,
    require_motion_disabled,
    sha256_file,
)
from experiments.phase1.run_simulation import (
    RunError,
    build_parser,
    make_run_id,
    run_once,
)
from experiments.phase1.simulation import SimulationCondition
from experiments.phase1.validate_run import validate_run_dir


SESSION_ID = "20260827T020000Z_phase1_simulation_test"
REPO_ROOT = Path(__file__).resolve().parents[3]


def build_args(
    output_root: Path,
    condition: SimulationCondition,
    repetition: int,
):
    return build_parser().parse_args(
        [
            "--condition",
            condition.value,
            "--service-time-s",
            "0.02",
            "--prelude-s",
            "0.004",
            "--postlude-s",
            "0.004",
            "--probe-period-ms",
            "1",
            "--probe-deadline-ms",
            "2",
            "--adapter-poll-interval-s",
            "0.001",
            "--join-timeout-s",
            "1",
            "--session-id",
            SESSION_ID,
            "--repetition",
            str(repetition),
            "--output-root",
            str(output_root),
            "--allow-dirty",
        ]
    )


class SimulationRunnerTests(unittest.TestCase):
    def test_run_id_uses_condition_and_utc_timestamp(self) -> None:
        now = datetime(2026, 8, 27, 2, 3, 4, tzinfo=timezone.utc)
        self.assertEqual(
            make_run_id(SimulationCondition.R3_ASYNC, 7, now=now),
            "20260827T020304Z_phase1_r3_async_simulated_007",
        )

    def test_motion_guard_fails_closed(self) -> None:
        for value in ("1", "true", "unexpected"):
            with self.subTest(value=value):
                with patch.dict(os.environ, {"ROBOT_ENABLE_MOTION": value}):
                    with self.assertRaises(SafetyError):
                        require_motion_disabled()
        with patch.dict(os.environ, {"ROBOT_ENABLE_MOTION": "0"}):
            self.assertFalse(require_motion_disabled()["motion_enabled"])

    def test_runner_refuses_a_phase0_output_root(self) -> None:
        args = build_args(
            REPO_ROOT / "experiments" / "phase0",
            SimulationCondition.R0_IDLE,
            1,
        )
        with patch.dict(os.environ, {"ROBOT_ENABLE_MOTION": "0"}):
            with self.assertRaisesRegex(RunError, "experiments/phase0"):
                run_once(args, repo_root=REPO_ROOT)

    def test_clean_run_is_marked_eligible_without_development_override(self) -> None:
        clean_environment = {
            "captured_at": "2026-08-27T02:00:00Z",
            "platform": "test",
            "machine": "test",
            "python": "3.12",
            "l4t_release": "",
            "git": {
                "commit": "a" * 40,
                "branch": "feat/phase1-simulation-runner",
                "dirty": False,
                "status_porcelain": [],
                "error_codes": [],
            },
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            args = build_args(
                Path(temp_dir),
                SimulationCondition.R0_IDLE,
                1,
            )
            args.allow_dirty = False
            with (
                patch.dict(os.environ, {"ROBOT_ENABLE_MOTION": "0"}),
                patch(
                    "experiments.phase1.run_simulation.collect_environment",
                    return_value=clean_environment,
                ),
            ):
                run_dir = run_once(args, repo_root=REPO_ROOT)
            manifest = json.loads(
                (run_dir / "manifest.json").read_text(encoding="utf-8")
            )

        self.assertFalse(manifest["reproducibility"]["development_override"])
        self.assertTrue(manifest["reproducibility"]["formal_evidence_eligible"])

    def test_all_conditions_produce_valid_isolated_artifacts(self) -> None:
        baseline_threads = {thread.name for thread in threading.enumerate()}
        with tempfile.TemporaryDirectory() as temp_dir:
            output_root = Path(temp_dir)
            run_dirs = []
            with patch.dict(os.environ, {"ROBOT_ENABLE_MOTION": "0"}):
                for repetition, condition in enumerate(SimulationCondition, start=1):
                    with self.subTest(condition=condition.value):
                        run_dir = run_once(
                            build_args(output_root, condition, repetition),
                            repo_root=REPO_ROOT,
                        )
                        run_dirs.append(run_dir)
                        self.assertEqual(validate_run_dir(run_dir), [])
                        manifest = json.loads(
                            (run_dir / "manifest.json").read_text(encoding="utf-8")
                        )
                        summary = json.loads(
                            (run_dir / "summary.json").read_text(encoding="utf-8")
                        )
                        self.assertEqual(manifest["status"], "completed")
                        self.assertTrue(
                            manifest["reproducibility"]["development_override"]
                        )
                        self.assertFalse(
                            manifest["reproducibility"]["formal_evidence_eligible"]
                        )
                        self.assertTrue(summary["valid"])
                        self.assertTrue(summary["descriptive_only"])
                        self.assertFalse(summary["inference_claim_permitted"])

            inline_summary = json.loads(
                (run_dirs[1] / "summary.json").read_text(encoding="utf-8")
            )
            self.assertGreater(inline_summary["probe"]["skipped_releases"], 0)
            stale_summary = json.loads(
                (run_dirs[4] / "summary.json").read_text(encoding="utf-8")
            )
            stale_dispositions = dict(stale_summary["lifecycle"]["disposition_counts"])
            self.assertEqual(stale_dispositions["rejected_state"], 1)
            self.assertEqual(stale_summary["lifecycle"]["accepted_result_count"], 0)

        remaining = {thread.name for thread in threading.enumerate()}
        self.assertEqual(remaining, baseline_threads)

    def test_validator_rebuilds_summary_instead_of_trusting_its_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.dict(os.environ, {"ROBOT_ENABLE_MOTION": "0"}):
                run_dir = run_once(
                    build_args(
                        Path(temp_dir),
                        SimulationCondition.R3_ASYNC,
                        1,
                    ),
                    repo_root=REPO_ROOT,
                )
            summary_path = run_dir / "summary.json"
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            summary["probe"]["tick_count"] += 1
            summary_path.write_text(
                json.dumps(summary, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            manifest_path = run_dir / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["artifacts"]["summary.json"] = {
                "size_bytes": summary_path.stat().st_size,
                "sha256": sha256_file(summary_path),
            }
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            errors = validate_run_dir(run_dir)

        self.assertTrue(any("independently rebuilt" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
