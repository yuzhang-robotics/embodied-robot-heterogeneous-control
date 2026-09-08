"""Single-request orchestration for the fixed-input Phase 1 LLM slice."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Protocol

from jetson.phase1_runtime import (
    BrokerSnapshot,
    ClaimedTask,
    ExecutorShutdownReport,
    OverflowPolicy,
    PayloadRef,
    ProbeStopReport,
    ResultEnvelope,
    RuntimeEventSink,
    StateToken,
    TaskEnvelope,
    TaskKind,
)

from experiments.phase1.workloads._slice import (
    SingleRequestOutcome,
    make_single_request_task,
    run_single_request_slice,
    slice_report_dict,
    validate_slice_timing,
)
from experiments.phase1.workloads.llm.adapter import (
    LLM_EMPTY_HISTORY_SHA256,
    FixedInputLLMAdapter,
    LLMExecutionRecord,
)


class LLMAdapter(Protocol):
    """Minimum parent-side contract for one local llama.cpp request."""

    inference_started_event: threading.Event

    @property
    def last_record(self) -> LLMExecutionRecord | None: ...

    def __call__(self, claimed: ClaimedTask) -> ResultEnvelope: ...


class LLMSliceCondition(str, Enum):
    """The first two real LLM correctness conditions."""

    ASYNC = "llm_async"
    STALE = "llm_stale"


@dataclass(frozen=True, slots=True)
class LLMSliceSpec:
    """Frozen controls for one fixed-input LLM request."""

    condition: LLMSliceCondition
    result_validity_s: float = 180.0
    completion_timeout_s: float = 150.0
    join_timeout_s: float = 10.0
    probe_join_timeout_s: float = 5.0
    prelude_s: float = 1.0
    postlude_s: float = 1.0
    stale_observation_s: float = 0.5
    probe_period_ns: int = 100_000_000
    probe_deadline_ns: int = 100_000_000

    def __post_init__(self) -> None:
        if not isinstance(self.condition, LLMSliceCondition):
            raise TypeError("condition must be an LLMSliceCondition")
        validate_slice_timing(
            self,
            stale_observation_s=self.stale_observation_s,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "condition": self.condition.value,
            "task_kind": TaskKind.LLM.value,
            "request_count": 1,
            "pending_capacity": 1,
            "result_capacity": 1,
            "overflow_policy": OverflowPolicy.REJECT_NEW.value,
            "queue_semantics": "conversation_fifo",
            "history_messages": 0,
            "history_sha256": LLM_EMPTY_HISTORY_SHA256,
            "result_validity_s": self.result_validity_s,
            "completion_timeout_s": self.completion_timeout_s,
            "join_timeout_s": self.join_timeout_s,
            "probe_join_timeout_s": self.probe_join_timeout_s,
            "prelude_s": self.prelude_s,
            "postlude_s": self.postlude_s,
            "stale_observation_s": self.stale_observation_s,
            "probe_period_ns": self.probe_period_ns,
            "probe_deadline_ns": self.probe_deadline_ns,
            "state_advance_after_inference_start": (
                self.condition is LLMSliceCondition.STALE
            ),
        }


@dataclass(frozen=True, slots=True)
class LLMSliceReport:
    """Closed runtime, probe and adapter facts for one request."""

    condition: LLMSliceCondition
    task_id: str
    state_advanced: bool
    consumed: bool
    final_disposition: str
    adapter: LLMExecutionRecord
    shutdown: ExecutorShutdownReport
    probe: ProbeStopReport
    final_snapshot: BrokerSnapshot

    def to_dict(self) -> dict[str, object]:
        outcome = SingleRequestOutcome(
            task_id=self.task_id,
            state_advanced=self.state_advanced,
            consumed=self.consumed,
            final_disposition=self.final_disposition,
            adapter_record=self.adapter,
            shutdown=self.shutdown,
            probe=self.probe,
            final_snapshot=self.final_snapshot,
        )
        return slice_report_dict(
            condition=self.condition.value,
            outcome=outcome,
            adapter=self.adapter.to_dict(),
        )


def make_llm_task(
    payload: PayloadRef,
    *,
    state_token: StateToken,
    task_id: str,
    result_validity_s: float,
    clock_ns: Callable[[], int] = time.monotonic_ns,
) -> TaskEnvelope:
    """Create the only empty-history dialogue request admitted by one run."""

    return make_single_request_task(
        payload,
        state_token=state_token,
        task_id=task_id,
        task_kind=TaskKind.LLM,
        result_validity_s=result_validity_s,
        clock_ns=clock_ns,
        metadata={
            "protocol": "phase1d_llm",
            "fixed_input": True,
            "queue_semantics": "conversation_fifo",
            "history_messages": 0,
            "history_sha256": LLM_EMPTY_HISTORY_SHA256,
            "raw_prompt_recorded": False,
            "raw_output_recorded": False,
        },
    )


def run_llm_slice(
    spec: LLMSliceSpec,
    payload: PayloadRef,
    event_sink: RuntimeEventSink,
    *,
    adapter: LLMAdapter | None = None,
    task_id: str = "llm-001",
) -> LLMSliceReport:
    """Run one nominal or invalidated LLM request and close all local threads."""

    if not isinstance(spec, LLMSliceSpec):
        raise TypeError("spec must be an LLMSliceSpec")
    if not isinstance(payload, PayloadRef):
        raise TypeError("payload must be a PayloadRef")
    if not callable(getattr(event_sink, "emit", None)):
        raise TypeError("event_sink must provide emit(event)")
    resolved_adapter = adapter or FixedInputLLMAdapter()
    if not callable(resolved_adapter):
        raise TypeError("adapter must be callable")

    outcome = run_single_request_slice(
        workload_label="LLM",
        task_kind=TaskKind.LLM,
        state_scope="llm-slice",
        pending_capacity=1,
        result_capacity=1,
        worker_name="phase1-llm-worker",
        probe_name="phase1-llm-probe",
        is_stale=spec.condition is LLMSliceCondition.STALE,
        timing=spec,
        stale_observation_s=spec.stale_observation_s,
        task_factory=lambda state_token: make_llm_task(
            payload,
            state_token=state_token,
            task_id=task_id,
            result_validity_s=spec.result_validity_s,
        ),
        adapter=resolved_adapter,
        event_sink=event_sink,
    )
    return LLMSliceReport(
        condition=spec.condition,
        task_id=outcome.task_id,
        state_advanced=outcome.state_advanced,
        consumed=outcome.consumed,
        final_disposition=outcome.final_disposition,
        adapter=outcome.adapter_record,
        shutdown=outcome.shutdown,
        probe=outcome.probe,
        final_snapshot=outcome.final_snapshot,
    )
