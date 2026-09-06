from __future__ import annotations

import copy
import unittest
from unittest.mock import patch

from experiments.phase1.formal_preflight import (
    build_formal_preflight,
    formal_preflight_errors,
)
from experiments.phase1.formal_protocol import (
    LLAMA_SOURCE_VERSION,
    VLM_MOONDREAM_DIGEST,
    build_formal_protocol,
)
from experiments.phase1.llm_adapter import LLM_EXPECTED_SERVED_MODEL_ID
from experiments.phase1.tests.formal_fixture import passing_base_preflight
from jetson.phase1_runtime import PayloadRef


def fixture_payload(media_type: str) -> PayloadRef:
    return PayloadRef(
        ref="fixture://formal-input",
        sha256="a" * 64,
        size_bytes=1,
        media_type=media_type,
    )


class FormalPreflightTests(unittest.TestCase):
    def build(
        self,
        *,
        protocol: dict[str, object] | None = None,
        base: dict[str, object] | None = None,
        llm: dict[str, object] | None = None,
        vlm: dict[str, object] | None = None,
        ollama: dict[str, object] | None = None,
        service_identity: dict[str, object] | None = None,
        services_restarted: bool = True,
        dynamic_dvfs_confirmed: bool = True,
    ) -> dict[str, object]:
        qwen = [LLM_EXPECTED_SERVED_MODEL_ID]
        base_record = base or passing_base_preflight()
        llm_record = llm or {
            "runtime": {
                "source_version": LLAMA_SOURCE_VERSION,
                "served_model_ids": qwen,
            }
        }
        vlm_record = vlm or {
            "services": {
                "ollama": {"model_digest": VLM_MOONDREAM_DIGEST},
                "qwen": {"served_model_ids": qwen},
            }
        }
        ollama_record = ollama or {
            "version_output": "ollama version is 0.24.0",
            "binary_sha256": (
                "6273a99e321b5e69741aa024cc22e0ce2803aa2bdf20185ea19627b4d891c87a"
            ),
            "executable_path_recorded": False,
            "active_model_count": 0,
            "active_model_names_recorded": False,
            "error_code": None,
        }
        service_identity_record = service_identity or {
            "llama-server": {
                "process_count": 1,
                "process_start_identities": ["1 start"],
                "arguments_recorded": False,
            },
            "ollama": {
                "process_count": 1,
                "process_start_identities": ["2 start"],
                "arguments_recorded": False,
            },
        }
        with (
            patch(
                "experiments.phase1.formal_preflight.fixed_asr_payload",
                return_value=fixture_payload("audio/wav"),
            ),
            patch(
                "experiments.phase1.formal_preflight.fixed_llm_payload",
                return_value=fixture_payload("text/plain"),
            ),
            patch(
                "experiments.phase1.formal_preflight.fixed_c100_payload",
                return_value=fixture_payload("image/jpeg"),
            ),
            patch(
                "experiments.phase1.formal_preflight.asr_preflight_errors",
                return_value=[],
            ),
            patch(
                "experiments.phase1.formal_preflight.llm_preflight_errors",
                return_value=[],
            ),
            patch(
                "experiments.phase1.formal_preflight.vlm_preflight_errors",
                return_value=[],
            ),
            patch(
                "experiments.phase1.formal_preflight.command_snapshot",
                return_value={
                    "returncode": 0,
                    "output": "2" * 40,
                    "error_code": None,
                },
            ),
        ):
            return build_formal_preflight(
                ".",
                protocol or build_formal_protocol(),
                asr_input="asr.wav",
                llm_input="prompt.txt",
                vlm_input="image.jpg",
                services_restarted=services_restarted,
                dynamic_dvfs_confirmed=dynamic_dvfs_confirmed,
                base_preflight=base_record,
                asr_preflight={},
                llm_preflight=llm_record,
                vlm_preflight=vlm_record,
                ollama_identity=ollama_record,
                service_identity=service_identity_record,
            )

    def test_active_v3_protocol_is_eligible(self) -> None:
        preflight = self.build()

        self.assertTrue(preflight["eligible"])
        self.assertEqual(formal_preflight_errors(preflight), [])
        self.assertFalse(preflight["protocol"]["path_recorded"])
        activated = next(
            check
            for check in preflight["checks"]
            if check["name"] == "protocol_activated"
        )
        self.assertEqual(
            activated["observed"]["collection_status"],
            "active",
        )

    def test_confirmation_and_identity_drift_fail_closed(self) -> None:
        preflight = self.build()
        changed = copy.deepcopy(preflight)
        changed["checks"][6]["passed"] = False
        changed["eligible"] = False

        errors = formal_preflight_errors(changed)

        self.assertIn("formal preflight check failed: dynamic_dvfs_confirmed", errors)

    def test_frozen_environment_and_service_drift_fail_closed(self) -> None:
        baseline = self.build()
        cases: list[tuple[str, dict[str, object]]] = []

        base = copy.deepcopy(baseline["base"])
        base["environment"]["python"] = "3.10.13"
        cases.append(("python_version", {"base": base}))

        ollama = copy.deepcopy(baseline["ollama"])
        ollama["binary_sha256"] = "0" * 64
        cases.append(("ollama_binary_identity", {"ollama": ollama}))

        ollama = copy.deepcopy(baseline["ollama"])
        ollama["active_model_count"] = 1
        cases.append(("unrelated_inference_absent", {"ollama": ollama}))

        llm = copy.deepcopy(baseline["workloads"]["llm"])
        llm["runtime"]["source_version"] = "changed"
        cases.append(("llama_source_version", {"llm": llm}))

        vlm = copy.deepcopy(baseline["workloads"]["vlm"])
        vlm["services"]["ollama"]["model_digest"] = "0" * 64
        cases.append(("moondream_digest", {"vlm": vlm}))

        services = copy.deepcopy(baseline["service_identity"])
        services["ollama"]["process_count"] = 0
        cases.append(("services_restarted", {"service_identity": services}))

        for expected_check, kwargs in cases:
            with self.subTest(expected_check=expected_check):
                preflight = self.build(**kwargs)
                self.assertFalse(preflight["eligible"])
                errors = formal_preflight_errors(preflight)
                self.assertIn(
                    f"formal preflight check failed: {expected_check}", errors
                )


if __name__ == "__main__":
    unittest.main()
