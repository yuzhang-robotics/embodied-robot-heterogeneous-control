# Phase 1 Runtime Contract

This document defines the correctness and safety contract for the first
asynchronous runtime used by the Octopus robot research platform. The
host-only model, broker, observable executor, periodic probes, trace replay and
portable simulation protocol have been implemented. One motion-disabled Jetson
simulation pilot has validated the protocol and runtime semantics. A
fixed-input VLM correctness pilot has also completed nominal consumption and
old-generation rejection on the Jetson. A spawned-process VLM adapter and its
evidence Gates have since completed one process-isolated Jetson correctness
pilot. Formal performance behavior remains unvalidated.

> 中文简介：本文冻结 Phase 1 异步运行时的任务模型、生命周期、队列、取消、结果新鲜度、
> 快速周期代理和安全边界。host-only worker、周期探针、trace replay 和模拟实验运行器已实现；
> Jetson simulation pilot 与固定输入 VLM correctness pilot 已完成并通过独立验证；
> VLM 进程隔离路径已完成 Jetson correctness pilot；正式同步/异步对比实验仍需按 Gate 逐步完成。

## Status

- Phase: Phase 1C process-isolated VLM pilot analysis
- Contract status: frozen through independently validated Jetson simulation,
  thread-mode VLM and process-isolated VLM correctness pilots
- VLM-pilot result: `main@aebd1a2`, session
  `20260830T073825Z_phase1_vlm_pilot`
- VLM-pilot public analysis: `main@95a839d`
- Process-isolated VLM result: `main@1818c83`, session
  `20260830T122541Z_phase1_vlm_process_reaping`
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

The VLM pilots include no formal performance data. They validate the real-model
integration, result-freshness and process-boundary paths. The thread pilot's
skipped releases show that the simulated sleep result cannot be generalized to
every Python worker workload; the later process pilot removes those observed
gaps in two single-run conditions. No completed pilot authorizes an
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
The adapter invokes the existing Moondream description, Qwen rewrite with
Argos fallback, speech-oriented normalization and per-request Moondream unload
functions. Those dependencies are imported only inside the explicit worker
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

Numerical jitter and non-inferiority thresholds will be frozen during the next
protocol review and before formal data. The current 300 ms unchanged-command
refresh target is a Phase 2 readiness reference; the STM32 1.2 s watchdog is a
last defense and must not be used as the Jetson scheduling success target.

## Phase 1 completion boundary

Phase 1 is complete only when:

1. host-only concurrency and lifecycle replay pass;
2. safe Jetson simulation passes with valid telemetry;
3. real VLM, ASR, and LLM slices pass their Gates;
4. a pre-registered synchronous/asynchronous comparison is completed;
5. results and limitations are published without hard-real-time claims;
6. at least one opt-in, motion-disabled application slice uses the validated
   runtime while the synchronous baseline remains available.
