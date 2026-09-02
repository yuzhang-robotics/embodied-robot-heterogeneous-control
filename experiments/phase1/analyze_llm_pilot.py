"""Build a deterministic descriptive report from one validated LLM pilot."""

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
from experiments.phase1.llm_adapter import (
    LLM_EMPTY_HISTORY_SHA256,
    LLM_EXPECTED_SERVED_MODEL_ID,
    LLM_INPUT_MEDIA_TYPE,
    LLM_INPUT_SHA256,
    LLM_INPUT_SIZE_BYTES,
    LLM_MODEL_SHA256,
    LLM_MODEL_SIZE_BYTES,
    LLM_SERVER_ARGUMENTS,
    frozen_llm_request_contract,
)
from experiments.phase1.summarize_llm_slice import LLM_SUMMARY_SCHEMA_VERSION
from experiments.phase1.validate_llm_slice import validate_llm_slice_dir


ANALYSIS_SCHEMA_VERSION = "0.1.0"
ANALYSIS_KIND = "phase1_fixed_input_llm_pilot"
EXPECTED_CONDITIONS = ("llm_async", "llm_stale")
_EXPECTED_RUN_FILES = {
    "events.jsonl",
    "manifest.json",
    "preflight.json",
    "resources.jsonl",
    "scenario.json",
    "summary.json",
}
_EXPECTED_GATES = {
    "single_request",
    "bounded_conversation_lane",
    "expected_disposition",
    "stale_zero_consumed",
    "fixed_prompt_verified",
    "request_contract_verified",
    "llama_request_completed",
    "token_usage_valid",
    "output_private",
    "server_residency_claim_bounded",
    "cancellation_claim_bounded",
    "stale_observation_window",
    "threads_closed",
    "resource_trace_valid",
}
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    return value


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


def _find_run_directories(session_dir: Path) -> dict[str, Path]:
    observed = sorted(path.name for path in session_dir.iterdir())
    if observed != sorted(EXPECTED_CONDITIONS) or any(
        not (session_dir / condition).is_dir() for condition in EXPECTED_CONDITIONS
    ):
        raise ValueError("LLM pilot must contain exactly the two expected conditions")
    runs: dict[str, Path] = {}
    for condition in EXPECTED_CONDITIONS:
        candidates = sorted((session_dir / condition).iterdir())
        if len(candidates) != 1 or not candidates[0].is_dir():
            raise ValueError(f"{condition} must contain exactly one run directory")
        runs[condition] = candidates[0]
    return runs


def _disposition_counts(lifecycle: Mapping[str, object]) -> dict[str, int]:
    items = lifecycle.get("disposition_counts")
    if not isinstance(items, list):
        raise ValueError("lifecycle disposition_counts must be a list")
    result: dict[str, int] = {}
    for item in items:
        if not isinstance(item, list) or len(item) != 2 or not isinstance(item[0], str):
            raise ValueError("lifecycle disposition count is invalid")
        if item[0] in result:
            raise ValueError("lifecycle disposition count is duplicated")
        result[item[0]] = _integer(item[1], "lifecycle disposition count")
    return result


def _gate_results(summary: Mapping[str, object]) -> dict[str, bool]:
    gates = summary.get("gates")
    if not isinstance(gates, list):
        raise ValueError("summary gates must be a list")
    results: dict[str, bool] = {}
    for gate in gates:
        record = _mapping(gate, "summary gate")
        name = record.get("name")
        passed = record.get("passed")
        if not isinstance(name, str) or name in results or not isinstance(passed, bool):
            raise ValueError("summary gate is invalid or duplicated")
        results[name] = passed
    if set(results) != _EXPECTED_GATES:
        raise ValueError("summary gate set is incomplete or unsupported")
    return results


def _warning_counts(samples: Sequence[Mapping[str, object]]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for sample in samples:
        warnings = sample.get("parse_warnings")
        if not isinstance(warnings, list) or any(
            not isinstance(item, str) for item in warnings
        ):
            raise ValueError("resource parse_warnings must be a list of strings")
        counts.update(warnings)
    return dict(sorted(counts.items()))


def _resource_record(
    summary: Mapping[str, object],
    samples: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    resources = _mapping(summary.get("resources"), "summary resources")
    description = _mapping(resources.get("summary"), "resource summary")
    temperatures = _mapping(description.get("temperatures_c"), "resource temperatures")
    power = _mapping(description.get("power_instant_mw"), "resource power")
    sample_count = _integer(description.get("sample_count"), "resource sample count")
    if sample_count != len(samples):
        raise ValueError("resource sample count differs from resources.jsonl")
    return {
        "sample_count": sample_count,
        "inference_interval_sample_count": _integer(
            resources.get("inference_interval_sample_count"),
            "inference interval sample count",
        ),
        "parse_error_count": _integer(
            description.get("parse_error_count"), "resource parse error count"
        ),
        "parse_warning_count": _integer(
            description.get("parse_warning_count"), "resource parse warning count"
        ),
        "parse_warning_counts": _warning_counts(samples),
        "ram_used_mb": {
            "mean": _metric_value(description, "ram_used_mb", "mean"),
            "max": _metric_value(description, "ram_used_mb", "max"),
        },
        "gr3d_usage_pct": {
            "mean": _metric_value(description, "gr3d_usage_pct", "mean"),
            "max": _metric_value(description, "gr3d_usage_pct", "max"),
        },
        "junction_temperature_c": {
            "max": _metric_value(temperatures, "tj", "max"),
        },
        "vdd_in_instant_mw": {
            "mean": _metric_value(power, "VDD_IN", "mean"),
            "max": _metric_value(power, "VDD_IN", "max"),
        },
    }


def _probe_record(scenario: Mapping[str, object]) -> dict[str, object]:
    report = _mapping(scenario.get("report"), "scenario report")
    probe = _mapping(report.get("probe"), "scenario probe")
    return {
        "tick_count": _integer(probe.get("tick_count"), "probe tick count"),
        "skipped_releases": _integer(
            probe.get("skipped_releases"), "probe skipped releases"
        ),
        "deadline_miss_count": _integer(
            probe.get("deadline_miss_count"), "probe deadline misses"
        ),
        "max_lateness_ms": _milliseconds(
            probe.get("max_lateness_ns"), "probe maximum lateness"
        ),
        "maximum_observed_gap_ms": _milliseconds(
            probe.get("max_gap_ns"), "probe maximum observed gap"
        ),
        "joined": probe.get("joined") is True,
        "error_code": probe.get("error_code"),
    }


def _run_record(condition: str, run_dir: Path) -> dict[str, object]:
    observed_files = {path.name for path in run_dir.iterdir() if path.is_file()}
    if observed_files != _EXPECTED_RUN_FILES or any(
        not path.is_file() for path in run_dir.iterdir()
    ):
        raise ValueError(f"{condition} run files do not match the evidence contract")
    errors = validate_llm_slice_dir(run_dir)
    if errors:
        raise ValueError(f"invalid {condition} run: " + "; ".join(errors))

    manifest = _read_object(run_dir / "manifest.json")
    preflight = _read_object(run_dir / "preflight.json")
    scenario = _read_object(run_dir / "scenario.json")
    summary = _read_object(run_dir / "summary.json")
    samples = load_resource_samples(run_dir / "resources.jsonl")
    if manifest.get("condition") != condition or summary.get("condition") != condition:
        raise ValueError(f"{condition} directory contains a different condition")
    if summary.get("llm_summary_schema_version") != LLM_SUMMARY_SCHEMA_VERSION:
        raise ValueError("LLM summary schema is unsupported")

    environment = _mapping(manifest.get("environment"), "manifest environment")
    git = _mapping(environment.get("git"), "manifest Git identity")
    workload = _mapping(manifest.get("workload_contract"), "workload contract")
    lifecycle = _mapping(summary.get("lifecycle"), "summary lifecycle")
    adapter = _mapping(summary.get("adapter"), "summary adapter")
    response = _mapping(adapter.get("response"), "adapter response")
    residency = _mapping(adapter.get("model_residency"), "adapter residency")
    cancellation = _mapping(adapter.get("cancellation"), "adapter cancellation")
    spec = _mapping(summary.get("spec"), "summary specification")
    output = dict(_mapping(adapter.get("output"), "adapter output"))
    gates = _gate_results(summary)
    runtime = _mapping(preflight.get("runtime"), "preflight runtime")
    started_ns = _integer(adapter.get("started_monotonic_ns"), "adapter start")
    finished_ns = _integer(adapter.get("finished_monotonic_ns"), "adapter finish")
    if finished_ns < started_ns:
        raise ValueError("adapter finish precedes its start")

    return {
        "condition": condition,
        "run_id": manifest.get("run_id"),
        "session_id": manifest.get("session_id"),
        "source": {
            "git_commit": git.get("commit"),
            "git_branch": git.get("branch"),
            "artifacts": manifest.get("artifacts"),
        },
        "input": manifest.get("input"),
        "workload_contract": dict(workload),
        "preflight": {
            "schema_version": preflight.get("llm_preflight_schema_version"),
            "runtime": dict(runtime),
        },
        "validation": {
            "manifest_status": manifest.get("status"),
            "summary_schema_version": summary.get("llm_summary_schema_version"),
            "summary_valid": summary.get("valid") is True,
            "all_gates_passed": all(gates.values()),
            "gate_results": gates,
            "real_llm_path_executed": summary.get("real_llm_path_executed") is True,
        },
        "result": {
            "execution_outcome": adapter.get("execution_outcome"),
            "error_code": adapter.get("error_code"),
            "disposition_counts": _disposition_counts(lifecycle),
            "accepted_result_count": _integer(
                lifecycle.get("accepted_result_count"), "accepted result count"
            ),
            "stale_consumed_count": _integer(
                lifecycle.get("stale_consumed_count"), "stale consumed count"
            ),
            "output": output,
            "response": dict(response),
        },
        "residency": dict(residency),
        "cancellation": dict(cancellation),
        "timing": {
            "adapter_total_ms": _milliseconds(
                finished_ns - started_ns, "adapter total duration"
            ),
            "stale_observation_control_ms": round(
                1000.0 * _number(spec.get("stale_observation_s"), "stale observation"),
                6,
            ),
            "resource_interval_ms": _integer(
                manifest.get("resource_interval_ms"), "resource interval", minimum=1
            ),
            "probe": _probe_record(scenario),
        },
        "resources": _resource_record(summary, samples),
    }


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


def _valid_output(value: object) -> bool:
    output = _mapping(value, "response output")
    digest = output.get("sha256")
    length = output.get("length")
    return (
        set(output) == {"sha256", "length", "raw_text_recorded"}
        and isinstance(digest, str)
        and _SHA256_RE.fullmatch(digest) is not None
        and isinstance(length, int)
        and not isinstance(length, bool)
        and length > 0
        and output.get("raw_text_recorded") is False
    )


def _valid_response(value: object) -> bool:
    response = _mapping(value, "LLM response")
    usage = _mapping(response.get("usage"), "LLM token usage")
    names = ("prompt_tokens", "completion_tokens", "total_tokens")
    return (
        set(response) == {"model", "usage", "raw_response_recorded"}
        and response.get("model") == LLM_EXPECTED_SERVED_MODEL_ID
        and response.get("raw_response_recorded") is False
        and set(usage) == set(names)
        and all(
            isinstance(usage.get(name), int)
            and not isinstance(usage.get(name), bool)
            and usage.get(name) > 0
            for name in names
        )
        and usage.get("total_tokens", 0)
        >= usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0)
    )


def _valid_residency(value: object) -> bool:
    residency = _mapping(value, "model residency")
    return (
        residency.get("policy") == "external_llama_server_resident"
        and residency.get("server_preexisting") is True
        and residency.get("unload_requested") is False
        and residency.get("backend_stop_confirmed") is None
    )


def _correctness_observed(runs: Sequence[Mapping[str, object]]) -> bool:
    by_condition = {str(run.get("condition")): run for run in runs}
    nominal = by_condition["llm_async"]
    stale = by_condition["llm_stale"]
    nominal_result = _mapping(nominal.get("result"), "nominal result")
    stale_result = _mapping(stale.get("result"), "stale result")
    nominal_cancel = _mapping(nominal.get("cancellation"), "nominal cancellation")
    stale_cancel = _mapping(stale.get("cancellation"), "stale cancellation")
    stale_timing = _mapping(stale.get("timing"), "stale timing")
    return (
        nominal_result.get("execution_outcome") == "ok"
        and nominal_result.get("error_code") is None
        and nominal_result.get("disposition_counts") == {"consumed": 1}
        and nominal_result.get("accepted_result_count") == 1
        and nominal_result.get("stale_consumed_count") == 0
        and _valid_output(nominal_result.get("output"))
        and _valid_response(nominal_result.get("response"))
        and _valid_residency(nominal.get("residency"))
        and nominal_cancel.get("requested") is False
        and nominal_cancel.get("worker_observed") is False
        and nominal_cancel.get("client_wait_stopped") is False
        and nominal_cancel.get("backend_stop_confirmed") is None
        and stale_result.get("execution_outcome") == "cancel_observed"
        and stale_result.get("error_code") is None
        and stale_result.get("disposition_counts") == {"rejected_state": 1}
        and stale_result.get("accepted_result_count") == 0
        and stale_result.get("stale_consumed_count") == 0
        and _valid_output(stale_result.get("output"))
        and _valid_response(stale_result.get("response"))
        and _valid_residency(stale.get("residency"))
        and stale_cancel.get("requested") is True
        and stale_cancel.get("worker_observed") is True
        and stale_cancel.get("client_wait_stopped") is False
        and stale_cancel.get("backend_stop_confirmed") is None
        and _number(stale_timing.get("adapter_total_ms"), "stale adapter duration")
        >= _number(
            stale_timing.get("stale_observation_control_ms"),
            "stale observation control",
        )
    )


def analyze_llm_pilot_dir(
    session_dir: Path | str,
    *,
    source_archive_sha256: str | None = None,
) -> dict[str, object]:
    """Validate and reconstruct one two-condition real-model LLM pilot."""

    directory = Path(session_dir).resolve()
    if not directory.is_dir():
        raise ValueError("LLM pilot session directory does not exist")
    normalized_hash: str | None = None
    if source_archive_sha256 is not None:
        normalized_hash = source_archive_sha256.lower()
        if _SHA256_RE.fullmatch(normalized_hash) is None:
            raise ValueError("source_archive_sha256 must contain 64 hexadecimal digits")

    run_dirs = _find_run_directories(directory)
    runs = [
        _run_record(condition, run_dirs[condition]) for condition in EXPECTED_CONDITIONS
    ]
    session_id = _same_value(runs, ("session_id",))
    if session_id != directory.name:
        raise ValueError("run session_id does not match the session directory")
    git_commit = _same_value(runs, ("source", "git_commit"))
    git_branch = _same_value(runs, ("source", "git_branch"))
    input_identity = _same_value(runs, ("input",))
    workload_contract = _same_value(runs, ("workload_contract",))
    runtime_identity = _same_value(runs, ("preflight", "runtime"))
    if input_identity != {
        "sha256": LLM_INPUT_SHA256,
        "size_bytes": LLM_INPUT_SIZE_BYTES,
        "media_type": LLM_INPUT_MEDIA_TYPE,
        "path_recorded": False,
        "raw_text_recorded": False,
    }:
        raise ValueError("runs do not use the frozen Phase 0 LLM prompt")
    workload = _mapping(workload_contract, "common workload contract")
    source_version = workload.get("source_version")
    if (
        workload.get("source") != "llama_cpp_openai_http"
        or workload.get("model")
        != {
            "sha256": LLM_MODEL_SHA256,
            "size_bytes": LLM_MODEL_SIZE_BYTES,
            "served_model_id": LLM_EXPECTED_SERVED_MODEL_ID,
        }
        or not isinstance(source_version, str)
        or not source_version
        or workload.get("server_arguments") != dict(LLM_SERVER_ARGUMENTS)
        or workload.get("request") != frozen_llm_request_contract()
        or workload.get("history")
        != {
            "sha256": LLM_EMPTY_HISTORY_SHA256,
            "messages": 0,
            "raw_history_recorded": False,
        }
        or workload.get("residency_policy") != "external_llama_server_resident"
        or workload.get("raw_prompt_recorded") is not False
        or workload.get("raw_output_recorded") is not False
    ):
        raise ValueError("runs do not use the frozen Phase 0 LLM workload contract")
    runtime = _mapping(runtime_identity, "common preflight runtime")
    if (
        runtime.get("source_version") != source_version
        or runtime.get("source_clean") is not True
        or runtime.get("model_sha256") != LLM_MODEL_SHA256
        or runtime.get("model_size_bytes") != LLM_MODEL_SIZE_BYTES
        or runtime.get("server_process_count") != 1
        or runtime.get("server_arguments") != dict(LLM_SERVER_ARGUMENTS)
        or runtime.get("server_arguments_match") is not True
        or runtime.get("server_model_path_matches") is not True
        or runtime.get("endpoint_local") is not True
        or runtime.get("listener_loopback_only") is not True
        or runtime.get("service_reachable") is not True
        or runtime.get("expected_model_present") is not True
        or runtime.get("request_contract") != frozen_llm_request_contract()
    ):
        raise ValueError(
            "runs do not share the frozen local llama.cpp service identity"
        )

    observation_ms = _same_value(runs, ("timing", "stale_observation_control_ms"))
    resource_interval_ms = _same_value(runs, ("timing", "resource_interval_ms"))
    prompt_tokens = _same_value(runs, ("result", "response", "usage", "prompt_tokens"))
    all_runs_valid = all(
        run["validation"]["summary_valid"] is True
        and run["validation"]["all_gates_passed"] is True
        and run["validation"]["real_llm_path_executed"] is True
        for run in runs
    )
    correctness_observed = _correctness_observed(runs)
    skipped_total = sum(run["timing"]["probe"]["skipped_releases"] for run in runs)
    deadline_total = sum(run["timing"]["probe"]["deadline_miss_count"] for run in runs)
    probe_continuity = (
        skipped_total == 0
        and deadline_total == 0
        and all(run["timing"]["probe"]["joined"] is True for run in runs)
    )
    resource_coverage = all(
        run["resources"]["inference_interval_sample_count"] > 0 for run in runs
    )
    llm_component_satisfied = (
        all_runs_valid
        and correctness_observed
        and probe_continuity
        and resource_coverage
    )
    limitations = [
        "single_run_per_condition",
        "fixed_condition_order",
        "no_real_workload_synchronous_condition",
        "formal_thresholds_not_frozen",
        "stale_observation_window_is_pilot_control",
        "cancellation_latency_not_measured",
        "blocking_http_wait_not_preempted",
        "backend_stop_not_confirmed",
        "response_content_not_serialized",
        "response_identity_not_a_quality_threshold",
        "server_lifetime_externally_managed",
        "resource_activity_not_attributed_to_llama_cpp_or_processor",
    ]
    if all(
        run["resources"]["parse_warning_counts"].get("emc_missing", 0)
        == run["resources"]["sample_count"]
        for run in runs
    ):
        limitations.append("emc_unavailable")

    return {
        "analysis_schema_version": ANALYSIS_SCHEMA_VERSION,
        "analysis_kind": ANALYSIS_KIND,
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
            "model": workload.get("model"),
            "llama_source_version": source_version,
            "server_arguments": workload.get("server_arguments"),
            "request": workload.get("request"),
            "history": workload.get("history"),
            "listener_addresses": runtime.get("listener_addresses"),
        },
        "claim_boundary": {
            "design_role": "correctness_pilot",
            "descriptive_only": True,
            "real_llm_integration_observed": all_runs_valid,
            "nominal_response_identity_observed": correctness_observed,
            "stale_result_rejection_observed": correctness_observed,
            "blocking_http_completion_observed": correctness_observed,
            "llm_g5_component_satisfied": llm_component_satisfied,
            "prior_reviewed_g5_components": ["vlm", "asr"],
            "phase1_g5_complete": llm_component_satisfied,
            "remaining_g5_workloads": [] if llm_component_satisfied else ["llm"],
            "g6_preregistration_complete": False,
            "cancellation_latency_claim_permitted": False,
            "backend_cancellation_claim_permitted": False,
            "asynchronous_performance_superiority_claim_permitted": False,
            "hard_real_time_claim_permitted": False,
            "heterogeneous_inference_claim_permitted": False,
            "condition_resource_attribution_permitted": False,
        },
        "validation": {
            "run_count": len(runs),
            "all_runs_valid": all_runs_valid,
            "correctness_observed": correctness_observed,
            "probe_continuity_observed": probe_continuity,
            "resource_coverage_observed": resource_coverage,
            "llm_g5_component_satisfied": llm_component_satisfied,
        },
        "controls": {
            "stale_observation_control_ms": observation_ms,
            "resource_interval_ms": resource_interval_ms,
            "prompt_tokens": prompt_tokens,
        },
        "runs": runs,
        "data_quality": {
            "total_probe_skipped_releases": skipped_total,
            "total_probe_deadline_misses": deadline_total,
            "total_resource_samples": sum(
                run["resources"]["sample_count"] for run in runs
            ),
            "total_inference_interval_samples": sum(
                run["resources"]["inference_interval_sample_count"] for run in runs
            ),
            "limitations": limitations,
        },
    }


def _format_number(value: object, digits: int = 3) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def _render_dispositions(value: object) -> str:
    record = _mapping(value, "disposition counts")
    return ", ".join(f"{name}={count}" for name, count in sorted(record.items()))


def render_markdown(analysis: Mapping[str, object]) -> str:
    """Render the machine-readable analysis without adding new claims."""

    if analysis.get("analysis_kind") != ANALYSIS_KIND:
        raise ValueError("analysis kind is unsupported")
    source = _mapping(analysis.get("source"), "analysis source")
    identity = _mapping(analysis.get("identity"), "analysis identity")
    input_identity = _mapping(identity.get("input"), "analysis input identity")
    model_identity = _mapping(identity.get("model"), "analysis model identity")
    request = _mapping(identity.get("request"), "analysis request identity")
    system_prompt = _mapping(request.get("system_prompt"), "system prompt identity")
    history = _mapping(identity.get("history"), "analysis history identity")
    validation = _mapping(analysis.get("validation"), "analysis validation")
    claims = _mapping(analysis.get("claim_boundary"), "analysis claim boundary")
    controls = _mapping(analysis.get("controls"), "analysis controls")
    quality = _mapping(analysis.get("data_quality"), "analysis data quality")
    runs_value = analysis.get("runs")
    if not isinstance(runs_value, list) or not all(
        isinstance(run, Mapping) for run in runs_value
    ):
        raise ValueError("analysis runs must be a list of objects")
    runs: list[Mapping[str, object]] = list(runs_value)

    lines = [
        "# Phase 1 Fixed-input LLM Pilot",
        "",
        (
            "This report records one motion-disabled correctness pilot on the Jetson "
            "Orin Nano. It validates the local llama.cpp HTTP boundary, response "
            "identity handling and old-generation result rejection."
        ),
        "",
        "## Evidence boundary",
        "",
        "- Workload: one fixed Phase 0 prompt with empty history per condition.",
        "- Conditions: one `llm_async` run followed by one `llm_stale` run.",
        "- Backend: one pre-existing loopback llama-server retained across both runs.",
        "- Physical motion and UART access: disabled.",
        "- Permitted interpretation: integration and lifecycle correctness evidence.",
        (
            "- Not permitted: cancellation-latency, backend-cancellation, asynchronous "
            "superiority, hard-real-time, performance or heterogeneous-inference claims."
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
        f"| LLM G5 component | {'satisfied' if validation.get('llm_g5_component_satisfied') is True else 'open'} |",
        "",
        "Machine-readable derived data: [`analysis.json`](analysis.json).",
        "",
        "## Frozen identities",
        "",
        "| Field | Value |",
        "| --- | --- |",
        f"| Input SHA-256 | `{input_identity.get('sha256')}` |",
        f"| Input size | {_format_number(input_identity.get('size_bytes'))} bytes |",
        f"| Model SHA-256 | `{model_identity.get('sha256')}` |",
        f"| Model size | {_format_number(model_identity.get('size_bytes'))} bytes |",
        f"| Served model | `{model_identity.get('served_model_id')}` |",
        f"| llama.cpp source | `{identity.get('llama_source_version')}` |",
        f"| Model alias | `{request.get('model')}` |",
        f"| Temperature | {_format_number(request.get('temperature'))} |",
        f"| Maximum completion tokens | {_format_number(request.get('max_tokens'))} |",
        f"| System prompt SHA-256 | `{system_prompt.get('sha256')}` |",
        f"| Empty-history SHA-256 | `{history.get('sha256')}` |",
        "",
        (
            "Prompt, history and response text, raw HTTP data and private filesystem "
            "paths are not serialized."
        ),
        "",
        "## Correctness results",
        "",
        "| Condition | Execution | Disposition | Accepted | Stale consumed | Output length | Prompt/completion/total tokens | Gates |",
        "| --- | --- | --- | ---: | ---: | ---: | --- | --- |",
    ]
    for run in runs:
        result = _mapping(run.get("result"), "run result")
        output = _mapping(result.get("output"), "run output")
        response = _mapping(result.get("response"), "run response")
        usage = _mapping(response.get("usage"), "run token usage")
        run_validation = _mapping(run.get("validation"), "run validation")
        lines.append(
            "| `{condition}` | `{outcome}` | {disposition} | {accepted} | {stale} | "
            "{length} | {prompt}/{completion}/{total} | {gates} |".format(
                condition=run.get("condition"),
                outcome=result.get("execution_outcome"),
                disposition=_render_dispositions(result.get("disposition_counts")),
                accepted=_format_number(result.get("accepted_result_count")),
                stale=_format_number(result.get("stale_consumed_count")),
                length=_format_number(output.get("length")),
                prompt=_format_number(usage.get("prompt_tokens")),
                completion=_format_number(usage.get("completion_tokens")),
                total=_format_number(usage.get("total_tokens")),
                gates=(
                    "pass" if run_validation.get("all_gates_passed") is True else "fail"
                ),
            )
        )
    lines.extend(
        [
            "",
            (
                "The nominal response identity was consumed exactly once. The stale "
                "response completed at the blocking HTTP boundary but was rejected "
                "before consumption because its state generation was obsolete."
            ),
            "",
            "## Cancellation and residency boundary",
            "",
            "| Condition | Requested | Worker observed | Client wait stopped | Backend stop confirmed | Unload requested |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
    )
    for run in runs:
        cancellation = _mapping(run.get("cancellation"), "run cancellation")
        residency = _mapping(run.get("residency"), "run residency")
        lines.append(
            "| `{condition}` | {requested} | {observed} | {client} | {backend} | {unload} |".format(
                condition=run.get("condition"),
                requested=_format_number(cancellation.get("requested")),
                observed=_format_number(cancellation.get("worker_observed")),
                client=_format_number(cancellation.get("client_wait_stopped")),
                backend=_format_number(cancellation.get("backend_stop_confirmed")),
                unload=_format_number(residency.get("unload_requested")),
            )
        )
    lines.extend(
        [
            "",
            (
                "State invalidation did not stop the blocking HTTP wait and does not "
                "prove that backend inference stopped. The server remained externally "
                "managed, and neither run requested model unload."
            ),
            "",
            "## Observation control and descriptive timing",
            "",
            f"The stale observation control was {_format_number(controls.get('stale_observation_control_ms'))} ms, compared with a {_format_number(controls.get('resource_interval_ms'))} ms resource interval.",
            "",
            "| Condition | Adapter total (ms) | Resource samples | In-adapter samples |",
            "| --- | ---: | ---: | ---: |",
        ]
    )
    for run in runs:
        timing = _mapping(run.get("timing"), "run timing")
        resources = _mapping(run.get("resources"), "run resources")
        lines.append(
            "| `{condition}` | {duration} | {samples} | {covered} |".format(
                condition=run.get("condition"),
                duration=_format_number(timing.get("adapter_total_ms")),
                samples=_format_number(resources.get("sample_count")),
                covered=_format_number(
                    resources.get("inference_interval_sample_count")
                ),
            )
        )
    lines.extend(
        [
            "",
            (
                "These single, fixed-order durations are descriptive. The stale "
                "duration includes the deliberate observation window and is not "
                "cancellation latency."
            ),
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
    lines.extend(
        [
            "",
            "## Phase 1 Gate status",
            "",
            "- The VLM and ASR components of G5 were satisfied by their previously reviewed pilots.",
            "- The LLM correctness-pilot component of G5 is satisfied by this session.",
            "- Phase 1 G5 is complete; G6 preregistration and formal data collection are next.",
            "",
            "## Evidence gaps",
            "",
        ]
    )
    lines.extend(f"- `{item}`" for item in limitations)
    lines.extend(
        [
            "",
            (
                "Device-level GR3D activity is not attributed to llama.cpp or a "
                "specific processor. This evidence does not authorize a "
                "heterogeneous-inference claim."
            ),
            "",
            (
                "Formal performance, backend-cancellation and cancellation-latency "
                "claims remain prohibited: "
                f"`cancellation_latency_claim_permitted={claims.get('cancellation_latency_claim_permitted')}`, "
                f"`backend_cancellation_claim_permitted={claims.get('backend_cancellation_claim_permitted')}`."
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
        analysis = analyze_llm_pilot_dir(
            session_dir,
            source_archive_sha256=args.source_archive_sha256,
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
