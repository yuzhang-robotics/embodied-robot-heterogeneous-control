"""Fixed-input Whisper adapter for the first real Phase 1 ASR slice."""

from __future__ import annotations

import hashlib
import math
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from jetson.phase1_runtime import (
    CancellationReport,
    ClaimedTask,
    ExecutionOutcome,
    PayloadRef,
    ResultEnvelope,
    TaskKind,
)


ASR_INPUT_SHA256 = "3fffeee1e04250faa483174a423878bf220b95f6706684f6e109ed8f9b731440"
ASR_INPUT_SIZE_BYTES = 114136
ASR_INPUT_MEDIA_TYPE = "audio/wav"
ASR_EXPECTED_OUTPUT_SHA256 = (
    "9b718ac6e824461152cb5dd402453b7b43bf000f708b257cd6d2d10d109f4a49"
)
ASR_EXPECTED_OUTPUT_LENGTH = 21
ASR_MODEL_SHA256 = "1be3a9b2063867b937e64e2ec7483364a79917e157fa98c5d94b5c1fffea987b"
ASR_MODEL_SIZE_BYTES = 487601967
ASR_WHISPER_SOURCE_VERSION = "v1.8.4-326-gafa2ea54"
ASR_WHISPER_ARGUMENTS = ("-l", "zh", "-otxt", "-nt", "-np", "-bs", "1", "-bo", "1")


class ASRInputError(ValueError):
    """The fixed audio input does not match the frozen Phase 0 identity."""


class ASRExecutionError(RuntimeError):
    """One bounded stage of the Whisper adapter failed."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class ASRRuntime:
    """Filesystem locations for the Phase 0 whisper.cpp invocation."""

    whisper_dir: Path
    whisper_binary: Path
    whisper_model: Path


@dataclass(frozen=True, slots=True)
class ASRExecutionRecord:
    """Privacy-preserving facts from one supervised Whisper invocation."""

    task_id: str
    worker_thread_id: int
    started_monotonic_ns: int
    finished_monotonic_ns: int
    execution_outcome: str
    error_code: str | None
    input_sha256: str
    input_size_bytes: int
    output_sha256: str | None
    output_length: int | None
    process_started: bool
    process_exit_code: int | None
    terminate_requested: bool
    terminate_confirmed: bool
    kill_requested: bool
    kill_confirmed: bool
    process_reaped: bool
    cancellation_requested: bool
    worker_observed_cancellation: bool
    client_wait_stopped: bool
    backend_stop_confirmed: bool | None

    def to_dict(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "worker_thread_id": self.worker_thread_id,
            "started_monotonic_ns": self.started_monotonic_ns,
            "finished_monotonic_ns": self.finished_monotonic_ns,
            "duration_ns": self.finished_monotonic_ns - self.started_monotonic_ns,
            "execution_outcome": self.execution_outcome,
            "error_code": self.error_code,
            "input": {
                "sha256": self.input_sha256,
                "size_bytes": self.input_size_bytes,
                "media_type": ASR_INPUT_MEDIA_TYPE,
            },
            "output": (
                None
                if self.output_sha256 is None
                else {
                    "sha256": self.output_sha256,
                    "length": self.output_length,
                    "raw_text_recorded": False,
                }
            ),
            "process": {
                "started": self.process_started,
                "exit_code": self.process_exit_code,
                "terminate_requested": self.terminate_requested,
                "terminate_confirmed": self.terminate_confirmed,
                "kill_requested": self.kill_requested,
                "kill_confirmed": self.kill_confirmed,
                "reaped": self.process_reaped,
            },
            "cancellation": {
                "requested": self.cancellation_requested,
                "worker_observed": self.worker_observed_cancellation,
                "client_wait_stopped": self.client_wait_stopped,
                "backend_stop_confirmed": self.backend_stop_confirmed,
            },
        }


ProcessFactory = Callable[[list[str], Path], subprocess.Popen[bytes]]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fixed_asr_payload(path: Path | str) -> PayloadRef:
    """Verify and reference the exact WAV used by the formal Phase 0 ASR runs."""

    resolved = Path(path).expanduser().resolve()
    if not resolved.is_file():
        raise ASRInputError("fixed ASR input is not a regular file")
    size_bytes = resolved.stat().st_size
    if size_bytes != ASR_INPUT_SIZE_BYTES:
        raise ASRInputError("fixed ASR input size does not match Phase 0")
    digest = _sha256_file(resolved)
    if digest != ASR_INPUT_SHA256:
        raise ASRInputError("fixed ASR input hash does not match Phase 0")
    return PayloadRef(
        ref=str(resolved),
        sha256=digest,
        size_bytes=size_bytes,
        media_type=ASR_INPUT_MEDIA_TYPE,
    )


def load_phase0_asr_runtime() -> ASRRuntime:
    """Load whisper.cpp paths without importing the blocking robot application."""

    from jetson.config import WHISPER_ASR_MODEL, WHISPER_BIN, WHISPER_DIR

    return ASRRuntime(
        whisper_dir=Path(WHISPER_DIR).expanduser().resolve(),
        whisper_binary=Path(WHISPER_BIN).expanduser().resolve(),
        whisper_model=Path(WHISPER_ASR_MODEL).expanduser().resolve(),
    )


def build_whisper_command(
    runtime: ASRRuntime,
    input_path: Path,
    output_base: Path,
) -> list[str]:
    """Build the frozen Phase 0 command without executing it."""

    return [
        str(runtime.whisper_binary),
        "-m",
        str(runtime.whisper_model),
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


def _spawn_process(command: list[str], cwd: Path) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        command,
        cwd=str(cwd),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
    )


def _positive_finite(value: float, name: str) -> float:
    converted = float(value)
    if not math.isfinite(converted) or converted <= 0:
        raise ValueError(f"{name} must be positive and finite")
    return converted


class FixedInputASRAdapter:
    """Supervise one whisper-cli child and publish transcript identity only."""

    def __init__(
        self,
        *,
        runtime_loader: Callable[[], ASRRuntime] | None = None,
        process_factory: ProcessFactory | None = None,
        execution_timeout_s: float = 120.0,
        poll_interval_s: float = 0.05,
        terminate_timeout_s: float = 2.0,
        kill_timeout_s: float = 2.0,
        clock_ns: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        if runtime_loader is not None and not callable(runtime_loader):
            raise TypeError("runtime_loader must be callable or None")
        if process_factory is not None and not callable(process_factory):
            raise TypeError("process_factory must be callable or None")
        if not callable(clock_ns):
            raise TypeError("clock_ns must be callable")
        self._runtime_loader = runtime_loader or load_phase0_asr_runtime
        self._process_factory = process_factory or _spawn_process
        self._execution_timeout_s = _positive_finite(
            execution_timeout_s, "execution_timeout_s"
        )
        self._poll_interval_s = _positive_finite(poll_interval_s, "poll_interval_s")
        self._terminate_timeout_s = _positive_finite(
            terminate_timeout_s, "terminate_timeout_s"
        )
        self._kill_timeout_s = _positive_finite(kill_timeout_s, "kill_timeout_s")
        self._clock_ns = clock_ns
        self._invocation_lock = threading.Lock()
        self._record_lock = threading.Lock()
        self._last_record: ASRExecutionRecord | None = None
        self.inference_started_event = threading.Event()

    @property
    def last_record(self) -> ASRExecutionRecord | None:
        with self._record_lock:
            return self._last_record

    @staticmethod
    def _verify_runtime(runtime: ASRRuntime) -> None:
        if not isinstance(runtime, ASRRuntime):
            raise ASRExecutionError("invalid_asr_runtime")
        if not runtime.whisper_dir.is_dir():
            raise ASRExecutionError("whisper_dir_missing")
        if not runtime.whisper_binary.is_file():
            raise ASRExecutionError("whisper_binary_missing")
        if not runtime.whisper_model.is_file():
            raise ASRExecutionError("whisper_model_missing")
        if runtime.whisper_model.stat().st_size != ASR_MODEL_SIZE_BYTES:
            raise ASRExecutionError("whisper_model_size_mismatch")

    @staticmethod
    def _verify_claimed_input(claimed: ClaimedTask) -> Path:
        task = claimed.task
        if task.task_kind is not TaskKind.ASR:
            raise ASRExecutionError("invalid_task_kind")
        payload = task.payload
        if (
            payload.sha256 != ASR_INPUT_SHA256
            or payload.size_bytes != ASR_INPUT_SIZE_BYTES
            or payload.media_type != ASR_INPUT_MEDIA_TYPE
        ):
            raise ASRExecutionError("unsupported_fixed_input")
        path = Path(payload.ref).expanduser().resolve()
        if not path.is_file():
            raise ASRExecutionError("input_missing")
        if path.stat().st_size != payload.size_bytes:
            raise ASRExecutionError("input_size_mismatch")
        if _sha256_file(path) != payload.sha256:
            raise ASRExecutionError("input_hash_mismatch")
        return path

    def _stop_process(
        self,
        process: subprocess.Popen[bytes],
    ) -> tuple[bool, bool, bool, bool, bool]:
        terminate_requested = process.poll() is None
        terminate_confirmed = False
        kill_requested = False
        kill_confirmed = False
        if terminate_requested:
            process.terminate()
            try:
                process.wait(timeout=self._terminate_timeout_s)
                terminate_confirmed = True
            except subprocess.TimeoutExpired:
                kill_requested = True
                process.kill()
                try:
                    process.wait(timeout=self._kill_timeout_s)
                    kill_confirmed = True
                except subprocess.TimeoutExpired:
                    pass
        reaped = process.poll() is not None
        return (
            terminate_requested,
            terminate_confirmed,
            kill_requested,
            kill_confirmed,
            reaped,
        )

    def _finish(
        self,
        claimed: ClaimedTask,
        *,
        outcome: ExecutionOutcome,
        error_code: str | None,
        output_sha256: str | None,
        output_length: int | None,
        process_started: bool,
        process_exit_code: int | None,
        terminate_requested: bool,
        terminate_confirmed: bool,
        kill_requested: bool,
        kill_confirmed: bool,
        process_reaped: bool,
        worker_observed_cancellation: bool,
        client_wait_stopped: bool,
        backend_stop_confirmed: bool | None,
    ) -> ResultEnvelope:
        task = claimed.task
        finished = max(claimed.started_monotonic_ns, self._clock_ns())
        cancellation_requested = claimed.cancellation_token.is_requested()
        cancellation = CancellationReport(
            requested=cancellation_requested,
            client_wait_stopped=client_wait_stopped,
            worker_observed=worker_observed_cancellation,
            backend_stop_confirmed=backend_stop_confirmed,
        )
        result = ResultEnvelope(
            task_id=task.task_id,
            task_kind=task.task_kind,
            state_token=task.state_token,
            source_monotonic_ns=task.source_monotonic_ns,
            deadline_monotonic_ns=task.deadline_monotonic_ns,
            input_sha256=task.payload.sha256,
            started_monotonic_ns=claimed.started_monotonic_ns,
            finished_monotonic_ns=finished,
            execution_outcome=outcome,
            output_sha256=output_sha256,
            output_length=output_length,
            error_code=error_code,
            cancellation_report=cancellation,
        )
        record = ASRExecutionRecord(
            task_id=task.task_id,
            worker_thread_id=threading.get_ident(),
            started_monotonic_ns=claimed.started_monotonic_ns,
            finished_monotonic_ns=finished,
            execution_outcome=outcome.value,
            error_code=error_code,
            input_sha256=task.payload.sha256,
            input_size_bytes=task.payload.size_bytes,
            output_sha256=output_sha256,
            output_length=output_length,
            process_started=process_started,
            process_exit_code=process_exit_code,
            terminate_requested=terminate_requested,
            terminate_confirmed=terminate_confirmed,
            kill_requested=kill_requested,
            kill_confirmed=kill_confirmed,
            process_reaped=process_reaped,
            cancellation_requested=cancellation_requested,
            worker_observed_cancellation=worker_observed_cancellation,
            client_wait_stopped=client_wait_stopped,
            backend_stop_confirmed=backend_stop_confirmed,
        )
        with self._record_lock:
            self._last_record = record
        return result

    def __call__(self, claimed: ClaimedTask) -> ResultEnvelope:
        if not isinstance(claimed, ClaimedTask):
            raise TypeError("claimed must be a ClaimedTask")
        if not self._invocation_lock.acquire(blocking=False):
            raise ASRExecutionError("asr_adapter_busy")

        self.inference_started_event.clear()
        process: subprocess.Popen[bytes] | None = None
        process_started = False
        process_exit_code: int | None = None
        terminate_requested = False
        terminate_confirmed = False
        kill_requested = False
        kill_confirmed = False
        process_reaped = False
        worker_observed_cancellation = False
        client_wait_stopped = False
        backend_stop_confirmed: bool | None = None
        outcome = ExecutionOutcome.OK
        error_code: str | None = None
        output_sha256: str | None = None
        output_length: int | None = None

        try:
            if claimed.cancellation_token.is_requested():
                return self._finish(
                    claimed,
                    outcome=ExecutionOutcome.CANCEL_OBSERVED,
                    error_code=None,
                    output_sha256=None,
                    output_length=None,
                    process_started=False,
                    process_exit_code=None,
                    terminate_requested=False,
                    terminate_confirmed=False,
                    kill_requested=False,
                    kill_confirmed=False,
                    process_reaped=False,
                    worker_observed_cancellation=True,
                    client_wait_stopped=False,
                    backend_stop_confirmed=None,
                )

            try:
                input_path = self._verify_claimed_input(claimed)
                runtime = self._runtime_loader()
                self._verify_runtime(runtime)
                with tempfile.TemporaryDirectory(prefix="phase1-asr-") as temp_dir:
                    output_base = Path(temp_dir) / "asr_out"
                    output_txt = output_base.with_suffix(".txt")
                    command = build_whisper_command(runtime, input_path, output_base)
                    process = self._process_factory(command, runtime.whisper_dir)
                    if not isinstance(process, subprocess.Popen):
                        raise ASRExecutionError("invalid_whisper_process")
                    process_started = True
                    self.inference_started_event.set()
                    timeout_at_ns = self._clock_ns() + int(
                        self._execution_timeout_s * 1_000_000_000
                    )

                    while process.poll() is None:
                        now_ns = self._clock_ns()
                        if claimed.cancellation_token.is_requested():
                            worker_observed_cancellation = True
                            client_wait_stopped = True
                            (
                                terminate_requested,
                                terminate_confirmed,
                                kill_requested,
                                kill_confirmed,
                                process_reaped,
                            ) = self._stop_process(process)
                            backend_stop_confirmed = process_reaped
                            outcome = ExecutionOutcome.CANCEL_OBSERVED
                            break
                        if now_ns >= timeout_at_ns:
                            (
                                terminate_requested,
                                terminate_confirmed,
                                kill_requested,
                                kill_confirmed,
                                process_reaped,
                            ) = self._stop_process(process)
                            outcome = ExecutionOutcome.TIMEOUT
                            error_code = "whisper_timeout"
                            break
                        remaining_s = (timeout_at_ns - now_ns) / 1_000_000_000
                        claimed.cancellation_token.wait(
                            timeout=min(self._poll_interval_s, remaining_s)
                        )

                    if process.poll() is not None:
                        process.wait(timeout=0)
                        process_reaped = True
                        process_exit_code = process.returncode

                    if outcome is ExecutionOutcome.OK:
                        if process_exit_code != 0:
                            outcome = ExecutionOutcome.ERROR
                            error_code = "whisper_exit_nonzero"
                        elif not output_txt.is_file():
                            outcome = ExecutionOutcome.ERROR
                            error_code = "whisper_output_missing"
                        else:
                            transcript = output_txt.read_text(
                                encoding="utf-8", errors="replace"
                            ).strip()
                            encoded = transcript.encode("utf-8")
                            observed_hash = hashlib.sha256(encoded).hexdigest()
                            observed_length = len(transcript)
                            if (
                                observed_hash != ASR_EXPECTED_OUTPUT_SHA256
                                or observed_length != ASR_EXPECTED_OUTPUT_LENGTH
                            ):
                                outcome = ExecutionOutcome.ERROR
                                error_code = "unexpected_transcript"
                            else:
                                output_sha256 = observed_hash
                                output_length = observed_length

                    self._verify_claimed_input(claimed)
            except ASRExecutionError as exc:
                outcome = ExecutionOutcome.ERROR
                error_code = exc.code
            except OSError as exc:
                outcome = ExecutionOutcome.ERROR
                error_code = "whisper_" + type(exc).__name__.lower()
            finally:
                if process is not None and process.poll() is None:
                    (
                        cleanup_terminate_requested,
                        cleanup_terminate_confirmed,
                        cleanup_kill_requested,
                        cleanup_kill_confirmed,
                        cleanup_reaped,
                    ) = self._stop_process(process)
                    terminate_requested = (
                        terminate_requested or cleanup_terminate_requested
                    )
                    terminate_confirmed = (
                        terminate_confirmed or cleanup_terminate_confirmed
                    )
                    kill_requested = kill_requested or cleanup_kill_requested
                    kill_confirmed = kill_confirmed or cleanup_kill_confirmed
                    process_reaped = process_reaped or cleanup_reaped
                    process_exit_code = process.returncode
                if process_started and not process_reaped:
                    outcome = ExecutionOutcome.ERROR
                    error_code = "whisper_reap_failed"
                    output_sha256 = None
                    output_length = None

            return self._finish(
                claimed,
                outcome=outcome,
                error_code=error_code,
                output_sha256=output_sha256,
                output_length=output_length,
                process_started=process_started,
                process_exit_code=process_exit_code,
                terminate_requested=terminate_requested,
                terminate_confirmed=terminate_confirmed,
                kill_requested=kill_requested,
                kill_confirmed=kill_confirmed,
                process_reaped=process_reaped,
                worker_observed_cancellation=worker_observed_cancellation,
                client_wait_stopped=client_wait_stopped,
                backend_stop_confirmed=backend_stop_confirmed,
            )
        finally:
            self._invocation_lock.release()
