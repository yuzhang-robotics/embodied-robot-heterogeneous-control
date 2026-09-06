"""Validate the target-side Phase 1 VLM timeout-repair evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tarfile
from pathlib import Path, PurePosixPath
from typing import Mapping, Sequence

from experiments.phase1.analyze_vlm_pilot import (
    PROCESS_ANALYSIS_KIND,
    _find_run_directories,
    analyze_vlm_pilot_dir,
)
from experiments.phase1.analyze_vlm_timeout_diagnostic import (
    _llama_server_record,
)
from experiments.phase1.manifest import sha256_file, write_json_atomic
from experiments.phase1.vlm_process_adapter import PROCESS_PROTOCOL_VERSION
from jetson.vlm_request_contract import (
    QWEN_REQUEST_TIMEOUT_S,
    VLM_REQUEST_CONTRACT_VERSION,
    current_vlm_workload_contract,
)


VLM_TIMEOUT_REPAIR_ANALYSIS_SCHEMA_VERSION = "0.1.0"
VLM_TIMEOUT_REPAIR_ANALYSIS_KIND = "phase1_vlm_timeout_repair_validation"
VALIDATION_SESSION_ID = (
    "20260906T101723Z_phase1_vlm_timeout_repair_validation"
)
REPAIR_BASE_COMMIT = "52c041d2969dd8029c00e8c49f2009164c1debf9"
VALIDATION_COMMIT = "9bd2bcec49ad9faca972ffade515eea99fb4e9b2"
EXPECTED_STAGE_ORDER = (
    "input_verify_before",
    "module_import",
    "moondream_inference",
    "model_unload",
    "qwen_rewrite",
    "output_normalization",
    "input_verify_after",
)
EXPECTED_SOURCE_FILES = (
    "jetson/vlm_request_contract.py",
    "jetson/vision_vlm.py",
    "experiments/phase1/vlm_adapter.py",
    "experiments/phase1/run_vlm_slice.py",
    "experiments/phase1/summarize_vlm_slice.py",
    "experiments/phase1/validate_vlm_slice.py",
    "experiments/phase1/tests/test_vlm_request_contract.py",
    "experiments/phase1/tests/test_vlm_adapter.py",
    "experiments/phase1/tests/test_vlm_runner.py",
    "experiments/phase1/tests/test_vlm_slice.py",
    "experiments/phase1/tests/vlm_process_fixture.py",
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _normalized_sha256(value: str, name: str) -> str:
    normalized = value.lower()
    if _SHA256_RE.fullmatch(normalized) is None:
        raise ValueError(f"{name} must contain 64 hexadecimal digits")
    return normalized


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
    return value


def _read_object(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    return value


def _source_bundle_record(
    source_bundle: Path | str,
    repository_root: Path | str,
    *,
    expected_sha256: str,
) -> dict[str, object]:
    bundle = Path(source_bundle).resolve()
    repository = Path(repository_root).resolve()
    if not bundle.is_file():
        raise ValueError("validation source bundle does not exist")
    if not repository.is_dir():
        raise ValueError("repository root does not exist")
    expected_hash = _normalized_sha256(
        expected_sha256,
        "source_bundle_sha256",
    )
    actual_hash = sha256_file(bundle)
    if actual_hash != expected_hash:
        raise ValueError("validation source bundle SHA-256 differs")

    records: dict[str, dict[str, object]] = {}
    with tarfile.open(bundle, "r:gz") as archive:
        members = archive.getmembers()
        names = [member.name for member in members]
        if len(names) != len(set(names)):
            raise ValueError("validation source bundle contains duplicate members")
        if set(names) != set(EXPECTED_SOURCE_FILES):
            raise ValueError("validation source bundle inventory differs")
        for member in members:
            path = PurePosixPath(member.name)
            if (
                not member.isfile()
                or path.is_absolute()
                or ".." in path.parts
                or str(path) != member.name
            ):
                raise ValueError("validation source bundle member is unsafe")
            stream = archive.extractfile(member)
            if stream is None:
                raise ValueError("validation source bundle member is unreadable")
            data = stream.read()
            repository_path = repository.joinpath(*path.parts)
            if not repository_path.is_file():
                raise ValueError(f"repository source is missing: {member.name}")
            repository_data = repository_path.read_bytes()
            if data != repository_data:
                raise ValueError(
                    f"validated source differs from repository: {member.name}"
                )
            records[member.name] = {
                "size_bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
    return {
        "sha256": actual_hash,
        "member_count": len(records),
        "members": {
            name: records[name] for name in sorted(records)
        },
        "matches_repository": True,
        "private_path_recorded": False,
    }


def _validate_repair_contract(session_dir: Path) -> None:
    isolation, run_dirs = _find_run_directories(session_dir)
    if isolation != "spawned_process":
        raise ValueError("repair validation requires spawned-process isolation")
    expected_contract = current_vlm_workload_contract()
    for condition, run_dir in run_dirs.items():
        manifest = _read_object(run_dir / "manifest.json")
        summary = _read_object(run_dir / "summary.json")
        adapter = _mapping(summary.get("adapter"), "summary adapter")
        durations = _mapping(
            adapter.get("stage_durations_ns"),
            "adapter stage durations",
        )
        statuses = _mapping(adapter.get("stage_status"), "adapter stage status")
        stage_errors = _mapping(
            adapter.get("stage_error_codes"),
            "adapter stage error codes",
        )
        residency = _mapping(adapter.get("model_residency"), "model residency")
        if manifest.get("workload_contract") != expected_contract:
            raise ValueError(f"{condition} VLM request contract differs")
        if (
            set(durations) != set(EXPECTED_STAGE_ORDER)
            or set(statuses) != set(EXPECTED_STAGE_ORDER)
            or any(statuses[name] != "ok" for name in EXPECTED_STAGE_ORDER)
            or bool(stage_errors)
        ):
            raise ValueError(f"{condition} VLM stage contract differs")
        if residency != {
            "unload_requested": True,
            "unload_confirmed": True,
        }:
            raise ValueError(f"{condition} does not confirm model unload")


def _stage_duration(run: Mapping[str, object], name: str) -> float:
    timing = _mapping(run.get("timing"), "run timing")
    stages = timing.get("stages")
    if not isinstance(stages, list):
        raise ValueError("run stages must be a list")
    for stage_value in stages:
        stage = _mapping(stage_value, "run stage")
        if stage.get("stage") == name:
            value = stage.get("duration_ms")
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{name} duration is invalid")
            return float(value)
    raise ValueError(f"{name} stage is missing")


def analyze_vlm_timeout_repair(
    session_dir: Path | str,
    *,
    llama_log: Path | str,
    source_bundle: Path | str,
    repository_root: Path | str,
    collection_archive_sha256: str,
    llama_log_archive_sha256: str,
    source_bundle_sha256: str,
) -> dict[str, object]:
    """Reconstruct the target validation and bind it to the reviewed sources."""

    directory = Path(session_dir).resolve()
    if not directory.is_dir() or directory.name != VALIDATION_SESSION_ID:
        raise ValueError("repair validation session identity differs")
    collection_hash = _normalized_sha256(
        collection_archive_sha256,
        "collection_archive_sha256",
    )
    log_archive_hash = _normalized_sha256(
        llama_log_archive_sha256,
        "llama_log_archive_sha256",
    )
    _validate_repair_contract(directory)
    pilot = analyze_vlm_pilot_dir(
        directory,
        source_archive_sha256=collection_hash,
    )
    if pilot.get("analysis_kind") != PROCESS_ANALYSIS_KIND:
        raise ValueError("repair validation is not process isolated")
    source = _mapping(pilot.get("source"), "pilot source")
    if (
        source.get("git_commit") != VALIDATION_COMMIT
        or source.get("git_branch") != "main"
    ):
        raise ValueError("repair validation source identity differs")
    source_bundle_record = _source_bundle_record(
        source_bundle,
        repository_root,
        expected_sha256=source_bundle_sha256,
    )

    runs_value = pilot.get("runs")
    if not isinstance(runs_value, list) or len(runs_value) != 2:
        raise ValueError("repair validation must contain exactly two runs")
    runs = [_mapping(run, "repair validation run") for run in runs_value]
    if [run.get("condition") for run in runs] != ["vlm_async", "vlm_stale"]:
        raise ValueError("repair validation condition order differs")
    expected_outcomes = {
        "vlm_async": ("ok", {"consumed": 1}, 1),
        "vlm_stale": ("cancel_observed", {"rejected_state": 1}, 0),
    }
    qwen_durations: list[float] = []
    unload_durations: list[float] = []
    for run in runs:
        condition = str(run.get("condition"))
        result = _mapping(run.get("result"), "run result")
        validation = _mapping(run.get("validation"), "run validation")
        process = _mapping(run.get("process"), "run process")
        timing = _mapping(run.get("timing"), "run timing")
        probe = _mapping(timing.get("probe"), "run probe")
        stages_value = timing.get("stages")
        stages = (
            [_mapping(stage, "run stage") for stage in stages_value]
            if isinstance(stages_value, list)
            else []
        )
        outcome, dispositions, accepted = expected_outcomes[condition]
        if [stage.get("stage") for stage in stages] != list(EXPECTED_STAGE_ORDER):
            raise ValueError(f"{condition} stage order differs")
        if any(stage.get("status") != "ok" for stage in stages):
            raise ValueError(f"{condition} contains a failed VLM stage")
        if (
            result.get("execution_outcome") != outcome
            or result.get("translation_route") != "qwen"
            or result.get("disposition_counts") != dispositions
            or result.get("accepted_result_count") != accepted
            or result.get("stale_consumed_count") != 0
            or result.get("raw_text_recorded") is not False
            or result.get("model_unload")
            != {"unload_requested": True, "unload_confirmed": True}
        ):
            raise ValueError(f"{condition} correctness result differs")
        if not all(
            validation.get(name) is True
            for name in (
                "summary_valid",
                "all_gates_passed",
                "real_vlm_path_executed",
                "process_summary_valid",
                "all_process_gates_passed",
            )
        ):
            raise ValueError(f"{condition} validation Gates are incomplete")
        gate_results = _mapping(process.get("gate_results"), "process Gates")
        if (
            process.get("protocol_version") != PROCESS_PROTOCOL_VERSION
            or process.get("start_method") != "spawn"
            or process.get("protocol_complete") is not True
            or process.get("exit_code") != 0
            or process.get("terminate_requested") is not False
            or process.get("error_code") is not None
            or not gate_results
            or not all(value is True for value in gate_results.values())
        ):
            raise ValueError(f"{condition} process lifecycle differs")
        if (
            probe.get("skipped_releases") != 0
            or probe.get("deadline_miss_count") != 0
            or probe.get("joined") is not True
        ):
            raise ValueError(f"{condition} periodic probe continuity failed")
        qwen_durations.append(_stage_duration(run, "qwen_rewrite"))
        unload_durations.append(_stage_duration(run, "model_unload"))
    if any(duration >= QWEN_REQUEST_TIMEOUT_S * 1_000 for duration in qwen_durations):
        raise ValueError("Qwen stage exceeded the repaired request timeout")

    validation = _mapping(pilot.get("validation"), "pilot validation")
    if not all(
        validation.get(name) is True
        for name in (
            "all_runs_valid",
            "correctness_observed",
            "listener_binding_evidence_complete",
            "all_process_gates_passed",
            "process_boundary_correctness_observed",
            "periodic_probe_continuity_observed",
        )
    ):
        raise ValueError("repair validation summary is incomplete")
    quality = _mapping(pilot.get("data_quality"), "pilot data quality")
    limitations = quality.get("limitations")
    if not isinstance(limitations, list):
        raise ValueError("repair validation limitations are invalid")
    if "model_unload_not_independently_confirmed" in limitations:
        raise ValueError("repair validation did not preserve unload confirmation")

    log_path = Path(llama_log).resolve()
    if not log_path.is_file():
        raise ValueError("llama-server log does not exist")
    llama = _llama_server_record(log_path, expected_requests=2)
    llama["content_sha256"] = sha256_file(log_path)

    return {
        "analysis_schema_version": VLM_TIMEOUT_REPAIR_ANALYSIS_SCHEMA_VERSION,
        "analysis_kind": VLM_TIMEOUT_REPAIR_ANALYSIS_KIND,
        "validation_id": VALIDATION_SESSION_ID,
        "source": {
            "repair_base_commit": REPAIR_BASE_COMMIT,
            "validation_commit": VALIDATION_COMMIT,
            "validation_commit_role": "temporary_target_validation_checkout",
            "collection_archive_sha256": collection_hash,
            "llama_log_archive_sha256": log_archive_hash,
            "source_bundle": source_bundle_record,
            "run_artifacts": source.get("run_artifacts"),
            "raw_artifacts_recorded": False,
            "private_paths_recorded": False,
        },
        "identity": pilot.get("identity"),
        "contract": {
            "request_contract_version": VLM_REQUEST_CONTRACT_VERSION,
            "workload": current_vlm_workload_contract(),
            "adapter_isolation": "spawned_process",
            "process_protocol_version": PROCESS_PROTOCOL_VERSION,
            "condition_order": ["vlm_async", "vlm_stale"],
            "stage_order": list(EXPECTED_STAGE_ORDER),
            "motion_enabled": False,
            "uart_accessed": False,
        },
        "validation": {
            **dict(validation),
            "repair_contract_verified": True,
            "source_bundle_matches_repository": True,
            "all_model_unloads_confirmed": True,
            "all_qwen_requests_within_repaired_timeout": True,
            "llama_server": llama,
        },
        "runs": list(runs),
        "observations": {
            "qwen_rewrite_ms": qwen_durations,
            "model_unload_ms": unload_durations,
            "qwen_request_timeout_s": QWEN_REQUEST_TIMEOUT_S,
            "llama_server_request_count": llama["request_count"],
            "llama_server_cancellation_record_count": (
                llama["cancellation_record_count"]
            ),
        },
        "decision": {
            "repair_validated_on_target": True,
            "repair_ready_for_review": True,
            "g6_v3_remains_closed": True,
            "phase1_complete": False,
            "successor_formal_protocol_required": True,
            "successor_formal_protocol_active": False,
            "formal_collection_authorized": False,
            "application_slice_authorized": False,
        },
        "claim_boundary": {
            "design_role": "nonformal_target_repair_validation",
            "descriptive_only": True,
            "repair_path_correctness_observed": True,
            "performance_comparison_permitted": False,
            "timing_domain_isolation_claim_permitted": False,
            "hard_real_time_claim_permitted": False,
            "heterogeneous_inference_claim_permitted": False,
            "condition_resource_attribution_permitted": False,
            "backend_preemption_established": False,
        },
        "data_quality": {
            **dict(quality),
            "source_bundle_member_count": source_bundle_record["member_count"],
        },
    }


def _format_ms(value: object) -> str:
    return f"{float(value):.3f}" if isinstance(value, (int, float)) else "n/a"


def render_markdown(analysis: Mapping[str, object]) -> str:
    """Render the repair validation without expanding its claim boundary."""

    source = _mapping(analysis.get("source"), "analysis source")
    validation = _mapping(analysis.get("validation"), "analysis validation")
    observations = _mapping(analysis.get("observations"), "observations")
    decision = _mapping(analysis.get("decision"), "decision")
    bundle = _mapping(source.get("source_bundle"), "source bundle")
    llama = _mapping(validation.get("llama_server"), "llama-server record")
    runs_value = analysis.get("runs")
    if not isinstance(runs_value, list):
        raise ValueError("analysis runs must be a list")
    rows: list[str] = []
    for run_value in runs_value:
        run = _mapping(run_value, "analysis run")
        result = _mapping(run.get("result"), "run result")
        process = _mapping(run.get("process"), "run process")
        resources = _mapping(run.get("resources"), "run resources")
        temperatures = _mapping(
            resources.get("junction_temperature_c"),
            "temperature observations",
        )
        rows.append(
            "| `{}` | `{}` | `{}` | {} | {} | {} | {} |".format(
                run.get("condition"),
                result.get("execution_outcome"),
                result.get("translation_route"),
                _format_ms(_stage_duration(run, "model_unload")),
                _format_ms(_stage_duration(run, "qwen_rewrite")),
                resources.get("sample_count"),
                _format_ms(temperatures.get("max")),
            )
        )
        assert process.get("exit_code") == 0
    return "\n".join(
        [
            "# Phase 1 VLM Timeout-repair Target Validation",
            "",
            "This report reconstructs direct Jetson execution of the modified "
            "repository VLM path. It is a motion-disabled, nonformal repair "
            "validation and does not replace or reopen G6 v3.",
            "",
            "## Provenance and integrity",
            "",
            "| Field | Value |",
            "| --- | --- |",
            f"| Validation | `{analysis.get('validation_id')}` |",
            f"| Repair base commit | `{source.get('repair_base_commit')}` |",
            f"| Temporary validation commit | `{source.get('validation_commit')}` |",
            "| Collection archive SHA-256 | "
            f"`{source.get('collection_archive_sha256')}` |",
            "| llama-server log archive SHA-256 | "
            f"`{source.get('llama_log_archive_sha256')}` |",
            f"| Validation source bundle SHA-256 | `{bundle.get('sha256')}` |",
            "| Source files matched to this repository | "
            f"{bundle.get('member_count')} / {bundle.get('member_count')} |",
            "| Raw inputs, prompts, model text, logs or private paths published | no |",
            "",
            "The source bundle was applied to a clean temporary Jetson checkout "
            "from the recorded repair base. Its validation-only commit is provenance "
            "for the target run and is not part of the project history. Every bundled "
            "source file is byte-identical to the corresponding reviewed file.",
            "",
            "## Contract exercised",
            "",
            "Both conditions used the fixed C100 input, spawned-process protocol "
            f"`{PROCESS_PROTOCOL_VERSION}`, deterministic temperatures and seed, "
            "the existing prompts and token bounds, `Moondream -> confirmed unload "
            f"-> Qwen` order, and the repaired {QWEN_REQUEST_TIMEOUT_S} s Qwen "
            "client boundary. Physical motion remained disabled and UART was not "
            "accessed.",
            "",
            "## Target observations",
            "",
            "| Condition | Outcome | Route | Confirmed unload (ms) | Qwen (ms) "
            "| Resource samples | Max Tj (C) |",
            "| --- | --- | --- | ---: | ---: | ---: | ---: |",
            *rows,
            "",
            "Both independent slice validators returned valid results. All slice "
            "and process Gates passed, the nominal result was consumed once, the "
            "stale result was rejected before consumption, both child processes "
            "exited normally, and both unload operations recorded positive "
            "Ollama process-list absence confirmation.",
            "",
            f"The llama-server log contains {llama.get('request_count')} launches "
            f"and {llama.get('released_request_count')} matching releases, with "
            "zero cancellation, timeout or error records, and returns to idle after "
            "the final request.",
            "",
            "## Decision and boundary",
            "",
            "The modified repository repair path is validated on the target and is "
            "ready for review. This single fixed-order run per condition establishes "
            "repair-path correctness only; it is not a synchronous/asynchronous "
            "performance comparison and does not establish backend preemption, "
            "timing-domain isolation, hard-real-time behavior, heterogeneous "
            "inference or condition-level resource attribution.",
            "",
            "G6 v3 remains permanently closed and immutable. Phase 1 remains "
            "incomplete: no successor formal protocol is active, no formal "
            "collection is authorized, and the application slice remains blocked "
            "until a later reviewed formal result satisfies the completion Gates.",
            "",
        ]
    )


def _refuse_output_inside_source(output: Path | None, roots: Sequence[Path]) -> None:
    if output is None:
        return
    resolved = output.resolve()
    for root in roots:
        if resolved == root or resolved.is_relative_to(root):
            raise ValueError("derived output must remain outside private sources")


def _write_text_atomic(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(value, encoding="utf-8", newline="\n")
    os.replace(temporary, path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session_dir", type=Path)
    parser.add_argument("--llama-log", type=Path, required=True)
    parser.add_argument("--source-bundle", type=Path, required=True)
    parser.add_argument("--repository-root", type=Path, required=True)
    parser.add_argument("--collection-archive-sha256", required=True)
    parser.add_argument("--llama-log-archive-sha256", required=True)
    parser.add_argument("--source-bundle-sha256", required=True)
    parser.add_argument("--json-output", type=Path)
    parser.add_argument("--markdown-output", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        private_roots = (
            args.session_dir.resolve(),
            args.source_bundle.resolve(),
            args.llama_log.resolve(),
        )
        _refuse_output_inside_source(args.json_output, private_roots)
        _refuse_output_inside_source(args.markdown_output, private_roots)
        if (
            args.json_output is not None
            and args.markdown_output is not None
            and args.json_output.resolve() == args.markdown_output.resolve()
        ):
            raise ValueError("JSON and Markdown outputs must be distinct")
        analysis = analyze_vlm_timeout_repair(
            args.session_dir,
            llama_log=args.llama_log,
            source_bundle=args.source_bundle,
            repository_root=args.repository_root,
            collection_archive_sha256=args.collection_archive_sha256,
            llama_log_archive_sha256=args.llama_log_archive_sha256,
            source_bundle_sha256=args.source_bundle_sha256,
        )
        if args.json_output is not None:
            write_json_atomic(args.json_output, analysis)
        if args.markdown_output is not None:
            _write_text_atomic(args.markdown_output, render_markdown(analysis))
        if args.json_output is None and args.markdown_output is None:
            print(json.dumps(analysis, ensure_ascii=False, indent=2))
    except (OSError, UnicodeError, ValueError, tarfile.TarError) as exc:
        print(
            f"VLM timeout repair analysis failed: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
