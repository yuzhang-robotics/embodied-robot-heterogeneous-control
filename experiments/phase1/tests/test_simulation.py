from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from experiments.phase1.replay_lifecycle import (
    ReplayError,
    TraceProfile,
    replay_file,
)
from experiments.phase1.simulation import (
    InlineProbe,
    ScenarioSpec,
    SimulationCondition,
)
from experiments.phase1.telemetry import EventRecorder


RUN_ID = "20260827T020000Z_phase1_r1_inline_sync_simulated_001"


class FakeClock:
    def __init__(self, initial_ns: int = 1_000_000) -> None:
        self.now_ns = initial_ns

    def clock_ns(self) -> int:
        return self.now_ns

    def wait(self, seconds: float) -> None:
        self.now_ns += int(seconds * 1_000_000_000)

    def advance(self, nanoseconds: int) -> None:
        self.now_ns += nanoseconds


class SimulationPrimitiveTests(unittest.TestCase):
    def test_condition_profiles_are_explicit(self) -> None:
        expected = {
            SimulationCondition.R0_IDLE: TraceProfile.THREADED_PROBE,
            SimulationCondition.R1_INLINE_SYNC: TraceProfile.INLINE_PROBE,
            SimulationCondition.R2_THREADED_SYNC: TraceProfile.THREADED_PROBE,
            SimulationCondition.R3_ASYNC: TraceProfile.RUNTIME_THREADED_PROBE,
            SimulationCondition.R4_STALE: TraceProfile.RUNTIME_THREADED_PROBE,
            SimulationCondition.R4_OVERFLOW: TraceProfile.RUNTIME_THREADED_PROBE,
        }
        self.assertEqual(
            {condition: condition.trace_profile for condition in SimulationCondition},
            expected,
        )

    def test_r4_requires_positive_service_time(self) -> None:
        for condition in (
            SimulationCondition.R4_STALE,
            SimulationCondition.R4_OVERFLOW,
        ):
            with self.subTest(condition=condition):
                with self.assertRaisesRegex(ValueError, "positive service time"):
                    ScenarioSpec(condition=condition, service_time_s=0)

    def test_inline_probe_records_blocked_releases_without_a_join_event(self) -> None:
        clock = FakeClock()
        with tempfile.TemporaryDirectory() as temp_dir:
            recorder = EventRecorder(Path(temp_dir), RUN_ID)
            probe = InlineProbe(
                period_ns=1_000_000,
                deadline_ns=1_000_000,
                event_sink=recorder,
                clock_ns=clock.clock_ns,
                wait=clock.wait,
            )
            probe.start()
            probe.run_until(clock.clock_ns() + 2_000_000)
            clock.advance(5_000_000)
            probe.run_until(clock.clock_ns() + 2_000_000)
            report = probe.stop()
            recorder.close()
            summary = replay_file(
                recorder.path,
                profile=TraceProfile.INLINE_PROBE,
            )

        self.assertEqual(report.tick_count, 6)
        self.assertEqual(report.skipped_releases, 4)
        self.assertEqual(summary.probe_skipped_releases, 4)
        self.assertTrue(summary.probe_stopped)
        self.assertFalse(summary.probe_joined)

    def test_probe_only_trace_cannot_pass_as_a_runtime_trace(self) -> None:
        clock = FakeClock()
        with tempfile.TemporaryDirectory() as temp_dir:
            recorder = EventRecorder(Path(temp_dir), RUN_ID)
            probe = InlineProbe(
                period_ns=1_000_000,
                deadline_ns=1_000_000,
                event_sink=recorder,
                clock_ns=clock.clock_ns,
                wait=clock.wait,
            )
            probe.start()
            probe.run_until(clock.clock_ns())
            probe.stop()
            recorder.close()
            with self.assertRaisesRegex(ReplayError, "no runtime events"):
                replay_file(recorder.path, profile=TraceProfile.RUNTIME)

    def test_inline_probe_leaves_no_thread(self) -> None:
        before = {thread.ident for thread in threading.enumerate()}
        clock = FakeClock()
        with tempfile.TemporaryDirectory() as temp_dir:
            recorder = EventRecorder(Path(temp_dir), RUN_ID)
            probe = InlineProbe(
                period_ns=1_000_000,
                deadline_ns=1_000_000,
                event_sink=recorder,
                clock_ns=clock.clock_ns,
                wait=clock.wait,
            )
            probe.start()
            probe.run_until(clock.clock_ns())
            probe.stop()
            recorder.close()
        after = {thread.ident for thread in threading.enumerate()}
        self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
