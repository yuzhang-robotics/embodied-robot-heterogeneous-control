"""Independently validate and analyze the Phase 1 G6 formal collection."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import statistics
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import numpy as np

from experiments.phase1.asr_adapter import (
    ASR_EXPECTED_OUTPUT_LENGTH,
    ASR_EXPECTED_OUTPUT_SHA256,
    ASR_INPUT_MEDIA_TYPE,
)
from experiments.phase1.formal_preflight import (
    FROZEN_PROTOCOL_SHA256,
    formal_preflight_errors,
)
from experiments.phase1.formal_protocol import (
    ASYNC_MAX_GAP_P95_MS,
    BOOTSTRAP_RESAMPLES,
    BOOTSTRAP_SEED,
    FORMAL_PROTOCOL_ID,
    NONINFERIORITY_RATIO,
    PAIRS_PER_SESSION,
    SESSION_COUNT,
    WORKLOADS,
    formal_protocol_errors,
    protocol_sha256,
)
from experiments.phase1.jetson_telemetry import (
    load_resource_samples,
    validate_resource_samples,
)
from experiments.phase1.llm_adapter import (
    LLM_EXPECTED_SERVED_MODEL_ID,
    LLM_INPUT_MEDIA_TYPE,
    frozen_llm_request_contract,
)
from experiments.phase1.manifest import sha256_file, write_json_atomic
from experiments.phase1.telemetry import SCHEMA_VERSION
from experiments.phase1.vlm_adapter import C100_INPUT_MEDIA_TYPE
from jetson.vlm_request_contract import (
    MODEL_UNLOAD_POLL_INTERVAL_S,
    MODEL_UNLOAD_TIMEOUT_S,
)


FORMAL_ANALYSIS_SCHEMA_VERSION = "0.1.0"
FORMAL_SESSION_SCHEMA_VERSION = "0.1.0"
FORMAL_RUN_SCHEMA_VERSION = "0.1.0"
_SESSION_ATTEMPT_RE = re.compile(
    r"^session-(?P<session>0[1-5])-attempt-(?P<attempt>[0-9]{2})$"
)
_INFRASTRUCTURE_FAILURES = {
    "host_power_loss",
    "device_reboot",
    "unrecoverable_model_service_crash",
    "resource_sampler_failure",
}


@dataclass(frozen=True)
class FormalRecord:
    session: str
    block: int
    workload: str
    condition: str
    order: int
    run_dir: Path
    run: dict[str, Any]
    events: tuple[dict[str, Any], ...]
    resources: tuple[dict[str, Any], ...]


def nearest_rank(values: Iterable[float], percentile: float) -> float:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        raise ValueError("values must not be empty")
    if not 0 < percentile <= 100:
        raise ValueError("percentile must be in (0, 100]")
    return ordered[math.ceil(percentile / 100 * len(ordered)) - 1]


def descriptive(values: Iterable[float]) -> dict[str, float | int]:
    items = [float(value) for value in values]
    if not items or not all(math.isfinite(value) for value in items):
        raise ValueError("descriptive statistics require finite values")
    mean = statistics.fmean(items)
    sample_stddev = statistics.stdev(items) if len(items) > 1 else 0.0
    return {
        "count": len(items),
        "mean": mean,
        "median": statistics.median(items),
        "sample_stddev": sample_stddev,
        "cv_pct": sample_stddev / mean * 100 if mean else 0.0,
        "p95_nearest_rank": nearest_rank(items, 95),
        "min": min(items),
        "max": max(items),
    }


def _percentile_interval(values: np.ndarray) -> dict[str, float]:
    low, high = np.quantile(values, (0.025, 0.975))
    return {"low": float(low), "high": float(high)}


def paired_hierarchical_bootstrap(
    differences: Mapping[str, Sequence[Sequence[float]]],
    log_ratios: Mapping[str, Sequence[Sequence[float]]],
    *,
    resamples: int = BOOTSTRAP_RESAMPLES,
    seed: int = BOOTSTRAP_SEED,
) -> dict[str, dict[str, dict[str, float]]]:
    """Apply one shared session/block resampling stream to every workload."""

    if isinstance(resamples, bool) or not isinstance(resamples, int) or resamples <= 0:
        raise ValueError("resamples must be a positive integer")
    if set(differences) != set(WORKLOADS) or set(log_ratios) != set(WORKLOADS):
        raise ValueError("bootstrap inputs must cover every workload")
    difference_arrays = {
        workload: np.asarray(differences[workload], dtype=float)
        for workload in WORKLOADS
    }
    ratio_arrays = {
        workload: np.asarray(log_ratios[workload], dtype=float)
        for workload in WORKLOADS
    }
    expected_shape = (SESSION_COUNT, PAIRS_PER_SESSION)
    if any(array.shape != expected_shape for array in difference_arrays.values()):
        raise ValueError("difference arrays do not match the frozen design")
    if any(array.shape != expected_shape for array in ratio_arrays.values()):
        raise ValueError("ratio arrays do not match the frozen design")
    if any(
        not np.all(np.isfinite(array))
        for array in (*difference_arrays.values(), *ratio_arrays.values())
    ):
        raise ValueError("bootstrap inputs must be finite")

    rng = np.random.default_rng(seed)
    difference_samples = {workload: [] for workload in WORKLOADS}
    ratio_samples = {workload: [] for workload in WORKLOADS}
    remaining = resamples
    chunk_size = min(10_000, resamples)
    while remaining:
        current = min(chunk_size, remaining)
        selected_sessions = rng.integers(
            0,
            SESSION_COUNT,
            size=(current, SESSION_COUNT),
        )
        selected_blocks = rng.integers(
            0,
            PAIRS_PER_SESSION,
            size=(current, SESSION_COUNT, PAIRS_PER_SESSION),
        )
        for workload in WORKLOADS:
            selected_differences = []
            selected_log_ratios = []
            for slot in range(SESSION_COUNT):
                session_indices = selected_sessions[:, slot, None]
                block_indices = selected_blocks[:, slot, :]
                selected_differences.append(
                    difference_arrays[workload][session_indices, block_indices]
                )
                selected_log_ratios.append(
                    ratio_arrays[workload][session_indices, block_indices]
                )
            difference_samples[workload].append(
                np.concatenate(selected_differences, axis=1).mean(axis=1)
            )
            ratio_samples[workload].append(
                np.exp(np.concatenate(selected_log_ratios, axis=1).mean(axis=1))
            )
        remaining -= current
    return {
        workload: {
            "paired_mean_difference_ci95": _percentile_interval(
                np.concatenate(difference_samples[workload])
            ),
            "paired_geometric_mean_ratio_ci95": _percentile_interval(
                np.concatenate(ratio_samples[workload])
            ),
        }
        for workload in WORKLOADS
    }


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return value


def _read_jsonl(path: Path) -> tuple[dict[str, Any], ...]:
    values: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number}: invalid JSON") from exc
            if not isinstance(value, dict):
                raise ValueError(f"{path}:{line_number}: expected an object")
            values.append(value)
    return tuple(values)


def _parse_time(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("formal timestamp is missing")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("formal timestamp has no timezone")
    return parsed.astimezone(timezone.utc)


def _verify_artifacts(session_dir: Path, manifest: Mapping[str, object]) -> None:
    inventory = manifest.get("artifacts")
    if not isinstance(inventory, Mapping) or not inventory:
        raise ValueError(f"{session_dir}: artifact inventory is missing")
    observed = {
        path.relative_to(session_dir).as_posix()
        for path in session_dir.rglob("*")
        if path.is_file() and path.name != "manifest.json"
    }
    if set(inventory) != observed:
        raise ValueError(f"{session_dir}: artifact inventory does not match files")
    for relative, identity in inventory.items():
        if not isinstance(relative, str) or not isinstance(identity, Mapping):
            raise ValueError(f"{session_dir}: invalid artifact identity")
        path = session_dir / relative
        if identity.get("size_bytes") != path.stat().st_size or identity.get(
            "sha256"
        ) != sha256_file(path):
            raise ValueError(f"{path}: artifact identity mismatch")


def _thermal_gate_valid(value: object) -> bool:
    if not isinstance(value, Mapping):
        return False
    temperatures = value.get("observed_tj_c")
    first = value.get("first_sequence")
    last = value.get("last_sequence")
    return (
        value.get("maximum_tj_c") == 55.0
        and value.get("consecutive_samples") == 10
        and isinstance(temperatures, list)
        and len(temperatures) == 10
        and all(
            isinstance(item, (int, float))
            and not isinstance(item, bool)
            and math.isfinite(item)
            and item <= 55.0
            for item in temperatures
        )
        and _nonnegative_int(first)
        and _nonnegative_int(last)
        and last - first == 9
    )


def _validate_service_identity(value: object) -> None:
    if not isinstance(value, Mapping) or set(value) != {"llama-server", "ollama"}:
        raise ValueError("formal service identity is incomplete")
    for name in ("llama-server", "ollama"):
        service = value.get(name)
        service_record = service if isinstance(service, Mapping) else {}
        identities = service_record.get("process_start_identities")
        if (
            service_record.get("process_count") != 1
            or not isinstance(identities, list)
            or len(identities) != 1
            or not isinstance(identities[0], str)
            or not identities[0]
            or service_record.get("arguments_recorded") is not False
        ):
            raise ValueError(f"formal {name} service identity is invalid")


def _expected_entries(
    protocol_session: Mapping[str, object]
) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    ordinal = 0
    warmups = protocol_session.get("warmups")
    if not isinstance(warmups, list):
        raise ValueError("protocol warmups are missing")
    for warmup in warmups:
        if not isinstance(warmup, Mapping):
            raise ValueError("protocol warmup is not an object")
        ordinal += 1
        entry = dict(warmup)
        entry["condition"] = "formal_sync"
        entry["ordinal"] = ordinal
        entries.append(entry)
    measured = protocol_session.get("measured_runs")
    if not isinstance(measured, list):
        raise ValueError("protocol measured runs are missing")
    for planned in measured:
        if not isinstance(planned, Mapping):
            raise ValueError("protocol measured run is not an object")
        ordinal += 1
        entry = dict(planned)
        entry["ordinal"] = ordinal
        entries.append(entry)
    return entries


def _expected_ledger(
    entries: Sequence[Mapping[str, object]],
) -> list[tuple[str, Mapping[str, object] | str]]:
    warmups = [entry for entry in entries if entry.get("role") == "warmup"]
    measured = [entry for entry in entries if entry.get("role") == "measured"]
    expected: list[tuple[str, Mapping[str, object] | str]] = []
    for entry in warmups:
        expected.extend((("entry_started", entry), ("entry_completed", entry)))
    expected.extend(
        (("idle_started", "pre_measurement"), ("idle_completed", "pre_measurement"))
    )
    for entry in measured:
        expected.extend((("entry_started", entry), ("entry_completed", entry)))
    expected.extend(
        (("idle_started", "post_measurement"), ("idle_completed", "post_measurement"))
    )
    return expected


def _validate_ledger(
    session_dir: Path,
    ledger: Sequence[Mapping[str, object]],
    entries: Sequence[Mapping[str, object]],
) -> list[str]:
    expected = _expected_ledger(entries)
    if len(ledger) != len(expected):
        raise ValueError(f"{session_dir}: ledger length does not match the protocol")
    run_paths: list[str] = []
    for index, (item, (event, payload)) in enumerate(zip(ledger, expected)):
        if item.get("event") != event:
            raise ValueError(f"{session_dir}: ledger event {index} is reordered")
        if isinstance(payload, Mapping):
            if item.get("plan") != payload:
                raise ValueError(f"{session_dir}: ledger plan {index} was modified")
        elif item.get("label") != payload:
            raise ValueError(f"{session_dir}: ledger idle epoch {index} was modified")
        elif event == "idle_completed" and item.get("run") != f"idle/{payload}":
            raise ValueError(f"{session_dir}: ledger idle path {index} was modified")
        if event == "entry_completed":
            if item.get("valid") is not True or not isinstance(item.get("run"), str):
                raise ValueError(f"{session_dir}: completed entry is not valid")
            role_dir = "warmups" if payload.get("role") == "warmup" else "measured"
            expected_path = (
                f"{role_dir}/{int(payload['ordinal']):03d}-"
                f"{payload['workload']}-{payload['condition']}"
            )
            if item["run"] != expected_path:
                raise ValueError(
                    f"{session_dir}: completed entry path does not match the protocol"
                )
            run_paths.append(str(item["run"]))
    if len(run_paths) != len(entries) or len(set(run_paths)) != len(run_paths):
        raise ValueError(f"{session_dir}: ledger run paths are missing or duplicated")
    return run_paths


def _nonnegative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _positive_int(value: object) -> bool:
    return _nonnegative_int(value) and value > 0


def _expected_gate_names(
    workload: str,
    condition: str,
    workload_contract: Mapping[str, object],
) -> set[str]:
    names = {
        "adapter_completed",
        "output_private",
        "cancellation_absent",
        "probe_closed",
        "thermal_stop_absent",
    }
    if condition == "formal_sync":
        names.update({"synchronous_call_boundary", "runtime_not_used"})
    elif condition == "formal_async":
        names.update({"single_consumed_request", "bounded_lane", "worker_joined"})
    if workload == "asr":
        names.update({"transcript_identity", "child_process_reaped"})
    elif workload == "llm":
        names.update(
            {
                "request_contract_verified",
                "token_usage_valid",
                "server_residency_claim_bounded",
            }
        )
    elif workload == "vlm":
        names.update(
            {
                "translation_route_verified",
                "child_process_reaped",
                "model_unload_claim_bounded",
            }
        )
        if "process_protocol_version" in workload_contract:
            names.add("residency_contract_verified")
    return names


def _run_gate_errors(
    run: Mapping[str, object],
    entry: Mapping[str, object],
    workload_contract: Mapping[str, object],
    *,
    expected_failed_gates: frozenset[str] = frozenset(),
) -> list[str]:
    errors: list[str] = []
    expected_status = "failed" if expected_failed_gates else "completed"
    expected_valid = not expected_failed_gates
    if run.get("formal_run_schema_version") != FORMAL_RUN_SCHEMA_VERSION:
        errors.append("unsupported run schema")
    if run.get("status") != expected_status or run.get("valid") is not expected_valid:
        errors.append("run status does not match its expected Gate outcome")
    if run.get("raw_input_recorded") is not False:
        errors.append("raw input privacy flag is invalid")
    if run.get("raw_output_recorded") is not False:
        errors.append("raw output privacy flag is invalid")
    workload = str(entry.get("workload"))
    condition = str(entry.get("condition"))
    if (
        run.get("workload") != workload
        or run.get("condition") != condition
        or run.get("role") != entry.get("role")
        or run.get("plan") != entry
    ):
        errors.append("run identity does not match the protocol entry")

    started = run.get("started_monotonic_ns")
    finished = run.get("finished_monotonic_ns")
    duration = run.get("duration_ns")
    if (
        not _nonnegative_int(started)
        or not _positive_int(finished)
        or finished <= started
        or duration != finished - started
    ):
        errors.append("run timing facts are inconsistent")

    input_record = run.get("input")
    input_map = input_record if isinstance(input_record, Mapping) else {}
    media_type = {
        "asr": ASR_INPUT_MEDIA_TYPE,
        "llm": LLM_INPUT_MEDIA_TYPE,
        "vlm": C100_INPUT_MEDIA_TYPE,
    }.get(workload)
    if (
        input_map.get("sha256") != workload_contract.get("input_sha256")
        or input_map.get("size_bytes") != workload_contract.get("input_size_bytes")
        or input_map.get("media_type") != media_type
        or input_map.get("path_recorded") is not False
    ):
        errors.append("run input identity does not match the frozen workload")

    adapter = run.get("adapter")
    adapter_record = adapter if isinstance(adapter, Mapping) else {}
    adapter_started = adapter_record.get("started_monotonic_ns")
    adapter_finished = adapter_record.get("finished_monotonic_ns")
    adapter_duration = adapter_record.get("duration_ns")
    adapter_interval_inside_run = (
        _nonnegative_int(started)
        and _positive_int(finished)
        and _nonnegative_int(adapter_started)
        and _positive_int(adapter_finished)
        and started <= adapter_started <= adapter_finished <= finished
    )
    if (
        adapter_record.get("task_id") != run.get("task_id")
        or adapter_record.get("execution_outcome") != "ok"
        or adapter_record.get("error_code") is not None
        or not _nonnegative_int(adapter_started)
        or not _positive_int(adapter_finished)
        or adapter_finished <= adapter_started
        or adapter_duration != adapter_finished - adapter_started
        or not adapter_interval_inside_run
    ):
        errors.append("adapter completion facts are inconsistent")
    adapter_input = adapter_record.get("input")
    adapter_input_record = adapter_input if isinstance(adapter_input, Mapping) else {}
    if (
        adapter_input_record.get("sha256") != input_map.get("sha256")
        or adapter_input_record.get("size_bytes") != input_map.get("size_bytes")
        or adapter_input_record.get("media_type") != media_type
        or (
            workload == "llm"
            and adapter_input_record.get("raw_text_recorded") is not False
        )
    ):
        errors.append("adapter input identity does not match the run")
    output = adapter_record.get("output")
    output_record = output if isinstance(output, Mapping) else {}
    if (
        not isinstance(output, Mapping)
        or not isinstance(output_record.get("sha256"), str)
        or len(str(output_record.get("sha256"))) != 64
        or not _positive_int(output_record.get("length"))
        or output_record.get("raw_text_recorded") is not False
        or "output_ref" in adapter_record
    ):
        errors.append("adapter output privacy or identity facts are invalid")
    cancellation = adapter_record.get("cancellation")
    cancellation_record = cancellation if isinstance(cancellation, Mapping) else {}
    if cancellation_record.get("requested") is not False:
        errors.append("adapter cancellation was requested")
    result = run.get("result")
    result_record = result if isinstance(result, Mapping) else {}
    result_output = result_record.get("output")
    result_output_record = result_output if isinstance(result_output, Mapping) else {}
    result_cancellation = result_record.get("cancellation")
    result_cancellation_record = (
        result_cancellation if isinstance(result_cancellation, Mapping) else {}
    )
    result_source = result_record.get("source_monotonic_ns")
    result_deadline = result_record.get("deadline_monotonic_ns")
    result_interval_valid = (
        _nonnegative_int(result_source)
        and _positive_int(result_deadline)
        and _nonnegative_int(adapter_started)
        and _positive_int(adapter_finished)
        and result_source <= adapter_started <= adapter_finished <= result_deadline
    )
    if (
        result_record.get("task_id") != run.get("task_id")
        or result_record.get("task_kind") != workload
        or result_record.get("state_scope_id") != "phase1-formal"
        or result_record.get("state_generation") != 0
        or result_record.get("input_sha256") != input_map.get("sha256")
        or not result_interval_valid
        or result_record.get("started_monotonic_ns") != adapter_started
        or result_record.get("finished_monotonic_ns") != adapter_finished
        or result_record.get("execution_outcome") != "ok"
        or result_record.get("error_code") is not None
        or result_output_record != output_record
        or result_record.get("output_ref_recorded") is not False
        or result_cancellation_record.get("requested") is not False
    ):
        errors.append("result envelope does not match adapter completion")
    if run.get("thermal_stop_requested") is not False:
        errors.append("thermal stop was requested during the run")

    gates = run.get("gates")
    gate_items = gates if isinstance(gates, list) else []
    names: set[str] = set()
    if not gate_items:
        errors.append("run gates are missing")
    else:
        for gate in gate_items:
            if not isinstance(gate, Mapping):
                errors.append("run gate is not an object")
                continue
            name = gate.get("name")
            if not isinstance(name, str) or name in names:
                errors.append("run gate name is invalid or duplicated")
            else:
                names.add(name)
            expected_passed = name not in expected_failed_gates
            if gate.get("passed") is not expected_passed:
                if expected_passed:
                    errors.append(f"run gate failed: {name}")
                else:
                    errors.append(f"expected run gate did not fail: {name}")
    if names != _expected_gate_names(workload, condition, workload_contract):
        errors.append("run gate set is incomplete or unsupported")
    if not expected_failed_gates.issubset(names):
        errors.append("expected failed Gate set is incomplete or unsupported")

    probe = run.get("probe")
    runtime = run.get("runtime")
    probe_record = probe if isinstance(probe, Mapping) else {}
    runtime_record = runtime if isinstance(runtime, Mapping) else {}
    if (
        probe_record.get("joined") is not True
        or probe_record.get("error_code") is not None
        or not _positive_int(probe_record.get("tick_count"))
        or probe_record.get("tick_count", 0) < 2
        or not _nonnegative_int(probe_record.get("skipped_releases"))
        or not _nonnegative_int(probe_record.get("deadline_miss_count"))
        or not _nonnegative_int(probe_record.get("max_lateness_ns"))
        or not _nonnegative_int(probe_record.get("max_gap_ns"))
    ):
        errors.append("probe closure facts are invalid")
    if condition == "formal_sync":
        if (
            probe_record.get("implementation") != "inline_same_thread"
            or runtime_record.get("used") is not False
            or runtime_record.get("pending_capacity") != 0
            or runtime_record.get("result_capacity") != 0
            or runtime_record.get("final_snapshot") is not None
            or runtime_record.get("shutdown") is not None
        ):
            errors.append("formal_sync execution boundary is invalid")
        gate = next(
            (
                item
                for item in gate_items
                if isinstance(item, Mapping)
                and item.get("name") == "synchronous_call_boundary"
            ),
            {},
        )
        observed = gate.get("observed") if isinstance(gate, Mapping) else None
        boundary = observed if isinstance(observed, Mapping) else {}
        if workload == "vlm":
            process = run.get("process")
            process_record = process if isinstance(process, Mapping) else {}
            if (
                process_record.get("start_method") != "spawn"
                or process_record.get("protocol_complete") is not True
            ):
                errors.append("formal_sync VLM process boundary is invalid")
        elif adapter_record.get("worker_thread_id") != boundary.get(
            "adapter_thread_id"
        ) or boundary.get("adapter_thread_id") != boundary.get("calling_thread_id"):
            errors.append("formal_sync calling-thread boundary is invalid")
    elif condition == "formal_async":
        if (
            probe_record.get("implementation") != "independent_thread"
            or runtime_record.get("used") is not True
            or runtime_record.get("pending_capacity") != 1
            or runtime_record.get("result_capacity") != 1
        ):
            errors.append("formal_async execution boundary is invalid")
        snapshot = runtime_record.get("final_snapshot")
        snapshot_record = snapshot if isinstance(snapshot, Mapping) else {}
        dispositions = snapshot_record.get("disposition_counts")
        disposition_record = dispositions if isinstance(dispositions, Mapping) else {}
        if (
            snapshot_record.get("state") != "closed"
            or snapshot_record.get("submission_attempts") != 1
            or snapshot_record.get("admitted_total") != 1
            or snapshot_record.get("rejected_at_ingress_total") != 0
            or snapshot_record.get("terminal_admitted_total") != 1
            or snapshot_record.get("queued") != 0
            or snapshot_record.get("running") != 0
            or snapshot_record.get("result_pending") != 0
            or snapshot_record.get("max_pending_depth", 2) > 1
            or snapshot_record.get("max_result_depth", 2) > 1
            or snapshot_record.get("accounting_holds") is not True
            or disposition_record.get("consumed") != 1
            or dict(disposition_record) != {"consumed": 1}
        ):
            errors.append("formal_async lifecycle accounting is invalid")
        shutdown = runtime_record.get("shutdown")
        shutdown_record = shutdown if isinstance(shutdown, Mapping) else {}
        if (
            shutdown_record.get("complete") is not True
            or shutdown_record.get("broker_state") != "closed"
            or shutdown_record.get("joined") is not True
            or shutdown_record.get("worker_error_code") is not None
            or shutdown_record.get("event_error_code") is not None
            or not _nonnegative_int(shutdown_record.get("join_latency_ns"))
        ):
            errors.append("formal_async worker did not close cleanly")
    else:
        errors.append("unsupported formal condition")

    if workload == "asr":
        process = adapter_record.get("process")
        process_record = process if isinstance(process, Mapping) else {}
        if (
            output_record.get("sha256") != ASR_EXPECTED_OUTPUT_SHA256
            or output_record.get("length") != ASR_EXPECTED_OUTPUT_LENGTH
        ):
            errors.append("ASR transcript identity does not match the frozen output")
        if (
            process_record.get("started") is not True
            or process_record.get("exit_code") != 0
            or process_record.get("reaped") is not True
            or process_record.get("terminate_requested") is not False
            or process_record.get("kill_requested") is not False
        ):
            errors.append("ASR child process did not close normally")
    elif workload == "llm":
        request = adapter_record.get("request")
        expected_request = {
            **frozen_llm_request_contract(),
            "raw_prompt_recorded": False,
        }
        response = adapter_record.get("response")
        response_record = response if isinstance(response, Mapping) else {}
        usage = response_record.get("usage")
        usage_record = usage if isinstance(usage, Mapping) else {}
        residency = adapter_record.get("model_residency")
        residency_record = residency if isinstance(residency, Mapping) else {}
        if request != expected_request:
            errors.append("LLM request does not match the frozen contract")
        if (
            response_record.get("model") != LLM_EXPECTED_SERVED_MODEL_ID
            or response_record.get("raw_response_recorded") is not False
            or any(
                not _positive_int(usage_record.get(name))
                for name in ("prompt_tokens", "completion_tokens", "total_tokens")
            )
            or usage_record.get("total_tokens")
            != usage_record.get("prompt_tokens", 0)
            + usage_record.get("completion_tokens", 0)
        ):
            errors.append("LLM response identity or token usage is invalid")
        if (
            residency_record.get("policy") != "external_llama_server_resident"
            or residency_record.get("server_preexisting") is not True
            or residency_record.get("unload_requested") is not False
            or residency_record.get("backend_stop_confirmed") is not None
        ):
            errors.append("LLM server residency facts are invalid")
    elif workload == "vlm":
        process = run.get("process")
        process_record = process if isinstance(process, Mapping) else {}
        residency = adapter_record.get("model_residency")
        residency_record = residency if isinstance(residency, Mapping) else {}
        stage_status_value = adapter_record.get("stage_status")
        stage_status = (
            stage_status_value if isinstance(stage_status_value, Mapping) else {}
        )
        stage_errors_value = adapter_record.get("stage_error_codes")
        stage_errors = (
            stage_errors_value if isinstance(stage_errors_value, Mapping) else {}
        )
        unload_confirmation = workload_contract.get("unload_confirmation")
        unload_confirmation_required = isinstance(unload_confirmation, Mapping)
        expected_unload_confirmed = True if unload_confirmation_required else None
        observed_unload_confirmed = residency_record.get("unload_confirmed")
        if (
            "translation_route_verified" not in expected_failed_gates
            and adapter_record.get("translation_route") != "qwen"
        ):
            errors.append("VLM translation route does not match the protocol")
        if (
            process_record.get("start_method") != "spawn"
            or process_record.get("protocol_complete") is not True
            or process_record.get("exit_code") != 0
            or process_record.get("error_code") is not None
            or not _positive_int(process_record.get("joined_monotonic_ns"))
        ):
            errors.append("VLM child process did not close normally")
        if (
            residency_record.get("unload_requested") is not True
            or observed_unload_confirmed is not expected_unload_confirmed
        ):
            errors.append("VLM model unload claim is invalid")
        expected_residency_policy = (
            "moondream_unload_confirmed_before_qwen_per_invocation"
            if unload_confirmation_required
            else "moondream_unload_requested_before_qwen_per_invocation"
        )
        unload_contract_valid = (
            dict(unload_confirmation)
            == {
                "method": "ollama_process_list_absence",
                "timeout_s": MODEL_UNLOAD_TIMEOUT_S,
                "poll_interval_ms": int(MODEL_UNLOAD_POLL_INTERVAL_S * 1_000),
            }
            if unload_confirmation_required
            else unload_confirmation == "not_available"
        )
        if "process_protocol_version" in workload_contract and (
            workload_contract.get("process_protocol_version") != "0.2.0"
            or workload_contract.get("residency_policy")
            != expected_residency_policy
            or workload_contract.get("successful_stage_order")
            != [
                "input_verify_before",
                "module_import",
                "moondream_inference",
                "model_unload",
                "qwen_rewrite",
                "output_normalization",
                "input_verify_after",
            ]
            or workload_contract.get("cleanup_unload_on_failure") is not True
            or not unload_contract_valid
            or process_record.get("protocol_version")
            != workload_contract.get("process_protocol_version")
            or set(stage_status)
            != set(workload_contract.get("successful_stage_order", []))
            or any(value != "ok" for value in stage_status.values())
            or bool(stage_errors)
        ):
            errors.append("VLM residency-order execution contract is invalid")
    return errors


def _validate_events(
    events: Sequence[Mapping[str, object]],
    run: Mapping[str, object],
) -> None:
    if not events:
        raise ValueError("formal event trace is empty")
    previous_monotonic_ns = -1
    names: set[str] = set()
    run_id = run.get("run_id")
    started = run.get("started_monotonic_ns")
    finished = run.get("finished_monotonic_ns")
    for expected_sequence, event in enumerate(events):
        monotonic_ns = event.get("monotonic_ns")
        if (
            event.get("schema_version") != SCHEMA_VERSION
            or event.get("run_id") != run_id
            or event.get("seq") != expected_sequence
            or not isinstance(event.get("event"), str)
            or not isinstance(event.get("component"), str)
            or not isinstance(event.get("status"), str)
            or not _nonnegative_int(monotonic_ns)
            or monotonic_ns < previous_monotonic_ns
            or not _positive_int(event.get("wall_time_ns"))
            or not _positive_int(event.get("pid"))
            or not _positive_int(event.get("thread_id"))
            or not isinstance(event.get("details"), Mapping)
        ):
            raise ValueError("formal event trace is malformed")
        if (
            _nonnegative_int(started)
            and _positive_int(finished)
            and not started <= monotonic_ns <= finished
        ):
            raise ValueError("formal event lies outside the run interval")
        previous_monotonic_ns = monotonic_ns
        names.add(str(event["event"]))
    required = {"probe.started", "probe.tick", "probe.stopped"}
    if run.get("condition") == "formal_async":
        required.update(
            {"task.enqueued", "task.started", "task.finished", "result.accepted"}
        )
    elif run.get("condition") == "formal_idle":
        required.update({"formal.idle_started", "formal.idle_stopped"})
    if not required.issubset(names):
        raise ValueError("formal event trace is missing lifecycle boundaries")


def _idle_run_errors(
    run: Mapping[str, object],
    *,
    label: str,
    duration_s: int,
) -> list[str]:
    errors: list[str] = []
    started = run.get("started_monotonic_ns")
    finished = run.get("finished_monotonic_ns")
    if (
        run.get("formal_run_schema_version") != FORMAL_RUN_SCHEMA_VERSION
        or run.get("status") != "completed"
        or run.get("role") != "idle_reference"
        or run.get("condition") != "formal_idle"
        or run.get("label") != label
        or run.get("duration_s") != duration_s
        or run.get("valid") is not True
    ):
        errors.append("idle record does not match the protocol")
    if (
        not _nonnegative_int(started)
        or not _positive_int(finished)
        or finished - started < duration_s * 1_000_000_000
    ):
        errors.append("idle duration is shorter than the protocol")
    probe = run.get("probe")
    probe_record = probe if isinstance(probe, Mapping) else {}
    if (
        probe_record.get("implementation") != "independent_thread"
        or probe_record.get("joined") is not True
        or probe_record.get("error_code") is not None
        or not _positive_int(probe_record.get("tick_count"))
        or probe_record.get("tick_count", 0) < 2
        or not _nonnegative_int(probe_record.get("skipped_releases"))
        or not _nonnegative_int(probe_record.get("deadline_miss_count"))
        or not _nonnegative_int(probe_record.get("max_lateness_ns"))
        or not _nonnegative_int(probe_record.get("max_gap_ns"))
    ):
        errors.append("idle probe did not close cleanly")
    return errors


def _resource_slice(
    samples: Sequence[dict[str, Any]],
    run: Mapping[str, object],
) -> tuple[dict[str, Any], ...]:
    started = run.get("started_monotonic_ns")
    finished = run.get("finished_monotonic_ns")
    if (
        isinstance(started, bool)
        or not isinstance(started, int)
        or isinstance(finished, bool)
        or not isinstance(finished, int)
        or finished <= started
    ):
        raise ValueError("run monotonic interval is invalid")
    selected = tuple(
        sample
        for sample in samples
        if started <= sample["sample_monotonic_ns"] <= finished
    )
    if not selected:
        raise ValueError("resource samples do not cover a formal run")
    return selected


def _validate_lifecycle(run: Mapping[str, object]) -> dict[str, int]:
    counts = {
        "stale_consumed_count": 0,
        "capacity_violation_count": 0,
        "unreaped_process_count": 0,
        "unjoined_thread_count": 0,
    }
    runtime = run.get("runtime")
    runtime_record = runtime if isinstance(runtime, Mapping) else {}
    if runtime_record.get("used") is True:
        snapshot = runtime_record.get("final_snapshot")
        snapshot_record = snapshot if isinstance(snapshot, Mapping) else {}
        if (
            snapshot_record.get("max_pending_depth", 2) > 1
            or snapshot_record.get("max_result_depth", 2) > 1
            or snapshot_record.get("accounting_holds") is not True
        ):
            counts["capacity_violation_count"] += 1
        dispositions = snapshot_record.get("disposition_counts")
        disposition_record = dispositions if isinstance(dispositions, Mapping) else {}
        counts["stale_consumed_count"] += int(
            disposition_record.get("rejected_state", 0) > 0
            and disposition_record.get("consumed", 0) > 0
        )
        shutdown = runtime_record.get("shutdown")
        shutdown_record = shutdown if isinstance(shutdown, Mapping) else {}
        if shutdown_record.get("joined") is not True:
            counts["unjoined_thread_count"] += 1
    probe = run.get("probe")
    probe_record = probe if isinstance(probe, Mapping) else {}
    if probe_record.get("joined") is not True:
        counts["unjoined_thread_count"] += 1
    workload = run.get("workload")
    adapter = run.get("adapter")
    adapter_record = adapter if isinstance(adapter, Mapping) else {}
    if workload == "asr":
        process = adapter_record.get("process")
        process_record = process if isinstance(process, Mapping) else {}
        if process_record.get("reaped") is not True:
            counts["unreaped_process_count"] += 1
    if workload == "vlm":
        process = run.get("process")
        process_record = process if isinstance(process, Mapping) else {}
        if (
            process_record.get("protocol_complete") is not True
            or process_record.get("exit_code") != 0
        ):
            counts["unreaped_process_count"] += 1
    return counts


def _performance_metric(run: Mapping[str, object]) -> float:
    workload = run.get("workload")
    adapter = run.get("adapter")
    adapter_record = adapter if isinstance(adapter, Mapping) else {}
    if workload in {"asr", "vlm"}:
        value = adapter_record.get("duration_ns")
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError("adapter duration is not positive")
        return value / 1_000_000
    if workload == "llm":
        stages = adapter_record.get("stage_durations_ns")
        stage_record = stages if isinstance(stages, Mapping) else {}
        response = adapter_record.get("response")
        response_record = response if isinstance(response, Mapping) else {}
        usage = response_record.get("usage")
        usage_record = usage if isinstance(usage, Mapping) else {}
        request_ns = stage_record.get("llama_inference")
        completion_tokens = usage_record.get("completion_tokens")
        if (
            isinstance(request_ns, bool)
            or not isinstance(request_ns, int)
            or request_ns <= 0
            or isinstance(completion_tokens, bool)
            or not isinstance(completion_tokens, int)
            or completion_tokens <= 0
        ):
            raise ValueError("LLM length-normalized timing inputs are invalid")
        return request_ns / 1_000_000 / completion_tokens
    raise ValueError("unsupported workload")


def _event_diagnostics(
    events: Sequence[Mapping[str, object]]
) -> dict[str, float | None]:
    by_name = {str(event.get("event")): event for event in events}
    enqueued = by_name.get("task.enqueued")
    started = by_name.get("task.started")
    finished = by_name.get("task.finished")
    accepted = by_name.get("result.accepted")

    def detail(event: Mapping[str, object] | None, name: str) -> int | None:
        details = event.get("details") if isinstance(event, Mapping) else None
        value = details.get(name) if isinstance(details, Mapping) else None
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    created_ns = detail(enqueued, "created_monotonic_ns")
    started_ns = detail(started, "started_monotonic_ns")
    finished_ns = detail(finished, "finished_monotonic_ns")
    terminal_ns = detail(accepted, "transition_monotonic_ns")
    return {
        "queue_wait_ms": (
            (started_ns - created_ns) / 1_000_000
            if started_ns is not None and created_ns is not None
            else None
        ),
        "result_age_ms": (
            (terminal_ns - finished_ns) / 1_000_000
            if terminal_ns is not None and finished_ns is not None
            else None
        ),
    }


def _resource_diagnostics(samples: Sequence[Mapping[str, object]]) -> dict[str, object]:
    ram = [float(sample["ram"]["used_mb"]) for sample in samples]
    gr3d = [float(sample["gr3d"]["usage_pct"]) for sample in samples]
    tj = [float(sample["temperatures_c"]["tj"]) for sample in samples]
    power = [float(sample["power"]["VDD_IN"]["instant_mw"]) for sample in samples]
    return {
        "sample_count": len(samples),
        "ram_used_mb": {"mean": statistics.fmean(ram), "peak": max(ram)},
        "gr3d_usage_pct": {"mean": statistics.fmean(gr3d), "peak": max(gr3d)},
        "tj_c": {"mean": statistics.fmean(tj), "peak": max(tj)},
        "vdd_in_mw": {"mean": statistics.fmean(power), "peak": max(power)},
    }


def _session_records(
    session_dir: Path,
    manifest: dict[str, Any],
    protocol: Mapping[str, object],
    protocol_session: Mapping[str, object],
) -> tuple[list[FormalRecord], dict[str, object]]:
    _verify_artifacts(session_dir, manifest)
    expected_session = str(protocol_session.get("session"))
    match = _SESSION_ATTEMPT_RE.fullmatch(session_dir.name)
    expected_attempt = int(match.group("attempt")) if match is not None else None
    if manifest.get("formal_session_schema_version") != FORMAL_SESSION_SCHEMA_VERSION:
        raise ValueError(f"{session_dir}: unsupported session schema")
    if (
        match is None
        or manifest.get("artifact_kind") != "phase1_g6_formal_session"
        or manifest.get("collection_id") != session_dir.parent.name
        or manifest.get("session_id") != session_dir.name
        or manifest.get("protocol_session") != expected_session
        or f"session-{match.group('session')}" != expected_session
        or manifest.get("attempt") != expected_attempt
    ):
        raise ValueError(f"{session_dir}: session identity does not match its path")
    if (
        manifest.get("status") != "completed"
        or manifest.get("formal_evidence_eligible") is not True
        or manifest.get("development_injection") is not False
    ):
        raise ValueError(f"{session_dir}: session is not formal-analysis eligible")
    protocol = _read_json(session_dir / "protocol.json")
    errors = formal_protocol_errors(protocol)
    if errors or protocol_sha256(protocol) != FROZEN_PROTOCOL_SHA256:
        raise ValueError(f"{session_dir}: protocol copy does not match G6")
    preflight = _read_json(session_dir / "preflight.json")
    preflight_failures = formal_preflight_errors(preflight)
    if preflight_failures:
        raise ValueError(
            f"{session_dir}: formal preflight is invalid: "
            + "; ".join(preflight_failures)
        )
    protocol_identity = preflight.get("protocol")
    protocol_record = (
        protocol_identity if isinstance(protocol_identity, Mapping) else {}
    )
    base = preflight.get("base")
    base_record = base if isinstance(base, Mapping) else {}
    environment = base_record.get("environment")
    environment_record = environment if isinstance(environment, Mapping) else {}
    git = environment_record.get("git")
    git_record = git if isinstance(git, Mapping) else {}
    if (
        protocol_record.get("id") != FORMAL_PROTOCOL_ID
        or protocol_record.get("sha256") != FROZEN_PROTOCOL_SHA256
        or protocol_record.get("path_recorded") is not False
        or protocol_record.get("runner_commit") != git_record.get("commit")
        or not re.fullmatch(r"[0-9a-f]{40}", str(protocol_record.get("runner_commit")))
        or not re.fullmatch(
            r"[0-9a-f]{40}", str(protocol_record.get("protocol_commit"))
        )
    ):
        raise ValueError(f"{session_dir}: preflight commit identity is inconsistent")
    _validate_service_identity(preflight.get("service_identity"))
    manifest_preflight = manifest.get("preflight")
    expected_manifest_preflight = {
        "protocol_commit": preflight.get("protocol", {}).get("protocol_commit"),
        "runner_commit": preflight.get("protocol", {}).get("runner_commit"),
        "service_identity": preflight.get("service_identity"),
    }
    if manifest_preflight != expected_manifest_preflight:
        raise ValueError(f"{session_dir}: manifest preflight identity was modified")
    if (
        manifest.get("protocol_id") != FORMAL_PROTOCOL_ID
        or manifest.get("protocol_sha256") != FROZEN_PROTOCOL_SHA256
        or manifest.get("completed_entries") != 41
    ):
        raise ValueError(f"{session_dir}: session manifest is not protocol-complete")
    thermal = manifest.get("thermal")
    thermal_record = thermal if isinstance(thermal, Mapping) else {}
    if (
        not _thermal_gate_valid(thermal_record.get("session_start"))
        or not _thermal_gate_valid(thermal_record.get("measurement_start"))
        or thermal_record.get("stop_tj_c") != 85.0
        or thermal_record.get("stop_requested") is not False
    ):
        raise ValueError(f"{session_dir}: thermal gates are incomplete")
    sampler = manifest.get("resource_sampler_report")
    sampler_record = sampler if isinstance(sampler, Mapping) else {}
    if sampler_record.get("successful") is not True:
        raise ValueError(f"{session_dir}: resource sampler was not successful")
    resources = load_resource_samples(session_dir / "resources.jsonl")
    resource_errors = validate_resource_samples(resources)
    if resource_errors:
        raise ValueError(f"{session_dir}: {'; '.join(resource_errors)}")
    if (
        sampler_record.get("sample_count") != len(resources)
        or sampler_record.get("parse_error_count") != 0
        or sampler_record.get("first_sample_monotonic_ns")
        != resources[0]["sample_monotonic_ns"]
        or sampler_record.get("last_sample_monotonic_ns")
        != resources[-1]["sample_monotonic_ns"]
        or sampler_record.get("stop_method") not in {"terminated", "killed"}
        or sampler_record.get("reader_joined") is not True
        or sampler_record.get("reader_error_code") is not None
    ):
        raise ValueError(f"{session_dir}: resource sampler report is inconsistent")
    entries = _expected_entries(protocol_session)
    workload_contracts = protocol.get("workloads")
    if not isinstance(workload_contracts, Mapping):
        raise ValueError(f"{session_dir}: workload contracts are missing")
    ledger = _read_jsonl(session_dir / "ledger.jsonl")
    run_paths = _validate_ledger(session_dir, ledger, entries)
    records: list[FormalRecord] = []
    for entry, relative in zip(entries, run_paths):
        run_dir = session_dir / relative
        run = _read_json(run_dir / "run.json")
        if run.get("plan") != entry:
            raise ValueError(f"{run_dir}: run plan does not match the ledger")
        workload_contract = workload_contracts.get(str(entry.get("workload")))
        if not isinstance(workload_contract, Mapping):
            raise ValueError(f"{run_dir}: workload contract is missing")
        run_errors = _run_gate_errors(run, entry, workload_contract)
        if run_errors:
            raise ValueError(f"{run_dir}: {'; '.join(run_errors)}")
        events = _read_jsonl(run_dir / "events.jsonl")
        _validate_events(events, run)
        selected_resources = _resource_slice(resources, run)
        if entry.get("role") == "measured":
            records.append(
                FormalRecord(
                    session=str(protocol_session["session"]),
                    block=int(entry["block"]),
                    workload=str(entry["workload"]),
                    condition=str(entry["condition"]),
                    order=int(entry["sequence"]),
                    run_dir=run_dir,
                    run=run,
                    events=events,
                    resources=selected_resources,
                )
            )
    if len(records) != 36:
        raise ValueError(f"{session_dir}: measured run count is not 36")
    idle_intervals: list[dict[str, Any]] = []
    idle_diagnostics: list[dict[str, object]] = []
    for label, protocol_key in (
        ("pre_measurement", "pre_measurement_idle"),
        ("post_measurement", "post_measurement_idle"),
    ):
        idle_plan = protocol_session.get(protocol_key)
        idle_plan_record = idle_plan if isinstance(idle_plan, Mapping) else {}
        duration_s = idle_plan_record.get("duration_s")
        if not _positive_int(duration_s):
            raise ValueError(f"{session_dir}: idle protocol is invalid")
        idle_dir = session_dir / "idle" / label
        idle_run = _read_json(idle_dir / "run.json")
        idle_errors = _idle_run_errors(
            idle_run,
            label=label,
            duration_s=duration_s,
        )
        if idle_errors:
            raise ValueError(f"{idle_dir}: {'; '.join(idle_errors)}")
        idle_events = _read_jsonl(idle_dir / "events.jsonl")
        _validate_events(idle_events, idle_run)
        idle_resources = _resource_slice(resources, idle_run)
        idle_intervals.append(idle_run)
        idle_probe = idle_run["probe"]
        idle_diagnostics.append(
            {
                "label": label,
                "duration_s": duration_s,
                "probe": {
                    "tick_count": idle_probe["tick_count"],
                    "skipped_releases": idle_probe["skipped_releases"],
                    "deadline_miss_count": idle_probe["deadline_miss_count"],
                    "max_lateness_ms": idle_probe["max_lateness_ns"] / 1_000_000,
                    "max_gap_ms": idle_probe["max_gap_ns"] / 1_000_000,
                },
                "resources": _resource_diagnostics(idle_resources),
            }
        )
    first_sample = resources[0]["sample_monotonic_ns"]
    last_sample = resources[-1]["sample_monotonic_ns"]
    run_intervals = [
        _read_json(session_dir / relative / "run.json") for relative in run_paths
    ]
    all_intervals = run_intervals + idle_intervals
    if first_sample > min(item["started_monotonic_ns"] for item in all_intervals):
        raise ValueError(f"{session_dir}: resources start after session activity")
    if last_sample < max(item["finished_monotonic_ns"] for item in all_intervals):
        raise ValueError(f"{session_dir}: resources stop before session activity")
    return records, {
        "session_id": manifest.get("session_id"),
        "attempt": manifest.get("attempt"),
        "created_at": manifest.get("created_at"),
        "completed_at": manifest.get("completed_at"),
        "runner_commit": preflight.get("protocol", {}).get("runner_commit"),
        "protocol_commit": preflight.get("protocol", {}).get("protocol_commit"),
        "service_identity": preflight.get("service_identity"),
        "resource_sample_count": len(resources),
        "idle_references": idle_diagnostics,
    }


def _discover(
    collection_root: Path,
    protocol: Mapping[str, object],
) -> tuple[list[FormalRecord], dict[str, object]]:
    manifests = sorted(collection_root.glob("session-*-attempt-*/manifest.json"))
    if not manifests:
        raise ValueError("formal collection contains no session attempts")
    attempts: list[dict[str, object]] = []
    attempts_by_session: dict[str, list[tuple[Path, dict[str, Any]]]] = {}
    completed: dict[str, list[tuple[Path, dict[str, Any]]]] = {}
    for path in manifests:
        manifest = _read_json(path)
        directory = path.parent
        match = _SESSION_ATTEMPT_RE.fullmatch(directory.name)
        protocol_session = str(manifest.get("protocol_session"))
        if (
            match is None
            or manifest.get("collection_id") != collection_root.name
            or manifest.get("session_id") != directory.name
            or protocol_session != f"session-{match.group('session')}"
            or manifest.get("attempt") != int(match.group("attempt"))
            or manifest.get("protocol_id") != FORMAL_PROTOCOL_ID
            or manifest.get("protocol_sha256") != FROZEN_PROTOCOL_SHA256
        ):
            raise ValueError(f"{directory}: attempt identity does not match its path")
        attempts.append(
            {
                "session_id": manifest.get("session_id"),
                "protocol_session": protocol_session,
                "attempt": manifest.get("attempt"),
                "status": manifest.get("status"),
                "failure_class": manifest.get("failure_class"),
                "failure_code": manifest.get("failure_code"),
                "replacement_for": manifest.get("replacement_for"),
                "infrastructure_failure": manifest.get("infrastructure_failure"),
            }
        )
        attempts_by_session.setdefault(protocol_session, []).append(
            (directory, manifest)
        )
        if manifest.get("status") == "completed":
            completed.setdefault(protocol_session, []).append((directory, manifest))
    expected_sessions = [f"session-{index:02d}" for index in range(1, 6)]
    if set(attempts_by_session) != set(expected_sessions):
        raise ValueError("formal collection attempt inventory is incomplete")
    for name in expected_sessions:
        session_attempts = sorted(
            attempts_by_session[name], key=lambda item: int(item[1]["attempt"])
        )
        observed_attempts = [int(item[1]["attempt"]) for item in session_attempts]
        if observed_attempts != list(range(1, len(session_attempts) + 1)):
            raise ValueError(f"{name}: replacement attempts are not contiguous")
        for index, (directory, manifest) in enumerate(session_attempts):
            if index == 0:
                if (
                    manifest.get("replacement_for") is not None
                    or manifest.get("infrastructure_failure") is not None
                ):
                    raise ValueError(f"{directory}: attempt 01 cannot be a replacement")
            else:
                previous_directory, previous = session_attempts[index - 1]
                if (
                    manifest.get("replacement_for") != previous_directory.name
                    or manifest.get("infrastructure_failure")
                    not in _INFRASTRUCTURE_FAILURES
                    or previous.get("status") not in {"running", "aborted"}
                    or (
                        previous.get("status") == "aborted"
                        and previous.get("failure_class") != "infrastructure"
                    )
                ):
                    raise ValueError(
                        f"{directory}: replacement attempt is not authorized"
                    )
            if (
                manifest.get("status") == "completed"
                and index != len(session_attempts) - 1
            ):
                raise ValueError(f"{directory}: attempts continue after completion")
    if set(completed) != set(expected_sessions) or any(
        len(completed[name]) != 1 for name in expected_sessions
    ):
        raise ValueError(
            "formal collection does not contain one complete attempt per session"
        )
    protocol_sessions = protocol.get("sessions")
    if not isinstance(protocol_sessions, list):
        raise ValueError("formal protocol sessions are missing")
    protocol_by_name = {
        str(session["session"]): session
        for session in protocol_sessions
        if isinstance(session, Mapping)
    }
    if set(protocol_by_name) != set(expected_sessions) or len(protocol_sessions) != len(
        expected_sessions
    ):
        raise ValueError("formal protocol session inventory is invalid")
    records: list[FormalRecord] = []
    sessions: dict[str, object] = {}
    previous_completed: datetime | None = None
    previous_services: object = None
    runner_commits: set[object] = set()
    protocol_commits: set[object] = set()
    for name in expected_sessions:
        directory, manifest = completed[name][0]
        session_records, metadata = _session_records(
            directory,
            manifest,
            protocol,
            protocol_by_name[name],
        )
        created = _parse_time(metadata["created_at"])
        completed_at = _parse_time(metadata["completed_at"])
        if completed_at <= created:
            raise ValueError(f"{directory}: session timestamps are invalid")
        if previous_completed is not None:
            if (created - previous_completed).total_seconds() < 30 * 60:
                raise ValueError("formal sessions violate the 30-minute separation")
            if not _service_identities_changed(
                previous_services, metadata["service_identity"]
            ):
                raise ValueError("formal model services were not restarted")
        previous_completed = completed_at
        previous_services = metadata["service_identity"]
        runner_commits.add(metadata["runner_commit"])
        protocol_commits.add(metadata["protocol_commit"])
        sessions[name] = metadata
        records.extend(session_records)
    if len(runner_commits) != 1 or None in runner_commits:
        raise ValueError("formal collection has mixed runner commits")
    if len(protocol_commits) != 1 or None in protocol_commits:
        raise ValueError("formal collection has mixed protocol commits")
    return records, {
        "attempts": attempts,
        "sessions": sessions,
        "runner_commit": next(iter(runner_commits)),
        "protocol_commit": next(iter(protocol_commits)),
    }


def _service_identities_changed(previous: object, current: object) -> bool:
    if not isinstance(previous, Mapping) or not isinstance(current, Mapping):
        return False
    for service in ("llama-server", "ollama"):
        before = previous.get(service)
        after = current.get(service)
        if not isinstance(before, Mapping) or not isinstance(after, Mapping):
            return False
        old_identities = before.get("process_start_identities")
        new_identities = after.get("process_start_identities")
        if not old_identities or not new_identities or old_identities == new_identities:
            return False
    return True


def _paired_arrays(
    records: Sequence[FormalRecord],
) -> tuple[
    dict[str, list[list[float]]],
    dict[str, list[list[float]]],
    dict[str, dict[str, list[float]]],
    dict[str, list[dict[str, object]]],
    dict[str, dict[str, int]],
    dict[str, dict[str, list[list[float]]]],
]:
    by_key: dict[tuple[str, int, str], dict[str, FormalRecord]] = {}
    conditions = {
        workload: {"formal_sync": [], "formal_async": []} for workload in WORKLOADS
    }
    diagnostics = {workload: [] for workload in WORKLOADS}
    lifecycle = {
        workload: {
            "stale_consumed_count": 0,
            "capacity_violation_count": 0,
            "unreaped_process_count": 0,
            "unjoined_thread_count": 0,
        }
        for workload in WORKLOADS
    }
    effects = {
        workload: {
            name: [[0.0] * PAIRS_PER_SESSION for _ in range(SESSION_COUNT)]
            for name in (
                "probe_percentage_change",
                "performance_difference",
                "performance_percentage_change",
            )
        }
        for workload in WORKLOADS
    }
    for record in records:
        key = (record.session, record.block, record.workload)
        by_key.setdefault(key, {})[record.condition] = record
        probe = record.run["probe"]
        performance = _performance_metric(record.run)
        conditions[record.workload][record.condition].append(
            float(probe["max_gap_ns"]) / 1_000_000
        )
        event_metrics = _event_diagnostics(record.events)
        diagnostics[record.workload].append(
            {
                "session": record.session,
                "block": record.block,
                "condition": record.condition,
                "order": record.order,
                "probe_lateness_ms": probe["max_lateness_ns"] / 1_000_000,
                "probe_deadline_miss_count": probe["deadline_miss_count"],
                "probe_skipped_releases": probe["skipped_releases"],
                "queue_wait_ms": event_metrics["queue_wait_ms"],
                "result_age_ms": event_metrics["result_age_ms"],
                "shutdown_join_ms": (
                    record.run["runtime"]["shutdown"]["join_latency_ns"] / 1_000_000
                    if record.condition == "formal_async"
                    else None
                ),
                "performance_metric": performance,
                "resources": _resource_diagnostics(record.resources),
                "vlm_stage_durations_ms": (
                    {
                        key: value / 1_000_000
                        for key, value in record.run["adapter"]
                        .get("stage_durations_ns", {})
                        .items()
                    }
                    if record.workload == "vlm"
                    else None
                ),
                "llm_completion_tokens_per_second": (
                    1000.0 / performance if record.workload == "llm" else None
                ),
            }
        )
        for key_name, count in _validate_lifecycle(record.run).items():
            lifecycle[record.workload][key_name] += count
    expected_keys = {
        (f"session-{session:02d}", block, workload)
        for session in range(1, SESSION_COUNT + 1)
        for block in range(1, PAIRS_PER_SESSION + 1)
        for workload in WORKLOADS
    }
    if set(by_key) != expected_keys or any(
        set(pair) != {"formal_sync", "formal_async"} for pair in by_key.values()
    ):
        raise ValueError("formal pair inventory is incomplete or duplicated")
    differences = {
        workload: [[0.0] * PAIRS_PER_SESSION for _ in range(SESSION_COUNT)]
        for workload in WORKLOADS
    }
    log_ratios = {
        workload: [[0.0] * PAIRS_PER_SESSION for _ in range(SESSION_COUNT)]
        for workload in WORKLOADS
    }
    for (session, block, workload), pair in by_key.items():
        sync = pair["formal_sync"]
        async_record = pair["formal_async"]
        sync_gap = sync.run["probe"]["max_gap_ns"] / 1_000_000
        async_gap = async_record.run["probe"]["max_gap_ns"] / 1_000_000
        sync_performance = _performance_metric(sync.run)
        async_performance = _performance_metric(async_record.run)
        if (
            sync_gap <= 0
            or async_gap <= 0
            or sync_performance <= 0
            or async_performance <= 0
        ):
            raise ValueError("paired probe and performance metrics must be positive")
        session_index = int(session.rsplit("-", 1)[-1]) - 1
        differences[workload][session_index][block - 1] = async_gap - sync_gap
        log_ratios[workload][session_index][block - 1] = math.log(
            async_performance / sync_performance
        )
        effects[workload]["probe_percentage_change"][session_index][block - 1] = (
            100 * async_gap / sync_gap - 100
        )
        effects[workload]["performance_difference"][session_index][block - 1] = (
            async_performance - sync_performance
        )
        effects[workload]["performance_percentage_change"][session_index][block - 1] = (
            100 * async_performance / sync_performance - 100
        )
    return differences, log_ratios, conditions, diagnostics, lifecycle, effects


def analyze_formal_collection(collection_root: Path | str) -> dict[str, object]:
    root = Path(collection_root).resolve()
    protocol_files = sorted(root.glob("session-*-attempt-*/protocol.json"))
    if not protocol_files:
        raise ValueError("formal collection has no protocol copy")
    manifest_directories = {
        path.parent for path in root.glob("session-*-attempt-*/manifest.json")
    }
    if {path.parent for path in protocol_files} != manifest_directories:
        raise ValueError("formal collection has an orphaned session attempt")
    protocol = _read_json(protocol_files[0])
    errors = formal_protocol_errors(protocol)
    if errors or protocol_sha256(protocol) != FROZEN_PROTOCOL_SHA256:
        raise ValueError("formal collection protocol does not match activated G6")
    for path in protocol_files[1:]:
        candidate = _read_json(path)
        if candidate != protocol:
            raise ValueError(f"{path}: formal protocol copies are inconsistent")
    records, metadata = _discover(root, protocol)
    if len(records) != 180:
        raise ValueError("formal collection measured count is not 180")
    (
        differences,
        log_ratios,
        conditions,
        diagnostics,
        lifecycle,
        effects,
    ) = _paired_arrays(records)
    bootstrap = paired_hierarchical_bootstrap(differences, log_ratios)
    workloads: dict[str, object] = {}
    all_criteria: list[bool] = []
    for workload in WORKLOADS:
        difference_values = [value for row in differences[workload] for value in row]
        ratio_values = [
            math.exp(value) for row in log_ratios[workload] for value in row
        ]
        probe_percentage_changes = [
            value
            for row in effects[workload]["probe_percentage_change"]
            for value in row
        ]
        performance_differences = [
            value
            for row in effects[workload]["performance_difference"]
            for value in row
        ]
        performance_percentage_changes = [
            value
            for row in effects[workload]["performance_percentage_change"]
            for value in row
        ]
        async_p95 = nearest_rank(conditions[workload]["formal_async"], 95)
        difference_point = statistics.fmean(difference_values)
        ratio_point = math.exp(
            statistics.fmean(value for row in log_ratios[workload] for value in row)
        )
        difference_ci = bootstrap[workload]["paired_mean_difference_ci95"]
        ratio_ci = bootstrap[workload]["paired_geometric_mean_ratio_ci95"]
        criteria = {
            "formal_async_p95_lte_300_ms": async_p95 <= ASYNC_MAX_GAP_P95_MS,
            "paired_mean_difference_ci95_high_lt_0_ms": difference_ci["high"] < 0,
            "paired_geometric_mean_ratio_ci95_high_lte_1_10": (
                ratio_ci["high"] <= NONINFERIORITY_RATIO
            ),
            "lifecycle": all(count == 0 for count in lifecycle[workload].values()),
        }
        all_criteria.extend(criteria.values())
        workloads[workload] = {
            "condition_statistics": {
                "probe_max_gap_ms": {
                    condition: descriptive(values)
                    for condition, values in conditions[workload].items()
                },
                "performance_metric": {
                    condition: descriptive(
                        item["performance_metric"]
                        for item in diagnostics[workload]
                        if item["condition"] == condition
                    )
                    for condition in ("formal_sync", "formal_async")
                },
            },
            "responsiveness": {
                "metric": "probe.maximum_observed_gap_ms",
                "paired_mean_async_minus_sync_ms": difference_point,
                "paired_mean_difference_ci95": difference_ci,
                "paired_difference_statistics": descriptive(difference_values),
                "paired_mean_percentage_change": statistics.fmean(
                    probe_percentage_changes
                ),
                "paired_percentage_change_statistics": descriptive(
                    probe_percentage_changes
                ),
                "async_p95_nearest_rank_ms": async_p95,
            },
            "workload_noninferiority": {
                "metric": {
                    "asr": "adapter_total_ms",
                    "llm": "request_ms_per_completion_token",
                    "vlm": "adapter_total_ms",
                }[workload],
                "paired_geometric_mean_async_sync_ratio": ratio_point,
                "paired_geometric_mean_ratio_ci95": ratio_ci,
                "paired_ratio_statistics": descriptive(ratio_values),
                "paired_mean_async_minus_sync": statistics.fmean(
                    performance_differences
                ),
                "paired_difference_statistics": descriptive(performance_differences),
                "paired_geometric_mean_change_pct": 100 * ratio_point - 100,
                "paired_percentage_change_statistics": descriptive(
                    performance_percentage_changes
                ),
            },
            "criteria": criteria,
            "pass": all(criteria.values()),
            "diagnostics": diagnostics[workload],
        }
    formal_pass = all(all_criteria)
    return {
        "formal_analysis_schema_version": FORMAL_ANALYSIS_SCHEMA_VERSION,
        "analysis_kind": "phase1_g6_confirmatory_comparison",
        "protocol": {
            "id": FORMAL_PROTOCOL_ID,
            "sha256": FROZEN_PROTOCOL_SHA256,
            "protocol_commit": metadata["protocol_commit"],
            "runner_commit": metadata["runner_commit"],
        },
        "source_path_recorded": False,
        "analysis_parameters": {
            "unit": "within_session_workload_block_pair",
            "p95_method": "nearest_rank",
            "bootstrap": {
                "method": "paired_hierarchical_percentile_bootstrap",
                "confidence_level": 0.95,
                "resamples": BOOTSTRAP_RESAMPLES,
                "seed": BOOTSTRAP_SEED,
                "resampling": (
                    "five sessions with replacement, then six paired blocks "
                    "with replacement within each selected session"
                ),
                "quantile_method": "linear",
                "shared_indices_across_workloads": True,
            },
            "outlier_exclusion": False,
            "imputation": False,
        },
        "dataset": {
            "planned_measured_runs": 180,
            "validated_measured_runs": len(records),
            "paired_units_per_workload": 30,
            "session_count": 5,
            "attempts": metadata["attempts"],
            "sessions": metadata["sessions"],
        },
        "lifecycle": {
            "all_run_gates_pass": True,
            "by_workload": lifecycle,
            "totals": {
                name: sum(lifecycle[workload][name] for workload in WORKLOADS)
                for name in (
                    "stale_consumed_count",
                    "capacity_violation_count",
                    "unreaped_process_count",
                    "unjoined_thread_count",
                )
            },
        },
        "workloads": workloads,
        "decision": {
            "rule": "intersection_union_all_workloads_and_criteria",
            "pass": formal_pass,
            "formal_claim_permitted": formal_pass,
        },
        "claim_boundary": {
            "fixed_input_single_device_only": True,
            "hard_real_time_claim_permitted": False,
            "heterogeneous_inference_claim_permitted": False,
            "resource_attribution_claim_permitted": False,
        },
    }


def render_markdown(analysis: Mapping[str, object]) -> str:
    dataset = analysis["dataset"]
    decision = analysis["decision"]
    workloads = analysis["workloads"]
    lines = [
        "# Phase 1 G6 Formal Comparison",
        "",
        f"Overall confirmatory decision: **{'PASS' if decision['pass'] else 'FAIL'}**.",
        "",
        "## Dataset",
        "",
        f"- Validated measured runs: {dataset['validated_measured_runs']}/"
        f"{dataset['planned_measured_runs']}.",
        f"- Paired units: {dataset['paired_units_per_workload']} per workload.",
        f"- Protocol: `{FORMAL_PROTOCOL_ID}`.",
        "- No post-hoc exclusions or imputation.",
        "",
        "## Confirmatory endpoints",
        "",
        "| Workload | Async p95 gap ms | Paired gap difference ms (95% CI) | "
        "Performance ratio (95% CI) | Decision |",
        "| --- | ---: | ---: | ---: | --- |",
    ]
    for workload in WORKLOADS:
        result = workloads[workload]
        responsiveness = result["responsiveness"]
        noninferiority = result["workload_noninferiority"]
        difference_ci = responsiveness["paired_mean_difference_ci95"]
        ratio_ci = noninferiority["paired_geometric_mean_ratio_ci95"]
        lines.append(
            f"| {workload.upper()} | "
            f"{responsiveness['async_p95_nearest_rank_ms']:.3f} | "
            f"{responsiveness['paired_mean_async_minus_sync_ms']:.3f} "
            f"[{difference_ci['low']:.3f}, {difference_ci['high']:.3f}] | "
            f"{noninferiority['paired_geometric_mean_async_sync_ratio']:.4f} "
            f"[{ratio_ci['low']:.4f}, {ratio_ci['high']:.4f}] | "
            f"{'PASS' if result['pass'] else 'FAIL'} |"
        )
    lines.extend(
        [
            "",
            "The decision is intersection-union: every responsiveness, "
            "noninferiority and lifecycle criterion must pass for all three workloads.",
            "Resource measurements and order diagnostics are descriptive only.",
            "",
            "## Scope",
            "",
            "Results apply only to the preregistered fixed inputs on one validated "
            "Jetson configuration. They do not establish hard-real-time behavior, "
            "resource attribution or a heterogeneous-inference comparison.",
            "",
        ]
    )
    return "\n".join(lines)


def _refuse_output_inside_collection(output: Path | None, root: Path) -> None:
    if output is None:
        return
    try:
        output.resolve().relative_to(root.resolve())
    except ValueError:
        return
    raise ValueError("analysis output must not be written inside the source collection")


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


def _write_text_atomic(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(value, encoding="utf-8", newline="\n")
    os.replace(temporary, path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("collection_root", type=Path)
    parser.add_argument("--json-output", type=Path)
    parser.add_argument("--markdown-output", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        root = args.collection_root.resolve()
        _refuse_output_inside_collection(args.json_output, root)
        _refuse_output_inside_collection(args.markdown_output, root)
        _require_distinct_outputs(args.json_output, args.markdown_output)
        analysis = analyze_formal_collection(root)
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    if args.json_output is None:
        print(json.dumps(analysis, ensure_ascii=False, indent=2, allow_nan=False))
    else:
        write_json_atomic(args.json_output, analysis)
    if args.markdown_output is not None:
        _write_text_atomic(
            args.markdown_output,
            render_markdown(analysis).rstrip("\n") + "\n",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
