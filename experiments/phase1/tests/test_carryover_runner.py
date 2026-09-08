from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

from experiments.phase1.carryover_protocol import DEFAULT_PROTOCOL_PATH
from experiments.phase1.jetson_telemetry import TegrastatsSampler
from experiments.phase1.run_carryover_session import (
    CarryoverSessionError,
    make_run_id,
    run_session,
    validate_fixed_interval,
    validate_primer_2,
)
from experiments.phase1.telemetry import EventRecorder
from experiments.phase1.tests.carryover_fixture import passing_carryover_preflight
from jetson.phase1_runtime import PayloadRef


TEGRASTATS_SAMPLE = (
    "09-07-2026 10:00:00 RAM 3000/7607MB (lfb 4x4MB) "
    "SWAP 0/3804MB (cached 0MB) CPU [1%@729,2%@729,3%@729,4%@729,5%@729,6%@729] "
    "EMC_FREQ 2%@2133 GR3D_FREQ 7%@[306] cpu@50C gpu@50C tj@50C "
    "VDD_IN 5800mW/5800mW"
)


def sampler_factory(
    session_dir: Path, interval_ms: int, **kwargs: object
) -> TegrastatsSampler:
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


def run_record(
    started: int, finished: int, *, duration_ms: float = 1_000.0
) -> dict[str, object]:
    return {
        "valid": True,
        "adapter": {
            "started_monotonic_ns": started,
            "finished_monotonic_ns": finished,
            "duration_ns": int(duration_ms * 1_000_000),
        },
    }


class CarryoverRunnerTests(unittest.TestCase):
    def test_run_id_is_accepted_by_event_recorder(self) -> None:
        run_id = make_run_id(
            "asr",
            1,
            datetime(2026, 9, 7, 16, 58, 53, tzinfo=timezone.utc),
        )
        self.assertEqual(
            run_id,
            "20260907T165853Z_phase1_carryover_asr_001",
        )
        with tempfile.TemporaryDirectory() as temporary:
            recorder = EventRecorder(Path(temporary), run_id)
            recorder.close()

    def test_primer_and_fixed_interval_fail_closed(self) -> None:
        primer = run_record(1, 2_000_000_001, duration_ms=2_000.0)
        self.assertEqual(validate_primer_2(primer), 2_000.0)
        too_slow = run_record(1, 6_000_000_001, duration_ms=6_000.0)
        with self.assertRaisesRegex(CarryoverSessionError, "warm state"):
            validate_primer_2(too_slow)

        target = 2_000_000_001 + 150_000_000_000
        measured = run_record(target + 10_000_000, target + 1_010_000_000)
        timing = validate_fixed_interval(
            primer,
            measured,
            interposer_finished_monotonic_ns=target - 1,
        )
        self.assertEqual(timing["start_lateness_ms"], 10.0)

        early = run_record(target - 1, target + 1)
        with self.assertRaisesRegex(CarryoverSessionError, "before"):
            validate_fixed_interval(
                primer,
                early,
                interposer_finished_monotonic_ns=target - 1,
            )
        late = run_record(target + 251_000_000, target + 300_000_000)
        with self.assertRaisesRegex(CarryoverSessionError, "lateness"):
            validate_fixed_interval(
                primer,
                late,
                interposer_finished_monotonic_ns=target - 1,
            )
        with self.assertRaisesRegex(CarryoverSessionError, "exceeded"):
            validate_fixed_interval(
                primer,
                measured,
                interposer_finished_monotonic_ns=target + 1,
            )

    def test_injected_session_preserves_order_and_has_no_formal_claim(self) -> None:
        observed: list[tuple[int, str, str]] = []
        fake_now = [time.monotonic_ns()]

        def preflight_builder(*_args: object, **_kwargs: object) -> dict[str, object]:
            return passing_carryover_preflight()

        def invocation_runner(
            session_dir: Path,
            *,
            ordinal: int,
            unit_index: int,
            workload: str,
            diagnostic_role: str,
            not_before_monotonic_ns: int | None = None,
            **_kwargs: object,
        ) -> tuple[Path, dict[str, object]]:
            observed.append((unit_index, workload, diagnostic_role))
            started = (
                not_before_monotonic_ns + 10_000_000
                if not_before_monotonic_ns is not None
                else fake_now[0] + 1_000_000
            )
            duration_ms = 2_000.0 if workload == "asr" else 10_000.0
            finished = started + int(duration_ms * 1_000_000)
            fake_now[0] = finished
            run_dir = session_dir / "runs" / f"{ordinal:03d}-fixture"
            run_dir.mkdir(parents=True)
            (run_dir / "events.jsonl").write_text("", encoding="utf-8")
            record = run_record(started, finished, duration_ms=duration_ms)
            (run_dir / "run.json").write_text(json.dumps(record), encoding="utf-8")
            return run_dir, record

        def observation_reader(_path: Path | str) -> dict[str, object]:
            return {
                "observed_monotonic_ns": fake_now[0],
                "memory": {},
                "whisper_model_residency": {},
            }

        payloads = {
            name: PayloadRef(
                ref=f"fixture://{name}",
                sha256="a" * 64,
                size_bytes=1,
                media_type="application/octet-stream",
            )
            for name in ("asr", "llm", "vlm")
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            args = argparse.Namespace(
                session_index=1,
                attempt=1,
                collection_id="20260907T000000Z_phase1_asr_vlm_carryover_v1",
                protocol=DEFAULT_PROTOCOL_PATH,
                output_root=Path(temp_dir),
                asr_input=Path("fixture-asr"),
                llm_input=Path("fixture-llm"),
                vlm_input=Path("fixture-vlm"),
                confirm_services_restarted=True,
                confirm_dynamic_dvfs=True,
                thermal_wait_timeout_s=1.0,
            )
            session = run_session(
                args,
                repo_root=Path(__file__).resolve().parents[3],
                preflight_builder=preflight_builder,
                sampler_factory=sampler_factory,
                invocation_runner=invocation_runner,
                observation_reader=observation_reader,
                payloads_override=payloads,
                whisper_model_path="fixture-model",
            )
            manifest = json.loads(
                (session / "manifest.json").read_text(encoding="utf-8")
            )
            units = sorted((session / "units").glob("*/unit.json"))

        self.assertEqual(manifest["status"], "completed")
        self.assertFalse(manifest["formal_evidence"])
        self.assertEqual(manifest["interposer_order"], ["idle", "llm", "vlm"])
        self.assertEqual(len(units), 3)
        self.assertEqual(
            [item for item in observed if item[2] == "interposer"],
            [(2, "llm", "interposer"), (3, "vlm", "interposer")],
        )


if __name__ == "__main__":
    unittest.main()
