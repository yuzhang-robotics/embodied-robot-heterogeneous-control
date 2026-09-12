from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from experiments.phase2.residency.prefetch import (
    PREFETCH_ACTION_SCHEMA_VERSION,
    PrefetchInputError,
    run_sequential_prefetch,
    validate_prefetch_action_record,
)


def _blocking_worker(connection, path, expected_size_bytes, buffer_size_bytes):
    del path, expected_size_bytes, buffer_size_bytes
    try:
        time.sleep(10)
    finally:
        connection.close()


class SequentialPrefetchTests(unittest.TestCase):
    def test_spawned_child_reads_exact_file_and_records_no_private_data(self) -> None:
        payload = (b"phase2-prefetch-payload" * 4096) + b"tail"
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "private-model-name.bin"
            path.write_bytes(payload)
            record = run_sequential_prefetch(
                path,
                expected_size_bytes=len(payload),
                buffer_size_bytes=64 * 1024,
                timeout_s=5.0,
            )

            value = record.to_dict()
            self.assertEqual(value["prefetch_action_schema_version"], "0.1.0")
            self.assertEqual(value["status"], "ok", value)
            self.assertEqual(value["bytes_read"], len(payload))
            self.assertEqual(value["chunk_count"], 2)
            self.assertEqual(value["child_exit_code"], 0)
            self.assertTrue(value["child_protocol_complete"])
            self.assertTrue(value["child_joined"])
            self.assertFalse(value["terminate_requested"])
            self.assertFalse(value["kill_requested"])
            self.assertFalse(value["path_recorded"])
            self.assertFalse(value["contents_recorded"])
            self.assertFalse(value["pid_recorded"])
            serialized = json.dumps(value, sort_keys=True)
            self.assertNotIn(str(path), serialized)
            self.assertNotIn("private-model-name", serialized)
            self.assertNotIn(payload[:20].decode("ascii"), serialized)

    def test_timeout_terminates_and_reaps_owned_child(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "model.bin"
            path.write_bytes(b"bounded")
            record = run_sequential_prefetch(
                path,
                expected_size_bytes=7,
                buffer_size_bytes=4,
                timeout_s=0.05,
                _worker=_blocking_worker,
            )

        value = record.to_dict()
        self.assertEqual(value["status"], "timeout")
        self.assertEqual(value["error_code"], "timeout")
        self.assertTrue(value["terminate_requested"])
        self.assertTrue(value["child_joined"])
        self.assertFalse(value["child_protocol_complete"])

    def test_input_mismatch_fails_before_process_start(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "model.bin"
            path.write_bytes(b"1234")
            with self.assertRaises(PrefetchInputError):
                run_sequential_prefetch(path, expected_size_bytes=5)

    def test_missing_and_empty_targets_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with self.assertRaises(PrefetchInputError):
                run_sequential_prefetch(root / "missing.bin", expected_size_bytes=1)
            empty = root / "empty.bin"
            empty.touch()
            with self.assertRaises(PrefetchInputError):
                run_sequential_prefetch(empty, expected_size_bytes=0)

    def test_action_configuration_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "model.bin"
            path.write_bytes(b"x")
            with self.assertRaises(PrefetchInputError):
                run_sequential_prefetch(
                    path,
                    expected_size_bytes=1,
                    buffer_size_bytes=16 * 1024 * 1024 + 1,
                )
            with self.assertRaises(PrefetchInputError):
                run_sequential_prefetch(
                    path,
                    expected_size_bytes=1,
                    timeout_s=120.1,
                )

    def test_record_validator_rejects_success_tampering(self) -> None:
        value = {
            "prefetch_action_schema_version": PREFETCH_ACTION_SCHEMA_VERSION,
            "status": "ok",
            "expected_size_bytes": 8,
            "buffer_size_bytes": 4,
            "timeout_ms": 1000.0,
            "action_started_monotonic_ns": 10,
            "child_started_monotonic_ns": 11,
            "read_completed_monotonic_ns": 12,
            "child_finished_monotonic_ns": 13,
            "action_finished_monotonic_ns": 14,
            "bytes_read": 8,
            "chunk_count": 2,
            "child_exit_code": 0,
            "child_protocol_complete": True,
            "terminate_requested": False,
            "kill_requested": False,
            "child_joined": True,
            "path_recorded": False,
            "contents_recorded": False,
            "pid_recorded": False,
            "error_code": None,
        }
        validate_prefetch_action_record(value)
        value["bytes_read"] = 7
        with self.assertRaises(ValueError):
            validate_prefetch_action_record(value)


if __name__ == "__main__":
    unittest.main()
