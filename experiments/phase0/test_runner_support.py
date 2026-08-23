"""Host-side tests for Phase 0 manifest and run validation helpers."""

import ast
import csv
import io
import json
import os
import sys
import tempfile
import types
import unittest
from argparse import Namespace
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from .aggregate_runs import aggregate_run_dirs, descriptive_stats
from .benchmark_recorder import nearest_rank, run_benchmark
from .manifest import file_identity, sha256_text, write_json_atomic
from .run_workload import (
    REPO_ROOT,
    WorkloadError,
    _SYSTEM_PROMPT,
    _run_vlm,
    llama_models_url,
    make_run_id,
    run_once,
)
from .summarize_run import _llm_token_usage, summarize_run_dir
from .telemetry import EventRecorder, RESOURCE_FIELDS
from .validate_run import validate_run_dir


class ManifestTests(unittest.TestCase):
    def test_file_identity_and_atomic_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "input.txt"
            source_bytes = "章鱼号\n".encode("utf-8")
            source.write_bytes(source_bytes)
            identity = file_identity(source)

            self.assertEqual(identity["size_bytes"], len(source_bytes))
            self.assertEqual(identity["sha256"], sha256_text("章鱼号\n"))

            target = root / "manifest.json"
            write_json_atomic(target, {"label": "同步基线"})
            self.assertEqual(
                json.loads(target.read_text(encoding="utf-8"))["label"],
                "同步基线",
            )
            self.assertFalse((root / "manifest.json.tmp").exists())

    def test_make_run_id_uses_utc_and_role_number(self) -> None:
        moment = datetime(2026, 8, 23, 6, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(
            make_run_id("vlm", 0, moment),
            "20260823T060000Z_phase0_vlm_000",
        )

    def test_llama_models_url_preserves_origin_and_prefix(self) -> None:
        self.assertEqual(
            llama_models_url("http://127.0.0.1:8080/v1/chat/completions"),
            "http://127.0.0.1:8080/v1/models",
        )
        self.assertEqual(
            llama_models_url("http://host:8080/prefix/v1/chat/completions"),
            "http://host:8080/prefix/v1/models",
        )

    def test_llm_prompt_matches_synchronous_baseline(self) -> None:
        tree = ast.parse((REPO_ROOT / "jetson" / "app.py").read_text(encoding="utf-8"))
        baseline_prompt = None
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            if any(
                isinstance(target, ast.Name) and target.id == "SYSTEM_PROMPT"
                for target in node.targets
            ):
                baseline_prompt = ast.literal_eval(node.value)
                break
        self.assertEqual(_SYSTEM_PROMPT, baseline_prompt)

    def test_runner_refuses_enabled_motion_before_touching_inputs(self) -> None:
        with mock.patch.dict(os.environ, {"ROBOT_ENABLE_MOTION": "1"}):
            with self.assertRaises(WorkloadError):
                run_once(Namespace())

    def test_runner_refuses_unknown_motion_value_before_touching_inputs(self) -> None:
        with mock.patch.dict(os.environ, {"ROBOT_ENABLE_MOTION": "typo"}):
            with self.assertRaisesRegex(WorkloadError, "unrecognized"):
                run_once(Namespace())


class RecorderBenchmarkTests(unittest.TestCase):
    def test_nearest_rank(self) -> None:
        values = list(range(1, 101))
        self.assertEqual(nearest_rank(values, 50), 50)
        self.assertEqual(nearest_rank(values, 99), 99)
        self.assertEqual(nearest_rank(values, 100), 100)

    def test_benchmark_writes_requested_number_of_events(self) -> None:
        result = run_benchmark(100)
        self.assertEqual(result["event_count"], 100)
        self.assertGreater(result["events_file_bytes"], 0)
        self.assertGreaterEqual(result["emit_max_ns"], result["emit_p99_ns"])


class AggregateTests(unittest.TestCase):
    def test_descriptive_stats(self) -> None:
        result = descriptive_stats([1.0, 2.0, 3.0])
        self.assertEqual(result["count"], 3)
        self.assertEqual(result["mean"], 2.0)
        self.assertEqual(result["median"], 2.0)
        self.assertEqual(result["range"], 2.0)
        self.assertEqual(result["sample_stddev"], 1.0)

    def test_llm_token_usage_uses_request_wall_duration(self) -> None:
        result = _llm_token_usage(
            "llm",
            {
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 25,
                    "total_tokens": 125,
                }
            },
            [
                {
                    "component": "llama",
                    "stage": "inference",
                    "duration_ms": 2000.0,
                }
            ],
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["completion_tokens"], 25)
        self.assertEqual(result["request_completion_tokens_per_second"], 12.5)
        self.assertEqual(result["request_ms_per_completion_token"], 80.0)
        self.assertIn("not decode-only", result["rate_basis"])

    def test_non_llm_has_no_token_usage(self) -> None:
        self.assertIsNone(_llm_token_usage("asr", {"usage": {}}, []))

    def test_aggregate_includes_llm_token_metrics(self) -> None:
        summary = {
            "run_id": "20260823T060000Z_phase0_llm_001",
            "workload": "llm",
            "sample_role": "measured",
            "residency_policy": "test_policy",
            "baseline_commit": "61db058",
            "runner_git": {"commit": "test-runner-commit", "dirty": False},
            "input_sha256": "test-input-sha256",
            "result": {
                "text_sha256": "test-output-sha256",
                "output_chars": 10,
                "token_usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 25,
                    "total_tokens": 125,
                    "request_completion_tokens_per_second": 12.5,
                    "request_ms_per_completion_token": 80.0,
                },
            },
            "timing": {
                "experiment_duration_ms": 2200.0,
                "stages": [
                    {
                        "component": "llama",
                        "stage": "inference",
                        "duration_ms": 2000.0,
                    }
                ],
            },
            "resources": {
                field: {"min": 1.0, "mean": 2.0, "max": 3.0}
                for field in (
                    "cpu_mean_across_cores_pct",
                    "gr3d_usage_pct",
                    "ram_used_mb",
                    "temperature_all_sensors_c",
                    "vdd_in_mw",
                )
            }
            | {"sample_count": 1},
        }
        with mock.patch(
            "experiments.phase0.aggregate_runs.summarize_run_dir",
            return_value=summary,
        ):
            result = aggregate_run_dirs(["simulated-run"])

        usage = result["outputs"]["token_usage"]
        self.assertEqual(usage["completion_tokens"]["mean"], 25.0)
        self.assertEqual(
            usage["request_completion_tokens_per_second"]["mean"], 12.5
        )
        self.assertEqual(result["runs"][0]["token_usage"]["total_tokens"], 125)

    def test_aggregate_rejects_mixed_vlm_translation_routes(self) -> None:
        base = {
            "workload": "vlm",
            "sample_role": "measured",
            "residency_policy": "test_policy",
            "baseline_commit": "61db058",
            "input_sha256": "test-input-sha256",
        }
        qwen = base | {"result": {"translation_route": "qwen"}}
        argos = base | {"result": {"translation_route": "argos"}}
        with mock.patch(
            "experiments.phase0.aggregate_runs.summarize_run_dir",
            side_effect=[qwen, argos],
        ):
            with self.assertRaisesRegex(ValueError, "translation route"):
                aggregate_run_dirs(["qwen-run", "argos-run"])

    def test_aggregate_rejects_mixed_runner_commits(self) -> None:
        base = {
            "workload": "llm",
            "sample_role": "measured",
            "residency_policy": "test_policy",
            "baseline_commit": "61db058",
            "input_sha256": "test-input-sha256",
            "result": {"translation_route": None},
            "timing": {"stages": []},
        }
        first = base | {"runner_git": {"commit": "runner-a"}}
        second = base | {"runner_git": {"commit": "runner-b"}}
        with mock.patch(
            "experiments.phase0.aggregate_runs.summarize_run_dir",
            side_effect=[first, second],
        ):
            with self.assertRaisesRegex(ValueError, "runner commit"):
                aggregate_run_dirs(["first-run", "second-run"])


class VlmTraceTests(unittest.TestCase):
    def test_vlm_records_import_and_qwen_stages(self) -> None:
        fake_module = types.ModuleType("jetson.vision_vlm")
        fake_module.ask_moondream_english = lambda path: "a robot"
        fake_module.make_speech_friendly = lambda chinese, english: chinese
        fake_module.translate_en_to_zh = lambda text: "一个机器人"
        fake_module.translate_with_qwen = lambda text: "一台机器人"
        fake_module.unload_moondream = lambda: None

        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir)
            run_id = "20260823T060000Z_phase0_vlm_000"
            with (
                mock.patch.dict(sys.modules, {"jetson.vision_vlm": fake_module}),
                EventRecorder(run_dir, run_id) as recorder,
            ):
                result = _run_vlm(
                    Path("fixed-image.png"), recorder, io.StringIO()
                )

            events = [
                json.loads(line)
                for line in (run_dir / "events.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]

        self.assertEqual(result["translation_route"], "qwen")
        names = [(item["component"], item["event"]) for item in events]
        self.assertIn(("vlm_runtime", "module_import.start"), names)
        self.assertIn(("vlm_runtime", "module_import.end"), names)
        self.assertIn(("qwen", "rewrite.start"), names)
        self.assertIn(("qwen", "rewrite.end"), names)
        self.assertNotIn(("argos", "inference.start"), names)

    def test_vlm_records_qwen_timeout_and_argos_fallback(self) -> None:
        fake_module = types.ModuleType("jetson.vision_vlm")
        fake_module.ask_moondream_english = lambda path: "a robot"
        fake_module.make_speech_friendly = lambda chinese, english: chinese
        fake_module.translate_en_to_zh = lambda text: "一个机器人"

        def time_out(text):
            raise TimeoutError("timed out")

        fake_module.translate_with_qwen = time_out
        fake_module.unload_moondream = lambda: None

        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir)
            run_id = "20260823T060000Z_phase0_vlm_000"
            with (
                mock.patch.dict(sys.modules, {"jetson.vision_vlm": fake_module}),
                EventRecorder(run_dir, run_id) as recorder,
            ):
                result = _run_vlm(
                    Path("fixed-image.png"), recorder, io.StringIO()
                )

            events = [
                json.loads(line)
                for line in (run_dir / "events.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]

        self.assertEqual(result["translation_route"], "argos")
        qwen_end = next(
            item
            for item in events
            if item["component"] == "qwen" and item["event"] == "rewrite.end"
        )
        self.assertEqual(qwen_end["status"], "error")
        self.assertEqual(qwen_end["details"]["error_type"], "TimeoutError")
        names = [(item["component"], item["event"]) for item in events]
        self.assertIn(("argos", "inference.start"), names)
        self.assertIn(("argos", "inference.end"), names)


class RunValidationTests(unittest.TestCase):
    def _create_valid_run(self, root: Path) -> Path:
        run_id = "20260823T060000Z_phase0_llm_001"
        run_dir = root / run_id
        run_dir.mkdir()
        write_json_atomic(
            run_dir / "manifest.json",
            {
                "schema_version": "0.1.0",
                "run_id": run_id,
                "workload": "llm",
                "sample_role": "measured",
                "residency_policy": "test_policy",
                "baseline_commit": "61db058",
                "status": "completed",
                "safety": {"motion_enabled": False},
                "input": {"sha256": "test-input-sha256"},
                "environment": {"git": {"commit": "test-runner-commit"}},
            },
        )
        write_json_atomic(
            run_dir / "result.json",
            {"schema_version": "0.1.0", "run_id": run_id, "status": "ok"},
        )
        (run_dir / "stdout.log").write_text("ok\n", encoding="utf-8")

        with EventRecorder(run_dir, run_id) as recorder:
            recorder.emit(
                task_id="llm-task",
                event="experiment.start",
                component="runner",
                status="started",
            )
            recorder.emit(
                task_id="llm-task",
                event="experiment.end",
                component="runner",
                status="ok",
            )

        with (run_dir / "resources.csv").open(
            "w", encoding="utf-8", newline=""
        ) as stream:
            writer = csv.DictWriter(stream, fieldnames=RESOURCE_FIELDS)
            writer.writeheader()
            row = {field: "" for field in RESOURCE_FIELDS}
            row["sample_monotonic_ns"] = 1
            row["sample_wall_time_ns"] = 2
            row["parse_error"] = ""
            row["raw_line"] = "sample"
            writer.writerow(row)
        return run_dir

    def test_accepts_complete_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = self._create_valid_run(Path(temp_dir))
            self.assertEqual(validate_run_dir(run_dir), [])

            summary = summarize_run_dir(run_dir)
            self.assertEqual(summary["run_id"], run_dir.name)
            self.assertEqual(summary["resources"]["sample_count"], 1)
            self.assertIsNotNone(summary["timing"]["experiment_duration_ms"])

    def test_rejects_missing_end_event(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = self._create_valid_run(Path(temp_dir))
            lines = (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
            (run_dir / "events.jsonl").write_text(lines[0] + "\n", encoding="utf-8")
            errors = validate_run_dir(run_dir)
            self.assertTrue(any("experiment.end" in error for error in errors))


class SimulatedRunnerTests(unittest.TestCase):
    def test_full_run_lifecycle_without_real_jetson_services(self) -> None:
        class FakeSampler:
            def __init__(self, run_dir, interval_ms):
                self.run_dir = Path(run_dir)
                self.interval_ms = interval_ms
                self.error = None

            def start(self):
                with (self.run_dir / "resources.csv").open(
                    "w", encoding="utf-8", newline=""
                ) as stream:
                    writer = csv.DictWriter(stream, fieldnames=RESOURCE_FIELDS)
                    writer.writeheader()
                    row = {field: "" for field in RESOURCE_FIELDS}
                    row["sample_monotonic_ns"] = 1
                    row["sample_wall_time_ns"] = 2
                    row["parse_error"] = ""
                    row["raw_line"] = "simulated"
                    writer.writerow(row)

            def stop(self):
                return None

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_path = root / "prompt.txt"
            input_path.write_bytes("固定提示词\n".encode("utf-8"))
            args = Namespace(
                workload="llm",
                input=input_path,
                repetition=1,
                run_root=root / "runs",
                resource_interval_ms=200,
                timeout_seconds=300,
            )

            with (
                mock.patch.dict(os.environ, {"ROBOT_ENABLE_MOTION": "0"}),
                mock.patch(
                    "experiments.phase0.run_workload.collect_environment",
                    return_value={"git": {"commit": "61db058"}},
                ),
                mock.patch(
                    "experiments.phase0.run_workload.collect_workload_metadata",
                    return_value={"model": "simulated"},
                ),
                mock.patch(
                    "experiments.phase0.run_workload.TegrastatsSampler",
                    FakeSampler,
                ),
                mock.patch(
                    "experiments.phase0.run_workload.execute_workload",
                    return_value={"text": "模拟结果", "output_chars": 4},
                ),
                mock.patch("experiments.phase0.run_workload.time.sleep"),
            ):
                run_dir = run_once(args)

            self.assertEqual(validate_run_dir(run_dir), [])
            manifest = json.loads(
                (run_dir / "manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["status"], "completed")
            self.assertEqual(manifest["baseline_commit"], "61db058")
            self.assertEqual(manifest["sample_role"], "measured")


if __name__ == "__main__":
    unittest.main()
