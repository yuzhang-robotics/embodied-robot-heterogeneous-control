from __future__ import annotations

import copy
import json
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

from experiments.phase2.analyze_confirmatory import (
    ConfirmatoryAnalysisError,
    _pair_summary,
    analyze_confirmatory_collection,
    classify_estimands,
    paired_hierarchical_bootstrap,
)
from experiments.phase2.confirmatory import (
    BOOTSTRAP_SEED,
    COMMISSIONING_RESULT_SHA256,
    CONFIRMATORY_PROTOCOL_ID,
    CONFIRMATORY_PROTOCOL_SHA256,
    DEFAULT_CONFIRMATORY_PROTOCOL_PATH,
    canonical_confirmatory_protocol_text,
    confirmatory_conditions,
    confirmatory_protocol_errors,
    confirmatory_protocol_sha256,
    load_confirmatory_protocol,
)
from experiments.phase2.confirmatory_preflight import (
    build_confirmatory_preflight,
    confirmatory_preflight_errors,
)
from experiments.phase2.evidence import (
    CONTROL_CONDITION,
    PREFETCH_CONDITION,
    validate_unit_evidence,
)
from experiments.phase2.run_confirmatory_session import (
    ConfirmatorySessionError,
    run_session,
)
from experiments.phase2.tests.test_commissioning import (
    _artifact_inventory,
    _event,
    _evidence,
    _refresh_inventory,
    _unit_observations,
    _write_json,
    _write_jsonl,
    service_identity,
    target_preflight,
)
from experiments.phase2.tests.test_correctness_pilot import (
    FakeSampler,
    FakeThermalMonitor,
    process_observation,
    resource_trace,
)


MS = 1_000_000
COLLECTION_ID = "20260921T000000Z_phase2_confirmatory_v1"


def prior_manifest(
    session_index: int,
    services: dict[str, object],
    *,
    completed_at: str,
) -> dict[str, object]:
    return {
        "collection_id": COLLECTION_ID,
        "session_index": session_index,
        "attempt": 1,
        "status": "completed",
        "completed_at": completed_at,
        "protocol_id": CONFIRMATORY_PROTOCOL_ID,
        "protocol_sha256": CONFIRMATORY_PROTOCOL_SHA256,
        "preflight": {"service_identity": services},
    }


class ProtocolAndPreflightTests(unittest.TestCase):
    def test_protocol_freezes_six_sessions_four_pairs_and_analysis(self) -> None:
        protocol = load_confirmatory_protocol()
        self.assertEqual(confirmatory_protocol_errors(protocol), [])
        self.assertEqual(
            confirmatory_protocol_sha256(protocol), CONFIRMATORY_PROTOCOL_SHA256
        )
        self.assertEqual(len(confirmatory_conditions(protocol, 1)), 8)
        self.assertEqual(
            confirmatory_conditions(protocol, 1)[:2],
            (PREFETCH_CONDITION, CONTROL_CONDITION),
        )
        self.assertEqual(
            confirmatory_conditions(protocol, 2)[:2],
            (CONTROL_CONDITION, PREFETCH_CONDITION),
        )
        self.assertEqual(protocol["analysis"]["bootstrap_seed"], BOOTSTRAP_SEED)
        self.assertEqual(protocol["parameters"]["prefetch_timeout_s"], 20.0)
        self.assertFalse(protocol["application_slice_authorized"])

        changed = copy.deepcopy(protocol)
        changed["parameters"]["prefetch_timeout_s"] = 21.0
        self.assertTrue(confirmatory_protocol_errors(changed))
        self.assertNotEqual(
            confirmatory_protocol_sha256(changed), CONFIRMATORY_PROTOCOL_SHA256
        )

    def test_preflight_requires_exact_chain_separation_and_service_changes(
        self,
    ) -> None:
        root = Path(__file__).resolve().parents[3]
        protocol = load_confirmatory_protocol()
        snapshot = lambda *_args, **_kwargs: {
            "returncode": 0,
            "output": "a" * 40,
            "error_code": None,
        }
        with patch(
            "experiments.phase2.confirmatory_preflight.correctness_preflight_errors",
            return_value=[],
        ):
            first = build_confirmatory_preflight(
                root,
                protocol,
                collection_id=COLLECTION_ID,
                session_index=1,
                previous_session=None,
                asr_input="unused",
                vlm_input="unused",
                services_restarted=True,
                dynamic_dvfs_confirmed=True,
                collection_authorized=True,
                target_preflight=target_preflight("1"),
                snapshotter=snapshot,
                captured_at="2026-09-21T00:00:00Z",
            )
            self.assertEqual(confirmatory_preflight_errors(first), [])
            self.assertEqual(
                first["commissioning_result"]["sha256"],
                COMMISSIONING_RESULT_SHA256,
            )

            unauthorized = build_confirmatory_preflight(
                root,
                protocol,
                collection_id=COLLECTION_ID,
                session_index=1,
                previous_session=None,
                asr_input="unused",
                vlm_input="unused",
                services_restarted=True,
                dynamic_dvfs_confirmed=True,
                collection_authorized=False,
                target_preflight=target_preflight("1"),
                snapshotter=snapshot,
                captured_at="2026-09-21T00:00:00Z",
            )
            self.assertFalse(unauthorized["eligible"])
            self.assertTrue(confirmatory_preflight_errors(unauthorized))

            previous = prior_manifest(
                1,
                service_identity("1"),
                completed_at="2026-09-21T00:00:00Z",
            )
            second = build_confirmatory_preflight(
                root,
                protocol,
                collection_id=COLLECTION_ID,
                session_index=2,
                previous_session=previous,
                asr_input="unused",
                vlm_input="unused",
                services_restarted=True,
                dynamic_dvfs_confirmed=True,
                collection_authorized=True,
                target_preflight=target_preflight("2"),
                snapshotter=snapshot,
                captured_at="2026-09-21T00:30:00Z",
            )
            self.assertEqual(confirmatory_preflight_errors(second), [])
            self.assertEqual(second["separation"]["observed_s"], 1800.0)

            unchanged = build_confirmatory_preflight(
                root,
                protocol,
                collection_id=COLLECTION_ID,
                session_index=2,
                previous_session=previous,
                asr_input="unused",
                vlm_input="unused",
                services_restarted=True,
                dynamic_dvfs_confirmed=True,
                collection_authorized=True,
                target_preflight=target_preflight("1"),
                snapshotter=snapshot,
                captured_at="2026-09-21T00:30:00Z",
            )
            self.assertFalse(unchanged["eligible"])
            self.assertTrue(confirmatory_preflight_errors(unchanged))

            early = build_confirmatory_preflight(
                root,
                protocol,
                collection_id=COLLECTION_ID,
                session_index=2,
                previous_session=previous,
                asr_input="unused",
                vlm_input="unused",
                services_restarted=True,
                dynamic_dvfs_confirmed=True,
                collection_authorized=True,
                target_preflight=target_preflight("2"),
                snapshotter=snapshot,
                captured_at="2026-09-21T00:29:59Z",
            )
            self.assertFalse(early["eligible"])
            self.assertTrue(confirmatory_preflight_errors(early))


class ConfirmatoryRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        FakeSampler.instances.clear()

    def _args(self, root: Path, session_index: int = 1) -> Namespace:
        return Namespace(
            session_index=session_index,
            attempt=1,
            collection_id=COLLECTION_ID,
            protocol=DEFAULT_CONFIRMATORY_PROTOCOL_PATH,
            output_root=root / "experiments" / "runs" / "confirmatory",
            asr_input=Path("unused-asr"),
            vlm_input=Path("unused-vlm"),
            confirm_services_restarted=True,
            confirm_dynamic_dvfs=True,
            confirm_collection_authorized=True,
            thermal_wait_timeout_s=1.0,
        )

    def test_runner_emits_exact_eight_unit_schedule(self) -> None:
        calls: list[tuple[int, str]] = []

        def preflight(_root, _protocol, **kwargs):
            return {
                "captured_at": "2026-09-21T00:00:00Z",
                "protocol": {
                    "protocol_commit": "a" * 40,
                    "runner_commit": "b" * 40,
                },
                "commissioning_result": {
                    "sha256": COMMISSIONING_RESULT_SHA256,
                    "result_commit": "c" * 40,
                },
                "prior_session": {"session_index": None},
                "separation": {"observed_s": None},
                "service_identity": service_identity("1"),
                "target_preflight": {"whisper_file": {"size_bytes": 8}},
            }

        def unit_runner(_session_dir, **kwargs):
            calls.append((kwargs["unit_index"], kwargs["condition"]))
            return {"status": "completed"}, kwargs["ordinal_start"] + 5

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            model = root / "model.bin"
            model.write_bytes(b"12345678")
            with patch(
                "experiments.phase2.run_confirmatory_session.confirmatory_preflight_errors",
                return_value=[],
            ):
                session = run_session(
                    self._args(root),
                    repo_root=root,
                    preflight_builder=preflight,
                    sampler_factory=FakeSampler,
                    unit_runner=unit_runner,
                    resource_reader=lambda _path: resource_trace(),
                    thermal_monitor_factory=FakeThermalMonitor,
                    payloads_override={"asr": object(), "vlm": object()},
                    whisper_model_path=model,
                )
            manifest = json.loads((session / "manifest.json").read_text())

        self.assertEqual(
            manifest["conditions"],
            list(confirmatory_conditions(load_confirmatory_protocol(), 1)),
        )
        self.assertEqual(manifest["completed_units"], 8)
        self.assertTrue(manifest["formal_evidence"])
        self.assertTrue(manifest["confirmatory_data"])
        self.assertTrue(manifest["development_injection"])
        self.assertEqual(len(calls), 8)
        self.assertEqual(FakeSampler.instances[0].stop_count, 1)

    def test_runner_refuses_replacement_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            args = self._args(Path(temporary))
            args.attempt = 2
            with self.assertRaisesRegex(ConfirmatorySessionError, "attempt 1"):
                run_session(args, repo_root=temporary)


def _create_session(
    collection: Path,
    *,
    session_index: int,
    services: dict[str, object],
) -> None:
    protocol = load_confirmatory_protocol()
    conditions = confirmatory_conditions(protocol, session_index)
    session = collection / f"session-{session_index:02d}-attempt-01"
    session.mkdir(parents=True)
    (session / "protocol.json").write_text(
        canonical_confirmatory_protocol_text(protocol), encoding="utf-8"
    )
    separation = None if session_index == 1 else 1800.0
    preflight = {
        "captured_at": f"2026-09-{21 + session_index:02d}T00:30:00Z",
        "protocol": {
            "protocol_commit": "a" * 40,
            "runner_commit": "b" * 40,
        },
        "commissioning_result": {
            "sha256": COMMISSIONING_RESULT_SHA256,
            "result_commit": "c" * 40,
        },
        "prior_session": {
            "session_index": None if session_index == 1 else session_index - 1,
        },
        "separation": {"observed_s": separation},
        "service_identity": services,
    }
    _write_json(session / "preflight.json", preflight)
    resources = resource_trace()
    _write_jsonl(session / "resources.jsonl", resources)

    ledger: list[dict[str, object]] = []
    ordinal = 0
    for unit_index, condition in enumerate(conditions, start=1):
        label = "control" if condition == CONTROL_CONDITION else "prefetch"
        unit_dir = session / "units" / f"unit-{unit_index:02d}-{label}"
        unit_dir.mkdir(parents=True)
        ledger.append(
            {
                "event": "unit_started",
                "at": "2026-09-21T00:00:00Z",
                "unit_index": unit_index,
                "condition": condition,
            }
        )
        invocations: dict[str, str] = {}
        for role in (
            "primer_1",
            "primer_2",
            "vlm",
            "measured_asr",
            "recovery_asr",
        ):
            ordinal += 1
            workload = "vlm" if role == "vlm" else "asr"
            run_dir = session / "runs" / f"{ordinal:03d}-{workload}-{label}-{role}"
            run_dir.mkdir(parents=True)
            gates = [{"name": "lifecycle", "passed": True}]
            if workload == "vlm":
                gates = [
                    {"name": name, "passed": True}
                    for name in (
                        "child_process_reaped",
                        "model_unload_claim_bounded",
                        "residency_contract_verified",
                    )
                ]
            _write_json(
                run_dir / "run.json",
                {
                    "artifact_kind": "phase2_correctness_pilot_invocation",
                    "condition": condition,
                    "pilot_role": role,
                    "workload": workload,
                    "status": "completed",
                    "valid": True,
                    "gates": gates,
                    "process_observation": process_observation(),
                    "formal_evidence": False,
                    "application_slice_authorized": False,
                    "raw_input_recorded": False,
                    "raw_output_recorded": False,
                },
            )
            _write_jsonl(
                run_dir / "events.jsonl",
                [_event(f"20260921T000000Z_phase2_confirmatory_{ordinal:03d}")],
            )
            invocations[role] = run_dir.relative_to(session).as_posix()
        _write_json(
            unit_dir / "unit.json",
            {
                "correctness_unit_schema_version": "0.1.0",
                "artifact_kind": "phase2_correctness_pilot_unit",
                "unit_index": unit_index,
                "condition": condition,
                "status": "completed",
                "completed_steps": protocol["unit_sequence"],
                "observations": _unit_observations(condition),
                "invocations": invocations,
                "primer_2_duration_ms": 10.0,
                "measured_asr_duration_ms": 100.0,
                "recovery_asr_duration_ms": 10.0,
                "evidence_ref": "evidence.json",
                "probe": {
                    "implementation": "independent_thread",
                    "joined": True,
                    "tick_count": 10,
                    "skipped_releases": 0,
                    "deadline_miss_count": 0,
                    "max_lateness_ns": 1,
                    "max_gap_ns": 100 * MS,
                    "error_code": None,
                    "responsiveness_reference_ms": 300.0,
                    "responsiveness_reference_met": True,
                },
                "formal_evidence": False,
                "application_slice_authorized": False,
            },
        )
        _write_json(unit_dir / "evidence.json", _evidence(condition))
        _write_jsonl(
            unit_dir / "events.jsonl",
            [_event(f"20260921T000000Z_phase2_confirmatory_unit_{unit_index:03d}")],
        )
        ledger.append(
            {
                "event": "unit_completed",
                "at": "2026-09-21T00:10:00Z",
                "unit_index": unit_index,
                "condition": condition,
                "status": "completed",
            }
        )
    _write_jsonl(session / "ledger.jsonl", ledger)
    manifest = {
        "confirmatory_session_schema_version": "0.1.0",
        "artifact_kind": "phase2_confirmatory_session",
        "collection_id": COLLECTION_ID,
        "session_id": session.name,
        "session_index": session_index,
        "attempt": 1,
        "protocol_id": CONFIRMATORY_PROTOCOL_ID,
        "protocol_sha256": CONFIRMATORY_PROTOCOL_SHA256,
        "conditions": list(conditions),
        "status": "completed",
        "failure_class": None,
        "failure_code": None,
        "created_at": "2026-09-21T00:00:00Z",
        "completed_at": "2026-09-21T00:10:00Z",
        "formal_evidence": True,
        "confirmatory_data": True,
        "application_slice_authorized": False,
        "development_injection": False,
        "preflight": {
            "captured_at": preflight["captured_at"],
            "protocol_commit": "a" * 40,
            "runner_commit": "b" * 40,
            "commissioning_result_sha256": COMMISSIONING_RESULT_SHA256,
            "commissioning_result_commit": "c" * 40,
            "service_identity": services,
            "prior_session_index": None if session_index == 1 else session_index - 1,
            "separation_s": separation,
        },
        "thermal": {
            "readiness": {
                "maximum_tj_c": 55.0,
                "consecutive_samples": 10,
                "first_sequence": 0,
                "last_sequence": 9,
                "observed_tj_c": [50.0] * 10,
            },
            "stop_tj_c": 85.0,
            "stop_requested": False,
        },
        "resource_sampler_report": {
            "sample_count": len(resources),
            "parse_error_count": 0,
            "reader_joined": True,
            "successful": True,
        },
        "completed_units": 8,
        "artifacts": {},
    }
    manifest["artifacts"] = _artifact_inventory(session)
    _write_json(session / "manifest.json", manifest)


def _create_collection(root: Path) -> Path:
    collection = root / COLLECTION_ID
    collection.mkdir()
    for session_index in range(1, 7):
        _create_session(
            collection,
            session_index=session_index,
            services=service_identity(str(session_index)),
        )
    return collection


class AnalysisTests(unittest.TestCase):
    def test_bootstrap_is_deterministic_and_respects_frozen_shape(self) -> None:
        log_ratios = [[-0.1] * 4 for _ in range(6)]
        residency = [[0.5] * 4 for _ in range(6)]
        faults = [[-2.0] * 4 for _ in range(6)]
        first = paired_hierarchical_bootstrap(
            log_ratios, residency, faults, resamples=200, seed=7
        )
        second = paired_hierarchical_bootstrap(
            log_ratios, residency, faults, resamples=200, seed=7
        )
        self.assertEqual(first, second)
        with self.assertRaisesRegex(ValueError, "6-by-4"):
            paired_hierarchical_bootstrap(
                [[-0.1] * 3 for _ in range(6)],
                residency,
                faults,
                resamples=10,
            )

    def test_positive_negative_and_inconclusive_decisions_are_layered(self) -> None:
        def values(op, residency, faults):
            return {
                "operational_benefit": {"ci95": {"low": op[0], "high": op[1]}},
                "residency_difference": {
                    "ci95": {"low": residency[0], "high": residency[1]}
                },
                "major_fault_difference": {
                    "ci95": {"low": faults[0], "high": faults[1]}
                },
            }

        positive = classify_estimands(
            values((0.8, 0.9), (0.1, 0.2), (-3.0, -1.0)),
            responsiveness_p95_ms=100.0,
        )
        negative = classify_estimands(
            values((1.1, 1.2), (-0.2, -0.1), (1.0, 2.0)),
            responsiveness_p95_ms=400.0,
        )
        inconclusive = classify_estimands(
            values((0.9, 1.1), (-0.1, 0.2), (-1.0, 1.0)),
            responsiveness_p95_ms=100.0,
        )
        self.assertEqual(positive["operational_benefit_decision"], "positive")
        self.assertTrue(positive["residency_mechanism_supported"])
        self.assertEqual(negative["operational_benefit_decision"], "negative")
        self.assertEqual(negative["residency_mechanism_decision"], "negative")
        self.assertFalse(negative["responsiveness_preserved"])
        self.assertEqual(inconclusive["operational_benefit_decision"], "inconclusive")
        self.assertEqual(inconclusive["residency_mechanism_decision"], "inconclusive")

    def test_timeout_is_retained_as_a_pair_outcome(self) -> None:
        control = _evidence(CONTROL_CONDITION)
        treatment = _evidence(PREFETCH_CONDITION)
        action = treatment["prefetch_action"]
        action.update(
            {
                "status": "timeout",
                "read_completed_monotonic_ns": None,
                "bytes_read": None,
                "chunk_count": None,
                "child_exit_code": -15,
                "child_protocol_complete": False,
                "terminate_requested": True,
                "error_code": "timeout",
            }
        )
        validate_unit_evidence(treatment)
        unit = {
            "primer_2_duration_ms": 10.0,
            "recovery_asr_duration_ms": 10.0,
            "probe": {
                "max_gap_ns": 100 * MS,
                "max_lateness_ns": 1,
                "deadline_miss_count": 0,
                "skipped_releases": 0,
            },
        }
        pair = _pair_summary(
            1,
            1,
            [CONTROL_CONDITION, PREFETCH_CONDITION],
            [(unit, control), (unit, treatment)],
        )
        self.assertEqual(pair["treatment"]["prefetch_status"], "timeout")
        self.assertIsNone(pair["treatment"]["prefetch_bytes_read"])

    def test_complete_collection_is_valid_private_free_and_non_application(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            collection = _create_collection(Path(temporary))
            with patch(
                "experiments.phase2.analyze_confirmatory.confirmatory_preflight_errors",
                return_value=[],
            ):
                result = analyze_confirmatory_collection(collection)
        self.assertTrue(result["study_valid"])
        self.assertEqual(result["session_count"], 6)
        self.assertEqual(result["pair_count"], 24)
        self.assertEqual(result["unit_count"], 48)
        self.assertFalse(result["application_slice_authorized"])
        self.assertFalse(result["private_paths_recorded"])
        self.assertFalse(result["replacement_applied"])

    def test_analysis_rejects_artifact_ledger_and_gate_tampering(self) -> None:
        cases = ("artifact", "ledger", "gate")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                collection = _create_collection(Path(temporary))
                session = collection / "session-01-attempt-01"
                if case == "artifact":
                    evidence = next((session / "units").glob("*/evidence.json"))
                    original = evidence.read_text(encoding="utf-8")
                    evidence.write_text("[" + original[1:], encoding="utf-8")
                    pattern = "SHA-256"
                elif case == "ledger":
                    ledger_path = session / "ledger.jsonl"
                    ledger = [
                        json.loads(line)
                        for line in ledger_path.read_text(encoding="utf-8").splitlines()
                    ]
                    ledger[0], ledger[1] = ledger[1], ledger[0]
                    _write_jsonl(ledger_path, ledger)
                    _refresh_inventory(session)
                    pattern = "ledger"
                else:
                    run_path = next((session / "runs").glob("*/run.json"))
                    run = json.loads(run_path.read_text(encoding="utf-8"))
                    run["gates"][0]["passed"] = False
                    run["valid"] = False
                    _write_json(run_path, run)
                    _refresh_inventory(session)
                    pattern = "invalid or exceeds"
                with (
                    patch(
                        "experiments.phase2.analyze_confirmatory.confirmatory_preflight_errors",
                        return_value=[],
                    ),
                    self.assertRaisesRegex(ConfirmatoryAnalysisError, pattern),
                ):
                    analyze_confirmatory_collection(collection)

    def test_analysis_rejects_missing_or_replacement_session(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            collection = Path(temporary) / COLLECTION_ID
            collection.mkdir()
            with self.assertRaisesRegex(ConfirmatoryAnalysisError, "exactly six"):
                analyze_confirmatory_collection(collection)


if __name__ == "__main__":
    unittest.main()
