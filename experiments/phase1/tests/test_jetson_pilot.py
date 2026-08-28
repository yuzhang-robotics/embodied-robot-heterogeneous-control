from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from experiments.phase1.jetson_preflight import build_jetson_preflight
from experiments.phase1.jetson_telemetry import TegrastatsSampler
from experiments.phase1.manifest import sha256_file
from experiments.phase1.pilot import (
    PilotError,
    build_pilot_plan,
    make_pilot_session_id,
    validate_pilot_dir,
)
from experiments.phase1.run_jetson_pilot import (
    build_parser,
    run_pilot_session,
)
from experiments.phase1.tests.test_jetson_telemetry import sampler_command


SESSION_ID = "20260827T120000Z_phase1_jetson_pilot_test"
REPO_ROOT = Path(__file__).resolve().parents[3]


def clean_environment() -> dict[str, object]:
    return {
        "captured_at": "2026-08-27T12:00:00Z",
        "platform": "Linux-5.15.0-tegra-aarch64-with-glibc2.35",
        "machine": "aarch64",
        "python": "3.10.12",
        "l4t_release": "# R36 (release), REVISION: 4.7",
        "git": {
            "commit": "a" * 40,
            "branch": "main",
            "dirty": False,
            "status_porcelain": [],
            "upstream": "origin/main",
            "upstream_commit": "a" * 40,
            "ahead_behind": "0\t0",
            "error_codes": [],
        },
        "jetpack_packages": {"returncode": 0, "output": "", "error_code": None},
        "nvpmodel": {"returncode": 0, "output": "MAXN", "error_code": None},
        "jetson_clocks": {"returncode": 0, "output": "", "error_code": None},
    }


def passing_preflight() -> dict[str, object]:
    with patch.dict(os.environ, {"ROBOT_ENABLE_MOTION": "0"}):
        return build_jetson_preflight(
            REPO_ROOT,
            environment=clean_environment(),
            tegrastats_available=True,
            loaded_modules={"experiments.phase1.run_jetson_pilot"},
        )


def pilot_args(output_root: Path):
    return build_parser().parse_args(
        [
            "--service-times-s",
            "0.02",
            "--correctness-service-time-s",
            "0.02",
            "--prelude-s",
            "0.02",
            "--postlude-s",
            "0.02",
            "--probe-period-ms",
            "5",
            "--probe-deadline-ms",
            "10",
            "--adapter-poll-interval-s",
            "0.001",
            "--join-timeout-s",
            "1",
            "--resource-interval-ms",
            "50",
            "--resource-tail-s",
            "0.02",
            "--resource-first-sample-timeout-s",
            "2",
            "--session-id",
            SESSION_ID,
            "--output-root",
            str(output_root),
        ]
    )


class JetsonPilotTests(unittest.TestCase):
    def test_preflight_requires_jetson_clean_main_and_safe_imports(self) -> None:
        preflight = passing_preflight()
        self.assertTrue(preflight["eligible"])

        environment = clean_environment()
        environment["machine"] = "x86_64"
        environment["git"] = dict(environment["git"], dirty=True)
        with patch.dict(os.environ, {"ROBOT_ENABLE_MOTION": "0"}):
            failed = build_jetson_preflight(
                REPO_ROOT,
                environment=environment,
                tegrastats_available=True,
                loaded_modules={"jetson.robot_comm"},
            )
        failed_names = {
            check["name"] for check in failed["checks"] if check["passed"] is False
        }
        self.assertEqual(
            failed_names,
            {"arm64_machine", "git_tree_clean", "forbidden_modules_absent"},
        )

    def test_preflight_fails_closed_on_motion_and_upstream_mismatch(self) -> None:
        environment = clean_environment()
        environment["git"] = dict(
            environment["git"],
            upstream_commit="b" * 40,
            ahead_behind="1\t0",
        )
        for motion_value in ("1", "unexpected"):
            with self.subTest(motion_value=motion_value):
                with patch.dict(
                    os.environ, {"ROBOT_ENABLE_MOTION": motion_value}, clear=False
                ):
                    preflight = build_jetson_preflight(
                        REPO_ROOT,
                        environment=environment,
                        tegrastats_available=True,
                        loaded_modules=set(),
                    )
                failed_names = {
                    check["name"]
                    for check in preflight["checks"]
                    if check["passed"] is False
                }
                self.assertEqual(
                    failed_names,
                    {"motion_disabled", "git_upstream_synchronized"},
                )
                self.assertFalse(preflight["eligible"])

    def test_plan_freezes_responsiveness_and_correctness_roles(self) -> None:
        plan = build_pilot_plan(
            session_id=SESSION_ID,
            service_times_s=[2.0, 5.0],
            repetitions=2,
            correctness_service_time_s=2.0,
        )

        self.assertEqual(len(plan["runs"]), 20)
        self.assertEqual(
            [entry["condition"] for entry in plan["runs"][:6]],
            [
                "r0_idle",
                "r1_inline_sync",
                "r2_threaded_sync",
                "r3_async",
                "r0_idle",
                "r1_inline_sync",
            ],
        )
        self.assertEqual(plan["runs"][8]["role"], "correctness")
        self.assertFalse(plan["inference_claim_permitted"])

        with self.assertRaisesRegex(TypeError, "probe_period_ms"):
            build_pilot_plan(
                session_id=SESSION_ID,
                service_times_s=[2.0],
                probe_period_ms=True,
            )

    def test_session_id_uses_utc_and_optional_label(self) -> None:
        now = datetime(2026, 8, 27, 12, 3, 4, tzinfo=timezone.utc)
        self.assertEqual(
            make_pilot_session_id(now, label="smoke"),
            "20260827T120304Z_phase1_jetson_pilot_smoke",
        )

    def test_preflight_failure_creates_no_session_directory(self) -> None:
        failed_preflight = passing_preflight()
        failed_preflight["checks"][0]["passed"] = False
        failed_preflight["eligible"] = False
        with tempfile.TemporaryDirectory() as temp_dir:
            output_root = Path(temp_dir)
            with self.assertRaisesRegex(PilotError, "preflight failed"):
                run_pilot_session(
                    pilot_args(output_root),
                    repo_root=REPO_ROOT,
                    preflight_builder=lambda *args, **kwargs: failed_preflight,
                )
            self.assertEqual(list(output_root.iterdir()), [])

    def test_pilot_refuses_the_phase0_source_as_output(self) -> None:
        with self.assertRaisesRegex(PilotError, "experiments/phase0"):
            run_pilot_session(
                pilot_args(REPO_ROOT / "experiments" / "phase0"),
                repo_root=REPO_ROOT,
                preflight_builder=lambda *args, **kwargs: passing_preflight(),
            )

    def test_sampler_construction_failure_marks_the_session_failed(self) -> None:
        def failing_sampler_factory(*args, **kwargs):
            raise RuntimeError("sampler unavailable")

        with tempfile.TemporaryDirectory() as temp_dir:
            output_root = Path(temp_dir)
            with self.assertRaisesRegex(RuntimeError, "sampler unavailable"):
                run_pilot_session(
                    pilot_args(output_root),
                    repo_root=REPO_ROOT,
                    preflight_builder=lambda *args, **kwargs: passing_preflight(),
                    sampler_factory=failing_sampler_factory,
                )
            manifest = json.loads(
                (output_root / SESSION_ID / "session_manifest.json").read_text(
                    encoding="utf-8"
                )
            )

        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(manifest["failure_code"], "runtimeerror")

    def test_complete_session_is_reconstructable_and_leak_free(self) -> None:
        baseline_threads = {thread.name for thread in threading.enumerate()}
        preflight = passing_preflight()

        def sampler_factory(session_dir: Path, interval_ms: int):
            return TegrastatsSampler(
                session_dir,
                interval_ms,
                command=sampler_command(),
            )

        with tempfile.TemporaryDirectory() as temp_dir:
            with patch(
                "experiments.phase1.run_simulation.collect_environment",
                return_value=clean_environment(),
            ):
                session_dir = run_pilot_session(
                    pilot_args(Path(temp_dir)),
                    repo_root=REPO_ROOT,
                    preflight_builder=lambda *args, **kwargs: preflight,
                    sampler_factory=sampler_factory,
                )
            self.assertEqual(validate_pilot_dir(session_dir), [])
            summary = json.loads(
                (session_dir / "pilot_summary.json").read_text(encoding="utf-8")
            )
            manifest = json.loads(
                (session_dir / "session_manifest.json").read_text(encoding="utf-8")
            )
            self.assertTrue(summary["valid"])
            self.assertEqual(summary["run_count"], 6)
            self.assertTrue(manifest["resource_sampler_report"]["successful"])
            self.assertTrue(
                all(run["resources"]["sample_count"] > 0 for run in summary["runs"])
            )

            summary["run_count"] += 1
            summary_path = session_dir / "pilot_summary.json"
            summary_path.write_text(
                json.dumps(summary, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            manifest["artifacts"]["pilot_summary.json"] = {
                "size_bytes": summary_path.stat().st_size,
                "sha256": sha256_file(summary_path),
            }
            (session_dir / "session_manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            summary_errors = validate_pilot_dir(session_dir)

            summary["run_count"] -= 1
            summary_path.write_text(
                json.dumps(summary, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            manifest["artifacts"]["pilot_summary.json"] = {
                "size_bytes": summary_path.stat().st_size,
                "sha256": sha256_file(summary_path),
            }
            resources_path = session_dir / "resources.jsonl"
            resource_rows = [
                json.loads(line)
                for line in resources_path.read_text(encoding="utf-8").splitlines()
            ]
            resource_rows[0]["gr3d"]["usage_pct"] = 101
            resources_path.write_text(
                "".join(
                    json.dumps(row, ensure_ascii=False) + "\n" for row in resource_rows
                ),
                encoding="utf-8",
            )
            manifest["artifacts"]["resources.jsonl"] = {
                "size_bytes": resources_path.stat().st_size,
                "sha256": sha256_file(resources_path),
            }
            (session_dir / "session_manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            (session_dir / "unexpected.txt").write_text(
                "unexpected\n", encoding="utf-8"
            )
            resource_errors = validate_pilot_dir(session_dir)

        self.assertTrue(
            any("independently rebuilt" in error for error in summary_errors)
        )
        self.assertTrue(any("GR3D usage" in error for error in resource_errors))
        self.assertTrue(
            any("unexpected pilot artifact" in error for error in resource_errors)
        )
        remaining_threads = {thread.name for thread in threading.enumerate()}
        self.assertEqual(remaining_threads, baseline_threads)


if __name__ == "__main__":
    unittest.main()
