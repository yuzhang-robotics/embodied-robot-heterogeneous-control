from __future__ import annotations

import copy
import unittest

from experiments.phase1.carryover_protocol import (
    CARRYOVER_PROTOCOL_ID,
    CARRYOVER_PROTOCOL_SHA256,
    diagnostic_orders,
    expected_protocol,
    load_protocol,
    protocol_errors,
    protocol_sha256,
    session_order,
)


class CarryoverProtocolTests(unittest.TestCase):
    def test_tracked_protocol_is_exact_and_has_six_permutations(self) -> None:
        protocol = load_protocol()

        self.assertEqual(protocol["protocol_id"], CARRYOVER_PROTOCOL_ID)
        self.assertEqual(protocol_errors(protocol), [])
        self.assertEqual(protocol_sha256(protocol), CARRYOVER_PROTOCOL_SHA256)
        observed = [session_order(protocol, index) for index in range(1, 7)]
        self.assertEqual(observed, list(diagnostic_orders()))
        self.assertEqual(len(set(observed)), 6)

    def test_protocol_forbids_formal_claims_and_retains_gpu_layers(self) -> None:
        protocol = expected_protocol()

        self.assertFalse(protocol["claim_boundary"]["formal_evidence"])
        self.assertFalse(protocol["analysis"]["formal_pass_fail_field_permitted"])
        self.assertEqual(protocol["safety"]["llama_n_gpu_layers"], 10)

    def test_any_schedule_or_claim_change_is_rejected(self) -> None:
        changed = copy.deepcopy(expected_protocol())
        changed["design"]["sessions"][0]["interposer_order"].reverse()
        self.assertTrue(protocol_errors(changed))

        changed = copy.deepcopy(expected_protocol())
        changed["claim_boundary"]["formal_evidence"] = True
        self.assertTrue(protocol_errors(changed))


if __name__ == "__main__":
    unittest.main()
