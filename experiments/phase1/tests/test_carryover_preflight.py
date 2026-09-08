from __future__ import annotations

import copy
import unittest

from experiments.phase1.carryover.preflight import carryover_preflight_errors
from experiments.phase1.tests.carryover_fixture import passing_carryover_preflight


class CarryoverPreflightTests(unittest.TestCase):
    def test_complete_preflight_is_eligible_and_nonformal(self) -> None:
        preflight = passing_carryover_preflight()

        self.assertEqual(carryover_preflight_errors(preflight), [])
        self.assertTrue(preflight["eligible"])
        self.assertFalse(preflight["formal_evidence"])

    def test_failed_required_check_is_rejected(self) -> None:
        preflight = copy.deepcopy(passing_carryover_preflight())
        preflight["checks"][0]["passed"] = False
        preflight["eligible"] = False

        errors = carryover_preflight_errors(preflight)
        self.assertTrue(any("check failed" in error for error in errors))

    def test_formal_evidence_and_protocol_drift_are_rejected(self) -> None:
        preflight = copy.deepcopy(passing_carryover_preflight())
        preflight["formal_evidence"] = True
        self.assertTrue(
            any(
                "must not claim formal evidence" in error
                for error in carryover_preflight_errors(preflight)
            )
        )

        preflight = copy.deepcopy(passing_carryover_preflight())
        preflight["protocol"]["sha256"] = "0" * 64
        self.assertTrue(
            any("SHA-256" in error for error in carryover_preflight_errors(preflight))
        )


if __name__ == "__main__":
    unittest.main()
