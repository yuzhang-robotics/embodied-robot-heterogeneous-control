from __future__ import annotations

import copy
import json
import tempfile
import threading
import unittest
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from experiments.phase1.common.telemetry_jetson import parse_tegrastats_line
from experiments.phase1.formal.protocol import (
    LLAMA_SOURCE_VERSION,
    VLM_MOONDREAM_DIGEST,
    VLM_OLLAMA_BINARY_SHA256,
    VLM_OLLAMA_VERSION,
)
from experiments.phase1.tests.formal_fixture import passing_base_preflight
from experiments.phase1.workloads.asr.adapter import (
    ASR_MODEL_SHA256,
    ASR_MODEL_SIZE_BYTES,
)
from experiments.phase1.workloads.llm.adapter import (
    LLM_EXPECTED_SERVED_MODEL_ID,
    LLM_MODEL_SHA256,
    LLM_MODEL_SIZE_BYTES,
    LLM_SERVER_ARGUMENTS,
)
from experiments.phase2.correctness import (
    CORRECTNESS_PROTOCOL_SHA256,
    DEFAULT_PROTOCOL_PATH,
    load_protocol,
    protocol_errors,
    protocol_sha256,
)
from experiments.phase2.evidence import CONTROL_CONDITION, PREFETCH_CONDITION
from experiments.phase2.observations import BOUNDARY_OBSERVATION_SCHEMA_VERSION
from experiments.phase2.preflight import (
    build_correctness_preflight,
    correctness_preflight_errors,
)
from experiments.phase2.residency.prefetch import PREFETCH_ACTION_SCHEMA_VERSION
from experiments.phase2.run_correctness_pilot import (
    PHASE2_EVENT_SCHEMA_VERSION,
    Phase2EventRecorder,
    PrefetchProcessObserver,
    run_pilot,
)
from jetson.phase1_runtime import EventStatus, PayloadRef, RuntimeEvent


MS = 1_000_000


def process_observation() -> dict[str, object]:
    return {
        "observation_schema_version": "0.2.0",
        "method": "sampled_linux_proc",
        "sample_interval_ms": 10.0,
        "sample_count": 3,
        "read_error_count": 0,
        "process_exit_observed": True,
        "user_time_s": 0.1,
        "system_time_s": 0.02,
        "minor_faults": 3,
        "major_faults": 1,
        "maximum_rss_bytes": 4096,
        "voluntary_context_switches": 2,
        "involuntary_context_switches": 1,
        "pid_recorded": False,
        "command_recorded": False,
    }


def boundary_record(
    boundary: str, started_ms: int, finished_ms: int
) -> dict[str, object]:
    return {
        "boundary_observation_schema_version": BOUNDARY_OBSERVATION_SCHEMA_VERSION,
        "boundary": boundary,
        "observation_started_monotonic_ns": started_ms * MS,
        "observation_finished_monotonic_ns": finished_ms * MS,
        "expected_size_bytes": 8,
        "mem_available_bytes": 1000,
        "cached_bytes": 2000,
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


def prefetch_action() -> dict[str, object]:
    return {
        "prefetch_action_schema_version": PREFETCH_ACTION_SCHEMA_VERSION,
        "status": "ok",
        "expected_size_bytes": 8,
        "buffer_size_bytes": 4 * 1024 * 1024,
        "timeout_ms": 20_000.0,
        "action_started_monotonic_ns": 510 * MS,
        "child_started_monotonic_ns": 511 * MS,
        "read_completed_monotonic_ns": 530 * MS,
        "child_finished_monotonic_ns": 531 * MS,
        "action_finished_monotonic_ns": 540 * MS,
        "bytes_read": 8,
        "chunk_count": 1,
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


def resource_trace() -> list[dict[str, object]]:
    samples: list[dict[str, object]] = []
    for sequence, timestamp_ms in enumerate(range(50, 851, 100)):
        line = (
            "09-12-2026 10:00:00 RAM 3000/7607MB (lfb 4x4MB) "
            "SWAP 0/3804MB (cached 0MB) CPU [1%@729,2%@729] "
            "EMC_FREQ 2%@2133 GR3D_FREQ 7%@[306] cpu@50C gpu@51C "
            "tj@52C VDD_IN 2000mW/2000mW"
        )
        samples.append(
            parse_tegrastats_line(
                line,
                sequence=sequence,
                sample_monotonic_ns=timestamp_ms * MS,
                sample_wall_time_ns=(1000 + timestamp_ms) * MS,
            )
        )
    return samples


def passing_preflight() -> dict[str, object]:
    checks = [
        "protocol_identity",
        "python_version",
        "jetpack_version",
        "l4t_core_version",
        "power_mode",
        "dynamic_dvfs_confirmed",
        "services_restarted",
        "ollama_version",
        "ollama_binary_identity",
        "whisper_prefetch_file_identity",
        "vlm_model_identity",
        "qwen_runtime_identity",
        "unrelated_inference_absent",
        "workload_preflights",
        "resource_and_safety_prerequisites",
    ]
    return {
        "correctness_preflight_schema_version": "0.1.0",
        "captured_at": "2026-09-13T00:00:00Z",
        "protocol": {
            "id": "phase2_bounded_whisper_residency_correctness_pilot_v1",
            "sha256": CORRECTNESS_PROTOCOL_SHA256,
            "protocol_commit": "2" * 40,
            "runner_commit": "1" * 40,
            "path_recorded": False,
        },
        "base": passing_base_preflight(),
        "workloads": {"asr": {}, "vlm": {}},
        "qwen_runtime": {},
        "ollama": {},
        "service_identity": {},
        "whisper_file": {
            "regular_file": True,
            "size_bytes": ASR_MODEL_SIZE_BYTES,
            "sha256": ASR_MODEL_SHA256,
            "page_size_bytes": 4096,
            "filesystem_block_size_bytes": 4096,
            "device_major": 1,
            "device_minor": 2,
            "path_recorded": False,
            "error_code": None,
        },
        "checks": [
            {
                "name": name,
                "required": True,
                "passed": True,
                "observed": True,
                "requirement": "fixture",
            }
            for name in checks
        ],
        "eligible": True,
        "formal_evidence": False,
        "application_slice_authorized": False,
    }


class Record:
    def __init__(self, value: dict[str, object]) -> None:
        self.value = value

    def to_dict(self) -> dict[str, object]:
        return copy.deepcopy(self.value)


class FakeStopReport:
    successful = True

    def to_dict(self) -> dict[str, object]:
        return {
            "sample_count": 9,
            "parse_error_count": 0,
            "reader_joined": True,
            "successful": True,
        }


class FakeSampler:
    instances: list["FakeSampler"] = []

    def __init__(self, *_args, **_kwargs) -> None:
        self.is_running = False
        self.waited: list[int] = []
        self.stop_count = 0
        self.__class__.instances.append(self)

    def start(self, **_kwargs) -> None:
        self.is_running = True

    def wait_for_sample_at_or_after(self, boundary: int, **_kwargs) -> int:
        self.waited.append(boundary)
        return boundary

    def stop(self) -> FakeStopReport:
        self.stop_count += 1
        self.is_running = False
        return FakeStopReport()


class FakeThermalMonitor:
    def __init__(self, **_kwargs) -> None:
        self.stop_requested = threading.Event()

    def observe(self, _sample) -> None:
        pass

    def wait_below(self, **_kwargs) -> dict[str, object]:
        return {
            "maximum_tj_c": 55.0,
            "consecutive_samples": 10,
            "first_sequence": 0,
            "last_sequence": 9,
            "observed_tj_c": [50.0] * 10,
        }


class FakeProbe:
    instances: list["FakeProbe"] = []

    def __init__(self, **_kwargs) -> None:
        self.started = False
        self.stopped = False
        self.__class__.instances.append(self)

    def start(self) -> None:
        self.started = True

    def stop(self, **_kwargs) -> SimpleNamespace:
        self.stopped = True
        return SimpleNamespace(
            joined=True,
            tick_count=5,
            skipped_releases=0,
            deadline_miss_count=0,
            max_lateness_ns=1,
            max_gap_ns=100 * MS,
            error_code=None,
        )


class ProtocolAndPreflightTests(unittest.TestCase):
    def test_protocol_identity_is_frozen_and_nonformal(self) -> None:
        protocol = load_protocol()
        self.assertEqual(protocol_errors(protocol), [])
        self.assertEqual(protocol_sha256(protocol), CORRECTNESS_PROTOCOL_SHA256)
        self.assertFalse(protocol["formal_evidence"])
        self.assertFalse(protocol["application_slice_authorized"])

        changed = copy.deepcopy(protocol)
        changed["parameters"]["prefetch_timeout_s"] = 21.0
        self.assertTrue(protocol_errors(changed))
        self.assertNotEqual(protocol_sha256(changed), CORRECTNESS_PROTOCOL_SHA256)

    def test_preflight_binds_target_identity_and_fails_on_dirty_main(self) -> None:
        root = Path(__file__).resolve().parents[3]
        protocol = load_protocol()
        base = passing_base_preflight()
        base["safety"]["robot_enable_motion_raw"] = "0"
        qwen = {
            "model_size_bytes": LLM_MODEL_SIZE_BYTES,
            "model_sha256": LLM_MODEL_SHA256,
            "source_version": LLAMA_SOURCE_VERSION,
            "source_clean": True,
            "server_process_count": 1,
            "server_arguments": dict(LLM_SERVER_ARGUMENTS),
            "server_arguments_match": True,
            "server_model_path_matches": True,
            "endpoint_local": True,
            "listener_loopback_only": True,
            "service_reachable": True,
            "served_model_ids": [LLM_EXPECTED_SERVED_MODEL_ID],
            "expected_model_present": True,
            "error_code": None,
        }
        vlm = {
            "services": {
                "ollama": {"model_digest": VLM_MOONDREAM_DIGEST},
                "qwen": {"served_model_ids": [LLM_EXPECTED_SERVED_MODEL_ID]},
            }
        }
        ollama = {
            "version_output": f"ollama version is {VLM_OLLAMA_VERSION}",
            "binary_sha256": VLM_OLLAMA_BINARY_SHA256,
            "executable_path_recorded": False,
            "active_model_count": 0,
            "active_model_names_recorded": False,
            "error_code": None,
        }
        services = {
            name: {
                "process_count": 1,
                "process_start_identities": [f"{index} start"],
                "arguments_recorded": False,
            }
            for index, name in enumerate(("llama-server", "ollama"), start=1)
        }
        whisper = passing_preflight()["whisper_file"]
        payload = PayloadRef(
            ref="fixture://input",
            sha256="a" * 64,
            size_bytes=1,
            media_type="application/octet-stream",
        )

        def build(base_record: dict[str, object]) -> dict[str, object]:
            with (
                patch(
                    "experiments.phase2.preflight.fixed_asr_payload",
                    return_value=payload,
                ),
                patch(
                    "experiments.phase2.preflight.fixed_c100_payload",
                    return_value=payload,
                ),
                patch(
                    "experiments.phase2.preflight.asr_preflight_errors",
                    return_value=[],
                ),
                patch(
                    "experiments.phase2.preflight.vlm_preflight_errors",
                    return_value=[],
                ),
                patch(
                    "experiments.phase2.preflight.command_snapshot",
                    return_value={
                        "returncode": 0,
                        "output": "2" * 40,
                        "error_code": None,
                    },
                ),
            ):
                return build_correctness_preflight(
                    root,
                    protocol,
                    asr_input="unused",
                    vlm_input="unused",
                    services_restarted=True,
                    dynamic_dvfs_confirmed=True,
                    base_preflight=base_record,
                    asr_preflight={},
                    vlm_preflight=vlm,
                    llm_runtime=qwen,
                    ollama_identity=ollama,
                    service_identity=services,
                    whisper_file_identity=whisper,
                )

        passing = build(base)
        with (
            patch(
                "experiments.phase2.preflight.asr_preflight_errors",
                return_value=[],
            ),
            patch(
                "experiments.phase2.preflight.vlm_preflight_errors",
                return_value=[],
            ),
        ):
            self.assertEqual(correctness_preflight_errors(passing), [])
        self.assertTrue(passing["eligible"])

        dirty = copy.deepcopy(base)
        dirty["environment"]["git"]["dirty"] = True
        dirty["checks"][6]["passed"] = False
        dirty["eligible"] = False
        rejected = build(dirty)
        self.assertFalse(rejected["eligible"])
        self.assertFalse(
            next(
                item
                for item in rejected["checks"]
                if item["name"] == "resource_and_safety_prerequisites"
            )["passed"]
        )


class ObserverAndEventTests(unittest.TestCase):
    def test_prefetch_process_observer_closes_without_pid(self) -> None:
        observer = PrefetchProcessObserver(
            sample_interval_s=1.0,
            reader=lambda _pid: {
                "user_time_s": 0.1,
                "system_time_s": 0.2,
                "minor_faults": 3,
                "major_faults": 4,
                "rss_bytes": 1024,
                "high_water_rss_bytes": 2048,
                "voluntary_context_switches": 5,
                "involuntary_context_switches": 6,
            },
        )
        observer.start(123)
        report = observer.finish()
        self.assertFalse(report["pid_recorded"])
        self.assertNotIn("pid", report)
        self.assertEqual(report["maximum_rss_bytes"], 2048)

    def test_phase2_event_record_removes_runtime_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            recorder = Phase2EventRecorder(
                temporary,
                "20260913T000000Z_phase2_pilot_unit_001",
            )
            recorder.emit(
                RuntimeEvent(
                    event="probe.tick",
                    component="probe",
                    status=EventStatus.OK,
                    details={"pid": 123, "thread_name": "private", "tick_index": 1},
                )
            )
            recorder.close()
            item = json.loads((Path(temporary) / "events.jsonl").read_text())
        self.assertEqual(
            item["phase2_event_schema_version"], PHASE2_EVENT_SCHEMA_VERSION
        )
        self.assertEqual(item["details"], {"tick_index": 1})
        self.assertNotIn("phase1", json.dumps(item))


class InjectedPilotTests(unittest.TestCase):
    def setUp(self) -> None:
        FakeSampler.instances.clear()
        FakeProbe.instances.clear()

    def _run(
        self, root: Path, *, fail_recovery: bool = False
    ) -> tuple[Path, list[tuple]]:
        boundaries = iter(
            [
                ("pre_unit", 10, 20),
                ("post_primer", 50, 60),
                ("post_vlm", 90, 100),
                ("post_asr", 301, 310),
                ("pre_unit", 400, 410),
                ("post_primer", 450, 460),
                ("post_vlm", 490, 500),
                ("post_action", 541, 550),
                ("post_asr", 701, 710),
            ]
        )
        calls: list[tuple] = []
        invocation_times = {
            (1, "primer_1"): (21, 30),
            (1, "primer_2"): (31, 40),
            (1, "vlm"): (61, 80),
            (1, "measured_asr"): (110, 300),
            (1, "recovery_asr"): (311, 350),
            (2, "primer_1"): (411, 420),
            (2, "primer_2"): (421, 430),
            (2, "vlm"): (461, 480),
            (2, "measured_asr"): (560, 700),
            (2, "recovery_asr"): (711, 750),
        }

        def observe(_model, *, boundary: str, expected_size_bytes: int) -> Record:
            self.assertEqual(expected_size_bytes, 8)
            expected, started, finished = next(boundaries)
            self.assertEqual(boundary, expected)
            calls.append(("observation", boundary))
            return Record(boundary_record(boundary, started, finished))

        def invoke(session_dir: Path, **kwargs) -> tuple[Path, dict[str, object]]:
            unit = kwargs["unit_index"]
            role = kwargs["role"]
            condition = kwargs["condition"]
            calls.append(("invocation", unit, condition, role))
            if fail_recovery and unit == 2 and role == "recovery_asr":
                raise RuntimeError("injected recovery failure")
            started, finished = invocation_times[(unit, role)]
            run_dir = session_dir / "injected-runs" / f"{unit}-{role}"
            run_dir.mkdir(parents=True)
            gates = (
                [
                    {"name": name, "passed": True}
                    for name in (
                        "child_process_reaped",
                        "model_unload_claim_bounded",
                        "residency_contract_verified",
                    )
                ]
                if role == "vlm"
                else []
            )
            return run_dir, {
                "adapter": {
                    "started_monotonic_ns": started * MS,
                    "finished_monotonic_ns": finished * MS,
                },
                "process_observation": (
                    process_observation() if kwargs["workload"] == "asr" else None
                ),
                "gates": gates,
                "valid": True,
            }

        def treatment(_model, **kwargs) -> tuple[dict[str, object], dict[str, object]]:
            calls.append(("treatment", kwargs["expected_size_bytes"]))
            self.assertFalse(kwargs["stop_requested"]())
            return prefetch_action(), process_observation()

        model_path = root / "private-model-name.bin"
        model_path.write_bytes(b"12345678")
        args = Namespace(
            collection_id="20260913T000000Z_phase2_correctness_pilot_v1",
            protocol=DEFAULT_PROTOCOL_PATH,
            output_root=root / "experiments" / "runs" / "output",
            asr_input=Path("unused-asr"),
            vlm_input=Path("unused-vlm"),
            confirm_services_restarted=True,
            confirm_dynamic_dvfs=True,
            thermal_wait_timeout_s=1.0,
        )
        injected_preflight = passing_preflight()
        injected_preflight["whisper_file"]["size_bytes"] = 8
        injected_preflight["whisper_file"]["sha256"] = "a" * 64
        with (
            patch(
                "experiments.phase2.run_correctness_pilot.correctness_preflight_errors",
                return_value=[],
            ),
            patch(
                "experiments.phase2.run_correctness_pilot.validate_resource_samples",
                return_value=[],
            ),
        ):
            session = run_pilot(
                args,
                repo_root=root,
                preflight_builder=lambda *_args, **_kwargs: injected_preflight,
                sampler_factory=FakeSampler,
                invocation_runner=invoke,
                observation_reader=observe,
                treatment_runner=treatment,
                resource_reader=lambda _path: resource_trace(),
                probe_factory=FakeProbe,
                thermal_monitor_factory=FakeThermalMonitor,
                payloads_override={"asr": object(), "vlm": object()},
                whisper_model_path=model_path,
            )
        return session, calls

    def test_full_pair_preserves_order_cost_boundary_and_nonformal_authority(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            session, calls = self._run(Path(temporary))
            manifest = json.loads((session / "manifest.json").read_text())
            control = json.loads(
                (session / "units" / "unit-01-control" / "evidence.json").read_text()
            )
            treatment = json.loads(
                (session / "units" / "unit-02-prefetch" / "evidence.json").read_text()
            )
            artifacts = "\n".join(
                path.read_text(encoding="utf-8") for path in session.rglob("*.json")
            )

        self.assertEqual(manifest["status"], "completed")
        self.assertEqual(manifest["completed_units"], 2)
        self.assertFalse(manifest["formal_evidence"])
        self.assertFalse(manifest["application_slice_authorized"])
        self.assertEqual(control["fully_charged_duration_ms"], 200.0)
        self.assertEqual(treatment["fully_charged_duration_ms"], 200.0)
        self.assertIsNone(control["prefetch_action"])
        self.assertEqual(treatment["prefetch_action"]["status"], "ok")
        self.assertEqual(sum(1 for item in calls if item[0] == "treatment"), 1)
        self.assertEqual(len([item for item in calls if item[0] == "invocation"]), 10)
        self.assertNotIn("phase1_", artifacts)
        self.assertTrue(
            all(probe.started and probe.stopped for probe in FakeProbe.instances)
        )
        self.assertEqual(FakeSampler.instances[0].stop_count, 1)

    def test_failure_closes_sampler_and_probe_and_retains_completed_action(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(RuntimeError, "recovery"):
                session, _calls = self._run(Path(temporary), fail_recovery=True)
            session = (
                Path(temporary)
                / "experiments"
                / "runs"
                / "output"
                / "20260913T000000Z_phase2_correctness_pilot_v1"
            )
            manifest = json.loads((session / "manifest.json").read_text())
            unit = json.loads(
                (session / "units" / "unit-02-prefetch" / "unit.json").read_text()
            )

        self.assertEqual(manifest["status"], "aborted")
        self.assertEqual(manifest["failure_class"], "pilot_execution")
        self.assertEqual(unit["status"], "aborted")
        self.assertEqual(unit["prefetch_action"]["status"], "ok")
        self.assertIn("condition_action", unit["completed_steps"])
        self.assertTrue(all(probe.stopped for probe in FakeProbe.instances))
        self.assertEqual(FakeSampler.instances[0].stop_count, 1)


if __name__ == "__main__":
    unittest.main()
