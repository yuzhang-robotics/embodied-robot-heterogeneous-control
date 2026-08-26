"""Immutable task and result contracts for the Phase 1 runtime."""

from __future__ import annotations

import json
import math
import re
import threading
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Mapping, TypeAlias


MAX_IDENTIFIER_LENGTH = 128
MAX_REFERENCE_LENGTH = 1024
MAX_MEDIA_TYPE_LENGTH = 128
MAX_METADATA_KEYS = 16
MAX_METADATA_KEY_LENGTH = 64
MAX_METADATA_STRING_LENGTH = 256
MAX_METADATA_BYTES = 1024

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MEDIA_TYPE_RE = re.compile(r"^[a-z0-9][a-z0-9.+-]*/[a-z0-9][a-z0-9.+-]*$")
_METADATA_KEY_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_ERROR_CODE_RE = re.compile(r"^[a-z][a-z0-9_]*$")

MetadataScalar: TypeAlias = str | int | float | bool | None


class TaskKind(str, Enum):
    """Workload identity accepted by the first runtime version."""

    SIMULATED = "simulated"
    VLM = "vlm"
    ASR = "asr"
    LLM = "llm"


class ExecutionOutcome(str, Enum):
    """What happened while the workload adapter executed."""

    OK = "ok"
    ERROR = "error"
    TIMEOUT = "timeout"
    CANCEL_OBSERVED = "cancel_observed"


class TaskLocation(str, Enum):
    """The one runtime-owned location of an admitted task."""

    QUEUED = "queued"
    RUNNING = "running"
    RESULT_PENDING = "result_pending"
    TERMINAL = "terminal"


class FinalDisposition(str, Enum):
    """The single final delivery decision assigned to one task."""

    CONSUMED = "consumed"
    DROPPED_OVERFLOW = "dropped_overflow"
    REJECTED_BUSY = "rejected_busy"
    CANCELLED_QUEUED = "cancelled_queued"
    REJECTED_CANCELLED = "rejected_cancelled"
    REJECTED_EXPIRED = "rejected_expired"
    REJECTED_STATE = "rejected_state"
    REJECTED_IDENTITY = "rejected_identity"
    EXECUTION_ERROR = "execution_error"
    RESULT_BACKPRESSURE = "result_backpressure"
    SHUTDOWN_CANCELLED = "shutdown_cancelled"


def _require_non_negative_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer")
    return value


def _require_bounded_text(
    value: object,
    field_name: str,
    *,
    max_length: int,
    optional: bool = False,
) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    if not 1 <= len(value) <= max_length:
        raise ValueError(f"{field_name} must contain 1 to {max_length} characters")
    if value != value.strip() or not value.isprintable():
        raise ValueError(f"{field_name} must be printable without outer whitespace")
    return value


def _require_sha256(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ValueError(f"{field_name} must be a lowercase SHA-256 hex digest")
    return value


def _freeze_metadata(
    metadata: Mapping[str, MetadataScalar],
) -> Mapping[str, MetadataScalar]:
    if not isinstance(metadata, Mapping):
        raise TypeError("metadata must be a mapping")
    if len(metadata) > MAX_METADATA_KEYS:
        raise ValueError(f"metadata must contain at most {MAX_METADATA_KEYS} keys")

    copied: dict[str, MetadataScalar] = {}
    for key, value in metadata.items():
        if not isinstance(key, str) or not _METADATA_KEY_RE.fullmatch(key):
            raise ValueError("metadata keys must use lower_snake_case")
        if len(key) > MAX_METADATA_KEY_LENGTH:
            raise ValueError(
                f"metadata keys must not exceed {MAX_METADATA_KEY_LENGTH} characters"
            )
        if not isinstance(value, (str, int, float, bool, type(None))):
            raise TypeError("metadata values must be JSON scalar values")
        if isinstance(value, str) and len(value) > MAX_METADATA_STRING_LENGTH:
            raise ValueError(
                "metadata string values must not exceed "
                f"{MAX_METADATA_STRING_LENGTH} characters"
            )
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("metadata floats must be finite")
        copied[key] = value

    encoded = json.dumps(
        copied,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > MAX_METADATA_BYTES:
        raise ValueError(
            f"serialized metadata must not exceed {MAX_METADATA_BYTES} bytes"
        )
    return MappingProxyType(copied)


@dataclass(frozen=True, slots=True)
class StateToken:
    """A generation within one independent state scope."""

    scope_id: str
    generation: int

    def __post_init__(self) -> None:
        _require_bounded_text(
            self.scope_id,
            "scope_id",
            max_length=MAX_IDENTIFIER_LENGTH,
        )
        _require_non_negative_int(self.generation, "generation")


@dataclass(frozen=True, slots=True)
class PayloadRef:
    """Private payload reference plus immutable content identity."""

    ref: str = field(repr=False)
    sha256: str
    size_bytes: int
    media_type: str

    def __post_init__(self) -> None:
        _require_bounded_text(
            self.ref,
            "ref",
            max_length=MAX_REFERENCE_LENGTH,
        )
        _require_sha256(self.sha256, "sha256")
        _require_non_negative_int(self.size_bytes, "size_bytes")
        if (
            not isinstance(self.media_type, str)
            or len(self.media_type) > MAX_MEDIA_TYPE_LENGTH
            or not _MEDIA_TYPE_RE.fullmatch(self.media_type)
        ):
            raise ValueError("media_type must be a bounded lowercase MIME type")


@dataclass(frozen=True, slots=True)
class TaskEnvelope:
    """Immutable task admitted to one bounded inference lane."""

    task_id: str
    task_kind: TaskKind
    source_monotonic_ns: int
    created_monotonic_ns: int
    deadline_monotonic_ns: int
    state_token: StateToken
    payload: PayloadRef
    parent_task_id: str | None = None
    supersession_key: str | None = None
    metadata: Mapping[str, MetadataScalar] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_bounded_text(
            self.task_id,
            "task_id",
            max_length=MAX_IDENTIFIER_LENGTH,
        )
        if not isinstance(self.task_kind, TaskKind):
            raise TypeError("task_kind must be a TaskKind")
        source = _require_non_negative_int(
            self.source_monotonic_ns,
            "source_monotonic_ns",
        )
        created = _require_non_negative_int(
            self.created_monotonic_ns,
            "created_monotonic_ns",
        )
        deadline = _require_non_negative_int(
            self.deadline_monotonic_ns,
            "deadline_monotonic_ns",
        )
        if source > created:
            raise ValueError("source_monotonic_ns must not be after creation")
        if created > deadline:
            raise ValueError("deadline_monotonic_ns must not be before creation")
        if not isinstance(self.state_token, StateToken):
            raise TypeError("state_token must be a StateToken")
        if not isinstance(self.payload, PayloadRef):
            raise TypeError("payload must be a PayloadRef")
        _require_bounded_text(
            self.parent_task_id,
            "parent_task_id",
            max_length=MAX_IDENTIFIER_LENGTH,
            optional=True,
        )
        _require_bounded_text(
            self.supersession_key,
            "supersession_key",
            max_length=MAX_IDENTIFIER_LENGTH,
            optional=True,
        )
        object.__setattr__(self, "metadata", _freeze_metadata(self.metadata))


@dataclass(frozen=True, slots=True)
class CancellationReport:
    """Separate local cancellation facts from backend-stop confirmation."""

    requested: bool = False
    client_wait_stopped: bool = False
    worker_observed: bool = False
    backend_stop_confirmed: bool | None = None

    def __post_init__(self) -> None:
        values = (
            self.requested,
            self.client_wait_stopped,
            self.worker_observed,
        )
        if not all(isinstance(value, bool) for value in values):
            raise TypeError("cancellation flags must be booleans")
        if self.backend_stop_confirmed is not None and not isinstance(
            self.backend_stop_confirmed, bool
        ):
            raise TypeError("backend_stop_confirmed must be bool or None")
        if not self.requested and any(
            (
                self.client_wait_stopped,
                self.worker_observed,
                self.backend_stop_confirmed is not None,
            )
        ):
            raise ValueError("cancellation effects require requested=True")


@dataclass(frozen=True, slots=True)
class ResultEnvelope:
    """Privacy-preserving descriptor returned by a workload adapter."""

    task_id: str
    task_kind: TaskKind
    state_token: StateToken
    source_monotonic_ns: int
    deadline_monotonic_ns: int
    input_sha256: str
    started_monotonic_ns: int
    finished_monotonic_ns: int
    execution_outcome: ExecutionOutcome
    output_sha256: str | None = None
    output_length: int | None = None
    output_ref: str | None = field(default=None, repr=False)
    error_code: str | None = None
    cancellation_report: CancellationReport = field(default_factory=CancellationReport)

    def __post_init__(self) -> None:
        _require_bounded_text(
            self.task_id,
            "task_id",
            max_length=MAX_IDENTIFIER_LENGTH,
        )
        if not isinstance(self.task_kind, TaskKind):
            raise TypeError("task_kind must be a TaskKind")
        if not isinstance(self.state_token, StateToken):
            raise TypeError("state_token must be a StateToken")
        source = _require_non_negative_int(
            self.source_monotonic_ns,
            "source_monotonic_ns",
        )
        deadline = _require_non_negative_int(
            self.deadline_monotonic_ns,
            "deadline_monotonic_ns",
        )
        started = _require_non_negative_int(
            self.started_monotonic_ns,
            "started_monotonic_ns",
        )
        finished = _require_non_negative_int(
            self.finished_monotonic_ns,
            "finished_monotonic_ns",
        )
        if source > started:
            raise ValueError("source_monotonic_ns must not be after worker start")
        if started > finished:
            raise ValueError("finished_monotonic_ns must not be before worker start")
        if source > deadline:
            raise ValueError("deadline_monotonic_ns must not be before source")
        _require_sha256(self.input_sha256, "input_sha256")
        if not isinstance(self.execution_outcome, ExecutionOutcome):
            raise TypeError("execution_outcome must be an ExecutionOutcome")

        if (self.output_sha256 is None) != (self.output_length is None):
            raise ValueError("output_sha256 and output_length must be present together")
        if self.output_sha256 is not None:
            _require_sha256(self.output_sha256, "output_sha256")
            _require_non_negative_int(self.output_length, "output_length")
        _require_bounded_text(
            self.output_ref,
            "output_ref",
            max_length=MAX_REFERENCE_LENGTH,
            optional=True,
        )
        if self.error_code is not None:
            if (
                not isinstance(self.error_code, str)
                or len(self.error_code) > MAX_METADATA_KEY_LENGTH
                or not _ERROR_CODE_RE.fullmatch(self.error_code)
            ):
                raise ValueError("error_code must be bounded lower_snake_case")
        if (
            self.execution_outcome
            in {
                ExecutionOutcome.ERROR,
                ExecutionOutcome.TIMEOUT,
            }
            and self.error_code is None
        ):
            raise ValueError("error and timeout outcomes require error_code")
        if not isinstance(self.cancellation_report, CancellationReport):
            raise TypeError("cancellation_report must be a CancellationReport")
        if (
            self.execution_outcome is ExecutionOutcome.CANCEL_OBSERVED
            and not self.cancellation_report.worker_observed
        ):
            raise ValueError(
                "cancel_observed outcome requires worker_observed cancellation"
            )


class CancellationToken:
    """Thread-safe cooperative cancellation signal owned by one task record."""

    __slots__ = ("_event", "_lock", "_reason", "_requested_at_ns")

    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._reason: str | None = None
        self._requested_at_ns: int | None = None

    def request(self, reason: str, requested_at_ns: int) -> bool:
        """Request cancellation once and return whether this call changed it."""

        checked_reason = _require_bounded_text(
            reason,
            "reason",
            max_length=MAX_IDENTIFIER_LENGTH,
        )
        checked_time = _require_non_negative_int(
            requested_at_ns,
            "requested_at_ns",
        )
        assert checked_reason is not None
        with self._lock:
            if self._event.is_set():
                return False
            self._reason = checked_reason
            self._requested_at_ns = checked_time
            self._event.set()
            return True

    def is_requested(self) -> bool:
        return self._event.is_set()

    def wait(self, timeout: float | None = None) -> bool:
        return self._event.wait(timeout)

    @property
    def reason(self) -> str | None:
        with self._lock:
            return self._reason

    @property
    def requested_at_ns(self) -> int | None:
        with self._lock:
            return self._requested_at_ns
