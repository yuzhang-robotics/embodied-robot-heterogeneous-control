"""Validate the structural integrity of one Phase 0 run directory."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any

from .telemetry import RESOURCE_FIELDS, SCHEMA_VERSION


REQUIRED_EVENT_FIELDS = {
    "schema_version",
    "run_id",
    "seq",
    "task_id",
    "event",
    "component",
    "monotonic_ns",
    "wall_time_ns",
    "status",
    "details",
}


def _read_json(path: Path, errors: list[str]) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        errors.append(f"{path.name}: {type(exc).__name__}: {exc}")
        return {}
    if not isinstance(value, dict):
        errors.append(f"{path.name}: root must be an object")
        return {}
    return value


def validate_run_dir(run_dir: Path | str) -> list[str]:
    directory = Path(run_dir)
    errors: list[str] = []

    required_files = [
        "manifest.json",
        "events.jsonl",
        "resources.csv",
        "stdout.log",
        "result.json",
    ]
    for name in required_files:
        if not (directory / name).is_file():
            errors.append(f"missing file: {name}")
    if errors:
        return errors

    manifest = _read_json(directory / "manifest.json", errors)
    result = _read_json(directory / "result.json", errors)
    run_id = manifest.get("run_id")
    if run_id != directory.name:
        errors.append("manifest run_id does not match directory name")
    if manifest.get("schema_version") != SCHEMA_VERSION:
        errors.append("unsupported manifest schema_version")
    if manifest.get("status") != "completed":
        errors.append(f"manifest status is not completed: {manifest.get('status')!r}")
    safety = manifest.get("safety", {})
    if safety.get("motion_enabled") is not False:
        errors.append("manifest does not prove motion was disabled")
    if safety.get("motion_value_valid") is False:
        errors.append("manifest contains an unrecognized motion setting")
    if not manifest.get("input", {}).get("sha256"):
        errors.append("manifest does not identify the input SHA-256")
    if result.get("run_id") != run_id:
        errors.append("result run_id does not match manifest")
    if result.get("status") != "ok":
        errors.append(f"result status is not ok: {result.get('status')!r}")

    events: list[dict[str, Any]] = []
    try:
        with (directory / "events.jsonl").open("r", encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, start=1):
                try:
                    item = json.loads(line)
                except json.JSONDecodeError as exc:
                    errors.append(f"events.jsonl line {line_number}: {exc}")
                    continue
                if not isinstance(item, dict):
                    errors.append(f"events.jsonl line {line_number}: event must be an object")
                    continue
                missing = REQUIRED_EVENT_FIELDS - item.keys()
                if missing:
                    errors.append(
                        f"events.jsonl line {line_number}: missing {sorted(missing)}"
                    )
                events.append(item)
    except (OSError, UnicodeError) as exc:
        errors.append(f"events.jsonl: {type(exc).__name__}: {exc}")

    if not events:
        errors.append("events.jsonl contains no events")
    else:
        expected_seq = list(range(len(events)))
        actual_seq = [item.get("seq") for item in events]
        if actual_seq != expected_seq:
            errors.append("event seq is not contiguous from zero")
        monotonic_values = [item.get("monotonic_ns") for item in events]
        if not all(isinstance(value, int) for value in monotonic_values):
            errors.append("event monotonic_ns must be integers")
        elif any(
            later < earlier
            for earlier, later in zip(monotonic_values, monotonic_values[1:])
        ):
            errors.append("event monotonic_ns moved backwards")
        if any(item.get("run_id") != run_id for item in events):
            errors.append("one or more events have the wrong run_id")
        start_count = sum(item.get("event") == "experiment.start" for item in events)
        end_count = sum(item.get("event") == "experiment.end" for item in events)
        if start_count != 1:
            errors.append(f"expected one experiment.start, found {start_count}")
        if end_count != 1:
            errors.append(f"expected one experiment.end, found {end_count}")

        open_stages: dict[tuple[Any, Any, str], int] = {}
        for item in events:
            name = item.get("event")
            if not isinstance(name, str):
                continue
            if name.endswith(".start"):
                base = name.rsplit(".", 1)[0]
                key = (item.get("task_id"), item.get("component"), base)
                open_stages[key] = open_stages.get(key, 0) + 1
            elif name.endswith(".end"):
                base = name.rsplit(".", 1)[0]
                key = (item.get("task_id"), item.get("component"), base)
                if open_stages.get(key, 0) == 0:
                    errors.append(f"unmatched end event: {key}")
                else:
                    open_stages[key] -= 1
        for key, count in open_stages.items():
            if count:
                errors.append(f"unmatched start event: {key} x{count}")

    resource_rows = 0
    try:
        with (directory / "resources.csv").open(
            "r", encoding="utf-8", newline=""
        ) as stream:
            reader = csv.DictReader(stream)
            if reader.fieldnames != RESOURCE_FIELDS:
                errors.append("resources.csv header does not match the schema")
            for row_number, row in enumerate(reader, start=2):
                resource_rows += 1
                if row.get("parse_error"):
                    errors.append(
                        f"resources.csv line {row_number}: {row['parse_error']}"
                    )
                if not row.get("sample_monotonic_ns"):
                    errors.append(
                        f"resources.csv line {row_number}: missing sample_monotonic_ns"
                    )
    except (OSError, UnicodeError, csv.Error) as exc:
        errors.append(f"resources.csv: {type(exc).__name__}: {exc}")
    if resource_rows == 0:
        errors.append("resources.csv contains no samples")

    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    args = parser.parse_args(argv)
    errors = validate_run_dir(args.run_dir)
    if errors:
        print("INVALID")
        for error in errors:
            print(f"- {error}")
        return 1
    print("VALID")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
