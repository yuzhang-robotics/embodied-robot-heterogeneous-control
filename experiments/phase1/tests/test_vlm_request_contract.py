from __future__ import annotations

import unittest

from jetson.vlm_request_contract import (
    MODEL_UNLOAD_POLL_INTERVAL_S,
    MODEL_UNLOAD_TIMEOUT_S,
    MOONDREAM_PROMPTS,
    QWEN_REQUEST_MAX_TOKENS,
    QWEN_REQUEST_TIMEOUT_S,
    VLM_REQUEST_SEED,
    build_moondream_payload,
    build_qwen_payload,
    current_vlm_workload_contract,
    ollama_model_running,
    wait_for_ollama_model_unload,
)


class VLMRequestContractTests(unittest.TestCase):
    def test_request_payloads_are_deterministic_and_bounded(self) -> None:
        moondream = build_moondream_payload(
            "moondream",
            MOONDREAM_PROMPTS[0],
            "private-image-base64",
        )
        qwen = build_qwen_payload("private English description")

        self.assertEqual(
            moondream["options"],
            {
                "temperature": 0.0,
                "seed": VLM_REQUEST_SEED,
                "num_predict": 100,
            },
        )
        self.assertFalse(moondream["stream"])
        self.assertEqual(qwen["temperature"], 0.0)
        self.assertEqual(qwen["seed"], VLM_REQUEST_SEED)
        self.assertEqual(qwen["max_tokens"], QWEN_REQUEST_MAX_TOKENS)
        self.assertFalse(qwen["stream"])

    def test_workload_contract_records_timeout_and_unload_confirmation(self) -> None:
        contract = current_vlm_workload_contract()

        self.assertEqual(
            contract["qwen_rewrite"]["request_timeout_s"],
            QWEN_REQUEST_TIMEOUT_S,
        )
        self.assertEqual(
            contract["unload_confirmation"],
            {
                "method": "ollama_process_list_absence",
                "timeout_s": MODEL_UNLOAD_TIMEOUT_S,
                "poll_interval_ms": int(MODEL_UNLOAD_POLL_INTERVAL_S * 1000),
            },
        )
        self.assertTrue(contract["unload_before_qwen"])

    def test_ollama_model_identity_accepts_latest_tag(self) -> None:
        response = {
            "models": [{"name": "moondream:latest", "model": "moondream:latest"}]
        }

        self.assertTrue(ollama_model_running(response, "moondream"))
        self.assertFalse(ollama_model_running(response, "qwen2.5vl:3b"))

    def test_unload_waits_until_model_is_absent(self) -> None:
        responses = iter(
            [
                {"models": [{"name": "moondream:latest"}]},
                {"models": [{"name": "moondream:latest"}]},
                {"models": []},
            ]
        )
        clock_values = iter((0.0, 0.1, 0.2, 0.3, 0.4))
        sleeps: list[float] = []

        confirmed = wait_for_ollama_model_unload(
            "moondream",
            lambda: next(responses),
            timeout_s=1.0,
            poll_interval_s=0.1,
            clock=lambda: next(clock_values),
            sleeper=sleeps.append,
        )

        self.assertTrue(confirmed)
        self.assertEqual(sleeps, [0.1, 0.1])

    def test_unload_confirmation_is_bounded(self) -> None:
        clock_values = iter((0.0, 0.4, 0.8, 1.0))
        sleeps: list[float] = []

        confirmed = wait_for_ollama_model_unload(
            "moondream",
            lambda: {"models": [{"name": "moondream:latest"}]},
            timeout_s=1.0,
            poll_interval_s=0.5,
            clock=lambda: next(clock_values),
            sleeper=sleeps.append,
        )

        self.assertFalse(confirmed)
        self.assertEqual(len(sleeps), 2)
        self.assertAlmostEqual(sleeps[0], 0.5)
        self.assertAlmostEqual(sleeps[1], 0.2)


if __name__ == "__main__":
    unittest.main()
