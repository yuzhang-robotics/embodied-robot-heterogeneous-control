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
    NONINFERIORITY_RATIO,
    PAIRS_PER_SESSION,
    SESSION_COUNT,
    VLM_OLLAMA_BINARY_SHA256,
    VLM_OLLAMA_VERSION,
    WORKLOADS,
    build_formal_protocol,
    formal_protocol_errors,
    load_formal_protocol,
    main,
    protocol_sha256,
)


EXPECTED_PROTOCOL_SHA256 = (
    "022df6af4bb3236a28b2e47f0edb9afbc6078131441a1c1f9e8730920c660761"
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
