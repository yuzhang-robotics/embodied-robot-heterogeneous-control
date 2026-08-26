"""Bounded-lane policies for the Phase 1 runtime."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .model import TaskKind


MAX_CAPACITY = 100_000


class OverflowPolicy(str, Enum):
    """How a lane handles a submission that cannot be appended directly."""

    REJECT_NEW = "reject_new"
    DROP_OLDEST = "drop_oldest"
    COALESCE_BY_KEY = "coalesce_by_key"


def _positive_capacity(value: object, field_name: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= MAX_CAPACITY
    ):
        raise ValueError(
            f"{field_name} must be an integer between 1 and {MAX_CAPACITY}"
        )
    return value


@dataclass(frozen=True, slots=True)
class LaneConfig:
    """All in-memory capacities required by one single-consumer lane."""

    task_kind: TaskKind
    pending_capacity: int
    result_capacity: int = 1
    terminal_record_capacity: int = 1024
    state_scope_capacity: int = 64
    overflow_policy: OverflowPolicy = OverflowPolicy.REJECT_NEW

    def __post_init__(self) -> None:
        if not isinstance(self.task_kind, TaskKind):
            raise TypeError("task_kind must be a TaskKind")
        _positive_capacity(self.pending_capacity, "pending_capacity")
        _positive_capacity(self.result_capacity, "result_capacity")
        _positive_capacity(
            self.terminal_record_capacity,
            "terminal_record_capacity",
        )
        _positive_capacity(self.state_scope_capacity, "state_scope_capacity")
        if not isinstance(self.overflow_policy, OverflowPolicy):
            raise TypeError("overflow_policy must be an OverflowPolicy")
