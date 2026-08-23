"""Host-side tests for Phase 0 telemetry helpers."""

import json
import tempfile
import unittest
from pathlib import Path

from .telemetry import EventRecorder, parse_tegrastats_line


TEGRASTATS_SAMPLE = (
    "08-23-2026 13:57:46 RAM 3595/7607MB (lfb 1x1MB) "
    "SWAP 11/3804MB (cached 0MB) "
    "CPU [100%@1728,1%@1728,3%@1728,0%@1728,1%@729,1%@729] "
    "GR3D_FREQ 0% cpu@51C soc2@50.562C soc0@51.031C "
    "gpu@50.625C tj@51.781C soc1@51.781C "
    "VDD_IN 5832mW/5852mW VDD_CPU_GPU_CV 1307mW/1327mW "
    "VDD_SOC 1468mW/1468mW"
)


class EventRecorderTests(unittest.TestCase):
    def test_writes_ordered_utf8_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            run_id = "20260823T060000Z_phase0_asr_001"
            run_dir = Path(temp_dir) / run_id

            with EventRecorder(run_dir, run_id) as recorder:
                first = recorder.emit(
                    task_id="asr-001",
                    event="experiment.start",
                    component="runner",
                    status="started",
                    details={"label": "固定中文音频"},
                )
                second = recorder.emit(
                    task_id="asr-001",
                    event="inference.start",
                    component="whisper",
                    status="started",
                )
                third = recorder.emit(
                    task_id="asr-001",
                    event="inference.end",
                    component="whisper",
                    status="ok",
                )

            lines = (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
            events = [json.loads(line) for line in lines]

            self.assertEqual([item["seq"] for item in events], [0, 1, 2])
            self.assertEqual(events[0]["details"]["label"], "固定中文音频")
            self.assertLessEqual(
                first["monotonic_ns"], second["monotonic_ns"]
            )
            self.assertLessEqual(
                second["monotonic_ns"], third["monotonic_ns"]
            )

    def test_rejects_invalid_run_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(ValueError):
                EventRecorder(Path(temp_dir) / "bad", "phase0-asr")

    def test_refuses_to_overwrite_existing_trace(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            run_id = "20260823T060000Z_phase0_llm_001"
            run_dir = Path(temp_dir) / run_id
            run_dir.mkdir()
            (run_dir / "manifest.json").write_text("{}\n", encoding="utf-8")

            with EventRecorder(run_dir, run_id):
                pass

            with self.assertRaises(FileExistsError):
                EventRecorder(run_dir, run_id)


class TegrastatsParserTests(unittest.TestCase):
    def test_parses_validated_jetson_line(self) -> None:
        row = parse_tegrastats_line(TEGRASTATS_SAMPLE)

        self.assertEqual(row["parse_error"], "")
        self.assertEqual(row["tegrastats_time"], "08-23-2026 13:57:46")
        self.assertEqual(row["ram_used_mb"], 3595)
        self.assertEqual(row["ram_total_mb"], 7607)
        self.assertEqual(row["ram_lfb_count"], 1)
        self.assertEqual(row["ram_lfb_size_mb"], 1)
        self.assertEqual(row["swap_used_mb"], 11)
        self.assertEqual(row["cpu0_usage_pct"], 100)
        self.assertEqual(row["cpu5_freq_mhz"], 729)
        self.assertEqual(row["gr3d_usage_pct"], 0)
        self.assertEqual(row["temp_cpu_c"], 51.0)
        self.assertEqual(row["temp_gpu_c"], 50.625)
        self.assertEqual(row["vdd_in_mw"], 5832)
        self.assertEqual(row["vdd_in_avg_mw"], 5852)
        self.assertEqual(row["vdd_cpu_gpu_cv_mw"], 1307)
        self.assertEqual(row["vdd_soc_avg_mw"], 1468)

    def test_normalizes_kilobyte_lfb_sizes_observed_under_memory_pressure(self) -> None:
        observed_formats = [
            ("1x512kB", 1, 0.5),
            ("6x256kB", 6, 0.25),
            ("205x128kB", 205, 0.125),
            ("1311x64kB", 1311, 0.0625),
        ]

        for lfb, expected_count, expected_size_mb in observed_formats:
            with self.subTest(lfb=lfb):
                line = TEGRASTATS_SAMPLE.replace("1x1MB", lfb)
                row = parse_tegrastats_line(line)

                self.assertEqual(row["parse_error"], "")
                self.assertEqual(row["ram_lfb_count"], expected_count)
                self.assertEqual(row["ram_lfb_size_mb"], expected_size_mb)

    def test_reports_missing_sections(self) -> None:
        row = parse_tegrastats_line("unexpected output")
        self.assertIn("timestamp", row["parse_error"])
        self.assertIn("ram", row["parse_error"])
        self.assertIn("temperature=", row["parse_error"])
        self.assertIn("power=", row["parse_error"])
        self.assertEqual(row["raw_line"], "unexpected output")


if __name__ == "__main__":
    unittest.main()
