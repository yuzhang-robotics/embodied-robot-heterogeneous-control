from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from experiments.phase1.carryover.observation import (
    OBSERVATION_SCHEMA_VERSION,
    ObservationError,
    memory_observation,
    observe_file_residency,
    parse_meminfo,
    parse_proc_stat,
    read_process_counters,
    validate_memory_observation,
    validate_process_observation,
)


class CarryoverObservationTests(unittest.TestCase):
    def test_meminfo_requires_all_fields_and_converts_kibibytes(self) -> None:
        parsed = parse_meminfo(
            "MemTotal: 100 kB\n"
            "MemAvailable: 90 kB\n"
            "Cached: 20 kB\n"
            "SReclaimable: 3 kB\n"
        )
        self.assertEqual(parsed["MemAvailable_bytes"], 90 * 1024)
        self.assertEqual(parsed["Cached_bytes"], 20 * 1024)
        self.assertEqual(parsed["SReclaimable_bytes"], 3 * 1024)

        with self.assertRaisesRegex(ObservationError, "missing"):
            parse_meminfo("MemAvailable: 90 kB\nCached: 20 kB\n")

    def test_mincore_vector_is_counted_without_recording_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "model.bin"
            target.write_bytes(b"x" * 5_000)
            calls: list[tuple[int, int, int]] = []

            def probe(address: int, length: int, pages: int) -> bytes:
                calls.append((address, length, pages))
                return bytes([1, 0])

            observed = observe_file_residency(
                target,
                platform_name="linux",
                page_size=4096,
                mincore_probe=probe,
            )

        self.assertEqual(calls[0][1:], (5_000, 2))
        self.assertEqual(observed["resident_pages"], 1)
        self.assertEqual(observed["resident_fraction"], 0.5)
        self.assertFalse(observed["path_recorded"])

    def test_mincore_rejects_wrong_platform_and_vector_length(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "model.bin"
            target.write_bytes(b"x")
            with self.assertRaisesRegex(ObservationError, "requires Linux"):
                observe_file_residency(target, platform_name="win32")
            with self.assertRaisesRegex(ObservationError, "invalid residency vector"):
                observe_file_residency(
                    target,
                    platform_name="linux",
                    page_size=4096,
                    mincore_probe=lambda *_args: b"",
                )

    def test_memory_observation_fails_closed_on_invalid_residency(self) -> None:
        value = memory_observation(
            "fixture",
            meminfo_reader=lambda: {
                "MemAvailable_bytes": 1,
                "Cached_bytes": 2,
                "SReclaimable_bytes": 3,
            },
            residency_reader=lambda _path: {
                "method": "non_touching_mmap_mincore",
                "file_size_bytes": 4096,
                "page_size_bytes": 4096,
                "total_pages": 1,
                "resident_pages": 1,
                "resident_fraction": 1.0,
                "path_recorded": False,
            },
            clock_ns=lambda: 10,
        )
        validate_memory_observation(value)
        self.assertEqual(
            value["observation_schema_version"], OBSERVATION_SCHEMA_VERSION
        )

        value["whisper_model_residency"]["resident_fraction"] = 0.5
        with self.assertRaisesRegex(ObservationError, "values"):
            validate_memory_observation(value)

    def test_proc_stat_and_status_counters_are_parsed(self) -> None:
        fields = ["S"] + ["0"] * 21
        fields[7] = "11"
        fields[9] = "2"
        fields[11] = "25"
        fields[12] = "5"
        fields[21] = "7"
        parsed = parse_proc_stat(
            "123 (whisper cli) " + " ".join(fields),
            clock_ticks_per_second=10,
        )
        self.assertEqual(parsed["user_time_s"], 2.5)
        self.assertEqual(parsed["system_time_s"], 0.5)
        self.assertEqual(parsed["minor_faults"], 11)
        self.assertEqual(parsed["major_faults"], 2)

        with tempfile.TemporaryDirectory() as temp_dir:
            proc = Path(temp_dir) / "123"
            proc.mkdir()
            (proc / "stat").write_text(
                "123 (whisper cli) " + " ".join(fields), encoding="ascii"
            )
            (proc / "status").write_text(
                "VmHWM:\t8 kB\n"
                "voluntary_ctxt_switches:\t3\n"
                "nonvoluntary_ctxt_switches:\t4\n",
                encoding="ascii",
            )
            counters = read_process_counters(
                123,
                proc_root=temp_dir,
                page_size=4096,
                clock_ticks_per_second=10,
            )
        self.assertEqual(counters["rss_bytes"], 7 * 4096)
        self.assertEqual(counters["high_water_rss_bytes"], 8 * 1024)
        self.assertNotIn("filesystem_read_bytes", counters)

    def test_process_observation_rejects_missing_or_invalid_metrics(self) -> None:
        report = {
            "observation_schema_version": OBSERVATION_SCHEMA_VERSION,
            "method": "sampled_linux_proc",
            "sample_interval_ms": 10.0,
            "sample_count": 2,
            "read_error_count": 0,
            "process_exit_observed": True,
            "user_time_s": 1.0,
            "system_time_s": 0.5,
            "minor_faults": 2,
            "major_faults": 0,
            "maximum_rss_bytes": 10,
            "voluntary_context_switches": 1,
            "involuntary_context_switches": 1,
            "pid_recorded": False,
            "command_recorded": False,
        }
        validate_process_observation(report)
        del report["major_faults"]
        with self.assertRaisesRegex(ObservationError, "major_faults"):
            validate_process_observation(report)


if __name__ == "__main__":
    unittest.main()
