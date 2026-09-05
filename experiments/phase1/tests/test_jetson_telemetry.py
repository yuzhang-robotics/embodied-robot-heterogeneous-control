from __future__ import annotations

import copy
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from experiments.phase1.jetson_telemetry import (
    TegrastatsSampler,
    load_resource_samples,
    parse_tegrastats_line,
    summarize_resource_samples,
    validate_resource_samples,
)


TEGRASTATS_SAMPLE = (
    "08-23-2026 13:57:46 RAM 3595/7607MB (lfb 1x1MB) "
    "SWAP 11/3804MB (cached 0MB) CPU [1%@729,off,3%@729,4%@729,5%@729,6%@729] "
    "EMC_FREQ 2%@2133 GR3D_FREQ 7%@[306] cpu@51C soc2@50.562C "
    "soc0@51.031C gpu@50.5C tj@51C soc1@50C "
    "VDD_IN 5832mW/5852mW VDD_CPU_GPU_CV 1307mW/1327mW "
    "VDD_SOC 1468mW/1468mW"
)


def sampler_command(line: str = TEGRASTATS_SAMPLE) -> list[str]:
    code = (
        "import time\n"
        f"line = {line!r}\n"
        "while True:\n"
        "    print(line, flush=True)\n"
        "    time.sleep(0.005)\n"
    )
    return [sys.executable, "-u", "-c", code]


class JetsonTelemetryTests(unittest.TestCase):
    def test_parser_supports_dynamic_sensors_and_offline_cores(self) -> None:
        sample = parse_tegrastats_line(
            TEGRASTATS_SAMPLE,
            sequence=0,
            sample_monotonic_ns=100,
            sample_wall_time_ns=200,
        )

        self.assertEqual(sample["parse_errors"], [])
        self.assertEqual(sample["tegrastats_time"], "08-23-2026 13:57:46")
        self.assertEqual(sample["ram"]["used_mb"], 3595)
        self.assertFalse(sample["cpu"][1]["online"])
        self.assertEqual(sample["gr3d"]["frequencies_mhz"], [306])
        self.assertEqual(sample["temperatures_c"]["soc2"], 50.562)
        self.assertEqual(sample["power"]["VDD_IN"]["instant_mw"], 5832)

    def test_missing_optional_timestamp_is_a_warning(self) -> None:
        sample = parse_tegrastats_line(
            "RAM " + TEGRASTATS_SAMPLE.split(" RAM ", 1)[1],
            sequence=0,
            sample_monotonic_ns=100,
            sample_wall_time_ns=200,
        )

        self.assertEqual(sample["parse_errors"], [])
        self.assertIn("tegrastats_timestamp_missing", sample["parse_warnings"])

    def test_validator_rejects_tampered_structure_and_ranges(self) -> None:
        sample = parse_tegrastats_line(
            TEGRASTATS_SAMPLE,
            sequence=0,
            sample_monotonic_ns=100,
            sample_wall_time_ns=200,
        )
        tampered_range = copy.deepcopy(sample)
        tampered_range["gr3d"]["usage_pct"] = 101
        tampered_shape = copy.deepcopy(sample)
        tampered_shape["unexpected"] = True

        self.assertTrue(
            any(
                "GR3D usage" in error
                for error in validate_resource_samples([tampered_range])
            )
        )
        self.assertTrue(
            any(
                "fields do not match" in error
                for error in validate_resource_samples([tampered_shape])
            )
        )

    def test_sampler_stops_process_and_non_daemon_reader(self) -> None:
        baseline_threads = {thread.name for thread in threading.enumerate()}
        with tempfile.TemporaryDirectory() as temp_dir:
            sampler = TegrastatsSampler(
                Path(temp_dir),
                50,
                command=sampler_command(),
            )
            sampler.start(first_sample_timeout_s=2)
            time.sleep(0.04)
            report = sampler.stop()
            samples = load_resource_samples(Path(temp_dir) / "resources.jsonl")

        self.assertTrue(report.successful)
        self.assertTrue(report.reader_joined)
        self.assertGreaterEqual(report.sample_count, 2)
        self.assertEqual(validate_resource_samples(samples), [])
        resource_summary = summarize_resource_samples(samples)
        self.assertEqual(resource_summary["sample_count"], len(samples))
        remaining_threads = {thread.name for thread in threading.enumerate()}
        self.assertEqual(remaining_threads, baseline_threads)

    def test_sampler_waits_until_the_trace_covers_a_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            sampler = TegrastatsSampler(
                Path(temp_dir),
                50,
                command=sampler_command(),
            )
            sampler.start(first_sample_timeout_s=2)
            boundary_ns = time.monotonic_ns() + 20_000_000
            observed_ns = sampler.wait_for_sample_at_or_after(
                boundary_ns,
                timeout_s=2,
            )
            report = sampler.stop()

        self.assertGreaterEqual(observed_ns, boundary_ns)
        self.assertGreaterEqual(report.last_sample_monotonic_ns, boundary_ns)
        self.assertTrue(report.successful)

    def test_sampler_boundary_wait_rejects_an_early_exit(self) -> None:
        code = f"print({TEGRASTATS_SAMPLE!r}, flush=True)"
        with tempfile.TemporaryDirectory() as temp_dir:
            sampler = TegrastatsSampler(
                Path(temp_dir),
                50,
                command=[sys.executable, "-u", "-c", code],
            )
            sampler.start(first_sample_timeout_s=2)
            time.sleep(0.05)
            with self.assertRaisesRegex(RuntimeError, "exited before covering"):
                sampler.wait_for_sample_at_or_after(
                    time.monotonic_ns() + 1,
                    timeout_s=0.1,
                )
            report = sampler.stop()

        self.assertFalse(report.successful)

    def test_sampler_rejects_an_unparseable_first_sample(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            sampler = TegrastatsSampler(
                Path(temp_dir),
                50,
                command=sampler_command("unexpected output"),
            )
            with self.assertRaisesRegex(RuntimeError, "parser contract"):
                sampler.start(first_sample_timeout_s=2)

        self.assertIsNotNone(sampler.stop_report)
        self.assertFalse(sampler.stop_report.successful)
        self.assertGreater(sampler.stop_report.parse_error_count, 0)

    def test_sampler_rejects_an_early_process_exit(self) -> None:
        code = f"print({TEGRASTATS_SAMPLE!r}, flush=True)"
        with tempfile.TemporaryDirectory() as temp_dir:
            sampler = TegrastatsSampler(
                Path(temp_dir),
                50,
                command=[sys.executable, "-u", "-c", code],
            )
            sampler.start(first_sample_timeout_s=2)
            time.sleep(0.05)
            report = sampler.stop()

        self.assertEqual(report.stop_method, "exited")
        self.assertFalse(report.successful)


if __name__ == "__main__":
    unittest.main()
