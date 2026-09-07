# Phase 1 Runtime Contract

This document defines the correctness and safety contract for the first
asynchronous runtime used by the Octopus robot research platform. The
host-only model, broker, observable executor, periodic probes, trace replay and
portable simulation protocol have been implemented. One motion-disabled Jetson
simulation pilot has validated the protocol and runtime semantics. A
fixed-input VLM correctness pilot has also completed nominal consumption and
old-generation rejection on the Jetson. A spawned-process VLM adapter and its
evidence Gates have since completed one process-isolated Jetson correctness
pilot. Fixed-input ASR and LLM adapters have since completed independently
validated and analyzed Jetson correctness pilots. All three real-workload
correctness components are complete, closing G5. The protocol-bound formal
session runner and independent analyzer are implemented. Jetson commissioning
exposed an LLM empty-history identity mismatch before measurement and a
resource-trace tail race after one complete session. An outcome-independent
design audit then found
that v1 repeated the same condition/predecessor relationship across sessions.
Neither collection is admissible formal evidence; v2 changed only the frozen
condition-order matrix while retaining the remaining scientific design. The
first v2 attempt then stopped on a VLM Qwen timeout and failed its required
translation-route Gate. V2 is closed without a confirmatory claim. A subsequent
descriptive Jetson diagnostic validated the corrected Moondream-unload-before-
Qwen order in both lifecycle conditions. G6 v3 retains the v2 scientific design
and timeout while binding that order and spawned-process protocol `0.2.0`. Its
first formal attempt stopped on a synchronous VLM Qwen timeout and failed two
system-under-test Gates. V3 is permanently closed. A later three-repetition
diagnostic supports deterministic model requests, explicit unload confirmation
and a 60 s Qwen client boundary. A subsequent nonformal target validation
directly exercised the modified repository path in both VLM lifecycle
conditions; both runs confirmed unload, used the Qwen route and passed their
slice/process Gates. G6 v4 retains the complete v3 scientific design and freezes
the target-validated repair. Its reviewed merge activates a fresh formal
collection from session 1. Phase 1 remains incomplete; no formal performance
comparison or application slice is authorized yet.

> 中文简介：本文冻结 Phase 1 异步运行时的任务模型、生命周期、队列、取消、结果新鲜度、
> 快速周期代理和安全边界。host-only worker、周期探针、trace replay 和模拟实验运行器已实现；
> Jetson simulation pilot 与固定输入 VLM correctness pilot 已完成并通过独立验证；
> VLM 进程隔离路径以及固定输入 ASR、LLM 路径均已完成 Jetson correctness pilot，G5 已关闭；
> G6 v2 正式协议及其失败证据已冻结；协议绑定的正式 runner 与独立分析器已实现；Jetson
> commissioning 先后发现 measured run 开始前的 LLM 空历史身份不一致，以及一个完整
> session 后的资源轨迹尾部竞态；结果无关设计审计随后发现 v1 的跨 session 条件顺序关系
> 重复；两次 collection 均不作为正式证据，v2 仅修改冻结的条件顺序矩阵。首次 v2 正式
> 尝试随后因 VLM 的 Qwen 30 秒超时而停止并关闭；随后完成的描述性 Jetson 诊断验证了
> 修正后的 `Moondream -> 卸载请求 -> Qwen` 路径。G6 v3 保留 v2 的科学设计和超时，
> 仅冻结该顺序与进程协议。首次 v3 正式尝试又因同步 VLM Qwen 超时触发两个系统被测
> 对象 Gate 失败，v3 永久关闭。随后完成的三次描述性诊断支持确定性请求、显式卸载确认
> 与 60 秒 Qwen 边界；之后的非正式目标机验证直接运行了修改后的仓库路径，两个 VLM
> 生命周期条件均确认卸载、使用 Qwen 路径并通过切片与进程 Gate。G6 v4 保留 v3 的
> 完整科学设计并冻结目标机验证后的修复，评审合并后从 session 1 开始新的正式采集。
> Phase 1 尚未完成，v4 对照通过前不进行整机应用切片。

## Status

- Phase: incomplete after the closed G6 v3 attempt; repaired VLM path validated
  on target and frozen in G6 v4; fresh formal comparison pending; application
  slice not authorized
- Contract status: frozen through independently validated Jetson simulation,
  thread/process VLM pilots, and fixed-input ASR and LLM correctness pilots
- VLM-pilot result: `main@aebd1a2`, session
  `20260830T073825Z_phase1_vlm_pilot`
- VLM-pilot public analysis: `main@95a839d`
- Process-isolated VLM result: `main@1818c83`, session
  `20260830T122541Z_phase1_vlm_process_reaping`
- ASR-pilot result: `main@bc1ca35`, session
  `20260831T140705Z_phase1_asr_pilot_v2`
- LLM-pilot result: `main@6e83ede`, session
  `20260901T143315Z_phase1_llm_pilot`
- G6 v2 failed attempt: `main@1e5e1c7`, collection
  `20260905T140816Z_phase1_formal_g6_v2`; no formal claim permitted
- VLM residency-order diagnostic: `main@08e262d`, session
  `20260905T160805Z_phase1_vlm_residency_diag`; descriptive only
- G6 v3 failed attempt: `main@3dca66b`, collection
  `20260906T055511Z_phase1_formal_g6_v3`; no formal claim permitted
- VLM timeout-repair diagnostic: `main@52c041d`, diagnostic
  `20260906T082627Z_phase1_vlm_timeout_diag`; descriptive only
- VLM timeout-repair target validation: base `main@52c041d`, validation
  `20260906T101723Z_phase1_vlm_timeout_repair_validation`; modified repository
  path validated; nonformal correctness evidence only
- G6 v4 preregistration: retains the v3 scientific design, freezes the VLM
  request and unload-confirmation repair, and activates only after reviewed merge
- Jetson-pilot result: `main@77138f2`, session `20260828T121142Z_phase1_jetson_pilot`
- Jetson-pilot harness starting point: `main@844b633`
- Simulation-runner starting point: `main@4514d97`
- Observable-executor starting point: `main@1d29b14`
- Initial runtime-kernel starting point: `main@043e1bb`
- Synchronous functional baseline: `61db058`
- Physical motion: disabled and outside this phase
- UART and STM32 firmware: unchanged

The contract was reviewed before the host-only implementation began. Later
implementation findings may require an explicit contract revision, but the
contract must not be changed silently after formal data collection starts.

The current implementation includes:

- immutable task, result, state and payload-reference contracts;
- bounded pending, active, result-mailbox, state-scope and terminal ownership;
- one non-daemon worker with cooperative cancellation and finite join reports;
- a simulated adapter with finite service time and explicit cancellation facts;
- inline and independent absolute-schedule periodic probes;
- schema `0.2.0` JSONL recording and profile-aware offline lifecycle replay;
- R0--R4 simulated-condition orchestration, atomic manifests, validation and
  descriptive summaries;
- fail-closed Jetson preflight, a continuous non-daemon `tegrastats` sampler,
  an immutable pilot matrix and independent session reconstruction;
- a lazy fixed-input VLM adapter, nominal/stale single-request orchestration,
  model-service preflight, resource trace and independent run validation;
- deterministic VLM-pilot reconstruction and fail-closed recording of actual
  model-service listener bindings for subsequent runs;
- a per-request spawned-process VLM supervisor, bounded IPC, explicit process
  cleanup facts and independently rebuilt process Gates;
- deterministic process-pilot reconstruction with a hash-fixed descriptive
  thread reference.
- a shared deterministic VLM request contract, a 60 s Qwen client boundary and
  fail-closed Ollama process-list polling after each unload request;
- a fixed-input ASR adapter that supervises the native `whisper-cli` process,
  publishes transcript identity only, and distinguishes normal exit, timeout,
  cancellation termination and process reaping;
- ASR-specific preflight, nominal/stale orchestration, atomic run artifacts,
  deterministic Gates and an independent validator;
- deterministic ASR-pilot reconstruction and a hash-fixed public descriptive
  report;
- a fixed-input LLM adapter that preserves the Phase 0 prompt, empty-history
  snapshot and llama.cpp request contract while serializing only output identity
  and token usage;
- LLM-specific model/server preflight, nominal/stale orchestration, resource
  coverage Gates, atomic artifacts and an independent validator;
- deterministic LLM-pilot reconstruction and a hash-fixed public descriptive
  report;
- a machine-validated G6 preregistration with fixed hypotheses, environment,
  paired schedule, thresholds, exclusions, stopping rules and analysis method;
- a protocol-bound formal session runner with exact preflight identities,
  inline synchronous and bounded asynchronous paths, continuous thermal/resource
  monitoring, append-only ordering evidence and fail-closed session artifacts;
- an independent formal analyzer that revalidates artifact hashes, reconstructs
  every preregistered pair and applies the frozen hierarchical bootstrap and
  intersection-union decision;
- a deterministic failed-attempt analyzer that verifies an aborted collection's
  artifact inventory, protocol, preflight, ledger prefix, run records, resource
  trace and correlated llama-server cancellation without publishing raw logs;
- a VLM adapter residency-order correction that requests Moondream unload before
  Qwen and records bounded stage exception classes while preserving legacy
  schema reconstruction.
- a deterministic residency-order diagnostic analyzer that binds the collection
  and service-log archives, verifies the corrected stage/process contract and
  publishes only claim-bounded derived evidence;
- a G6 v3 preregistration that preserves the v2 schedule, hypotheses, sample
  size, thresholds and analysis while binding the corrected VLM order;
- a deterministic G6 v3 failure analyzer that verifies the closed attempt,
  correlates all llama-server requests and publishes only claim-bounded evidence.

The VLM, ASR and LLM pilots include no formal performance data. They validate
real-model integration, result freshness and workload-specific boundaries. The
thread pilot's skipped releases show that the simulated sleep result cannot be
generalized to every Python worker workload; the later process pilot removes
those observed gaps in two single-run conditions. No completed pilot authorizes an
asynchronous-performance, hard-real-time, timing-isolation or
heterogeneous-inference claim.

## Research objective

Phase 1 investigates whether slow ASR, LLM, and VLM work can be separated from
time-constrained Jetson work without allowing unbounded backlog or invalid
results to escape into downstream consumers.

The main research claims will concern observed behavior:

1. a bounded runtime can keep slow work from owning the fast execution path;
2. every admitted task can be assigned one closed lifecycle;
3. expired, cancelled, superseded, mismatched, or old-generation results can
   be rejected before consumption;
4. the runtime overhead can be measured against a direct synchronous call that
   uses the same workload adapter;
5. the same kernel can express different admission policies without hiding
   their workload-specific semantics.

Python threads are not hard-real-time threads. Phase 1 reports measured period,
lateness, gaps, and deadline misses. It does not provide a worst-case execution
time or operating-system scheduling guarantee.

## Scope and exclusions

Phase 1 includes:

- an immutable task and result model;
- one bounded single-consumer inference lane per experiment;
- bounded input and accepted-result storage;
- scoped state generations and result freshness checks;
- explicit queued, in-flight, and post-completion cancellation semantics;
- a 100 ms software periodic probe;
- privacy-preserving event traces and independent lifecycle replay;
- simulated, VLM, ASR, and LLM adapters introduced in that order;
- synchronous and asynchronous experimental conditions sharing one adapter.

Phase 1 excludes:

- physical motion and automatic UART access;
- changes to the STM32 firmware, watchdog, or wire protocol;
- encoder feedback control and mecanum kinematics;
- CPU affinity, real-time priority, model residency comparisons, and
  simultaneous LLM/VLM inference;
- upgrades to JetPack, CUDA, PyTorch, ONNX Runtime, or model weights;
- replacement or removal of the validated synchronous `jetson.app` path.

## Safety boundary

All Phase 1 automated tests and initial Jetson experiments must satisfy:

- `ROBOT_ENABLE_MOTION` is unset or explicitly false;
- an unrecognized motion value causes the runner to refuse startup;
- the Phase 1 runtime does not import `jetson.robot_comm`,
  `jetson.motion_planner`, or `jetson.app`;
- `/dev/ttyTHS1` is not opened;
- no STM32 or motor power is required;
- Phase 0 run directories, schemas, reports, and archives are never modified;
- new data uses a Phase 1 run root and schema identity.

Any UART access or physical motion output is a stop condition. Performance
results cannot compensate for a safety-boundary failure.

## Time model

Durations, queue waits, result ages, and scheduling metrics use
`time.monotonic_ns()` only. Wall time is recorded only for human correlation.

For one task:

```text
source <= created <= enqueued <= started <= finished <= consumed
source <= deadline
```

Some later timestamps do not exist for tasks rejected or cancelled earlier.
The deadline is a consume-by validity boundary; it does not claim that Python,
an HTTP client, or a model backend can be forcibly preempted at that instant.

Cross-run and cross-device absolute monotonic timestamps are not compared.
Only within-run differences are meaningful.

## Identity model

### StateToken

A bare integer generation is insufficient when independent conversations or
task scopes coexist. Each task therefore carries:

| Field | Meaning |
| --- | --- |
| `scope_id` | Bounded identifier for the state domain, such as one interaction |
| `generation` | Non-negative generation within that scope |

Changing one scope must not invalidate an unrelated scope.

### PayloadRef

| Field | Meaning |
| --- | --- |
| `ref` | Private file or immutable-object reference used by the worker |
| `sha256` | Content identity captured before admission |
| `size_bytes` | Expected bounded payload size |
| `media_type` | Bounded media type such as `image/jpeg` or `audio/wav` |

The event trace records the hash, size, and media type, not the raw payload or
private absolute path. A file payload is verified before admission and again
before execution.

Live acquisition must create a unique immutable file per task. The current
single `scene_vlm.jpg` path must not be reused by an asynchronous producer,
because a later capture could overwrite data referenced by a queued or running
task.

## TaskEnvelope

The initial immutable task envelope contains:

| Field | Requirement |
| --- | --- |
| `task_id` | Unique, stable, 1 to 128 characters |
| `task_kind` | `simulated`, `vlm`, `asr`, or `llm` |
| `parent_task_id` | Optional bounded parent interaction ID |
| `supersession_key` | Optional key for coalescing related pending tasks |
| `source_monotonic_ns` | Source observation or utterance completion time |
| `created_monotonic_ns` | Envelope construction time |
| `deadline_monotonic_ns` | Last valid consumption time |
| `state_token` | Scoped generation captured at creation |
| `payload` | `PayloadRef` identity |
| `metadata` | Small bounded scalar metadata only |

Metadata must not become an unbounded escape hatch. The initial implementation
will reject non-scalar values, excessive keys, oversized strings, and a
serialized representation above a fixed byte limit. Prompts, responses,
images, audio, tracebacks, and secrets are never metadata.

## ResultEnvelope

The immutable result envelope contains:

| Field | Requirement |
| --- | --- |
| `task_id` | Must match the admitted task |
| `task_kind` | Must match the admitted task |
| `state_token` | Propagated without worker modification |
| `source_monotonic_ns` | Propagated without worker modification |
| `deadline_monotonic_ns` | Propagated without worker modification |
| `input_sha256` | Must match the admitted payload identity |
| `started_monotonic_ns` | Worker claim time |
| `finished_monotonic_ns` | Execution completion time |
| `execution_outcome` | `ok`, `error`, `timeout`, or `cancel_observed` |
| `output_sha256` | Optional privacy-preserving output identity |
| `output_length` | Optional bounded output length |
| `output_ref` | Optional private result reference |
| `error_code` | Optional bounded error category |
| `cancellation_report` | What was requested, observed, and confirmed |

The worker may carry a private in-memory output for a later safe consumer. The
event serializer exposes only its bounded descriptor.

## Two orthogonal outcomes

Execution and delivery are different facts. A VLM request may execute
successfully and still be rejected because its result is old.

Execution outcomes are:

```text
not_started | ok | error | timeout | cancel_observed
```

Final dispositions are:

```text
consumed
dropped_overflow
rejected_busy
cancelled_queued
rejected_cancelled
rejected_expired
rejected_state
rejected_identity
execution_error
result_backpressure
shutdown_cancelled
```

Every admitted task receives exactly one final disposition. A submission
rejected before admission is counted separately and never appears as a live
runtime task.

## Lifecycle

The broker owns all task-location transitions:

```text
submission attempt
    |-- rejected at ingress --------------------------> terminal accounting
    `-- admitted -> QUEUED -> RUNNING -> RESULT_PENDING -> TERMINAL
                       |          |             |
                       |          |             `------> stale/cancelled
                       |          `--------------------> error/stale/cancelled
                       `-------------------------------> dropped/cancelled
```

The runtime keeps queue membership, the active task, the result mailbox,
state generations, and lifecycle records under one condition lock. This avoids
snapshots assembled from incompatible locks.

Expected accounting is:

```text
submission_attempts = admitted + rejected_at_ingress

admitted = queued + running + result_pending + terminal_admitted
```

After a successful shutdown:

```text
queued = running = result_pending = 0
```

The terminal-record cache and scoped-generation table are themselves bounded.
Evicted terminal records remain represented by cumulative counters and the
append-only event trace, preventing the task registry from becoming a hidden
unbounded queue. A lane refuses a new scope when its configured scope table is
full rather than silently forgetting an older generation.

## Admission and overflow policies

The kernel provides policies; workload adapters select them explicitly.

### VLM

- the fixed-input Phase 1C slice admits exactly one task with pending and
  result capacities of one and `reject_new` overflow;
- later live acquisition may coalesce related pending tasks by
  `supersession_key`, but that policy is not inferred from the single-request
  slice;
- any later replacement must emit a dropped disposition for the replaced
  pending task;
- a new image does not silently claim to stop an active backend request;
- active result invalidation requires an explicit cancel or state-generation
  change.

This distinction matters because continuously superseding a roughly 70-second
VLM request with camera frames could otherwise prevent any result from ever
being consumable.

### ASR

- pending capacity: 2;
- FIFO ordering;
- reject-new on overflow;
- return an explicit busy outcome to the producer;
- never silently replace an already completed utterance.

### LLM

- one active and at most one pending request;
- reject-new on overflow;
- carry a conversation-history snapshot in the private payload identity;
- update shared history only after the result is consumed, never merely after
  inference completes.

The pending capacity excludes the one active task. The accepted-result mailbox
also has an independently configured capacity and full policy.

## Freshness and identity validation

Validation happens twice.

At worker completion, the broker checks:

- the task still exists in the expected running state;
- task, kind, input hash, source time, deadline, and state token match;
- cancellation or supersession has not invalidated the task;
- the result has not already expired;
- the result mailbox has capacity.

Immediately before consumption, the broker checks again:

- current monotonic time is not after the deadline;
- the scoped generation still matches;
- cancellation or supersession has not occurred since completion;
- the result identity still matches the original task.

Only then can the result receive the `consumed` disposition. In Phase 1 the
consumer has no physical-motion side effects. A future Phase 2 motion
supervisor must perform its own final authorization before actuation.

## State-generation update

Advancing one state scope is atomic under the broker lock:

1. increment the scope generation;
2. cancel matching queued tasks;
3. request cancellation/invalidation for a matching active task;
4. reject matching result-pending tasks;
5. record one bounded state-change reason and all resulting transitions.

The operation is observable and cannot partially update the three task
locations.

## Cancellation semantics

Cancellation reports separate:

| Fact | Meaning |
| --- | --- |
| `requested` | The runtime requested cancellation or invalidation |
| `client_wait_stopped` | The local process or transport stopped waiting |
| `worker_observed` | Cooperative code observed the token |
| `backend_stop_confirmed` | Backend computation is known to have stopped |

Queued cancellation removes the task before execution. Simulated work can
cooperate fully. A Whisper child process may be terminated and reaped. Closing
or timing out an HTTP request does not by itself prove that llama.cpp or Ollama
stopped model computation; that fact remains `unknown` unless independently
confirmed.

Result invalidation is always available even when backend cancellation is not.
Documentation must say "result rejected" rather than "GPU inference
cancelled" in that case.

## Shutdown contract

The lane has `OPEN`, `CLOSING`, and `CLOSED` states. It supports:

- `drain`: reject new submissions and finish admitted work;
- `cancel`: reject new submissions, cancel pending work, signal active work,
  and reject pending results.

Shutdown returns a report containing join latency, remaining task identity,
and cancellation capability. A join timeout is a failed Gate, not a successful
shutdown. Real HTTP adapters use finite transport and overall budgets so that
failure is eventually observable, but Phase 1 does not claim instantaneous
backend preemption.

## Observable executor boundary

`ObservableExecutor` is the only traced orchestration entry for one broker.
Producers submit, cancel, advance state and consume results through this
facade; the worker uses the same boundary for claim and completion. Callers
must not mutate the owned broker directly while claiming a replayable trace.

The short boundary lock covers one broker operation, its before/after depth
accounting and synchronous event emission. It never covers workload adapter
execution. This makes concurrent producer operations totally ordered in the
trace without serializing the slow inference interval.

The worker does not automatically consume a successful result. Consumption is
an explicit upper-layer action so result-mailbox pressure and the second
freshness check remain observable. Normal adapter exceptions become bounded
`adapter_exception` execution results; exception messages and tracebacks are
not written to events. An event-sink failure stops admission and requests
cancel shutdown. A closed broker with a worker, probe or event-recording error
is not reported as a successful run.

The simulated adapter can model finite service time, execution errors,
timeouts, cooperative cancellation and a finite non-cooperative interval. It
does not model GPU work or prove backend preemption.

## Periodic probe contract

The nominal period is 100 ms. Releases use an absolute schedule:

```text
release[n] = start + n * period
```

The probe records scheduled, started, and finished monotonic timestamps,
start lateness, execution time, actual period, signed and absolute period
error, skipped releases, and deadline misses.

If delayed across multiple releases, the probe records skipped releases and
moves to the next future release. It does not emit a burst of catch-up ticks,
which would hide the real scheduling gap.

The experiment layer provides both:

- an inline probe representing the blocking synchronous orchestration path;
- an independent threaded probe representing the isolated fast timing domain.

The probe is a software scheduling proxy, not a motor controller.

Both probe paths and their pure release/tick calculations are implemented. The
inline path advances on the caller thread and therefore records releases
skipped while a direct synchronous adapter call owns that thread. The threaded
path has independent ownership, but it still shares the Python process and GIL.
The fixed-input VLM pilot consequently recorded skipped releases during lazy
module import even though inference ran in the worker thread. Thread ownership
alone is not accepted as evidence of timing isolation. Both paths invoke the
same scenario adapter contract.

## Process-isolated VLM boundary

The first mitigation keeps task admission, queue ownership, state generations,
result validation, event recording and the periodic probe in the parent
process. Only the fixed-input VLM adapter invocation moves into one child
created with the `spawn` start method. `fork` is excluded because the parent is
already multithreaded and may have initialized library state that is unsafe to
inherit.

The parent remains the only lifecycle authority. The child cannot mutate the
broker, consume a result, advance state or emit a final disposition. It receives
one immutable task descriptor through a private pipe and returns only the
existing bounded result and adapter records. Application messages are encoded
as JSON with a 65,536-byte maximum. The fixed input path is needed inside that
private message but remains excluded from the scenario, summary and process
artifacts. Model text, prompts, stdout, stderr and exception tracebacks do not
cross the evidence boundary.

One process is created per request in this initial correctness experiment. This
keeps ownership and cleanup observable without introducing a persistent worker
pool or model concurrency. The supervisor records the spawn request, child
start, inference-start signal, completion receipt, join, exit code and any
forced termination. Every successful invocation must close the protocol, exit
with code zero and be reaped without `terminate`. An EOF, malformed message,
unexpected child exit or execution timeout becomes a bounded adapter error;
the parent still reaps or terminates the child within finite budgets.

After the final protocol message is sent, the child stops its signal monitor,
closes the private pipe and exits from inside the process with the appropriate
status code. This explicit process boundary prevents imported inference
runtimes from holding the interpreter open after their bounded result has been
delivered. It does not bypass adapter cleanup: model unload and output
normalization finish before the completion message is constructed.

Cancellation is forwarded through a process-safe event. For `vlm_stale`, the
parent advances the generation only after receiving the child inference-start
signal. The child may later report that it observed cancellation, but the
broker still owns the `rejected_state` decision. Terminating or reaping the
Python child proves only the local worker-process fact. It does not prove that
Ollama, llama.cpp or GPU work stopped, so `backend_stop_confirmed` remains
unknown unless a separate backend acknowledgement is introduced.

The process runner adds `adapter_isolation=spawned_process`, a distinct run ID
and `process.json`. The latter is rebuilt from the supervisor facts in
`scenario.json` and fails closed unless spawn ownership, protocol completion,
boundary order, cancellation forwarding and normal child reaping all pass.
The runner writes the scenario, process summary and any available slice summary
before applying those final Gates. A failed manifest remains ineligible but
hashes each completed diagnostic artifact, so a cleanup failure does not erase
the evidence needed to identify it.
The original thread mode remains the default and the first VLM pilot remains
valid under its earlier artifact contract.

Host fault-injection covers nominal completion, state invalidation, child-side
execution errors, abrupt exit and timeout termination. These tests validate
the boundary implementation, not Jetson scheduling behavior. The subsequent
motion-disabled real-model pilot recorded no module-import-scale probe gaps in
its two process-isolated runs. This single fixed-order session is descriptive
evidence and does not establish a general timing-isolation claim.

## Event and replay requirements

Phase 1 creates a new event schema and run root. Phase 0 schema version `0.1.0`
remains frozen.

Schema `0.2.0` uses one shared recorder lock to assign contiguous
sequence numbers and primary monotonic timestamps. Runtime and probe threads
may share that recorder. Payload references, raw inputs, prompts, model output,
secrets and exception tracebacks are excluded; only bounded scalar event
details are accepted.

Every lifecycle event includes enough bounded information to replay:

- submission and admission result;
- transition from/to state and reason;
- queue/result depth before and after;
- configured capacity and policy;
- task identity, source, deadline, and state token;
- execution outcome and final disposition;
- cancellation facts;
- probe releases, ticks, skips, and misses;
- shutdown request and worker join outcome.

An independent offline validator reconstructs task states and queue depths from
the trace without calling runtime internals. It rejects impossible transitions,
capacity violations, duplicate terminals, identity mismatches, missing final
states, non-monotonic sequence/timestamps, or a consumed stale result.

Runtime counters alone are not accepted as correctness evidence.

The implemented lifecycle vocabulary is:

- `task.enqueued`, `task.rejected`, `task.started`, `task.finished` and
  `task.terminal`;
- `task.cancel_requested`, `task.cancel_missing` and `state.advanced`;
- `result.accepted` and `result.rejected`;
- `shutdown.requested`, `worker.started`, `worker.failed`, `worker.stopped`
  and `worker.joined`;
- `probe.started`, `probe.skipped`, `probe.tick`, `probe.failed`,
  `probe.stopped` and `probe.joined`.

The replay implementation imports no runtime classes. It validates the
serialized schema fields, reconstructs every task location and state
generation, checks recorded depths against its own counts, enforces pending,
active and result capacities, and rejects stale consumption or incomplete
shutdown. Completion uses an explicit trace profile:

- `inline_probe` requires a stopped probe and forbids worker lifecycle events;
- `threaded_probe` requires a stopped and joined probe and forbids worker
  lifecycle events;
- `runtime` requires a closed and joined worker, with an optional probe;
- `runtime_threaded_probe` requires both complete lifecycles.

The validator never infers a weaker completion contract from whichever events
happen to be present.

## Experimental decomposition

Correctness, responsiveness, and runtime overhead are separate protocols.

### Semantic correctness

Deterministic simulated schedules cover overflow, cancellation at each
lifecycle location, deadline expiry, state changes, result-mailbox pressure,
executor failure, shutdown races, and duplicate IDs. These are zero-tolerance
tests, not inferential performance comparisons.

### Responsiveness

The implemented portable conditions are:

| Condition | Probe | Slow work | Runtime |
| --- | --- | --- | --- |
| `R0_IDLE` | independent | none | none |
| `R1_INLINE_SYNC` | inline | direct synchronous | none |
| `R2_THREADED_SYNC` | independent | direct synchronous | none |
| `R3_ASYNC` | independent | worker | bounded runtime |
| `R4_STALE` | independent | non-cooperative worker plus state advance | bounded runtime |
| `R4_OVERFLOW` | independent | worker plus excess arrivals | bounded runtime |

`R2` separates the benefit of an independent fast timing domain from the
additional semantics and overhead of the full broker. R0--R3 form the primary
responsiveness decomposition. Stale-result and overflow tests remain separate
zero-tolerance correctness conditions so their different arrival and
invalidation patterns do not confound that comparison. R4 uses a claim barrier
to inject its state change or excess arrivals at a deterministic lifecycle
location; timings that include this barrier are not used for the direct-versus-
worker overhead comparison.

Each run records an atomic manifest, scenario facts, an event trace and a
descriptive summary. A manifest becomes `completed` only after explicit-profile
replay, condition-specific Gates, artifact hashing and final directory
validation pass. Pilot summaries are marked descriptive-only and cannot
authorize a performance claim.

### Runtime overhead

A queue-empty, single-task direct condition is compared with a queue-empty,
single-task worker condition. Both invoke the same workload adapter. ASR is the
sensitive low-variance control; LLM retains token-normalized metrics; VLM
retains module-import, Moondream, rewrite/fallback, and unload-request stages.

Formal analysis treats a run or paired block as the experimental unit. Probe
ticks within one run are temporally dependent and are not treated as hundreds
of independent experimental samples.

## Jetson simulation-pilot evidence contract

The first Jetson experiment remains a simulated-load pilot. It tests whether
the portable protocol and its evidence chain remain valid under real Jetson
scheduling and resource behavior; it does not test a model and does not make a
performance-improvement claim.

### Preflight

The pilot refuses to create a session unless it records all of the following:

- Linux on ARM64 with a non-empty NVIDIA L4T identity;
- `tegrastats` available in `PATH`;
- a clean, named `main` branch synchronized with the recorded `origin/main`
  commit, with zero ahead/behind counts;
- `ROBOT_ENABLE_MOTION` unset or explicitly false;
- `jetson.app`, `jetson.motion_planner`, and `jetson.robot_comm` absent from the
  loaded module set.

The preflight uses read-only software-identity commands. It never imports a
robot device module, checks a serial device, or opens UART. Each individual run
must repeat the same Git commit, branch, cleanliness and motion facts captured
by the session preflight.

### Pilot matrix

The plan is written atomically before the resource sampler or first condition
starts. For every predeclared service duration and repetition, the
responsiveness block contains R0, R1, R2 and R3. R4-stale and R4-overflow run
once per repetition at a separate correctness duration. Their timing remains
excluded from the responsiveness and overhead comparison.

The first descriptive pilot uses explicit simulated durations selected from
the Phase 0 workload scale. The command records the exact values; no duration,
condition or repetition is added after looking at the results. Formal condition
order, thresholds and sample size remain unfrozen until this pilot is reviewed.

### Continuous resource trace

One non-daemon `tegrastats` reader spans the complete session. Starting and
stopping a separate resource process for every condition would add a systematic
boundary disturbance and break resource continuity, so the pilot does not do
that. The reader must produce a parseable first sample before R0 begins and
must terminate or be killed and joined within finite time after the final run.

Resource schema `0.1.0` is separate from runtime-event schema `0.2.0`. Each
JSONL sample has its own sequence, monotonic timestamp and wall timestamp.
Required parsed observations are RAM, swap, per-core CPU state and frequency,
GR3D usage, at least one temperature and at least one power rail. EMC and the
`tegrastats` wall-clock prefix are recorded when available but are not assumed
to exist on every supported format. Sensor and power-rail names are discovered
from the line rather than fixed to one board revision. The bounded raw line is
retained so a parser decision can be audited. Session validation reparses every
raw line and requires the reconstructed object to match the recorded fields.

Condition-level power descriptions use the instantaneous rail values. The
second value reported by `tegrastats` is retained as
`power_reported_average_mw` for diagnostics, but its averaging window may span
the continuous session and is not treated as an independent per-condition
mean.

Resource samples are associated with a condition only when their monotonic
timestamps fall within that run's recorded start and finish interval. Cross-run
absolute monotonic values are never compared as durations. A run with no
resource sample inside its interval fails the pilot Gate.

### Session artifacts and completion

One session contains:

```text
<session_id>/
├── session_manifest.json
├── pilot_plan.json
├── preflight.json
├── resources.jsonl
├── pilot_summary.json
└── <condition>/<run_id>/
    ├── manifest.json
    ├── scenario.json
    ├── events.jsonl
    └── summary.json
```

The session manifest advances from `running` to `completed` only after:

1. every predeclared run passes the existing run-directory validator;
2. run paths, conditions, service times and Git identities match the plan and
   preflight;
3. the resource sequence closes with zero unexplained parse errors;
4. the sampler process and reader thread stop with no leak;
5. every run has resource coverage and zero stale result consumption;
6. top-level artifacts and every child manifest have recorded SHA-256 values;
7. an independent validator reconstructs the complete pilot summary.

A failed sampler, missing or unexpected artifact, path escape, dirty or
different source identity, failed child Gate, uncovered run or summary mismatch
leaves the session failed.
The summary is permanently marked `descriptive_only=true` and
`inference_claim_permitted=false`.

### First pilot outcome

Session `20260828T121142Z_phase1_jetson_pilot` completed the 14-run matrix on
`main@77138f2` with all seven session Gates passing. The inline condition
produced service-time-scale probe gaps and skipped releases, while the threaded
direct and bounded-runtime conditions recorded no skipped releases. R4 rejected
one old-state result, bounded pending and result depth at one, and consumed zero
stale results.

That result is specific to the simulated adapter, whose service delay releases
the interpreter. It demonstrates the intended ownership and evidence path but
does not prove that a Python thread isolates real adapters that hold the GIL.

The public
[derived report](../../experiments/phase1/results/20260828T121142Z_phase1_jetson_pilot/)
also records the limits discovered by the pilot. It contains one fixed-order
repetition, all resource rows report `emc_missing`, the unprivileged
`jetson_clocks --show` snapshot is unavailable, and sustained unattributed CPU
activity crosses condition boundaries. No resource difference is therefore
assigned causally to R0--R4, and no sample is excluded post hoc.

## Fixed-input VLM evidence contract

Phase 1C reuses the exact Phase 0 C100 JPEG identity (320 × 193, 9009 bytes,
SHA-256 `607c9faf3ea03b8b032d8c1d9e86c697d9fb48ca3c2f278e453941da6b871be7`).
The adapter invokes the existing Moondream description, per-request Moondream
unload, Qwen rewrite with Argos fallback and speech-oriented normalization
functions in that order. Cleanup still requests unload when an earlier stage
fails. Those dependencies are imported only inside the explicit worker
call; importing the Phase 1 runtime or experiment modules must not start a
model, service request, camera or device.

The first real-workload protocol contains one request per run:

| Condition | Injection | Required terminal result |
| --- | --- | --- |
| `vlm_async` | no state change | `consumed` once |
| `vlm_stale` | generation advances after Moondream starts | `rejected_state` once |

Both conditions use pending and result capacities of one, an independent
100 ms probe, finite service and join budgets, and a continuous 200 ms
`tegrastats` trace. The stale condition intentionally does not attempt to
interrupt the Ollama request. It records whether the worker later observes the
cancellation token while leaving `backend_stop_confirmed` unknown. State
invalidation and backend preemption remain separate facts.

The input is hashed before admission, rechecked immediately before model work
and rechecked again after the pipeline finishes. A size or hash change makes
the result an execution error. Public scenario and summary artifacts contain
the input identity, output SHA-256 and character count, translation route,
stage durations, cancellation facts and lifecycle decisions. They do not
contain the file path, prompt, English description, Chinese output or captured
stdout/stderr.

Before creating a run directory, preflight requires the existing Jetson safety
and synchronized-`main` checks plus the fixed input, local-only Ollama and
llama.cpp endpoints, installed Moondream and Qwen model identities, and the
Ollama CLI plus OpenCV/Argos dependencies. Preflight schema `0.2.0` queries the
numeric TCP listeners with `ss`, records only their bound addresses and fails
if either service has no listener or any wildcard/non-loopback binding. The
validator retains read support for the first pilot's `0.1.0` record without
inventing listener evidence that was not archived. A completed manifest
additionally requires resource coverage during the adapter interval, valid
replay, exactly one terminal disposition, zero stale consumption, a returned
model-unload request, worker/probe/sampler joins, artifact hashes and an
independently rebuilt summary.

The host implementation and fault-injection tests do not count as a real-model
result. The first Jetson runs remain descriptive correctness evidence. They do
not measure visual accuracy, compare synchronous and asynchronous performance,
prove GPU preemption or authorize a heterogeneous-compute performance claim.

### First fixed-input VLM pilot outcome

Session `20260830T073825Z_phase1_vlm_pilot` ran one `vlm_async` request and one
`vlm_stale` request on `main@aebd1a2`. Both independently validate. The nominal
result was consumed exactly once; the old-generation result completed as
`cancel_observed`, was recorded once as `rejected_state`, and was never
consumed. Both used the Qwen rewrite route, kept raw model text out of the
artifacts and returned a Moondream unload request without claiming confirmed
eviction.

The 100 ms probe recorded 85 skipped releases and a 4262.876 ms maximum gap in
`vlm_async`, then 63 skipped releases and a 2700.375 ms maximum gap in
`vlm_stale`. Reconstruction assigns every skipped release to the lazy
`module_import` interval. The real-model pilot therefore passes its lifecycle
correctness Gates but does not pass a timing-isolation interpretation. A
process-level worker or an equivalent mitigation must be evaluated before the
formal protocol is frozen.

The source runs used VLM preflight schema `0.1.0`. The operator bound
llama.cpp to `127.0.0.1` and checked it before execution, but the archived
preflight records only the configured loopback request URL. The derived report
marks actual listener evidence incomplete. New runs use schema `0.2.0` and fail
closed on the observed bindings.

### Process-isolated VLM pilot outcome

Session `20260830T122541Z_phase1_vlm_process_reaping` ran the same nominal and
stale conditions on `main@1818c83`. Both source runs and their derived process
summaries validate independently. Each child completed the bounded protocol,
exited with code zero and was reaped without forced termination. Cancellation
was forwarded only for `vlm_stale`; its result was still rejected by the
parent's state-generation authority, with zero stale results consumed.

The process-isolated 100 ms probes recorded 0 skipped releases, 0 deadline
misses and maximum observed gaps of 100.647 ms and 100.272 ms. The published
thread reference recorded 148 skipped releases across its two runs. This is a
descriptive mitigation signal: the sessions each contain one fixed-order run
per condition and come from different commits. It therefore does not authorize
causal attribution, performance superiority or a general timing-isolation
claim. Process exit also remains distinct from confirmed model-backend
preemption.

### Fixed-input ASR correctness contract

The first Phase 1D ASR slice reuses the formal Phase 0 WAV identity (114136
bytes, SHA-256
`3fffeee1e04250faa483174a423878bf220b95f6706684f6e109ed8f9b731440`),
the `ggml-small.bin` identity (487601967 bytes, SHA-256
`1be3a9b2063867b937e64e2ec7483364a79917e157fa98c5d94b5c1fffea987b`),
whisper.cpp source version `v1.8.4-326-gafa2ea54` and the Phase 0 inference
arguments. The 30 measured Phase 0 ASR runs produced one stable transcript
identity and length. Phase 1 verifies those values but never serializes the
transcript, input path, model path or command line.

ASR uses its workload-specific FIFO lane with pending and result capacities of
two. The first correctness slice admits one request so nominal completion and
state invalidation remain isolated from later arrival-rate experiments. Its
conditions are `asr_async` and `asr_stale`. The nominal condition consumes one
matching transcript identity. The stale condition observes active Whisper for
0.5 s after its process starts, advances the state generation, requests
cancellation, stops and reaps the native `whisper-cli` process, and consumes no
result.

The 0.5 s observation window is an experimental correctness control, not a
formal performance threshold or a measurement of cancellation latency. It is
greater than the default 200 ms resource-sampling period so the stale execution
can contain at least one resource sample. The runner fails before creating a
run directory if the stale window does not exceed the configured resource
interval or if it reaches the adapter or slice completion timeout. This rule was
added after an invalid Jetson attempt cancelled Whisper in about 6.5 ms and
passed every lifecycle/process Gate while correctly failing resource coverage
with zero in-interval samples. That failed attempt is diagnostic only.

The cancellation claim differs from the VLM HTTP-service path. Whisper is the
backend process for this invocation; successful termination and reaping can set
`backend_stop_confirmed=true`. That fact does not imply GPU-wide preemption,
driver-level cancellation, or anything about a different process or service.
Timeout and cancellation termination, escalation to kill, exit code and reaping
remain separate recorded facts.

The ASR preflight fails closed unless the fixed input, model, source version and
arguments match, no pre-existing `whisper-cli` process is running, the Git tree
is synchronized, and motion/device-module checks pass. The implementation,
summary reconstruction and validator completed session
`20260831T140705Z_phase1_asr_pilot_v2` on synchronized `main@bc1ca35`. Both
conditions passed independent validation and every ASR Gate; the derived report
therefore satisfies the ASR component of G5. The later LLM correctness pilot
closes the remaining component and G5 overall. These controls and single-run
descriptive observations do not freeze formal numerical thresholds or measure
cancellation latency.

### Fixed-input LLM correctness contract

The first LLM slice reuses the tracked Phase 0 prompt identity (124 bytes,
SHA-256
`15ee277f4140cb3c2bca3d4762e6462e098787e5b5843245760d9f40da2ea7f2`)
and the Qwen GGUF identity (1117320736 bytes, SHA-256
`6a1a2eb6d15622bf3c96857206351ba97e1af16c30d7a74ee38970e434e9407e`).
It also freezes the served model identity
`qwen2.5-1.5b-instruct-q4_k_m.gguf`, an empty conversation-history snapshot,
the Phase 0 system-prompt identity, model alias `qwen`, temperature `0.4`,
`max_tokens=80` and non-streaming response mode. Prompt text, response text,
private paths and the raw HTTP response are never serialized.

LLM retains the contract of one active request and at most one pending request,
with reject-new overflow behavior. The correctness slice admits only one empty-
history request in each of `llm_async` and `llm_stale`. The nominal condition
consumes one response identity. The stale condition observes an active HTTP
request for 0.5 s, advances the conversation state generation and requires one
`rejected_state` disposition with zero consumption.

The Python worker performs a blocking HTTP request to a pre-existing local
llama-server. State invalidation can request cancellation and prevent result
consumption, but it does not interrupt that HTTP wait or prove that server-side
inference stopped. The adapter therefore records `client_wait_stopped=false`
and `backend_stop_confirmed=null` even in the stale condition. The resident
server is managed outside the run; the adapter neither unloads the model nor
stops the service. The 0.5 s observation window is a telemetry-coverage control,
not a cancellation-latency or performance threshold.

The fail-closed preflight verifies the prompt and model hashes, records a clean
llama.cpp source identity, requires exactly one server process with the frozen
Phase 0 launch arguments and model path, checks a loopback-only listener, and
confirms the expected served model identity. The implementation, summary
reconstruction and validator completed session
`20260901T143315Z_phase1_llm_pilot` on synchronized `main@6e83ede`. Both
conditions passed independent validation and all fourteen LLM Gates. The
nominal response identity was consumed once. The old-generation response in the
stale condition completed at the blocking HTTP boundary but was rejected before
consumption; the run records no client-wait or backend-stop confirmation. The
derived report therefore satisfies the LLM component and closes G5 overall.
The single fixed-order runs remain descriptive and do not freeze G6 thresholds,
sample size, order, exclusions or statistical methods.

## Gates

The following correctness thresholds are fixed before the pilot:

- zero stale or identity-mismatched results consumed;
- zero queue or result-mailbox capacity violations;
- zero duplicate final dispositions;
- zero live admitted tasks after successful shutdown;
- zero worker or child-process leaks;
- zero UART access and physical motion output;
- zero unexplained telemetry parse errors;
- zero Phase 0 artifact modifications.

The fixed-input VLM slice adds these zero-tolerance Gates:

- the C100 identity matches before and after the request;
- Moondream, one translation route, normalization and the unload call close;
- the artifact records an unload request without claiming confirmed eviction;
- raw model text and the private input path are absent from public artifacts;
- model-service TCP listeners are present and bound only to loopback addresses;
- `vlm_async` consumes exactly one result;
- `vlm_stale` records exactly one `rejected_state` and zero accepted results;
- cancellation evidence does not claim backend stop without confirmation;
- at least one valid resource sample falls inside the adapter interval.

The process-isolated variant additionally requires:

- the adapter uses `spawn` and records one positive child process identity;
- the bounded process protocol completes without an error;
- spawn, child start, inference start, completion and join boundaries are
  present and ordered;
- `vlm_stale` forwards cancellation after inference starts, while `vlm_async`
  forwards none;
- the child exits with code zero and is reaped without forced termination;
- process cleanup is not represented as confirmed model-backend cancellation.

The fixed-input ASR slice additionally requires:

- the Phase 0 WAV, model, source version and command arguments match;
- no pre-existing `whisper-cli` process is present at preflight;
- `asr_async` consumes exactly one matching transcript identity;
- `asr_stale` records exactly one `rejected_state` and zero accepted results;
- nominal Whisper exits with code zero and is reaped without termination;
- stale cancellation is observed, stops the local wait, terminates and reaps
  Whisper, and records backend-stop confirmation only for that child;
- the stale adapter remains active for the recorded observation window, which
  must exceed the configured resource-sampling interval;
- raw transcript text and private filesystem paths are absent from artifacts;
- at least one valid resource sample falls inside the adapter interval.

The fixed-input LLM slice additionally requires:

- the fixed prompt, empty history, Qwen model, served model identity, system
  prompt identity, request fields and llama-server launch arguments match;
- exactly one pre-existing llama-server is present and bound only to loopback;
- `llm_async` consumes exactly one non-empty response identity;
- `llm_stale` records exactly one `rejected_state` and zero accepted results;
- prompt, response, history and private path text are absent from artifacts;
- llama.cpp reports valid prompt, completion and total token counts;
- state invalidation is observed without claiming that the HTTP wait or backend
  inference was stopped;
- the externally managed server remains resident and receives no unload or stop
  request from the slice;
- the stale adapter covers its observation control and at least one valid
  resource sample falls inside each adapter interval.

G5 requires independently validated Jetson correctness pilots for VLM, ASR and
LLM. Those three components are now satisfied. The G6 preregistration freezes
numerical thresholds, balanced order, sample size, exclusions and statistical
analysis before formal data collection.

The G6 protocol fixes the asynchronous p95 maximum-gap bound at 300 ms and the
workload-performance noninferiority ratio at `1.10`. The 300 ms threshold is the
Phase 2 unchanged-command refresh reference; the STM32 1.2 s watchdog is a last
defense and must not be used as the Jetson scheduling success target.

## G6 formal preregistration

The G6 v4 formal comparison is preserved by the amended
[G6 preregistration](phase1-formal-preregistration.md) and its machine-readable
v4 protocol. V1, v2 and v3 remain immutable history; v2 and v3 are closed after
system-under-test failures. V4 becomes active only through its reviewed merge to
`main`; data collected before that event are not eligible for confirmatory
analysis, and no v3 run can be reused or reclassified.

The design contains five sessions and six paired sync/async blocks per workload
and session: 30 pairs per workload, 30 measured runs per condition and workload,
and 180 measured runs overall. Every session uses each of the six workload
orders once. Sync-first and async-first pair order each occur three times per
workload in every session and 15 times overall. Warm-ups and two 30 s idle
references per session are retained but excluded by their predeclared roles.
The retained v2 matrix additionally balances pair order two/three within every
workload/block across sessions, five/five within every workload/position, and
five/five or seven/eight within every measured preceding-workload context. Each
session/block contains both pair orders.

The primary responsiveness endpoint requires the asynchronous nearest-rank p95
of per-run maximum probe gaps to remain at or below 300 ms and the upper 95%
confidence bound for paired `async - sync` mean difference to remain below
zero. Workload noninferiority uses an upper `1.10` bound for the geometric mean
of within-pair async/sync ratios: total adapter time for ASR and VLM, and request
milliseconds per completion token for LLM. Lifecycle success requires every run
Gate to pass with zero stale consumption, capacity violations, process leaks or
unjoined threads.

Paired hierarchical percentile bootstrap intervals resample five sessions and
then six paired blocks within each selected session, using 100,000 resamples,
95% confidence and seed `20260902`. The decision is intersection-union across
all endpoints and workloads. No post-hoc outlier exclusion, imputation,
failed-run replacement or operator-selected reordering is permitted. The
tracked protocol SHA-256 is
`84da36aa9b4a804ecc5692b12902321e42254f707463d1a5937e7049ffa0d054`.

The formal runner executes one complete protocol session per invocation. It
loads the tracked protocol by hash, requires synchronized clean `main`, records
the protocol and runner commits, and checks the exact Python, JetPack, L4T,
power-mode, model, executable and service contracts. Session start and
measurement start each require ten consecutive Tj samples no greater than 55 C.
The continuous sampler requests a stop at 85 C, and every exception closes the
sampler, probe, worker and child-process evidence before marking the attempt
aborted. An append-only ledger fixes all warm-up, idle and measured transitions.

`formal_sync` invokes the same adapter on the calling control flow while an
inline absolute-schedule probe exposes the blocking interval. `formal_async`
uses a one-pending/one-result Phase 1 lane and an independent probe. ASR keeps
its supervised Whisper subprocess in both conditions; LLM keeps the same
pre-existing llama-server; VLM keeps the spawned-process adapter, deterministic
request contract `0.1.0`, 60 s Qwen client boundary, confirmed
Moondream-unload-before-Qwen policy and process protocol `0.2.0` in both
conditions. Unload confirmation polls the Ollama process list every 100 ms for
at most 20 s and fails closed. Thus the intended
condition difference is the Phase 1 scheduling boundary rather than a model,
request or residency change. Each run binds the adapter record to a separate
privacy-preserving result envelope and enforces workload-specific output,
request, process and residency Gates.

The first commissioning collection,
`20260905T062312Z_phase1_formal_g6`, completed the three ASR warm-ups and then
stopped before the first LLM request, either idle epoch or any measured run. The
formal task builder supplied the SHA-256 of an empty byte string rather than the
frozen empty JSON history identity required by the LLM adapter. This is a runner
integration defect, not a system-under-test outcome or a preregistered
infrastructure replacement. The aborted attempt remains diagnostic. The
correction binds task metadata to the existing adapter constant and adds
sync/async integration tests; it changes no protocol identity, hypothesis,
schedule, threshold or analysis method. The measured collection restarts under
a new collection identifier after the correction is reviewed on `main`.

The second commissioning collection,
`20260905T065922Z_phase1_formal_g6`, completed session 1's five warm-ups, both
idle epochs and 36 measured invocations. The required pre-continuation integrity
check ran before any endpoint review and found that the final resource sample
preceded the post-measurement idle finish boundary by 170.607 ms, within the
frozen 200 ms sampling period. The runner stopped tegrastats immediately after
the idle probe returned, so this scheduler race could leave the trace short
while marking the manifest complete. The collection is retained unchanged, is
not continued or analyzed, and is not formal evidence. The correction waits for
a resource sample at or after the final activity boundary before shutdown and
treats failure to obtain it as a resource-sampler failure. It changes no
protocol identity, hypothesis, schedule, threshold, exclusion or analysis
method. Admissible collection restarts from session 1 under a new identifier
after the correction is reviewed on `main`.

Before restarting collection, an outcome-independent audit of the serialized
v1 schedule found that both six-block cycles reset at every session. Its
marginal three/three condition balance therefore hid a repeated relationship
between pair order and the preceding workload. No timing, resource,
model-output or endpoint value was used in the audit or replacement schedule.
The exact v1 protocol remains tracked under its original ID and hash. G6 v2
supersedes it with a fixed cross-balanced condition-order matrix and excludes
all v1 commissioning data. The hypotheses, sample size, workloads, inputs,
conditions, environment, thresholds, analysis, exclusions and stopping rules
remain unchanged.

The first v2 collection,
`20260905T140816Z_phase1_formal_g6_v2`, passed the frozen preflight and completed
five warm-ups, the pre-measurement idle reference and 12 measured runs. Measured
ordinal 18 then reached the VLM Qwen rewrite's 30 s client timeout, used the
Argos fallback and failed `translation_route_verified`. Independent
reconstruction verified all 42 manifest artifacts, the 37-record ledger prefix,
18 run records, 3,558 resource samples and 179 passed Gates plus the single
failure. The child process exited normally, the llama-server task was cancelled
at the timeout boundary and its slot returned to idle. Session Tj peaked at
55.093 C and no sampler or thermal failure occurred. The public
[failed-attempt report](../../experiments/phase1/results/20260905T140816Z_phase1_formal_g6_v2/)
contains derived identities and diagnostics only.

The v2 stage order placed its Moondream unload request after Qwen and the Argos
fallback. This is a residency-order confound but does not establish the timeout's
cause. The isolated implementation correction moves unload between Moondream
inference and Qwen, retains cleanup on earlier failure and leaves the 30 s Qwen
timeout unchanged.

The separate diagnostic `20260905T160805Z_phase1_vlm_residency_diag` executed
one process-isolated run per lifecycle condition. Both used Qwen after the
unload request, completed the slice/process Gate sets and exited normally. The
Qwen stages took 18400.091 ms and 18864.649 ms, and the bound llama-server log
contained two completed requests with no cancellation record. Its
[derived report](../../experiments/phase1/results/20260905T160805Z_phase1_vlm_residency_diag/)
binds the collection and log archive hashes without publishing raw evidence.
The fixed order and single run per condition prohibit causal or performance
claims, but support retaining the 30 s boundary for v3.

The first v3 collection, `20260906T055511Z_phase1_formal_g6_v3`, passed the
frozen preflight and completed five warm-ups, the pre-measurement idle reference
and four measured runs. Measured ordinal 10 then reached the synchronous VLM
Qwen rewrite's 30 s client timeout, used the Argos fallback and failed
`translation_route_verified` and `residency_contract_verified`. Independent
reconstruction verified 26 manifest artifacts, the 21-record ledger prefix, 10
run records, 1,724 resource samples, 97 passed Gates and two failed Gates. The
VLM child exited normally under process protocol `0.2.0`; all five llama-server
requests completed and released their slots, the server returned to idle, and
no thermal, sampler or service failure occurred. The public
[v3 failed-attempt report](../../experiments/phase1/results/20260906T055511Z_phase1_formal_g6_v3/)
contains only hash-bound derived evidence.

The failed server request completed in 30117.120 ms, 117.120 ms beyond the
configured client boundary, with no cancellation record. It used 10 more prompt
tokens and generated 5 more tokens than the VLM warm-up request, but these two
observations do not establish a causal explanation. The unload request returned
before Qwen; actual Ollama unload completion remains unobservable. The separate
`multiprocessing.resource_tracker` semaphore warning seen only on the operator
console is secondary, is absent from the bound archives, did not fail a Gate and
does not contradict the recorded child-process closure.

The independent analyzer does not trust runner summaries. It verifies the
protocol copy and every artifact hash, reconstructs the session ledger and all
90 pairs, checks event boundaries, idle duration, thermal/resource coverage,
result consistency and lifecycle closure, calculates the three preregistered
performance metrics, then applies one shared seeded session/block resampling
stream for 100,000 percentile-bootstrap draws. Any missing run, reordered entry,
mixed commit, un-restarted service, incomplete thermal gate, lifecycle failure
or modified artifact invalidates the collection.
The v3 identity refuses any further session. The failed measured run is not
replaced, the collection is not continued, and its partial timing data do not
enter confirmatory analysis. The incomplete matrix supports no sync/async
performance conclusion and did not itself imply an automatic v4 retry. G6 was
not met and the application slice was not authorized.

The subsequent descriptive timeout-repair diagnostic repeated the fixed input
three times with temperature `0.0`, seed `20260906`, the existing model and
output-token limits, explicit Ollama process-list polling after unload and a
60 s Qwen client boundary. Qwen completed in 21753.498, 10883.012 and
10203.343 ms with identical 164-token prompts and 32-token completions. All
three llama-server tasks released normally, no cancellation was recorded, all
739 tegrastats samples parsed, and maximum Tj was 54.062 C. Its
[derived report](../../experiments/phase1/results/20260906T082627Z_phase1_vlm_timeout_diag/)
contains no raw model text, logs, telemetry or private paths.

The diagnostic reproduced the candidate contract in an inline harness rather
than executing the modified repository adapter. It supports the repair design
but did not itself validate the repository path. The subsequent
[target validation](../../experiments/phase1/results/20260906T101723Z_phase1_vlm_timeout_repair_validation/)
applied the exact repair source bundle to a clean Jetson checkout and directly
ran `run_vlm_slice` for the nominal and stale conditions. Both independent
validators returned `VALID`; all slice/process Gates passed, both Moondream
unloads were confirmed, both rewrites used Qwen, and the service log contained
two matching request releases with no cancellation, timeout or error record.
The Qwen stages completed in 23704.782 and 26854.584 ms. This is nonformal
correctness evidence from one fixed-order run per condition, not a performance
comparison. V3 remains closed and is not reclassified. G6 v4 is the explicit
successor protocol: it retains the complete scientific design while freezing
only the validated deterministic request, 60 s Qwen timeout, bounded positive
unload confirmation and amendment provenance. Its reviewed merge activates a
fresh collection from session 1. Phase 1 remains incomplete.

## Phase 1 completion boundary

The original completion boundary required:

1. host-only concurrency and lifecycle replay pass;
2. safe Jetson simulation passes with valid telemetry;
3. real VLM, ASR, and LLM slices pass their Gates;
4. a pre-registered synchronous/asynchronous comparison is completed;
5. results and limitations are published without hard-real-time claims;
6. at least one opt-in, motion-disabled application slice uses the validated
   runtime while the synchronous baseline remains available.

Items 4 and 6 remain unsatisfied. The non-replaceable G6 v3 failure closed that
protocol and blocked its application slice. The VLM repair path is validated on
the target, and G6 v4 now preregisters a fresh comparison after reviewed merge to
`main`. The corrective result alone does not complete Phase 1: v4 must complete
and pass before the motion-disabled application slice is authorized.
