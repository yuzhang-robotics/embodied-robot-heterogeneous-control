"""Single-request orchestration for the fixed-input Phase 1 ASR slice."""

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
from experiments.phase1.workloads.asr.adapter import (
    ASRExecutionRecord,
    FixedInputASRAdapter,
)


class ASRAdapter(Protocol):
    """Minimum parent-side contract for one supervised Whisper invocation."""

    inference_started_event: threading.Event

    @property
    def last_record(self) -> ASRExecutionRecord | None: ...

    def __call__(self, claimed: ClaimedTask) -> ResultEnvelope: ...


class ASRSliceCondition(str, Enum):
    """The first two real ASR correctness conditions."""

    ASYNC = "asr_async"
    STALE = "asr_stale"


@dataclass(frozen=True, slots=True)
class ASRSliceSpec:
    """Frozen controls for one fixed-input ASR request."""

    condition: ASRSliceCondition
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
        if not isinstance(self.condition, ASRSliceCondition):
            raise TypeError("condition must be an ASRSliceCondition")
        validate_slice_timing(
            self,
            stale_observation_s=self.stale_observation_s,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "condition": self.condition.value,
            "task_kind": TaskKind.ASR.value,
            "request_count": 1,
            "pending_capacity": 2,
            "result_capacity": 2,
            "overflow_policy": OverflowPolicy.REJECT_NEW.value,
            "queue_semantics": "utterance_fifo",
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
                self.condition is ASRSliceCondition.STALE
            ),
        }


@dataclass(frozen=True, slots=True)
class ASRSliceReport:
    """Closed runtime, probe and adapter facts for one request."""

    condition: ASRSliceCondition
    task_id: str
    state_advanced: bool
    consumed: bool
    final_disposition: str
    adapter: ASRExecutionRecord
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


def make_asr_task(
    payload: PayloadRef,
    *,
    state_token: StateToken,
    task_id: str,
    result_validity_s: float,
    clock_ns: Callable[[], int] = time.monotonic_ns,
) -> TaskEnvelope:
    """Create the only utterance admitted by one Phase 1D correctness run."""

    return make_single_request_task(
        payload,
        state_token=state_token,
        task_id=task_id,
        task_kind=TaskKind.ASR,
        result_validity_s=result_validity_s,
        clock_ns=clock_ns,
        metadata={
            "protocol": "phase1d_asr",
            "fixed_input": True,
            "queue_semantics": "utterance_fifo",
            "raw_output_recorded": False,
        },
    )


def run_asr_slice(
    spec: ASRSliceSpec,
    payload: PayloadRef,
    event_sink: RuntimeEventSink,
    *,
    adapter: ASRAdapter | None = None,
    task_id: str = "asr-001",
) -> ASRSliceReport:
    """Run one nominal or invalidated ASR request and close all processes."""

    if not isinstance(spec, ASRSliceSpec):
        raise TypeError("spec must be an ASRSliceSpec")
    if not isinstance(payload, PayloadRef):
        raise TypeError("payload must be a PayloadRef")
    if not callable(getattr(event_sink, "emit", None)):
        raise TypeError("event_sink must provide emit(event)")
    resolved_adapter = adapter or FixedInputASRAdapter()
    if not callable(resolved_adapter):
        raise TypeError("adapter must be callable")

    outcome = run_single_request_slice(
        workload_label="ASR",
        task_kind=TaskKind.ASR,
        state_scope="asr-slice",
        pending_capacity=2,
        result_capacity=2,
        worker_name="phase1-asr-worker",
        probe_name="phase1-asr-probe",
        is_stale=spec.condition is ASRSliceCondition.STALE,
        timing=spec,
        stale_observation_s=spec.stale_observation_s,
        task_factory=lambda state_token: make_asr_task(
            payload,
            state_token=state_token,
            task_id=task_id,
            result_validity_s=spec.result_validity_s,
        ),
        adapter=resolved_adapter,
        event_sink=event_sink,
    )
    return ASRSliceReport(
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
