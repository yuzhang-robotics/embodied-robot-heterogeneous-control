"""Validate and describe the closed Phase 1 G6 v2 formal attempt."""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Mapping, Sequence

from experiments.phase1.analyze_formal_runs import (
    FORMAL_SESSION_SCHEMA_VERSION,
    _expected_entries,
    _expected_ledger,
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
from experiments.phase1.formal_preflight import formal_preflight_errors
from experiments.phase1.formal_protocol import (
    FORMAL_V2_COLLECTION_STATUS,
    FORMAL_V2_PROTOCOL_ID,
    FORMAL_V2_PROTOCOL_PATH,
    FORMAL_V2_PROTOCOL_SHA256,
    protocol_sha256,
)
from experiments.phase1.jetson_telemetry import (
    load_resource_samples,
    validate_resource_samples,
)
from experiments.phase1.manifest import sha256_file, write_json_atomic


FORMAL_FAILURE_ANALYSIS_SCHEMA_VERSION = "0.1.0"
FORMAL_FAILURE_ANALYSIS_KIND = "phase1_g6_v2_system_under_test_failure"
EXPECTED_FAILED_GATES = frozenset({"translation_route_verified"})
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SESSION_ATTEMPT_RE = re.compile(
    r"^session-(?P<session>0[1-5])-attempt-(?P<attempt>[0-9]{2})$"
)
_LOG_TIME_RE = re.compile(
    r"^(?P<minutes>[0-9]+)\.(?P<seconds>[0-5][0-9])\."
    r"(?P<millis>[0-9]{3})\.(?P<micros>[0-9]{3})"
)
_LOG_TASK_RE = re.compile(r"\btask (?P<task>[0-9]+) \|")
_LOG_CANCEL_RE = re.compile(r"\bid_task = (?P<task>[0-9]+)\b")


def _normalized_sha256(value: str, name: str) -> str:
    normalized = value.lower()
    if _SHA256_RE.fullmatch(normalized) is None:
        raise ValueError(f"{name} must contain 64 hexadecimal digits")
    return normalized


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


def _llama_cancellation_record(path: Path | str) -> dict[str, object]:
    log_path = Path(path)
    if not log_path.is_file():
        raise ValueError("llama-server log does not exist")
    launches: dict[int, tuple[int, float]] = {}
    cancellations: list[tuple[int, int, float]] = []
    releases: dict[int, tuple[int, float]] = {}
    idle_lines: list[int] = []
    with log_path.open("r", encoding="utf-8", errors="strict") as stream:
        for index, line in enumerate(stream):
            elapsed = _elapsed_ms(line)
            if elapsed is None:
                continue
            if "launch_slot_" in line and "processing task" in line:
                match = _LOG_TASK_RE.search(line)
                if match is not None:
                    launches[int(match.group("task"))] = (index, elapsed)
            elif "cancel task" in line:
                match = _LOG_CANCEL_RE.search(line)
                if match is not None:
                    cancellations.append((int(match.group("task")), index, elapsed))
            elif "slot      release:" in line and "stop processing" in line:
                match = _LOG_TASK_RE.search(line)
                if match is not None:
                    releases[int(match.group("task"))] = (index, elapsed)
            elif "all slots are idle" in line:
                idle_lines.append(index)
    if len(cancellations) != 1:
        raise ValueError("llama-server log must contain one cancelled task")
    task_id, cancel_line, cancelled_ms = cancellations[0]
    launch = launches.get(task_id)
    release = releases.get(task_id)
    if launch is None or release is None:
        raise ValueError("cancelled llama-server task boundaries are incomplete")
    launch_line, launched_ms = launch
    release_line, released_ms = release
    if not launch_line < cancel_line < release_line:
        raise ValueError("cancelled llama-server task boundaries are reordered")
    idle_after_release = any(index > release_line for index in idle_lines)
    if not idle_after_release:
        raise ValueError(
            "llama-server did not return to an idle slot after cancellation"
        )
    return {
        "content_sha256": sha256_file(log_path),
        "size_bytes": log_path.stat().st_size,
        "cancelled_task_id": task_id,
        "launch_to_cancel_ms": round(cancelled_ms - launched_ms, 6),
        "cancel_to_release_ms": round(released_ms - cancelled_ms, 6),
        "slot_idle_after_release": True,
        "raw_log_recorded": False,
    }


def _entry_run_path(entry: Mapping[str, object]) -> str:
    role_dir = "warmups" if entry.get("role") == "warmup" else "measured"
    return (
        f"{role_dir}/{int(entry['ordinal']):03d}-"
        f"{entry['workload']}-{entry['condition']}"
    )


def _validate_ledger_prefix(
    session_dir: Path,
    ledger: Sequence[Mapping[str, object]],
    entries: Sequence[Mapping[str, object]],
    completed_entries: int,
) -> tuple[list[tuple[dict[str, object], str]], dict[str, object]]:
    expected = _expected_ledger(entries)
    if not ledger or len(ledger) >= len(expected):
        raise ValueError(f"{session_dir}: failed-attempt ledger is not a strict prefix")
    completed: list[tuple[dict[str, object], str]] = []
    for index, (item, (event, payload)) in enumerate(zip(ledger, expected)):
        if item.get("event") != event:
            raise ValueError(f"{session_dir}: ledger event {index} is reordered")
        if isinstance(payload, Mapping):
            entry = dict(payload)
            if item.get("plan") != entry:
                raise ValueError(f"{session_dir}: ledger plan {index} was modified")
            if event == "entry_completed":
                run_path = _entry_run_path(entry)
                if item.get("valid") is not True or item.get("run") != run_path:
                    raise ValueError(
                        f"{session_dir}: completed ledger entry is inconsistent"
                    )
                completed.append((entry, run_path))
        else:
            if item.get("label") != payload:
                raise ValueError(f"{session_dir}: ledger idle epoch was modified")
            if event == "idle_completed" and item.get("run") != f"idle/{payload}":
                raise ValueError(f"{session_dir}: ledger idle path was modified")
    if len(completed) != completed_entries:
        raise ValueError(f"{session_dir}: completed-entry count is inconsistent")
    final = ledger[-1]
    if final.get("event") != "entry_started" or not isinstance(
        final.get("plan"), Mapping
    ):
        raise ValueError(f"{session_dir}: ledger does not end at a failed entry")
    failed_entry = dict(final["plan"])
    expected_index = len(ledger) - 1
    expected_event, expected_payload = expected[expected_index]
    if expected_event != "entry_started" or failed_entry != expected_payload:
        raise ValueError(f"{session_dir}: failed entry does not match the protocol")
    return completed, failed_entry


def _counter(records: Sequence[Mapping[str, object]], name: str) -> dict[str, int]:
    return dict(sorted(Counter(str(record.get(name)) for record in records).items()))


def _finite_number(value: object, name: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
    ):
        raise ValueError(f"{name} is not finite")
    return float(value)


def _failed_run_record(
    run: Mapping[str, object],
    protocol: Mapping[str, object],
    llama: Mapping[str, object],
) -> dict[str, object]:
    gates = run.get("gates")
    gate_items = gates if isinstance(gates, list) else []
    failed_gates = sorted(
        str(gate.get("name"))
        for gate in gate_items
        if isinstance(gate, Mapping) and gate.get("passed") is False
    )
    if failed_gates != sorted(EXPECTED_FAILED_GATES):
        raise ValueError("failed run does not contain the expected VLM Gate failure")
    adapter = run.get("adapter")
    adapter_record = adapter if isinstance(adapter, Mapping) else {}
    statuses = adapter_record.get("stage_status")
    status_record = statuses if isinstance(statuses, Mapping) else {}
    durations = adapter_record.get("stage_durations_ns")
    duration_record = durations if isinstance(durations, Mapping) else {}
    expected_statuses = {
        "input_verify_before": "ok",
        "module_import": "ok",
        "moondream_inference": "ok",
        "qwen_rewrite": "error",
        "argos_fallback": "ok",
        "output_normalization": "ok",
        "model_unload": "ok",
        "input_verify_after": "ok",
    }
    if dict(status_record) != expected_statuses:
        raise ValueError("failed VLM stage outcomes are not the expected fallback path")
    if set(duration_record) != set(expected_statuses) or any(
        isinstance(value, bool) or not isinstance(value, int) or value < 0
        for value in duration_record.values()
    ):
        raise ValueError("failed VLM stage durations are invalid")
    if adapter_record.get("translation_route") != "argos":
        raise ValueError("failed VLM run did not use the fallback translation route")
    workloads = protocol.get("workloads")
    workload_records = workloads if isinstance(workloads, Mapping) else {}
    vlm = workload_records.get("vlm")
    vlm_record = vlm if isinstance(vlm, Mapping) else {}
    qwen = vlm_record.get("qwen")
    qwen_record = qwen if isinstance(qwen, Mapping) else {}
    request = qwen_record.get("request")
    request_record = request if isinstance(request, Mapping) else {}
    timeout_s = _finite_number(request_record.get("timeout_s"), "Qwen timeout")
    qwen_ms = int(duration_record["qwen_rewrite"]) / 1_000_000
    log_cancel_ms = _finite_number(
        llama.get("launch_to_cancel_ms"), "llama-server cancellation duration"
    )
    timeout_boundary_consistent = (
        timeout_s * 1_000 <= qwen_ms <= timeout_s * 1_000 + 1_000
        and timeout_s * 1_000 <= log_cancel_ms <= timeout_s * 1_000 + 1_000
    )
    if not timeout_boundary_consistent:
        raise ValueError(
            "Qwen and llama-server timing do not match the timeout boundary"
        )
    process = run.get("process")
    process_record = process if isinstance(process, Mapping) else {}
    return {
        "ordinal": run.get("plan", {}).get("ordinal"),
        "role": run.get("role"),
        "block": run.get("plan", {}).get("block"),
        "workload": run.get("workload"),
        "condition": run.get("condition"),
        "run_status": run.get("status"),
        "failed_gates": failed_gates,
        "adapter_execution_outcome": adapter_record.get("execution_outcome"),
        "translation_route": adapter_record.get("translation_route"),
        "stage_order": [
            "moondream_inference",
            "qwen_rewrite",
            "argos_fallback",
            "model_unload",
        ],
        "stage_status": dict(status_record),
        "stage_duration_ms": {
            name: int(duration_record[name]) / 1_000_000 for name in expected_statuses
        },
        "qwen_timeout_s": timeout_s,
        "timeout_boundary_consistent": True,
        "child_process": {
            "protocol_version": process_record.get("protocol_version"),
            "exit_code": process_record.get("exit_code"),
            "protocol_complete": process_record.get("protocol_complete"),
            "terminate_requested": process_record.get("terminate_requested"),
            "error_code": process_record.get("error_code"),
        },
        "residency": {
            "moondream_unload_requested": adapter_record.get("model_residency", {}).get(
                "unload_requested"
            ),
            "unload_confirmation_available": False,
            "qwen_preceded_unload_request": True,
        },
        "llama_server": dict(llama),
    }


def analyze_failed_formal_attempt(
    collection_root: Path | str,
    *,
    llama_log: Path | str,
    source_archive_sha256: str,
    llama_log_archive_sha256: str,
) -> dict[str, object]:
    """Reconstruct the sole system-under-test failure in the closed v2 attempt."""

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
    if FORMAL_V2_COLLECTION_STATUS != "closed_after_system_under_test_failure":
        raise ValueError("formal v2 collection status is not closed")
    _verify_artifacts(session_dir, manifest)

    protocol = _read_json(session_dir / "protocol.json")
    tracked_v2_protocol = _read_json(FORMAL_V2_PROTOCOL_PATH)
    if (
        protocol != tracked_v2_protocol
        or protocol_sha256(protocol) != FORMAL_V2_PROTOCOL_SHA256
        or manifest.get("protocol_id") != FORMAL_V2_PROTOCOL_ID
        or manifest.get("protocol_sha256") != FORMAL_V2_PROTOCOL_SHA256
    ):
        raise ValueError("failed attempt protocol identity does not match G6 v2")
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
        expected_protocol_id=FORMAL_V2_PROTOCOL_ID,
        expected_protocol_sha256=FORMAL_V2_PROTOCOL_SHA256,
    )
    if preflight_failures:
        raise ValueError(
            "failed attempt preflight is invalid: " + "; ".join(preflight_failures)
        )
    _validate_service_identity(preflight.get("service_identity"))
    preflight_protocol = preflight.get("protocol")
    preflight_protocol_record = (
        preflight_protocol if isinstance(preflight_protocol, Mapping) else {}
    )
    base = preflight.get("base")
    base_record = base if isinstance(base, Mapping) else {}
    environment = base_record.get("environment")
    environment_record = environment if isinstance(environment, Mapping) else {}
    git = environment_record.get("git")
    git_record = git if isinstance(git, Mapping) else {}
    expected_manifest_preflight = {
        "protocol_commit": preflight_protocol_record.get("protocol_commit"),
        "runner_commit": preflight_protocol_record.get("runner_commit"),
        "service_identity": preflight.get("service_identity"),
    }
    if (
        preflight_protocol_record.get("id") != FORMAL_V2_PROTOCOL_ID
        or preflight_protocol_record.get("sha256") != FORMAL_V2_PROTOCOL_SHA256
        or preflight_protocol_record.get("path_recorded") is not False
        or preflight_protocol_record.get("runner_commit") != git_record.get("commit")
        or manifest.get("preflight") != expected_manifest_preflight
    ):
        raise ValueError("failed attempt preflight identity is inconsistent")
    thermal = manifest.get("thermal")
    thermal_record = thermal if isinstance(thermal, Mapping) else {}
    if (
        not _thermal_gate_valid(thermal_record.get("session_start"))
        or not _thermal_gate_valid(thermal_record.get("measurement_start"))
        or thermal_record.get("stop_tj_c") != 85.0
        or thermal_record.get("stop_requested") is not False
    ):
        raise ValueError("failed attempt thermal gates are invalid")

    resources = load_resource_samples(session_dir / "resources.jsonl")
    resource_errors = validate_resource_samples(resources)
    if resource_errors:
        raise ValueError(
            "failed attempt resource trace is invalid: " + "; ".join(resource_errors)
        )
    sampler = manifest.get("resource_sampler_report")
    sampler_record = sampler if isinstance(sampler, Mapping) else {}
    if (
        sampler_record.get("successful") is not True
        or sampler_record.get("sample_count") != len(resources)
        or sampler_record.get("parse_error_count") != 0
        or sampler_record.get("first_sample_monotonic_ns")
        != resources[0]["sample_monotonic_ns"]
        or sampler_record.get("last_sample_monotonic_ns")
        != resources[-1]["sample_monotonic_ns"]
        or sampler_record.get("reader_joined") is not True
        or sampler_record.get("reader_error_code") is not None
    ):
        raise ValueError("failed attempt sampler report is inconsistent")

    completed_count = manifest.get("completed_entries")
    if isinstance(completed_count, bool) or not isinstance(completed_count, int):
        raise ValueError("failed attempt completed-entry count is invalid")
    entries = _expected_entries(protocol_session)
    ledger = _read_jsonl(session_dir / "ledger.jsonl")
    completed, failed_entry = _validate_ledger_prefix(
        session_dir,
        ledger,
        entries,
        completed_count,
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

    workloads = protocol.get("workloads")
    workload_contracts = workloads if isinstance(workloads, Mapping) else {}
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
    if failed_errors:
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
    llama = _llama_cancellation_record(llama_log)
    failure = _failed_run_record(failed_run, protocol, llama)

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
        "formal_failure_analysis_schema_version": (
            FORMAL_FAILURE_ANALYSIS_SCHEMA_VERSION
        ),
        "analysis_kind": FORMAL_FAILURE_ANALYSIS_KIND,
        "collection_id": root.name,
        "source": {
            "git_commit": git_record.get("commit"),
            "git_branch": git_record.get("branch"),
            "source_archive_sha256": _normalized_sha256(
                source_archive_sha256,
                "source_archive_sha256",
            ),
            "llama_log_archive_sha256": _normalized_sha256(
                llama_log_archive_sha256,
                "llama_log_archive_sha256",
            ),
            "session_artifacts": manifest.get("artifacts"),
            "source_path_recorded": False,
        },
        "protocol": {
            "id": FORMAL_V2_PROTOCOL_ID,
            "sha256": FORMAL_V2_PROTOCOL_SHA256,
            "collection_status": FORMAL_V2_COLLECTION_STATUS,
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
            "thermal_stop_tj_c": thermal_record.get("stop_tj_c"),
            "thermal_stop_requested": thermal_record.get("stop_requested"),
        },
        "interpretation": {
            "client_timeout_observed": True,
            "model_service_crash_observed": False,
            "child_process_lifecycle_failure_observed": False,
            "resource_sampler_failure_observed": False,
            "thermal_failure_observed": False,
            "residency_order_confound_present": True,
            "residency_order_causality_established": False,
        },
        "decision": {
            "confirmatory_analysis_permitted": False,
            "formal_claim_permitted": False,
            "v2_rerun_permitted": False,
            "v2_replacement_permitted": False,
            "next_evidence_role": "descriptive_vlm_residency_order_diagnostic",
        },
        "claim_boundary": {
            "timeout_cause_established": False,
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
    llama = failure["llama_server"]
    durations = failure["stage_duration_ms"]
    lines = [
        "# Phase 1 G6 v2 Failed Formal Attempt",
        "",
        "This report preserves and independently reconstructs the first G6 v2 "
        "formal attempt. The attempt stopped on a system-under-test Gate failure "
        "and is not confirmatory evidence.",
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
        f"| Verified manifest artifacts | " f"{integrity['manifest_artifact_count']} |",
        f"| Resource samples | {resources['session']['sample_count']} |",
        "",
        "All manifest-declared artifact sizes and hashes, the frozen protocol, "
        "preflight, ledger prefix, event traces and resource records were "
        "revalidated. Raw inputs, model text, private paths and raw service logs "
        "are not included in this report.",
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
        f"| Failed Gate | `{failure['failed_gates'][0]}` |",
        f"| Observed translation route | `{failure['translation_route']}` |",
        "",
        "## Failure diagnosis",
        "",
        f"Moondream completed in {durations['moondream_inference']:.3f} ms. "
        f"The Qwen rewrite then remained at its 30 s client boundary for "
        f"{durations['qwen_rewrite']:.3f} ms, failed, and the adapter completed "
        f"through the Argos fallback. The bound llama-server record shows the "
        f"corresponding task was cancelled {llama['launch_to_cancel_ms']:.3f} ms "
        "after launch and the slot returned to idle. The VLM child process exited "
        "normally and its IPC protocol completed.",
        "",
        "In the recorded implementation, the Moondream unload request followed "
        "the Qwen rewrite and fallback. The attempt therefore contains a model-"
        "residency-order confound. It does not by itself prove that residency "
        "caused the timeout. The isolated correction moves the unload request "
        "between Moondream inference and Qwen rewriting while retaining the 30 s "
        "Qwen timeout for the next descriptive diagnostic.",
        "",
        "## Safety and resource checks",
        "",
        f"The session maximum Tj was {resources['session_maximum_tj_c']:.3f} C; "
        f"the failed-run maximum was {resources['failed_run_maximum_tj_c']:.3f} C, "
        f"well below the {resources['thermal_stop_tj_c']:.0f} C stop threshold. "
        "No thermal stop, sampler failure, child-process lifecycle failure or "
        "model-service crash was observed.",
        "",
        "## Decision",
        "",
        "G6 v2 is closed after this system-under-test failure. The attempt will "
        "not be rerun, replaced or entered into confirmatory timing analysis, and "
        "no Phase 1 formal claim is permitted. After the residency-order "
        "correction is reviewed, a separate descriptive Jetson diagnostic will "
        "determine whether a future protocol version can retain the 30 s timeout.",
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
        analysis = analyze_failed_formal_attempt(
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
            f"Formal failure analysis failed: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
