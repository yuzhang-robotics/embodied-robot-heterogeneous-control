"""Fixed-input llama.cpp adapter for the Phase 1 LLM correctness slice."""

from __future__ import annotations

import hashlib
import json
import math
import socket
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Mapping

from experiments.phase0.run_workload import (
    LLM_REQUEST_MAX_TOKENS as PHASE0_LLM_REQUEST_MAX_TOKENS,
    LLM_REQUEST_MODEL as PHASE0_LLM_REQUEST_MODEL,
    LLM_REQUEST_STREAM as PHASE0_LLM_REQUEST_STREAM,
    LLM_REQUEST_TEMPERATURE as PHASE0_LLM_REQUEST_TEMPERATURE,
    LLM_SYSTEM_PROMPT,
)
from jetson.phase1_runtime import (
    CancellationReport,
    ClaimedTask,
    ExecutionOutcome,
    PayloadRef,
    ResultEnvelope,
    TaskKind,
)


LLM_INPUT_SHA256 = "15ee277f4140cb3c2bca3d4762e6462e098787e5b5843245760d9f40da2ea7f2"
LLM_INPUT_SIZE_BYTES = 124
LLM_INPUT_MEDIA_TYPE = "text/plain"
LLM_MODEL_SHA256 = "6a1a2eb6d15622bf3c96857206351ba97e1af16c30d7a74ee38970e434e9407e"
LLM_MODEL_SIZE_BYTES = 1_117_320_736
LLM_EXPECTED_SERVED_MODEL_ID = "qwen2.5-1.5b-instruct-q4_k_m.gguf"
LLM_EXPECTED_REQUEST_MODEL = "qwen"
LLM_EXPECTED_REQUEST_TEMPERATURE = 0.4
LLM_EXPECTED_REQUEST_MAX_TOKENS = 80
LLM_EXPECTED_REQUEST_STREAM = False
LLM_SYSTEM_PROMPT_SHA256 = (
    "5e4cd3892f6603935b7c33f0c77c4b47936cdeab9dfc0db67f86c85e35b10081"
)
LLM_SYSTEM_PROMPT_LENGTH = 123
LLM_EMPTY_HISTORY_SHA256 = hashlib.sha256(b"[]").hexdigest()
LLM_SERVER_ARGUMENTS = MappingProxyType(
    {
        "host": "127.0.0.1",
        "port": 8080,
        "n_gpu_layers": 10,
        "ctx_size": 1024,
        "threads": 4,
        "parallel": 1,
        "cache_ram": 0,
    }
)


class LLMInputError(ValueError):
    """The prompt does not match the frozen Phase 0 identity."""


class LLMExecutionError(RuntimeError):
    """One bounded LLM adapter stage failed."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


HTTPRequester = Callable[[str, bytes, float], bytes]


def llm_request_contract() -> dict[str, object]:
    """Return the current public, text-free Phase 0 request identity."""

    return {
        "model": PHASE0_LLM_REQUEST_MODEL,
        "temperature": PHASE0_LLM_REQUEST_TEMPERATURE,
        "max_tokens": PHASE0_LLM_REQUEST_MAX_TOKENS,
        "stream": PHASE0_LLM_REQUEST_STREAM,
        "system_prompt": {
            "sha256": hashlib.sha256(LLM_SYSTEM_PROMPT.encode("utf-8")).hexdigest(),
            "length": len(LLM_SYSTEM_PROMPT),
            "raw_text_recorded": False,
        },
    }


def frozen_llm_request_contract() -> dict[str, object]:
    """Return the independently frozen Phase 0 request identity."""

    return {
        "model": LLM_EXPECTED_REQUEST_MODEL,
        "temperature": LLM_EXPECTED_REQUEST_TEMPERATURE,
        "max_tokens": LLM_EXPECTED_REQUEST_MAX_TOKENS,
        "stream": LLM_EXPECTED_REQUEST_STREAM,
        "system_prompt": {
            "sha256": LLM_SYSTEM_PROMPT_SHA256,
            "length": LLM_SYSTEM_PROMPT_LENGTH,
            "raw_text_recorded": False,
        },
    }


@dataclass(frozen=True, slots=True)
class LLMExecutionRecord:
    """Privacy-preserving facts from one llama.cpp request."""

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
    response_model: str | None
    token_usage: Mapping[str, int]
    stage_durations_ns: Mapping[str, int]
    stage_status: Mapping[str, str]
    cancellation_requested: bool
    worker_observed_cancellation: bool
    backend_stop_confirmed: bool | None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "token_usage", MappingProxyType(dict(self.token_usage))
        )
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
                "media_type": LLM_INPUT_MEDIA_TYPE,
                "raw_text_recorded": False,
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
            "request": {
                **llm_request_contract(),
                "raw_prompt_recorded": False,
            },
            "response": {
                "model": self.response_model,
                "usage": dict(self.token_usage),
                "raw_response_recorded": False,
            },
            "model_residency": {
                "policy": "external_llama_server_resident",
                "server_preexisting": True,
                "unload_requested": False,
                "backend_stop_confirmed": self.backend_stop_confirmed,
            },
            "stage_durations_ns": dict(self.stage_durations_ns),
            "stage_status": dict(self.stage_status),
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


def fixed_llm_payload(path: Path | str) -> PayloadRef:
    """Verify and reference the exact text prompt used by Phase 0."""

    resolved = Path(path).expanduser().resolve()
    if not resolved.is_file():
        raise LLMInputError("fixed LLM input is not a regular file")
    size_bytes = resolved.stat().st_size
    if size_bytes != LLM_INPUT_SIZE_BYTES:
        raise LLMInputError("fixed LLM input size does not match Phase 0")
    digest = _sha256_file(resolved)
    if digest != LLM_INPUT_SHA256:
        raise LLMInputError("fixed LLM input hash does not match Phase 0")
    return PayloadRef(
        ref=str(resolved),
        sha256=digest,
        size_bytes=size_bytes,
        media_type=LLM_INPUT_MEDIA_TYPE,
    )


def _load_llama_endpoint() -> str:
    from jetson.config import LLAMA_API_URL

    return LLAMA_API_URL


def _post_json(url: str, encoded_payload: bytes, timeout_s: float) -> bytes:
    request = urllib.request.Request(
        url,
        data=encoded_payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout_s) as response:
        return response.read()


def _positive_finite(value: object, name: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError(f"{name} must be positive and finite")
    return float(value)


def _sanitize_error_code(prefix: str, exc: BaseException) -> str:
    value = f"{prefix}_{type(exc).__name__.lower()}"
    return "".join(character if character.isalnum() else "_" for character in value)[
        :64
    ]


def _parse_response(raw: bytes) -> tuple[str, str | None, dict[str, int]]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise LLMExecutionError("invalid_llm_json") from exc
    if not isinstance(value, Mapping):
        raise LLMExecutionError("invalid_llm_response")
    choices = value.get("choices")
    if not isinstance(choices, list) or not choices:
        raise LLMExecutionError("missing_llm_choice")
    choice = choices[0]
    message = choice.get("message") if isinstance(choice, Mapping) else None
    content = message.get("content") if isinstance(message, Mapping) else None
    if not isinstance(content, str) or not content.strip():
        raise LLMExecutionError("empty_llm_output")
    usage_value = value.get("usage")
    if not isinstance(usage_value, Mapping):
        raise LLMExecutionError("missing_llm_usage")
    usage: dict[str, int] = {}
    for name in ("prompt_tokens", "completion_tokens", "total_tokens"):
        observed = usage_value.get(name)
        if isinstance(observed, bool) or not isinstance(observed, int) or observed < 0:
            raise LLMExecutionError("invalid_llm_usage")
        usage[name] = observed
    if usage["total_tokens"] < usage["prompt_tokens"] + usage["completion_tokens"]:
        raise LLMExecutionError("invalid_llm_usage")
    response_model = value.get("model")
    if response_model is not None and not isinstance(response_model, str):
        raise LLMExecutionError("invalid_llm_model_identity")
    return content.strip(), response_model, usage


class FixedInputLLMAdapter:
    """Call the local Phase 0 llama.cpp path without serializing model text."""

    def __init__(
        self,
        *,
        request_timeout_s: float = 120.0,
        endpoint_loader: Callable[[], str] | None = None,
        requester: HTTPRequester = _post_json,
        clock_ns: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        self._request_timeout_s = _positive_finite(
            request_timeout_s, "request_timeout_s"
        )
        self._endpoint_loader = endpoint_loader or _load_llama_endpoint
        if not callable(self._endpoint_loader):
            raise TypeError("endpoint_loader must be callable")
        if not callable(requester):
            raise TypeError("requester must be callable")
        if not callable(clock_ns):
            raise TypeError("clock_ns must be callable")
        self._requester = requester
        self._clock_ns = clock_ns
        self._invocation_lock = threading.Lock()
        self._record_lock = threading.Lock()
        self._last_record: LLMExecutionRecord | None = None
        self.inference_started_event = threading.Event()

    @property
    def last_record(self) -> LLMExecutionRecord | None:
        with self._record_lock:
            return self._last_record

    def _verify_claimed_input(self, claimed: ClaimedTask) -> tuple[Path, str]:
        task = claimed.task
        if task.task_kind is not TaskKind.LLM:
            raise LLMExecutionError("invalid_task_kind")
        payload = task.payload
        if (
            payload.sha256 != LLM_INPUT_SHA256
            or payload.size_bytes != LLM_INPUT_SIZE_BYTES
            or payload.media_type != LLM_INPUT_MEDIA_TYPE
        ):
            raise LLMExecutionError("unsupported_fixed_input")
        if (
            task.metadata.get("history_sha256") != LLM_EMPTY_HISTORY_SHA256
            or task.metadata.get("history_messages") != 0
        ):
            raise LLMExecutionError("unsupported_history_snapshot")
        path = Path(payload.ref).expanduser().resolve()
        if not path.is_file():
            raise LLMExecutionError("input_missing")
        if path.stat().st_size != payload.size_bytes:
            raise LLMExecutionError("input_size_mismatch")
        if _sha256_file(path) != payload.sha256:
            raise LLMExecutionError("input_hash_mismatch")
        try:
            prompt = path.read_text(encoding="utf-8").strip()
        except UnicodeError as exc:
            raise LLMExecutionError("input_not_utf8") from exc
        if not prompt:
            raise LLMExecutionError("empty_llm_prompt")
        return path, prompt

    def _stage(
        self,
        name: str,
        operation: Callable[[], object],
        durations: dict[str, int],
        statuses: dict[str, str],
    ) -> object:
        started = self._clock_ns()
        try:
            value = operation()
        except Exception:
            statuses[name] = "error"
            raise
        else:
            statuses[name] = "ok"
            return value
        finally:
            durations[name] = max(0, self._clock_ns() - started)

    def _publish_result(
        self,
        claimed: ClaimedTask,
        *,
        outcome: ExecutionOutcome,
        error_code: str | None,
        output_sha256: str | None,
        output_length: int | None,
        response_model: str | None,
        token_usage: Mapping[str, int],
        durations: Mapping[str, int],
        statuses: Mapping[str, str],
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
            error_code=error_code,
            cancellation_report=cancellation,
        )
        record = LLMExecutionRecord(
            task_id=task.task_id,
            worker_thread_id=threading.get_ident(),
            started_monotonic_ns=claimed.started_monotonic_ns,
            finished_monotonic_ns=finished,
            execution_outcome=resolved_outcome.value,
            error_code=error_code,
            input_sha256=task.payload.sha256,
            input_size_bytes=task.payload.size_bytes,
            output_sha256=output_sha256,
            output_length=output_length,
            response_model=response_model,
            token_usage=token_usage,
            stage_durations_ns=durations,
            stage_status=statuses,
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
            raise LLMExecutionError("llm_adapter_busy")

        self.inference_started_event.clear()
        durations: dict[str, int] = {}
        statuses: dict[str, str] = {}
        output_sha256: str | None = None
        output_length: int | None = None
        response_model: str | None = None
        token_usage: dict[str, int] = {}
        outcome = ExecutionOutcome.OK
        error_code: str | None = None

        try:
            if claimed.cancellation_token.is_requested():
                return self._publish_result(
                    claimed,
                    outcome=ExecutionOutcome.CANCEL_OBSERVED,
                    error_code=None,
                    output_sha256=None,
                    output_length=None,
                    response_model=None,
                    token_usage={},
                    durations=durations,
                    statuses=statuses,
                )

            input_path: Path | None = None
            try:
                verified = self._stage(
                    "input_verify_before",
                    lambda: self._verify_claimed_input(claimed),
                    durations,
                    statuses,
                )
                if not isinstance(verified, tuple) or len(verified) != 2:
                    raise LLMExecutionError("invalid_prompt_verification")
                input_path, prompt = verified
                endpoint = self._endpoint_loader()
                if not isinstance(endpoint, str) or not endpoint:
                    raise LLMExecutionError("invalid_llm_endpoint")
                payload_value = self._stage(
                    "request_build",
                    lambda: json.dumps(
                        {
                            "model": PHASE0_LLM_REQUEST_MODEL,
                            "messages": [
                                {"role": "system", "content": LLM_SYSTEM_PROMPT},
                                {"role": "user", "content": prompt},
                            ],
                            "temperature": PHASE0_LLM_REQUEST_TEMPERATURE,
                            "max_tokens": PHASE0_LLM_REQUEST_MAX_TOKENS,
                            "stream": PHASE0_LLM_REQUEST_STREAM,
                        },
                        ensure_ascii=False,
                    ).encode("utf-8"),
                    durations,
                    statuses,
                )
                if not isinstance(payload_value, bytes):
                    raise LLMExecutionError("invalid_llm_request")
                self.inference_started_event.set()
                raw_value = self._stage(
                    "llama_inference",
                    lambda: self._requester(
                        endpoint,
                        payload_value,
                        self._request_timeout_s,
                    ),
                    durations,
                    statuses,
                )
                if not isinstance(raw_value, bytes):
                    raise LLMExecutionError("invalid_llm_response_bytes")
                parsed = self._stage(
                    "response_parse",
                    lambda: _parse_response(raw_value),
                    durations,
                    statuses,
                )
                if not isinstance(parsed, tuple) or len(parsed) != 3:
                    raise LLMExecutionError("invalid_llm_response")
                output, response_model, usage_value = parsed
                if not isinstance(output, str) or not isinstance(usage_value, dict):
                    raise LLMExecutionError("invalid_llm_response")
                encoded_output = output.encode("utf-8")
                output_sha256 = hashlib.sha256(encoded_output).hexdigest()
                output_length = len(output)
                token_usage = usage_value
            except LLMExecutionError as exc:
                outcome = ExecutionOutcome.ERROR
                error_code = exc.code
            except (TimeoutError, socket.timeout):
                outcome = ExecutionOutcome.TIMEOUT
                error_code = "llm_request_timeout"
            except (urllib.error.HTTPError, urllib.error.URLError) as exc:
                outcome = ExecutionOutcome.ERROR
                error_code = _sanitize_error_code("llm_request", exc)
            except Exception as exc:
                outcome = ExecutionOutcome.ERROR
                error_code = _sanitize_error_code("llm_adapter", exc)
            finally:
                if input_path is not None:
                    try:
                        self._stage(
                            "input_verify_after",
                            lambda: self._verify_claimed_input(claimed),
                            durations,
                            statuses,
                        )
                    except Exception:
                        outcome = ExecutionOutcome.ERROR
                        error_code = "input_changed_during_execution"

            return self._publish_result(
                claimed,
                outcome=outcome,
                error_code=error_code,
                output_sha256=output_sha256,
                output_length=output_length,
                response_model=response_model,
                token_usage=token_usage,
                durations=durations,
                statuses=statuses,
            )
        finally:
            self._invocation_lock.release()
