"""Fixed-input VLM adapter for the first real Phase 1 workload slice."""

from __future__ import annotations

import contextlib
import hashlib
import io
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Mapping

from jetson.phase1_runtime import (
    CancellationReport,
    ClaimedTask,
    ExecutionOutcome,
    PayloadRef,
    ResultEnvelope,
    TaskKind,
)


C100_INPUT_SHA256 = "607c9faf3ea03b8b032d8c1d9e86c697d9fb48ca3c2f278e453941da6b871be7"
C100_INPUT_SIZE_BYTES = 9009
C100_INPUT_MEDIA_TYPE = "image/jpeg"


class VLMInputError(ValueError):
    """The fixed input does not match the frozen Phase 0 identity."""


class VLMExecutionError(RuntimeError):
    """One bounded stage of the VLM pipeline failed."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class VLMPipeline:
    """Callables loaded from the validated Phase 0 VLM path."""

    describe_english: Callable[[Path], str]
    rewrite_chinese: Callable[[str], str]
    translate_fallback: Callable[[str], str]
    normalize_output: Callable[[str, str], str]
    unload_model: Callable[[], object]


@dataclass(frozen=True, slots=True)
class VLMExecutionRecord:
    """Privacy-preserving facts from one adapter invocation."""

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
    translation_route: str | None
    model_unload_requested: bool
    model_unload_confirmed: bool | None
    stage_durations_ns: Mapping[str, int]
    stage_status: Mapping[str, str]
    stage_error_codes: Mapping[str, str]
    cancellation_requested: bool
    worker_observed_cancellation: bool
    backend_stop_confirmed: bool | None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "stage_durations_ns",
            MappingProxyType(dict(self.stage_durations_ns)),
        )
        object.__setattr__(
            self,
            "stage_status",
            MappingProxyType(dict(self.stage_status)),
        )
        object.__setattr__(
            self,
            "stage_error_codes",
            MappingProxyType(dict(self.stage_error_codes)),
        )

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
                "media_type": C100_INPUT_MEDIA_TYPE,
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
            "translation_route": self.translation_route,
            "model_residency": {
                "unload_requested": self.model_unload_requested,
                "unload_confirmed": self.model_unload_confirmed,
            },
            "stage_durations_ns": dict(self.stage_durations_ns),
            "stage_status": dict(self.stage_status),
            "stage_error_codes": dict(self.stage_error_codes),
            "cancellation": {
                "requested": self.cancellation_requested,
                "worker_observed": self.worker_observed_cancellation,
                "client_wait_stopped": False,
                "backend_stop_confirmed": self.backend_stop_confirmed,
            },
        }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _exception_code(exc: BaseException) -> str:
    raw = type(exc).__name__.lower()
    normalized = "".join(
        character if character.isalnum() else "_" for character in raw
    ).strip("_")
    return (normalized or "exception")[:64]


def fixed_c100_payload(path: Path | str) -> PayloadRef:
    """Verify and reference the exact C100 image used by Phase 0."""

    resolved = Path(path).expanduser().resolve()
    if not resolved.is_file():
        raise VLMInputError("fixed VLM input is not a regular file")
    size_bytes = resolved.stat().st_size
    if size_bytes != C100_INPUT_SIZE_BYTES:
        raise VLMInputError("fixed VLM input size does not match Phase 0")
    digest = _sha256_file(resolved)
    if digest != C100_INPUT_SHA256:
        raise VLMInputError("fixed VLM input hash does not match Phase 0")
    return PayloadRef(
        ref=str(resolved),
        sha256=digest,
        size_bytes=size_bytes,
        media_type=C100_INPUT_MEDIA_TYPE,
    )


def _load_phase0_pipeline() -> VLMPipeline:
    from jetson.vision_vlm import (
        ask_moondream_english,
        make_speech_friendly,
        translate_en_to_zh,
        translate_with_qwen,
        unload_moondream,
    )

    return VLMPipeline(
        describe_english=ask_moondream_english,
        rewrite_chinese=translate_with_qwen,
        translate_fallback=translate_en_to_zh,
        normalize_output=make_speech_friendly,
        unload_model=unload_moondream,
    )


class FixedInputVLMAdapter:
    """Run the Phase 0 VLM path without exposing model text to artifacts."""

    def __init__(
        self,
        *,
        pipeline_loader: Callable[[], VLMPipeline] | None = None,
        clock_ns: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        if pipeline_loader is not None and not callable(pipeline_loader):
            raise TypeError("pipeline_loader must be callable or None")
        if not callable(clock_ns):
            raise TypeError("clock_ns must be callable")
        self._pipeline_loader = pipeline_loader or _load_phase0_pipeline
        self._clock_ns = clock_ns
        self._invocation_lock = threading.Lock()
        self._record_lock = threading.Lock()
        self._last_record: VLMExecutionRecord | None = None
        self.inference_started_event = threading.Event()

    @property
    def last_record(self) -> VLMExecutionRecord | None:
        with self._record_lock:
            return self._last_record

    def _verify_claimed_input(self, claimed: ClaimedTask) -> Path:
        task = claimed.task
        if task.task_kind is not TaskKind.VLM:
            raise VLMExecutionError("invalid_task_kind")
        payload = task.payload
        if (
            payload.sha256 != C100_INPUT_SHA256
            or payload.size_bytes != C100_INPUT_SIZE_BYTES
            or payload.media_type != C100_INPUT_MEDIA_TYPE
        ):
            raise VLMExecutionError("unsupported_fixed_input")
        path = Path(payload.ref).expanduser().resolve()
        if not path.is_file():
            raise VLMExecutionError("input_missing")
        if path.stat().st_size != payload.size_bytes:
            raise VLMExecutionError("input_size_mismatch")
        if _sha256_file(path) != payload.sha256:
            raise VLMExecutionError("input_hash_mismatch")
        return path

    def _stage(
        self,
        name: str,
        operation: Callable[[], object],
        durations: dict[str, int],
        statuses: dict[str, str],
        error_codes: dict[str, str],
    ) -> object:
        started = self._clock_ns()
        try:
            value = operation()
        except Exception as exc:
            statuses[name] = "error"
            error_codes[name] = _exception_code(exc)
            raise
        else:
            statuses[name] = "ok"
            return value
        finally:
            durations[name] = max(0, self._clock_ns() - started)

    def _result(
        self,
        claimed: ClaimedTask,
        *,
        outcome: ExecutionOutcome,
        error_code: str | None,
        output_sha256: str | None,
        output_length: int | None,
        route: str | None,
        durations: Mapping[str, int],
        statuses: Mapping[str, str],
        stage_error_codes: Mapping[str, str],
    ) -> ResultEnvelope:
        task = claimed.task
        finished = max(claimed.started_monotonic_ns, self._clock_ns())
        cancellation_requested = claimed.cancellation_token.is_requested()
        observed = cancellation_requested
        resolved_outcome = (
            ExecutionOutcome.CANCEL_OBSERVED
            if outcome is ExecutionOutcome.OK and observed
            else outcome
        )
        resolved_error = error_code
        cancellation = CancellationReport(
            requested=cancellation_requested,
            client_wait_stopped=False,
            worker_observed=observed,
            backend_stop_confirmed=None,
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
            execution_outcome=resolved_outcome,
            output_sha256=output_sha256,
            output_length=output_length,
            error_code=resolved_error,
            cancellation_report=cancellation,
        )
        record = VLMExecutionRecord(
            task_id=task.task_id,
            worker_thread_id=threading.get_ident(),
            started_monotonic_ns=claimed.started_monotonic_ns,
            finished_monotonic_ns=finished,
            execution_outcome=resolved_outcome.value,
            error_code=resolved_error,
            input_sha256=task.payload.sha256,
            input_size_bytes=task.payload.size_bytes,
            output_sha256=output_sha256,
            output_length=output_length,
            translation_route=route,
            model_unload_requested="model_unload" in statuses,
            model_unload_confirmed=None,
            stage_durations_ns=durations,
            stage_status=statuses,
            stage_error_codes=stage_error_codes,
            cancellation_requested=cancellation_requested,
            worker_observed_cancellation=observed,
            backend_stop_confirmed=None,
        )
        with self._record_lock:
            self._last_record = record
        return result

    def __call__(self, claimed: ClaimedTask) -> ResultEnvelope:
        if not isinstance(claimed, ClaimedTask):
            raise TypeError("claimed must be a ClaimedTask")
        if not self._invocation_lock.acquire(blocking=False):
            raise VLMExecutionError("vlm_adapter_busy")

        self.inference_started_event.clear()
        durations: dict[str, int] = {}
        statuses: dict[str, str] = {}
        stage_error_codes: dict[str, str] = {}
        output_sha256: str | None = None
        output_length: int | None = None
        route: str | None = None
        outcome = ExecutionOutcome.OK
        error_code: str | None = None
        pipeline: VLMPipeline | None = None

        try:
            if claimed.cancellation_token.is_requested():
                return self._result(
                    claimed,
                    outcome=ExecutionOutcome.CANCEL_OBSERVED,
                    error_code=None,
                    output_sha256=None,
                    output_length=None,
                    route=None,
                    durations=durations,
                    statuses=statuses,
                    stage_error_codes=stage_error_codes,
                )

            try:
                input_path = self._stage(
                    "input_verify_before",
                    lambda: self._verify_claimed_input(claimed),
                    durations,
                    statuses,
                    stage_error_codes,
                )
                assert isinstance(input_path, Path)
                pipeline_value = self._stage(
                    "module_import",
                    self._pipeline_loader,
                    durations,
                    statuses,
                    stage_error_codes,
                )
                if not isinstance(pipeline_value, VLMPipeline):
                    raise VLMExecutionError("invalid_pipeline")
                pipeline = pipeline_value

                captured_stdout = io.StringIO()
                captured_stderr = io.StringIO()
                with (
                    contextlib.redirect_stdout(captured_stdout),
                    contextlib.redirect_stderr(captured_stderr),
                ):
                    self.inference_started_event.set()
                    english_value = self._stage(
                        "moondream_inference",
                        lambda: pipeline.describe_english(input_path),
                        durations,
                        statuses,
                        stage_error_codes,
                    )
                    english = str(english_value).strip()
                    if not english:
                        raise VLMExecutionError("empty_vlm_description")

                    try:
                        self._stage(
                            "model_unload",
                            pipeline.unload_model,
                            durations,
                            statuses,
                            stage_error_codes,
                        )
                    except Exception as exc:
                        raise VLMExecutionError("model_unload_failed") from exc

                    try:
                        chinese_value = self._stage(
                            "qwen_rewrite",
                            lambda: pipeline.rewrite_chinese(english),
                            durations,
                            statuses,
                            stage_error_codes,
                        )
                        chinese = str(chinese_value).strip()
                        route = "qwen"
                    except Exception:
                        fallback_value = self._stage(
                            "argos_fallback",
                            lambda: pipeline.translate_fallback(english),
                            durations,
                            statuses,
                            stage_error_codes,
                        )
                        chinese = str(fallback_value).strip()
                        route = "argos"

                    output_value = self._stage(
                        "output_normalization",
                        lambda: pipeline.normalize_output(chinese, english),
                        durations,
                        statuses,
                        stage_error_codes,
                    )
                    output = str(output_value).strip()
                    if not output:
                        raise VLMExecutionError("empty_vlm_output")
                    encoded_output = output.encode("utf-8")
                    output_sha256 = hashlib.sha256(encoded_output).hexdigest()
                    output_length = len(output)
            except VLMExecutionError as exc:
                outcome = ExecutionOutcome.ERROR
                error_code = exc.code
            except Exception as exc:
                outcome = ExecutionOutcome.ERROR
                error_code = "vlm_pipeline_" + type(exc).__name__.lower()
                error_code = "".join(
                    character if character.isalnum() else "_"
                    for character in error_code
                )[:64]
            finally:
                if pipeline is not None and "model_unload" not in statuses:
                    try:
                        with contextlib.redirect_stdout(
                            io.StringIO()
                        ), contextlib.redirect_stderr(io.StringIO()):
                            self._stage(
                                "model_unload",
                                pipeline.unload_model,
                                durations,
                                statuses,
                                stage_error_codes,
                            )
                    except Exception:
                        outcome = ExecutionOutcome.ERROR
                        error_code = "model_unload_failed"
                if (
                    "input_verify_before" in statuses
                    and statuses["input_verify_before"] == "ok"
                ):
                    try:
                        self._stage(
                            "input_verify_after",
                            lambda: self._verify_claimed_input(claimed),
                            durations,
                            statuses,
                            stage_error_codes,
                        )
                    except Exception:
                        outcome = ExecutionOutcome.ERROR
                        error_code = "input_changed_during_execution"

            return self._result(
                claimed,
                outcome=outcome,
                error_code=error_code,
                output_sha256=output_sha256,
                output_length=output_length,
                route=route,
                durations=durations,
                statuses=statuses,
                stage_error_codes=stage_error_codes,
            )
        finally:
            self._invocation_lock.release()
