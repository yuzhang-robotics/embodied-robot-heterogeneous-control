"""Validate and describe the Phase 1 VLM timeout-repair diagnostic."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from statistics import mean
from typing import Mapping, Sequence

from experiments.phase1.jetson_telemetry import parse_tegrastats_line
from experiments.phase1.manifest import sha256_file, write_json_atomic
from experiments.phase1.vlm_adapter import (
    C100_INPUT_SHA256,
    C100_INPUT_SIZE_BYTES,
)


VLM_TIMEOUT_DIAGNOSTIC_ANALYSIS_SCHEMA_VERSION = "0.1.0"
VLM_TIMEOUT_DIAGNOSTIC_ANALYSIS_KIND = "phase1_vlm_timeout_repair_diagnostic"
DIAGNOSTIC_SCHEMA_VERSION = "0.1.0"
DIAGNOSTIC_DESIGN_ROLE = "descriptive_vlm_timeout_repair_diagnostic"
DIAGNOSTIC_SOURCE_COMMIT = "52c041d2969dd8029c00e8c49f2009164c1debf9"
EXPECTED_FILES = frozenset(
    {
        "llama-models.json",
        "llama-server.log",
        "llama-server.pid",
        "ollama-tags.json",
        "results.json",
        "tegrastats.log",
    }
)
EXPECTED_REQUEST_CONTRACT = {
    "llama_server_arguments_changed": False,
    "moondream_num_predict": 100,
    "moondream_temperature": 0.0,
    "moondream_timeout_s": 180,
    "qwen_max_tokens": 96,
    "qwen_temperature": 0.0,
    "qwen_timeout_s": 60,
    "seed": 20_260_906,
    "unload_confirmation": "ollama_process_list_absence",
}
EXPECTED_QWEN_USAGE = {
    "completion_tokens": 32,
    "prompt_tokens": 164,
    "total_tokens": 196,
}
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_TASK_RE = re.compile(r"\btask (?P<task>[0-9]+) \|")
_TIMING_RE = re.compile(
    r"=\s*(?P<milliseconds>[0-9.]+)\s*ms\s*/\s*(?P<units>[0-9]+)"
)


def _read_object(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    return value


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
    return value


def _finite_number(value: object, name: str, *, positive: bool = False) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or (value <= 0 if positive else value < 0)
    ):
        qualifier = "positive" if positive else "non-negative"
        raise ValueError(f"{name} must be a finite {qualifier} number")
    return float(value)


def _nonnegative_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _sha256(value: object, name: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value.lower()) is None:
        raise ValueError(f"{name} must contain 64 hexadecimal digits")
    return value.lower()


def _timestamp(value: object, name: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{name} must contain a timezone")
    return parsed


def _validate_inventory(directory: Path) -> dict[str, object]:
    names = {path.name for path in directory.iterdir() if path.is_file()}
    if names != EXPECTED_FILES:
        raise ValueError("diagnostic file inventory differs from the recorded design")
    if any(path.is_dir() for path in directory.iterdir()):
        raise ValueError("diagnostic directory contains an unexpected subdirectory")
    try:
        server_pid = int((directory / "llama-server.pid").read_text().strip())
    except ValueError as exc:
        raise ValueError("llama-server.pid is invalid") from exc
    if server_pid <= 0:
        raise ValueError("llama-server.pid is invalid")
    return {
        "file_count": len(names),
        "file_sha256": {
            name: sha256_file(directory / name) for name in sorted(names)
        },
        "llama_server_pid_record_valid": True,
    }


def _validate_model_inventory(directory: Path) -> dict[str, object]:
    ollama = _read_object(directory / "ollama-tags.json")
    ollama_models = ollama.get("models")
    if not isinstance(ollama_models, list):
        raise ValueError("Ollama inventory does not contain a model list")
    installed: list[dict[str, object]] = []
    for item in ollama_models:
        model = _mapping(item, "Ollama model record")
        name = model.get("name")
        digest = model.get("digest")
        if not isinstance(name, str) or not name:
            raise ValueError("Ollama model name is invalid")
        installed.append(
            {
                "name": name,
                "digest": _sha256(digest, f"{name} digest"),
            }
        )
    expected_ollama = {"moondream:latest", "qwen2.5vl:3b"}
    if {item["name"] for item in installed} != expected_ollama:
        raise ValueError("Ollama model inventory differs from the diagnostic design")

    llama = _read_object(directory / "llama-models.json")
    models = llama.get("models")
    if not isinstance(models, list) or len(models) != 1:
        raise ValueError("llama-server inventory must contain one model")
    model = _mapping(models[0], "llama-server model record")
    name = model.get("name")
    if name != "qwen2.5-1.5b-instruct-q4_k_m.gguf":
        raise ValueError("llama-server model identity differs")
    return {
        "ollama_models": sorted(installed, key=lambda item: str(item["name"])),
        "llama_server_model": name,
    }


def _validate_records(result: Mapping[str, object]) -> dict[str, object]:
    records_value = result.get("records")
    if not isinstance(records_value, list) or len(records_value) != 3:
        raise ValueError("diagnostic must contain exactly three records")

    records: list[dict[str, object]] = []
    previous_completed: datetime | None = None
    for index, value in enumerate(records_value, start=1):
        record = _mapping(value, f"diagnostic record {index}")
        if record.get("repetition") != index:
            raise ValueError("diagnostic repetition order is invalid")
        if record.get("status") != "completed":
            raise ValueError(f"diagnostic repetition {index} did not complete")
        if record.get("error_stage") is not None or record.get("error_code") is not None:
            raise ValueError(f"diagnostic repetition {index} contains an error")
        started = _timestamp(record.get("started_at"), "record start")
        completed = _timestamp(record.get("completed_at"), "record completion")
        if completed < started or (
            previous_completed is not None and started < previous_completed
        ):
            raise ValueError("diagnostic record chronology is invalid")
        previous_completed = completed

        description = _mapping(record.get("description"), "description record")
        if description.get("raw_text_recorded") is not False:
            raise ValueError("description raw-text boundary is invalid")
        description_record = {
            "sha256": _sha256(description.get("sha256"), "description SHA-256"),
            "characters": _nonnegative_int(
                description.get("characters"), "description characters"
            ),
            "bytes": _nonnegative_int(description.get("bytes"), "description bytes"),
            "raw_text_recorded": False,
        }

        moondream = _mapping(record.get("moondream"), "Moondream timings")
        moondream_record = {
            key: _finite_number(moondream.get(key), f"Moondream {key}", positive=True)
            for key in (
                "client_ms",
                "generation_ms",
                "load_ms",
                "prompt_eval_ms",
                "total_ms",
            )
        }
        moondream_record.update(
            {
                key: _nonnegative_int(moondream.get(key), f"Moondream {key}")
                for key in ("eval_count", "prompt_eval_count")
            }
        )

        unload = _mapping(record.get("model_unload"), "model unload record")
        if unload.get("requested") is not True or unload.get("confirmed") is not True:
            raise ValueError(f"diagnostic repetition {index} did not confirm unload")
        unload_record = {
            "requested": True,
            "confirmed": True,
            "duration_ms": _finite_number(
                unload.get("duration_ms"), "model unload duration", positive=True
            ),
        }

        qwen = _mapping(record.get("qwen"), "Qwen record")
        usage = dict(_mapping(qwen.get("usage"), "Qwen usage"))
        if usage != EXPECTED_QWEN_USAGE:
            raise ValueError("Qwen request size differs from the diagnostic design")
        qwen_ms = _finite_number(qwen.get("client_ms"), "Qwen client time", positive=True)
        output = _mapping(qwen.get("output"), "Qwen output identity")
        if output.get("raw_text_recorded") is not False:
            raise ValueError("Qwen raw-text boundary is invalid")
        output_record = {
            "sha256": _sha256(output.get("sha256"), "Qwen output SHA-256"),
            "characters": _nonnegative_int(
                output.get("characters"), "Qwen output characters"
            ),
            "bytes": _nonnegative_int(output.get("bytes"), "Qwen output bytes"),
            "raw_text_recorded": False,
        }
        records.append(
            {
                "repetition": index,
                "duration_ms": round((completed - started).total_seconds() * 1000, 3),
                "description": description_record,
                "moondream": moondream_record,
                "model_unload": unload_record,
                "qwen": {
                    "client_ms": qwen_ms,
                    "usage": usage,
                    "output": output_record,
                },
            }
        )

    description_ids = {str(item["description"]["sha256"]) for item in records}
    output_ids = {str(item["qwen"]["output"]["sha256"]) for item in records}
    qwen_times = [float(item["qwen"]["client_ms"]) for item in records]
    summary = _mapping(result.get("summary"), "diagnostic summary")
    expected_summary = {
        "all_completed": True,
        "all_unloads_confirmed": True,
        "completed_repetitions": 3,
        "description_identity_count": len(description_ids),
        "qwen_client_max_ms": max(qwen_times),
        "qwen_client_min_ms": min(qwen_times),
        "qwen_output_identity_count": len(output_ids),
        "qwen_requests_over_legacy_timeout": sum(
            value >= 30_000 for value in qwen_times
        ),
        "requested_repetitions": 3,
    }
    if dict(summary) != expected_summary:
        raise ValueError("diagnostic summary does not match the record reconstruction")
    if len(description_ids) != 1:
        raise ValueError("Moondream description identity was not stable")
    return {
        "records": records,
        "summary": expected_summary,
    }


def _validate_results(directory: Path) -> dict[str, object]:
    result = _read_object(directory / "results.json")
    if result.get("diagnostic_schema_version") != DIAGNOSTIC_SCHEMA_VERSION:
        raise ValueError("diagnostic schema version differs")
    if result.get("design_role") != DIAGNOSTIC_DESIGN_ROLE:
        raise ValueError("diagnostic design role differs")
    if result.get("formal_evidence_eligible") is not False:
        raise ValueError("diagnostic must not be marked as formal evidence")
    if (
        result.get("raw_model_text_recorded") is not False
        or result.get("raw_prompt_recorded") is not False
    ):
        raise ValueError("diagnostic privacy boundary is invalid")
    _timestamp(result.get("created_at"), "diagnostic creation")
    safety = _mapping(result.get("safety"), "safety record")
    if dict(safety) != {
        "motion_enabled": False,
        "motion_environment_value": "0",
        "uart_accessed": False,
    }:
        raise ValueError("diagnostic safety record is invalid")
    input_record = _mapping(result.get("input"), "input record")
    if dict(input_record) != {
        "path_recorded": False,
        "sha256": C100_INPUT_SHA256,
        "size_bytes": C100_INPUT_SIZE_BYTES,
    }:
        raise ValueError("diagnostic fixed-input identity differs")
    request_contract = _mapping(result.get("request_contract"), "request contract")
    if dict(request_contract) != EXPECTED_REQUEST_CONTRACT:
        raise ValueError("diagnostic request contract differs")
    reconstructed = _validate_records(result)
    return {
        "created_at": result["created_at"],
        "safety": dict(safety),
        "input": dict(input_record),
        "request_contract": dict(request_contract),
        **reconstructed,
    }


def _llama_server_record(
    path: Path,
    *,
    expected_requests: int = 3,
) -> dict[str, object]:
    launches: list[int] = []
    releases: list[int] = []
    server_total_ms: dict[int, float] = {}
    generation_tokens: dict[int, int] = {}
    prompt_eval_tokens: dict[int, int] = {}
    cancellation_count = 0
    timeout_count = 0
    error_count = 0
    idle_after_release = False
    last_release_line = -1
    with path.open("r", encoding="utf-8", errors="strict") as stream:
        lines = list(stream)
    for line_number, line in enumerate(lines):
        task_match = _TASK_RE.search(line)
        if "launch_slot_" in line and "processing task" in line and task_match:
            launches.append(int(task_match.group("task")))
        elif "slot      release:" in line and "stop processing" in line and task_match:
            releases.append(int(task_match.group("task")))
            last_release_line = line_number
        elif "cancel task" in line or "should_stop" in line:
            cancellation_count += 1
        if "timeout" in line.lower():
            timeout_count += 1
        if re.search(r"\b(error|exception|failed)\b", line, flags=re.IGNORECASE):
            error_count += 1
        if task_match is not None:
            task = int(task_match.group("task"))
            timing = _TIMING_RE.search(line)
            if timing is not None:
                milliseconds = float(timing.group("milliseconds"))
                units = int(timing.group("units"))
                if "prompt eval time" in line:
                    prompt_eval_tokens[task] = units
                elif "       eval time" in line:
                    generation_tokens[task] = units
                elif "total time" in line:
                    server_total_ms[task] = milliseconds
        if "all slots are idle" in line and line_number > last_release_line >= 0:
            idle_after_release = True
    if len(launches) != expected_requests or len(set(launches)) != expected_requests:
        raise ValueError(
            f"llama-server log must contain {expected_requests} distinct requests"
        )
    if releases != launches:
        raise ValueError("llama-server launch and release order differs")
    if set(server_total_ms) != set(launches):
        raise ValueError("llama-server timing records are incomplete")
    if set(generation_tokens) != set(launches) or any(
        generation_tokens[task] != 32 for task in launches
    ):
        raise ValueError("llama-server generation count differs")
    if prompt_eval_tokens.get(launches[0]) != 164:
        raise ValueError("first llama-server prompt count differs")
    if cancellation_count or timeout_count or error_count:
        raise ValueError("llama-server log contains a cancellation, timeout or error")
    if not idle_after_release:
        raise ValueError("llama-server did not return to idle")
    return {
        "line_count": len(lines),
        "request_count": len(launches),
        "released_request_count": len(releases),
        "cancellation_record_count": cancellation_count,
        "timeout_record_count": timeout_count,
        "error_record_count": error_count,
        "all_requests_released": True,
        "idle_after_final_release": True,
        "server_total_ms": [server_total_ms[task] for task in launches],
        "first_request_prompt_tokens": prompt_eval_tokens[launches[0]],
        "generation_tokens": [generation_tokens[task] for task in launches],
        "subsequent_prompt_eval_tokens": [
            prompt_eval_tokens[task] for task in launches[1:]
        ],
        "raw_log_recorded": False,
    }


def _resource_record(path: Path) -> dict[str, object]:
    samples: list[dict[str, object]] = []
    with path.open("r", encoding="utf-8", errors="strict") as stream:
        for sequence, line in enumerate(stream):
            sample = parse_tegrastats_line(
                line,
                sequence=sequence,
                sample_monotonic_ns=sequence * 200_000_000,
                sample_wall_time_ns=sequence * 200_000_000,
            )
            if sample["parse_errors"]:
                raise ValueError(
                    f"tegrastats line {sequence + 1} has parse errors: "
                    + ", ".join(sample["parse_errors"])
                )
            samples.append(sample)
    if not samples:
        raise ValueError("tegrastats log is empty")
    ram = [int(_mapping(sample["ram"], "RAM record")["used_mb"]) for sample in samples]
    gr3d = [
        int(_mapping(sample["gr3d"], "GR3D record")["usage_pct"])
        for sample in samples
    ]
    temperatures = [
        float(_mapping(sample["temperatures_c"], "temperature record")["tj"])
        for sample in samples
    ]
    power = [
        int(
            _mapping(
                _mapping(sample["power"], "power record")["VDD_IN"],
                "VDD_IN record",
            )["instant_mw"]
        )
        for sample in samples
    ]
    return {
        "sample_count": len(samples),
        "parse_error_count": 0,
        "first_tegrastats_time": samples[0]["tegrastats_time"],
        "last_tegrastats_time": samples[-1]["tegrastats_time"],
        "ram_used_mb": {"min": min(ram), "max": max(ram)},
        "gr3d_usage_pct": {
            "mean": round(mean(gr3d), 6),
            "max": max(gr3d),
        },
        "maximum_tj_c": max(temperatures),
        "vdd_in_instant_mw": {
            "mean": round(mean(power), 6),
            "max": max(power),
        },
        "raw_telemetry_recorded": False,
    }


def analyze_vlm_timeout_diagnostic(
    diagnostic_dir: Path | str,
    *,
    source_archive_sha256: str,
) -> dict[str, object]:
    """Reconstruct the diagnostic without publishing model text or private paths."""

    directory = Path(diagnostic_dir).resolve()
    if not directory.is_dir():
        raise ValueError("diagnostic directory does not exist")
    archive_sha256 = _sha256(source_archive_sha256, "source archive SHA-256")
    integrity = _validate_inventory(directory)
    models = _validate_model_inventory(directory)
    result = _validate_results(directory)
    server = _llama_server_record(directory / "llama-server.log")
    resources = _resource_record(directory / "tegrastats.log")
    records = result["records"]
    client_ms = [float(record["qwen"]["client_ms"]) for record in records]
    server_ms = [float(value) for value in server["server_total_ms"]]
    return {
        "analysis_schema_version": VLM_TIMEOUT_DIAGNOSTIC_ANALYSIS_SCHEMA_VERSION,
        "analysis_kind": VLM_TIMEOUT_DIAGNOSTIC_ANALYSIS_KIND,
        "source": {
            "diagnostic_id": directory.name,
            "source_archive_sha256": archive_sha256,
            "operator_recorded_source_commit": DIAGNOSTIC_SOURCE_COMMIT,
            "raw_artifacts_recorded": False,
            "private_paths_recorded": False,
        },
        "integrity": integrity,
        "design": {
            "role": DIAGNOSTIC_DESIGN_ROLE,
            "formal_evidence_eligible": False,
            "repository_adapter_executed": False,
            "request_contract": result["request_contract"],
            "safety": result["safety"],
        },
        "input": result["input"],
        "models": models,
        "runs": records,
        "reconstruction": {
            **result["summary"],
            "all_qwen_requests_below_legacy_timeout": all(
                value < 30_000 for value in client_ms
            ),
            "qwen_client_ms": client_ms,
            "model_unload_ms": [
                float(record["model_unload"]["duration_ms"]) for record in records
            ],
            "client_minus_server_ms": [
                round(client - service, 6)
                for client, service in zip(client_ms, server_ms, strict=True)
            ],
        },
        "llama_server": server,
        "resources": resources,
        "decision": {
            "repair_contract_supported": True,
            "qwen_timeout_extension_supported": True,
            "deterministic_request_size_supported": True,
            "model_unload_polling_supported": True,
            "llama_server_argument_change_supported": False,
            "actual_repaired_path_validation_required": True,
            "formal_collection_authorized": False,
            "phase1_complete": False,
            "g6_v3_remains_closed": True,
        },
        "evidence_gaps": [
            "descriptive_three_run_diagnostic",
            "inline_contract_replica_not_repository_adapter",
            "unload_process_list_responses_not_retained",
            "qwen_output_identity_not_fixed",
            "formal_evidence_ineligible",
        ],
    }


def render_markdown(analysis: Mapping[str, object]) -> str:
    source = _mapping(analysis["source"], "analysis source")
    integrity = _mapping(analysis["integrity"], "analysis integrity")
    reconstruction = _mapping(analysis["reconstruction"], "reconstruction")
    server = _mapping(analysis["llama_server"], "llama-server analysis")
    resources = _mapping(analysis["resources"], "resource analysis")
    decision = _mapping(analysis["decision"], "decision")
    runs = analysis["runs"]
    assert isinstance(runs, list)
    lines = [
        "# Phase 1 VLM Timeout-repair Diagnostic",
        "",
        "This report independently reconstructs a three-repetition, motion-disabled "
        "Jetson diagnostic of the proposed VLM request and residency contract. It is "
        "descriptive repair evidence, not formal G6 evidence.",
        "",
        "## Source and integrity",
        "",
        "| Field | Value |",
        "| --- | --- |",
        f"| Diagnostic | `{source['diagnostic_id']}` |",
        f"| Operator-recorded source commit | `{source['operator_recorded_source_commit']}` |",
        f"| Transfer archive SHA-256 | `{source['source_archive_sha256']}` |",
        f"| Files independently hashed | {integrity['file_count']} |",
        f"| Formal evidence eligible | no |",
        "| Physical motion / UART | disabled / not accessed |",
        "",
        "Raw prompts, model text, service logs, telemetry and private paths are not "
        "included in this report or its machine-readable derivative.",
        "",
        "## Repair contract exercised",
        "",
        "The diagnostic used temperature `0.0` and seed `20260906` for both model "
        "requests, retained the existing prompts, models and output-token bounds, "
        "extended only the Qwen client timeout from 30 s to 60 s, and polled the "
        "Ollama process list after each Moondream stop request. The llama-server "
        "arguments were unchanged.",
        "",
        "## Reconstructed runs",
        "",
        "| Repetition | Moondream (ms) | Unload confirmed (ms) | Qwen client (ms) | Qwen usage |",
        "| ---: | ---: | ---: | ---: | ---: |",
    ]
    for run in runs:
        record = _mapping(run, "run")
        moondream = _mapping(record["moondream"], "Moondream record")
        unload = _mapping(record["model_unload"], "unload record")
        qwen = _mapping(record["qwen"], "Qwen record")
        usage = _mapping(qwen["usage"], "Qwen usage")
        lines.append(
            f"| {record['repetition']} | {float(moondream['client_ms']):.3f} | "
            f"yes ({float(unload['duration_ms']):.3f}) | "
            f"{float(qwen['client_ms']):.3f} | "
            f"{usage['prompt_tokens']} + {usage['completion_tokens']} = "
            f"{usage['total_tokens']} |"
        )
    lines.extend(
        [
            "",
            f"All three Qwen client calls completed below the former 30 s boundary; "
            f"the observed range was {float(reconstruction['qwen_client_min_ms']):.3f} "
            f"to {float(reconstruction['qwen_client_max_ms']):.3f} ms. The Moondream "
            "description identity and Qwen request size were stable. The Qwen output "
            "had two identities, so deterministic request construction is supported "
            "but byte-identical output is not claimed.",
            "",
            "## Service and resource checks",
            "",
            f"The llama-server log contains {server['request_count']} launches, "
            f"{server['released_request_count']} matching releases and zero "
            "cancellation, timeout or error records. It returned to idle after the "
            "final request. The first request evaluated 164 prompt tokens and all "
            "three generated 32 tokens; later prompt evaluation reused the server "
            "cache.",
            "",
            f"All {resources['sample_count']} tegrastats lines parsed without error. "
            f"RAM use ranged from {resources['ram_used_mb']['min']} to "
            f"{resources['ram_used_mb']['max']} MB, maximum GR3D use was "
            f"{resources['gr3d_usage_pct']['max']}%, maximum Tj was "
            f"{float(resources['maximum_tj_c']):.3f} C, and maximum instantaneous "
            f"VDD_IN was {resources['vdd_in_instant_mw']['max']} mW.",
            "",
            "## Decision and boundary",
            "",
            "The observations support the proposed deterministic request contract, "
            "60 s Qwen timeout and fail-closed unload polling. They do not support a "
            "llama-server argument change.",
            "",
            "The diagnostic reproduced the proposed contract in an inline harness; it "
            "did not execute the modified repository adapter. The unload process-list "
            "responses were also not retained, although each client record reports "
            "positive absence confirmation. Direct execution of the repaired path on "
            "the Jetson therefore remains required before this repair is considered "
            "validated.",
            "",
            "G6 v3 remains permanently closed and is not rerun or replaced. Phase 1 "
            "remains incomplete during corrective work, and no formal collection, "
            "sync/async performance claim or application integration is authorized.",
            "",
        ]
    )
    assert decision["actual_repaired_path_validation_required"] is True
    return "\n".join(lines)


def _write_text_atomic(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(value, encoding="utf-8")
    os.replace(temporary, path)


def _refuse_output_inside_diagnostic(output: Path | None, root: Path) -> None:
    if output is None:
        return
    resolved = output.resolve()
    if resolved == root or resolved.is_relative_to(root):
        raise ValueError("analysis output must remain outside the raw diagnostic")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("diagnostic", type=Path)
    parser.add_argument("--source-archive-sha256", required=True)
    parser.add_argument("--json-output", type=Path)
    parser.add_argument("--markdown-output", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        root = args.diagnostic.resolve()
        _refuse_output_inside_diagnostic(args.json_output, root)
        _refuse_output_inside_diagnostic(args.markdown_output, root)
        analysis = analyze_vlm_timeout_diagnostic(
            root,
            source_archive_sha256=args.source_archive_sha256,
        )
        if args.json_output is not None and args.markdown_output is not None:
            if args.json_output.resolve() == args.markdown_output.resolve():
                raise ValueError("JSON and Markdown outputs must differ")
        if args.json_output is not None:
            write_json_atomic(args.json_output, analysis)
        if args.markdown_output is not None:
            _write_text_atomic(args.markdown_output, render_markdown(analysis))
        if args.json_output is None and args.markdown_output is None:
            print(json.dumps(analysis, ensure_ascii=False, indent=2))
    except (OSError, UnicodeError, ValueError) as exc:
        print(
            f"VLM timeout diagnostic analysis failed: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
