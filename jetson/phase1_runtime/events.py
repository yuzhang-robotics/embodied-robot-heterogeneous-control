"""Bounded observations emitted by the Phase 1 runtime."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Mapping, Protocol, TypeAlias

from .model import StateToken, TaskKind


MAX_EVENT_DETAILS = 32
MAX_EVENT_DETAIL_BYTES = 4096
MAX_EVENT_TEXT_LENGTH = 256

_EVENT_RE = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$")
_COMPONENT_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_DETAIL_KEY_RE = re.compile(r"^[a-z][a-z0-9_]*$")

EventScalar: TypeAlias = str | int | float | bool | None


class EventStatus(str, Enum):
    """Portable statuses used by runtime and probe observations."""

    STARTED = "started"
    OK = "ok"
    ERROR = "error"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    DROPPED = "dropped"
    STALE = "stale"
    REJECTED = "rejected"
    INFO = "info"


def _freeze_details(details: Mapping[str, EventScalar]) -> Mapping[str, EventScalar]:
    if not isinstance(details, Mapping):
        raise TypeError("event details must be a mapping")
    if len(details) > MAX_EVENT_DETAILS:
        raise ValueError(f"event details must contain at most {MAX_EVENT_DETAILS} keys")

    copied: dict[str, EventScalar] = {}
    for key, value in details.items():
        if not isinstance(key, str) or not _DETAIL_KEY_RE.fullmatch(key):
            raise ValueError("event detail keys must use lower_snake_case")
        if not isinstance(value, (str, int, float, bool, type(None))):
            raise TypeError("event detail values must be JSON scalar values")
        if isinstance(value, str) and not 1 <= len(value) <= MAX_EVENT_TEXT_LENGTH:
            raise ValueError(
                "event detail strings must contain 1 to "
                f"{MAX_EVENT_TEXT_LENGTH} characters"
            )
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("event detail floats must be finite")
        copied[key] = value

    encoded = json.dumps(
        copied,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > MAX_EVENT_DETAIL_BYTES:
        raise ValueError(
            f"serialized event details must not exceed {MAX_EVENT_DETAIL_BYTES} bytes"
        )
    return MappingProxyType(copied)


@dataclass(frozen=True, slots=True)
class RuntimeEvent:
    """One hardware-independent observation awaiting recorder timestamps."""

    event: str
    component: str
    status: EventStatus
    task_id: str | None = None
    task_kind: TaskKind | None = None
    parent_task_id: str | None = None
    source_monotonic_ns: int | None = None
    deadline_monotonic_ns: int | None = None
    state_token: StateToken | None = None
    details: Mapping[str, EventScalar] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.event, str) or not _EVENT_RE.fullmatch(self.event):
            raise ValueError("event must contain at least two dotted name segments")
        if not isinstance(self.component, str) or not _COMPONENT_RE.fullmatch(
            self.component
        ):
            raise ValueError("component must use lower_snake_case")
        if not isinstance(self.status, EventStatus):
            raise TypeError("status must be an EventStatus")
        for field_name in ("task_id", "parent_task_id"):
            value = getattr(self, field_name)
            if value is not None and (
                not isinstance(value, str)
                or not 1 <= len(value) <= 128
                or value != value.strip()
                or not value.isprintable()
            ):
                raise ValueError(f"{field_name} must be a bounded printable string")
        if self.task_kind is not None and not isinstance(self.task_kind, TaskKind):
            raise TypeError("task_kind must be a TaskKind or None")
        for field_name in ("source_monotonic_ns", "deadline_monotonic_ns"):
            value = getattr(self, field_name)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise ValueError(f"{field_name} must be a non-negative integer")
        if self.state_token is not None and not isinstance(
            self.state_token, StateToken
        ):
            raise TypeError("state_token must be a StateToken or None")
        object.__setattr__(self, "details", _freeze_details(self.details))


class RuntimeEventSink(Protocol):
    """Synchronous sink used to preserve operation and trace ordering."""

    def emit(self, event: RuntimeEvent) -> None:
        """Record one immutable event or raise on recording failure."""


class NullEventSink:
    """Default sink for callers that do not need a trace."""

    def emit(self, event: RuntimeEvent) -> None:
        if not isinstance(event, RuntimeEvent):
            raise TypeError("event must be a RuntimeEvent")
