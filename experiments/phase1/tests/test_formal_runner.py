from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from experiments.phase1.formal_protocol import (
    DEFAULT_PROTOCOL_PATH,
    FORMAL_COLLECTION_STATUS,
)
from experiments.phase1.jetson_telemetry import TegrastatsSampler
from experiments.phase1.run_formal_session import (
    FormalSessionError,
    ThermalMonitor,
    _check_collection_order,
    _services_changed,
    run_session,
)
from experiments.phase1.tests.formal_fixture import passing_formal_preflight
from jetson.phase1_runtime import PayloadRef


TEGRASTATS_SAMPLE = (
    "09-02-2026 10:00:00 RAM 3000/7607MB (lfb 4x4MB) "
    "SWAP 0/3804MB (cached 0MB) CPU [1%@729,2%@729,3%@729,4%@729,5%@729,6%@729] "
    "EMC_FREQ 2%@2133 GR3D_FREQ 7%@[306] cpu@50C gpu@50C tj@50C "
    "VDD_IN 5800mW/5800mW"
)


def sampler_factory(session_dir: Path, interval_ms: int, **kwargs: object) -> object:
    code = (
        "import time\n"
        f"line = {TEGRASTATS_SAMPLE!r}\n"
        "while True:\n"
        "    print(line, flush=True)\n"
        "    time.sleep(0.002)\n"
    )
    return TegrastatsSampler(
        session_dir,
        interval_ms,
        command=[sys.executable, "-u", "-c", code],
        **kwargs,
    )


def payload(media_type: str) -> PayloadRef:
    return PayloadRef(
        ref="fixture://formal-input",
        sha256="a" * 64,
        size_bytes=1,
        media_type=media_type,
    )


class FormalRunnerTests(unittest.TestCase):
    def test_default_formal_collection_is_closed_after_v3_failure(self) -> None:
        self.assertEqual(
            FORMAL_COLLECTION_STATUS,
            "closed_after_system_under_test_failure",
        )

    def test_thermal_monitor_requires_consecutive_cool_samples_and_stops_high(
        self,
    ) -> None:
        monitor = ThermalMonitor(stop_tj_c=85.0)
        for sequence in range(10):
            monitor.observe(
                {
                    "seq": sequence,
                    "temperatures_c": {"tj": 50.0},
                    "parse_errors": [],
                }
            )

        gate = monitor.wait_below(
            maximum_tj_c=55.0,
            consecutive_samples=10,
            timeout_s=0.1,
        )
        self.assertEqual(gate["first_sequence"], 0)
        self.assertEqual(gate["last_sequence"], 9)
        self.assertFalse(monitor.stop_requested.is_set())

        monitor.observe(
            {
                "seq": 10,
                "temperatures_c": {"tj": 85.0},
                "parse_errors": [],
            }
        )
        self.assertTrue(monitor.stop_requested.is_set())

    def test_replacement_requires_a_recorded_infrastructure_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            collection = Path(temp_dir)
            attempt_dir = collection / "session-01-attempt-01"
            attempt_dir.mkdir()
            manifest_path = attempt_dir / "manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "protocol_session": "session-01",
                        "status": "aborted",
                        "failure_class": "system_under_test",
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                FormalSessionError, "system-under-test failures"
            ):
                _check_collection_order(
                    collection,
                    session_index=1,
                    attempt=2,
                    minimum_separation_minutes=30,
                    now=datetime.now(timezone.utc),
                    replacement_for="session-01-attempt-01",
                    infrastructure_failure="resource_sampler_failure",
                )

            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["failure_class"] = "infrastructure"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            result = _check_collection_order(
                collection,
                session_index=1,
                attempt=2,
                minimum_separation_minutes=30,
                now=datetime.now(timezone.utc),
                replacement_for="session-01-attempt-01",
                infrastructure_failure="resource_sampler_failure",
            )
            self.assertIsNone(result)

    def test_service_restart_requires_both_process_identities_to_change(self) -> None:
        previous_preflight = passing_formal_preflight(service_suffix="old")
        previous = {
            "preflight": {"service_identity": previous_preflight["service_identity"]}
        }
        unchanged = passing_formal_preflight(service_suffix="old")
        changed = passing_formal_preflight(service_suffix="new")
        one_changed = passing_formal_preflight(service_suffix="new")
        one_changed["service_identity"]["ollama"] = previous_preflight[
            "service_identity"
        ]["ollama"]

        self.assertFalse(_services_changed(previous, unchanged))
        self.assertFalse(_services_changed(previous, one_changed))
        self.assertTrue(_services_changed(previous, changed))

    def test_full_injected_session_preserves_the_frozen_order(self) -> None:
        observed: list[dict[str, object]] = []
        tail_boundaries: list[int] = []

        def preflight_builder(*args: object, **kwargs: object) -> dict[str, object]:
            return passing_formal_preflight()

        def entry_runner(
            session_dir: Path,
            entry: dict[str, object],
            *,
            ordinal: int,
            **kwargs: object,
        ) -> tuple[Path, dict[str, object]]:
            observed.append(dict(entry))
            role = "warmups" if entry["role"] == "warmup" else "measured"
            run_dir = session_dir / role / f"{ordinal:03d}-fixture"
            run_dir.mkdir(parents=True)
            (run_dir / "events.jsonl").write_text("", encoding="utf-8")
            (run_dir / "run.json").write_text("{}\n", encoding="utf-8")
            return run_dir, {"valid": True}

        def idle_runner(
            session_dir: Path,
            *,
            label: str,
            **kwargs: object,
        ) -> tuple[Path, dict[str, object]]:
            started_ns = time.monotonic_ns()
            time.sleep(0.04)
            finished_ns = time.monotonic_ns()
            run_dir = session_dir / "idle" / label
            run_dir.mkdir(parents=True)
            (run_dir / "events.jsonl").write_text("", encoding="utf-8")
            record = {
                "started_monotonic_ns": started_ns,
                "finished_monotonic_ns": finished_ns,
                "valid": True,
            }
            (run_dir / "run.json").write_text(
                json.dumps(record) + "\n",
                encoding="utf-8",
            )
            return run_dir, record

        original_tail_wait = TegrastatsSampler.wait_for_sample_at_or_after

        def recording_tail_wait(
            sampler: TegrastatsSampler,
            monotonic_ns: int,
            *,
            timeout_s: float = 2.0,
        ) -> int:
            tail_boundaries.append(monotonic_ns)
            return original_tail_wait(
                sampler,
                monotonic_ns,
                timeout_s=timeout_s,
            )

        with tempfile.TemporaryDirectory() as temp_dir:
            args = argparse.Namespace(
                protocol=DEFAULT_PROTOCOL_PATH,
                session_index=1,
                attempt=1,
                collection_id="20260902T000000Z_phase1_formal_test",
                replacement_for=None,
                infrastructure_failure=None,
                output_root=Path(temp_dir),
                asr_input=Path("asr.wav"),
                llm_input=Path("llm.txt"),
                vlm_input=Path("vlm.jpg"),
                confirm_services_restarted=True,
                confirm_dynamic_dvfs=True,
                thermal_wait_timeout_s=2.0,
            )
            with mock.patch.object(
                TegrastatsSampler,
                "wait_for_sample_at_or_after",
                recording_tail_wait,
            ):
                session_dir = run_session(
                    args,
                    preflight_builder=preflight_builder,
                    sampler_factory=sampler_factory,
                    payloads_override={
                        "asr": payload("audio/wav"),
                        "llm": payload("text/plain"),
                        "vlm": payload("image/jpeg"),
                    },
                    entry_runner=entry_runner,
                    idle_runner=idle_runner,
                )
            manifest = json.loads(
                (session_dir / "manifest.json").read_text(encoding="utf-8")
            )
            post_idle = json.loads(
                (session_dir / "idle" / "post_measurement" / "run.json").read_text(
                    encoding="utf-8"
                )
            )
            ledger = [
                json.loads(line)
                for line in (session_dir / "ledger.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]

        self.assertEqual(len(observed), 41)
        self.assertEqual(
            [item["workload"] for item in observed[:5]],
            ["asr", "asr", "asr", "llm", "vlm"],
        )
        measured = [item for item in observed if item["role"] == "measured"]
        self.assertEqual(len(measured), 36)
        self.assertEqual([item["sequence"] for item in measured], list(range(1, 37)))
        self.assertEqual(manifest["status"], "completed")
        self.assertFalse(manifest["formal_evidence_eligible"])
        self.assertEqual(manifest["completed_entries"], 41)
        self.assertEqual(len(ledger), 86)
        self.assertEqual(tail_boundaries, [post_idle["finished_monotonic_ns"]])
        self.assertGreaterEqual(
            manifest["resource_sampler_report"]["last_sample_monotonic_ns"],
            post_idle["finished_monotonic_ns"],
        )


if __name__ == "__main__":
    unittest.main()
