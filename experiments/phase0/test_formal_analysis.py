"""Host-side tests for the Phase 0 formal analysis helpers."""

import unittest

from .analyze_formal_runs import (
    _validate_resource_intervals,
    formal_stats,
    linear_trend,
    nearest_rank,
)


class FormalStatisticsTests(unittest.TestCase):
    def test_nearest_rank_matches_phase0_convention(self) -> None:
        self.assertEqual(nearest_rank(range(1, 31), 95), 29.0)
        self.assertEqual(nearest_rank([1, 2, 3, 4], 100), 4.0)

    def test_formal_stats_are_deterministic_and_session_stratified(self) -> None:
        groups = {"session-01": [1.0, 2.0], "session-02": [3.0, 4.0]}
        first = formal_stats(groups, resamples=2_000, seed=1234)
        second = formal_stats(groups, resamples=2_000, seed=1234)

        self.assertEqual(first, second)
        self.assertEqual(first["count"], 4)
        self.assertEqual(first["mean"], 2.5)
        self.assertEqual(first["median"], 2.5)
        self.assertEqual(first["p95_nearest_rank"], 4.0)
        self.assertLessEqual(first["mean_ci95"]["low"], first["mean"])
        self.assertGreaterEqual(first["mean_ci95"]["high"], first["mean"])

    def test_linear_trend_reports_half_session_change(self) -> None:
        result = linear_trend([10.0, 8.0, 6.0, 4.0])

        self.assertAlmostEqual(result["slope_per_run"], -2.0)
        self.assertEqual(result["first_half_mean"], 9.0)
        self.assertEqual(result["second_half_mean"], 5.0)
        self.assertAlmostEqual(result["second_vs_first_pct"], -44.4444444444)
        self.assertAlmostEqual(result["position_pearson_r"], -1.0)

    def test_formal_resource_interval_must_be_exactly_200_ms(self) -> None:
        self.assertEqual(_validate_resource_intervals([200, 200]), 200)
        with self.assertRaisesRegex(ValueError, "expected 200 ms"):
            _validate_resource_intervals([100, 100])
        with self.assertRaisesRegex(ValueError, "expected 200 ms"):
            _validate_resource_intervals([200, 100])


if __name__ == "__main__":
    unittest.main()
