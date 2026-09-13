from __future__ import annotations

import copy
import json
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

from experiments.phase1.common.manifest import sha256_file
from experiments.phase2.commissioning import (
    COMMISSIONING_PROTOCOL_ID,
    COMMISSIONING_PROTOCOL_SHA256,
    DEFAULT_COMMISSIONING_PROTOCOL_PATH,
    canonical_commissioning_protocol_text,
    commissioning_conditions,
    commissioning_protocol_errors,
    commissioning_protocol_sha256,
    load_commissioning_protocol,
)
from experiments.phase2.commissioning_preflight import (
    build_commissioning_preflight,
    commissioning_preflight_errors,
)
from experiments.phase2.evidence import (
    CONTROL_CONDITION,
    PREFETCH_CONDITION,
    build_unit_evidence,
)
from experiments.phase2.reconstruct_commissioning import (
    CommissioningReconstructionError,
    reconstruct_collection,
)
from experiments.phase2.run_commissioning_session import (
    CommissioningSessionError,
    run_session,
)
from experiments.phase2.tests.test_correctness_pilot import (
    FakeSampler,
    FakeThermalMonitor,
    boundary_record,
    passing_preflight,
    prefetch_action,
    process_observation,
    resource_trace,
)


MS = 1_000_000
COLLECTION_ID = "20260914T000000Z_phase2_commissioning_v1"


def service_identity(seed: str) -> dict[str, object]:
    return {
        name: {
            "process_count": 1,
            "process_start_identity_sha256": seed * 64,
            "arguments_recorded": False,
            "pid_recorded": False,
        }
        for name in ("llama-server", "ollama")
    }


def target_preflight(seed: str) -> dict[str, object]:
    value = passing_preflight()
    value["service_identity"] = service_identity(seed)
    return value


def prior_manifest(
    services: dict[str, object],
    *,
    completed_at: str = "2026-09-14T00:00:00Z",
) -> dict[str, object]:
    return {
        "collection_id": COLLECTION_ID,
        "session_index": 1,
        "attempt": 1,
        "status": "completed",
        "completed_at": completed_at,
        "protocol_id": COMMISSIONING_PROTOCOL_ID,
        "protocol_sha256": COMMISSIONING_PROTOCOL_SHA256,
        "preflight": {"service_identity": services},
    }


class ProtocolAndPreflightTests(unittest.TestCase):
    def test_protocol_freezes_minimal_complementary_commissioning(self) -> None:
        protocol = load_commissioning_protocol()
        self.assertEqual(commissioning_protocol_errors(protocol), [])
        self.assertEqual(
            commissioning_protocol_sha256(protocol),
            COMMISSIONING_PROTOCOL_SHA256,
        )
        self.assertEqual(
            commissioning_conditions(protocol, 1),
            (PREFETCH_CONDITION, CONTROL_CONDITION),
        )
        self.assertEqual(
            commissioning_conditions(protocol, 2),
            (CONTROL_CONDITION, PREFETCH_CONDITION),
        )
        self.assertFalse(protocol["formal_evidence"])
        self.assertFalse(protocol["confirmatory_data"])

        changed = copy.deepcopy(protocol)
        changed["session_count"] = 3
        self.assertTrue(commissioning_protocol_errors(changed))
        self.assertNotEqual(
            commissioning_protocol_sha256(changed),
            COMMISSIONING_PROTOCOL_SHA256,
        )

    def test_preflight_requires_separation_and_both_service_changes(self) -> None:
        root = Path(__file__).resolve().parents[3]
        protocol = load_commissioning_protocol()
        snapshot = lambda *_args, **_kwargs: {
            "returncode": 0,
            "output": "a" * 40,
            "error_code": None,
        }
        with patch(
            "experiments.phase2.commissioning_preflight.correctness_preflight_errors",
            return_value=[],
        ):
            first = build_commissioning_preflight(
                root,
                protocol,
                collection_id=COLLECTION_ID,
                session_index=1,
                previous_session=None,
                asr_input="unused",
                vlm_input="unused",
                services_restarted=True,
                dynamic_dvfs_confirmed=True,
                target_preflight=target_preflight("1"),
                snapshotter=snapshot,
                captured_at="2026-09-14T00:00:00Z",
            )
            self.assertEqual(commissioning_preflight_errors(first), [])

            previous = prior_manifest(service_identity("1"))
            second = build_commissioning_preflight(
                root,
                protocol,
                collection_id=COLLECTION_ID,
                session_index=2,
                previous_session=previous,
                asr_input="unused",
                vlm_input="unused",
                services_restarted=True,
                dynamic_dvfs_confirmed=True,
                target_preflight=target_preflight("2"),
                snapshotter=snapshot,
                captured_at="2026-09-14T00:30:00Z",
            )
            self.assertEqual(commissioning_preflight_errors(second), [])
            self.assertEqual(second["separation"]["observed_s"], 1800.0)

            unchanged = build_commissioning_preflight(
                root,
                protocol,
                collection_id=COLLECTION_ID,
                session_index=2,
                previous_session=previous,
                asr_input="unused",
                vlm_input="unused",
                services_restarted=True,
                dynamic_dvfs_confirmed=True,
                target_preflight=target_preflight("1"),
                snapshotter=snapshot,
                captured_at="2026-09-14T00:30:00Z",
            )
            self.assertFalse(unchanged["eligible"])
            self.assertTrue(commissioning_preflight_errors(unchanged))

            early = build_commissioning_preflight(
                root,
                protocol,
                collection_id=COLLECTION_ID,
                session_index=2,
                previous_session=previous,
                asr_input="unused",
                vlm_input="unused",
                services_restarted=True,
                dynamic_dvfs_confirmed=True,
                target_preflight=target_preflight("2"),
                snapshotter=snapshot,
                captured_at="2026-09-14T00:29:59Z",
            )
            self.assertFalse(early["eligible"])
            self.assertTrue(commissioning_preflight_errors(early))


class CommissioningRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        FakeSampler.instances.clear()

    def _args(self, root: Path, session_index: int) -> Namespace:
        return Namespace(
            session_index=session_index,
            attempt=1,
            collection_id=COLLECTION_ID,
            protocol=DEFAULT_COMMISSIONING_PROTOCOL_PATH,
            output_root=root / "experiments" / "runs" / "commissioning",
            asr_input=Path("unused-asr"),
            vlm_input=Path("unused-vlm"),
            confirm_services_restarted=True,
            confirm_dynamic_dvfs=True,
            thermal_wait_timeout_s=1.0,
        )

    def test_two_sessions_use_complementary_orders_and_attempt_one(self) -> None:
        calls: list[tuple[int, str]] = []

        def preflight(_root, _protocol, **kwargs):
            index = kwargs["session_index"]
            return {
                "captured_at": f"2026-09-14T0{index - 1}:30:00Z",
                "protocol": {
                    "protocol_commit": "a" * 40,
                    "runner_commit": "b" * 40,
                },
                "prior_session": {
                    "session_index": None if index == 1 else 1,
                },
                "separation": {"observed_s": None if index == 1 else 1800.0},
                "service_identity": service_identity(str(index)),
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
                "experiments.phase2.run_commissioning_session.commissioning_preflight_errors",
                return_value=[],
            ):
                first = run_session(
                    self._args(root, 1),
                    repo_root=root,
                    preflight_builder=preflight,
                    sampler_factory=FakeSampler,
                    unit_runner=unit_runner,
                    resource_reader=lambda _path: resource_trace(),
                    thermal_monitor_factory=FakeThermalMonitor,
                    payloads_override={"asr": object(), "vlm": object()},
                    whisper_model_path=model,
                )
                second = run_session(
                    self._args(root, 2),
                    repo_root=root,
                    preflight_builder=preflight,
                    sampler_factory=FakeSampler,
                    unit_runner=unit_runner,
                    resource_reader=lambda _path: resource_trace(),
                    thermal_monitor_factory=FakeThermalMonitor,
                    payloads_override={"asr": object(), "vlm": object()},
                    whisper_model_path=model,
                )
            first_manifest = json.loads((first / "manifest.json").read_text())
            second_manifest = json.loads((second / "manifest.json").read_text())

        self.assertEqual(
            first_manifest["conditions"],
            [PREFETCH_CONDITION, CONTROL_CONDITION],
        )
        self.assertEqual(
            second_manifest["conditions"],
            [CONTROL_CONDITION, PREFETCH_CONDITION],
        )
        self.assertEqual(first_manifest["status"], "completed")
        self.assertEqual(second_manifest["status"], "completed")
        self.assertEqual(first_manifest["completed_units"], 2)
        self.assertEqual(second_manifest["completed_units"], 2)
        self.assertEqual(
            [condition for _index, condition in calls],
            [
                PREFETCH_CONDITION,
                CONTROL_CONDITION,
                CONTROL_CONDITION,
                PREFETCH_CONDITION,
            ],
        )
        self.assertEqual(FakeSampler.instances[0].stop_count, 1)
        self.assertEqual(FakeSampler.instances[1].stop_count, 1)

    def test_runner_refuses_replacement_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            args = self._args(Path(temporary), 1)
            args.attempt = 2
            with self.assertRaisesRegex(CommissioningSessionError, "attempt 1"):
                run_session(args, repo_root=temporary)


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _write_jsonl(path: Path, values: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(
            json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n"
            for value in values
        ),
        encoding="utf-8",
    )


def _event(run_id: str) -> dict[str, object]:
    return {
        "phase2_event_schema_version": "0.1.0",
        "run_id": run_id,
        "seq": 0,
        "event": "fixture.complete",
        "component": "fixture",
        "status": "ok",
        "monotonic_ns": 100 * MS,
        "wall_time_ns": 1000 * MS,
        "details": {},
    }


def _evidence(condition: str) -> dict[str, object]:
    if condition == CONTROL_CONDITION:
        return build_unit_evidence(
            condition=condition,
            common_post_vlm_observation=boundary_record("post_vlm", 90, 100),
            verified_post_action_observation=None,
            prefetch_action=None,
            prefetch_process_observation=None,
            asr_started_monotonic_ns=110 * MS,
            asr_result_available_monotonic_ns=300 * MS,
            asr_process_observation=process_observation(),
            resource_samples=resource_trace(),
            maximum_resource_gap_ns=400 * MS,
        )
    return build_unit_evidence(
        condition=condition,
        common_post_vlm_observation=boundary_record("post_vlm", 490, 500),
        verified_post_action_observation=boundary_record("post_action", 541, 550),
        prefetch_action=prefetch_action(),
        prefetch_process_observation=process_observation(),
        asr_started_monotonic_ns=560 * MS,
        asr_result_available_monotonic_ns=700 * MS,
        asr_process_observation=process_observation(),
        resource_samples=resource_trace(),
        maximum_resource_gap_ns=400 * MS,
    )


def _unit_observations(condition: str) -> dict[str, object]:
    if condition == CONTROL_CONDITION:
        return {
            "pre_unit": boundary_record("pre_unit", 10, 20),
            "post_primer": boundary_record("post_primer", 50, 60),
            "post_vlm": boundary_record("post_vlm", 90, 100),
            "post_asr": boundary_record("post_asr", 301, 310),
        }
    return {
        "pre_unit": boundary_record("pre_unit", 400, 410),
        "post_primer": boundary_record("post_primer", 450, 460),
        "post_vlm": boundary_record("post_vlm", 490, 500),
        "post_action": boundary_record("post_action", 541, 550),
        "post_asr": boundary_record("post_asr", 701, 710),
    }


def _artifact_inventory(session_dir: Path) -> dict[str, object]:
    files = sorted(
        path
        for path in session_dir.rglob("*")
        if path.is_file()
        and path.name != "manifest.json"
        and not path.name.endswith(".tmp")
    )
    return {
        "count": len(files),
        "files": [
            {
                "relative_name": path.relative_to(session_dir).as_posix(),
                "size_bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
            for path in files
        ],
    }


def _refresh_inventory(session_dir: Path) -> None:
    manifest_path = session_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["artifacts"] = _artifact_inventory(session_dir)
    _write_json(manifest_path, manifest)


def _create_session(
    collection: Path,
    *,
    session_index: int,
    services: dict[str, object],
) -> None:
    protocol = load_commissioning_protocol()
    conditions = commissioning_conditions(protocol, session_index)
    session = collection / f"session-{session_index:02d}-attempt-01"
    session.mkdir(parents=True)
    (session / "protocol.json").write_text(
        canonical_commissioning_protocol_text(protocol), encoding="utf-8"
    )
    separation = None if session_index == 1 else 1800.0
    preflight = {
        "captured_at": f"2026-09-14T0{session_index - 1}:30:00Z",
        "protocol": {
            "protocol_commit": "a" * 40,
            "runner_commit": "b" * 40,
        },
        "prior_session": {
            "session_index": None if session_index == 1 else 1,
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
                "at": "2026-09-14T00:00:00Z",
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
                [_event(f"20260914T000000Z_phase2_pilot_{workload}_{ordinal:03d}")],
            )
            invocations[role] = run_dir.relative_to(session).as_posix()
        unit = {
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
        }
        _write_json(unit_dir / "unit.json", unit)
        _write_json(unit_dir / "evidence.json", _evidence(condition))
        _write_jsonl(
            unit_dir / "events.jsonl",
            [_event(f"20260914T000000Z_phase2_pilot_unit_{unit_index:03d}")],
        )
        ledger.append(
            {
                "event": "unit_completed",
                "at": "2026-09-14T00:10:00Z",
                "unit_index": unit_index,
                "condition": condition,
                "status": "completed",
            }
        )
    _write_jsonl(session / "ledger.jsonl", ledger)
    manifest = {
        "commissioning_session_schema_version": "0.1.0",
        "artifact_kind": "phase2_commissioning_session",
        "collection_id": COLLECTION_ID,
        "session_id": session.name,
        "session_index": session_index,
        "attempt": 1,
        "protocol_id": COMMISSIONING_PROTOCOL_ID,
        "protocol_sha256": COMMISSIONING_PROTOCOL_SHA256,
        "conditions": list(conditions),
        "status": "completed",
        "failure_class": None,
        "failure_code": None,
        "created_at": "2026-09-14T00:00:00Z",
        "completed_at": "2026-09-14T00:10:00Z",
        "formal_evidence": False,
        "confirmatory_data": False,
        "application_slice_authorized": False,
        "development_injection": False,
        "preflight": {
            "captured_at": preflight["captured_at"],
            "protocol_commit": "a" * 40,
            "runner_commit": "b" * 40,
            "service_identity": services,
            "prior_session_index": None if session_index == 1 else 1,
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
        "completed_units": 2,
        "artifacts": {},
    }
    manifest["artifacts"] = _artifact_inventory(session)
    _write_json(session / "manifest.json", manifest)


def _create_collection(root: Path) -> Path:
    collection = root / COLLECTION_ID
    collection.mkdir()
    _create_session(collection, session_index=1, services=service_identity("1"))
    _create_session(collection, session_index=2, services=service_identity("2"))
    return collection


class ReconstructionTests(unittest.TestCase):
    def test_reconstruction_is_deterministic_nonformal_and_pair_complete(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            collection = _create_collection(Path(temporary))
            with patch(
                "experiments.phase2.reconstruct_commissioning.commissioning_preflight_errors",
                return_value=[],
            ):
                first = reconstruct_collection(collection)
                second = reconstruct_collection(collection)

        self.assertEqual(first, second)
        self.assertTrue(first["commissioning_valid"])
        self.assertTrue(first["evidence_path_executable"])
        self.assertEqual(first["session_count"], 2)
        self.assertEqual(first["pair_count"], 2)
        self.assertFalse(first["formal_evidence"])
        self.assertFalse(first["confirmatory_data"])
        self.assertFalse(first["confirmatory_collection_authorized"])
        self.assertFalse(first["operational_benefit_interpretation_permitted"])
        self.assertEqual(
            [pair["condition_order"] for pair in first["pairs"]],
            [
                [PREFETCH_CONDITION, CONTROL_CONDITION],
                [CONTROL_CONDITION, PREFETCH_CONDITION],
            ],
        )

    def test_reconstruction_rejects_artifact_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            collection = _create_collection(Path(temporary))
            evidence = (
                collection
                / "session-01-attempt-01"
                / "units"
                / "unit-01-prefetch"
                / "evidence.json"
            )
            original = evidence.read_text(encoding="utf-8")
            evidence.write_text("[" + original[1:], encoding="utf-8")
            with (
                patch(
                    "experiments.phase2.reconstruct_commissioning.commissioning_preflight_errors",
                    return_value=[],
                ),
                self.assertRaisesRegex(CommissioningReconstructionError, "SHA-256"),
            ):
                reconstruct_collection(collection)

    def test_reconstruction_rejects_missing_session(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            collection = Path(temporary) / COLLECTION_ID
            collection.mkdir()
            with self.assertRaisesRegex(
                CommissioningReconstructionError, "exactly two sessions"
            ):
                reconstruct_collection(collection)

    def test_reconstruction_rejects_rehashed_ledger_reordering(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            collection = _create_collection(Path(temporary))
            session = collection / "session-01-attempt-01"
            ledger_path = session / "ledger.jsonl"
            ledger = [
                json.loads(line)
                for line in ledger_path.read_text(encoding="utf-8").splitlines()
            ]
            ledger[0], ledger[1] = ledger[1], ledger[0]
            _write_jsonl(ledger_path, ledger)
            _refresh_inventory(session)
            with (
                patch(
                    "experiments.phase2.reconstruct_commissioning.commissioning_preflight_errors",
                    return_value=[],
                ),
                self.assertRaisesRegex(CommissioningReconstructionError, "ledger"),
            ):
                reconstruct_collection(collection)

    def test_reconstruction_rejects_rehashed_invocation_gate_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            collection = _create_collection(Path(temporary))
            session = collection / "session-01-attempt-01"
            run_path = next((session / "runs").glob("*/run.json"))
            run = json.loads(run_path.read_text(encoding="utf-8"))
            run["gates"][0]["passed"] = False
            run["valid"] = False
            _write_json(run_path, run)
            _refresh_inventory(session)
            with (
                patch(
                    "experiments.phase2.reconstruct_commissioning.commissioning_preflight_errors",
                    return_value=[],
                ),
                self.assertRaisesRegex(
                    CommissioningReconstructionError, "invalid or exceeds"
                ),
            ):
                reconstruct_collection(collection)


if __name__ == "__main__":
    unittest.main()
