"""Execute one protocol-defined Phase 1 formal workload run."""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Mapping, Protocol

from experiments.phase1.asr_adapter import (
    ASR_EXPECTED_OUTPUT_LENGTH,
    ASR_EXPECTED_OUTPUT_SHA256,
)
from experiments.phase1.llm_adapter import (
    LLM_EMPTY_HISTORY_SHA256,
    LLM_EXPECTED_SERVED_MODEL_ID,
    frozen_llm_request_contract,
)
from experiments.phase1.simulation import InlineProbe
from jetson.phase1_runtime import (
    BoundedTaskBroker,
    BrokerSnapshot,
    CancellationToken,
    ClaimedTask,
    ExecutionOutcome,
    FinalDisposition,
    LaneConfig,
    ObservableExecutor,
    OverflowPolicy,
    PayloadRef,
    PeriodicProbe,
    ResultEnvelope,
    RuntimeEventSink,
    StateToken,
    TaskEnvelope,
    TaskKind,
)


FORMAL_RUN_SCHEMA_VERSION = "0.1.0"


class FormalCondition(str, Enum):
    """Conditions defined by the active Phase 1 formal protocol."""

    IDLE = "formal_idle"
    SYNC = "formal_sync"
    ASYNC = "formal_async"


class FormalAdapter(Protocol):
    """Privacy-preserving interface shared by the three formal adapters."""

    @property
    def last_record(self) -> object | None: ...

    def __call__(self, claimed: ClaimedTask) -> ResultEnvelope: ...


def _positive_finite(value: object, name: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError(f"{name} must be positive and finite")
    return float(value)


@dataclass(frozen=True, slots=True)
class FormalRunSpec:
    """Frozen execution controls for one measured or warm-up invocation."""

    workload: str
    condition: FormalCondition
    role: str
    prelude_s: float = 1.0
    postlude_s: float = 1.0
    result_validity_s: float = 900.0
    completion_timeout_s: float = 720.0
    join_timeout_s: float = 30.0
    probe_period_ns: int = 100_000_000
    probe_deadline_ns: int = 100_000_000

    def __post_init__(self) -> None:
        if self.workload not in {"asr", "llm", "vlm"}:
            raise ValueError("workload must be asr, llm or vlm")
        if not isinstance(self.condition, FormalCondition):
            raise TypeError("condition must be a FormalCondition")
        if self.condition is FormalCondition.IDLE:
            raise ValueError("formal workload runs cannot use formal_idle")
        if self.role not in {"warmup", "measured"}:
            raise ValueError("role must be warmup or measured")
        for value, name in (
            (self.prelude_s, "prelude_s"),
            (self.postlude_s, "postlude_s"),
            (self.result_validity_s, "result_validity_s"),
            (self.completion_timeout_s, "completion_timeout_s"),
            (self.join_timeout_s, "join_timeout_s"),
        ):
            _positive_finite(value, name)
        if self.result_validity_s <= self.completion_timeout_s:
            raise ValueError("result_validity_s must exceed completion_timeout_s")
        for value, name in (
            (self.probe_period_ns, "probe_period_ns"),
            (self.probe_deadline_ns, "probe_deadline_ns"),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")


def _task_kind(workload: str) -> TaskKind:
    return {
        "asr": TaskKind.ASR,
        "llm": TaskKind.LLM,
        "vlm": TaskKind.VLM,
    }[workload]


def _make_task(
    spec: FormalRunSpec,
    payload: PayloadRef,
    *,
    task_id: str,
    state_token: StateToken,
) -> TaskEnvelope:
    now = time.monotonic_ns()
    return TaskEnvelope(
        task_id=task_id,
        task_kind=_task_kind(spec.workload),
        source_monotonic_ns=now,
        created_monotonic_ns=now,
        deadline_monotonic_ns=(now + int(spec.result_validity_s * 1_000_000_000)),
        state_token=state_token,
        payload=payload,
        metadata={
            "protocol": "phase1_g6_formal",
            "fixed_input": True,
            "history_sha256": (
                LLM_EMPTY_HISTORY_SHA256 if spec.workload == "llm" else None
            ),
            "history_messages": 0 if spec.workload == "llm" else None,
            "raw_output_recorded": False,
        },
    )


def _snapshot(snapshot: BrokerSnapshot) -> dict[str, object]:
    return {
        "state": snapshot.state.value,
        "submission_attempts": snapshot.submission_attempts,
        "admitted_total": snapshot.admitted_total,
        "rejected_at_ingress_total": snapshot.rejected_at_ingress_total,
        "terminal_admitted_total": snapshot.terminal_admitted_total,
        "queued": snapshot.queued,
        "running": snapshot.running,
        "result_pending": snapshot.result_pending,
        "max_pending_depth": snapshot.max_pending_depth,
        "max_result_depth": snapshot.max_result_depth,
        "accounting_holds": snapshot.accounting_holds,
        "disposition_counts": {
            disposition.value: count
            for disposition, count in snapshot.disposition_counts
        },
    }


def _probe_record(report: object, *, inline: bool) -> dict[str, object]:
    return {
        "implementation": "inline_same_thread" if inline else "independent_thread",
        "joined": bool(getattr(report, "joined", True)),
        "tick_count": int(getattr(report, "tick_count")),
        "skipped_releases": int(getattr(report, "skipped_releases")),
        "deadline_miss_count": int(getattr(report, "deadline_miss_count")),
        "max_lateness_ns": int(getattr(report, "max_lateness_ns")),
        "max_gap_ns": int(getattr(report, "max_gap_ns")),
        "error_code": getattr(report, "error_code", None),
    }


def _adapter_record(adapter: FormalAdapter, task_id: str) -> dict[str, object]:
    record = adapter.last_record
    if record is None or getattr(record, "task_id", None) != task_id:
        raise RuntimeError("adapter did not publish the matching execution record")
    converter = getattr(record, "to_dict", None)
    if not callable(converter):
        raise TypeError("adapter execution record must provide to_dict()")
    value = converter()
    if not isinstance(value, dict):
        raise TypeError("adapter execution record must serialize to an object")
    return value


def _process_record(adapter: FormalAdapter) -> dict[str, object] | None:
    report = getattr(adapter, "last_process_report", None)
    if report is None:
        return None
    converter = getattr(report, "to_dict", None)
    if not callable(converter):
        raise TypeError("process report must provide to_dict()")
    value = converter()
    if not isinstance(value, dict):
        raise TypeError("process report must serialize to an object")
    return value


def _result_record(result: ResultEnvelope) -> dict[str, object]:
    return {
        "task_id": result.task_id,
        "task_kind": result.task_kind.value,
        "state_scope_id": result.state_token.scope_id,
        "state_generation": result.state_token.generation,
        "source_monotonic_ns": result.source_monotonic_ns,
        "deadline_monotonic_ns": result.deadline_monotonic_ns,
        "input_sha256": result.input_sha256,
        "started_monotonic_ns": result.started_monotonic_ns,
        "finished_monotonic_ns": result.finished_monotonic_ns,
        "execution_outcome": result.execution_outcome.value,
        "output": (
            None
            if result.output_sha256 is None
            else {
                "sha256": result.output_sha256,
                "length": result.output_length,
                "raw_text_recorded": False,
            }
        ),
        "output_ref_recorded": False,
        "error_code": result.error_code,
        "cancellation": {
            "requested": result.cancellation_report.requested,
            "client_wait_stopped": result.cancellation_report.client_wait_stopped,
            "worker_observed": result.cancellation_report.worker_observed,
            "backend_stop_confirmed": (
                result.cancellation_report.backend_stop_confirmed
            ),
        },
    }


def _gate(name: str, passed: bool, observed: object) -> dict[str, object]:
    return {"name": name, "passed": bool(passed), "observed": observed}


def _gates(
    spec: FormalRunSpec,
    *,
    result: ResultEnvelope,
    adapter: Mapping[str, object],
    process: Mapping[str, object] | None,
    probe: Mapping[str, object],
    runtime: Mapping[str, object],
    thermal_stop_requested: bool,
) -> list[dict[str, object]]:
    output = adapter.get("output")
    result_output_matches = (
        isinstance(output, Mapping)
        and output.get("sha256") == result.output_sha256
        and output.get("length") == result.output_length
    )
    cancellation = adapter.get("cancellation")
    cancellation_record = cancellation if isinstance(cancellation, Mapping) else {}
    gates = [
        _gate(
            "adapter_completed",
            result.execution_outcome is ExecutionOutcome.OK
            and adapter.get("execution_outcome") == ExecutionOutcome.OK.value
            and adapter.get("error_code") is None
            and adapter.get("task_id") == result.task_id
            and adapter.get("started_monotonic_ns") == result.started_monotonic_ns
            and adapter.get("finished_monotonic_ns") == result.finished_monotonic_ns
            and result.input_sha256
            == (
                adapter.get("input", {}).get("sha256")
                if isinstance(adapter.get("input"), Mapping)
                else None
            )
            and result_output_matches
            and result.output_ref is None,
            {
                "result_outcome": result.execution_outcome.value,
                "record_outcome": adapter.get("execution_outcome"),
                "error_code": adapter.get("error_code"),
                "task_identity_matches": adapter.get("task_id") == result.task_id,
                "timing_matches": adapter.get("started_monotonic_ns")
                == result.started_monotonic_ns
                and adapter.get("finished_monotonic_ns")
                == result.finished_monotonic_ns,
                "output_matches": result_output_matches,
                "output_ref_recorded": result.output_ref is not None,
            },
        ),
        _gate(
            "output_private",
            isinstance(output, Mapping)
            and output.get("raw_text_recorded") is False
            and "output_ref" not in adapter,
            {
                "output_present": isinstance(output, Mapping),
                "raw_text_recorded": (
                    output.get("raw_text_recorded")
                    if isinstance(output, Mapping)
                    else None
                ),
            },
        ),
        _gate(
            "cancellation_absent",
            cancellation_record.get("requested") is False
            and result.cancellation_report.requested is False,
            {
                "adapter_requested": cancellation_record.get("requested"),
                "result_requested": result.cancellation_report.requested,
            },
        ),
        _gate(
            "probe_closed",
            probe.get("joined") is True
            and probe.get("error_code") is None
            and isinstance(probe.get("tick_count"), int)
            and probe.get("tick_count", 0) >= 2,
            {
                "joined": probe.get("joined"),
                "error_code": probe.get("error_code"),
                "tick_count": probe.get("tick_count"),
            },
        ),
        _gate(
            "thermal_stop_absent",
            not thermal_stop_requested,
            thermal_stop_requested,
        ),
    ]
    if spec.condition is FormalCondition.SYNC:
        synchronous_boundary_holds = (
            adapter.get("worker_thread_id") == threading.get_ident()
            if spec.workload != "vlm"
            else process is not None
            and process.get("start_method") == "spawn"
            and process.get("protocol_complete") is True
        )
        gates.extend(
            [
                _gate(
                    "synchronous_call_boundary",
                    synchronous_boundary_holds,
                    {
                        "workload": spec.workload,
                        "adapter_thread_id": adapter.get("worker_thread_id"),
                        "calling_thread_id": threading.get_ident(),
                        "process_start_method": (
                            process.get("start_method") if process is not None else None
                        ),
                    },
                ),
                _gate(
                    "runtime_not_used",
                    runtime.get("used") is False,
                    runtime.get("used"),
                ),
            ]
        )
    else:
        snapshot = runtime.get("final_snapshot")
        snapshot_record = snapshot if isinstance(snapshot, Mapping) else {}
        shutdown = runtime.get("shutdown")
        shutdown_record = shutdown if isinstance(shutdown, Mapping) else {}
        dispositions = snapshot_record.get("disposition_counts")
        disposition_record = dispositions if isinstance(dispositions, Mapping) else {}
        gates.extend(
            [
                _gate(
                    "single_consumed_request",
                    snapshot_record.get("submission_attempts") == 1
                    and snapshot_record.get("admitted_total") == 1
                    and snapshot_record.get("terminal_admitted_total") == 1
                    and disposition_record.get(FinalDisposition.CONSUMED.value) == 1,
                    {
                        "submission_attempts": snapshot_record.get(
                            "submission_attempts"
                        ),
                        "admitted_total": snapshot_record.get("admitted_total"),
                        "terminal_admitted_total": snapshot_record.get(
                            "terminal_admitted_total"
                        ),
                        "disposition_counts": dict(disposition_record),
                    },
                ),
                _gate(
                    "bounded_lane",
                    snapshot_record.get("max_pending_depth", 2) <= 1
                    and snapshot_record.get("max_result_depth", 2) <= 1
                    and snapshot_record.get("accounting_holds") is True,
                    {
                        "max_pending_depth": snapshot_record.get("max_pending_depth"),
                        "max_result_depth": snapshot_record.get("max_result_depth"),
                        "accounting_holds": snapshot_record.get("accounting_holds"),
                    },
                ),
                _gate(
                    "worker_joined",
                    shutdown_record.get("complete") is True
                    and shutdown_record.get("joined") is True
                    and shutdown_record.get("worker_error_code") is None
                    and shutdown_record.get("event_error_code") is None,
                    dict(shutdown_record),
                ),
            ]
        )
    if spec.workload == "asr":
        process_value = adapter.get("process")
        asr_process = process_value if isinstance(process_value, Mapping) else {}
        gates.extend(
            [
                _gate(
                    "transcript_identity",
                    isinstance(output, Mapping)
                    and output.get("sha256") == ASR_EXPECTED_OUTPUT_SHA256
                    and output.get("length") == ASR_EXPECTED_OUTPUT_LENGTH,
                    {
                        "sha256": (
                            output.get("sha256")
                            if isinstance(output, Mapping)
                            else None
                        ),
                        "length": (
                            output.get("length")
                            if isinstance(output, Mapping)
                            else None
                        ),
                    },
                ),
                _gate(
                    "child_process_reaped",
                    asr_process.get("started") is True
                    and asr_process.get("exit_code") == 0
                    and asr_process.get("reaped") is True,
                    dict(asr_process),
                ),
            ]
        )
    if spec.workload == "llm":
        request = adapter.get("request")
        request_record = request if isinstance(request, Mapping) else {}
        response = adapter.get("response")
        response_record = response if isinstance(response, Mapping) else {}
        usage = response_record.get("usage")
        usage_record = usage if isinstance(usage, Mapping) else {}
        residency = adapter.get("model_residency")
        residency_record = residency if isinstance(residency, Mapping) else {}
        expected_request = {
            **frozen_llm_request_contract(),
            "raw_prompt_recorded": False,
        }
        gates.extend(
            [
                _gate(
                    "request_contract_verified",
                    dict(request_record) == expected_request,
                    {
                        "matches": dict(request_record) == expected_request,
                        "raw_prompt_recorded": request_record.get(
                            "raw_prompt_recorded"
                        ),
                    },
                ),
                _gate(
                    "token_usage_valid",
                    response_record.get("model") == LLM_EXPECTED_SERVED_MODEL_ID
                    and response_record.get("raw_response_recorded") is False
                    and all(
                        isinstance(usage_record.get(name), int)
                        and not isinstance(usage_record.get(name), bool)
                        and usage_record.get(name, 0) > 0
                        for name in (
                            "prompt_tokens",
                            "completion_tokens",
                            "total_tokens",
                        )
                    )
                    and usage_record.get("total_tokens")
                    == usage_record.get("prompt_tokens", 0)
                    + usage_record.get("completion_tokens", 0),
                    {
                        "model": response_record.get("model"),
                        "usage": dict(usage_record),
                        "raw_response_recorded": response_record.get(
                            "raw_response_recorded"
                        ),
                    },
                ),
                _gate(
                    "server_residency_claim_bounded",
                    residency_record.get("policy") == "external_llama_server_resident"
                    and residency_record.get("server_preexisting") is True
                    and residency_record.get("unload_requested") is False
                    and residency_record.get("backend_stop_confirmed") is None,
                    dict(residency_record),
                ),
            ]
        )
    if spec.workload == "vlm":
        process_record = process or {}
        residency = adapter.get("model_residency")
        residency_record = residency if isinstance(residency, Mapping) else {}
        gates.extend(
            [
                _gate(
                    "translation_route_verified",
                    adapter.get("translation_route") == "qwen",
                    adapter.get("translation_route"),
                ),
                _gate(
                    "child_process_reaped",
                    process_record.get("protocol_complete") is True
                    and process_record.get("exit_code") == 0
                    and process_record.get("error_code") is None
                    and process_record.get("joined_monotonic_ns") is not None,
                    dict(process_record),
                ),
                _gate(
                    "model_unload_claim_bounded",
                    residency_record.get("unload_requested") is True
                    and residency_record.get("unload_confirmed") is None,
                    dict(residency_record),
                ),
            ]
        )
    return gates


def _monitor_cancellation(
    token: CancellationToken,
    stop_requested: threading.Event,
    finished: threading.Event,
) -> None:
    while not finished.wait(0.02):
        if stop_requested.is_set():
            token.request("formal_thermal_stop", time.monotonic_ns())
            return


def _run_sync(
    spec: FormalRunSpec,
    payload: PayloadRef,
    event_sink: RuntimeEventSink,
    adapter: FormalAdapter,
    *,
    task_id: str,
    thermal_stop: threading.Event,
) -> tuple[ResultEnvelope, dict[str, object], dict[str, object]]:
    probe = InlineProbe(
        period_ns=spec.probe_period_ns,
        deadline_ns=spec.probe_deadline_ns,
        event_sink=event_sink,
    )
    probe.start()
    result: ResultEnvelope | None = None
    probe_report = None
    try:
        probe.run_until(time.monotonic_ns() + int(spec.prelude_s * 1_000_000_000))
        if thermal_stop.is_set():
            raise RuntimeError("thermal stop was requested before workload execution")
        task = _make_task(
            spec,
            payload,
            task_id=task_id,
            state_token=StateToken("phase1-formal", 0),
        )
        token = CancellationToken()
        finished = threading.Event()
        watcher = threading.Thread(
            target=_monitor_cancellation,
            args=(token, thermal_stop, finished),
            name="phase1-formal-thermal-watch",
            daemon=False,
        )
        watcher.start()
        try:
            started = time.monotonic_ns()
            result = adapter(
                ClaimedTask(
                    task=task,
                    cancellation_token=token,
                    started_monotonic_ns=started,
                )
            )
        finally:
            finished.set()
            watcher.join(spec.join_timeout_s)
        if watcher.is_alive():
            raise RuntimeError("thermal watcher did not join")
        probe.run_until(time.monotonic_ns() + int(spec.postlude_s * 1_000_000_000))
    finally:
        probe_report = probe.stop()
    if result is None:
        raise RuntimeError("formal synchronous adapter did not return a result")
    probe_record = _probe_record(probe_report, inline=True)
    runtime = {
        "used": False,
        "pending_capacity": 0,
        "result_capacity": 0,
        "final_snapshot": None,
        "shutdown": None,
    }
    return result, probe_record, runtime


def _run_async(
    spec: FormalRunSpec,
    payload: PayloadRef,
    event_sink: RuntimeEventSink,
    adapter: FormalAdapter,
    *,
    task_id: str,
    thermal_stop: threading.Event,
) -> tuple[ResultEnvelope, dict[str, object], dict[str, object]]:
    broker = BoundedTaskBroker(
        LaneConfig(
            task_kind=_task_kind(spec.workload),
            pending_capacity=1,
            result_capacity=1,
            overflow_policy=OverflowPolicy.REJECT_NEW,
        )
    )
    executor = ObservableExecutor(
        broker,
        adapter,
        event_sink=event_sink,
        worker_name=f"phase1-formal-{spec.workload}-worker",
    )
    probe = PeriodicProbe(
        period_ns=spec.probe_period_ns,
        deadline_ns=spec.probe_deadline_ns,
        event_sink=event_sink,
        thread_name=f"phase1-formal-{spec.workload}-probe",
    )
    executor.start()
    probe.start()
    shutdown = None
    result: ResultEnvelope | None = None
    try:
        threading.Event().wait(spec.prelude_s)
        if thermal_stop.is_set():
            raise RuntimeError("thermal stop was requested before workload execution")
        task = _make_task(
            spec,
            payload,
            task_id=task_id,
            state_token=broker.current_state_token("phase1-formal"),
        )
        submission = executor.submit(task)
        if not submission.admitted:
            raise RuntimeError("formal asynchronous request was not admitted")
        deadline = time.monotonic() + spec.completion_timeout_s
        while time.monotonic() < deadline:
            snapshot = executor.snapshot()
            if snapshot.result_pending == 1 or snapshot.terminal_admitted_total == 1:
                break
            if thermal_stop.is_set():
                executor.cancel(task_id, reason="formal_thermal_stop")
                raise RuntimeError(
                    "thermal stop was requested during workload execution"
                )
            threading.Event().wait(0.01)
        else:
            executor.cancel(task_id, reason="formal_completion_timeout")
            raise TimeoutError("formal asynchronous request did not complete")
        decision = executor.consume_next()
        if decision is None or not decision.consumed or decision.result is None:
            raise RuntimeError("formal asynchronous result was not consumed")
        result = decision.result
        threading.Event().wait(spec.postlude_s)
        shutdown = executor.shutdown(
            cancel_live=False,
            join_timeout_s=spec.join_timeout_s,
        )
    finally:
        if shutdown is None and executor.is_alive:
            shutdown = executor.shutdown(
                cancel_live=True,
                join_timeout_s=spec.join_timeout_s,
            )
        probe_report = probe.stop(join_timeout_s=spec.join_timeout_s)
    if result is None or shutdown is None or not shutdown.complete:
        raise RuntimeError("formal asynchronous runtime did not close cleanly")
    probe_record = _probe_record(probe_report, inline=False)
    runtime = {
        "used": True,
        "pending_capacity": 1,
        "result_capacity": 1,
        "final_snapshot": _snapshot(executor.snapshot()),
        "shutdown": {
            "complete": shutdown.complete,
            "broker_state": shutdown.broker_state.value,
            "joined": shutdown.joined,
            "join_latency_ns": shutdown.join_latency_ns,
            "active_cancellation_requested": (shutdown.active_cancellation_requested),
            "worker_error_code": shutdown.worker_error_code,
            "event_error_code": shutdown.event_error_code,
        },
    }
    return result, probe_record, runtime


def run_formal_workload(
    spec: FormalRunSpec,
    payload: PayloadRef,
    event_sink: RuntimeEventSink,
    adapter: FormalAdapter,
    *,
    task_id: str,
    thermal_stop: threading.Event | None = None,
) -> dict[str, object]:
    """Run one workload and return closed facts without payload or output text."""

    if not isinstance(spec, FormalRunSpec):
        raise TypeError("spec must be a FormalRunSpec")
    if not isinstance(payload, PayloadRef):
        raise TypeError("payload must be a PayloadRef")
    if not callable(getattr(event_sink, "emit", None)):
        raise TypeError("event_sink must provide emit(event)")
    if not callable(adapter):
        raise TypeError("adapter must be callable")
    stop = thermal_stop or threading.Event()
    started_ns = time.monotonic_ns()
    if spec.condition is FormalCondition.SYNC:
        result, probe, runtime = _run_sync(
            spec,
            payload,
            event_sink,
            adapter,
            task_id=task_id,
            thermal_stop=stop,
        )
    else:
        result, probe, runtime = _run_async(
            spec,
            payload,
            event_sink,
            adapter,
            task_id=task_id,
            thermal_stop=stop,
        )
    finished_ns = time.monotonic_ns()
    adapter_record = _adapter_record(adapter, task_id)
    process_record = _process_record(adapter)
    result_record = _result_record(result)
    gates = _gates(
        spec,
        result=result,
        adapter=adapter_record,
        process=process_record,
        probe=probe,
        runtime=runtime,
        thermal_stop_requested=stop.is_set(),
    )
    return {
        "formal_run_schema_version": FORMAL_RUN_SCHEMA_VERSION,
        "workload": spec.workload,
        "condition": spec.condition.value,
        "role": spec.role,
        "task_id": task_id,
        "started_monotonic_ns": started_ns,
        "finished_monotonic_ns": finished_ns,
        "duration_ns": finished_ns - started_ns,
        "adapter": adapter_record,
        "result": result_record,
        "process": process_record,
        "probe": probe,
        "runtime": runtime,
        "thermal_stop_requested": stop.is_set(),
        "gates": gates,
        "valid": all(gate["passed"] is True for gate in gates),
    }
