"""Build a deterministic descriptive report from one validated VLM pilot."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Mapping, Sequence

from experiments.phase1.jetson_telemetry import load_resource_samples
from experiments.phase1.manifest import sha256_file
from experiments.phase1.summarize_vlm_process_slice import VLM_PROCESS_ISOLATION
from experiments.phase1.validate_vlm_slice import validate_vlm_slice_dir


ANALYSIS_SCHEMA_VERSION = "0.1.0"
EXPECTED_CONDITIONS = ("vlm_async", "vlm_stale")
THREAD_ANALYSIS_KIND = "phase1_fixed_input_vlm_pilot"
PROCESS_ANALYSIS_KIND = "phase1_fixed_input_vlm_process_pilot"
_CONDITION_LAYOUTS = {
    "thread": {
        "vlm_async": "vlm_async",
        "vlm_stale": "vlm_stale",
    },
    VLM_PROCESS_ISOLATION: {
        "vlm_async": "vlm_process_async",
        "vlm_stale": "vlm_process_stale",
    },
}
_EXPECTED_RUN_FILES = {
    "events.jsonl",
    "manifest.json",
    "preflight.json",
    "resources.jsonl",
    "scenario.json",
    "summary.json",
}
_EXPECTED_PROCESS_GATES = {
    "spawned_process",
    "bounded_protocol",
    "process_reaped",
    "boundary_order",
    "cancellation_forwarding",
}
_STAGE_ORDER = (
    "input_verify_before",
    "module_import",
    "moondream_inference",
    "qwen_rewrite",
    "argos_fallback",
    "output_normalization",
    "model_unload",
    "input_verify_after",
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    return value


def _read_events(path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), 1
    ):
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"{path.name}:{line_number} is not a JSON object")
        events.append(value)
    if not events:
        raise ValueError(f"{path.name} contains no events")
    return events


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
    return value


def _integer(value: object, name: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be an integer not less than {minimum}")
    return value


def _number(value: object, name: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
    ):
        raise ValueError(f"{name} must be finite and numeric")
    return float(value)


def _milliseconds(value: object, name: str) -> float:
    return round(_number(value, name) / 1_000_000.0, 6)


def _metric_value(
    summary: Mapping[str, object],
    metric: str,
    field: str,
) -> float | None:
    value = summary.get(metric)
    if value is None:
        return None
    record = _mapping(value, f"resource metric {metric}")
    observed = record.get(field)
    if observed is None:
        return None
    return round(_number(observed, f"resource metric {metric}.{field}"), 6)


def _find_run_directories(session_dir: Path) -> tuple[str, dict[str, Path]]:
    observed = sorted(path.name for path in session_dir.iterdir())
    matches = [
        (adapter_isolation, layout)
        for adapter_isolation, layout in _CONDITION_LAYOUTS.items()
        if observed == sorted(layout.values())
        and all((session_dir / name).is_dir() for name in layout.values())
    ]
    if len(matches) != 1:
        raise ValueError(
            "VLM pilot must contain exactly one supported two-condition layout"
        )
    adapter_isolation, layout = matches[0]
    runs: dict[str, Path] = {}
    for condition in EXPECTED_CONDITIONS:
        condition_directory = layout[condition]
        candidates = sorted((session_dir / condition_directory).iterdir())
        if len(candidates) != 1 or not candidates[0].is_dir():
            raise ValueError(
                f"{condition_directory} must contain exactly one run directory"
            )
        runs[condition] = candidates[0]
    return adapter_isolation, runs


def _disposition_counts(lifecycle: Mapping[str, object]) -> dict[str, int]:
    items = lifecycle.get("disposition_counts")
    if not isinstance(items, list):
        raise ValueError("lifecycle disposition_counts must be a list")
    result: dict[str, int] = {}
    for item in items:
        if not isinstance(item, list) or len(item) != 2 or not isinstance(item[0], str):
            raise ValueError("lifecycle disposition count is invalid")
        result[item[0]] = _integer(item[1], "lifecycle disposition count")
    return result


def _stage_records(adapter: Mapping[str, object]) -> list[dict[str, object]]:
    durations = _mapping(adapter.get("stage_durations_ns"), "adapter stage durations")
    statuses = _mapping(adapter.get("stage_status"), "adapter stage status")
    started_ns = _integer(adapter.get("started_monotonic_ns"), "adapter start")
    cursor = started_ns
    rows: list[dict[str, object]] = []
    for stage in _STAGE_ORDER:
        if stage not in durations:
            continue
        duration_ns = _integer(durations.get(stage), f"stage duration {stage}")
        rows.append(
            {
                "stage": stage,
                "status": statuses.get(stage),
                "duration_ms": _milliseconds(duration_ns, f"stage duration {stage}"),
                "start_monotonic_ns": cursor,
                "finish_monotonic_ns": cursor + duration_ns,
            }
        )
        cursor += duration_ns
    if not rows:
        raise ValueError("adapter contains no stage durations")
    return rows


def _skipped_release_attribution(
    events: Sequence[Mapping[str, object]],
    stages: Sequence[Mapping[str, object]],
    expected_total: int,
) -> dict[str, int]:
    started = next(
        (event for event in events if event.get("event") == "probe.started"), None
    )
    if started is None:
        raise ValueError("event trace contains no probe.started event")
    details = _mapping(started.get("details"), "probe.started details")
    origin_ns = _integer(details.get("origin_monotonic_ns"), "probe origin")
    period_ns = _integer(details.get("period_ns"), "probe period", minimum=1)
    counts: Counter[str] = Counter()
    observed_total = 0
    for event in events:
        if event.get("event") != "probe.skipped":
            continue
        skipped = _mapping(event.get("details"), "probe.skipped details")
        first = _integer(skipped.get("from_index"), "first skipped release")
        last = _integer(skipped.get("to_index"), "last skipped release")
        count = _integer(skipped.get("skipped_releases"), "skipped release count")
        if last - first != count:
            raise ValueError("probe.skipped index range does not match its count")
        observed_total += count
        for tick_index in range(first, last):
            scheduled_ns = origin_ns + tick_index * period_ns
            stage_name = "unattributed"
            for stage in stages:
                start_ns = _integer(stage.get("start_monotonic_ns"), "stage start")
                finish_ns = _integer(stage.get("finish_monotonic_ns"), "stage finish")
                if start_ns <= scheduled_ns < finish_ns:
                    stage_name = str(stage.get("stage"))
                    break
            counts[stage_name] += 1
    if observed_total != expected_total:
        raise ValueError("probe skipped-release events do not match the probe report")
    return {name: counts[name] for name in sorted(counts)}


def _probe_record(
    scenario: Mapping[str, object],
    events: Sequence[Mapping[str, object]],
    stages: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    report = _mapping(scenario.get("report"), "scenario report")
    probe = _mapping(report.get("probe"), "scenario probe report")
    spec = _mapping(scenario.get("spec"), "scenario spec")
    ticks = _integer(probe.get("tick_count"), "probe tick count")
    skipped = _integer(probe.get("skipped_releases"), "probe skipped releases")
    scheduled = ticks + skipped
    return {
        "period_ms": _milliseconds(spec.get("probe_period_ns"), "probe period"),
        "deadline_ms": _milliseconds(spec.get("probe_deadline_ns"), "probe deadline"),
        "tick_count": ticks,
        "skipped_releases": skipped,
        "scheduled_release_count": scheduled,
        "skipped_release_rate": round(skipped / scheduled, 9) if scheduled else None,
        "deadline_miss_count": _integer(
            probe.get("deadline_miss_count"), "probe deadline misses"
        ),
        "max_lateness_ms": _milliseconds(
            probe.get("max_lateness_ns"), "probe maximum lateness"
        ),
        "maximum_observed_gap_ms": _milliseconds(
            probe.get("max_gap_ns"), "probe maximum gap"
        ),
        "joined": probe.get("joined") is True,
        "error_code": probe.get("error_code"),
        "skipped_releases_by_adapter_stage": _skipped_release_attribution(
            events,
            stages,
            skipped,
        ),
    }


def _warning_counts(samples: Sequence[Mapping[str, object]]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for sample in samples:
        warnings = sample.get("parse_warnings")
        if not isinstance(warnings, list):
            raise ValueError("resource sample parse_warnings must be a list")
        if not all(isinstance(item, str) for item in warnings):
            raise ValueError("resource parse warning must be a string")
        counts.update(warnings)
    return {name: counts[name] for name in sorted(counts)}


def _resource_record(
    summary: Mapping[str, object],
    samples: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    resources = _mapping(summary.get("resources"), "summary resources")
    description = _mapping(resources.get("summary"), "resource summary")
    return {
        "sample_count": _integer(
            description.get("sample_count"), "resource sample count"
        ),
        "inference_interval_sample_count": _integer(
            resources.get("inference_interval_sample_count"),
            "inference resource sample count",
        ),
        "parse_error_count": _integer(
            description.get("parse_error_count"), "resource parse error count"
        ),
        "parse_warning_counts": _warning_counts(samples),
        "sample_interval_ms": {
            field: (
                round(value / 1_000_000.0, 6)
                if (value := _metric_value(description, "sample_interval_ns", field))
                is not None
                else None
            )
            for field in ("mean", "p95", "p99", "max")
        },
        "ram_used_mb": {
            field: _metric_value(description, "ram_used_mb", field)
            for field in ("mean", "p95", "max")
        },
        "gr3d_usage_pct": {
            field: _metric_value(description, "gr3d_usage_pct", field)
            for field in ("mean", "p95", "max")
        },
        "junction_temperature_c": {
            field: _metric_value(
                _mapping(description.get("temperatures_c"), "temperature summary"),
                "tj",
                field,
            )
            for field in ("mean", "p95", "max")
        },
        "vdd_in_instant_mw": {
            field: _metric_value(
                _mapping(description.get("power_instant_mw"), "power summary"),
                "VDD_IN",
                field,
            )
            for field in ("mean", "p95", "max")
        },
    }


def _listener_evidence(preflight: Mapping[str, object]) -> dict[str, object]:
    version = preflight.get("vlm_preflight_schema_version")
    services = _mapping(preflight.get("services"), "preflight services")
    records: dict[str, object] = {}
    complete = version == "0.2.0"
    for name in ("ollama", "qwen"):
        service = _mapping(services.get(name), f"{name} service")
        addresses = service.get("listener_addresses")
        loopback_only = service.get("listener_loopback_only")
        recorded = (
            isinstance(addresses, list)
            and all(isinstance(item, str) for item in addresses)
            and bool(addresses)
            and loopback_only is True
        )
        records[name] = {
            "recorded": recorded,
            "addresses": addresses if recorded else [],
            "loopback_only": loopback_only if recorded else None,
        }
        complete = complete and recorded
    return {
        "preflight_schema_version": version,
        "complete": complete,
        "services": records,
    }


def _process_interval_ms(
    earlier: object,
    later: object,
    name: str,
) -> float:
    earlier_ns = _integer(earlier, f"{name} start")
    later_ns = _integer(later, f"{name} finish")
    if later_ns < earlier_ns:
        raise ValueError(f"{name} boundaries are reversed")
    return _milliseconds(later_ns - earlier_ns, name)


def _process_record(
    condition: str,
    scenario: Mapping[str, object],
    process_summary: Mapping[str, object],
) -> dict[str, object]:
    scenario_facts = _mapping(scenario.get("process"), "scenario process facts")
    summary_facts = _mapping(process_summary.get("process"), "process summary facts")
    if dict(scenario_facts) != dict(summary_facts):
        raise ValueError("scenario and process summary facts differ")
    if process_summary.get("condition") != condition:
        raise ValueError("process summary condition is inconsistent")
    if process_summary.get("adapter_isolation") != VLM_PROCESS_ISOLATION:
        raise ValueError("process summary isolation is inconsistent")
    gates = process_summary.get("gates")
    if not isinstance(gates, list) or not all(
        isinstance(gate, Mapping) for gate in gates
    ):
        raise ValueError("process gates must be a list of objects")
    gate_names = [gate.get("name") for gate in gates]
    if (
        len(gate_names) != len(_EXPECTED_PROCESS_GATES)
        or set(gate_names) != _EXPECTED_PROCESS_GATES
        or any(not isinstance(name, str) for name in gate_names)
    ):
        raise ValueError("process gate set is incomplete or unsupported")
    process_id = _integer(summary_facts.get("process_id"), "process identifier")
    if process_id <= 0:
        raise ValueError("process identifier must be positive")

    spawn_ns = summary_facts.get("spawn_requested_monotonic_ns")
    child_ns = summary_facts.get("child_started_monotonic_ns")
    inference_ns = summary_facts.get("inference_started_monotonic_ns")
    completion_ns = summary_facts.get("completion_received_monotonic_ns")
    joined_ns = summary_facts.get("joined_monotonic_ns")
    return {
        "protocol_version": summary_facts.get("protocol_version"),
        "start_method": summary_facts.get("start_method"),
        "process_id_recorded": True,
        "protocol_complete": summary_facts.get("protocol_complete"),
        "exit_code": summary_facts.get("exit_code"),
        "cancellation_forwarded": summary_facts.get("cancellation_forwarded"),
        "terminate_requested": summary_facts.get("terminate_requested"),
        "terminate_confirmed": summary_facts.get("terminate_confirmed"),
        "error_code": summary_facts.get("error_code"),
        "valid": process_summary.get("valid") is True,
        "gate_results": {
            str(gate["name"]): gate.get("passed") is True for gate in gates
        },
        "timing_ms": {
            "spawn_to_child_start": _process_interval_ms(
                spawn_ns,
                child_ns,
                "spawn to child start",
            ),
            "child_start_to_inference": _process_interval_ms(
                child_ns,
                inference_ns,
                "child start to inference",
            ),
            "inference_to_completion_receipt": _process_interval_ms(
                inference_ns,
                completion_ns,
                "inference to completion receipt",
            ),
            "completion_receipt_to_join": _process_interval_ms(
                completion_ns,
                joined_ns,
                "completion receipt to join",
            ),
            "total_supervision": _process_interval_ms(
                spawn_ns,
                joined_ns,
                "total process supervision",
            ),
        },
    }


def _run_record(
    condition: str,
    run_dir: Path,
    *,
    adapter_isolation: str,
) -> dict[str, object]:
    expected_files = set(_EXPECTED_RUN_FILES)
    process_isolated = adapter_isolation == VLM_PROCESS_ISOLATION
    if process_isolated:
        expected_files.add("process.json")
    observed_files = {path.name for path in run_dir.iterdir() if path.is_file()}
    if observed_files != expected_files or any(
        not path.is_file() for path in run_dir.iterdir()
    ):
        raise ValueError(f"{condition} run files do not match the evidence contract")
    errors = validate_vlm_slice_dir(run_dir)
    if errors:
        raise ValueError(f"invalid {condition} run: " + "; ".join(errors))
    manifest = _read_object(run_dir / "manifest.json")
    preflight = _read_object(run_dir / "preflight.json")
    scenario = _read_object(run_dir / "scenario.json")
    summary = _read_object(run_dir / "summary.json")
    events = _read_events(run_dir / "events.jsonl")
    samples = load_resource_samples(run_dir / "resources.jsonl")
    if manifest.get("condition") != condition or summary.get("condition") != condition:
        raise ValueError(f"{condition} directory contains a different condition")
    adapter = _mapping(summary.get("adapter"), "summary adapter")
    lifecycle = _mapping(summary.get("lifecycle"), "summary lifecycle")
    stages = _stage_records(adapter)
    started_ns = _integer(adapter.get("started_monotonic_ns"), "adapter start")
    finished_ns = _integer(adapter.get("finished_monotonic_ns"), "adapter finish")
    environment = _mapping(manifest.get("environment"), "manifest environment")
    git = _mapping(environment.get("git"), "manifest Git identity")
    services = _mapping(preflight.get("services"), "preflight services")
    ollama = _mapping(services.get("ollama"), "Ollama service")
    qwen = _mapping(services.get("qwen"), "Qwen service")
    gates = summary.get("gates")
    if not isinstance(gates, list) or not all(
        isinstance(gate, Mapping) for gate in gates
    ):
        raise ValueError("summary gates must be a list of objects")
    record: dict[str, object] = {
        "condition": condition,
        "run_id": manifest.get("run_id"),
        "session_id": manifest.get("session_id"),
        "source": {
            "git_commit": git.get("commit"),
            "git_branch": git.get("branch"),
            "artifacts": manifest.get("artifacts"),
        },
        "input": manifest.get("input"),
        "services": {
            "moondream_model": ollama.get("model"),
            "moondream_digest": ollama.get("model_digest"),
            "qwen_model_ids": qwen.get("served_model_ids"),
            "listener_evidence": _listener_evidence(preflight),
        },
        "validation": {
            "manifest_status": manifest.get("status"),
            "summary_valid": summary.get("valid") is True,
            "all_gates_passed": all(gate.get("passed") is True for gate in gates),
            "real_vlm_path_executed": summary.get("real_vlm_path_executed") is True,
        },
        "result": {
            "execution_outcome": adapter.get("execution_outcome"),
            "translation_route": adapter.get("translation_route"),
            "disposition_counts": _disposition_counts(lifecycle),
            "accepted_result_count": _integer(
                lifecycle.get("accepted_result_count"), "accepted result count"
            ),
            "stale_consumed_count": _integer(
                lifecycle.get("stale_consumed_count"), "stale consumed count"
            ),
            "raw_text_recorded": _mapping(adapter.get("output"), "adapter output").get(
                "raw_text_recorded"
            ),
            "model_unload": adapter.get("model_residency"),
            "cancellation": adapter.get("cancellation"),
        },
        "timing": {
            "adapter_total_ms": _milliseconds(
                finished_ns - started_ns, "adapter total duration"
            ),
            "stages": [
                {
                    "stage": stage["stage"],
                    "status": stage["status"],
                    "duration_ms": stage["duration_ms"],
                }
                for stage in stages
            ],
            "probe": _probe_record(scenario, events, stages),
        },
        "resources": _resource_record(summary, samples),
    }
    if process_isolated:
        record["adapter_isolation"] = VLM_PROCESS_ISOLATION
        if manifest.get("adapter_isolation") != VLM_PROCESS_ISOLATION:
            raise ValueError("manifest process isolation is inconsistent")
        process_summary = _read_object(run_dir / "process.json")
        process = _process_record(condition, scenario, process_summary)
        record["process"] = process
        validation = record["validation"]
        assert isinstance(validation, dict)
        validation["process_summary_valid"] = process["valid"] is True
        validation["all_process_gates_passed"] = all(process["gate_results"].values())
    return record


def _same_value(runs: Sequence[Mapping[str, object]], path: Sequence[str]) -> object:
    values: list[object] = []
    for run in runs:
        current: object = run
        for key in path:
            current = _mapping(current, ".".join(path)).get(key)
        values.append(current)
    if any(value != values[0] for value in values[1:]):
        raise ValueError(f"runs disagree on {'.'.join(path)}")
    return values[0]


def _thread_reference_record(
    path: Path,
    *,
    input_identity: object,
    moondream_digest: object,
    qwen_model_ids: object,
) -> dict[str, object]:
    reference = _read_object(path)
    if reference.get("analysis_kind") != THREAD_ANALYSIS_KIND:
        raise ValueError("thread reference analysis kind is unsupported")
    validation = _mapping(reference.get("validation"), "thread reference validation")
    if validation.get("all_runs_valid") is not True:
        raise ValueError("thread reference does not contain valid source runs")
    identity = _mapping(reference.get("identity"), "thread reference identity")
    expected_identity = {
        "input": input_identity,
        "moondream_digest": moondream_digest,
        "qwen_model_ids": qwen_model_ids,
    }
    if any(identity.get(name) != value for name, value in expected_identity.items()):
        raise ValueError("thread reference workload identity does not match")
    claims = _mapping(
        reference.get("claim_boundary"), "thread reference claim boundary"
    )
    for name in (
        "timing_domain_isolation_claim_permitted",
        "asynchronous_performance_superiority_claim_permitted",
        "hard_real_time_claim_permitted",
        "heterogeneous_inference_claim_permitted",
    ):
        if claims.get(name) is not False:
            raise ValueError("thread reference claim boundary is not conservative")
    runs_value = reference.get("runs")
    if (
        not isinstance(runs_value, list)
        or len(runs_value) != 2
        or not all(isinstance(run, Mapping) for run in runs_value)
    ):
        raise ValueError("thread reference runs must be a list of objects")
    runs_by_condition = {str(run.get("condition")): run for run in runs_value}
    if set(runs_by_condition) != set(EXPECTED_CONDITIONS):
        raise ValueError("thread reference condition set is incomplete")

    conditions: dict[str, object] = {}
    for condition in EXPECTED_CONDITIONS:
        run = runs_by_condition[condition]
        timing = _mapping(run.get("timing"), "thread reference timing")
        probe = _mapping(timing.get("probe"), "thread reference probe")
        conditions[condition] = {
            "adapter_total_ms": round(
                _number(timing.get("adapter_total_ms"), "thread adapter duration"),
                6,
            ),
            "probe_skipped_releases": _integer(
                probe.get("skipped_releases"), "thread skipped releases"
            ),
            "probe_deadline_miss_count": _integer(
                probe.get("deadline_miss_count"), "thread deadline misses"
            ),
            "probe_max_lateness_ms": round(
                _number(
                    probe.get("max_lateness_ms"),
                    "thread probe maximum lateness",
                ),
                6,
            ),
            "probe_maximum_observed_gap_ms": round(
                _number(
                    probe.get("maximum_observed_gap_ms"),
                    "thread probe maximum gap",
                ),
                6,
            ),
        }
    source = _mapping(reference.get("source"), "thread reference source")
    return {
        "analysis_sha256": sha256_file(path),
        "analysis_kind": reference.get("analysis_kind"),
        "analysis_schema_version": reference.get("analysis_schema_version"),
        "session_id": reference.get("session_id"),
        "git_commit": source.get("git_commit"),
        "source_archive_sha256": source.get("source_archive_sha256"),
        "workload_identity_matched": True,
        "conditions": conditions,
    }


def _thread_process_comparison(
    runs: Sequence[Mapping[str, object]],
    reference: Mapping[str, object],
) -> dict[str, object]:
    process_runs = {str(run.get("condition")): run for run in runs}
    reference_conditions = _mapping(
        reference.get("conditions"), "thread reference conditions"
    )
    conditions: dict[str, object] = {}
    thread_skipped_total = 0
    process_skipped_total = 0
    for condition in EXPECTED_CONDITIONS:
        process_timing = _mapping(
            process_runs[condition].get("timing"), "process run timing"
        )
        process_probe = _mapping(process_timing.get("probe"), "process run probe")
        thread = _mapping(
            reference_conditions.get(condition), "thread reference condition"
        )
        thread_skipped = _integer(
            thread.get("probe_skipped_releases"), "thread skipped releases"
        )
        process_skipped = _integer(
            process_probe.get("skipped_releases"), "process skipped releases"
        )
        thread_skipped_total += thread_skipped
        process_skipped_total += process_skipped
        conditions[condition] = {
            "thread": dict(thread),
            "spawned_process": {
                "adapter_total_ms": process_timing.get("adapter_total_ms"),
                "probe_skipped_releases": process_skipped,
                "probe_deadline_miss_count": process_probe.get("deadline_miss_count"),
                "probe_max_lateness_ms": process_probe.get("max_lateness_ms"),
                "probe_maximum_observed_gap_ms": process_probe.get(
                    "maximum_observed_gap_ms"
                ),
            },
            "observed_skipped_release_difference": (process_skipped - thread_skipped),
        }
    return {
        "comparison_role": "cross_session_descriptive_reference",
        "conditions": conditions,
        "total_probe_skipped_releases": {
            "thread": thread_skipped_total,
            "spawned_process": process_skipped_total,
        },
        "descriptive_mitigation_signal_observed": (
            thread_skipped_total > 0 and process_skipped_total == 0
        ),
        "causal_attribution_permitted": False,
        "performance_superiority_claim_permitted": False,
    }


def analyze_vlm_pilot_dir(
    session_dir: Path | str,
    *,
    source_archive_sha256: str | None = None,
    thread_reference_analysis: Path | str | None = None,
) -> dict[str, object]:
    """Validate and reconstruct one two-condition real-model VLM pilot."""

    directory = Path(session_dir).resolve()
    if not directory.is_dir():
        raise ValueError("VLM pilot session directory does not exist")
    normalized_hash: str | None = None
    if source_archive_sha256 is not None:
        normalized_hash = source_archive_sha256.lower()
        if _SHA256_RE.fullmatch(normalized_hash) is None:
            raise ValueError("source_archive_sha256 must contain 64 hexadecimal digits")
    adapter_isolation, run_dirs = _find_run_directories(directory)
    process_isolated = adapter_isolation == VLM_PROCESS_ISOLATION
    if thread_reference_analysis is not None and not process_isolated:
        raise ValueError("thread reference is only supported for a process pilot")
    runs = [
        _run_record(
            condition,
            run_dirs[condition],
            adapter_isolation=adapter_isolation,
        )
        for condition in EXPECTED_CONDITIONS
    ]
    session_id = _same_value(runs, ("session_id",))
    if session_id != directory.name:
        raise ValueError("run session_id does not match the session directory")
    git_commit = _same_value(runs, ("source", "git_commit"))
    git_branch = _same_value(runs, ("source", "git_branch"))
    input_identity = _same_value(runs, ("input",))
    moondream_digest = _same_value(runs, ("services", "moondream_digest"))
    qwen_models = _same_value(runs, ("services", "qwen_model_ids"))

    async_run, stale_run = runs
    correctness_observed = (
        async_run["result"]["disposition_counts"] == {"consumed": 1}
        and async_run["result"]["accepted_result_count"] == 1
        and stale_run["result"]["disposition_counts"] == {"rejected_state": 1}
        and stale_run["result"]["accepted_result_count"] == 0
        and all(run["result"]["stale_consumed_count"] == 0 for run in runs)
    )
    listener_complete = all(
        run["services"]["listener_evidence"]["complete"] is True for run in runs
    )
    skipped_total = sum(run["timing"]["probe"]["skipped_releases"] for run in runs)
    deadline_miss_total = sum(
        run["timing"]["probe"]["deadline_miss_count"] for run in runs
    )
    process_boundary_correctness = process_isolated and all(
        run["process"]["valid"] is True
        and all(run["process"]["gate_results"].values())
        and run["process"]["protocol_complete"] is True
        and run["process"]["exit_code"] == 0
        and run["process"]["terminate_requested"] is False
        and run["process"]["error_code"] is None
        for run in runs
    )
    periodic_probe_continuity = (
        skipped_total == 0
        and deadline_miss_total == 0
        and all(run["timing"]["probe"]["joined"] is True for run in runs)
    )
    limitations = [
        "single_run_per_condition",
        "fixed_condition_order",
        "no_real_workload_synchronous_condition",
        "formal_thresholds_not_frozen",
        "model_unload_not_independently_confirmed",
        "resource_activity_not_attributed_to_a_model_or_processor",
    ]
    if not listener_complete:
        limitations.append("listener_binding_evidence_not_recorded")
    if skipped_total:
        limitations.append("probe_skipped_releases_observed")
    if all(
        run["resources"]["parse_warning_counts"].get("emc_missing", 0)
        == run["resources"]["sample_count"]
        for run in runs
    ):
        limitations.append("emc_unavailable")

    thread_reference: dict[str, object] | None = None
    comparison: dict[str, object] | None = None
    if thread_reference_analysis is not None:
        reference_path = Path(thread_reference_analysis).resolve()
        if not reference_path.is_file():
            raise ValueError("thread reference analysis does not exist")
        thread_reference = _thread_reference_record(
            reference_path,
            input_identity=input_identity,
            moondream_digest=moondream_digest,
            qwen_model_ids=qwen_models,
        )
        comparison = _thread_process_comparison(runs, thread_reference)
        limitations.append("thread_process_comparison_crosses_sessions")
        limitations.append("thread_process_order_not_randomized_or_balanced")
        if thread_reference.get("git_commit") != git_commit:
            limitations.append("thread_process_source_commits_differ")
    if process_isolated:
        limitations.append("backend_stop_not_confirmed_by_process_exit")

    analysis: dict[str, object] = {
        "analysis_schema_version": ANALYSIS_SCHEMA_VERSION,
        "analysis_kind": (
            PROCESS_ANALYSIS_KIND if process_isolated else THREAD_ANALYSIS_KIND
        ),
        "session_id": session_id,
        "source": {
            "git_commit": git_commit,
            "git_branch": git_branch,
            "source_archive_sha256": normalized_hash,
            "run_artifacts": {
                run["condition"]: run["source"]["artifacts"] for run in runs
            },
        },
        "identity": {
            "input": input_identity,
            "moondream_digest": moondream_digest,
            "qwen_model_ids": qwen_models,
        },
        "claim_boundary": {
            "design_role": "correctness_pilot",
            "descriptive_only": True,
            "real_workload_integration_observed": all(
                run["validation"]["real_vlm_path_executed"] is True for run in runs
            ),
            "stale_result_rejection_observed": correctness_observed,
            "timing_domain_isolation_claim_permitted": False,
            "asynchronous_performance_superiority_claim_permitted": False,
            "hard_real_time_claim_permitted": False,
            "heterogeneous_inference_claim_permitted": False,
            "condition_resource_attribution_permitted": False,
        },
        "validation": {
            "run_count": len(runs),
            "all_runs_valid": all(
                run["validation"]["summary_valid"] is True
                and run["validation"]["all_gates_passed"] is True
                for run in runs
            ),
            "correctness_observed": correctness_observed,
            "listener_binding_evidence_complete": listener_complete,
        },
        "runs": runs,
        "data_quality": {
            "total_probe_skipped_releases": skipped_total,
            "limitations": limitations,
        },
    }
    if process_isolated:
        analysis["adapter_isolation"] = VLM_PROCESS_ISOLATION
        source = analysis["source"]
        assert isinstance(source, dict)
        source["thread_reference"] = thread_reference
        claim_boundary = analysis["claim_boundary"]
        assert isinstance(claim_boundary, dict)
        claim_boundary["process_boundary_correctness_observed"] = (
            process_boundary_correctness
        )
        claim_boundary["periodic_probe_continuity_observed"] = periodic_probe_continuity
        validation = analysis["validation"]
        assert isinstance(validation, dict)
        validation["all_process_gates_passed"] = all(
            run["validation"]["process_summary_valid"] is True
            and run["validation"]["all_process_gates_passed"] is True
            for run in runs
        )
        validation["process_boundary_correctness_observed"] = (
            process_boundary_correctness
        )
        validation["periodic_probe_continuity_observed"] = periodic_probe_continuity
        data_quality = analysis["data_quality"]
        assert isinstance(data_quality, dict)
        data_quality["total_probe_deadline_misses"] = deadline_miss_total
        analysis["thread_process_comparison"] = comparison
    return analysis


def _format_number(value: object, digits: int = 3) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (int, float)):
        return f"{value:.{digits}f}" if isinstance(value, float) else str(value)
    return str(value)


def _stage_duration(run: Mapping[str, object], name: str) -> object:
    timing = _mapping(run.get("timing"), "run timing")
    stages = timing.get("stages")
    if not isinstance(stages, list):
        raise ValueError("run timing stages must be a list")
    for stage in stages:
        if isinstance(stage, Mapping) and stage.get("stage") == name:
            return stage.get("duration_ms")
    return None


def _render_dispositions(value: object) -> str:
    record = _mapping(value, "disposition counts")
    return ", ".join(f"{name}={record[name]}" for name in sorted(record))


def _render_process_markdown(analysis: Mapping[str, object]) -> str:
    source = _mapping(analysis.get("source"), "analysis source")
    identity = _mapping(analysis.get("identity"), "analysis identity")
    validation = _mapping(analysis.get("validation"), "analysis validation")
    quality = _mapping(analysis.get("data_quality"), "analysis data quality")
    claims = _mapping(analysis.get("claim_boundary"), "analysis claim boundary")
    runs_value = analysis.get("runs")
    if not isinstance(runs_value, list) or not all(
        isinstance(run, Mapping) for run in runs_value
    ):
        raise ValueError("analysis runs must be a list of objects")
    runs: list[Mapping[str, object]] = list(runs_value)
    input_identity = _mapping(identity.get("input"), "analysis input identity")
    qwen_ids = identity.get("qwen_model_ids")
    lines = [
        "# Phase 1 Process-isolated Fixed-input VLM Pilot",
        "",
        (
            "This report records one motion-disabled spawned-process correctness "
            "pilot on the Jetson Orin Nano. It validates process ownership, bounded "
            "IPC, stale-result rejection and deterministic child reaping."
        ),
        "",
        "## Evidence boundary",
        "",
        "- Workload: one fixed Phase 0 C100 image per condition.",
        "- Conditions: one `vlm_async` run followed by one `vlm_stale` run.",
        "- Isolation: one spawned child per VLM request; broker and probe remain in the parent.",
        "- Physical motion and UART access: disabled.",
        "- Permitted interpretation: integration, lifecycle and process-boundary correctness.",
        (
            "- Not permitted: causal performance claims, timing-domain isolation, "
            "hard-real-time or heterogeneous-inference claims."
        ),
        "",
        "## Provenance",
        "",
        "| Field | Value |",
        "| --- | --- |",
        f"| Session | `{analysis.get('session_id')}` |",
        f"| Source commit | `{source.get('git_commit')}` |",
        f"| Source branch | `{source.get('git_branch')}` |",
        f"| Transfer archive SHA-256 | `{source.get('source_archive_sha256')}` |",
        f"| Independently valid runs | {_format_number(validation.get('all_runs_valid'))} |",
        f"| Process Gates passed | {_format_number(validation.get('all_process_gates_passed'))} |",
        "",
        "Machine-readable derived data: [`analysis.json`](analysis.json).",
        "",
        "## Frozen identities",
        "",
        "| Field | Value |",
        "| --- | --- |",
        f"| Input SHA-256 | `{input_identity.get('sha256')}` |",
        f"| Input size | {_format_number(input_identity.get('size_bytes'))} bytes |",
        f"| Moondream digest | `{identity.get('moondream_digest')}` |",
        f"| Qwen model | `{', '.join(qwen_ids) if isinstance(qwen_ids, list) else qwen_ids}` |",
        "",
        "## Correctness and process results",
        "",
        "| Condition | Execution | Disposition | Accepted | Cancel forwarded | Exit | Terminate | Slice/Process Gates |",
        "| --- | --- | --- | ---: | --- | ---: | --- | --- |",
    ]
    for run in runs:
        result = _mapping(run.get("result"), "run result")
        process = _mapping(run.get("process"), "run process")
        run_validation = _mapping(run.get("validation"), "run validation")
        gates_passed = (
            run_validation.get("all_gates_passed") is True
            and run_validation.get("all_process_gates_passed") is True
        )
        lines.append(
            "| `{condition}` | `{outcome}` | {dispositions} | {accepted} | "
            "{cancel} | {exit_code} | {terminate} | {gates} |".format(
                condition=run.get("condition"),
                outcome=result.get("execution_outcome"),
                dispositions=_render_dispositions(result.get("disposition_counts")),
                accepted=_format_number(result.get("accepted_result_count")),
                cancel=_format_number(process.get("cancellation_forwarded")),
                exit_code=_format_number(process.get("exit_code")),
                terminate=_format_number(process.get("terminate_requested")),
                gates="pass" if gates_passed else "fail",
            )
        )
    lines.extend(
        [
            "",
            (
                "The nominal result was consumed once. The stale result was rejected "
                "before consumption after cancellation was forwarded. Child exit does "
                "not confirm that the external model backend stopped inference."
            ),
            "",
            "## Process supervision timing",
            "",
            "| Condition | Spawn -> start (ms) | Start -> inference (ms) | Inference -> completion receipt (ms) | Completion -> join (ms) | Total supervision (ms) |",
            "| --- | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for run in runs:
        process = _mapping(run.get("process"), "run process")
        timing = _mapping(process.get("timing_ms"), "process timing")
        lines.append(
            "| `{condition}` | {spawn} | {start} | {inference} | {join} | {total} |".format(
                condition=run.get("condition"),
                spawn=_format_number(timing.get("spawn_to_child_start")),
                start=_format_number(timing.get("child_start_to_inference")),
                inference=_format_number(timing.get("inference_to_completion_receipt")),
                join=_format_number(timing.get("completion_receipt_to_join")),
                total=_format_number(timing.get("total_supervision")),
            )
        )
    lines.extend(
        [
            "",
            "## Pipeline timing",
            "",
            "| Condition | Adapter total (ms) | Module import | Moondream | Qwen | Unload |",
            "| --- | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for run in runs:
        timing = _mapping(run.get("timing"), "run timing")
        lines.append(
            "| `{condition}` | {total} | {module} | {moondream} | {qwen} | {unload} |".format(
                condition=run.get("condition"),
                total=_format_number(timing.get("adapter_total_ms")),
                module=_format_number(_stage_duration(run, "module_import")),
                moondream=_format_number(_stage_duration(run, "moondream_inference")),
                qwen=_format_number(_stage_duration(run, "qwen_rewrite")),
                unload=_format_number(_stage_duration(run, "model_unload")),
            )
        )
    lines.extend(
        [
            "",
            "These single, fixed-order timings are descriptive and are not compared inferentially.",
            "",
            "## Periodic-probe observations",
            "",
            "| Condition | Ticks | Skipped | Deadline misses | Max lateness (ms) | Max gap (ms) |",
            "| --- | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for run in runs:
        probe = _mapping(
            _mapping(run.get("timing"), "run timing").get("probe"), "run probe"
        )
        lines.append(
            "| `{condition}` | {ticks} | {skipped} | {misses} | {lateness} | {gap} |".format(
                condition=run.get("condition"),
                ticks=_format_number(probe.get("tick_count")),
                skipped=_format_number(probe.get("skipped_releases")),
                misses=_format_number(probe.get("deadline_miss_count")),
                lateness=_format_number(probe.get("max_lateness_ms")),
                gap=_format_number(probe.get("maximum_observed_gap_ms")),
            )
        )

    comparison_value = analysis.get("thread_process_comparison")
    if isinstance(comparison_value, Mapping):
        reference = _mapping(source.get("thread_reference"), "thread reference")
        comparison = comparison_value
        conditions = _mapping(comparison.get("conditions"), "comparison conditions")
        lines.extend(
            [
                "",
                "## Descriptive thread reference",
                "",
                f"- Reference session: `{reference.get('session_id')}`",
                f"- Reference commit: `{reference.get('git_commit')}`",
                f"- Reference analysis SHA-256: `{reference.get('analysis_sha256')}`",
                "",
                "| Condition | Thread skipped | Process skipped | Thread max gap (ms) | Process max gap (ms) |",
                "| --- | ---: | ---: | ---: | ---: |",
            ]
        )
        for condition in EXPECTED_CONDITIONS:
            item = _mapping(conditions.get(condition), "comparison condition")
            thread = _mapping(item.get("thread"), "thread comparison record")
            process = _mapping(item.get("spawned_process"), "process comparison record")
            lines.append(
                "| `{condition}` | {thread_skipped} | {process_skipped} | {thread_gap} | {process_gap} |".format(
                    condition=condition,
                    thread_skipped=_format_number(thread.get("probe_skipped_releases")),
                    process_skipped=_format_number(
                        process.get("probe_skipped_releases")
                    ),
                    thread_gap=_format_number(
                        thread.get("probe_maximum_observed_gap_ms")
                    ),
                    process_gap=_format_number(
                        process.get("probe_maximum_observed_gap_ms")
                    ),
                )
            )
        totals = _mapping(
            comparison.get("total_probe_skipped_releases"), "comparison totals"
        )
        lines.extend(
            [
                "",
                (
                    "The thread reference recorded {thread} skipped releases and the "
                    "spawned-process pilot recorded {process}. This is a descriptive "
                    "mitigation signal, not a causal or performance-superiority claim; "
                    "the sessions used single fixed-order runs from different commits."
                ).format(
                    thread=totals.get("thread"),
                    process=totals.get("spawned_process"),
                ),
            ]
        )

    lines.extend(
        [
            "",
            "## Resource observations",
            "",
            "| Condition | Samples | Covered | RAM mean/max (MB) | GR3D mean/max (%) | Tj max (C) | VDD_IN mean/max (mW) |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for run in runs:
        resources = _mapping(run.get("resources"), "run resources")
        ram = _mapping(resources.get("ram_used_mb"), "RAM summary")
        gr3d = _mapping(resources.get("gr3d_usage_pct"), "GR3D summary")
        temperature = _mapping(
            resources.get("junction_temperature_c"), "temperature summary"
        )
        power = _mapping(resources.get("vdd_in_instant_mw"), "power summary")
        lines.append(
            "| `{condition}` | {samples} | {covered} | {ram_mean}/{ram_max} | "
            "{gpu_mean}/{gpu_max} | {temperature} | {power_mean}/{power_max} |".format(
                condition=run.get("condition"),
                samples=_format_number(resources.get("sample_count")),
                covered=_format_number(
                    resources.get("inference_interval_sample_count")
                ),
                ram_mean=_format_number(ram.get("mean")),
                ram_max=_format_number(ram.get("max")),
                gpu_mean=_format_number(gr3d.get("mean")),
                gpu_max=_format_number(gr3d.get("max")),
                temperature=_format_number(temperature.get("max")),
                power_mean=_format_number(power.get("mean")),
                power_max=_format_number(power.get("max")),
            )
        )
    limitations = quality.get("limitations")
    if not isinstance(limitations, list):
        raise ValueError("analysis limitations must be a list")
    lines.extend(["", "## Evidence gaps", ""])
    lines.extend(f"- `{item}`" for item in limitations)
    lines.extend(
        [
            "",
            (
                "Both process runs recorded loopback-only listener bindings. Process "
                "exit and cancellation forwarding are bounded local facts. This evidence "
                "does not prove backend preemption."
            ),
            "",
            (
                "GR3D activity is device-level evidence and is not attributed to a "
                "particular model or processor. The result does not authorize a "
                "heterogeneous-inference claim."
            ),
            "",
            (
                "A timing-domain or performance claim remains prohibited: "
                f"`timing_domain_isolation_claim_permitted={claims.get('timing_domain_isolation_claim_permitted')}`."
            ),
            "",
        ]
    )
    return "\n".join(lines)


def render_markdown(analysis: Mapping[str, object]) -> str:
    """Render the machine-readable analysis without adding new claims."""

    if analysis.get("analysis_kind") == PROCESS_ANALYSIS_KIND:
        return _render_process_markdown(analysis)
    if analysis.get("analysis_kind") != THREAD_ANALYSIS_KIND:
        raise ValueError("analysis kind is unsupported")

    source = _mapping(analysis.get("source"), "analysis source")
    identity = _mapping(analysis.get("identity"), "analysis identity")
    validation = _mapping(analysis.get("validation"), "analysis validation")
    quality = _mapping(analysis.get("data_quality"), "analysis data quality")
    runs_value = analysis.get("runs")
    if not isinstance(runs_value, list) or not all(
        isinstance(run, Mapping) for run in runs_value
    ):
        raise ValueError("analysis runs must be a list of objects")
    runs: list[Mapping[str, object]] = list(runs_value)
    input_identity = _mapping(identity.get("input"), "analysis input identity")
    qwen_ids = identity.get("qwen_model_ids")
    lines = [
        "# Phase 1 Fixed-input VLM Pilot",
        "",
        (
            "This report records one motion-disabled correctness pilot on the Jetson "
            "Orin Nano. It validates real-model integration and stale-result rejection; "
            "it is not a synchronous/asynchronous performance comparison."
        ),
        "",
        "## Evidence boundary",
        "",
        "- Workload: one fixed Phase 0 C100 image per condition.",
        "- Conditions: one `vlm_async` run followed by one `vlm_stale` run.",
        "- Model path: Ollama/Moondream description followed by llama.cpp/Qwen rewriting.",
        "- Physical motion and UART access: disabled.",
        "- Permitted interpretation: integration and lifecycle correctness evidence only.",
        (
            "- Not permitted: asynchronous superiority, hard-real-time, timing-isolation "
            "or heterogeneous-inference claims."
        ),
        "",
        "## Provenance",
        "",
        "| Field | Value |",
        "| --- | --- |",
        f"| Session | `{analysis.get('session_id')}` |",
        f"| Source commit | `{source.get('git_commit')}` |",
        f"| Source branch | `{source.get('git_branch')}` |",
        f"| Transfer archive SHA-256 | `{source.get('source_archive_sha256')}` |",
        f"| Independently valid runs | {_format_number(validation.get('all_runs_valid'))} |",
        "",
        "Machine-readable derived data: [`analysis.json`](analysis.json).",
        "",
        "## Frozen identities",
        "",
        "| Field | Value |",
        "| --- | --- |",
        f"| Input SHA-256 | `{input_identity.get('sha256')}` |",
        f"| Input size | {_format_number(input_identity.get('size_bytes'))} bytes |",
        f"| Moondream digest | `{identity.get('moondream_digest')}` |",
        f"| Qwen model | `{', '.join(qwen_ids) if isinstance(qwen_ids, list) else qwen_ids}` |",
        "",
        "## Correctness results",
        "",
        "| Condition | Execution | Route | Disposition | Accepted | Stale consumed | Gates |",
        "| --- | --- | --- | --- | ---: | ---: | --- |",
    ]
    for run in runs:
        result = _mapping(run.get("result"), "run result")
        run_validation = _mapping(run.get("validation"), "run validation")
        lines.append(
            "| `{condition}` | `{outcome}` | `{route}` | {dispositions} | {accepted} | "
            "{stale} | {gates} |".format(
                condition=run.get("condition"),
                outcome=result.get("execution_outcome"),
                route=result.get("translation_route"),
                dispositions=_render_dispositions(result.get("disposition_counts")),
                accepted=_format_number(result.get("accepted_result_count")),
                stale=_format_number(result.get("stale_consumed_count")),
                gates=(
                    "pass" if run_validation.get("all_gates_passed") is True else "fail"
                ),
            )
        )
    lines.extend(
        [
            "",
            (
                "The nominal result was consumed once. After the state generation "
                "advanced, the stale result completed but was rejected before consumption. "
                "Backend stop remains unconfirmed by design."
            ),
            "",
            "## Pipeline timing",
            "",
            "| Condition | Adapter total (ms) | Module import | Moondream | Qwen | Unload |",
            "| --- | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for run in runs:
        timing = _mapping(run.get("timing"), "run timing")
        lines.append(
            "| `{condition}` | {total} | {module} | {moondream} | {qwen} | {unload} |".format(
                condition=run.get("condition"),
                total=_format_number(timing.get("adapter_total_ms")),
                module=_format_number(_stage_duration(run, "module_import")),
                moondream=_format_number(_stage_duration(run, "moondream_inference")),
                qwen=_format_number(_stage_duration(run, "qwen_rewrite")),
                unload=_format_number(_stage_duration(run, "model_unload")),
            )
        )
    lines.extend(
        [
            "",
            "These single, fixed-order observations are descriptive and are not compared inferentially.",
            "",
            "## Periodic-probe observations",
            "",
            (
                "The probe used a 100 ms absolute schedule. Skipped releases are "
                "reported separately from callbacks that started after their deadline."
            ),
            "",
            "| Condition | Ticks | Skipped | Skip rate (%) | Deadline misses | Max lateness (ms) | Max gap (ms) |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for run in runs:
        probe = _mapping(
            _mapping(run.get("timing"), "run timing").get("probe"), "run probe"
        )
        lines.append(
            "| `{condition}` | {ticks} | {skipped} | {rate} | {misses} | {lateness} | {gap} |".format(
                condition=run.get("condition"),
                ticks=_format_number(probe.get("tick_count")),
                skipped=_format_number(probe.get("skipped_releases")),
                rate=_format_number(
                    (
                        100.0 * probe["skipped_release_rate"]
                        if probe.get("skipped_release_rate") is not None
                        else None
                    )
                ),
                misses=_format_number(probe.get("deadline_miss_count")),
                lateness=_format_number(probe.get("max_lateness_ms")),
                gap=_format_number(probe.get("maximum_observed_gap_ms")),
            )
        )
    lines.extend(
        [
            "",
            "Skipped-release attribution from scheduled timestamps:",
            "",
            "| Condition | Adapter stage | Skipped releases |",
            "| --- | --- | ---: |",
        ]
    )
    for run in runs:
        probe = _mapping(
            _mapping(run.get("timing"), "run timing").get("probe"), "run probe"
        )
        attribution = _mapping(
            probe.get("skipped_releases_by_adapter_stage"), "skip attribution"
        )
        for stage, count in attribution.items():
            lines.append(
                f"| `{run.get('condition')}` | `{stage}` | {_format_number(count)} |"
            )
    lines.extend(
        [
            "",
            (
                "Both runs missed scheduled releases during lazy module import. The "
                "simulated sleep pilot therefore does not establish that a Python worker "
                "thread isolates every real workload. Process-level isolation or an "
                "equivalent mitigation must be evaluated before a timing-isolation claim."
            ),
            "",
            "## Resource observations",
            "",
            "| Condition | Samples | Covered | RAM mean/max (MB) | GR3D mean/max (%) | Tj max (C) | VDD_IN mean/max (mW) |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for run in runs:
        resources = _mapping(run.get("resources"), "run resources")
        ram = _mapping(resources.get("ram_used_mb"), "RAM summary")
        gr3d = _mapping(resources.get("gr3d_usage_pct"), "GR3D summary")
        temperature = _mapping(
            resources.get("junction_temperature_c"), "temperature summary"
        )
        power = _mapping(resources.get("vdd_in_instant_mw"), "power summary")
        lines.append(
            "| `{condition}` | {samples} | {covered} | {ram_mean}/{ram_max} | "
            "{gpu_mean}/{gpu_max} | {temperature} | {power_mean}/{power_max} |".format(
                condition=run.get("condition"),
                samples=_format_number(resources.get("sample_count")),
                covered=_format_number(
                    resources.get("inference_interval_sample_count")
                ),
                ram_mean=_format_number(ram.get("mean")),
                ram_max=_format_number(ram.get("max")),
                gpu_mean=_format_number(gr3d.get("mean")),
                gpu_max=_format_number(gr3d.get("max")),
                temperature=_format_number(temperature.get("max")),
                power_mean=_format_number(power.get("mean")),
                power_max=_format_number(power.get("max")),
            )
        )
    limitations = quality.get("limitations")
    if not isinstance(limitations, list):
        raise ValueError("analysis limitations must be a list")
    lines.extend(["", "## Evidence gaps", ""])
    lines.extend(f"- `{item}`" for item in limitations)
    lines.extend(
        [
            "",
            (
                "The source runs used VLM preflight schema 0.1.0, which recorded a "
                "loopback request URL but not the TCP listener addresses. The operator "
                "verified loopback binding before execution, but that observation is not "
                "contained in the archived artifacts and is not elevated into a reproducible claim."
            ),
            "",
            (
                "GR3D activity confirms only that the device reported GPU activity during "
                "the run window. The trace does not attribute activity to Moondream, Qwen "
                "or a particular processor, so it does not authorize a heterogeneous-inference claim."
            ),
            "",
        ]
    )
    return "\n".join(lines)


def _refuse_output_inside_session(output: Path | None, session_dir: Path) -> None:
    if output is None:
        return
    try:
        output.resolve().relative_to(session_dir.resolve())
    except ValueError:
        return
    raise ValueError("analysis output must not be written inside the source session")


def _require_distinct_outputs(
    json_output: Path | None,
    markdown_output: Path | None,
) -> None:
    if (
        json_output is not None
        and markdown_output is not None
        and json_output.resolve() == markdown_output.resolve()
    ):
        raise ValueError("JSON and Markdown outputs must use distinct paths")


def _write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8", newline="\n")
    os.replace(temporary, path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session_dir", type=Path)
    parser.add_argument("--source-archive-sha256")
    parser.add_argument("--thread-reference-analysis", type=Path)
    parser.add_argument("--json-output", type=Path)
    parser.add_argument("--markdown-output", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    session_dir = args.session_dir.resolve()
    try:
        _refuse_output_inside_session(args.json_output, session_dir)
        _refuse_output_inside_session(args.markdown_output, session_dir)
        _require_distinct_outputs(args.json_output, args.markdown_output)
        analysis = analyze_vlm_pilot_dir(
            session_dir,
            source_archive_sha256=args.source_archive_sha256,
            thread_reference_analysis=args.thread_reference_analysis,
        )
        json_text = json.dumps(
            analysis,
            ensure_ascii=False,
            indent=2,
            allow_nan=False,
        )
        if args.json_output is not None:
            _write_text_atomic(args.json_output, json_text + "\n")
        else:
            print(json_text)
        if args.markdown_output is not None:
            markdown_text = render_markdown(analysis).rstrip("\n") + "\n"
            _write_text_atomic(args.markdown_output, markdown_text)
    except (OSError, TypeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
