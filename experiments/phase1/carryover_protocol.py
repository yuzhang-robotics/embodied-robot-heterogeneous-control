"""Load and validate the Phase 1 ASR/VLM carryover diagnostic protocol."""

from __future__ import annotations

import hashlib
import json
from itertools import permutations
from pathlib import Path
from typing import Mapping


CARRYOVER_PROTOCOL_SCHEMA_VERSION = "0.1.0"
CARRYOVER_PROTOCOL_ID = "phase1-asr-vlm-carryover-diagnostic-v1"
CARRYOVER_PROTOCOL_STATUS = "active"
CARRYOVER_PROTOCOL_SHA256 = (
    "7b12b83d8a08699107e4776ba40a43ce86dafaceb2bffaf8d785d224301092ee"
)
CARRYOVER_SESSION_COUNT = 6
INTERPOSER_INTERVAL_S = 150.0
PRIMER_WARM_MAX_MS = 5_000.0
START_LATENESS_MAX_MS = 250.0
RESOURCE_INTERVAL_MS = 200
DEFAULT_PROTOCOL_PATH = (
    Path(__file__).resolve().parent / "diagnostic" / "phase1-asr-vlm-carryover-v1.json"
)
INTERPOSERS = ("idle", "llm", "vlm")


def diagnostic_orders() -> tuple[tuple[str, ...], ...]:
    """Return the frozen six-session Williams-complete permutation set."""

    return tuple(permutations(INTERPOSERS))


def expected_protocol() -> dict[str, object]:
    """Return the complete diagnostic contract expected by the runner."""

    return {
        "schema_version": CARRYOVER_PROTOCOL_SCHEMA_VERSION,
        "protocol_id": CARRYOVER_PROTOCOL_ID,
        "status": CARRYOVER_PROTOCOL_STATUS,
        "source_result": {
            "protocol_id": "phase1-g6-fixed-input-sync-async-v4",
            "protocol_sha256": (
                "84da36aa9b4a804ecc5692b12902321e42254f707463d1a5937e7049ffa0d054"
            ),
            "collection_id": "20260907T051448Z_phase1_formal_g6_v4",
            "decision": "closed_negative_result",
        },
        "claim_boundary": {
            "exploratory_only": True,
            "formal_evidence": False,
            "g6_v4_reopening_permitted": False,
            "phase2_authorized": False,
        },
        "safety": {
            "motion_required": False,
            "uart_access_permitted": False,
            "llama_n_gpu_layers": 10,
        },
        "design": {
            "primary_question": "asr_carryover_after_vlm_vs_duration_matched_idle",
            "interposers": list(INTERPOSERS),
            "interposer_interval_s": INTERPOSER_INTERVAL_S,
            "primer_count": 2,
            "primer_2_warm_max_ms": PRIMER_WARM_MAX_MS,
            "measured_asr_count_per_unit": 1,
            "recovery_asr_count_per_unit": 1,
            "measured_start_lateness_max_ms": START_LATENESS_MAX_MS,
            "resource_interval_ms": RESOURCE_INTERVAL_MS,
            "restart_model_services_each_session": True,
            "reprime_asr_each_unit": True,
            "sessions": [
                {
                    "session": f"session-{index:02d}",
                    "interposer_order": list(order),
                }
                for index, order in enumerate(diagnostic_orders(), start=1)
            ],
        },
        "observations": {
            "fixed_inputs": ["asr", "llm", "vlm"],
            "boundaries": [
                "before_primers",
                "after_primer_2",
                "after_interposer",
                "after_measured_asr",
            ],
            "memory": ["MemAvailable", "Cached", "SReclaimable"],
            "whisper_model_residency": "non_touching_mmap_mincore",
            "asr_process": [
                "user_time",
                "system_time",
                "maximum_rss",
                "minor_faults",
                "major_faults",
                "filesystem_input",
                "voluntary_context_switches",
                "involuntary_context_switches",
            ],
            "raw_input_recorded": False,
            "raw_model_text_recorded": False,
            "private_paths_recorded": False,
        },
        "analysis": {
            "primary_contrasts": [
                "measured_asr_duration_ratio_vlm_over_idle",
                "post_interposer_resident_fraction_difference_vlm_minus_idle",
                "measured_asr_fault_difference_vlm_minus_idle",
            ],
            "secondary_contrasts": [
                "measured_asr_duration_ratio_llm_over_idle",
                "post_interposer_resident_fraction_difference_llm_minus_idle",
                "measured_asr_fault_difference_llm_minus_idle",
            ],
            "formal_pass_fail_field_permitted": False,
        },
    }


def canonical_protocol_text(protocol: Mapping[str, object]) -> str:
    """Serialize a protocol deterministically for hashing and copying."""

    return (
        json.dumps(
            dict(protocol),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )


def protocol_sha256(protocol: Mapping[str, object]) -> str:
    return hashlib.sha256(canonical_protocol_text(protocol).encode("utf-8")).hexdigest()


def protocol_errors(protocol: Mapping[str, object]) -> list[str]:
    """Reject any schedule or claim-boundary change, not only malformed fields."""

    return (
        []
        if dict(protocol) == expected_protocol()
        else ["protocol does not exactly match the expected carryover diagnostic"]
    )


def load_protocol(path: Path | str = DEFAULT_PROTOCOL_PATH) -> dict[str, object]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("carryover diagnostic protocol root must be an object")
    errors = protocol_errors(value)
    if errors:
        raise ValueError("; ".join(errors))
    return value


def session_order(
    protocol: Mapping[str, object], session_index: int
) -> tuple[str, ...]:
    if not 1 <= session_index <= CARRYOVER_SESSION_COUNT:
        raise ValueError("session index must be from 1 to 6")
    design = protocol.get("design")
    if not isinstance(design, Mapping):
        raise ValueError("diagnostic design is missing")
    sessions = design.get("sessions")
    if not isinstance(sessions, list) or len(sessions) != CARRYOVER_SESSION_COUNT:
        raise ValueError("diagnostic session schedule is invalid")
    selected = sessions[session_index - 1]
    if not isinstance(selected, Mapping):
        raise ValueError("diagnostic session entry is invalid")
    order = selected.get("interposer_order")
    if not isinstance(order, list) or set(order) != set(INTERPOSERS):
        raise ValueError("diagnostic interposer order is invalid")
    return tuple(str(value) for value in order)


if __name__ == "__main__":
    loaded = load_protocol()
    if protocol_sha256(loaded) != CARRYOVER_PROTOCOL_SHA256:
        raise SystemExit("INVALID: frozen protocol SHA-256 mismatch")
    print("VALID")
    print(protocol_sha256(loaded))
