from __future__ import annotations

import json
import math
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from experiments.phase1.analyze_formal_runs import (
    _refuse_output_inside_collection,
    _require_distinct_outputs,
    analyze_formal_collection,
    descriptive,
    nearest_rank,
    paired_hierarchical_bootstrap,
)
from experiments.phase1.asr_adapter import (
    ASR_EXPECTED_OUTPUT_LENGTH,
    ASR_EXPECTED_OUTPUT_SHA256,
    ASR_INPUT_MEDIA_TYPE,
)
from experiments.phase1.formal_preflight import FROZEN_PROTOCOL_SHA256
from experiments.phase1.formal_protocol import (
    FORMAL_PROTOCOL_ID,
    WORKLOADS,
    build_formal_protocol,
    canonical_protocol_text,
)
from experiments.phase1.jetson_telemetry import parse_tegrastats_line
from experiments.phase1.llm_adapter import (
    LLM_EXPECTED_SERVED_MODEL_ID,
    LLM_INPUT_MEDIA_TYPE,
    frozen_llm_request_contract,
)
from experiments.phase1.manifest import sha256_file
from experiments.phase1.tests.formal_fixture import passing_formal_preflight
from experiments.phase1.vlm_adapter import C100_INPUT_MEDIA_TYPE


TEGRASTATS_SAMPLE = (
    "09-02-2026 10:00:00 RAM 3000/7607MB (lfb 4x4MB) "
    "SWAP 0/3804MB (cached 0MB) CPU [1%@729,2%@729,3%@729,4%@729,5%@729,6%@729] "
    "EMC_FREQ 2%@2133 GR3D_FREQ 7%@[306] cpu@50C gpu@50C tj@50C "
    "VDD_IN 5800mW/5800mW"
)


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def read_json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError("fixture JSON root must be an object")
    return value


def refresh_manifest(session_dir: Path) -> None:
    manifest_path = session_dir / "manifest.json"
    manifest = read_json(manifest_path)
    manifest["artifacts"] = {
        path.relative_to(session_dir).as_posix(): {
            "size_bytes": path.stat().st_size,
            "sha256": sha256_file(path),
        }
        for path in session_dir.rglob("*")
        if path.is_file() and path.name != "manifest.json"
    }
    write_json(manifest_path, manifest)


def write_event_trace(run_dir: Path, run: dict[str, object]) -> None:
    started_ns = int(run["started_monotonic_ns"])
    if run["condition"] == "formal_idle":
        event_specs: list[tuple[str, dict[str, object]]] = [
            ("formal.idle_started", {}),
            ("probe.started", {}),
            ("probe.tick", {}),
            ("probe.stopped", {}),
            ("formal.idle_stopped", {}),
        ]
    else:
        adapter = run["adapter"]
        event_specs = [("probe.started", {}), ("probe.tick", {})]
    if run["condition"] == "formal_async":
        adapter_started = int(adapter["started_monotonic_ns"])
        adapter_finished = int(adapter["finished_monotonic_ns"])
        event_specs.extend(
            [
                ("task.enqueued", {"created_monotonic_ns": adapter_started}),
                ("task.started", {"started_monotonic_ns": adapter_started}),
                ("task.finished", {"finished_monotonic_ns": adapter_finished}),
                ("result.accepted", {"transition_monotonic_ns": adapter_finished}),
            ]
        )
    if run["condition"] != "formal_idle":
        event_specs.append(("probe.stopped", {}))
    with (run_dir / "events.jsonl").open("w", encoding="utf-8") as stream:
        for sequence, (name, details) in enumerate(event_specs):
            monotonic_ns = started_ns + sequence
            event = {
                "schema_version": "0.2.0",
                "run_id": run["run_id"],
                "seq": sequence,
                "event": name,
                "component": "fixture",
                "status": "ok",
                "monotonic_ns": monotonic_ns,
                "wall_time_ns": monotonic_ns + 1_000_000_000,
                "pid": 1,
                "thread_id": 1,
                "details": details,
            }
            stream.write(json.dumps(event, separators=(",", ":")) + "\n")


def run_record(
    entry: dict[str, object],
    *,
    started_ns: int,
    async_condition: bool,
    workload_contract: dict[str, object],
) -> dict[str, object]:
    workload = str(entry["workload"])
    duration_ns = 105_000_000 if async_condition else 100_000_000
    input_media_type = {
        "asr": ASR_INPUT_MEDIA_TYPE,
        "llm": LLM_INPUT_MEDIA_TYPE,
        "vlm": C100_INPUT_MEDIA_TYPE,
    }[workload]
    output_sha256 = ASR_EXPECTED_OUTPUT_SHA256 if workload == "asr" else "b" * 64
    output_length = ASR_EXPECTED_OUTPUT_LENGTH if workload == "asr" else 1
    adapter: dict[str, object] = {
        "task_id": "fixture",
        "worker_thread_id": 2 if async_condition else 1,
        "started_monotonic_ns": started_ns,
        "finished_monotonic_ns": started_ns + duration_ns,
        "duration_ns": duration_ns,
        "execution_outcome": "ok",
        "error_code": None,
        "input": {
            "sha256": workload_contract["input_sha256"],
            "size_bytes": workload_contract["input_size_bytes"],
            "media_type": input_media_type,
        },
        "output": {
            "sha256": output_sha256,
            "length": output_length,
            "raw_text_recorded": False,
        },
        "cancellation": {"requested": False},
    }
    process: dict[str, object] | None = None
    if workload == "asr":
        adapter["process"] = {
            "started": True,
            "reaped": True,
            "exit_code": 0,
            "terminate_requested": False,
            "terminate_confirmed": False,
            "kill_requested": False,
            "kill_confirmed": False,
        }
    elif workload == "llm":
        adapter["input"]["raw_text_recorded"] = False
        adapter["stage_durations_ns"] = {"llama_inference": duration_ns}
        adapter["request"] = {
            **frozen_llm_request_contract(),
            "raw_prompt_recorded": False,
        }
        adapter["response"] = {
            "model": LLM_EXPECTED_SERVED_MODEL_ID,
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 10,
                "total_tokens": 20,
            },
            "raw_response_recorded": False,
        }
        adapter["model_residency"] = {
            "policy": "external_llama_server_resident",
            "server_preexisting": True,
            "unload_requested": False,
            "backend_stop_confirmed": None,
        }
    else:
        adapter["stage_durations_ns"] = {"moondream_inference": duration_ns}
        adapter["translation_route"] = "qwen"
        adapter["model_residency"] = {
            "unload_requested": True,
            "unload_confirmed": None,
        }
        process = {
            "start_method": "spawn",
            "protocol_complete": True,
            "exit_code": 0,
            "error_code": None,
            "joined_monotonic_ns": started_ns + duration_ns,
        }
    probe = {
        "implementation": (
            "independent_thread" if async_condition else "inline_same_thread"
        ),
        "joined": True,
        "tick_count": 10,
        "skipped_releases": 0,
        "deadline_miss_count": 0,
        "max_lateness_ns": 1_000_000,
        "max_gap_ns": 20_000_000 if async_condition else 100_000_000,
        "error_code": None,
    }
    runtime = (
        {
            "used": True,
            "pending_capacity": 1,
            "result_capacity": 1,
            "final_snapshot": {
                "state": "closed",
                "submission_attempts": 1,
                "admitted_total": 1,
                "rejected_at_ingress_total": 0,
                "terminal_admitted_total": 1,
                "queued": 0,
                "running": 0,
                "result_pending": 0,
                "max_pending_depth": 1,
                "max_result_depth": 1,
                "accounting_holds": True,
                "disposition_counts": {"consumed": 1},
            },
            "shutdown": {
                "complete": True,
                "broker_state": "closed",
                "joined": True,
                "join_latency_ns": 1_000_000,
                "worker_error_code": None,
                "event_error_code": None,
            },
        }
        if async_condition
        else {
            "used": False,
            "pending_capacity": 0,
            "result_capacity": 0,
            "final_snapshot": None,
            "shutdown": None,
        }
    )
    gate_names = {
        "adapter_completed",
        "output_private",
        "cancellation_absent",
        "probe_closed",
        "thermal_stop_absent",
    }
    if async_condition:
        gate_names.update({"single_consumed_request", "bounded_lane", "worker_joined"})
    else:
        gate_names.update({"synchronous_call_boundary", "runtime_not_used"})
    if workload == "asr":
        gate_names.update({"transcript_identity", "child_process_reaped"})
    elif workload == "llm":
        gate_names.update(
            {
                "request_contract_verified",
                "token_usage_valid",
                "server_residency_claim_bounded",
            }
        )
    else:
        gate_names.update(
            {
                "translation_route_verified",
                "child_process_reaped",
                "model_unload_claim_bounded",
            }
        )
    gates = [
        {
            "name": name,
            "passed": True,
            "observed": (
                {
                    "workload": workload,
                    "adapter_thread_id": 1,
                    "calling_thread_id": 1,
                    "process_start_method": ("spawn" if workload == "vlm" else None),
                }
                if name == "synchronous_call_boundary"
                else True
            ),
        }
        for name in sorted(gate_names)
    ]
    result = {
        "task_id": "fixture",
        "task_kind": workload,
        "state_scope_id": "phase1-formal",
        "state_generation": 0,
        "source_monotonic_ns": started_ns,
        "deadline_monotonic_ns": started_ns + 1_000_000_000,
        "input_sha256": workload_contract["input_sha256"],
        "started_monotonic_ns": started_ns,
        "finished_monotonic_ns": started_ns + duration_ns,
        "execution_outcome": "ok",
        "output": dict(adapter["output"]),
        "output_ref_recorded": False,
        "error_code": None,
        "cancellation": {
            "requested": False,
            "client_wait_stopped": False,
            "worker_observed": False,
            "backend_stop_confirmed": None,
        },
    }
    return {
        "formal_run_schema_version": "0.1.0",
        "run_id": "fixture",
        "status": "completed",
        "workload": workload,
        "condition": entry["condition"],
        "role": entry["role"],
        "task_id": "fixture",
        "started_monotonic_ns": started_ns,
        "finished_monotonic_ns": started_ns + duration_ns,
        "duration_ns": duration_ns,
        "adapter": adapter,
        "result": result,
        "process": process,
        "probe": probe,
        "runtime": runtime,
        "thermal_stop_requested": False,
        "gates": gates,
        "valid": True,
        "plan": entry,
        "input": {
            "sha256": workload_contract["input_sha256"],
            "size_bytes": workload_contract["input_size_bytes"],
            "media_type": input_media_type,
            "path_recorded": False,
        },
        "raw_input_recorded": False,
        "raw_output_recorded": False,
    }


def build_collection(root: Path) -> Path:
    protocol = build_formal_protocol()
    collection = root / "20260902T000000Z_phase1_formal_fixture"
    base_time = datetime(2026, 9, 2, tzinfo=timezone.utc)
    for session_index, session_plan in enumerate(protocol["sessions"], start=1):
        session_dir = collection / f"session-{session_index:02d}-attempt-01"
        session_dir.mkdir(parents=True)
        (session_dir / "protocol.json").write_text(
            canonical_protocol_text(protocol), encoding="utf-8"
        )
        preflight = passing_formal_preflight(service_suffix=str(session_index))
        write_json(session_dir / "preflight.json", preflight)
        entries: list[dict[str, object]] = []
        ordinal = 0
        for warmup in session_plan["warmups"]:
            ordinal += 1
            entry = dict(warmup)
            entry["condition"] = "formal_sync"
            entry["ordinal"] = ordinal
            entries.append(entry)
        for measured in session_plan["measured_runs"]:
            ordinal += 1
            entry = dict(measured)
            entry["ordinal"] = ordinal
            entries.append(entry)

        ledger: list[dict[str, object]] = []
        sample_times = [0]
        clock_ns = 10_000
        warmups = [entry for entry in entries if entry["role"] == "warmup"]
        measured = [entry for entry in entries if entry["role"] == "measured"]

        def add_entry(entry: dict[str, object]) -> None:
            nonlocal clock_ns
            role_dir = "warmups" if entry["role"] == "warmup" else "measured"
            relative = (
                f"{role_dir}/{entry['ordinal']:03d}-"
                f"{entry['workload']}-{entry['condition']}"
            )
            run_dir = session_dir / relative
            run_dir.mkdir(parents=True)
            ledger.append({"event": "entry_started", "at": "fixture", "plan": entry})
            run = run_record(
                entry,
                started_ns=clock_ns,
                async_condition=entry["condition"] == "formal_async",
                workload_contract=protocol["workloads"][entry["workload"]],
            )
            write_json(run_dir / "run.json", run)
            write_event_trace(run_dir, run)
            ledger.append(
                {
                    "event": "entry_completed",
                    "at": "fixture",
                    "plan": entry,
                    "run": relative,
                    "valid": True,
                }
            )
            sample_times.append(clock_ns + 50_000_000)
            clock_ns += 200_000_000

        for entry in warmups:
            add_entry(entry)
        for label in ("pre_measurement",):
            ledger.append({"event": "idle_started", "at": "fixture", "label": label})
            idle_dir = session_dir / "idle" / label
            idle_run = {
                "formal_run_schema_version": "0.1.0",
                "run_id": f"fixture-{label}",
                "status": "completed",
                "role": "idle_reference",
                "condition": "formal_idle",
                "label": label,
                "duration_s": 30,
                "started_monotonic_ns": clock_ns,
                "finished_monotonic_ns": clock_ns + 30_000_000_000,
                "probe": {
                    "implementation": "independent_thread",
                    "joined": True,
                    "tick_count": 300,
                    "skipped_releases": 0,
                    "deadline_miss_count": 0,
                    "max_lateness_ns": 1_000_000,
                    "max_gap_ns": 100_000_000,
                    "error_code": None,
                },
                "valid": True,
            }
            write_json(
                idle_dir / "run.json",
                idle_run,
            )
            write_event_trace(idle_dir, idle_run)
            ledger.append(
                {
                    "event": "idle_completed",
                    "at": "fixture",
                    "label": label,
                    "run": f"idle/{label}",
                }
            )
            sample_times.append(clock_ns + 50_000_000)
            clock_ns += 30_200_000_000
        for entry in measured:
            add_entry(entry)
        label = "post_measurement"
        ledger.append({"event": "idle_started", "at": "fixture", "label": label})
        idle_dir = session_dir / "idle" / label
        idle_run = {
            "formal_run_schema_version": "0.1.0",
            "run_id": f"fixture-{label}",
            "status": "completed",
            "role": "idle_reference",
            "condition": "formal_idle",
            "label": label,
            "duration_s": 30,
            "started_monotonic_ns": clock_ns,
            "finished_monotonic_ns": clock_ns + 30_000_000_000,
            "probe": {
                "implementation": "independent_thread",
                "joined": True,
                "tick_count": 300,
                "skipped_releases": 0,
                "deadline_miss_count": 0,
                "max_lateness_ns": 1_000_000,
                "max_gap_ns": 100_000_000,
                "error_code": None,
            },
            "valid": True,
        }
        write_json(
            idle_dir / "run.json",
            idle_run,
        )
        write_event_trace(idle_dir, idle_run)
        ledger.append(
            {
                "event": "idle_completed",
                "at": "fixture",
                "label": label,
                "run": f"idle/{label}",
            }
        )
        sample_times.extend((clock_ns + 50_000_000, clock_ns + 30_200_000_000))
        with (session_dir / "ledger.jsonl").open("w", encoding="utf-8") as stream:
            for item in ledger:
                stream.write(json.dumps(item, separators=(",", ":")) + "\n")
        with (session_dir / "resources.jsonl").open("w", encoding="utf-8") as stream:
            for sequence, monotonic_ns in enumerate(sample_times):
                sample = parse_tegrastats_line(
                    TEGRASTATS_SAMPLE,
                    sequence=sequence,
                    sample_monotonic_ns=monotonic_ns,
                    sample_wall_time_ns=monotonic_ns + 1_000_000_000,
                )
                stream.write(json.dumps(sample, separators=(",", ":")) + "\n")
        created = base_time + timedelta(minutes=31 * (session_index - 1))
        completed = created + timedelta(minutes=1)
        manifest = {
            "formal_session_schema_version": "0.1.0",
            "artifact_kind": "phase1_g6_formal_session",
            "collection_id": collection.name,
            "session_id": session_dir.name,
            "protocol_session": f"session-{session_index:02d}",
            "attempt": 1,
            "protocol_id": FORMAL_PROTOCOL_ID,
            "protocol_sha256": FROZEN_PROTOCOL_SHA256,
            "status": "completed",
            "failure_class": None,
            "failure_code": None,
            "created_at": created.isoformat().replace("+00:00", "Z"),
            "completed_at": completed.isoformat().replace("+00:00", "Z"),
            "formal_evidence_eligible": True,
            "development_injection": False,
            "preflight": {
                "protocol_commit": preflight["protocol"]["protocol_commit"],
                "runner_commit": preflight["protocol"]["runner_commit"],
                "service_identity": preflight["service_identity"],
            },
            "completed_entries": 41,
            "thermal": {
                "session_start": {
                    "maximum_tj_c": 55.0,
                    "consecutive_samples": 10,
                    "first_sequence": 0,
                    "last_sequence": 9,
                    "observed_tj_c": [50.0] * 10,
                },
                "measurement_start": {
                    "maximum_tj_c": 55.0,
                    "consecutive_samples": 10,
                    "first_sequence": 10,
                    "last_sequence": 19,
                    "observed_tj_c": [50.0] * 10,
                },
                "stop_tj_c": 85.0,
                "stop_requested": False,
            },
            "resource_sampler_report": {
                "sample_count": len(sample_times),
                "parse_error_count": 0,
                "first_sample_monotonic_ns": sample_times[0],
                "last_sample_monotonic_ns": sample_times[-1],
                "process_returncode": -15,
                "stop_method": "terminated",
                "reader_joined": True,
                "reader_error_code": None,
                "successful": True,
            },
            "artifacts": {},
        }
        manifest["artifacts"] = {
            path.relative_to(session_dir).as_posix(): {
                "size_bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
            for path in session_dir.rglob("*")
            if path.is_file() and path.name != "manifest.json"
        }
        write_json(session_dir / "manifest.json", manifest)
    return collection


class FormalAnalysisTests(unittest.TestCase):
    def test_analysis_outputs_cannot_modify_the_source_collection(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "collection"
            outside = Path(temp_dir) / "analysis.json"
            _refuse_output_inside_collection(outside, root)
            with self.assertRaisesRegex(ValueError, "source collection"):
                _refuse_output_inside_collection(root / "analysis.json", root)
            with self.assertRaisesRegex(ValueError, "distinct paths"):
                _require_distinct_outputs(outside, outside)

    def test_descriptive_statistics_use_nearest_rank(self) -> None:
        self.assertEqual(nearest_rank(range(1, 31), 95), 29.0)
        result = descriptive([1.0, 2.0, 3.0, 4.0])
        self.assertEqual(result["mean"], 2.5)
        self.assertEqual(result["p95_nearest_rank"], 4.0)

    def test_hierarchical_bootstrap_is_deterministic_and_paired(self) -> None:
        differences = {
            workload: [[-80.0] * 6 for _ in range(5)] for workload in WORKLOADS
        }
        log_ratios = {
            workload: [[math.log(1.05)] * 6 for _ in range(5)] for workload in WORKLOADS
        }

        first = paired_hierarchical_bootstrap(
            differences,
            log_ratios,
            resamples=2_000,
            seed=123,
        )
        second = paired_hierarchical_bootstrap(
            differences,
            log_ratios,
            resamples=2_000,
            seed=123,
        )

        self.assertEqual(first, second)
        for workload in WORKLOADS:
            self.assertAlmostEqual(
                first[workload]["paired_mean_difference_ci95"]["high"], -80.0
            )
            self.assertAlmostEqual(
                first[workload]["paired_geometric_mean_ratio_ci95"]["high"],
                1.05,
            )

    def test_complete_collection_is_reconstructed_and_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            collection = build_collection(Path(temp_dir))
            analysis = analyze_formal_collection(collection)

        self.assertEqual(analysis["dataset"]["validated_measured_runs"], 180)
        self.assertTrue(
            all(
                len(session["idle_references"]) == 2
                for session in analysis["dataset"]["sessions"].values()
            )
        )
        self.assertTrue(analysis["decision"]["pass"])
        self.assertTrue(analysis["decision"]["formal_claim_permitted"])
        for workload in WORKLOADS:
            self.assertTrue(analysis["workloads"][workload]["pass"])
            result = analysis["workloads"][workload]
            self.assertAlmostEqual(
                result["responsiveness"]["paired_mean_async_minus_sync_ms"],
                -80.0,
            )
            self.assertAlmostEqual(
                result["responsiveness"]["paired_mean_percentage_change"],
                -80.0,
            )
            self.assertIn(
                "paired_difference_statistics", result["workload_noninferiority"]
            )
            self.assertIn(
                "paired_percentage_change_statistics",
                result["workload_noninferiority"],
            )

    def test_tampered_artifact_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            collection = build_collection(Path(temp_dir))
            run = next(collection.glob("session-01-attempt-01/measured/*/run.json"))
            run.write_text("{}\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "artifact identity mismatch"):
                analyze_formal_collection(collection)

    def test_reordered_ledger_is_rejected_after_hash_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            collection = build_collection(Path(temp_dir))
            session_dir = collection / "session-01-attempt-01"
            ledger_path = session_dir / "ledger.jsonl"
            lines = ledger_path.read_text(encoding="utf-8").splitlines()
            lines[0], lines[1] = lines[1], lines[0]
            ledger_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            refresh_manifest(session_dir)

            with self.assertRaisesRegex(ValueError, "ledger event 0 is reordered"):
                analyze_formal_collection(collection)

    def test_missing_protocol_entry_is_rejected_after_hash_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            collection = build_collection(Path(temp_dir))
            session_dir = collection / "session-01-attempt-01"
            ledger_path = session_dir / "ledger.jsonl"
            lines = ledger_path.read_text(encoding="utf-8").splitlines()
            del lines[12:14]
            ledger_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            refresh_manifest(session_dir)

            with self.assertRaisesRegex(ValueError, "ledger length"):
                analyze_formal_collection(collection)

    def test_mixed_runner_commit_is_rejected_after_hash_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            collection = build_collection(Path(temp_dir))
            session_dir = collection / "session-05-attempt-01"
            preflight_path = session_dir / "preflight.json"
            preflight = read_json(preflight_path)
            preflight["protocol"]["runner_commit"] = "3" * 40
            git = preflight["base"]["environment"]["git"]
            git["commit"] = "3" * 40
            git["upstream_commit"] = "3" * 40
            write_json(preflight_path, preflight)
            manifest_path = session_dir / "manifest.json"
            manifest = read_json(manifest_path)
            manifest["preflight"]["runner_commit"] = "3" * 40
            write_json(manifest_path, manifest)
            refresh_manifest(session_dir)

            with self.assertRaisesRegex(ValueError, "mixed runner commits"):
                analyze_formal_collection(collection)

    def test_unchanged_service_identity_is_rejected_after_hash_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            collection = build_collection(Path(temp_dir))
            first_preflight = read_json(
                collection / "session-01-attempt-01" / "preflight.json"
            )
            session_dir = collection / "session-02-attempt-01"
            preflight_path = session_dir / "preflight.json"
            preflight = read_json(preflight_path)
            preflight["service_identity"]["ollama"] = first_preflight[
                "service_identity"
            ]["ollama"]
            write_json(preflight_path, preflight)
            manifest_path = session_dir / "manifest.json"
            manifest = read_json(manifest_path)
            manifest["preflight"]["service_identity"] = preflight["service_identity"]
            write_json(manifest_path, manifest)
            refresh_manifest(session_dir)

            with self.assertRaisesRegex(ValueError, "services were not restarted"):
                analyze_formal_collection(collection)

    def test_unjoined_worker_is_rejected_after_hash_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            collection = build_collection(Path(temp_dir))
            session_dir = collection / "session-01-attempt-01"
            run_path = next(
                path
                for path in session_dir.glob("measured/*/run.json")
                if read_json(path)["condition"] == "formal_async"
            )
            run = read_json(run_path)
            run["runtime"]["shutdown"]["joined"] = False
            write_json(run_path, run)
            refresh_manifest(session_dir)

            with self.assertRaisesRegex(ValueError, "worker did not close cleanly"):
                analyze_formal_collection(collection)

    def test_shortened_idle_epoch_is_rejected_after_hash_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            collection = build_collection(Path(temp_dir))
            session_dir = collection / "session-01-attempt-01"
            idle_path = session_dir / "idle" / "pre_measurement" / "run.json"
            idle = read_json(idle_path)
            idle["finished_monotonic_ns"] = idle["started_monotonic_ns"] + 1_000_000
            write_json(idle_path, idle)
            refresh_manifest(session_dir)

            with self.assertRaisesRegex(ValueError, "idle duration is shorter"):
                analyze_formal_collection(collection)

    def test_failed_run_gate_is_rejected_after_hash_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            collection = build_collection(Path(temp_dir))
            session_dir = collection / "session-01-attempt-01"
            run_path = next(session_dir.glob("measured/*/run.json"))
            run = read_json(run_path)
            run["gates"][0]["passed"] = False
            write_json(run_path, run)
            refresh_manifest(session_dir)

            with self.assertRaisesRegex(ValueError, "run gate failed"):
                analyze_formal_collection(collection)


if __name__ == "__main__":
    unittest.main()
