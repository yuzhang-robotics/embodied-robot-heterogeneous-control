"""Measure buffered EventRecorder overhead without running model workloads."""

from __future__ import annotations

import argparse
import json
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from .telemetry import EventRecorder


def nearest_rank(values: list[int], percentile: int) -> int:
    if not values:
        raise ValueError("values must not be empty")
    if not 1 <= percentile <= 100:
        raise ValueError("percentile must be between 1 and 100")
    ordered = sorted(values)
    rank = (len(ordered) * percentile + 99) // 100
    return ordered[rank - 1]


def run_benchmark(event_count: int) -> dict[str, int | str]:
    if event_count < 100:
        raise ValueError("event_count must be at least 100")

    with tempfile.TemporaryDirectory(prefix="octopus-phase0-recorder-") as temp_dir:
        run_id = (
            f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}_phase0_asr_000"
        )
        run_dir = Path(temp_dir) / run_id
        recorder = EventRecorder(run_dir, run_id)
        samples: list[int] = []

        total_start = time.perf_counter_ns()
        for index in range(event_count):
            start = time.perf_counter_ns()
            recorder.emit(
                task_id="recorder-benchmark",
                event="recorder.benchmark",
                component="recorder",
                status="info",
                details={"index": index},
            )
            samples.append(time.perf_counter_ns() - start)

        flush_start = time.perf_counter_ns()
        recorder.flush()
        flush_ns = time.perf_counter_ns() - flush_start
        recorder.close()
        total_ns = time.perf_counter_ns() - total_start
        file_size = (run_dir / "events.jsonl").stat().st_size

    return {
        "event_count": event_count,
        "emit_p50_ns": nearest_rank(samples, 50),
        "emit_p95_ns": nearest_rank(samples, 95),
        "emit_p99_ns": nearest_rank(samples, 99),
        "emit_max_ns": max(samples),
        "flush_ns": flush_ns,
        "total_ns": total_ns,
        "events_file_bytes": file_size,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--events", type=int, default=1000)
    parser.add_argument("--max-p99-ns", type=int, default=1_000_000)
    args = parser.parse_args(argv)

    result = run_benchmark(args.events)
    result["threshold_p99_ns"] = args.max_p99_ns
    result["status"] = (
        "PASS" if result["emit_p99_ns"] < args.max_p99_ns else "FAIL"
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
