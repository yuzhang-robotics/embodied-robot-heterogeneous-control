"""Independent replay of Phase 1 JSONL lifecycle traces."""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Iterable, Mapping


SCHEMA_VERSION = "0.2.0"
_LIVE_LOCATIONS = {"queued", "running", "result_pending"}
_TERMINAL = "terminal"
_DISPOSITIONS = {
    "consumed",
    "dropped_overflow",
    "rejected_busy",
    "cancelled_queued",
    "rejected_cancelled",
    "rejected_expired",
    "rejected_state",
    "rejected_identity",
    "execution_error",
    "result_backpressure",
    "shutdown_cancelled",
}
_EVENT_RE = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$")
_RUN_ID_RE = re.compile(
    r"^[0-9]{8}T[0-9]{6}Z_phase1_[a-z][a-z0-9_]{0,31}_"
    r"(?:simulated|vlm|asr|llm)_[0-9]{3}$"
)
_DETAIL_KEY_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_COMPONENTS = {"executor", "worker", "probe"}
_TASK_KINDS = {"simulated", "vlm", "asr", "llm"}
_OVERFLOW_POLICIES = {"reject_new", "drop_oldest", "coalesce_by_key"}
_STATUSES = {
    "started",
    "ok",
    "error",
    "timeout",
    "cancelled",
    "dropped",
    "stale",
    "rejected",
    "info",
}
_OPTIONAL_FIELDS = {
    "task_id",
    "task_kind",
    "parent_task_id",
    "source_monotonic_ns",
    "deadline_monotonic_ns",
    "state_scope_id",
    "state_generation",
}


class ReplayError(ValueError):
    """A trace cannot represent a legal bounded runtime history."""


class TraceProfile(str, Enum):
    """Explicit completion contract for one Phase 1 trace."""

    RUNTIME = "runtime"
    RUNTIME_THREADED_PROBE = "runtime_threaded_probe"
    THREADED_PROBE = "threaded_probe"
    INLINE_PROBE = "inline_probe"


@dataclass(slots=True)
class _ReplayTask:
    admitted: bool
    location: str
    task_kind: str
    source_ns: int
    created_ns: int
    deadline_ns: int
    scope_id: str
    generation: int
    payload_sha256: str
    started_ns: int | None = None
    cancellation_requested: bool = False
    disposition: str | None = None


@dataclass(frozen=True, slots=True)
class ReplaySummary:
    run_id: str
    trace_profile: str
    event_count: int
    submission_attempts: int
    admitted_total: int
    terminal_admitted_total: int
    accepted_result_count: int
    stale_consumed_count: int
    max_pending_depth: int
    max_result_depth: int
    probe_tick_count: int
    probe_skipped_releases: int
    probe_deadline_miss_count: int
    disposition_counts: tuple[tuple[str, int], ...]
    worker_joined: bool
    probe_stopped: bool
    probe_joined: bool
    final_broker_state: str | None


def load_events(path: Path | str) -> list[dict[str, object]]:
    """Load a non-empty JSONL trace without importing runtime code."""

    events: list[dict[str, object]] = []
    with Path(path).open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            if not line.endswith("\n"):
                raise ReplayError(f"line {line_number} is not newline terminated")
            if not line.strip():
                raise ReplayError(f"line {line_number} is blank")
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ReplayError(f"line {line_number} is not valid JSON") from exc
            if not isinstance(item, dict):
                raise ReplayError(f"line {line_number} must contain an object")
            events.append(item)
    if not events:
        raise ReplayError("trace must contain at least one event")
    return events


def _require_int(
    mapping: Mapping[str, object],
    key: str,
    *,
    minimum: int = 0,
) -> int:
    value = mapping.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ReplayError(f"{key} must be an integer of at least {minimum}")
    return value


def _require_str(mapping: Mapping[str, object], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise ReplayError(f"{key} must be a non-empty string")
    return value


class _LifecycleReplay:
    def __init__(self) -> None:
        self.run_id: str | None = None
        self.last_monotonic_ns = 0
        self.tasks: dict[str, _ReplayTask] = {}
        self.state_generations: dict[str, int] = {}
        self.dispositions: Counter[str] = Counter()
        self.submission_attempts = 0
        self.admitted_total = 0
        self.accepted_results = 0
        self.max_pending = 0
        self.max_result = 0
        self.probe_ticks = 0
        self.probe_misses = 0
        self.last_probe_index: int | None = None
        self.shutdown_seen = False
        self.worker_stopped = False
        self.worker_joined = False
        self.final_broker_state: str | None = None
        self.broker_state = "open"
        self.worker_error = False
        self.probe_started = False
        self.probe_stopped = False
        self.probe_joined = False
        self.probe_join_event_seen = False
        self.probe_error = False
        self.probe_period_ns: int | None = None
        self.probe_deadline_ns: int | None = None
        self.probe_previous_started_ns: int | None = None
        self.probe_skipped_releases = 0
        self.runtime_event_seen = False

    def _depths(self) -> tuple[int, int, int]:
        pending = sum(task.location == "queued" for task in self.tasks.values())
        active = sum(task.location == "running" for task in self.tasks.values())
        result = sum(task.location == "result_pending" for task in self.tasks.values())
        return pending, active, result

    def _check_depths(self, details: Mapping[str, object]) -> None:
        if "pending_depth" not in details:
            return
        observed = (
            _require_int(details, "pending_depth"),
            _require_int(details, "active_count"),
            _require_int(details, "result_depth"),
        )
        expected = self._depths()
        if observed != expected:
            raise ReplayError(
                f"recorded lifecycle depths {observed} do not match replay {expected}"
            )
        pending_capacity = _require_int(details, "pending_capacity", minimum=1)
        result_capacity = _require_int(details, "result_capacity", minimum=1)
        if _require_str(details, "overflow_policy") not in _OVERFLOW_POLICIES:
            raise ReplayError("overflow policy is not supported")
        if observed[0] > pending_capacity:
            raise ReplayError("pending queue capacity was exceeded")
        if observed[1] > 1:
            raise ReplayError("single-worker active capacity was exceeded")
        if observed[2] > result_capacity:
            raise ReplayError("result mailbox capacity was exceeded")
        self.max_pending = max(self.max_pending, observed[0])
        self.max_result = max(self.max_result, observed[2])

    def _identity_from_event(
        self,
        event: Mapping[str, object],
        details: Mapping[str, object],
    ) -> _ReplayTask:
        task_kind = _require_str(event, "task_kind")
        source = _require_int(event, "source_monotonic_ns")
        created = _require_int(details, "created_monotonic_ns")
        deadline = _require_int(event, "deadline_monotonic_ns")
        scope_id = _require_str(event, "state_scope_id")
        generation = _require_int(event, "state_generation")
        payload_sha256 = _require_str(details, "payload_sha256")
        if not source <= created <= deadline:
            raise ReplayError("task source, creation, and deadline order is invalid")
        if not _SHA256_RE.fullmatch(payload_sha256):
            raise ReplayError("payload_sha256 must be lowercase SHA-256 hex")
        return _ReplayTask(
            admitted=event["event"] == "task.enqueued",
            location="queued" if event["event"] == "task.enqueued" else _TERMINAL,
            task_kind=task_kind,
            source_ns=source,
            created_ns=created,
            deadline_ns=deadline,
            scope_id=scope_id,
            generation=generation,
            payload_sha256=payload_sha256,
        )

    def _get_task(self, event: Mapping[str, object]) -> tuple[str, _ReplayTask]:
        task_id = _require_str(event, "task_id")
        task = self.tasks.get(task_id)
        if task is None:
            raise ReplayError(f"event references unknown task: {task_id}")
        return task_id, task

    def _transition(
        self,
        task: _ReplayTask,
        details: Mapping[str, object],
        *,
        expected_previous: str | None = None,
    ) -> str:
        previous = _require_str(details, "previous_location")
        next_location = _require_str(details, "next_location")
        if previous != task.location:
            raise ReplayError(
                f"transition starts at {previous}, but task is at {task.location}"
            )
        if expected_previous is not None and previous != expected_previous:
            raise ReplayError(f"transition must start at {expected_previous}")
        if next_location not in _LIVE_LOCATIONS | {_TERMINAL}:
            raise ReplayError(f"unknown next lifecycle location: {next_location}")
        task.location = next_location
        return next_location

    @staticmethod
    def _result_identity_matches(
        task: _ReplayTask,
        details: Mapping[str, object],
    ) -> bool:
        return all(
            (
                details.get("result_input_sha256") == task.payload_sha256,
                details.get("result_task_kind") == task.task_kind,
                details.get("result_source_monotonic_ns") == task.source_ns,
                details.get("result_deadline_monotonic_ns") == task.deadline_ns,
                details.get("result_state_scope_id") == task.scope_id,
                details.get("result_state_generation") == task.generation,
            )
        )

    def apply(self, event: Mapping[str, object], expected_seq: int) -> None:
        required = {
            "schema_version",
            "run_id",
            "seq",
            "event",
            "component",
            "status",
            "monotonic_ns",
            "wall_time_ns",
            "pid",
            "thread_id",
            "details",
        }
        missing = sorted(required - event.keys())
        if missing:
            raise ReplayError(f"event is missing fields: {', '.join(missing)}")
        unknown = sorted(event.keys() - required - _OPTIONAL_FIELDS)
        if unknown:
            raise ReplayError(f"event has unknown fields: {', '.join(unknown)}")
        if event.get("schema_version") != SCHEMA_VERSION:
            raise ReplayError("trace schema version is not supported")
        run_id = _require_str(event, "run_id")
        if not _RUN_ID_RE.fullmatch(run_id):
            raise ReplayError("trace run ID does not match the Phase 1 format")
        if self.run_id is None:
            self.run_id = run_id
        elif run_id != self.run_id:
            raise ReplayError("trace mixes run IDs")
        if _require_int(event, "seq") != expected_seq:
            raise ReplayError("event sequence must be contiguous from zero")
        monotonic_ns = _require_int(event, "monotonic_ns")
        if monotonic_ns < self.last_monotonic_ns:
            raise ReplayError("primary event timestamps moved backwards")
        self.last_monotonic_ns = monotonic_ns
        event_name = _require_str(event, "event")
        if not _EVENT_RE.fullmatch(event_name):
            raise ReplayError("event name is not a dotted lower_snake_case name")
        if _require_str(event, "component") not in _COMPONENTS:
            raise ReplayError("event component is not supported")
        if _require_str(event, "status") not in _STATUSES:
            raise ReplayError("event status is not supported")
        _require_int(event, "wall_time_ns")
        _require_int(event, "pid", minimum=1)
        thread_id = event.get("thread_id")
        if isinstance(thread_id, bool) or not isinstance(thread_id, (int, str)):
            raise ReplayError("thread_id must be an integer or string")
        details_value = event.get("details")
        if not isinstance(details_value, dict):
            raise ReplayError("details must be an object")
        details: Mapping[str, object] = details_value
        if len(details) > 32:
            raise ReplayError("details contains more than 32 fields")
        for key, value in details.items():
            if not isinstance(key, str) or not _DETAIL_KEY_RE.fullmatch(key):
                raise ReplayError("detail keys must use lower_snake_case")
            if not isinstance(value, (str, int, float, bool, type(None))):
                raise ReplayError("detail values must be JSON scalars")
            if isinstance(value, str) and not 1 <= len(value) <= 256:
                raise ReplayError("detail strings must be bounded and non-empty")
            if isinstance(value, float) and not math.isfinite(value):
                raise ReplayError("detail floats must be finite")
        encoded_details = json.dumps(
            details,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded_details) > 4096:
            raise ReplayError("serialized details exceeds 4096 bytes")
        for key in ("task_id", "parent_task_id", "state_scope_id"):
            if key not in event:
                continue
            value = event[key]
            if (
                not isinstance(value, str)
                or not 1 <= len(value) <= 128
                or value != value.strip()
                or not value.isprintable()
            ):
                raise ReplayError(f"{key} must be a bounded printable string")
        if "task_kind" in event and event["task_kind"] not in _TASK_KINDS:
            raise ReplayError("task_kind is not supported")
        for key in (
            "source_monotonic_ns",
            "deadline_monotonic_ns",
            "state_generation",
        ):
            if key in event:
                _require_int(event, key)
        if ("state_scope_id" in event) != ("state_generation" in event):
            raise ReplayError("state scope and generation must be recorded together")

        if event_name.startswith(
            ("task.", "result.", "state.", "shutdown.", "worker.")
        ):
            self.runtime_event_seen = True

        if event_name in {"task.enqueued", "task.rejected"}:
            task_id = _require_str(event, "task_id")
            if task_id in self.tasks:
                raise ReplayError(f"duplicate task submission: {task_id}")
            task = self._identity_from_event(event, details)
            if task.created_ns > monotonic_ns:
                raise ReplayError("task creation is later than its trace event")
            if task.admitted and self.broker_state != "open":
                raise ReplayError("task was admitted after shutdown began")
            current_generation = self.state_generations.get(task.scope_id, 0)
            if task.admitted and task.generation != current_generation:
                raise ReplayError("admitted task used a non-current state generation")
            if not task.admitted:
                disposition = _require_str(details, "disposition")
                if disposition not in _DISPOSITIONS:
                    raise ReplayError("rejected task has an unknown disposition")
                if disposition == "consumed":
                    raise ReplayError("rejected task cannot use consumed disposition")
                task.disposition = disposition
                self.dispositions[disposition] += 1
            self.tasks[task_id] = task
            self.submission_attempts += 1
            self.admitted_total += int(task.admitted)

        elif event_name == "task.started":
            _, task = self._get_task(event)
            if self._depths()[1] != 0:
                raise ReplayError("a task started while another task was active")
            self._transition(task, details, expected_previous="queued")
            started_ns = _require_int(details, "started_monotonic_ns")
            if started_ns < task.created_ns:
                raise ReplayError("worker start precedes task creation")
            if started_ns > monotonic_ns:
                raise ReplayError("worker start is later than its trace event")
            task.started_ns = started_ns

        elif event_name == "task.finished":
            _, task = self._get_task(event)
            next_location = self._transition(task, details, expected_previous="running")
            started_ns = _require_int(details, "started_monotonic_ns")
            finished_ns = _require_int(details, "finished_monotonic_ns")
            if finished_ns < started_ns:
                raise ReplayError("result finished before worker start")
            if task.started_ns != started_ns:
                raise ReplayError("result worker start does not match the claim")
            if finished_ns > monotonic_ns:
                raise ReplayError("result finish is later than its trace event")
            identity_matches = self._result_identity_matches(task, details)
            if next_location == "result_pending":
                if not identity_matches:
                    raise ReplayError("identity-mismatched result entered the mailbox")
                if details.get("execution_outcome") != "ok":
                    raise ReplayError("non-success result entered the mailbox")
                if finished_ns > task.deadline_ns:
                    raise ReplayError("expired result entered the mailbox")
            elif next_location == _TERMINAL:
                disposition = _require_str(details, "disposition")
                if disposition not in _DISPOSITIONS:
                    raise ReplayError("finished task has an unknown disposition")
                if disposition == "consumed":
                    raise ReplayError("worker completion cannot consume a result")
                transition_ns = _require_int(details, "transition_monotonic_ns")
                if transition_ns > monotonic_ns:
                    raise ReplayError("task transition is later than its trace event")
                if not identity_matches and disposition != "rejected_identity":
                    raise ReplayError("result identity mismatch was misclassified")
                task.disposition = disposition
                self.dispositions[disposition] += 1
            else:
                raise ReplayError("finished task must enter result_pending or terminal")

        elif event_name in {"result.accepted", "result.rejected"}:
            _, task = self._get_task(event)
            self._transition(task, details, expected_previous="result_pending")
            if task.location != _TERMINAL:
                raise ReplayError("result decision must be terminal")
            disposition = _require_str(details, "disposition")
            if disposition not in _DISPOSITIONS:
                raise ReplayError("result decision has an unknown disposition")
            if not self._result_identity_matches(task, details):
                raise ReplayError("result decision contains mismatched identity")
            task.disposition = disposition
            self.dispositions[disposition] += 1
            if event_name == "result.accepted":
                if disposition != "consumed":
                    raise ReplayError("accepted result must use consumed disposition")
                transition_ns = _require_int(details, "transition_monotonic_ns")
                if transition_ns > monotonic_ns:
                    raise ReplayError("result transition is later than its trace event")
                current_generation = self.state_generations.get(task.scope_id, 0)
                if (
                    task.cancellation_requested
                    or task.generation != current_generation
                    or transition_ns > task.deadline_ns
                ):
                    raise ReplayError("a stale or cancelled result was consumed")
                self.accepted_results += 1
            elif disposition == "consumed":
                raise ReplayError("rejected result cannot use consumed disposition")

        elif event_name == "task.terminal":
            _, task = self._get_task(event)
            self._transition(task, details)
            if task.location != _TERMINAL:
                raise ReplayError("task.terminal must enter terminal state")
            disposition = _require_str(details, "disposition")
            if disposition not in _DISPOSITIONS:
                raise ReplayError("terminal event has an unknown disposition")
            if disposition == "consumed":
                raise ReplayError("task.terminal cannot consume a result")
            if task.disposition is not None:
                raise ReplayError("task received duplicate final dispositions")
            transition_ns = _require_int(details, "transition_monotonic_ns")
            if transition_ns > monotonic_ns:
                raise ReplayError("task transition is later than its trace event")
            task.disposition = disposition
            self.dispositions[disposition] += 1

        elif event_name == "task.cancel_requested":
            _, task = self._get_task(event)
            if details.get("found") is not True:
                raise ReplayError("cancel_requested event must refer to a found task")
            if details.get("request_changed") is True:
                task.cancellation_requested = True

        elif event_name == "task.cancel_missing":
            if details.get("found") is not False:
                raise ReplayError("cancel_missing event must report found=false")

        elif event_name == "state.advanced":
            scope_id = _require_str(details, "state_scope_id")
            previous = _require_int(details, "previous_generation")
            generation = _require_int(details, "state_generation")
            current = self.state_generations.get(scope_id, 0)
            if previous != current or generation != previous + 1:
                raise ReplayError("state generation did not advance by exactly one")
            self.state_generations[scope_id] = generation
            if details.get("active_cancellation_requested") is True:
                for task in self.tasks.values():
                    if task.location == "running" and task.scope_id == scope_id:
                        task.cancellation_requested = True

        elif event_name == "shutdown.requested":
            self.shutdown_seen = True
            self.final_broker_state = _require_str(details, "broker_state")
            self.broker_state = self.final_broker_state
            if details.get("cancel_live") is True:
                for task in self.tasks.values():
                    if task.location in _LIVE_LOCATIONS:
                        task.cancellation_requested = True

        elif event_name == "worker.stopped":
            self.worker_stopped = True
            if event.get("status") == "error":
                self.worker_error = True

        elif event_name == "worker.failed":
            self.worker_error = True

        elif event_name == "worker.joined":
            if details.get("joined") is True:
                self.worker_joined = True
            self.final_broker_state = _require_str(details, "broker_state")
            self.broker_state = self.final_broker_state
            if (
                details.get("worker_error_code") is not None
                or details.get("event_error_code") is not None
            ):
                self.worker_error = True

        elif event_name == "probe.started":
            if self.probe_started:
                raise ReplayError("probe.started appeared more than once")
            _require_int(details, "origin_monotonic_ns")
            self.probe_period_ns = _require_int(details, "period_ns", minimum=1)
            self.probe_deadline_ns = _require_int(details, "deadline_ns", minimum=1)
            self.probe_started = True

        elif event_name == "probe.skipped":
            if not self.probe_started:
                raise ReplayError("probe.skipped appeared before probe.started")
            from_index = _require_int(details, "from_index")
            to_index = _require_int(details, "to_index")
            skipped = _require_int(details, "skipped_releases", minimum=1)
            if to_index - from_index != skipped:
                raise ReplayError("probe skipped-release count is inconsistent")
            self.probe_skipped_releases += skipped

        elif event_name == "probe.stopped":
            if not self.probe_started:
                raise ReplayError("probe.stopped appeared before probe.started")
            if self.probe_stopped:
                raise ReplayError("probe.stopped appeared more than once")
            if _require_int(details, "tick_count") != self.probe_ticks:
                raise ReplayError("probe stopped with an inconsistent tick count")
            if _require_int(details, "skipped_releases") != self.probe_skipped_releases:
                raise ReplayError("probe stopped with an inconsistent skip count")
            if _require_int(details, "deadline_miss_count") != self.probe_misses:
                raise ReplayError("probe stopped with an inconsistent miss count")
            if details.get("error_code") is not None:
                self.probe_error = True
            self.probe_stopped = True

        elif event_name == "probe.joined":
            if not self.probe_stopped:
                raise ReplayError("probe.joined appeared before probe.stopped")
            if self.probe_join_event_seen:
                raise ReplayError("probe.joined appeared more than once")
            self.probe_join_event_seen = True
            if details.get("joined") is True:
                self.probe_joined = True
            if details.get("error_code") is not None:
                self.probe_error = True
            if _require_int(details, "tick_count") != self.probe_ticks:
                raise ReplayError("probe joined with an inconsistent tick count")
            if _require_int(details, "skipped_releases") != self.probe_skipped_releases:
                raise ReplayError("probe joined with an inconsistent skip count")
            if _require_int(details, "deadline_miss_count") != self.probe_misses:
                raise ReplayError("probe joined with an inconsistent miss count")

        elif event_name == "probe.failed":
            self.probe_error = True

        elif event_name == "probe.tick":
            if not self.probe_started:
                raise ReplayError("probe tick appeared before probe.started")
            assert self.probe_period_ns is not None
            assert self.probe_deadline_ns is not None
            index = _require_int(details, "tick_index")
            scheduled = _require_int(details, "scheduled_monotonic_ns")
            started = _require_int(details, "started_monotonic_ns")
            finished = _require_int(details, "finished_monotonic_ns")
            if not scheduled <= started <= finished:
                raise ReplayError("probe tick timestamps are not ordered")
            if details.get("start_lateness_ns") != started - scheduled:
                raise ReplayError("probe start lateness is inconsistent")
            if details.get("execution_ns") != finished - started:
                raise ReplayError("probe execution duration is inconsistent")
            actual_period = (
                None
                if self.probe_previous_started_ns is None
                else started - self.probe_previous_started_ns
            )
            if details.get("actual_period_ns") != actual_period:
                raise ReplayError("probe actual period is inconsistent")
            signed_error = (
                None if actual_period is None else actual_period - self.probe_period_ns
            )
            if details.get("signed_period_error_ns") != signed_error:
                raise ReplayError("probe signed period error is inconsistent")
            absolute_error = None if signed_error is None else abs(signed_error)
            if details.get("absolute_period_error_ns") != absolute_error:
                raise ReplayError("probe absolute period error is inconsistent")
            if self.last_probe_index is not None and index <= self.last_probe_index:
                raise ReplayError("probe tick indices must increase")
            self.last_probe_index = index
            deadline_miss = details.get("deadline_miss")
            if not isinstance(deadline_miss, bool):
                raise ReplayError("probe deadline_miss must be a boolean")
            expected_miss = finished > scheduled + self.probe_deadline_ns
            if deadline_miss != expected_miss:
                raise ReplayError("probe deadline miss is inconsistent")
            self.probe_ticks += 1
            self.probe_misses += int(deadline_miss)
            self.probe_previous_started_ns = started

        known_non_lifecycle = {"worker.started"}
        if (
            event_name
            not in {
                "task.enqueued",
                "task.rejected",
                "task.started",
                "task.finished",
                "result.accepted",
                "result.rejected",
                "task.terminal",
                "task.cancel_requested",
                "task.cancel_missing",
                "state.advanced",
                "shutdown.requested",
                "worker.failed",
                "worker.stopped",
                "worker.joined",
                "probe.started",
                "probe.skipped",
                "probe.stopped",
                "probe.failed",
                "probe.joined",
                "probe.tick",
            }
            | known_non_lifecycle
        ):
            raise ReplayError(f"unknown Phase 1 event: {event_name}")

        self._check_depths(details)

    def finish(
        self,
        event_count: int,
        *,
        require_complete: bool,
        profile: TraceProfile,
    ) -> ReplaySummary:
        terminal_admitted = sum(
            task.admitted and task.location == _TERMINAL for task in self.tasks.values()
        )
        terminal_tasks = [
            task for task in self.tasks.values() if task.location == _TERMINAL
        ]
        if any(task.disposition is None for task in terminal_tasks):
            raise ReplayError("terminal task is missing a final disposition")
        if sum(self.dispositions.values()) != len(terminal_tasks):
            raise ReplayError("final disposition accounting does not close")
        runtime_profile = profile in {
            TraceProfile.RUNTIME,
            TraceProfile.RUNTIME_THREADED_PROBE,
        }
        if runtime_profile and not self.runtime_event_seen:
            raise ReplayError("runtime trace profile contains no runtime events")
        if not runtime_profile and self.runtime_event_seen:
            raise ReplayError("probe-only trace contains runtime lifecycle events")

        if require_complete and runtime_profile:
            if not self.shutdown_seen:
                raise ReplayError("complete trace is missing shutdown.requested")
            if not self.worker_stopped or not self.worker_joined:
                raise ReplayError("complete trace is missing a successful worker join")
            live = [
                task_id
                for task_id, task in self.tasks.items()
                if task.admitted and task.location != _TERMINAL
            ]
            if live:
                raise ReplayError(f"complete trace retains live tasks: {live}")
            if self.final_broker_state != "closed":
                raise ReplayError("complete trace did not close the broker")
            if self.worker_error:
                raise ReplayError("complete trace contains a runtime worker error")
        if require_complete:
            probe_required = profile in {
                TraceProfile.RUNTIME_THREADED_PROBE,
                TraceProfile.THREADED_PROBE,
                TraceProfile.INLINE_PROBE,
            }
            if probe_required and not self.probe_started:
                raise ReplayError("trace profile requires a periodic probe")
            if self.probe_started and not self.probe_stopped:
                raise ReplayError("complete trace is missing probe.stopped")
            threaded_probe_expected = profile in {
                TraceProfile.RUNTIME_THREADED_PROBE,
                TraceProfile.THREADED_PROBE,
            } or (profile is TraceProfile.RUNTIME and self.probe_started)
            if threaded_probe_expected and not self.probe_joined:
                raise ReplayError("complete trace is missing a successful probe join")
            if profile is TraceProfile.INLINE_PROBE and self.probe_join_event_seen:
                raise ReplayError("inline probe trace must not contain probe.joined")
            if self.probe_error:
                raise ReplayError("complete trace contains a periodic probe error")
        assert self.run_id is not None
        return ReplaySummary(
            run_id=self.run_id,
            trace_profile=profile.value,
            event_count=event_count,
            submission_attempts=self.submission_attempts,
            admitted_total=self.admitted_total,
            terminal_admitted_total=terminal_admitted,
            accepted_result_count=self.accepted_results,
            stale_consumed_count=0,
            max_pending_depth=self.max_pending,
            max_result_depth=self.max_result,
            probe_tick_count=self.probe_ticks,
            probe_skipped_releases=self.probe_skipped_releases,
            probe_deadline_miss_count=self.probe_misses,
            disposition_counts=tuple(sorted(self.dispositions.items())),
            worker_joined=self.worker_joined,
            probe_stopped=self.probe_stopped,
            probe_joined=self.probe_joined,
            final_broker_state=self.final_broker_state,
        )


def replay_events(
    events: Iterable[Mapping[str, object]],
    *,
    require_complete: bool = True,
    profile: TraceProfile = TraceProfile.RUNTIME,
) -> ReplaySummary:
    """Reconstruct one trace using only serialized public event fields."""

    if not isinstance(profile, TraceProfile):
        raise TypeError("profile must be a TraceProfile")
    replay = _LifecycleReplay()
    count = 0
    for count, event in enumerate(events, start=1):
        if not isinstance(event, Mapping):
            raise ReplayError("each trace item must be a mapping")
        replay.apply(event, count - 1)
    if count == 0:
        raise ReplayError("trace must contain at least one event")
    return replay.finish(
        count,
        require_complete=require_complete,
        profile=profile,
    )


def replay_file(
    path: Path | str,
    *,
    require_complete: bool = True,
    profile: TraceProfile = TraceProfile.RUNTIME,
) -> ReplaySummary:
    return replay_events(
        load_events(path),
        require_complete=require_complete,
        profile=profile,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Replay and validate one Phase 1 lifecycle trace."
    )
    parser.add_argument("events", type=Path, help="path to events.jsonl")
    parser.add_argument(
        "--allow-incomplete",
        action="store_true",
        help="validate the recorded prefix without requiring closed shutdown",
    )
    parser.add_argument(
        "--profile",
        choices=[profile.value for profile in TraceProfile],
        default=TraceProfile.RUNTIME.value,
        help="explicit lifecycle contract for the recorded condition",
    )
    args = parser.parse_args(argv)
    try:
        summary = replay_file(
            args.events,
            require_complete=not args.allow_incomplete,
            profile=TraceProfile(args.profile),
        )
    except (OSError, ReplayError) as exc:
        print(f"INVALID: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(asdict(summary), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
