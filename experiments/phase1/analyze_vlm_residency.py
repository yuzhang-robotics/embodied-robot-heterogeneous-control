"""Validate and describe the Phase 1 VLM residency-order diagnostic."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Mapping, Sequence

from experiments.phase1.analyze_vlm_pilot import (
    PROCESS_ANALYSIS_KIND,
    _find_run_directories,
    analyze_vlm_pilot_dir,
)
from experiments.phase1.vlm_process_adapter import PROCESS_PROTOCOL_VERSION


VLM_RESIDENCY_ANALYSIS_SCHEMA_VERSION = "0.1.0"
VLM_RESIDENCY_ANALYSIS_KIND = "phase1_vlm_residency_order_diagnostic"
EXPECTED_STAGE_ORDER = (
    "input_verify_before",
    "module_import",
    "moondream_inference",
    "model_unload",
    "qwen_rewrite",
    "output_normalization",
    "input_verify_after",
)
QWEN_REQUEST_TIMEOUT_S = 30
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_TASK_RE = re.compile(r"\btask (?P<task>[0-9]+) \|")


def _normalized_sha256(value: str, name: str) -> str:
    normalized = value.lower()
    if _SHA256_RE.fullmatch(normalized) is None:
        raise ValueError(f"{name} must contain 64 hexadecimal digits")
    return normalized


def _read_object(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    return value


def _llama_server_record(path: Path | str) -> dict[str, object]:
    log_path = Path(path)
    if not log_path.is_file():
        raise ValueError("llama-server log does not exist")
    launches: list[int] = []
    releases: list[int] = []
    cancellation_count = 0
    last_release_line = -1
    idle_lines: list[int] = []
    with log_path.open("r", encoding="utf-8", errors="strict") as stream:
        for index, line in enumerate(stream):
            if "launch_slot_" in line and "processing task" in line:
                match = _TASK_RE.search(line)
                if match is not None:
                    launches.append(int(match.group("task")))
            elif "slot      release:" in line and "stop processing" in line:
                match = _TASK_RE.search(line)
                if match is not None:
                    releases.append(int(match.group("task")))
                    last_release_line = index
            elif "cancel task" in line:
                cancellation_count += 1
            elif "all slots are idle" in line:
                idle_lines.append(index)
    if len(launches) != 2 or len(set(launches)) != 2:
        raise ValueError("llama-server log must contain two distinct launched tasks")
    if releases != launches:
        raise ValueError("llama-server launched and released task order differs")
    if not any(index > last_release_line for index in idle_lines):
        raise ValueError("llama-server log does not return to idle after both tasks")
    return {
        "request_count": len(launches),
        "released_request_count": len(releases),
        "cancellation_record_count": cancellation_count,
        "all_requests_released": releases == launches,
        "idle_after_final_release": True,
        "raw_log_recorded": False,
    }


def _validate_residency_contract(session_dir: Path) -> None:
    isolation, run_dirs = _find_run_directories(session_dir)
    if isolation != "spawned_process":
        raise ValueError("residency diagnostic requires spawned-process isolation")
    for condition, run_dir in run_dirs.items():
        manifest = _read_object(run_dir / "manifest.json")
        contract_value = manifest.get("workload_contract")
        contract = contract_value if isinstance(contract_value, Mapping) else {}
        qwen_value = contract.get("qwen_rewrite")
        qwen = qwen_value if isinstance(qwen_value, Mapping) else {}
        summary = _read_object(run_dir / "summary.json")
        adapter_value = summary.get("adapter")
        adapter = adapter_value if isinstance(adapter_value, Mapping) else {}
        durations_value = adapter.get("stage_durations_ns")
        durations = durations_value if isinstance(durations_value, Mapping) else {}
        statuses_value = adapter.get("stage_status")
        statuses = statuses_value if isinstance(statuses_value, Mapping) else {}
        stage_errors_value = adapter.get("stage_error_codes")
        stage_errors = (
            stage_errors_value if isinstance(stage_errors_value, Mapping) else {}
        )
        scenario = _read_object(run_dir / "scenario.json")
        process_value = scenario.get("process")
        process = process_value if isinstance(process_value, Mapping) else {}
        if (
            contract.get("unload_before_qwen") is not True
            or contract.get("cleanup_unload_on_failure") is not True
            or "unload_after_request" in contract
            or qwen.get("request_timeout_s") != QWEN_REQUEST_TIMEOUT_S
            or set(durations) != set(EXPECTED_STAGE_ORDER)
            or set(statuses) != set(EXPECTED_STAGE_ORDER)
            or any(value != "ok" for value in statuses.values())
            or bool(stage_errors)
            or process.get("protocol_version") != PROCESS_PROTOCOL_VERSION
        ):
            raise ValueError(f"{condition} does not bind the residency-order contract")


def analyze_vlm_residency_diagnostic(
    session_dir: Path | str,
    *,
    llama_log: Path | str,
    source_archive_sha256: str,
    llama_log_archive_sha256: str,
) -> dict[str, object]:
    """Reconstruct one two-run diagnostic without publishing model text or paths."""

    directory = Path(session_dir).resolve()
    _validate_residency_contract(directory)
    pilot = analyze_vlm_pilot_dir(
        directory,
        source_archive_sha256=source_archive_sha256,
    )
    if pilot.get("analysis_kind") != PROCESS_ANALYSIS_KIND:
        raise ValueError("residency diagnostic is not a process-isolated VLM pilot")
    runs_value = pilot.get("runs")
    runs = runs_value if isinstance(runs_value, list) else []
    if len(runs) != 2:
        raise ValueError("residency diagnostic must contain exactly two runs")
    expected_conditions = ["vlm_async", "vlm_stale"]
    if [run.get("condition") for run in runs if isinstance(run, Mapping)] != (
        expected_conditions
    ):
        raise ValueError("residency diagnostic condition order is invalid")

    qwen_durations_ms: list[float] = []
    for run in runs:
        if not isinstance(run, Mapping):
            raise ValueError("residency diagnostic run is not an object")
        result_value = run.get("result")
        result = result_value if isinstance(result_value, Mapping) else {}
        timing_value = run.get("timing")
        timing = timing_value if isinstance(timing_value, Mapping) else {}
        stages_value = timing.get("stages")
        stages = stages_value if isinstance(stages_value, list) else []
        process_value = run.get("process")
        process = process_value if isinstance(process_value, Mapping) else {}
        validation_value = run.get("validation")
        validation = validation_value if isinstance(validation_value, Mapping) else {}
        if [
            stage.get("stage") for stage in stages if isinstance(stage, Mapping)
        ] != list(EXPECTED_STAGE_ORDER):
            raise ValueError("VLM stage order does not match the residency contract")
        if any(
            not isinstance(stage, Mapping) or stage.get("status") != "ok"
            for stage in stages
        ):
            raise ValueError("VLM diagnostic contains a failed stage")
        qwen_stage = next(
            stage
            for stage in stages
            if isinstance(stage, Mapping) and stage.get("stage") == "qwen_rewrite"
        )
        qwen_duration = qwen_stage.get("duration_ms")
        if not isinstance(qwen_duration, (int, float)) or isinstance(
            qwen_duration, bool
        ):
            raise ValueError("Qwen stage duration is invalid")
        qwen_durations_ms.append(float(qwen_duration))
        if (
            result.get("translation_route") != "qwen"
            or process.get("protocol_version") != PROCESS_PROTOCOL_VERSION
            or validation.get("summary_valid") is not True
            or validation.get("all_gates_passed") is not True
            or validation.get("process_summary_valid") is not True
            or validation.get("all_process_gates_passed") is not True
        ):
            raise ValueError("VLM diagnostic run does not satisfy the frozen contract")

    qwen_within_timeout = all(
        duration < QWEN_REQUEST_TIMEOUT_S * 1_000 for duration in qwen_durations_ms
    )
    if not qwen_within_timeout:
        raise ValueError("Qwen stage exceeded the retained request timeout")
    llama = _llama_server_record(llama_log)
    if llama["cancellation_record_count"] != 0:
        raise ValueError("llama-server log contains an unexpected cancellation")
    validation_value = pilot.get("validation")
    validation = validation_value if isinstance(validation_value, Mapping) else {}
    if not all(
        validation.get(name) is True
        for name in (
            "all_runs_valid",
            "correctness_observed",
            "all_process_gates_passed",
            "process_boundary_correctness_observed",
            "periodic_probe_continuity_observed",
        )
    ):
        raise ValueError("VLM diagnostic validation is incomplete")

    source_value = pilot.get("source")
    source = source_value if isinstance(source_value, Mapping) else {}
    return {
        "vlm_residency_analysis_schema_version": (
            VLM_RESIDENCY_ANALYSIS_SCHEMA_VERSION
        ),
        "analysis_kind": VLM_RESIDENCY_ANALYSIS_KIND,
        "session_id": pilot.get("session_id"),
        "source": {
            "git_commit": source.get("git_commit"),
            "git_branch": source.get("git_branch"),
            "source_archive_sha256": _normalized_sha256(
                source_archive_sha256, "source_archive_sha256"
            ),
            "llama_log_archive_sha256": _normalized_sha256(
                llama_log_archive_sha256, "llama_log_archive_sha256"
            ),
            "run_artifacts": source.get("run_artifacts"),
            "source_paths_recorded": False,
        },
        "identity": pilot.get("identity"),
        "design": {
            "role": "descriptive_correctness_diagnostic",
            "condition_order": expected_conditions,
            "run_count": len(runs),
            "fixed_input": True,
            "formal_evidence": False,
        },
        "contract": {
            "adapter_isolation": "spawned_process",
            "process_protocol_version": PROCESS_PROTOCOL_VERSION,
            "stage_order": list(EXPECTED_STAGE_ORDER),
            "moondream_unload_before_qwen": True,
            "cleanup_unload_on_failure": True,
            "qwen_request_timeout_s": QWEN_REQUEST_TIMEOUT_S,
            "model_unload_confirmation_available": False,
        },
        "validation": {
            **dict(validation),
            "residency_contract_verified": True,
            "qwen_route_all_runs": True,
            "all_stages_passed": True,
            "qwen_completed_within_request_timeout": qwen_within_timeout,
            "llama_server": llama,
        },
        "runs": runs,
        "decision": {
            "v3_preregistration_readiness_supported": True,
            "retain_qwen_request_timeout_s": QWEN_REQUEST_TIMEOUT_S,
            "timeout_change_supported": False,
            "v2_reopening_permitted": False,
        },
        "claim_boundary": {
            "descriptive_only": True,
            "residency_order_causality_established": False,
            "performance_comparison_permitted": False,
            "timing_domain_isolation_claim_permitted": False,
            "hard_real_time_claim_permitted": False,
            "heterogeneous_inference_claim_permitted": False,
            "condition_resource_attribution_permitted": False,
            "backend_preemption_established": False,
        },
        "data_quality": pilot.get("data_quality"),
    }


def _stage_duration(run: Mapping[str, object], name: str) -> object:
    timing = run.get("timing")
    timing_record = timing if isinstance(timing, Mapping) else {}
    stages = timing_record.get("stages")
    stage_records = stages if isinstance(stages, list) else []
    for stage in stage_records:
        if isinstance(stage, Mapping) and stage.get("stage") == name:
            return stage.get("duration_ms")
    return None


def _format_ms(value: object) -> str:
    return f"{float(value):.3f}" if isinstance(value, (int, float)) else "n/a"


def render_markdown(analysis: Mapping[str, object]) -> str:
    """Render the diagnostic with an explicit conservative claim boundary."""

    source = analysis["source"]
    contract = analysis["contract"]
    validation = analysis["validation"]
    decision = analysis["decision"]
    runs = analysis["runs"]
    assert isinstance(source, Mapping)
    assert isinstance(contract, Mapping)
    assert isinstance(validation, Mapping)
    assert isinstance(decision, Mapping)
    assert isinstance(runs, list)
    llama = validation["llama_server"]
    assert isinstance(llama, Mapping)
    rows: list[str] = []
    process_rows: list[str] = []
    for run in runs:
        assert isinstance(run, Mapping)
        result = run["result"]
        process = run["process"]
        assert isinstance(result, Mapping)
        assert isinstance(process, Mapping)
        dispositions = result.get("disposition_counts")
        disposition_record = dispositions if isinstance(dispositions, Mapping) else {}
        disposition = ", ".join(
            f"{name}={count}" for name, count in disposition_record.items()
        )
        rows.append(
            "| `{}` | `{}` | {} | {} | {} |".format(
                run.get("condition"),
                result.get("translation_route"),
                _format_ms(_stage_duration(run, "moondream_inference")),
                _format_ms(_stage_duration(run, "model_unload")),
                _format_ms(_stage_duration(run, "qwen_rewrite")),
            )
        )
        process_rows.append(
            "| `{}` | {} | {} | {} | `{}` |".format(
                run.get("condition"),
                disposition,
                result.get("accepted_result_count"),
                process.get("exit_code"),
                process.get("protocol_version"),
            )
        )
    return "\n".join(
        [
            "# Phase 1 VLM Residency-order Diagnostic",
            "",
            "This report reconstructs one motion-disabled, fixed-input diagnostic "
            "of the corrected VLM residency order. It is descriptive readiness "
            "evidence and is not part of a formal comparison.",
            "",
            "## Provenance",
            "",
            "| Field | Value |",
            "| --- | --- |",
            f"| Session | `{analysis.get('session_id')}` |",
            f"| Source commit | `{source.get('git_commit')}` |",
            f"| Collection archive SHA-256 | `{source.get('source_archive_sha256')}` |",
            f"| llama-server log archive SHA-256 | `{source.get('llama_log_archive_sha256')}` |",
            "| Raw inputs, model text, logs or paths published | no |",
            "",
            "All run artifacts were independently validated before these derived "
            "facts were emitted.",
            "",
            "## Frozen diagnostic contract",
            "",
            "The successful path was `Moondream inference -> unload request -> "
            "Qwen rewrite`, using spawned-process protocol "
            f"`{contract.get('process_protocol_version')}` and the existing "
            f"{contract.get('qwen_request_timeout_s')} s Qwen request timeout. "
            "Unload confirmation is not available.",
            "",
            "## Pipeline observations",
            "",
            "| Condition | Route | Moondream (ms) | Unload (ms) | Qwen (ms) |",
            "| --- | --- | ---: | ---: | ---: |",
            *rows,
            "",
            "## Lifecycle observations",
            "",
            "| Condition | Final disposition | Accepted | Child exit | Process protocol |",
            "| --- | --- | ---: | ---: | --- |",
            *process_rows,
            "",
            "Both slice and process Gate sets passed, both children exited "
            "normally, and the stale result was rejected before consumption. The "
            f"llama-server log contains {llama.get('request_count')} completed "
            f"requests and {llama.get('cancellation_record_count')} cancellation "
            "records.",
            "",
            "## Decision",
            "",
            f"The diagnostic supports freezing the corrected order for G6 v3 and "
            f"retaining the {decision.get('retain_qwen_request_timeout_s')} s "
            "Qwen timeout. It does not support changing that threshold or reopening "
            "G6 v2.",
            "",
            "## Claim boundary",
            "",
            "The single run per condition and fixed condition order do not establish "
            "residency-order causality or performance superiority. Device-wide "
            "resources are not attributed to a model or processor. Backend "
            "preemption, timing-domain isolation, hard-real-time behavior and "
            "heterogeneous inference remain unproven.",
            "",
            "`residency_order_causality_established=False`; "
            "`performance_comparison_permitted=False`.",
            "",
        ]
    )


def _refuse_output_inside_session(output: Path | None, session_dir: Path) -> None:
    if output is None:
        return
    resolved = output.resolve()
    try:
        resolved.relative_to(session_dir)
    except ValueError:
        return
    raise ValueError("derived output must not be written inside the source session")


def _write_text_atomic(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(value, encoding="utf-8", newline="\n")
    os.replace(temporary, path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session_dir", type=Path)
    parser.add_argument("--llama-log", type=Path, required=True)
    parser.add_argument("--source-archive-sha256", required=True)
    parser.add_argument("--llama-log-archive-sha256", required=True)
    parser.add_argument("--json-output", type=Path)
    parser.add_argument("--markdown-output", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    session_dir = args.session_dir.resolve()
    try:
        _refuse_output_inside_session(args.json_output, session_dir)
        _refuse_output_inside_session(args.markdown_output, session_dir)
        if (
            args.json_output is not None
            and args.markdown_output is not None
            and args.json_output.resolve() == args.markdown_output.resolve()
        ):
            raise ValueError("JSON and Markdown outputs must be distinct")
        analysis = analyze_vlm_residency_diagnostic(
            session_dir,
            llama_log=args.llama_log,
            source_archive_sha256=args.source_archive_sha256,
            llama_log_archive_sha256=args.llama_log_archive_sha256,
        )
        json_text = json.dumps(
            analysis,
            ensure_ascii=False,
            indent=2,
            allow_nan=False,
        )
        if args.json_output is None:
            print(json_text)
        else:
            _write_text_atomic(args.json_output, json_text + "\n")
        if args.markdown_output is not None:
            _write_text_atomic(
                args.markdown_output,
                render_markdown(analysis).rstrip("\n") + "\n",
            )
    except (OSError, TypeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
