from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from experiments.phase1.carryover.analysis import (
    analyze_collection,
    render_markdown,
)
from experiments.phase1.carryover.observation import OBSERVATION_SCHEMA_VERSION
from experiments.phase1.carryover.protocol import (
    CARRYOVER_PROTOCOL_ID,
    canonical_protocol_text,
    diagnostic_orders,
    load_protocol,
    protocol_sha256,
)
from experiments.phase1.common.telemetry_jetson import parse_tegrastats_line
from experiments.phase1.run_carryover_session import (
    CARRYOVER_RUN_SCHEMA_VERSION,
    CARRYOVER_SESSION_SCHEMA_VERSION,
    _artifact_inventory,
)
from experiments.phase1.tests.carryover_fixture import passing_carryover_preflight


TEGRASTATS_SAMPLE = (
    "09-07-2026 10:00:00 RAM 3000/7607MB (lfb 4x4MB) "
    "SWAP 0/3804MB (cached 0MB) CPU [1%@729,2%@729,3%@729,4%@729,5%@729,6%@729] "
    "EMC_FREQ 2%@2133 GR3D_FREQ 7%@[306] cpu@50C gpu@50C tj@50C "
    "VDD_IN 5800mW/5800mW"
)


def observation(resident_fraction: float, monotonic_ns: int) -> dict[str, object]:
    total = 100
    return {
        "observation_schema_version": OBSERVATION_SCHEMA_VERSION,
        "observed_monotonic_ns": monotonic_ns,
        "memory": {
            "MemAvailable_bytes": 1_000_000,
            "Cached_bytes": 500_000,
            "SReclaimable_bytes": 10_000,
        },
        "whisper_model_residency": {
            "method": "non_touching_mmap_mincore",
            "file_size_bytes": 409_600,
            "page_size_bytes": 4096,
            "total_pages": total,
            "resident_pages": round(total * resident_fraction),
            "resident_fraction": round(total * resident_fraction) / total,
            "path_recorded": False,
        },
    }


def process_observation(faults: int) -> dict[str, object]:
    return {
        "observation_schema_version": OBSERVATION_SCHEMA_VERSION,
        "method": "sampled_linux_proc",
        "sample_interval_ms": 10.0,
        "sample_count": 10,
        "read_error_count": 0,
        "process_exit_observed": True,
        "user_time_s": 1.0,
        "system_time_s": 0.5,
        "minor_faults": faults,
        "major_faults": faults // 10,
        "maximum_rss_bytes": 100_000,
        "voluntary_context_switches": 3,
        "involuntary_context_switches": 2,
        "pid_recorded": False,
        "command_recorded": False,
    }


class CarryoverAnalysisTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.collection = (
            Path(self.temporary.name) / "fixture_phase1_asr_vlm_carryover_v2"
        )
        self.collection.mkdir()
        protocol = load_protocol()
        for session_index, order in enumerate(diagnostic_orders(), start=1):
            session_dir = self.collection / f"session-{session_index:02d}-attempt-01"
            session_dir.mkdir()
            (session_dir / "protocol.json").write_text(
                canonical_protocol_text(protocol), encoding="utf-8"
            )
            preflight = passing_carryover_preflight(str(session_index))
            (session_dir / "preflight.json").write_text(
                json.dumps(preflight), encoding="utf-8"
            )
            (session_dir / "ledger.jsonl").write_text("", encoding="utf-8")
            earliest = 1_000_000_000
            latest = earliest
            for unit_index, condition in enumerate(order, start=1):
                unit_dir = session_dir / "units" / f"unit-{unit_index:02d}-{condition}"
                unit_dir.mkdir(parents=True)
                references: dict[str, str] = {}
                measured_duration = {"idle": 2_000.0, "llm": 2_200.0, "vlm": 20_000.0}[
                    condition
                ]
                roles = ["primer_1", "primer_2"]
                if condition != "idle":
                    roles.append("interposer")
                roles.extend(["measured_asr", "recovery_asr"])
                for role_index, role in enumerate(roles, start=1):
                    workload = condition if role == "interposer" else "asr"
                    duration_ms = (
                        measured_duration if role == "measured_asr" else 2_000.0
                    )
                    started = latest + 1_000_000
                    if role == "measured_asr":
                        started = latest + 150_000_000_000
                    finished = started + int(duration_ms * 1_000_000)
                    latest = finished
                    run_dir = (
                        session_dir / "runs" / f"u{unit_index}-{role_index}-{role}"
                    )
                    run_dir.mkdir(parents=True)
                    references[role] = run_dir.relative_to(session_dir).as_posix()
                    run = {
                        "carryover_run_schema_version": CARRYOVER_RUN_SCHEMA_VERSION,
                        "artifact_kind": "phase1_carryover_diagnostic_invocation",
                        "diagnostic_role": role,
                        "workload": workload,
                        "valid": True,
                        "formal_evidence": False,
                        "gates": [{"name": "fixture", "passed": True}],
                        "adapter": {
                            "started_monotonic_ns": started,
                            "finished_monotonic_ns": finished,
                            "duration_ns": int(duration_ms * 1_000_000),
                        },
                        "asr_process_observation": (
                            process_observation(
                                {"idle": 100, "llm": 120, "vlm": 1_000}[condition]
                            )
                            if workload == "asr"
                            else None
                        ),
                    }
                    (run_dir / "run.json").write_text(json.dumps(run), encoding="utf-8")
                    (run_dir / "events.jsonl").write_text("", encoding="utf-8")
                resident = {"idle": 0.9, "llm": 0.85, "vlm": 0.1}[condition]
                unit = {
                    "unit_schema_version": "0.1.0",
                    "unit_index": unit_index,
                    "interposer": condition,
                    "primer_2_duration_ms": 2_000.0,
                    "timing": {
                        "target_interval_s": 150.0,
                        "observed_interval_ms": 150_010.0,
                        "start_lateness_ms": 10.0,
                        "start_lateness_max_ms": 250.0,
                        "interposer_overrun": False,
                    },
                    "observations": {
                        "before_primers": observation(0.2, earliest),
                        "after_primer_2": observation(0.9, earliest + 1),
                        "after_interposer": observation(resident, earliest + 2),
                        "after_measured_asr": observation(0.9, earliest + 3),
                    },
                    "runs": references,
                    "measured_asr_duration_ms": measured_duration,
                    "recovery_asr_duration_ms": 2_000.0,
                    "valid": True,
                    "formal_evidence": False,
                }
                (unit_dir / "unit.json").write_text(json.dumps(unit), encoding="utf-8")
            resource_path = session_dir / "resources.jsonl"
            samples = [
                parse_tegrastats_line(
                    TEGRASTATS_SAMPLE,
                    sequence=index,
                    sample_monotonic_ns=value,
                    sample_wall_time_ns=value,
                )
                for index, value in enumerate((earliest - 1, latest + 1))
            ]
            resource_path.write_text(
                "\n".join(json.dumps(sample) for sample in samples) + "\n",
                encoding="utf-8",
            )
            manifest = {
                "carryover_session_schema_version": CARRYOVER_SESSION_SCHEMA_VERSION,
                "artifact_kind": "phase1_asr_vlm_carryover_diagnostic_session",
                "status": "completed",
                "formal_evidence": False,
                "protocol_id": CARRYOVER_PROTOCOL_ID,
                "protocol_sha256": protocol_sha256(protocol),
                "interposer_order": list(order),
                "completed_units": 3,
                "preflight": {
                    "runner_commit": preflight["protocol"]["runner_commit"],
                    "protocol_commit": preflight["protocol"]["protocol_commit"],
                    "service_identity": preflight["service_identity"],
                },
            }
            manifest["artifacts"] = _artifact_inventory(session_dir)
            (session_dir / "manifest.json").write_text(
                json.dumps(manifest), encoding="utf-8"
            )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_validates_six_sessions_and_reports_descriptive_contrasts(self) -> None:
        analysis = analyze_collection(self.collection)

        self.assertEqual(analysis["validation"]["session_count"], 6)
        self.assertEqual(analysis["validation"]["unit_count"], 18)
        self.assertAlmostEqual(
            analysis["contrasts"]["vlm_vs_idle"]["duration_ratio"]["geometric_mean"],
            10.0,
        )
        self.assertAlmostEqual(
            analysis["contrasts"]["vlm_vs_idle"]["resident_fraction_difference"][
                "mean"
            ],
            -0.8,
        )
        self.assertFalse(analysis["claim_boundary"]["formal_pass_fail_emitted"])
        serialized = json.dumps(analysis)
        self.assertNotIn(str(self.collection), serialized)

    def test_markdown_preserves_claim_boundary(self) -> None:
        report = render_markdown(analyze_collection(self.collection))
        self.assertIn("not a formal G6 comparison", report)
        self.assertIn("do not emit a formal pass/fail decision", report)

    def test_rejects_formal_claim_in_a_diagnostic_run(self) -> None:
        session_dir = self.collection / "session-01-attempt-01"
        run_path = next(session_dir.rglob("run.json"))
        run = json.loads(run_path.read_text(encoding="utf-8"))
        run["formal_claim_permitted"] = True
        run_path.write_text(json.dumps(run), encoding="utf-8")
        manifest_path = session_dir / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["artifacts"] = _artifact_inventory(session_dir)
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "diagnostic invocation"):
            analyze_collection(self.collection)


if __name__ == "__main__":
    unittest.main()
