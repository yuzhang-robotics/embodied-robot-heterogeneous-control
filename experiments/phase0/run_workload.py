"""Run one safe, fixed-input Phase 0 component workload on the Jetson."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

from .manifest import (
    MANIFEST_SCHEMA_VERSION,
    collect_environment,
    command_snapshot,
    file_identity,
    motion_environment,
    sha256_text,
    utc_now_iso,
    write_json_atomic,
)
from .telemetry import EventRecorder, TegrastatsSampler


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RUN_ROOT = REPO_ROOT / "experiments" / "runs"

_EXPECTED_MODEL_HASHES = {
    "asr": "1be3a9b2063867b937e64e2ec7483364a79917e157fa98c5d94b5c1fffea987b",
    "llm": "6a1a2eb6d15622bf3c96857206351ba97e1af16c30d7a74ee38970e434e9407e",
}

_RESIDENCY_POLICIES = {
    "asr": "whisper_cli_loads_model_per_invocation",
    "llm": "llama_server_model_resident_after_warmup",
    "vlm": "ollama_moondream_stopped_after_each_request",
}

_SYSTEM_PROMPT = (
    "你是章鱼号，一个由 yuzhang-robotics 开发、运行在 Jetson Orin Nano 上的离线中文语音助手。"
    "请用自然、简短、适合语音播报的中文回答。"
    "不要使用 Markdown 表格。"
    "除非用户要求详细解释，否则回答控制在三到六句话。"
)


class WorkloadError(RuntimeError):
    """A controlled component workload failed."""


def make_run_id(workload: str, repetition: int, now: datetime | None = None) -> str:
    if workload not in {"asr", "llm", "vlm"}:
        raise ValueError(f"unsupported workload: {workload}")
    if not 0 <= repetition <= 999:
        raise ValueError("repetition must be between 0 and 999")
    moment = now or datetime.now(timezone.utc)
    return f"{moment.astimezone(timezone.utc):%Y%m%dT%H%M%SZ}_phase0_{workload}_{repetition:03d}"


def _model_file_metadata(path: Path, expected_sha256: str) -> dict[str, Any]:
    identity = file_identity(path, calculate_hash=False)
    identity["expected_sha256"] = expected_sha256
    identity["hash_verified_during_environment_setup"] = True
    return identity


def _query_json(url: str, timeout: float = 5) -> dict[str, Any]:
    request = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def llama_models_url(api_url: str) -> str:
    parts = urllib.parse.urlsplit(api_url)
    prefix = parts.path.split("/v1/", 1)[0]
    path = f"{prefix}/v1/models"
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, path, "", "")
    )


def collect_workload_metadata(workload: str) -> dict[str, Any]:
    from jetson.config import (
        LLAMA_API_URL,
        OLLAMA_CHAT_URL,
        VLM_MODEL,
        WHISPER_ASR_MODEL,
        WHISPER_BIN,
        WHISPER_DIR,
    )

    if workload == "asr":
        return {
            "model": _model_file_metadata(
                WHISPER_ASR_MODEL, _EXPECTED_MODEL_HASHES["asr"]
            ),
            "binary": str(WHISPER_BIN.resolve()),
            "source_version": command_snapshot(
                ["git", "describe", "--tags", "--always", "--dirty"],
                cwd=WHISPER_DIR,
            ),
            "arguments": ["-l", "zh", "-otxt", "-nt", "-np", "-bs", "1", "-bo", "1"],
        }

    if workload == "llm":
        model_path = Path(
            os.environ.get(
                "PHASE0_QWEN_MODEL",
                str(
                    Path.home()
                    / "models/qwen2.5-1.5b/qwen2.5-1.5b-instruct-q4_k_m.gguf"
                ),
            )
        ).expanduser()
        return {
            "model": _model_file_metadata(model_path, _EXPECTED_MODEL_HASHES["llm"]),
            "endpoint": LLAMA_API_URL,
            "server_models": _query_json(llama_models_url(LLAMA_API_URL)),
            "source_version": command_snapshot(
                ["git", "describe", "--tags", "--always", "--dirty"],
                cwd=Path.home() / "llama.cpp",
            ),
            "server_arguments": {
                "n_gpu_layers": 10,
                "ctx_size": 1024,
                "threads": 4,
                "parallel": 1,
                "cache_ram": 0,
            },
            "request": {
                "model": "qwen",
                "temperature": 0.4,
                "max_tokens": 80,
                "stream": False,
            },
        }

    if workload == "vlm":
        tags_url = OLLAMA_CHAT_URL.rsplit("/", 1)[0] + "/tags"
        models = _query_json(tags_url).get("models", [])
        selected = next(
            (
                item
                for item in models
                if item.get("name") in {VLM_MODEL, f"{VLM_MODEL}:latest"}
            ),
            None,
        )
        if selected is None:
            raise WorkloadError(f"Ollama model not found: {VLM_MODEL}")
        return {
            "model": selected,
            "endpoint": OLLAMA_CHAT_URL,
            "request": {
                "temperature": 0.1,
                "num_predict": 100,
                "unload_after_request": True,
            },
        }

    raise ValueError(f"unsupported workload: {workload}")


def _write_log(stream: TextIO, message: str) -> None:
    stream.write(message.rstrip("\n") + "\n")
    stream.flush()


def _run_asr(
    input_path: Path,
    run_dir: Path,
    recorder: EventRecorder,
    log: TextIO,
    timeout_seconds: float,
) -> dict[str, Any]:
    from jetson.config import WHISPER_ASR_MODEL, WHISPER_BIN, WHISPER_DIR

    output_base = run_dir / "asr_out"
    output_txt = run_dir / "asr_out.txt"
    command = [
        str(WHISPER_BIN),
        "-m",
        str(WHISPER_ASR_MODEL),
        "-f",
        str(input_path),
        "-l",
        "zh",
        "-otxt",
        "-of",
        str(output_base),
        "-nt",
        "-np",
        "-bs",
        "1",
        "-bo",
        "1",
    ]

    recorder.emit(
        task_id="asr-task",
        event="inference.start",
        component="whisper",
        status="started",
        details={"command_argument_count": len(command)},
    )
    try:
        result = subprocess.run(
            command,
            cwd=str(WHISPER_DIR),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        recorder.emit(
            task_id="asr-task",
            event="inference.end",
            component="whisper",
            status="timeout",
            details={"timeout_seconds": timeout_seconds},
        )
        raise WorkloadError(f"Whisper timed out after {timeout_seconds} seconds") from exc

    _write_log(log, f"[whisper command] {' '.join(command)}")
    if result.stdout:
        _write_log(log, result.stdout)

    if result.returncode != 0:
        recorder.emit(
            task_id="asr-task",
            event="inference.end",
            component="whisper",
            status="error",
            details={"returncode": result.returncode},
        )
        raise WorkloadError(f"Whisper exited with code {result.returncode}")
    if not output_txt.exists():
        recorder.emit(
            task_id="asr-task",
            event="inference.end",
            component="whisper",
            status="error",
            details={"reason": "missing_output_file"},
        )
        raise WorkloadError("Whisper did not create asr_out.txt")

    text = output_txt.read_text(encoding="utf-8", errors="replace").strip()
    recorder.emit(
        task_id="asr-task",
        event="inference.end",
        component="whisper",
        status="ok",
        details={"output_chars": len(text)},
    )
    return {"text": text, "output_chars": len(text)}


def _run_llm(
    input_path: Path,
    recorder: EventRecorder,
    log: TextIO,
    timeout_seconds: float,
) -> dict[str, Any]:
    from jetson.config import LLAMA_API_URL

    prompt = input_path.read_text(encoding="utf-8").strip()
    payload = {
        "model": "qwen",
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.4,
        "max_tokens": 80,
        "stream": False,
    }
    request = urllib.request.Request(
        LLAMA_API_URL,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    recorder.emit(
        task_id="llm-task",
        event="inference.start",
        component="llama",
        status="started",
        details={"prompt_chars": len(prompt)},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            raw = response.read().decode("utf-8")
        _write_log(log, raw)
        response_data = json.loads(raw)
        text = response_data["choices"][0]["message"]["content"].strip()
        usage = response_data.get("usage", {})
    except Exception as exc:
        recorder.emit(
            task_id="llm-task",
            event="inference.end",
            component="llama",
            status="error",
            details={"error_type": type(exc).__name__},
        )
        raise WorkloadError(f"llama.cpp request failed: {exc}") from exc

    recorder.emit(
        task_id="llm-task",
        event="inference.end",
        component="llama",
        status="ok",
        details={"output_chars": len(text), "usage": usage},
    )
    return {"text": text, "output_chars": len(text), "usage": usage}


def _run_vlm(
    input_path: Path,
    recorder: EventRecorder,
    log: TextIO,
) -> dict[str, Any]:
    recorder.emit(
        task_id="vlm-task",
        event="module_import.start",
        component="vlm_runtime",
        status="started",
        details={},
    )
    try:
        from jetson.vision_vlm import (
            ask_moondream_english,
            make_speech_friendly,
            translate_en_to_zh,
            translate_with_qwen,
            unload_moondream,
        )
    except Exception as exc:
        recorder.emit(
            task_id="vlm-task",
            event="module_import.end",
            component="vlm_runtime",
            status="error",
            details={"error_type": type(exc).__name__},
        )
        raise
    recorder.emit(
        task_id="vlm-task",
        event="module_import.end",
        component="vlm_runtime",
        status="ok",
        details={},
    )

    recorder.emit(
        task_id="vlm-task",
        event="inference.start",
        component="moondream",
        status="started",
        details={},
    )
    translation_route = ""
    moondream_finished = False
    translation_started = False
    translation_finished = False
    try:
        with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
            english = ask_moondream_english(input_path)
        if not english:
            raise WorkloadError("Moondream returned an empty description")

        recorder.emit(
            task_id="vlm-task",
            event="inference.end",
            component="moondream",
            status="ok",
            details={"output_chars": len(english)},
        )
        moondream_finished = True
        recorder.emit(
            task_id="vlm-task",
            event="translation.start",
            component="translation",
            status="started",
            details={},
        )
        translation_started = True

        recorder.emit(
            task_id="vlm-task",
            event="rewrite.start",
            component="qwen",
            status="started",
            details={},
        )
        try:
            with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
                chinese = translate_with_qwen(english)
            translation_route = "qwen"
        except Exception as exc:
            recorder.emit(
                task_id="vlm-task",
                event="rewrite.end",
                component="qwen",
                status="error",
                details={"error_type": type(exc).__name__},
            )
            _write_log(log, f"[Qwen rewrite failed, using Argos] {type(exc).__name__}: {exc}")
            recorder.emit(
                task_id="vlm-task",
                event="translation.fallback",
                component="translation",
                status="info",
                details={"from": "qwen", "to": "argos", "error_type": type(exc).__name__},
            )
            recorder.emit(
                task_id="vlm-task",
                event="inference.start",
                component="argos",
                status="started",
                details={},
            )
            try:
                with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
                    chinese = translate_en_to_zh(english)
            except Exception as fallback_exc:
                recorder.emit(
                    task_id="vlm-task",
                    event="inference.end",
                    component="argos",
                    status="error",
                    details={"error_type": type(fallback_exc).__name__},
                )
                raise
            recorder.emit(
                task_id="vlm-task",
                event="inference.end",
                component="argos",
                status="ok",
                details={},
            )
            translation_route = "argos"
        else:
            recorder.emit(
                task_id="vlm-task",
                event="rewrite.end",
                component="qwen",
                status="ok",
                details={},
            )

        reply = make_speech_friendly(chinese, english)
        recorder.emit(
            task_id="vlm-task",
            event="translation.end",
            component="translation",
            status="ok",
            details={"route": translation_route, "output_chars": len(reply)},
        )
        translation_finished = True
        return {
            "english_text": english,
            "text": reply,
            "translation_route": translation_route,
            "output_chars": len(reply),
        }
    except Exception as exc:
        if not moondream_finished:
            recorder.emit(
                task_id="vlm-task",
                event="inference.end",
                component="moondream",
                status="error",
                details={"error_type": type(exc).__name__},
            )
        elif translation_started and not translation_finished:
            recorder.emit(
                task_id="vlm-task",
                event="translation.end",
                component="translation",
                status="error",
                details={"error_type": type(exc).__name__},
            )
        raise
    finally:
        recorder.emit(
            task_id="vlm-task",
            event="model_unload.start",
            component="moondream",
            status="started",
            details={},
        )
        with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
            unload_moondream()
        recorder.emit(
            task_id="vlm-task",
            event="model_unload.end",
            component="moondream",
            status="ok",
            details={},
        )


def execute_workload(
    workload: str,
    input_path: Path,
    run_dir: Path,
    recorder: EventRecorder,
    log: TextIO,
    timeout_seconds: float,
) -> dict[str, Any]:
    if workload == "asr":
        return _run_asr(input_path, run_dir, recorder, log, timeout_seconds)
    if workload == "llm":
        return _run_llm(input_path, recorder, log, timeout_seconds)
    if workload == "vlm":
        return _run_vlm(input_path, recorder, log)
    raise ValueError(f"unsupported workload: {workload}")


def run_once(args: argparse.Namespace) -> Path:
    safety = motion_environment()
    if not safety["motion_value_valid"]:
        raise WorkloadError(
            "refusing to run with an unrecognized ROBOT_ENABLE_MOTION value"
        )
    if safety["motion_enabled"]:
        raise WorkloadError("refusing to run while ROBOT_ENABLE_MOTION is enabled")

    input_path = args.input.expanduser().resolve()
    if not input_path.is_file():
        raise WorkloadError(f"input file does not exist: {input_path}")

    run_id = make_run_id(args.workload, args.repetition)
    run_dir = args.run_root.expanduser().resolve() / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    sample_role = "warmup" if args.repetition == 0 else "measured"

    manifest: dict[str, Any] = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "phase": "phase0",
        "baseline_commit": "61db058",
        "run_id": run_id,
        "workload": args.workload,
        "repetition": args.repetition,
        "sample_role": sample_role,
        "residency_policy": _RESIDENCY_POLICIES[args.workload],
        "status": "initializing",
        "started_at": utc_now_iso(),
        "finished_at": None,
        "input": file_identity(input_path),
        "safety": safety,
        "resource_interval_ms": args.resource_interval_ms,
        "environment": collect_environment(REPO_ROOT),
        "workload_config": {},
        "files": {
            "events": "events.jsonl",
            "resources": "resources.csv",
            "stdout": "stdout.log",
            "result": "result.json",
        },
        "errors": [],
    }
    write_json_atomic(run_dir / "manifest.json", manifest)

    status = "failed"
    result_data: dict[str, Any] = {}
    sampler: TegrastatsSampler | None = None

    with EventRecorder(run_dir, run_id) as recorder:
        recorder.emit(
            task_id=f"{args.workload}-task",
            event="experiment.start",
            component="runner",
            status="started",
            details={"sample_role": sample_role},
        )

        try:
            manifest["workload_config"] = collect_workload_metadata(args.workload)
            manifest["status"] = "running"
            write_json_atomic(run_dir / "manifest.json", manifest)

            sampler = TegrastatsSampler(run_dir, args.resource_interval_ms)
            sampler.start()
            time.sleep(max(0.25, args.resource_interval_ms / 1000))

            with (run_dir / "stdout.log").open(
                "w", encoding="utf-8", buffering=1
            ) as log:
                _write_log(log, f"run_id={run_id}")
                _write_log(log, f"workload={args.workload}")
                result_data = execute_workload(
                    args.workload,
                    input_path,
                    run_dir,
                    recorder,
                    log,
                    args.timeout_seconds,
                )

            result_text = str(result_data.get("text", ""))
            result_payload = {
                "schema_version": MANIFEST_SCHEMA_VERSION,
                "run_id": run_id,
                "status": "ok",
                "workload": args.workload,
                "text_sha256": sha256_text(result_text),
                "result": result_data,
            }
            write_json_atomic(run_dir / "result.json", result_payload)
            recorder.emit(
                task_id=f"{args.workload}-task",
                event="result.produced",
                component="runner",
                status="ok",
                details={
                    "text_chars": len(result_text),
                    "text_sha256": result_payload["text_sha256"],
                },
            )
            recorder.emit(
                task_id=f"{args.workload}-task",
                event="experiment.end",
                component="runner",
                status="ok",
                details={},
            )
            status = "completed"

        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            manifest["errors"].append(error)
            write_json_atomic(
                run_dir / "result.json",
                {
                    "schema_version": MANIFEST_SCHEMA_VERSION,
                    "run_id": run_id,
                    "status": "error",
                    "workload": args.workload,
                    "error": error,
                },
            )
            recorder.emit(
                task_id=f"{args.workload}-task",
                event="experiment.end",
                component="runner",
                status="error",
                details={"error_type": type(exc).__name__},
            )
            raise

        finally:
            if sampler is not None:
                time.sleep(max(0.25, args.resource_interval_ms / 1000))
                sampler.stop()
                if sampler.error:
                    manifest["errors"].append(sampler.error)
                    if status == "completed":
                        status = "invalid"

            manifest["status"] = status
            manifest["finished_at"] = utc_now_iso()
            write_json_atomic(run_dir / "manifest.json", manifest)

    return run_dir


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run one fixed-input, motion-disabled Phase 0 workload."
    )
    parser.add_argument("--workload", choices=["asr", "llm", "vlm"], required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--repetition", type=int, required=True)
    parser.add_argument("--run-root", type=Path, default=DEFAULT_RUN_ROOT)
    parser.add_argument("--resource-interval-ms", type=int, default=200)
    parser.add_argument("--timeout-seconds", type=float, default=300)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        run_dir = run_once(args)
    except Exception as exc:
        print(f"Phase 0 run failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(f"Phase 0 run completed: {run_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
