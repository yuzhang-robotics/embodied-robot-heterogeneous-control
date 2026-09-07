from __future__ import annotations

from experiments.phase1.carryover_preflight import (
    CARRYOVER_PREFLIGHT_SCHEMA_VERSION,
    _REQUIRED_CHECKS,
)
from experiments.phase1.carryover_protocol import (
    CARRYOVER_PROTOCOL_ID,
    CARRYOVER_PROTOCOL_SHA256,
)
from experiments.phase1.tests.formal_fixture import passing_base_preflight


def passing_carryover_preflight(service_suffix: str = "a") -> dict[str, object]:
    commit = "1" * 40
    return {
        "carryover_preflight_schema_version": CARRYOVER_PREFLIGHT_SCHEMA_VERSION,
        "captured_at": "2026-09-07T00:00:00Z",
        "protocol": {
            "id": CARRYOVER_PROTOCOL_ID,
            "sha256": CARRYOVER_PROTOCOL_SHA256,
            "protocol_commit": "3" * 40,
            "runner_commit": commit,
            "path_recorded": False,
        },
        "base": passing_base_preflight(commit),
        "workloads": {"asr": {}, "llm": {}, "vlm": {}},
        "ollama": {},
        "service_identity": {
            "llama-server": {
                "process_count": 1,
                "process_start_identities": [f"1 {service_suffix}"],
                "arguments_recorded": False,
            },
            "ollama": {
                "process_count": 1,
                "process_start_identities": [f"2 {service_suffix}"],
                "arguments_recorded": False,
            },
        },
        "checks": [
            {
                "name": name,
                "required": True,
                "passed": True,
                "observed": True,
                "requirement": "fixture",
            }
            for name in sorted(_REQUIRED_CHECKS)
        ],
        "eligible": True,
        "formal_evidence": False,
    }
