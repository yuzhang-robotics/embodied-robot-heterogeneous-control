from __future__ import annotations

import copy
import tempfile
import unittest
from collections import Counter
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

from experiments.phase1.formal_protocol import (
    ASYNC_MAX_GAP_P95_MS,
    BOOTSTRAP_RESAMPLES,
    BOOTSTRAP_SEED,
    CONDITIONS,
    DEFAULT_PROTOCOL_PATH,
    FORMAL_PROTOCOL_ID,
    FORMAL_V2_PROTOCOL_ID,
    FORMAL_V2_PROTOCOL_PATH,
    FORMAL_V2_PROTOCOL_SHA256,
    FORMAL_V3_PROTOCOL_ID,
    FORMAL_V3_PROTOCOL_PATH,
    FORMAL_V3_PROTOCOL_SHA256,
    NONINFERIORITY_RATIO,
    PAIRS_PER_SESSION,
    SESSION_COUNT,
    SUPERSEDED_PROTOCOL_ID,
    SUPERSEDED_PROTOCOL_PATH,
    SUPERSEDED_PROTOCOL_SHA256,
    VLM_PROCESS_PROTOCOL_VERSION,
    VLM_OLLAMA_BINARY_SHA256,
    VLM_OLLAMA_VERSION,
    WORKLOADS,
    build_formal_protocol,
    formal_protocol_errors,
    load_formal_protocol,
    main,
    protocol_sha256,
)
from jetson.vlm_request_contract import (
    MODEL_UNLOAD_POLL_INTERVAL_S,
    MODEL_UNLOAD_TIMEOUT_S,
    MOONDREAM_REQUEST_TEMPERATURE,
    QWEN_REQUEST_TEMPERATURE,
    QWEN_REQUEST_TIMEOUT_S,
    VLM_REQUEST_CONTRACT_VERSION,
    VLM_REQUEST_SEED,
)


EXPECTED_PROTOCOL_SHA256 = (
    "84da36aa9b4a804ecc5692b12902321e42254f707463d1a5937e7049ffa0d054"
)

EXPECTED_PAIR_ORDER_MATRIX = (
    {"asr": "100110", "llm": "100101", "vlm": "011001"},
    {"asr": "101001", "llm": "011010", "vlm": "010110"},
    {"asr": "010101", "llm": "101001", "vlm": "101010"},
    {"asr": "011010", "llm": "010110", "vlm": "100101"},
    {"asr": "001101", "llm": "100011", "vlm": "011010"},
)


class FormalProtocolTests(unittest.TestCase):
    def test_tracked_protocol_matches_the_frozen_builder(self) -> None:
        tracked = load_formal_protocol(DEFAULT_PROTOCOL_PATH)

        self.assertEqual(tracked, build_formal_protocol())
        self.assertEqual(formal_protocol_errors(tracked), [])
        self.assertEqual(tracked["protocol_id"], FORMAL_PROTOCOL_ID)
        self.assertEqual(protocol_sha256(tracked), EXPECTED_PROTOCOL_SHA256)
        self.assertEqual(tracked["design"]["session_count"], SESSION_COUNT)
        self.assertEqual(
            tracked["design"]["measured_runs_per_workload_per_condition"],
            SESSION_COUNT * PAIRS_PER_SESSION,
        )
        self.assertEqual(tracked["design"]["total_measured_runs"], 180)

    def test_superseded_protocol_is_preserved_with_its_frozen_identity(self) -> None:
        superseded = load_formal_protocol(SUPERSEDED_PROTOCOL_PATH)

        self.assertEqual(superseded["protocol_id"], SUPERSEDED_PROTOCOL_ID)
        self.assertEqual(protocol_sha256(superseded), SUPERSEDED_PROTOCOL_SHA256)
        self.assertEqual(
            build_formal_protocol()["amendment"]["superseded_protocol_artifact"],
            SUPERSEDED_PROTOCOL_PATH.name,
        )

    def test_closed_v2_and_v3_protocols_keep_their_frozen_identities(self) -> None:
        for path, protocol_id, digest in (
            (
                FORMAL_V2_PROTOCOL_PATH,
                FORMAL_V2_PROTOCOL_ID,
                FORMAL_V2_PROTOCOL_SHA256,
            ),
            (
                FORMAL_V3_PROTOCOL_PATH,
                FORMAL_V3_PROTOCOL_ID,
                FORMAL_V3_PROTOCOL_SHA256,
            ),
        ):
            with self.subTest(protocol_id=protocol_id):
                protocol = load_formal_protocol(path)
                self.assertEqual(protocol["protocol_id"], protocol_id)
                self.assertEqual(protocol_sha256(protocol), digest)

    def test_schedule_balances_conditions_pairs_and_workload_orders(self) -> None:
        protocol = build_formal_protocol()
        condition_counts: Counter[tuple[str, str]] = Counter()
        pair_counts: Counter[tuple[str, str]] = Counter()
        pair_orders: dict[str, Counter[tuple[str, ...]]] = {
            workload: Counter() for workload in WORKLOADS
        }
        workload_orders: Counter[tuple[str, ...]] = Counter()

        for session in protocol["sessions"]:
            runs = session["measured_runs"]
            for run in runs:
                condition_counts[(run["workload"], run["condition"])] += 1
                pair_counts[(run["workload"], run["pair_id"])] += 1
            for block in range(1, PAIRS_PER_SESSION + 1):
                block_runs = [run for run in runs if run["block"] == block]
                first_runs = sorted(
                    (run for run in block_runs if run["pair_position"] == 1),
                    key=lambda run: run["workload_position"],
                )
                workload_orders[tuple(run["workload"] for run in first_runs)] += 1
                for workload in WORKLOADS:
                    pair_orders[workload][
                        tuple(
                            run["condition"]
                            for run in block_runs
                            if run["workload"] == workload
                        )
                    ] += 1

        for workload in WORKLOADS:
            for condition in CONDITIONS:
                self.assertEqual(condition_counts[(workload, condition)], 30)
            self.assertEqual(set(pair_orders[workload].values()), {15})
        self.assertTrue(all(count == 2 for count in pair_counts.values()))
        self.assertEqual(len(pair_counts), 90)
        self.assertEqual(len(workload_orders), 6)
        self.assertEqual(set(workload_orders.values()), {5})

    def test_confirmatory_thresholds_and_analysis_are_fixed(self) -> None:
        protocol = build_formal_protocol()
        endpoints = protocol["confirmatory_endpoints"]
        analysis = protocol["analysis"]

        self.assertEqual(
            endpoints["responsiveness"]["criteria"]["formal_async_p95_lte_ms"],
            ASYNC_MAX_GAP_P95_MS,
        )
        self.assertEqual(
            endpoints["workload_noninferiority"]["criterion"][
                "paired_geometric_mean_ratio_ci95_high_lte"
            ],
            NONINFERIORITY_RATIO,
        )
        self.assertEqual(
            analysis["noninferiority_estimand"],
            "exponential_of_the_arithmetic_mean_of_within_pair_log_async_sync_ratios",
        )
        self.assertEqual(analysis["bootstrap"]["resamples"], BOOTSTRAP_RESAMPLES)
        self.assertEqual(analysis["bootstrap"]["seed"], BOOTSTRAP_SEED)
        self.assertEqual(analysis["p95_method"], "nearest_rank")
        self.assertEqual(
            protocol["exclusions_and_missing_data"][
                "post_hoc_outlier_exclusion_permitted"
            ],
            False,
        )
        self.assertEqual(
            protocol["exclusions_and_missing_data"]["imputation_permitted"],
            False,
        )
        ollama = protocol["workloads"]["vlm"]["moondream"]["service"]
        self.assertEqual(ollama["version"], VLM_OLLAMA_VERSION)
        self.assertEqual(ollama["binary_sha256"], VLM_OLLAMA_BINARY_SHA256)
        self.assertNotIn("path", ollama)
        vlm = protocol["workloads"]["vlm"]
        self.assertEqual(vlm["process_protocol_version"], VLM_PROCESS_PROTOCOL_VERSION)
        self.assertEqual(
            vlm["request_contract_version"], VLM_REQUEST_CONTRACT_VERSION
        )
        self.assertEqual(
            vlm["successful_stage_order"],
            [
                "input_verify_before",
                "module_import",
                "moondream_inference",
                "model_unload",
                "qwen_rewrite",
                "output_normalization",
                "input_verify_after",
            ],
        )
        self.assertEqual(
            vlm["moondream"]["request"]["temperature"],
            MOONDREAM_REQUEST_TEMPERATURE,
        )
        self.assertEqual(
            vlm["moondream"]["request"]["seed"], VLM_REQUEST_SEED
        )
        self.assertEqual(
            vlm["qwen"]["request"]["temperature"], QWEN_REQUEST_TEMPERATURE
        )
        self.assertEqual(vlm["qwen"]["request"]["seed"], VLM_REQUEST_SEED)
        self.assertEqual(
            vlm["qwen"]["request"]["timeout_s"], QWEN_REQUEST_TIMEOUT_S
        )
        self.assertTrue(vlm["cleanup_unload_on_failure"])
        self.assertEqual(
            vlm["unload_confirmation"],
            {
                "method": "ollama_process_list_absence",
                "timeout_s": MODEL_UNLOAD_TIMEOUT_S,
                "poll_interval_ms": int(MODEL_UNLOAD_POLL_INTERVAL_S * 1_000),
            },
        )
        amendment = protocol["amendment"]
        self.assertTrue(
            amendment[
                "failure_and_diagnostics_used_to_modify_vlm_workload_contract"
            ]
        )
        self.assertFalse(amendment["outcome_values_used_to_modify_schedule"])
        self.assertFalse(
            amendment["outcome_values_used_to_modify_hypotheses_thresholds_or_analysis"]
        )
        self.assertFalse(
            amendment["repair_provenance"]["target_validation"][
                "performance_or_causal_claim_permitted"
            ]
        )
        self.assertNotIn(
            "workload_models_fixed_inputs_and_request_parameters",
            amendment["unchanged_components"],
        )

    def test_each_session_is_independently_order_balanced(self) -> None:
        protocol = build_formal_protocol()

        for session in protocol["sessions"]:
            runs = session["measured_runs"]
            orders = Counter()
            pair_orders = {workload: Counter() for workload in WORKLOADS}
            for block in range(1, PAIRS_PER_SESSION + 1):
                block_runs = [run for run in runs if run["block"] == block]
                first_runs = sorted(
                    (run for run in block_runs if run["pair_position"] == 1),
                    key=lambda run: run["workload_position"],
                )
                orders[tuple(run["workload"] for run in first_runs)] += 1
                for workload in WORKLOADS:
                    pair_orders[workload][
                        tuple(
                            run["condition"]
                            for run in block_runs
                            if run["workload"] == workload
                        )
                    ] += 1

            self.assertEqual(len(orders), 6)
            self.assertEqual(set(orders.values()), {1})
            for workload in WORKLOADS:
                self.assertEqual(set(pair_orders[workload].values()), {3})

    def test_pair_order_matrix_is_fixed_and_cross_balanced(self) -> None:
        protocol = build_formal_protocol()
        observed_matrix = []
        by_block: dict[tuple[str, int], Counter[str]] = {
            (workload, block): Counter()
            for workload in WORKLOADS
            for block in range(1, PAIRS_PER_SESSION + 1)
        }
        by_position: dict[tuple[str, int], Counter[str]] = {
            (workload, position): Counter()
            for workload in WORKLOADS
            for position in range(1, len(WORKLOADS) + 1)
        }
        by_predecessor: dict[tuple[str, str], Counter[str]] = {
            (previous, workload): Counter()
            for previous in WORKLOADS
            for workload in WORKLOADS
            if previous != workload
        }

        for session in protocol["sessions"]:
            row = {workload: [] for workload in WORKLOADS}
            previous = None
            for block in range(1, PAIRS_PER_SESSION + 1):
                first_runs = sorted(
                    (
                        run
                        for run in session["measured_runs"]
                        if run["block"] == block and run["pair_position"] == 1
                    ),
                    key=lambda run: run["workload_position"],
                )
                self.assertIn(
                    sum(run["condition"] == "formal_async" for run in first_runs),
                    {1, 2},
                )
                for run in first_runs:
                    workload = run["workload"]
                    condition = run["condition"]
                    row[workload].append("1" if condition == "formal_async" else "0")
                    by_block[(workload, block)][condition] += 1
                    by_position[(workload, run["workload_position"])][condition] += 1
                    if previous is not None:
                        by_predecessor[(previous, workload)][condition] += 1
                    previous = workload
            observed_matrix.append(
                {workload: "".join(row[workload]) for workload in WORKLOADS}
            )

        self.assertEqual(tuple(observed_matrix), EXPECTED_PAIR_ORDER_MATRIX)
        for counts in by_block.values():
            self.assertEqual(sum(counts.values()), 5)
            self.assertEqual(set(counts.values()), {2, 3})
        for counts in by_position.values():
            self.assertEqual(
                counts,
                Counter({"formal_sync": 5, "formal_async": 5}),
            )
        for counts in by_predecessor.values():
            total = sum(counts.values())
            self.assertEqual(
                set(counts.values()),
                {total // 2, total - total // 2},
            )

    def test_protocol_rejects_schedule_threshold_and_key_changes(self) -> None:
        protocol = build_formal_protocol()

        changed_threshold = copy.deepcopy(protocol)
        changed_threshold["confirmatory_endpoints"]["responsiveness"]["criteria"][
            "formal_async_p95_lte_ms"
        ] = 301.0
        self.assertRegex(
            formal_protocol_errors(changed_threshold)[0],
            "formal_async_p95_lte_ms",
        )

        missing_run = copy.deepcopy(protocol)
        missing_run["sessions"][0]["measured_runs"].pop()
        self.assertRegex(formal_protocol_errors(missing_run)[0], "length")

        extra_key = copy.deepcopy(protocol)
        extra_key["unregistered"] = True
        self.assertRegex(formal_protocol_errors(extra_key)[0], "keys differ")

    def test_cli_writes_and_validates_deterministically(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "protocol.json"
            stdout = StringIO()
            stderr = StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                first_result = main([str(path), "--write", "--print-sha256"])
                first = path.read_bytes()
                second_result = main([str(path), "--write", "--print-sha256"])
                second = path.read_bytes()
                validation_result = main([str(path)])
            temporary_files = list(path.parent.glob("*.tmp"))

        self.assertEqual(first_result, 0)
        self.assertEqual(second_result, 0)
        self.assertEqual(validation_result, 0)
        self.assertEqual(first, second)
        self.assertIn(EXPECTED_PROTOCOL_SHA256, stdout.getvalue())
        self.assertEqual(stderr.getvalue(), "")
        self.assertFalse(temporary_files)


if __name__ == "__main__":
    unittest.main()
