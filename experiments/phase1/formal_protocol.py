"""Build and validate the Phase 1 G6 formal preregistration."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Mapping, Sequence

from experiments.phase1.asr_adapter import (
    ASR_INPUT_SHA256,
    ASR_INPUT_SIZE_BYTES,
    ASR_MODEL_SHA256,
    ASR_MODEL_SIZE_BYTES,
    ASR_WHISPER_ARGUMENTS,
    ASR_WHISPER_SOURCE_VERSION,
)
from experiments.phase1.llm_adapter import (
    LLM_EMPTY_HISTORY_SHA256,
    LLM_EXPECTED_SERVED_MODEL_ID,
    LLM_INPUT_SHA256,
    LLM_INPUT_SIZE_BYTES,
    LLM_MODEL_SHA256,
    LLM_MODEL_SIZE_BYTES,
    LLM_SERVER_ARGUMENTS,
    frozen_llm_request_contract,
)
from experiments.phase1.vlm_adapter import C100_INPUT_SHA256, C100_INPUT_SIZE_BYTES


FORMAL_PROTOCOL_SCHEMA_VERSION = "0.2.0"
FORMAL_PROTOCOL_ID = "phase1-g6-fixed-input-sync-async-v2"
FORMAL_COLLECTION_STATUS = "closed_after_system_under_test_failure"
DEFAULT_PROTOCOL_PATH = (
    Path(__file__).resolve().parent / "formal" / "phase1-g6-v2-preregistration.json"
)
SUPERSEDED_PROTOCOL_ID = "phase1-g6-fixed-input-sync-async-v1"
SUPERSEDED_PROTOCOL_SHA256 = (
    "022df6af4bb3236a28b2e47f0edb9afbc6078131441a1c1f9e8730920c660761"
)
SUPERSEDED_PROTOCOL_PATH = DEFAULT_PROTOCOL_PATH.with_name(
    "phase1-g6-preregistration.json"
)
WORKLOADS = ("asr", "llm", "vlm")
CONDITIONS = ("formal_sync", "formal_async")
SESSION_COUNT = 5
PAIRS_PER_SESSION = 6
PROBE_PERIOD_MS = 100
PROBE_DEADLINE_MS = 100
RESOURCE_INTERVAL_MS = 200
IDLE_EPOCH_S = 30
BOOTSTRAP_RESAMPLES = 100_000
BOOTSTRAP_SEED = 20_260_902
ASYNC_MAX_GAP_P95_MS = 300.0
NONINFERIORITY_RATIO = 1.10
LLAMA_SOURCE_VERSION = "b9246-2-g585080d31"
VLM_OLLAMA_VERSION = "0.24.0"
VLM_OLLAMA_BINARY_SHA256 = (
    "6273a99e321b5e69741aa024cc22e0ce2803aa2bdf20185ea19627b4d891c87a"
)
VLM_MOONDREAM_MODEL = "moondream"
VLM_MOONDREAM_DIGEST = (
    "55fc3abd386771e5b5d1bbcc732f3c3f4df6e9f9f08f1131f9cc27ba2d1eec5b"
)
VLM_MOONDREAM_PROMPT_IDENTITIES = (
    {
        "sha256": "d760336d0fd143932f194573dd63f6b531e0ffbc2c4f1595e015392a3471c318",
        "length": 28,
        "raw_text_recorded": False,
    },
    {
        "sha256": "ec277729ae2e219f8efcbd2ea558d183c79beefa639be80c118f2c77b11ef7f6",
        "length": 22,
        "raw_text_recorded": False,
    },
    {
        "sha256": "ad809847f2b61ef9a8be4d90776afd30b3329a9cb16eff06e950818d86c70ccd",
        "length": 66,
        "raw_text_recorded": False,
    },
)
VLM_QWEN_SYSTEM_PROMPT_IDENTITY = {
    "sha256": "d9ee8e3fa369672221d02d193a3a30abac0745d598f630023ffc90994e86537d",
    "length": 148,
    "raw_text_recorded": False,
}
VLM_QWEN_USER_PREFIX_IDENTITY = {
    "sha256": "60278ce5d5b81b3041241a2a2b42a7744b4953552f51783895737683195bd40a",
    "length": 30,
    "raw_text_recorded": False,
}
_WORKLOAD_ORDERS = (
    ("asr", "llm", "vlm"),
    ("llm", "vlm", "asr"),
    ("vlm", "asr", "llm"),
    ("vlm", "llm", "asr"),
    ("llm", "asr", "vlm"),
    ("asr", "vlm", "llm"),
)
_CONDITION_ORDERS = (CONDITIONS, tuple(reversed(CONDITIONS)))
_PAIR_ORDER_MATRIX = (
    {"asr": "100110", "llm": "100101", "vlm": "011001"},
    {"asr": "101001", "llm": "011010", "vlm": "010110"},
    {"asr": "010101", "llm": "101001", "vlm": "101010"},
    {"asr": "011010", "llm": "010110", "vlm": "100101"},
    {"asr": "001101", "llm": "100011", "vlm": "011010"},
)


def _measured_schedule(session_index: int) -> list[dict[str, object]]:
    if not 1 <= session_index <= SESSION_COUNT:
        raise ValueError("session index is outside the preregistered range")
    runs: list[dict[str, object]] = []
    sequence = 0
    for block in range(1, PAIRS_PER_SESSION + 1):
        workload_order = _WORKLOAD_ORDERS[block - 1]
        for position, workload in enumerate(workload_order, start=1):
            order_index = int(
                _PAIR_ORDER_MATRIX[session_index - 1][workload][block - 1]
            )
            condition_order = _CONDITION_ORDERS[order_index]
            pair_id = f"s{session_index:02d}-b{block:02d}-{workload}"
            for pair_position, condition in enumerate(condition_order, start=1):
                sequence += 1
                runs.append(
                    {
                        "sequence": sequence,
                        "block": block,
                        "workload_position": position,
                        "pair_id": pair_id,
                        "pair_position": pair_position,
                        "workload": workload,
                        "condition": condition,
                        "role": "measured",
                    }
                )
    return runs


def _session_plan(session_index: int) -> dict[str, object]:
    warmups = [
        {
            "workload": workload,
            "condition": "formal_sync",
            "role": "warmup",
            "repetition": repetition,
        }
        for workload, count in (("asr", 3), ("llm", 1), ("vlm", 1))
        for repetition in range(1, count + 1)
    ]
    return {
        "session": f"session-{session_index:02d}",
        "warmups": warmups,
        "pre_measurement_idle": {
            "condition": "formal_idle",
            "role": "idle_reference",
            "duration_s": IDLE_EPOCH_S,
        },
        "measured_runs": _measured_schedule(session_index),
        "post_measurement_idle": {
            "condition": "formal_idle",
            "role": "idle_reference",
            "duration_s": IDLE_EPOCH_S,
        },
    }


def _workload_contracts() -> dict[str, object]:
    return {
        "asr": {
            "input_sha256": ASR_INPUT_SHA256,
            "input_size_bytes": ASR_INPUT_SIZE_BYTES,
            "model_sha256": ASR_MODEL_SHA256,
            "model_size_bytes": ASR_MODEL_SIZE_BYTES,
            "source_version": ASR_WHISPER_SOURCE_VERSION,
            "arguments": list(ASR_WHISPER_ARGUMENTS),
            "execution_boundary": "supervised_whisper_subprocess",
            "residency_policy": "loads_model_per_invocation",
        },
        "llm": {
            "input_sha256": LLM_INPUT_SHA256,
            "input_size_bytes": LLM_INPUT_SIZE_BYTES,
            "model_sha256": LLM_MODEL_SHA256,
            "model_size_bytes": LLM_MODEL_SIZE_BYTES,
            "served_model_id": LLM_EXPECTED_SERVED_MODEL_ID,
            "source_version": LLAMA_SOURCE_VERSION,
            "server_arguments": dict(LLM_SERVER_ARGUMENTS),
            "request": frozen_llm_request_contract(),
            "history_sha256": LLM_EMPTY_HISTORY_SHA256,
            "execution_boundary": "blocking_loopback_http_request",
            "residency_policy": "external_llama_server_resident",
        },
        "vlm": {
            "input_sha256": C100_INPUT_SHA256,
            "input_size_bytes": C100_INPUT_SIZE_BYTES,
            "moondream": {
                "service": {
                    "name": "ollama",
                    "version": VLM_OLLAMA_VERSION,
                    "binary_sha256": VLM_OLLAMA_BINARY_SHA256,
                },
                "model": VLM_MOONDREAM_MODEL,
                "digest": VLM_MOONDREAM_DIGEST,
                "request": {
                    "prompt_identities": [
                        dict(identity) for identity in VLM_MOONDREAM_PROMPT_IDENTITIES
                    ],
                    "temperature": 0.1,
                    "num_predict": 100,
                    "stream": False,
                    "timeout_s": 180,
                },
            },
            "qwen": {
                "model_sha256": LLM_MODEL_SHA256,
                "model_size_bytes": LLM_MODEL_SIZE_BYTES,
                "served_model_id": LLM_EXPECTED_SERVED_MODEL_ID,
                "source_version": LLAMA_SOURCE_VERSION,
                "server_arguments": dict(LLM_SERVER_ARGUMENTS),
                "request": {
                    "model": "qwen",
                    "system_prompt": dict(VLM_QWEN_SYSTEM_PROMPT_IDENTITY),
                    "user_prefix": dict(VLM_QWEN_USER_PREFIX_IDENTITY),
                    "temperature": 0.2,
                    "max_tokens": 96,
                    "stream": False,
                    "timeout_s": 30,
                },
            },
            "translation_route": "qwen",
            "execution_boundary": "spawned_process_for_both_conditions",
            "residency_policy": "moondream_unload_requested_per_invocation",
        },
    }


def build_formal_protocol() -> dict[str, object]:
    """Return the complete protocol that becomes active when merged to main."""

    sessions = [_session_plan(index) for index in range(1, SESSION_COUNT + 1)]
    protocol = {
        "formal_protocol_schema_version": FORMAL_PROTOCOL_SCHEMA_VERSION,
        "protocol_id": FORMAL_PROTOCOL_ID,
        "design_role": "confirmatory_fixed_input_comparison",
        "activation": {
            "event": "reviewed_merge_to_main",
            "formal_data_before_activation_permitted": False,
            "changes_after_activation": (
                "create a new protocol version and restart formal collection"
            ),
        },
        "amendment": {
            "supersedes_protocol_id": SUPERSEDED_PROTOCOL_ID,
            "supersedes_protocol_sha256": SUPERSEDED_PROTOCOL_SHA256,
            "superseded_protocol_artifact": SUPERSEDED_PROTOCOL_PATH.name,
            "reason": (
                "replace the session-repeating pair-order schedule with a fixed "
                "cross-balanced schedule identified by an outcome-independent "
                "design audit before admissible collection"
            ),
            "prior_collection_data_eligible": False,
            "outcome_values_used_to_construct_schedule": False,
            "unchanged_components": [
                "research_questions_and_hypotheses",
                "sample_size",
                "workloads_and_fixed_inputs",
                "conditions_and_execution_boundaries",
                "environment_and_safety_constraints",
                "confirmatory_endpoints_and_thresholds",
                "analysis_and_bootstrap_method",
                "exclusions_missing_data_and_stopping_rules",
            ],
        },
        "claim_boundary": {
            "fixed_input_single_device_only": True,
            "physical_motion_permitted": False,
            "uart_access_permitted": False,
            "hard_real_time_claim_permitted": False,
            "heterogeneous_inference_claim_permitted": False,
            "resource_attribution_claim_permitted": False,
        },
        "hypotheses": {
            "responsiveness": (
                "Moving the same slow workload behind the Phase 1 execution boundary "
                "reduces periodic-probe disruption and keeps the asynchronous p95 of "
                "per-run maximum gaps within the preregistered practical bound."
            ),
            "workload_noninferiority": (
                "The asynchronous boundary preserves fixed-input workload performance "
                "within the preregistered ten-percent noninferiority margin."
            ),
            "lifecycle": (
                "All measured runs preserve bounded ownership, complete shutdown and "
                "zero stale consumption."
            ),
        },
        "environment": {
            "device": "Jetson Orin Nano Super 8GB",
            "machine": "aarch64",
            "python_version": "3.10.12",
            "jetpack_package_version": "6.2.2+b24",
            "l4t_core_package_version": "36.5.0-20260115194252",
            "power_mode": {"name": "MAXN_SUPER", "id": 2},
            "clock_policy": "dynamic_dvfs_jetson_clocks_not_enabled",
            "motion_environment_value": "0",
            "resource_interval_ms": RESOURCE_INTERVAL_MS,
            "probe_period_ms": PROBE_PERIOD_MS,
            "probe_deadline_ms": PROBE_DEADLINE_MS,
            "session_start_max_tj_c": 55.0,
            "session_start_consecutive_samples": 10,
            "measurement_start_max_tj_c": 55.0,
            "measurement_start_consecutive_samples": 10,
            "thermal_stop_tj_c": 85.0,
            "minimum_session_separation_minutes": 30,
            "service_restart_between_sessions": True,
            "one_slow_workload_at_a_time": True,
            "unrelated_inference_processes_permitted": False,
        },
        "workloads": _workload_contracts(),
        "conditions": {
            "formal_idle": {
                "slow_workload": None,
                "probe": "independent",
                "purpose": "session-local descriptive timing reference",
            },
            "formal_sync": {
                "slow_workload": "calling_thread",
                "probe": "inline_same_thread",
                "runtime_broker": False,
                "purpose": "blocking synchronous control-flow reference",
            },
            "formal_async": {
                "slow_workload": "phase1_single_worker",
                "probe": "independent",
                "runtime_broker": True,
                "purpose": "bounded asynchronous runtime condition",
            },
        },
        "design": {
            "session_count": SESSION_COUNT,
            "paired_blocks_per_session_per_workload": PAIRS_PER_SESSION,
            "measured_pairs_per_workload": SESSION_COUNT * PAIRS_PER_SESSION,
            "measured_runs_per_workload_per_condition": (
                SESSION_COUNT * PAIRS_PER_SESSION
            ),
            "total_measured_runs": (
                SESSION_COUNT * PAIRS_PER_SESSION * len(WORKLOADS) * len(CONDITIONS)
            ),
            "warmups_per_session": {"asr": 3, "llm": 1, "vlm": 1},
            "idle_epochs_per_session": 2,
            "idle_epoch_s": IDLE_EPOCH_S,
            "condition_order": "fixed_cross_balanced_by_session_block_and_workload",
            "workload_order": "each_permutation_once_per_session",
            "runtime_order_randomization": False,
            "prelude_s": 1.0,
            "postlude_s": 1.0,
        },
        "sessions": sessions,
        "confirmatory_endpoints": {
            "responsiveness": {
                "metric": "probe.maximum_observed_gap_ms",
                "workloads": list(WORKLOADS),
                "criteria": {
                    "formal_async_p95_lte_ms": ASYNC_MAX_GAP_P95_MS,
                    "paired_mean_difference_ci95_high_lt_ms": 0.0,
                },
            },
            "workload_noninferiority": {
                "metrics": {
                    "asr": "adapter_total_ms",
                    "llm": "request_ms_per_completion_token",
                    "vlm": "adapter_total_ms",
                },
                "effect": "geometric_mean_of_within_pair_async_sync_ratios",
                "criterion": {
                    "paired_geometric_mean_ratio_ci95_high_lte": (NONINFERIORITY_RATIO)
                },
            },
            "lifecycle": {
                "criteria": {
                    "all_run_gates_pass": True,
                    "stale_consumed_count": 0,
                    "capacity_violation_count": 0,
                    "unreaped_process_count": 0,
                    "unjoined_thread_count": 0,
                }
            },
            "decision_rule": (
                "all responsiveness, workload-noninferiority and lifecycle criteria "
                "must pass for every workload"
            ),
            "multiplicity": (
                "intersection-union decision; no claim passes unless every "
                "preregistered workload criterion passes"
            ),
        },
        "analysis": {
            "unit": "within_session_workload_block_pair",
            "pairing_keys": ["session", "block", "workload"],
            "p95_method": "nearest_rank",
            "bootstrap": {
                "method": "paired_hierarchical_percentile_bootstrap",
                "confidence_level": 0.95,
                "resamples": BOOTSTRAP_RESAMPLES,
                "seed": BOOTSTRAP_SEED,
                "procedure": (
                    "resample five sessions with replacement, then resample six paired "
                    "blocks with replacement within each selected session; retain each "
                    "sync/async pair"
                ),
            },
            "responsiveness_difference_estimand": (
                "arithmetic_mean_of_within_pair_async_minus_sync_differences"
            ),
            "noninferiority_estimand": (
                "exponential_of_the_arithmetic_mean_of_within_pair_log_async_sync_ratios"
            ),
            "positive_performance_metric_required": True,
            "effects": [
                "formal_async_minus_formal_sync",
                "100_times_formal_async_divided_by_formal_sync_minus_100",
            ],
            "descriptive_statistics": [
                "count",
                "mean",
                "median",
                "sample_stddev",
                "cv_pct",
                "nearest_rank_p95",
                "min",
                "max",
            ],
            "secondary_metrics": [
                "probe_lateness_ms",
                "probe_deadline_miss_count",
                "probe_skipped_releases",
                "queue_wait_ms",
                "result_age_ms",
                "shutdown_join_ms",
                "llm_completion_tokens_per_second",
                "vlm_stage_durations_ms",
                "resource_run_means_and_peaks",
                "session_and_block_order_diagnostics",
            ],
            "resource_metrics_are_descriptive_only": True,
        },
        "exclusions_and_missing_data": {
            "post_hoc_outlier_exclusion_permitted": False,
            "warmups_in_confirmatory_analysis": False,
            "idle_epochs_in_confirmatory_analysis": False,
            "failed_measured_run_replacement_permitted": False,
            "imputation_permitted": False,
            "planned_attempts_are_denominator": True,
            "incomplete_pair_result": "confirmatory_analysis_invalid",
            "aborted_session_policy": {
                "all_attempts_retained_in_ledger": True,
                "replacement_permitted_only_for_infrastructure_failure": True,
                "replacement_must_begin_before_outcome_review": True,
                "replacement_uses_new_session_identifier": True,
                "aborted_session_runs_in_confirmatory_timing_analysis": False,
                "system_under_test_failure_replacement_permitted": False,
            },
            "infrastructure_failures": [
                "host_power_loss",
                "device_reboot",
                "unrecoverable_model_service_crash",
                "resource_sampler_failure",
            ],
        },
        "stopping_rules": [
            "physical_motion_or_uart_access",
            "stale_result_consumed",
            "queue_or_lifecycle_invariant_violation",
            "worker_or_child_cleanup_failure",
            "unexplained_resource_parse_error",
            "git_input_model_or_environment_identity_drift",
            "thermal_stop_threshold_reached",
        ],
        "reporting": {
            "report_all_planned_attempts": True,
            "report_condition_order": True,
            "report_failures_and_aborted_sessions": True,
            "report_confidence_intervals_and_effects": True,
            "serialize_raw_input_or_model_output": False,
            "formal_claim_before_complete_validation_permitted": False,
        },
    }
    _validate_generated_schedule(protocol)
    return protocol


def _validate_generated_schedule(protocol: Mapping[str, object]) -> None:
    sessions = protocol["sessions"]
    if not isinstance(sessions, list) or len(sessions) != SESSION_COUNT:
        raise ValueError("generated protocol has an invalid session count")
    workload_orders: Counter[tuple[str, ...]] = Counter()
    pair_orders: dict[str, Counter[tuple[str, ...]]] = {
        workload: Counter() for workload in WORKLOADS
    }
    condition_counts: Counter[tuple[str, str]] = Counter()
    pair_counts: Counter[tuple[str, str]] = Counter()
    first_conditions_by_block: dict[tuple[str, int], Counter[str]] = {
        (workload, block): Counter()
        for workload in WORKLOADS
        for block in range(1, PAIRS_PER_SESSION + 1)
    }
    first_conditions_by_position: dict[tuple[str, int], Counter[str]] = {
        (workload, position): Counter()
        for workload in WORKLOADS
        for position in range(1, len(WORKLOADS) + 1)
    }
    first_conditions_by_predecessor: dict[tuple[str, str], Counter[str]] = {
        (previous, workload): Counter()
        for previous in WORKLOADS
        for workload in WORKLOADS
        if previous != workload
    }
    async_first_per_session_block: list[int] = []
    total_runs = 0
    for session in sessions:
        if not isinstance(session, Mapping):
            raise ValueError("generated session must be an object")
        runs = session["measured_runs"]
        if not isinstance(runs, list):
            raise ValueError("generated measured_runs must be a list")
        total_runs += len(runs)
        by_block: dict[int, list[Mapping[str, object]]] = {}
        for run in runs:
            if not isinstance(run, Mapping):
                raise ValueError("generated measured run must be an object")
            block = run["block"]
            if not isinstance(block, int):
                raise ValueError("generated block must be an integer")
            by_block.setdefault(block, []).append(run)
            workload = str(run["workload"])
            condition = str(run["condition"])
            pair_id = str(run["pair_id"])
            condition_counts[(workload, condition)] += 1
            pair_counts[(workload, pair_id)] += 1
        previous_workload: str | None = None
        for block in sorted(by_block):
            block_runs = by_block[block]
            first_by_workload = [run for run in block_runs if run["pair_position"] == 1]
            ordered_first_runs = sorted(
                first_by_workload, key=lambda item: item["workload_position"]
            )
            ordered = tuple(str(run["workload"]) for run in ordered_first_runs)
            workload_orders[ordered] += 1
            async_first_per_session_block.append(
                sum(run["condition"] == "formal_async" for run in ordered_first_runs)
            )
            for run in ordered_first_runs:
                workload = str(run["workload"])
                condition = str(run["condition"])
                position = int(run["workload_position"])
                first_conditions_by_block[(workload, block)][condition] += 1
                first_conditions_by_position[(workload, position)][condition] += 1
                if previous_workload is not None:
                    first_conditions_by_predecessor[(previous_workload, workload)][
                        condition
                    ] += 1
                previous_workload = workload
            for workload in WORKLOADS:
                pair = tuple(
                    str(run["condition"])
                    for run in block_runs
                    if run["workload"] == workload
                )
                pair_orders[workload][pair] += 1
        if len(by_block) != PAIRS_PER_SESSION:
            raise ValueError("generated session has an invalid block count")
        session_orders = Counter(
            tuple(
                str(run["workload"])
                for run in sorted(
                    (item for item in block_runs if item["pair_position"] == 1),
                    key=lambda item: item["workload_position"],
                )
            )
            for block_runs in by_block.values()
        )
        if session_orders != Counter({order: 1 for order in _WORKLOAD_ORDERS}):
            raise ValueError("generated session workload order is not balanced")
        for workload in WORKLOADS:
            session_pair_orders = Counter(
                tuple(
                    str(run["condition"])
                    for run in block_runs
                    if run["workload"] == workload
                )
                for block_runs in by_block.values()
            )
            if session_pair_orders != Counter(
                {_CONDITION_ORDERS[0]: 3, _CONDITION_ORDERS[1]: 3}
            ):
                raise ValueError("generated session condition order is not balanced")
    expected_runs = SESSION_COUNT * PAIRS_PER_SESSION * len(WORKLOADS) * len(CONDITIONS)
    if total_runs != expected_runs:
        raise ValueError("generated protocol has an invalid measured run count")
    if set(workload_orders) != set(_WORKLOAD_ORDERS) or set(
        workload_orders.values()
    ) != {5}:
        raise ValueError("generated workload order is not balanced")
    if any(count != 30 for count in condition_counts.values()):
        raise ValueError("generated condition counts are not balanced")
    if any(count != 2 for count in pair_counts.values()):
        raise ValueError("generated pairs are incomplete")
    if any(
        counter != Counter({_CONDITION_ORDERS[0]: 15, _CONDITION_ORDERS[1]: 15})
        for counter in pair_orders.values()
    ):
        raise ValueError("generated condition order is not balanced")
    if any(
        set(counter) != set(CONDITIONS)
        or sum(counter.values()) != SESSION_COUNT
        or set(counter.values()) != {2, 3}
        for counter in first_conditions_by_block.values()
    ):
        raise ValueError("generated condition order is not balanced across sessions")
    if any(
        counter != Counter({condition: 5 for condition in CONDITIONS})
        for counter in first_conditions_by_position.values()
    ):
        raise ValueError(
            "generated condition order is not balanced by workload position"
        )
    for counter in first_conditions_by_predecessor.values():
        total = sum(counter.values())
        expected_counts = {total // 2, total - total // 2}
        if set(counter) != set(CONDITIONS) or set(counter.values()) != expected_counts:
            raise ValueError(
                "generated condition order is not balanced by preceding workload"
            )
    if any(count not in {1, 2} for count in async_first_per_session_block):
        raise ValueError("generated session block has a uniform condition order")


def canonical_protocol_text(protocol: Mapping[str, object]) -> str:
    return json.dumps(protocol, ensure_ascii=False, indent=2, allow_nan=False) + "\n"


def protocol_sha256(protocol: Mapping[str, object]) -> str:
    return hashlib.sha256(canonical_protocol_text(protocol).encode("utf-8")).hexdigest()


def _first_difference(
    expected: object, observed: object, path: str = "protocol"
) -> str | None:
    if type(expected) is not type(observed):
        return f"{path} has type {type(observed).__name__}, expected {type(expected).__name__}"
    if isinstance(expected, Mapping):
        expected_keys = set(expected)
        observed_keys = set(observed)
        if expected_keys != observed_keys:
            missing = sorted(expected_keys - observed_keys)
            extra = sorted(observed_keys - expected_keys)
            return f"{path} keys differ: missing={missing}, extra={extra}"
        for key in expected:
            difference = _first_difference(
                expected[key], observed[key], f"{path}.{key}"
            )
            if difference is not None:
                return difference
        return None
    if isinstance(expected, list):
        if len(expected) != len(observed):
            return f"{path} length is {len(observed)}, expected {len(expected)}"
        for index, (expected_item, observed_item) in enumerate(zip(expected, observed)):
            difference = _first_difference(
                expected_item,
                observed_item,
                f"{path}[{index}]",
            )
            if difference is not None:
                return difference
        return None
    if expected != observed:
        return f"{path} is {observed!r}, expected {expected!r}"
    return None


def formal_protocol_errors(protocol: object) -> list[str]:
    if not isinstance(protocol, Mapping):
        return ["formal protocol must be a JSON object"]
    difference = _first_difference(build_formal_protocol(), protocol)
    return [] if difference is None else [difference]


def load_formal_protocol(path: Path | str = DEFAULT_PROTOCOL_PATH) -> dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("formal protocol must be a JSON object")
    return value


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8", newline="\n")
    os.replace(temporary, path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("protocol", nargs="?", type=Path, default=DEFAULT_PROTOCOL_PATH)
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--print-sha256", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.write:
            protocol = build_formal_protocol()
            _write_atomic(args.protocol, canonical_protocol_text(protocol))
        else:
            protocol = load_formal_protocol(args.protocol)
        errors = formal_protocol_errors(protocol)
        if errors:
            for error in errors:
                print(f"ERROR: {error}", file=sys.stderr)
            return 1
        print("VALID")
        if args.print_sha256:
            print(protocol_sha256(protocol))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
