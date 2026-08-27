from __future__ import annotations

import threading
import time
import unittest

from jetson.phase1_runtime import (
    PeriodicProbe,
    RuntimeEvent,
    build_probe_tick,
    select_release,
)


class RecordingSink:
    def __init__(self) -> None:
        self.events: list[RuntimeEvent] = []
        self.lock = threading.Lock()

    def emit(self, event: RuntimeEvent) -> None:
        with self.lock:
            self.events.append(event)

    def count(self, name: str) -> int:
        with self.lock:
            return sum(event.event == name for event in self.events)


class ProbeTests(unittest.TestCase):
    def wait_for(self, predicate, *, timeout_s: float = 1.0) -> None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if predicate():
                return
            threading.Event().wait(0.001)
        self.fail("condition did not become true before timeout")

    def test_release_selection_skips_without_catchup_burst(self) -> None:
        index, scheduled, skipped = select_release(
            origin_ns=1_000,
            index=2,
            started_ns=1_450,
            period_ns=100,
        )
        self.assertEqual(index, 4)
        self.assertEqual(scheduled, 1_400)
        self.assertEqual(skipped, 2)
        self.assertEqual(select_release(1_000, 2, 1_200, 100), (2, 1_200, 0))

    def test_tick_metrics_are_derived_from_monotonic_times(self) -> None:
        tick = build_probe_tick(
            index=3,
            scheduled_ns=1_300,
            started_ns=1_325,
            finished_ns=1_360,
            previous_started_ns=1_210,
            period_ns=100,
            deadline_ns=50,
            skipped_releases=1,
        )
        self.assertEqual(tick.start_lateness_ns, 25)
        self.assertEqual(tick.execution_ns, 35)
        self.assertEqual(tick.actual_period_ns, 115)
        self.assertEqual(tick.signed_period_error_ns, 15)
        self.assertEqual(tick.absolute_period_error_ns, 15)
        self.assertTrue(tick.deadline_miss)

    def test_threaded_probe_stops_without_leaking_worker(self) -> None:
        sink = RecordingSink()
        probe = PeriodicProbe(period_ns=2_000_000, event_sink=sink)
        probe.start()
        self.addCleanup(
            lambda: probe.stop(join_timeout_s=1.0) if probe.is_alive else None
        )
        self.wait_for(lambda: sink.count("probe.tick") >= 3)

        report = probe.stop(join_timeout_s=1.0)
        self.assertTrue(report.joined)
        self.assertGreaterEqual(report.tick_count, 3)
        self.assertIsNone(report.error_code)
        self.assertEqual(sink.count("probe.started"), 1)
        self.assertEqual(sink.count("probe.stopped"), 1)
        self.assertEqual(sink.count("probe.joined"), 1)

    def test_probe_callback_failure_is_bounded_and_observable(self) -> None:
        sink = RecordingSink()

        def fail() -> None:
            raise RuntimeError("unbounded private callback detail")

        probe = PeriodicProbe(
            period_ns=1_000_000,
            work=fail,
            event_sink=sink,
        )
        probe.start()
        self.wait_for(lambda: not probe.is_alive)
        report = probe.stop(join_timeout_s=1.0)

        self.assertTrue(report.joined)
        self.assertEqual(report.error_code, "probe_runtimeerror")
        details = " ".join(str(dict(event.details)) for event in sink.events)
        self.assertNotIn("unbounded private", details)
        self.assertEqual(sink.count("probe.failed"), 1)

    def test_stop_interrupts_wait_for_a_distant_release(self) -> None:
        sink = RecordingSink()
        probe = PeriodicProbe(period_ns=10_000_000_000, event_sink=sink)
        probe.start()
        self.wait_for(lambda: sink.count("probe.tick") >= 1)
        started = time.monotonic()
        report = probe.stop(join_timeout_s=1.0)
        elapsed = time.monotonic() - started

        self.assertTrue(report.joined)
        self.assertLess(elapsed, 1.0)


if __name__ == "__main__":
    unittest.main()
