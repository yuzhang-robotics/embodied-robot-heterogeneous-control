from __future__ import annotations

import copy
import json
import unittest

from experiments.phase1.carryover.protocol import (
    CARRYOVER_PROTOCOL_ID,
    CARRYOVER_PROTOCOL_SHA256,
    CARRYOVER_V1_PROTOCOL_ID,
    CARRYOVER_V1_PROTOCOL_SHA256,
    DEFAULT_PROTOCOL_PATH,
    diagnostic_orders,
    expected_protocol,
    load_protocol,
    protocol_errors,
    protocol_sha256,
    session_order,
)


class CarryoverProtocolTests(unittest.TestCase):
    def test_v1_protocol_is_preserved_at_its_original_hash(self) -> None:
        path = DEFAULT_PROTOCOL_PATH.with_name("phase1-asr-vlm-carryover-v1.json")
        protocol = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(protocol["protocol_id"], CARRYOVER_V1_PROTOCOL_ID)
        self.assertEqual(protocol_sha256(protocol), CARRYOVER_V1_PROTOCOL_SHA256)

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
