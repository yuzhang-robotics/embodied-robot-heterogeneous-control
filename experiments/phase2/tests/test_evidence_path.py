from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from experiments.phase1.common.telemetry_jetson import parse_tegrastats_line
from experiments.phase2.energy import (
    EnergyIntegrationError,
    integrate_vdd_in_window,
    validate_energy_window_record,
)
from experiments.phase2.evidence import (
    CONTROL_CONDITION,
    PREFETCH_CONDITION,
    UnitEvidenceError,
    build_unit_evidence,
    run_injected_evidence_path,
    validate_unit_evidence,
)
from experiments.phase2.observations import (
    BOUNDARY_OBSERVATION_SCHEMA_VERSION,
    ObservationValidationError,
    capture_boundary_observation,
    validate_boundary_observation,
    validate_process_observation,
)
from experiments.phase2.privacy import (
    PrivacyBoundaryError,
    validate_privacy_boundary,
)
from experiments.phase2.residency.prefetch import PREFETCH_ACTION_SCHEMA_VERSION


MS = 1_000_000


def resource_sample(
    sequence: int,
    timestamp_ms: int,
    power_mw: int,
    *,
    rail: str = "VDD_IN",
) -> dict:
    line = (
        "09-12-2026 10:00:00 RAM 3000/7607MB (lfb 4x4MB) "
        "SWAP 0/3804MB (cached 0MB) CPU [1%@729,2%@729] "
        "EMC_FREQ 2%@2133 GR3D_FREQ 7%@[306] cpu@50C gpu@51C "
        f"{rail} {power_mw}mW/{power_mw}mW"
    )
    return parse_tegrastats_line(
        line,
        sequence=sequence,
        sample_monotonic_ns=timestamp_ms * MS,
        sample_wall_time_ns=(1_000 + timestamp_ms) * MS,
    )


def resource_trace() -> list[dict]:
    return [
        resource_sample(0, 50, 1_000),
        resource_sample(1, 150, 2_000),
        resource_sample(2, 250, 3_000),
        resource_sample(3, 350, 4_000),
    ]


def boundary_record(boundary: str, started_ms: int, finished_ms: int) -> dict:
    return {
        "boundary_observation_schema_version": (BOUNDARY_OBSERVATION_SCHEMA_VERSION),
        "boundary": boundary,
        "observation_started_monotonic_ns": started_ms * MS,
        "observation_finished_monotonic_ns": finished_ms * MS,
        "expected_size_bytes": 8,
        "mem_available_bytes": 1_000,
        "cached_bytes": 2_000,
        "sreclaimable_bytes": 300,
        "residency_method": "non_touching_mmap_mincore",
        "file_size_bytes": 8,
        "page_size_bytes": 4,
        "total_pages": 2,
        "resident_pages": 1,
        "resident_fraction": 0.5,
        "path_recorded": False,
        "contents_recorded": False,
    }


def process_observation() -> dict:
    return {
        "observation_schema_version": "0.2.0",
        "method": "sampled_linux_proc",
        "sample_interval_ms": 10.0,
        "sample_count": 4,
        "read_error_count": 0,
        "process_exit_observed": True,
        "user_time_s": 0.1,
        "system_time_s": 0.02,
        "minor_faults": 3,
        "major_faults": 1,
        "maximum_rss_bytes": 4_096,
        "voluntary_context_switches": 2,
        "involuntary_context_switches": 1,
        "pid_recorded": False,
        "command_recorded": False,
    }


def prefetch_action(*, timeout: bool = False) -> dict:
    if timeout:
        return {
            "prefetch_action_schema_version": PREFETCH_ACTION_SCHEMA_VERSION,
            "status": "timeout",
            "expected_size_bytes": 8,
            "buffer_size_bytes": 4,
            "timeout_ms": 20.0,
            "action_started_monotonic_ns": 110 * MS,
            "child_started_monotonic_ns": None,
            "read_completed_monotonic_ns": None,
            "child_finished_monotonic_ns": None,
            "action_finished_monotonic_ns": 140 * MS,
            "bytes_read": None,
            "chunk_count": None,
            "child_exit_code": -15,
            "child_protocol_complete": False,
            "terminate_requested": True,
            "kill_requested": False,
            "child_joined": True,
            "path_recorded": False,
            "contents_recorded": False,
            "pid_recorded": False,
            "error_code": "timeout",
        }
    return {
        "prefetch_action_schema_version": PREFETCH_ACTION_SCHEMA_VERSION,
        "status": "ok",
        "expected_size_bytes": 8,
        "buffer_size_bytes": 4,
        "timeout_ms": 20.0,
        "action_started_monotonic_ns": 110 * MS,
        "child_started_monotonic_ns": 111 * MS,
        "read_completed_monotonic_ns": 130 * MS,
        "child_finished_monotonic_ns": 131 * MS,
        "action_finished_monotonic_ns": 140 * MS,
        "bytes_read": 8,
        "chunk_count": 2,
        "child_exit_code": 0,
        "child_protocol_complete": True,
        "terminate_requested": False,
        "kill_requested": False,
        "child_joined": True,
        "path_recorded": False,
        "contents_recorded": False,
        "pid_recorded": False,
        "error_code": None,
    }


class ObservationContractTests(unittest.TestCase):
    def test_capture_normalizes_boundary_without_private_identity(self) -> None:
        ticks = iter((90 * MS, 100 * MS))
        record = capture_boundary_observation(
            Path("private-model-name.bin"),
            boundary="post_vlm",
            expected_size_bytes=8,
            memory_reader=lambda: {
                "MemAvailable_bytes": 1_000,
                "Cached_bytes": 2_000,
                "SReclaimable_bytes": 300,
            },
            residency_reader=lambda _path: {
                "method": "non_touching_mmap_mincore",
                "file_size_bytes": 8,
                "page_size_bytes": 4,
                "total_pages": 2,
                "resident_pages": 1,
                "resident_fraction": 0.5,
                "path_recorded": False,
            },
            clock_ns=lambda: next(ticks),
        )

        serialized = json.dumps(record.to_dict(), sort_keys=True)
        self.assertNotIn("private-model-name", serialized)
        self.assertEqual(record.boundary, "post_vlm")
        self.assertFalse(record.path_recorded)

    def test_boundary_validation_rejects_size_fraction_and_privacy_tampering(
        self,
    ) -> None:
        value = boundary_record("post_vlm", 90, 100)
        validate_boundary_observation(value)
        for key, replacement in (
            ("file_size_bytes", 7),
            ("resident_fraction", 0.75),
            ("path_recorded", True),
        ):
            tampered = dict(value)
            tampered[key] = replacement
            with self.assertRaises(ObservationValidationError):
                validate_boundary_observation(tampered)

    def test_process_observation_rejects_identity_and_extra_fields(self) -> None:
        value = process_observation()
        validate_process_observation(value)
        value["command_recorded"] = True
        with self.assertRaises(ObservationValidationError):
            validate_process_observation(value)
        value = process_observation()
        value["pid"] = 123
        with self.assertRaises(ObservationValidationError):
            validate_process_observation(value)


class EnergyWindowTests(unittest.TestCase):
    def test_interpolates_boundaries_and_integrates_vdd_in(self) -> None:
        record = integrate_vdd_in_window(
            resource_trace(),
            window_started_monotonic_ns=100 * MS,
            window_finished_monotonic_ns=300 * MS,
            maximum_gap_ns=110 * MS,
        )

        self.assertEqual(record.start_power_mw, 1_500.0)
        self.assertEqual(record.end_power_mw, 3_500.0)
        self.assertAlmostEqual(record.energy_mj, 500.0)
        self.assertEqual(record.source_sample_count, 4)
        self.assertEqual(record.integration_point_count, 4)
        self.assertFalse(record.process_attribution)

    def test_fails_closed_on_coverage_gap_missing_rail_and_duplicate_time(self) -> None:
        with self.assertRaisesRegex(EnergyIntegrationError, "cover"):
            integrate_vdd_in_window(
                resource_trace(),
                window_started_monotonic_ns=40 * MS,
                window_finished_monotonic_ns=300 * MS,
                maximum_gap_ns=110 * MS,
            )
        with self.assertRaisesRegex(EnergyIntegrationError, "gap"):
            integrate_vdd_in_window(
                resource_trace(),
                window_started_monotonic_ns=100 * MS,
                window_finished_monotonic_ns=300 * MS,
                maximum_gap_ns=99 * MS,
            )

        missing_rail = copy.deepcopy(resource_trace())
        missing_rail[1] = resource_sample(1, 150, 100, rail="VDD_CPU_GPU_CV")
        with self.assertRaisesRegex(EnergyIntegrationError, "VDD_IN"):
            integrate_vdd_in_window(
                missing_rail,
                window_started_monotonic_ns=100 * MS,
                window_finished_monotonic_ns=300 * MS,
                maximum_gap_ns=110 * MS,
            )

        duplicate = resource_trace()
        duplicate[1]["sample_monotonic_ns"] = 50 * MS
        with self.assertRaisesRegex(EnergyIntegrationError, "strictly"):
            integrate_vdd_in_window(
                duplicate,
                window_started_monotonic_ns=50 * MS,
                window_finished_monotonic_ns=300 * MS,
                maximum_gap_ns=110 * MS,
            )

    def test_derived_record_validator_rejects_scope_tampering(self) -> None:
        value = integrate_vdd_in_window(
            resource_trace(),
            window_started_monotonic_ns=100 * MS,
            window_finished_monotonic_ns=300 * MS,
            maximum_gap_ns=110 * MS,
        ).to_dict()
        value["process_attribution"] = True
        with self.assertRaises(EnergyIntegrationError):
            validate_energy_window_record(value)


class PrivacyContractTests(unittest.TestCase):
    def test_rejects_private_fields_paths_binary_content_and_true_flags(self) -> None:
        for value in (
            {"model_path": "redacted"},
            {"note": "C:\\private\\model.bin"},
            {"payload": b"private"},
            {"raw_telemetry_recorded": True},
        ):
            with self.assertRaises(PrivacyBoundaryError):
                validate_privacy_boundary(value)
        validate_privacy_boundary(
            {"path_recorded": False, "status": "ok", "values": [1, 2, 3]}
        )


class InjectedEvidencePathTests(unittest.TestCase):
    def test_treatment_path_preserves_order_and_charges_complete_window(self) -> None:
        calls: list[str] = []

        def boundary_reader(boundary: str) -> dict:
            calls.append(f"boundary:{boundary}")
            return (
                boundary_record("post_vlm", 90, 100)
                if boundary == "post_vlm"
                else boundary_record("post_action", 141, 150)
            )

        def treatment_runner() -> tuple[dict, dict]:
            calls.append("prefetch")
            return prefetch_action(), process_observation()

        def asr_runner() -> tuple[int, int, dict]:
            calls.append("asr")
            return 160 * MS, 300 * MS, process_observation()

        def resources() -> list[dict]:
            calls.append("resources")
            return resource_trace()

        evidence = run_injected_evidence_path(
            condition=PREFETCH_CONDITION,
            boundary_reader=boundary_reader,
            treatment_runner=treatment_runner,
            asr_runner=asr_runner,
            resource_reader=resources,
            maximum_resource_gap_ns=110 * MS,
        )

        self.assertEqual(
            calls,
            [
                "boundary:post_vlm",
                "prefetch",
                "boundary:post_action",
                "asr",
                "resources",
            ],
        )
        self.assertEqual(evidence["fully_charged_duration_ms"], 200.0)
        self.assertEqual(evidence["asr_duration_ms"], 140.0)
        self.assertEqual(evidence["energy_window"]["energy_mj"], 500.0)
        self.assertFalse(evidence["formal_evidence"])
        self.assertFalse(evidence["application_slice_authorized"])

    def test_control_path_never_invokes_treatment_or_second_observation(self) -> None:
        boundaries: list[str] = []

        def boundary_reader(boundary: str) -> dict:
            boundaries.append(boundary)
            return boundary_record("post_vlm", 90, 100)

        def forbidden_treatment() -> tuple[dict, dict]:
            raise AssertionError("control invoked the treatment")

        evidence = run_injected_evidence_path(
            condition=CONTROL_CONDITION,
            boundary_reader=boundary_reader,
            treatment_runner=forbidden_treatment,
            asr_runner=lambda: (110 * MS, 300 * MS, process_observation()),
            resource_reader=resource_trace,
            maximum_resource_gap_ns=110 * MS,
        )

        self.assertEqual(boundaries, ["post_vlm"])
        self.assertIsNone(evidence["prefetch_action"])
        self.assertIsNone(evidence["verified_post_action_observation"])
        validate_unit_evidence(evidence)

    def test_timeout_remains_a_complete_treatment_outcome(self) -> None:
        evidence = build_unit_evidence(
            condition=PREFETCH_CONDITION,
            common_post_vlm_observation=boundary_record("post_vlm", 90, 100),
            verified_post_action_observation=boundary_record("post_action", 141, 150),
            prefetch_action=prefetch_action(timeout=True),
            prefetch_process_observation=process_observation(),
            asr_started_monotonic_ns=160 * MS,
            asr_result_available_monotonic_ns=300 * MS,
            asr_process_observation=process_observation(),
            resource_samples=resource_trace(),
            maximum_resource_gap_ns=110 * MS,
        )
        self.assertEqual(evidence["prefetch_action"]["status"], "timeout")
        validate_unit_evidence(evidence)

    def test_rejects_treatment_overlap_and_energy_boundary_tampering(self) -> None:
        action = prefetch_action()
        action["action_started_monotonic_ns"] = 99 * MS
        with self.assertRaisesRegex(UnitEvidenceError, "charged boundary"):
            build_unit_evidence(
                condition=PREFETCH_CONDITION,
                common_post_vlm_observation=boundary_record("post_vlm", 90, 100),
                verified_post_action_observation=boundary_record(
                    "post_action", 141, 150
                ),
                prefetch_action=action,
                prefetch_process_observation=process_observation(),
                asr_started_monotonic_ns=160 * MS,
                asr_result_available_monotonic_ns=300 * MS,
                asr_process_observation=process_observation(),
                resource_samples=resource_trace(),
                maximum_resource_gap_ns=110 * MS,
            )

        changed_identity = prefetch_action()
        changed_identity["expected_size_bytes"] = 12
        changed_identity["bytes_read"] = 12
        changed_identity["chunk_count"] = 3
        with self.assertRaisesRegex(UnitEvidenceError, "model-size identity"):
            build_unit_evidence(
                condition=PREFETCH_CONDITION,
                common_post_vlm_observation=boundary_record("post_vlm", 90, 100),
                verified_post_action_observation=boundary_record(
                    "post_action", 141, 150
                ),
                prefetch_action=changed_identity,
                prefetch_process_observation=process_observation(),
                asr_started_monotonic_ns=160 * MS,
                asr_result_available_monotonic_ns=300 * MS,
                asr_process_observation=process_observation(),
                resource_samples=resource_trace(),
                maximum_resource_gap_ns=110 * MS,
            )

        evidence = run_injected_evidence_path(
            condition=CONTROL_CONDITION,
            boundary_reader=lambda _boundary: boundary_record("post_vlm", 90, 100),
            treatment_runner=lambda: (prefetch_action(), process_observation()),
            asr_runner=lambda: (110 * MS, 300 * MS, process_observation()),
            resource_reader=resource_trace,
            maximum_resource_gap_ns=110 * MS,
        )
        evidence["energy_window"]["window_started_monotonic_ns"] = 99 * MS
        with self.assertRaises((UnitEvidenceError, EnergyIntegrationError)):
            validate_unit_evidence(evidence)


if __name__ == "__main__":
    unittest.main()
