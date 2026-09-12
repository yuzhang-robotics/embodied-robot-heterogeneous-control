"""Owned-child sequential file prefetch for the Phase 2 host boundary."""

from __future__ import annotations

import math
import multiprocessing
import re
import time
from dataclasses import asdict, dataclass
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Callable, Mapping


PREFETCH_ACTION_SCHEMA_VERSION = "0.1.0"
DEFAULT_BUFFER_SIZE_BYTES = 4 * 1024 * 1024
DEFAULT_TIMEOUT_S = 20.0
_MAX_BUFFER_SIZE_BYTES = 16 * 1024 * 1024
_MAX_TIMEOUT_S = 120.0
_POLL_INTERVAL_S = 0.01
_JOIN_GRACE_S = 1.0
_ERROR_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_STATUSES = {"ok", "error", "timeout"}


class PrefetchInputError(ValueError):
    """The file or bounded action configuration is invalid."""


class PrefetchLifecycleError(RuntimeError):
    """The owned child could not be started or deterministically reaped."""


@dataclass(frozen=True, slots=True)
class PrefetchActionRecord:
    """Privacy-preserving result of one owned sequential prefetch action."""

    prefetch_action_schema_version: str
    status: str
    expected_size_bytes: int
    buffer_size_bytes: int
    timeout_ms: float
    action_started_monotonic_ns: int
    child_started_monotonic_ns: int | None
    read_completed_monotonic_ns: int | None
    child_finished_monotonic_ns: int | None
    action_finished_monotonic_ns: int
    bytes_read: int | None
    chunk_count: int | None
    child_exit_code: int | None
    child_protocol_complete: bool
    terminate_requested: bool
    kill_requested: bool
    child_joined: bool
    path_recorded: bool
    contents_recorded: bool
    pid_recorded: bool
    error_code: str | None

    def to_dict(self) -> dict[str, object]:
        value = asdict(self)
        validate_prefetch_action_record(value)
        return value


_RECORD_FIELDS = set(PrefetchActionRecord.__dataclass_fields__)
_CHILD_MESSAGE_FIELDS = {
    "status",
    "started_monotonic_ns",
    "read_completed_monotonic_ns",
    "finished_monotonic_ns",
    "bytes_read",
    "chunk_count",
    "error_code",
}


def _positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise PrefetchInputError(f"{name} must be a positive integer")
    return value


def _positive_finite(value: object, name: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) <= 0
    ):
        raise PrefetchInputError(f"{name} must be positive and finite")
    return float(value)


def _error_code(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _ERROR_CODE_RE.fullmatch(value):
        return None
    return value


def _child_error_code(exc: BaseException) -> str:
    if isinstance(exc, FileNotFoundError):
        return "file_missing"
    if isinstance(exc, PermissionError):
        return "file_permission_denied"
    if isinstance(exc, IsADirectoryError):
        return "not_regular_file"
    if isinstance(exc, OSError):
        return "file_read_error"
    return "unexpected_child_error"


def _prefetch_child(
    connection: Connection,
    path: str,
    expected_size_bytes: int,
    buffer_size_bytes: int,
) -> None:
    """Read exactly one file and send one bounded scalar-only message."""

    started_ns = time.monotonic_ns()
    bytes_read = 0
    chunk_count = 0
    read_completed_ns: int | None = None
    status = "error"
    error_code: str | None = None
    try:
        resolved = Path(path)
        if not resolved.is_file():
            raise FileNotFoundError
        if resolved.stat().st_size != expected_size_bytes:
            error_code = "file_size_changed"
        else:
            buffer = bytearray(buffer_size_bytes)
            with resolved.open("rb", buffering=0) as stream:
                while bytes_read < expected_size_bytes:
                    count = stream.readinto(buffer)
                    if count is None or count <= 0:
                        break
                    bytes_read += count
                    chunk_count += 1
                extra = stream.read(1)
            if bytes_read != expected_size_bytes or extra:
                error_code = "file_size_changed"
            else:
                read_completed_ns = time.monotonic_ns()
                status = "ok"
    except BaseException as exc:
        error_code = _child_error_code(exc)
    finally:
        finished_ns = time.monotonic_ns()
        message = {
            "status": status,
            "started_monotonic_ns": started_ns,
            "read_completed_monotonic_ns": read_completed_ns,
            "finished_monotonic_ns": finished_ns,
            "bytes_read": bytes_read,
            "chunk_count": chunk_count,
            "error_code": error_code,
        }
        try:
            connection.send(message)
        finally:
            connection.close()


ChildWorker = Callable[[Connection, str, int, int], None]


def _validated_child_message(
    value: object,
    *,
    expected_size_bytes: int,
    buffer_size_bytes: int,
) -> dict[str, object] | None:
    if not isinstance(value, Mapping) or set(value) != _CHILD_MESSAGE_FIELDS:
        return None
    status = value.get("status")
    started = value.get("started_monotonic_ns")
    completed = value.get("read_completed_monotonic_ns")
    finished = value.get("finished_monotonic_ns")
    bytes_read = value.get("bytes_read")
    chunk_count = value.get("chunk_count")
    error_code = _error_code(value.get("error_code"))
    if (
        status not in {"ok", "error"}
        or isinstance(started, bool)
        or not isinstance(started, int)
        or started <= 0
        or isinstance(finished, bool)
        or not isinstance(finished, int)
        or finished < started
        or isinstance(bytes_read, bool)
        or not isinstance(bytes_read, int)
        or not 0 <= bytes_read <= expected_size_bytes
        or isinstance(chunk_count, bool)
        or not isinstance(chunk_count, int)
        or chunk_count < 0
    ):
        return None
    if status == "ok":
        expected_chunks = math.ceil(expected_size_bytes / buffer_size_bytes)
        if (
            isinstance(completed, bool)
            or not isinstance(completed, int)
            or not started <= completed <= finished
            or bytes_read != expected_size_bytes
            or chunk_count != expected_chunks
            or error_code is not None
        ):
            return None
    elif completed is not None or error_code is None:
        return None
    return dict(value)


def _stop_and_reap(
    process: multiprocessing.Process,
    *,
    terminate_requested: bool,
) -> tuple[bool, bool]:
    kill_requested = False
    if not terminate_requested:
        process.join(_JOIN_GRACE_S)
        terminate_requested = process.is_alive()
    if terminate_requested and process.is_alive():
        process.terminate()
    process.join(_JOIN_GRACE_S)
    if process.is_alive():
        kill_requested = True
        process.kill()
        process.join(_JOIN_GRACE_S)
    if process.is_alive():
        raise PrefetchLifecycleError("prefetch child could not be reaped")
    return terminate_requested, kill_requested


def run_sequential_prefetch(
    path: Path | str,
    *,
    expected_size_bytes: int,
    buffer_size_bytes: int = DEFAULT_BUFFER_SIZE_BYTES,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    start_method: str = "spawn",
    _worker: ChildWorker = _prefetch_child,
) -> PrefetchActionRecord:
    """Run one bounded child-owned sequential file read.

    Input identity is a later preflight responsibility. This action verifies
    only that the selected regular file still has the expected size and that
    the child reports reading exactly that many bytes.
    """

    expected = _positive_int(expected_size_bytes, "expected_size_bytes")
    buffer_size = _positive_int(buffer_size_bytes, "buffer_size_bytes")
    timeout = _positive_finite(timeout_s, "timeout_s")
    if buffer_size > _MAX_BUFFER_SIZE_BYTES:
        raise PrefetchInputError("buffer_size_bytes exceeds the bounded maximum")
    if timeout > _MAX_TIMEOUT_S:
        raise PrefetchInputError("timeout_s exceeds the bounded maximum")
    if not isinstance(start_method, str) or not start_method:
        raise PrefetchInputError("start_method must be a non-empty string")
    if not callable(_worker):
        raise TypeError("_worker must be callable")

    resolved = Path(path).expanduser().resolve()
    if not resolved.is_file():
        raise PrefetchInputError("prefetch target must be a regular file")
    if resolved.stat().st_size != expected:
        raise PrefetchInputError("prefetch target size does not match expected size")

    try:
        context = multiprocessing.get_context(start_method)
    except ValueError as exc:
        raise PrefetchInputError("unsupported multiprocessing start method") from exc
    receive_connection, send_connection = context.Pipe(duplex=False)
    process = context.Process(
        target=_worker,
        args=(send_connection, str(resolved), expected, buffer_size),
        name="phase2-whisper-prefetch",
        daemon=False,
    )
    action_started_ns = time.monotonic_ns()
    try:
        process.start()
    except BaseException as exc:
        receive_connection.close()
        send_connection.close()
        raise PrefetchLifecycleError("prefetch child could not be started") from exc
    send_connection.close()

    message: object | None = None
    timed_out = False
    deadline = time.monotonic() + timeout
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            if receive_connection.poll(min(_POLL_INTERVAL_S, remaining)):
                try:
                    message = receive_connection.recv()
                except EOFError:
                    message = None
                break
            if not process.is_alive():
                if receive_connection.poll(_POLL_INTERVAL_S):
                    try:
                        message = receive_connection.recv()
                    except EOFError:
                        message = None
                break
    finally:
        receive_connection.close()

    terminate_requested, kill_requested = _stop_and_reap(
        process,
        terminate_requested=timed_out,
    )
    action_finished_ns = time.monotonic_ns()
    child = _validated_child_message(
        message,
        expected_size_bytes=expected,
        buffer_size_bytes=buffer_size,
    )

    if timed_out:
        status = "timeout"
        error_code = "timeout"
    elif child is None:
        status = "error"
        error_code = "child_protocol_incomplete"
    elif child["status"] == "error":
        status = "error"
        error_code = str(child["error_code"])
    elif process.exitcode != 0:
        status = "error"
        error_code = "child_exit_nonzero"
    else:
        status = "ok"
        error_code = None

    record = PrefetchActionRecord(
        prefetch_action_schema_version=PREFETCH_ACTION_SCHEMA_VERSION,
        status=status,
        expected_size_bytes=expected,
        buffer_size_bytes=buffer_size,
        timeout_ms=timeout * 1000,
        action_started_monotonic_ns=action_started_ns,
        child_started_monotonic_ns=(
            int(child["started_monotonic_ns"]) if child is not None else None
        ),
        read_completed_monotonic_ns=(
            int(child["read_completed_monotonic_ns"])
            if child is not None and child["read_completed_monotonic_ns"] is not None
            else None
        ),
        child_finished_monotonic_ns=(
            int(child["finished_monotonic_ns"]) if child is not None else None
        ),
        action_finished_monotonic_ns=action_finished_ns,
        bytes_read=int(child["bytes_read"]) if child is not None else None,
        chunk_count=int(child["chunk_count"]) if child is not None else None,
        child_exit_code=process.exitcode,
        child_protocol_complete=child is not None,
        terminate_requested=terminate_requested,
        kill_requested=kill_requested,
        child_joined=not process.is_alive(),
        path_recorded=False,
        contents_recorded=False,
        pid_recorded=False,
        error_code=error_code,
    )
    validate_prefetch_action_record(record.to_dict())
    return record


def validate_prefetch_action_record(value: Mapping[str, object]) -> None:
    """Fail closed unless a serialized action record satisfies the contract."""

    if set(value) != _RECORD_FIELDS:
        raise ValueError("prefetch action record fields are invalid")
    if value.get("prefetch_action_schema_version") != PREFETCH_ACTION_SCHEMA_VERSION:
        raise ValueError("unsupported prefetch action schema version")
    status = value.get("status")
    if status not in _STATUSES:
        raise ValueError("prefetch action status is invalid")
    expected = value.get("expected_size_bytes")
    buffer_size = value.get("buffer_size_bytes")
    timeout_ms = value.get("timeout_ms")
    if (
        isinstance(expected, bool)
        or not isinstance(expected, int)
        or expected <= 0
        or isinstance(buffer_size, bool)
        or not isinstance(buffer_size, int)
        or not 0 < buffer_size <= _MAX_BUFFER_SIZE_BYTES
        or isinstance(timeout_ms, bool)
        or not isinstance(timeout_ms, (int, float))
        or not math.isfinite(float(timeout_ms))
        or not 0 < float(timeout_ms) <= _MAX_TIMEOUT_S * 1000
    ):
        raise ValueError("prefetch action bounds are invalid")

    started = value.get("action_started_monotonic_ns")
    finished = value.get("action_finished_monotonic_ns")
    if (
        isinstance(started, bool)
        or not isinstance(started, int)
        or started <= 0
        or isinstance(finished, bool)
        or not isinstance(finished, int)
        or finished < started
    ):
        raise ValueError("prefetch action timestamps are invalid")

    for name in (
        "child_protocol_complete",
        "terminate_requested",
        "kill_requested",
        "child_joined",
        "path_recorded",
        "contents_recorded",
        "pid_recorded",
    ):
        if not isinstance(value.get(name), bool):
            raise ValueError(f"{name} must be boolean")
    if (
        value.get("child_joined") is not True
        or value.get("path_recorded") is not False
        or value.get("contents_recorded") is not False
        or value.get("pid_recorded") is not False
    ):
        raise ValueError("prefetch action lifecycle or privacy flags are invalid")

    child_started = value.get("child_started_monotonic_ns")
    read_completed = value.get("read_completed_monotonic_ns")
    child_finished = value.get("child_finished_monotonic_ns")
    child_times = (child_started, read_completed, child_finished)
    for item in child_times:
        if item is not None and (
            isinstance(item, bool) or not isinstance(item, int) or item <= 0
        ):
            raise ValueError("prefetch child timestamps are invalid")
    if child_started is not None and child_finished is not None:
        if not started <= child_started <= child_finished <= finished:
            raise ValueError("prefetch lifecycle timestamp order is invalid")
    if read_completed is not None and (
        child_started is None
        or child_finished is None
        or not child_started <= read_completed <= child_finished
    ):
        raise ValueError("prefetch read timestamp order is invalid")

    bytes_read = value.get("bytes_read")
    chunk_count = value.get("chunk_count")
    exit_code = value.get("child_exit_code")
    if bytes_read is not None and (
        isinstance(bytes_read, bool)
        or not isinstance(bytes_read, int)
        or not 0 <= bytes_read <= expected
    ):
        raise ValueError("prefetch byte count is invalid")
    if chunk_count is not None and (
        isinstance(chunk_count, bool)
        or not isinstance(chunk_count, int)
        or chunk_count < 0
    ):
        raise ValueError("prefetch chunk count is invalid")
    if exit_code is not None and (
        isinstance(exit_code, bool) or not isinstance(exit_code, int)
    ):
        raise ValueError("prefetch child exit code is invalid")
    error_code = value.get("error_code")
    if error_code is not None and _error_code(error_code) is None:
        raise ValueError("prefetch error code is invalid")

    if status == "ok":
        if (
            value.get("child_protocol_complete") is not True
            or value.get("terminate_requested") is not False
            or value.get("kill_requested") is not False
            or child_started is None
            or read_completed is None
            or child_finished is None
            or bytes_read != expected
            or chunk_count != math.ceil(expected / buffer_size)
            or exit_code != 0
            or error_code is not None
        ):
            raise ValueError("successful prefetch action is inconsistent")
    else:
        if error_code is None:
            raise ValueError("failed prefetch action requires an error code")
        if status == "timeout" and value.get("terminate_requested") is not True:
            raise ValueError("timed-out prefetch action must request termination")
