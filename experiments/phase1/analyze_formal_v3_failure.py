"""Validate and describe the closed Phase 1 G6 v3 formal attempt."""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path
from typing import Mapping, Sequence

from experiments.phase1.analyze_formal_failure import (
    _counter,
    _entry_run_path,
    _normalized_sha256,
    _validate_ledger_prefix,
)
from experiments.phase1.analyze_formal_runs import (
    FORMAL_SESSION_SCHEMA_VERSION,
    _expected_entries,
    _parse_time,
    _read_json,
    _read_jsonl,
    _refuse_output_inside_collection,
    _require_distinct_outputs,
    _resource_diagnostics,
    _resource_slice,
    _run_gate_errors,
    _thermal_gate_valid,
    _validate_events,
    _validate_service_identity,
    _verify_artifacts,
    _write_text_atomic,
)
from experiments.phase1.formal_preflight import (
    formal_preflight_errors,
)
from experiments.phase1.formal_protocol import (
    FORMAL_V3_COLLECTION_STATUS,
    FORMAL_V3_PROTOCOL_ID,
    FORMAL_V3_PROTOCOL_PATH,
    FORMAL_V3_PROTOCOL_SHA256,
    protocol_sha256,
)
from experiments.phase1.jetson_telemetry import (
    load_resource_samples,
    validate_resource_samples,
)
from experiments.phase1.manifest import sha256_file, write_json_atomic


FORMAL_V3_FAILURE_ANALYSIS_SCHEMA_VERSION = "0.1.0"
FORMAL_V3_FAILURE_ANALYSIS_KIND = "phase1_g6_v3_system_under_test_failure"
EXPECTED_FAILED_GATES = frozenset(
    {"residency_contract_verified", "translation_route_verified"}
)
EXPECTED_FAILED_CONTRACT_ERRORS = ["VLM residency-order execution contract is invalid"]
_SESSION_ATTEMPT_RE = re.compile(
    r"^session-(?P<session>0[1-5])-attempt-(?P<attempt>[0-9]{2})$"
)
_LOG_TIME_RE = re.compile(
    r"^(?P<minutes>[0-9]+)\.(?P<seconds>[0-5][0-9])\."
    r"(?P<millis>[0-9]{3})\.(?P<micros>[0-9]{3})"
)
_LOG_TASK_RE = re.compile(r"\btask (?P<task>[0-9]+) \|")
_LOG_CANCEL_RE = re.compile(r"\bid_task = (?P<task>[0-9]+)\b")
_LOG_TIMING_RE = re.compile(
    r"=\s*(?P<milliseconds>[0-9.]+)\s*ms\s*/\s*(?P<units>[0-9]+)"
)
_EXPECTED_STAGE_ORDER = (
    "input_verify_before",
    "module_import",
    "moondream_inference",
    "model_unload",
    "qwen_rewrite",
    "argos_fallback",
    "output_normalization",
    "input_verify_after",
)


def _finite_number(value: object, name: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
    ):
        raise ValueError(f"{name} is not finite")
    return float(value)


def _millisecond_delta(left: float, right: float) -> float:
    """Return a stable, publication-safe millisecond difference."""

    return round(left - right, 6)


def _elapsed_ms(line: str) -> float | None:
    match = _LOG_TIME_RE.match(line)
    if match is None:
        return None
    return (
        int(match.group("minutes")) * 60_000
        + int(match.group("seconds")) * 1_000
        + int(match.group("millis"))
        + int(match.group("micros")) / 1_000
    )


def _timing_value(line: str) -> tuple[float, int] | None:
    match = _LOG_TIMING_RE.search(line)
    if match is None:
        return None
    return float(match.group("milliseconds")), int(match.group("units"))


def _llama_server_requests(path: Path | str) -> dict[str, object]:
    log_path = Path(path)
    if not log_path.is_file():
        raise ValueError("llama-server log does not exist")
    requests: list[dict[str, object]] = []
    by_task: dict[int, dict[str, object]] = {}
    active: dict[str, object] | None = None
    idle_lines: list[int] = []
    with log_path.open("r", encoding="utf-8", errors="strict") as stream:
        for index, line in enumerate(stream):
            elapsed = _elapsed_ms(line)
            if "launch_slot_" in line and "processing task" in line:
                match = _LOG_TASK_RE.search(line)
                if elapsed is None or match is None or active is not None:
                    raise ValueError("llama-server request launch is invalid")
                task = int(match.group("task"))
                if task in by_task:
                    raise ValueError("llama-server task identity is duplicated")
                active = {
                    "task_id": task,
                    "launch_line": index,
                    "launched_elapsed_ms": elapsed,
                    "cancelled": False,
                }
                requests.append(active)
                by_task[task] = active
            elif "cancel task" in line:
                match = _LOG_CANCEL_RE.search(line)
                if elapsed is None or match is None:
                    raise ValueError("llama-server cancellation is invalid")
                task = int(match.group("task"))
                request = by_task.get(task)
                if request is None or request.get("cancelled") is True:
                    raise ValueError("llama-server cancellation is unbound")
                request["cancelled"] = True
                request["cancel_line"] = index
                request["cancelled_elapsed_ms"] = elapsed
            elif "slot      release:" in line and "stop processing" in line:
                match = _LOG_TASK_RE.search(line)
                if elapsed is None or match is None:
                    raise ValueError("llama-server request release is invalid")
                task = int(match.group("task"))
                request = by_task.get(task)
                if request is None or request.get("release_line") is not None:
                    raise ValueError("llama-server request release is unbound")
                request["release_line"] = index
                request["released_elapsed_ms"] = elapsed
                if active is not request:
                    raise ValueError("llama-server request release is reordered")
                active = None
            elif "all slots are idle" in line:
                idle_lines.append(index)
            elif active is not None and "prompt eval time" in line:
                timing = _timing_value(line)
                if timing is not None:
                    active["prompt_eval_ms"], active["prompt_tokens"] = timing
            elif active is not None and "eval time" in line:
                timing = _timing_value(line)
                if timing is not None:
                    active["generation_ms"], active["generation_tokens"] = timing
            elif active is not None and "total time" in line:
                timing = _timing_value(line)
                if timing is not None:
                    active["server_total_ms"], active["total_tokens"] = timing
    if active is not None or not requests:
        raise ValueError("llama-server request sequence is incomplete")
    required = {
        "release_line",
        "released_elapsed_ms",
        "prompt_eval_ms",
        "prompt_tokens",
        "generation_ms",
        "generation_tokens",
        "server_total_ms",
        "total_tokens",
    }
    for request in requests:
        if not required.issubset(request):
            raise ValueError("llama-server request timing is incomplete")
        if not int(request["launch_line"]) < int(request["release_line"]):
            raise ValueError("llama-server request boundaries are reordered")
        elapsed_duration = float(request["released_elapsed_ms"]) - float(
            request["launched_elapsed_ms"]
        )
        if abs(elapsed_duration - float(request["server_total_ms"])) > 10.0:
            raise ValueError(
                "llama-server request timing does not match its boundaries"
            )
    last_release = int(requests[-1]["release_line"])
    if not any(index > last_release for index in idle_lines):
        raise ValueError("llama-server did not return to idle after the final request")
    return {
        "content_sha256": sha256_file(log_path),
        "size_bytes": log_path.stat().st_size,
        "request_count": len(requests),
        "released_request_count": len(requests),
        "cancellation_record_count": sum(
            request["cancelled"] is True for request in requests
        ),
        "idle_after_final_release": True,
        "raw_log_recorded": False,
        "requests": requests,
    }


def _public_request(request: Mapping[str, object]) -> dict[str, object]:
    return {
        "task_id": request.get("task_id"),
        "cancelled": request.get("cancelled"),
        "released": request.get("release_line") is not None,
        "prompt_tokens": request.get("prompt_tokens"),
        "prompt_eval_ms": request.get("prompt_eval_ms"),
        "generation_tokens": request.get("generation_tokens"),
        "generation_ms": request.get("generation_ms"),
        "total_tokens": request.get("total_tokens"),
        "server_total_ms": request.get("server_total_ms"),
    }


def _bind_llama_requests(
    completed: Sequence[tuple[Mapping[str, object], str]],
    failed_entry: Mapping[str, object],
    llama: Mapping[str, object],
) -> tuple[dict[str, object], dict[str, object]]:
    inference_entries = [
        dict(entry) for entry, _ in completed if entry.get("workload") in {"llm", "vlm"}
    ]
    if failed_entry.get("workload") in {"llm", "vlm"}:
        inference_entries.append(dict(failed_entry))
    inference_entries.sort(key=lambda entry: int(entry["ordinal"]))
    requests_value = llama.get("requests")
    requests = requests_value if isinstance(requests_value, list) else []
    if len(requests) != len(inference_entries):
        raise ValueError("llama-server request count does not match the ledger prefix")
    bindings = list(zip(inference_entries, requests))
    failed_request = next(
        (
            request
            for entry, request in bindings
            if entry.get("ordinal") == failed_entry.get("ordinal")
        ),
        None,
    )
    warmup_request = next(
        (
            request
            for entry, request in bindings
            if entry.get("workload") == "vlm" and entry.get("role") == "warmup"
        ),
        None,
    )
    if not isinstance(failed_request, Mapping) or not isinstance(
        warmup_request, Mapping
    ):
        raise ValueError("VLM llama-server request bindings are incomplete")
    if llama.get("cancellation_record_count") != 0:
        raise ValueError("G6 v3 llama-server log contains an unexpected cancellation")
    return _public_request(warmup_request), _public_request(failed_request)


def _failed_run_record(
    run: Mapping[str, object],
    protocol: Mapping[str, object],
    *,
    warmup_request: Mapping[str, object],
    failed_request: Mapping[str, object],
    llama: Mapping[str, object],
) -> dict[str, object]:
    gate_items = run.get("gates") if isinstance(run.get("gates"), list) else []
    failed_gates = sorted(
        str(gate.get("name"))
        for gate in gate_items
        if isinstance(gate, Mapping) and gate.get("passed") is False
    )
    if failed_gates != sorted(EXPECTED_FAILED_GATES):
        raise ValueError("failed run does not contain the expected G6 v3 Gates")
    adapter_value = run.get("adapter")
    adapter = adapter_value if isinstance(adapter_value, Mapping) else {}
    statuses_value = adapter.get("stage_status")
    statuses = statuses_value if isinstance(statuses_value, Mapping) else {}
    durations_value = adapter.get("stage_durations_ns")
    durations = durations_value if isinstance(durations_value, Mapping) else {}
    errors_value = adapter.get("stage_error_codes")
    stage_errors = errors_value if isinstance(errors_value, Mapping) else {}
    expected_statuses = {
        name: "error" if name == "qwen_rewrite" else "ok"
        for name in _EXPECTED_STAGE_ORDER
    }
    if dict(statuses) != expected_statuses:
        raise ValueError("failed VLM stage sequence is not the expected v3 path")
    if set(durations) != set(_EXPECTED_STAGE_ORDER) or any(
        isinstance(value, bool) or not isinstance(value, int) or value < 0
        for value in durations.values()
    ):
        raise ValueError("failed VLM stage durations are invalid")
    if dict(stage_errors) != {"qwen_rewrite": "timeouterror"}:
        raise ValueError("failed VLM stage error is not the expected timeout")
    if adapter.get("translation_route") != "argos":
        raise ValueError("failed VLM run did not use the fallback route")
    residency_value = adapter.get("model_residency")
    residency = residency_value if isinstance(residency_value, Mapping) else {}
    if (
        residency.get("unload_requested") is not True
        or residency.get("unload_confirmed") is not None
    ):
        raise ValueError("failed VLM residency claim is invalid")
    process_value = run.get("process")
    process = process_value if isinstance(process_value, Mapping) else {}
    if (
        process.get("protocol_version") != "0.2.0"
        or process.get("protocol_complete") is not True
        or process.get("exit_code") != 0
        or process.get("error_code") is not None
        or process.get("terminate_requested") is not False
        or process.get("joined_monotonic_ns") is None
    ):
        raise ValueError("failed VLM child process did not close normally")
    workloads_value = protocol.get("workloads")
    workloads = workloads_value if isinstance(workloads_value, Mapping) else {}
    vlm_value = workloads.get("vlm")
    vlm = vlm_value if isinstance(vlm_value, Mapping) else {}
    qwen_value = vlm.get("qwen")
    qwen = qwen_value if isinstance(qwen_value, Mapping) else {}
    request_value = qwen.get("request")
    request = request_value if isinstance(request_value, Mapping) else {}
    timeout_s = _finite_number(request.get("timeout_s"), "Qwen timeout")
    qwen_ms = int(durations["qwen_rewrite"]) / 1_000_000
    server_total_ms = _finite_number(
        failed_request.get("server_total_ms"), "failed server total"
    )
    warmup_total_ms = _finite_number(
        warmup_request.get("server_total_ms"), "warmup server total"
    )
    if not timeout_s * 1_000 <= qwen_ms <= timeout_s * 1_000 + 1_000:
        raise ValueError("Qwen client timing does not match the timeout boundary")
    if server_total_ms <= timeout_s * 1_000 or warmup_total_ms >= timeout_s * 1_000:
        raise ValueError("llama-server timings do not straddle the timeout boundary")
    if (
        failed_request.get("cancelled") is not False
        or failed_request.get("released") is not True
    ):
        raise ValueError("failed llama-server request lifecycle is invalid")
    prompt_delta = int(failed_request["prompt_tokens"]) - int(
        warmup_request["prompt_tokens"]
    )
    generation_delta = int(failed_request["generation_tokens"]) - int(
        warmup_request["generation_tokens"]
    )
    if prompt_delta <= 0 or generation_delta <= 0:
        raise ValueError("failed Qwen token counts do not exceed the warm-up")
    return {
        "ordinal": run.get("plan", {}).get("ordinal"),
        "role": run.get("role"),
        "block": run.get("plan", {}).get("block"),
        "workload": run.get("workload"),
        "condition": run.get("condition"),
        "run_status": run.get("status"),
        "failed_gates": failed_gates,
        "adapter_execution_outcome": adapter.get("execution_outcome"),
        "translation_route": adapter.get("translation_route"),
        "stage_order": list(_EXPECTED_STAGE_ORDER),
        "stage_status": dict(statuses),
        "stage_error_codes": dict(stage_errors),
        "stage_duration_ms": {
            name: int(durations[name]) / 1_000_000 for name in _EXPECTED_STAGE_ORDER
        },
        "qwen_timeout_s": timeout_s,
        "qwen_client_duration_ms": qwen_ms,
        "timeout_boundary_consistent": True,
        "child_process": {
            "protocol_version": process.get("protocol_version"),
            "exit_code": process.get("exit_code"),
            "protocol_complete": process.get("protocol_complete"),
            "terminate_requested": process.get("terminate_requested"),
            "error_code": process.get("error_code"),
        },
        "residency": {
            "moondream_unload_requested_before_qwen": True,
            "unload_confirmation_available": False,
        },
        "llama_server": {
            key: value for key, value in llama.items() if key != "requests"
        },
        "qwen_requests": {
            "warmup": dict(warmup_request),
            "failed_measured": dict(failed_request),
            "prompt_token_delta": prompt_delta,
            "generation_token_delta": generation_delta,
            "server_total_delta_ms": _millisecond_delta(
                server_total_ms, warmup_total_ms
            ),
            "failed_server_over_timeout_ms": _millisecond_delta(
                server_total_ms, timeout_s * 1_000
            ),
            "failed_server_minus_client_ms": _millisecond_delta(
                server_total_ms, qwen_ms
            ),
        },
    }


def analyze_v3_failed_formal_attempt(
    collection_root: Path | str,
    *,
    llama_log: Path | str,
    source_archive_sha256: str,
    llama_log_archive_sha256: str,
) -> dict[str, object]:
    """Reconstruct the sole system-under-test failure in the closed v3 attempt."""

    root = Path(collection_root).resolve()
    if not root.is_dir():
        raise ValueError("formal collection directory does not exist")
    manifests = sorted(root.glob("session-*-attempt-*/manifest.json"))
    if len(manifests) != 1:
        raise ValueError("failed formal collection must contain exactly one attempt")
    session_dir = manifests[0].parent
    manifest = _read_json(manifests[0])
    match = _SESSION_ATTEMPT_RE.fullmatch(session_dir.name)
    if (
        match is None
        or manifest.get("formal_session_schema_version")
        != FORMAL_SESSION_SCHEMA_VERSION
        or manifest.get("artifact_kind") != "phase1_g6_formal_session"
        or manifest.get("collection_id") != root.name
        or manifest.get("session_id") != session_dir.name
        or manifest.get("protocol_session") != f"session-{match.group('session')}"
        or manifest.get("attempt") != int(match.group("attempt"))
    ):
        raise ValueError("failed formal attempt identity does not match its path")
    if (
        manifest.get("status") != "aborted"
        or manifest.get("failure_class") != "system_under_test"
        or manifest.get("failure_code") != "formalsessionerror"
        or manifest.get("formal_evidence_eligible") is not False
        or manifest.get("development_injection") is not False
        or manifest.get("replacement_for") is not None
        or manifest.get("infrastructure_failure") is not None
    ):
        raise ValueError("attempt is not the unreplaced system-under-test failure")
    if FORMAL_V3_COLLECTION_STATUS != "closed_after_system_under_test_failure":
        raise ValueError("formal v3 collection status is not closed")
    _verify_artifacts(session_dir, manifest)

    protocol = _read_json(session_dir / "protocol.json")
    tracked_protocol = _read_json(FORMAL_V3_PROTOCOL_PATH)
    if (
        protocol != tracked_protocol
        or protocol_sha256(protocol) != FORMAL_V3_PROTOCOL_SHA256
        or manifest.get("protocol_id") != FORMAL_V3_PROTOCOL_ID
        or manifest.get("protocol_sha256") != FORMAL_V3_PROTOCOL_SHA256
    ):
        raise ValueError("failed attempt protocol identity does not match G6 v3")
    protocol_sessions = protocol.get("sessions")
    session_records = protocol_sessions if isinstance(protocol_sessions, list) else []
    protocol_session = next(
        (
            item
            for item in session_records
            if isinstance(item, Mapping)
            and item.get("session") == manifest.get("protocol_session")
        ),
        None,
    )
    if protocol_session is None:
        raise ValueError("failed attempt protocol session is missing")

    preflight = _read_json(session_dir / "preflight.json")
    preflight_failures = formal_preflight_errors(
        preflight,
        expected_protocol_id=FORMAL_V3_PROTOCOL_ID,
        expected_protocol_sha256=FORMAL_V3_PROTOCOL_SHA256,
    )
    if preflight_failures:
        raise ValueError(
            "failed attempt preflight is invalid: " + "; ".join(preflight_failures)
        )
    _validate_service_identity(preflight.get("service_identity"))
    preflight_protocol_value = preflight.get("protocol")
    preflight_protocol = (
        preflight_protocol_value
        if isinstance(preflight_protocol_value, Mapping)
        else {}
    )
    base_value = preflight.get("base")
    base = base_value if isinstance(base_value, Mapping) else {}
    environment_value = base.get("environment")
    environment = environment_value if isinstance(environment_value, Mapping) else {}
    git_value = environment.get("git")
    git = git_value if isinstance(git_value, Mapping) else {}
    expected_manifest_preflight = {
        "protocol_commit": preflight_protocol.get("protocol_commit"),
        "runner_commit": preflight_protocol.get("runner_commit"),
        "service_identity": preflight.get("service_identity"),
    }
    if (
        preflight_protocol.get("id") != FORMAL_V3_PROTOCOL_ID
        or preflight_protocol.get("sha256") != FORMAL_V3_PROTOCOL_SHA256
        or preflight_protocol.get("path_recorded") is not False
        or preflight_protocol.get("runner_commit") != git.get("commit")
        or manifest.get("preflight") != expected_manifest_preflight
    ):
        raise ValueError("failed attempt preflight identity is inconsistent")
    thermal_value = manifest.get("thermal")
    thermal = thermal_value if isinstance(thermal_value, Mapping) else {}
    if (
        not _thermal_gate_valid(thermal.get("session_start"))
        or not _thermal_gate_valid(thermal.get("measurement_start"))
        or thermal.get("stop_tj_c") != 85.0
        or thermal.get("stop_requested") is not False
    ):
        raise ValueError("failed attempt thermal gates are invalid")

    resources = load_resource_samples(session_dir / "resources.jsonl")
    resource_errors = validate_resource_samples(resources)
    if resource_errors:
        raise ValueError(
            "failed attempt resource trace is invalid: " + "; ".join(resource_errors)
        )
    sampler_value = manifest.get("resource_sampler_report")
    sampler = sampler_value if isinstance(sampler_value, Mapping) else {}
    if (
        sampler.get("successful") is not True
        or sampler.get("sample_count") != len(resources)
        or sampler.get("parse_error_count") != 0
        or sampler.get("first_sample_monotonic_ns")
        != resources[0]["sample_monotonic_ns"]
        or sampler.get("last_sample_monotonic_ns")
        != resources[-1]["sample_monotonic_ns"]
        or sampler.get("reader_joined") is not True
        or sampler.get("reader_error_code") is not None
    ):
        raise ValueError("failed attempt sampler report is inconsistent")

    completed_count = manifest.get("completed_entries")
    if isinstance(completed_count, bool) or not isinstance(completed_count, int):
        raise ValueError("failed attempt completed-entry count is invalid")
    entries = _expected_entries(protocol_session)
    ledger = _read_jsonl(session_dir / "ledger.jsonl")
    completed, failed_entry = _validate_ledger_prefix(
        session_dir, ledger, entries, completed_count
    )
    failed_path = _entry_run_path(failed_entry)
    expected_run_paths = {path for _, path in completed} | {failed_path}
    observed_run_paths = {
        path.parent.relative_to(session_dir).as_posix()
        for path in session_dir.glob("*/*/run.json")
        if path.parts[-3] in {"warmups", "measured"}
    }
    if observed_run_paths != expected_run_paths:
        raise ValueError(
            "failed attempt run inventory does not match the ledger prefix"
        )

    workloads_value = protocol.get("workloads")
    workload_contracts = workloads_value if isinstance(workloads_value, Mapping) else {}
    completed_runs: list[dict[str, object]] = []
    passed_gate_count = 0
    for entry, relative in completed:
        run_dir = session_dir / relative
        run = _read_json(run_dir / "run.json")
        contract = workload_contracts.get(str(entry.get("workload")))
        if not isinstance(contract, Mapping):
            raise ValueError(f"{run_dir}: workload contract is missing")
        run_errors = _run_gate_errors(run, entry, contract)
        if run_errors:
            raise ValueError(f"{run_dir}: {'; '.join(run_errors)}")
        _validate_events(_read_jsonl(run_dir / "events.jsonl"), run)
        _resource_slice(resources, run)
        gate_items = run.get("gates") if isinstance(run.get("gates"), list) else []
        passed_gate_count += sum(
            1
            for gate in gate_items
            if isinstance(gate, Mapping) and gate.get("passed") is True
        )
        completed_runs.append(run)

    failed_dir = session_dir / failed_path
    failed_run = _read_json(failed_dir / "run.json")
    failed_contract = workload_contracts.get(str(failed_entry.get("workload")))
    if not isinstance(failed_contract, Mapping):
        raise ValueError("failed run workload contract is missing")
    failed_errors = _run_gate_errors(
        failed_run,
        failed_entry,
        failed_contract,
        expected_failed_gates=EXPECTED_FAILED_GATES,
    )
    if failed_errors != EXPECTED_FAILED_CONTRACT_ERRORS:
        raise ValueError(f"{failed_dir}: {'; '.join(failed_errors)}")
    _validate_events(_read_jsonl(failed_dir / "events.jsonl"), failed_run)
    failed_resources = _resource_slice(resources, failed_run)
    failed_gate_items = (
        failed_run.get("gates") if isinstance(failed_run.get("gates"), list) else []
    )
    passed_gate_count += sum(
        1
        for gate in failed_gate_items
        if isinstance(gate, Mapping) and gate.get("passed") is True
    )

    llama = _llama_server_requests(llama_log)
    warmup_request, failed_request = _bind_llama_requests(
        completed, failed_entry, llama
    )
    failure = _failed_run_record(
        failed_run,
        protocol,
        warmup_request=warmup_request,
        failed_request=failed_request,
        llama=llama,
    )
    all_runs = completed_runs + [failed_run]
    created = _parse_time(manifest.get("created_at"))
    completed_at = _parse_time(manifest.get("completed_at"))
    if completed_at <= created:
        raise ValueError("failed attempt timestamps are invalid")
    maximum_tj = max(float(sample["temperatures_c"]["tj"]) for sample in resources)
    failed_maximum_tj = max(
        float(sample["temperatures_c"]["tj"]) for sample in failed_resources
    )
    return {
        "formal_v3_failure_analysis_schema_version": (
            FORMAL_V3_FAILURE_ANALYSIS_SCHEMA_VERSION
        ),
        "analysis_kind": FORMAL_V3_FAILURE_ANALYSIS_KIND,
        "collection_id": root.name,
        "source": {
            "git_commit": git.get("commit"),
            "git_branch": git.get("branch"),
            "source_archive_sha256": _normalized_sha256(
                source_archive_sha256, "source_archive_sha256"
            ),
            "llama_log_archive_sha256": _normalized_sha256(
                llama_log_archive_sha256, "llama_log_archive_sha256"
            ),
            "session_artifacts": manifest.get("artifacts"),
            "source_path_recorded": False,
        },
        "protocol": {
            "id": FORMAL_V3_PROTOCOL_ID,
            "sha256": FORMAL_V3_PROTOCOL_SHA256,
            "collection_status": FORMAL_V3_COLLECTION_STATUS,
        },
        "attempt": {
            "session_id": manifest.get("session_id"),
            "protocol_session": manifest.get("protocol_session"),
            "attempt": manifest.get("attempt"),
            "status": manifest.get("status"),
            "failure_class": manifest.get("failure_class"),
            "failure_code": manifest.get("failure_code"),
            "formal_evidence_eligible": False,
            "replacement_permitted": False,
            "duration_s": (completed_at - created).total_seconds(),
        },
        "integrity": {
            "manifest_artifact_count": len(manifest.get("artifacts", {})),
            "manifest_artifacts_verified": True,
            "protocol_verified": True,
            "preflight_verified": True,
            "ledger_prefix_verified": True,
            "resource_trace_verified": True,
            "resource_sampler_successful": True,
            "completed_run_records_verified": len(completed_runs),
            "failed_run_records_verified": 1,
            "run_record_count": len(all_runs),
            "passed_gate_count": passed_gate_count,
            "failed_gate_count": len(EXPECTED_FAILED_GATES),
        },
        "observed_runs": {
            "completed_by_role": _counter(completed_runs, "role"),
            "completed_by_workload": _counter(completed_runs, "workload"),
            "completed_by_condition": _counter(completed_runs, "condition"),
        },
        "failure": failure,
        "resources": {
            "session": _resource_diagnostics(resources),
            "failed_run": _resource_diagnostics(failed_resources),
            "session_maximum_tj_c": maximum_tj,
            "failed_run_maximum_tj_c": failed_maximum_tj,
            "thermal_stop_tj_c": thermal.get("stop_tj_c"),
            "thermal_stop_requested": thermal.get("stop_requested"),
        },
        "interpretation": {
            "client_timeout_observed": True,
            "server_completed_after_client_boundary": True,
            "server_cancellation_observed": False,
            "model_service_crash_observed": False,
            "child_process_lifecycle_failure_observed": False,
            "resource_sampler_failure_observed": False,
            "thermal_failure_observed": False,
            "corrected_residency_order_contract_bound": True,
            "unload_completion_confirmed": False,
            "prompt_token_count_increase_observed": True,
            "generation_token_count_increase_observed": True,
            "timeout_mechanism_established": True,
            "timeout_root_cause_established": False,
        },
        "decision": {
            "confirmatory_analysis_permitted": False,
            "formal_claim_permitted": False,
            "g6_success_criterion_met": False,
            "v3_rerun_permitted": False,
            "v3_replacement_permitted": False,
            "phase1_application_slice_permitted": False,
            "next_evidence_role": "phase1_g6_negative_closeout",
        },
        "claim_boundary": {
            "prompt_length_caused_timeout_claim_permitted": False,
            "residency_caused_timeout_claim_permitted": False,
            "performance_comparison_permitted": False,
            "hard_real_time_claim_permitted": False,
            "heterogeneous_inference_claim_permitted": False,
        },
    }


def render_markdown(analysis: Mapping[str, object]) -> str:
    source = analysis["source"]
    integrity = analysis["integrity"]
    attempt = analysis["attempt"]
    failure = analysis["failure"]
    resources = analysis["resources"]
    durations = failure["stage_duration_ms"]
    qwen = failure["qwen_requests"]
    warmup = qwen["warmup"]
    measured = qwen["failed_measured"]
    failed_gates = ", ".join(f"`{name}`" for name in failure["failed_gates"])
    lines = [
        "# Phase 1 G6 v3 Failed Formal Attempt",
        "",
        "This report preserves and independently reconstructs the first G6 v3 "
        "formal attempt. The attempt stopped on a system-under-test Gate failure "
        "and is not confirmatory performance evidence.",
        "",
        "## Source and integrity",
        "",
        "| Field | Value |",
        "| --- | --- |",
        f"| Collection | `{analysis['collection_id']}` |",
        f"| Session attempt | `{attempt['session_id']}` |",
        f"| Runner commit | `{source['git_commit']}` |",
        f"| Protocol | `{analysis['protocol']['id']}` |",
        f"| Protocol SHA-256 | `{analysis['protocol']['sha256']}` |",
        f"| Collection archive SHA-256 | `{source['source_archive_sha256']}` |",
        f"| llama-server log archive SHA-256 | "
        f"`{source['llama_log_archive_sha256']}` |",
        f"| Verified manifest artifacts | {integrity['manifest_artifact_count']} |",
        f"| Resource samples | {resources['session']['sample_count']} |",
        "",
        "All manifest-declared artifact sizes and hashes, the frozen protocol, "
        "preflight, ledger prefix, event traces, resource records and llama-server "
        "request sequence were revalidated. Raw inputs, model text, private paths "
        "and raw service logs are not included.",
        "",
        "## Attempt outcome",
        "",
        "| Field | Observation |",
        "| --- | --- |",
        f"| Manifest status | `{attempt['status']}` |",
        f"| Failure class | `{attempt['failure_class']}` |",
        f"| Completed entries | {integrity['completed_run_records_verified']} |",
        f"| Run records inspected | {integrity['run_record_count']} |",
        f"| Gates | {integrity['passed_gate_count']} passed, "
        f"{integrity['failed_gate_count']} failed |",
        f"| Failed entry | ordinal {failure['ordinal']}, "
        f"{str(failure['workload']).upper()} `{failure['condition']}` |",
        f"| Failed Gates | {failed_gates} |",
        f"| Translation route | `{failure['translation_route']}` |",
        "",
        "## Failure diagnosis",
        "",
        f"The corrected-order contract is bound to the runner commit: Moondream "
        f"completed in "
        f"{durations['moondream_inference']:.3f} ms, its unload request returned "
        f"in {durations['model_unload']:.3f} ms, and Qwen then remained at the "
        f"30 s client boundary for {durations['qwen_rewrite']:.3f} ms. The adapter "
        f"completed through the Argos fallback in "
        f"{durations['argos_fallback']:.3f} ms.",
        "",
        f"The bound llama-server request was not cancelled. It completed and "
        f"released its slot after {measured['server_total_ms']:.3f} ms, "
        f"{qwen['failed_server_over_timeout_ms']:.3f} ms beyond the configured "
        "timeout, and the server returned to idle. The VLM child process also "
        "exited normally with process protocol `0.2.0` complete.",
        "",
        "| Qwen request | Warm-up | Failed measured | Difference |",
        "| --- | ---: | ---: | ---: |",
        f"| Prompt tokens | {warmup['prompt_tokens']} | "
        f"{measured['prompt_tokens']} | {qwen['prompt_token_delta']:+d} |",
        f"| Generated tokens | {warmup['generation_tokens']} | "
        f"{measured['generation_tokens']} | {qwen['generation_token_delta']:+d} |",
        f"| Server total (ms) | {warmup['server_total_ms']:.3f} | "
        f"{measured['server_total_ms']:.3f} | "
        f"{qwen['server_total_delta_ms']:+.3f} |",
        "",
        "The longer prompt and generation counts are associated with the boundary "
        "crossing, but two observations do not establish why their counts or "
        "evaluation rates differed. In particular, unload completion is not "
        "observable from the current Ollama interface, so neither prompt length "
        "nor residency is assigned as a causal explanation.",
        "",
        "## Safety and resource checks",
        "",
        f"The session and failed-run maximum Tj were both "
        f"{resources['failed_run_maximum_tj_c']:.3f} C, below the "
        f"{resources['thermal_stop_tj_c']:.0f} C stop threshold. No thermal stop, "
        "sampler failure, child-process lifecycle failure or model-service crash "
        "was observed.",
        "",
        "## Decision",
        "",
        "G6 v3 is closed after this system-under-test failure. The attempt will "
        "not be rerun, replaced or entered into confirmatory timing analysis. "
        "The preregistered G6 success criterion is not met, so the Phase 1 "
        "application slice is not authorized. This is a negative Phase 1 result, "
        "not evidence for a synchronous/asynchronous performance comparison.",
        "",
    ]
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("collection", type=Path)
    parser.add_argument("--llama-log", type=Path, required=True)
    parser.add_argument("--source-archive-sha256", required=True)
    parser.add_argument("--llama-log-archive-sha256", required=True)
    parser.add_argument("--json-output", type=Path)
    parser.add_argument("--markdown-output", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        root = args.collection.resolve()
        _require_distinct_outputs(args.json_output, args.markdown_output)
        _refuse_output_inside_collection(args.json_output, root)
        _refuse_output_inside_collection(args.markdown_output, root)
        analysis = analyze_v3_failed_formal_attempt(
            root,
            llama_log=args.llama_log,
            source_archive_sha256=args.source_archive_sha256,
            llama_log_archive_sha256=args.llama_log_archive_sha256,
        )
        if args.json_output is not None:
            write_json_atomic(args.json_output, analysis)
        if args.markdown_output is not None:
            _write_text_atomic(args.markdown_output, render_markdown(analysis))
        if args.json_output is None and args.markdown_output is None:
            print(json.dumps(analysis, ensure_ascii=False, indent=2))
    except (OSError, UnicodeError, ValueError) as exc:
        print(
            f"Formal v3 failure analysis failed: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
