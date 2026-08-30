"""Process-isolated execution boundary for the fixed-input VLM adapter."""

from __future__ import annotations

import importlib
import json
import math
import multiprocessing
import re
import threading
import time
from dataclasses import dataclass, replace
from multiprocessing.connection import Connection
from typing import Callable, Mapping

from jetson.phase1_runtime import (
    CancellationReport,
    ClaimedTask,
    ExecutionOutcome,
    PayloadRef,
    ResultEnvelope,
    StateToken,
    TaskEnvelope,
    TaskKind,
)

from .vlm_adapter import VLMExecutionRecord


PROCESS_PROTOCOL_VERSION = "0.1.0"
DEFAULT_FACTORY_REF = "experiments.phase1.vlm_adapter:FixedInputVLMAdapter"
MAX_PROCESS_MESSAGE_BYTES = 65_536

_FACTORY_REF_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_.]*:[a-zA-Z_][a-zA-Z0-9_.]*$")
_ERROR_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_RECORD_KEYS = {
    "task_id",
    "worker_thread_id",
    "started_monotonic_ns",
    "finished_monotonic_ns",
    "duration_ns",
    "execution_outcome",
    "error_code",
    "input",
    "output",
    "translation_route",
    "model_residency",
    "stage_durations_ns",
    "stage_status",
    "cancellation",
}


class VLMProcessProtocolError(RuntimeError):
    """The child process violated the bounded VLM message protocol."""


class _VLMProcessConnectionClosed(VLMProcessProtocolError):
    pass


def _finite_positive_seconds(value: object, name: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError(f"{name} must be a finite positive number")
    return float(value)


def _bounded_error_code(prefix: str, value: object) -> str:
    raw = f"{prefix}_{value}".lower()
    normalized = "".join(character if character.isalnum() else "_" for character in raw)
    normalized = re.sub(r"_+", "_", normalized).strip("_")
    return (normalized or "process_worker_error")[:64]


def _encode_message(message: Mapping[str, object]) -> bytes:
    try:
        encoded = json.dumps(
            dict(message),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise VLMProcessProtocolError("process_message_not_json") from exc
    if len(encoded) > MAX_PROCESS_MESSAGE_BYTES:
        raise VLMProcessProtocolError("process_message_too_large")
    return encoded


def _send_message(
    connection: Connection,
    message: Mapping[str, object],
    *,
    lock: threading.Lock | None = None,
) -> None:
    encoded = _encode_message(message)
    if lock is None:
        connection.send_bytes(encoded)
        return
    with lock:
        connection.send_bytes(encoded)


def _receive_message(connection: Connection) -> dict[str, object]:
    try:
        encoded = connection.recv_bytes(MAX_PROCESS_MESSAGE_BYTES)
    except (EOFError, OSError) as exc:
        raise _VLMProcessConnectionClosed("process_connection_closed") from exc
    try:
        value = json.loads(encoded.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise VLMProcessProtocolError("process_message_invalid") from exc
    if not isinstance(value, dict):
        raise VLMProcessProtocolError("process_message_not_object")
    return value


def _serialize_claimed(claimed: ClaimedTask) -> dict[str, object]:
    task = claimed.task
    return {
        "task": {
            "task_id": task.task_id,
            "task_kind": task.task_kind.value,
            "source_monotonic_ns": task.source_monotonic_ns,
            "created_monotonic_ns": task.created_monotonic_ns,
            "deadline_monotonic_ns": task.deadline_monotonic_ns,
            "state_token": {
                "scope_id": task.state_token.scope_id,
                "generation": task.state_token.generation,
            },
            "payload": {
                "ref": task.payload.ref,
                "sha256": task.payload.sha256,
                "size_bytes": task.payload.size_bytes,
                "media_type": task.payload.media_type,
            },
            "parent_task_id": task.parent_task_id,
            "supersession_key": task.supersession_key,
            "metadata": dict(task.metadata),
        },
        "started_monotonic_ns": claimed.started_monotonic_ns,
    }


class _ProcessCancellationToken:
    def __init__(self, event: object) -> None:
        self._event = event

    def is_requested(self) -> bool:
        return bool(self._event.is_set())

    def wait(self, timeout: float | None = None) -> bool:
        return bool(self._event.wait(timeout))


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise VLMProcessProtocolError(f"{name}_not_object")
    return value


def _require_keys(
    value: Mapping[str, object],
    expected: set[str],
    name: str,
) -> None:
    if set(value) != expected:
        raise VLMProcessProtocolError(f"{name}_fields_invalid")


def _non_negative_integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise VLMProcessProtocolError(f"{name}_invalid")
    return value


def _deserialize_claimed(
    value: object,
    cancellation_event: object,
) -> ClaimedTask:
    claimed = _mapping(value, "claimed")
    if set(claimed) != {"task", "started_monotonic_ns"}:
        raise VLMProcessProtocolError("claimed_fields_invalid")
    task_value = _mapping(claimed["task"], "task")
    if set(task_value) != {
        "task_id",
        "task_kind",
        "source_monotonic_ns",
        "created_monotonic_ns",
        "deadline_monotonic_ns",
        "state_token",
        "payload",
        "parent_task_id",
        "supersession_key",
        "metadata",
    }:
        raise VLMProcessProtocolError("task_fields_invalid")
    state_value = _mapping(task_value["state_token"], "state_token")
    payload_value = _mapping(task_value["payload"], "payload")
    metadata = _mapping(task_value["metadata"], "metadata")
    _require_keys(state_value, {"scope_id", "generation"}, "state_token")
    _require_keys(
        payload_value,
        {"ref", "sha256", "size_bytes", "media_type"},
        "payload",
    )
    task = TaskEnvelope(
        task_id=task_value["task_id"],
        task_kind=TaskKind(task_value["task_kind"]),
        source_monotonic_ns=task_value["source_monotonic_ns"],
        created_monotonic_ns=task_value["created_monotonic_ns"],
        deadline_monotonic_ns=task_value["deadline_monotonic_ns"],
        state_token=StateToken(
            scope_id=state_value["scope_id"],
            generation=state_value["generation"],
        ),
        payload=PayloadRef(
            ref=payload_value["ref"],
            sha256=payload_value["sha256"],
            size_bytes=payload_value["size_bytes"],
            media_type=payload_value["media_type"],
        ),
        parent_task_id=task_value["parent_task_id"],
        supersession_key=task_value["supersession_key"],
        metadata=dict(metadata),
    )
    return ClaimedTask(
        task=task,
        cancellation_token=_ProcessCancellationToken(cancellation_event),
        started_monotonic_ns=claimed["started_monotonic_ns"],
    )


def _serialize_result(result: ResultEnvelope) -> dict[str, object]:
    if result.output_ref is not None:
        raise VLMProcessProtocolError("process_output_ref_forbidden")
    cancellation = result.cancellation_report
    return {
        "task_id": result.task_id,
        "task_kind": result.task_kind.value,
        "state_token": {
            "scope_id": result.state_token.scope_id,
            "generation": result.state_token.generation,
        },
        "source_monotonic_ns": result.source_monotonic_ns,
        "deadline_monotonic_ns": result.deadline_monotonic_ns,
        "input_sha256": result.input_sha256,
        "started_monotonic_ns": result.started_monotonic_ns,
        "finished_monotonic_ns": result.finished_monotonic_ns,
        "execution_outcome": result.execution_outcome.value,
        "output_sha256": result.output_sha256,
        "output_length": result.output_length,
        "error_code": result.error_code,
        "cancellation": {
            "requested": cancellation.requested,
            "client_wait_stopped": cancellation.client_wait_stopped,
            "worker_observed": cancellation.worker_observed,
            "backend_stop_confirmed": cancellation.backend_stop_confirmed,
        },
    }


def _deserialize_result(value: object) -> ResultEnvelope:
    result = _mapping(value, "result")
    expected = {
        "task_id",
        "task_kind",
        "state_token",
        "source_monotonic_ns",
        "deadline_monotonic_ns",
        "input_sha256",
        "started_monotonic_ns",
        "finished_monotonic_ns",
        "execution_outcome",
        "output_sha256",
        "output_length",
        "error_code",
        "cancellation",
    }
    if set(result) != expected:
        raise VLMProcessProtocolError("result_fields_invalid")
    state = _mapping(result["state_token"], "result_state_token")
    cancellation = _mapping(result["cancellation"], "result_cancellation")
    _require_keys(state, {"scope_id", "generation"}, "result_state_token")
    _require_keys(
        cancellation,
        {
            "requested",
            "client_wait_stopped",
            "worker_observed",
            "backend_stop_confirmed",
        },
        "result_cancellation",
    )
    return ResultEnvelope(
        task_id=result["task_id"],
        task_kind=TaskKind(result["task_kind"]),
        state_token=StateToken(
            scope_id=state["scope_id"],
            generation=state["generation"],
        ),
        source_monotonic_ns=result["source_monotonic_ns"],
        deadline_monotonic_ns=result["deadline_monotonic_ns"],
        input_sha256=result["input_sha256"],
        started_monotonic_ns=result["started_monotonic_ns"],
        finished_monotonic_ns=result["finished_monotonic_ns"],
        execution_outcome=ExecutionOutcome(result["execution_outcome"]),
        output_sha256=result["output_sha256"],
        output_length=result["output_length"],
        error_code=result["error_code"],
        cancellation_report=CancellationReport(
            requested=cancellation["requested"],
            client_wait_stopped=cancellation["client_wait_stopped"],
            worker_observed=cancellation["worker_observed"],
            backend_stop_confirmed=cancellation["backend_stop_confirmed"],
        ),
    )


def _deserialize_record(value: object) -> VLMExecutionRecord:
    record = _mapping(value, "record")
    if set(record) != _RECORD_KEYS:
        raise VLMProcessProtocolError("record_fields_invalid")
    input_value = _mapping(record["input"], "record_input")
    output_raw = record["output"]
    output_value = None if output_raw is None else _mapping(output_raw, "record_output")
    residency = _mapping(record["model_residency"], "record_residency")
    cancellation = _mapping(record["cancellation"], "record_cancellation")
    durations = _mapping(record["stage_durations_ns"], "record_durations")
    statuses = _mapping(record["stage_status"], "record_statuses")
    _require_keys(
        input_value,
        {"sha256", "size_bytes", "media_type"},
        "record_input",
    )
    if output_value is not None:
        _require_keys(
            output_value,
            {"sha256", "length", "raw_text_recorded"},
            "record_output",
        )
        if output_value["raw_text_recorded"] is not False:
            raise VLMProcessProtocolError("record_output_not_private")
    _require_keys(
        residency,
        {"unload_requested", "unload_confirmed"},
        "record_residency",
    )
    _require_keys(
        cancellation,
        {
            "requested",
            "worker_observed",
            "client_wait_stopped",
            "backend_stop_confirmed",
        },
        "record_cancellation",
    )
    if any(
        not isinstance(name, str)
        or isinstance(duration, bool)
        or not isinstance(duration, int)
        or duration < 0
        for name, duration in durations.items()
    ):
        raise VLMProcessProtocolError("record_durations_invalid")
    if any(
        not isinstance(name, str) or status not in {"ok", "error"}
        for name, status in statuses.items()
    ):
        raise VLMProcessProtocolError("record_statuses_invalid")
    return VLMExecutionRecord(
        task_id=record["task_id"],
        worker_thread_id=record["worker_thread_id"],
        started_monotonic_ns=record["started_monotonic_ns"],
        finished_monotonic_ns=record["finished_monotonic_ns"],
        execution_outcome=record["execution_outcome"],
        error_code=record["error_code"],
        input_sha256=input_value["sha256"],
        input_size_bytes=input_value["size_bytes"],
        output_sha256=None if output_value is None else output_value["sha256"],
        output_length=None if output_value is None else output_value["length"],
        translation_route=record["translation_route"],
        model_unload_requested=residency["unload_requested"],
        model_unload_confirmed=residency["unload_confirmed"],
        stage_durations_ns=dict(durations),
        stage_status=dict(statuses),
        cancellation_requested=cancellation["requested"],
        worker_observed_cancellation=cancellation["worker_observed"],
        backend_stop_confirmed=cancellation["backend_stop_confirmed"],
    )


def _decode_worker_message(
    message: Mapping[str, object],
) -> tuple[str, object]:
    message_type = message.get("type")
    if message_type == "worker_started":
        _require_keys(
            message,
            {"type", "protocol_version", "monotonic_ns"},
            "worker_started",
        )
        if message.get("protocol_version") != PROCESS_PROTOCOL_VERSION:
            raise VLMProcessProtocolError("process_protocol_version_mismatch")
        return message_type, _non_negative_integer(
            message.get("monotonic_ns"),
            "worker_started_time",
        )
    if message_type == "inference_started":
        _require_keys(
            message,
            {"type", "monotonic_ns"},
            "inference_started",
        )
        return message_type, _non_negative_integer(
            message.get("monotonic_ns"),
            "inference_started_time",
        )
    if message_type == "completed":
        _require_keys(
            message,
            {"type", "result", "record", "monotonic_ns"},
            "completed",
        )
        _non_negative_integer(message.get("monotonic_ns"), "completed_time")
        return message_type, (
            _deserialize_result(message.get("result")),
            _deserialize_record(message.get("record")),
        )
    if message_type == "failed":
        _require_keys(
            message,
            {"type", "error_code", "monotonic_ns"},
            "failed",
        )
        _non_negative_integer(message.get("monotonic_ns"), "failed_time")
        error_code = message.get("error_code")
        if not isinstance(error_code, str) or not _ERROR_CODE_RE.fullmatch(error_code):
            raise VLMProcessProtocolError("failed_error_code_invalid")
        return message_type, error_code
    raise VLMProcessProtocolError("process_message_type_invalid")


def _resolve_factory(reference: object) -> Callable[[], object]:
    if not isinstance(reference, str) or not _FACTORY_REF_RE.fullmatch(reference):
        raise VLMProcessProtocolError("factory_ref_invalid")
    module_name, attribute_path = reference.split(":", 1)
    value: object = importlib.import_module(module_name)
    for attribute in attribute_path.split("."):
        value = getattr(value, attribute)
    if not callable(value):
        raise VLMProcessProtocolError("factory_not_callable")
    return value


def _process_worker_main(
    connection: Connection,
    cancellation_event: object,
) -> None:
    send_lock = threading.Lock()
    monitor_stop = threading.Event()
    monitor: threading.Thread | None = None
    try:
        _send_message(
            connection,
            {
                "type": "worker_started",
                "protocol_version": PROCESS_PROTOCOL_VERSION,
                "monotonic_ns": time.monotonic_ns(),
            },
            lock=send_lock,
        )
        message = _receive_message(connection)
        if set(message) != {
            "type",
            "protocol_version",
            "factory_ref",
            "claimed",
        }:
            raise VLMProcessProtocolError("start_fields_invalid")
        if message["type"] != "start":
            raise VLMProcessProtocolError("start_message_missing")
        if message["protocol_version"] != PROCESS_PROTOCOL_VERSION:
            raise VLMProcessProtocolError("process_protocol_version_mismatch")
        claimed = _deserialize_claimed(message["claimed"], cancellation_event)
        factory = _resolve_factory(message["factory_ref"])
        adapter = factory()
        inference_event = getattr(adapter, "inference_started_event", None)
        if not callable(getattr(inference_event, "wait", None)):
            raise VLMProcessProtocolError("inference_signal_missing")

        def publish_inference_start() -> None:
            while not monitor_stop.is_set():
                if inference_event.wait(0.02):
                    _send_message(
                        connection,
                        {
                            "type": "inference_started",
                            "monotonic_ns": time.monotonic_ns(),
                        },
                        lock=send_lock,
                    )
                    return

        monitor = threading.Thread(
            target=publish_inference_start,
            name="phase1-vlm-process-signal",
        )
        monitor.start()
        result = adapter(claimed)
        record = getattr(adapter, "last_record", None)
        if not isinstance(result, ResultEnvelope):
            raise VLMProcessProtocolError("child_result_invalid")
        if not isinstance(record, VLMExecutionRecord):
            raise VLMProcessProtocolError("child_record_missing")
        _send_message(
            connection,
            {
                "type": "completed",
                "result": _serialize_result(result),
                "record": record.to_dict(),
                "monotonic_ns": time.monotonic_ns(),
            },
            lock=send_lock,
        )
    except BaseException as exc:
        try:
            _send_message(
                connection,
                {
                    "type": "failed",
                    "error_code": _bounded_error_code(
                        "process_worker", type(exc).__name__
                    ),
                    "monotonic_ns": time.monotonic_ns(),
                },
                lock=send_lock,
            )
        except BaseException:
            pass
    finally:
        monitor_stop.set()
        if monitor is not None:
            monitor.join(0.2)
        connection.close()


@dataclass(frozen=True, slots=True)
class VLMProcessReport:
    """Bounded supervisor facts for one child-process invocation."""

    protocol_version: str
    start_method: str
    process_name: str
    process_id: int | None
    spawn_requested_monotonic_ns: int
    child_started_monotonic_ns: int | None
    inference_started_monotonic_ns: int | None
    completion_received_monotonic_ns: int | None
    joined_monotonic_ns: int
    exit_code: int | None
    cancellation_forwarded: bool
    cancellation_forwarded_monotonic_ns: int | None
    terminate_requested: bool
    terminate_confirmed: bool
    protocol_complete: bool
    error_code: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "protocol_version": self.protocol_version,
            "start_method": self.start_method,
            "process_name": self.process_name,
            "process_id": self.process_id,
            "spawn_requested_monotonic_ns": self.spawn_requested_monotonic_ns,
            "child_started_monotonic_ns": self.child_started_monotonic_ns,
            "inference_started_monotonic_ns": self.inference_started_monotonic_ns,
            "completion_received_monotonic_ns": (self.completion_received_monotonic_ns),
            "joined_monotonic_ns": self.joined_monotonic_ns,
            "exit_code": self.exit_code,
            "cancellation_forwarded": self.cancellation_forwarded,
            "cancellation_forwarded_monotonic_ns": (
                self.cancellation_forwarded_monotonic_ns
            ),
            "terminate_requested": self.terminate_requested,
            "terminate_confirmed": self.terminate_confirmed,
            "protocol_complete": self.protocol_complete,
            "error_code": self.error_code,
        }


class ProcessIsolatedVLMAdapter:
    """Supervise one fixed-input VLM invocation in a spawned process."""

    def __init__(
        self,
        *,
        factory_ref: str = DEFAULT_FACTORY_REF,
        execution_timeout_s: float = 720.0,
        poll_interval_s: float = 0.02,
        join_timeout_s: float = 5.0,
        terminate_join_timeout_s: float = 5.0,
        start_method: str = "spawn",
        clock_ns: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        if not isinstance(factory_ref, str) or not _FACTORY_REF_RE.fullmatch(
            factory_ref
        ):
            raise ValueError("factory_ref must be a module:attribute reference")
        for name, value in (
            ("execution_timeout_s", execution_timeout_s),
            ("poll_interval_s", poll_interval_s),
            ("join_timeout_s", join_timeout_s),
            ("terminate_join_timeout_s", terminate_join_timeout_s),
        ):
            _finite_positive_seconds(value, name)
        if start_method != "spawn":
            raise ValueError("start_method must be spawn")
        if not callable(clock_ns):
            raise TypeError("clock_ns must be callable")
        self._factory_ref = factory_ref
        self._execution_timeout_s = float(execution_timeout_s)
        self._poll_interval_s = float(poll_interval_s)
        self._join_timeout_s = float(join_timeout_s)
        self._terminate_join_timeout_s = float(terminate_join_timeout_s)
        self._start_method = start_method
        self._clock_ns = clock_ns
        self._invocation_lock = threading.Lock()
        self._record_lock = threading.Lock()
        self._last_record: VLMExecutionRecord | None = None
        self._last_process_report: VLMProcessReport | None = None
        self.inference_started_event = threading.Event()

    @property
    def last_record(self) -> VLMExecutionRecord | None:
        with self._record_lock:
            return self._last_record

    @property
    def last_process_report(self) -> VLMProcessReport | None:
        with self._record_lock:
            return self._last_process_report

    def _publish(
        self,
        record: VLMExecutionRecord,
        process_report: VLMProcessReport,
    ) -> None:
        with self._record_lock:
            self._last_record = record
            self._last_process_report = process_report

    def _failure_result(
        self,
        claimed: ClaimedTask,
        *,
        outcome: ExecutionOutcome,
        error_code: str,
    ) -> tuple[ResultEnvelope, VLMExecutionRecord]:
        finished = max(claimed.started_monotonic_ns, self._clock_ns())
        requested = claimed.cancellation_token.is_requested()
        cancellation = CancellationReport(
            requested=requested,
            client_wait_stopped=False,
            worker_observed=requested,
            backend_stop_confirmed=None,
        )
        result = ResultEnvelope(
            task_id=claimed.task.task_id,
            task_kind=claimed.task.task_kind,
            state_token=claimed.task.state_token,
            source_monotonic_ns=claimed.task.source_monotonic_ns,
            deadline_monotonic_ns=claimed.task.deadline_monotonic_ns,
            input_sha256=claimed.task.payload.sha256,
            started_monotonic_ns=claimed.started_monotonic_ns,
            finished_monotonic_ns=finished,
            execution_outcome=outcome,
            error_code=error_code,
            cancellation_report=cancellation,
        )
        record = VLMExecutionRecord(
            task_id=claimed.task.task_id,
            worker_thread_id=threading.get_ident(),
            started_monotonic_ns=claimed.started_monotonic_ns,
            finished_monotonic_ns=finished,
            execution_outcome=outcome.value,
            error_code=error_code,
            input_sha256=claimed.task.payload.sha256,
            input_size_bytes=claimed.task.payload.size_bytes,
            output_sha256=None,
            output_length=None,
            translation_route=None,
            model_unload_requested=False,
            model_unload_confirmed=None,
            stage_durations_ns={},
            stage_status={},
            cancellation_requested=requested,
            worker_observed_cancellation=requested,
            backend_stop_confirmed=None,
        )
        return result, record

    def __call__(self, claimed: ClaimedTask) -> ResultEnvelope:
        if not isinstance(claimed, ClaimedTask):
            raise TypeError("claimed must be a ClaimedTask")
        if not self._invocation_lock.acquire(blocking=False):
            raise VLMProcessProtocolError("vlm_process_adapter_busy")
        self.inference_started_event.clear()
        context = multiprocessing.get_context(self._start_method)
        parent_connection, child_connection = context.Pipe(duplex=True)
        cancellation_event = context.Event()
        process = context.Process(
            target=_process_worker_main,
            args=(child_connection, cancellation_event),
            name="phase1-vlm-process-worker",
            daemon=False,
        )
        spawn_requested_ns = self._clock_ns()
        child_started_ns: int | None = None
        inference_started_ns: int | None = None
        completion_received_ns: int | None = None
        cancellation_forwarded_ns: int | None = None
        terminate_requested = False
        terminate_confirmed = False
        protocol_complete = False
        error_code: str | None = None
        result: ResultEnvelope | None = None
        record: VLMExecutionRecord | None = None
        try:
            try:
                process.start()
            except BaseException as exc:
                error_code = _bounded_error_code("process_start", type(exc).__name__)
            finally:
                child_connection.close()

            if error_code is None:
                try:
                    _send_message(
                        parent_connection,
                        {
                            "type": "start",
                            "protocol_version": PROCESS_PROTOCOL_VERSION,
                            "factory_ref": self._factory_ref,
                            "claimed": _serialize_claimed(claimed),
                        },
                    )
                except (OSError, VLMProcessProtocolError):
                    error_code = "process_start_message_failed"
                    terminate_requested = True
            if error_code is None:
                deadline = time.monotonic() + self._execution_timeout_s
                while result is None and error_code is None:
                    if (
                        claimed.cancellation_token.is_requested()
                        and cancellation_forwarded_ns is None
                    ):
                        cancellation_event.set()
                        cancellation_forwarded_ns = self._clock_ns()
                    if parent_connection.poll(self._poll_interval_s):
                        try:
                            message = _receive_message(parent_connection)
                        except _VLMProcessConnectionClosed:
                            error_code = "process_worker_exit"
                            continue
                        except VLMProcessProtocolError:
                            error_code = "process_message_invalid"
                            continue
                        try:
                            message_type, payload = _decode_worker_message(message)
                        except VLMProcessProtocolError as exc:
                            error_code = str(exc)
                            if not _ERROR_CODE_RE.fullmatch(error_code):
                                error_code = "process_message_invalid"
                            continue
                        if message_type == "worker_started":
                            assert isinstance(payload, int)
                            child_started_ns = payload
                        elif message_type == "inference_started":
                            assert isinstance(payload, int)
                            inference_started_ns = payload
                            self.inference_started_event.set()
                        elif message_type == "completed":
                            assert isinstance(payload, tuple)
                            result, record = payload
                            completion_received_ns = self._clock_ns()
                            protocol_complete = True
                        elif message_type == "failed":
                            assert isinstance(payload, str)
                            error_code = payload
                    elif not process.is_alive():
                        error_code = "process_worker_exit"
                    elif time.monotonic() >= deadline:
                        error_code = "process_worker_timeout"
                        terminate_requested = True

            if process.pid is not None:
                if terminate_requested and process.is_alive():
                    process.terminate()
                    process.join(self._terminate_join_timeout_s)
                else:
                    process.join(self._join_timeout_s)
                    if process.is_alive():
                        terminate_requested = True
                        process.terminate()
                        process.join(self._terminate_join_timeout_s)
                terminate_confirmed = terminate_requested and not process.is_alive()

            if result is None or record is None:
                outcome = (
                    ExecutionOutcome.TIMEOUT
                    if error_code == "process_worker_timeout"
                    else ExecutionOutcome.ERROR
                )
                result, record = self._failure_result(
                    claimed,
                    outcome=outcome,
                    error_code=error_code or "process_worker_failed",
                )
            elif claimed.cancellation_token.is_requested() and (
                result.execution_outcome is ExecutionOutcome.OK
            ):
                cancellation = CancellationReport(
                    requested=True,
                    client_wait_stopped=False,
                    worker_observed=True,
                    backend_stop_confirmed=None,
                )
                result = replace(
                    result,
                    execution_outcome=ExecutionOutcome.CANCEL_OBSERVED,
                    cancellation_report=cancellation,
                )
                record = replace(
                    record,
                    execution_outcome=ExecutionOutcome.CANCEL_OBSERVED.value,
                    cancellation_requested=True,
                    worker_observed_cancellation=True,
                    backend_stop_confirmed=None,
                )

            report = VLMProcessReport(
                protocol_version=PROCESS_PROTOCOL_VERSION,
                start_method=self._start_method,
                process_name=process.name,
                process_id=process.pid,
                spawn_requested_monotonic_ns=spawn_requested_ns,
                child_started_monotonic_ns=child_started_ns,
                inference_started_monotonic_ns=inference_started_ns,
                completion_received_monotonic_ns=completion_received_ns,
                joined_monotonic_ns=self._clock_ns(),
                exit_code=process.exitcode,
                cancellation_forwarded=cancellation_forwarded_ns is not None,
                cancellation_forwarded_monotonic_ns=cancellation_forwarded_ns,
                terminate_requested=terminate_requested,
                terminate_confirmed=terminate_confirmed,
                protocol_complete=protocol_complete,
                error_code=error_code,
            )
            self._publish(record, report)
            return result
        finally:
            parent_connection.close()
            if process.pid is not None and process.is_alive():
                process.terminate()
                process.join(self._terminate_join_timeout_s)
            self._invocation_lock.release()
