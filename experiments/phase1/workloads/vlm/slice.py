"""Single-request orchestration for the fixed-input Phase 1 VLM slice."""

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
from experiments.phase1.workloads.vlm.adapter import (
    FixedInputVLMAdapter,
    VLMExecutionRecord,
)


class VLMAdapter(Protocol):
    """Minimum parent-side contract shared by thread and process adapters."""

    inference_started_event: threading.Event

    @property
    def last_record(self) -> VLMExecutionRecord | None: ...

    def __call__(self, claimed: ClaimedTask) -> ResultEnvelope: ...


class VLMSliceCondition(str, Enum):
    """The first two real-workload correctness conditions."""

    ASYNC = "vlm_async"
    STALE = "vlm_stale"


@dataclass(frozen=True, slots=True)
class VLMSliceSpec:
    """Frozen controls for one real VLM request."""

    condition: VLMSliceCondition
    result_validity_s: float = 900.0
    completion_timeout_s: float = 720.0
    join_timeout_s: float = 720.0
    probe_join_timeout_s: float = 5.0
    prelude_s: float = 1.0
    postlude_s: float = 1.0
    probe_period_ns: int = 100_000_000
    probe_deadline_ns: int = 100_000_000

    def __post_init__(self) -> None:
        if not isinstance(self.condition, VLMSliceCondition):
            raise TypeError("condition must be a VLMSliceCondition")
        validate_slice_timing(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "condition": self.condition.value,
            "task_kind": TaskKind.VLM.value,
            "request_count": 1,
            "pending_capacity": 1,
            "result_capacity": 1,
            "overflow_policy": OverflowPolicy.REJECT_NEW.value,
            "result_validity_s": self.result_validity_s,
            "completion_timeout_s": self.completion_timeout_s,
            "join_timeout_s": self.join_timeout_s,
            "probe_join_timeout_s": self.probe_join_timeout_s,
            "prelude_s": self.prelude_s,
            "postlude_s": self.postlude_s,
            "probe_period_ns": self.probe_period_ns,
            "probe_deadline_ns": self.probe_deadline_ns,
            "state_advance_after_inference_start": (
                self.condition is VLMSliceCondition.STALE
            ),
        }


@dataclass(frozen=True, slots=True)
class VLMSliceReport:
    """Closed runtime, probe and adapter facts for one request."""

    condition: VLMSliceCondition
    task_id: str
    state_advanced: bool
    consumed: bool
    final_disposition: str
    adapter: VLMExecutionRecord
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


def make_vlm_task(
    payload: PayloadRef,
    *,
    state_token: StateToken,
    task_id: str,
    result_validity_s: float,
    clock_ns: Callable[[], int] = time.monotonic_ns,
) -> TaskEnvelope:
    """Create the only request admitted by one Phase 1C run."""

    return make_single_request_task(
        payload,
        state_token=state_token,
        task_id=task_id,
        task_kind=TaskKind.VLM,
        result_validity_s=result_validity_s,
        clock_ns=clock_ns,
        supersession_key="vlm-latest",
        metadata={
            "protocol": "phase1c",
            "fixed_input": True,
            "raw_output_recorded": False,
        },
    )


def run_vlm_slice(
    spec: VLMSliceSpec,
    payload: PayloadRef,
    event_sink: RuntimeEventSink,
    *,
    adapter: VLMAdapter | None = None,
    task_id: str = "vlm-001",
) -> VLMSliceReport:
    """Run one nominal or invalidated VLM request and close all threads."""

    if not isinstance(spec, VLMSliceSpec):
        raise TypeError("spec must be a VLMSliceSpec")
    if not isinstance(payload, PayloadRef):
        raise TypeError("payload must be a PayloadRef")
    if not callable(getattr(event_sink, "emit", None)):
        raise TypeError("event_sink must provide emit(event)")
    resolved_adapter = adapter or FixedInputVLMAdapter()
    if not callable(resolved_adapter):
        raise TypeError("adapter must be callable")

    outcome = run_single_request_slice(
        workload_label="VLM",
        task_kind=TaskKind.VLM,
        state_scope="vlm-slice",
        pending_capacity=1,
        result_capacity=1,
        worker_name="phase1-vlm-worker",
        probe_name="phase1-vlm-probe",
        is_stale=spec.condition is VLMSliceCondition.STALE,
        timing=spec,
        stale_observation_s=0.0,
        task_factory=lambda state_token: make_vlm_task(
            payload,
            state_token=state_token,
            task_id=task_id,
            result_validity_s=spec.result_validity_s,
        ),
        adapter=resolved_adapter,
        event_sink=event_sink,
    )
    return VLMSliceReport(
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
