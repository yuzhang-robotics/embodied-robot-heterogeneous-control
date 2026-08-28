# Phase 1 Runtime Contract

This document defines the correctness and safety contract for the first
asynchronous runtime used by the Octopus robot research platform. The
host-only model, broker, observable executor, periodic probes, trace replay and
portable simulation protocol have been implemented. Jetson behavior and
performance remain unvalidated.

> 中文简介：本文冻结 Phase 1 异步运行时的任务模型、生命周期、队列、取消、结果新鲜度、
> 快速周期代理和安全边界。host-only worker、周期探针、trace replay 和模拟实验运行器已实现；
> Jetson pilot、真实模型接入和正式实验仍需按 Gate 逐步完成。

## Status

- Phase: Phase 1.4 Jetson simulation-pilot protocol
- Contract status: frozen and host-tested through the pilot infrastructure
- Jetson-pilot starting point: `main@844b633`
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
  an immutable pilot matrix and independent session reconstruction.

It does not include a completed Jetson simulation pilot, real workload adapters
or formal performance data.

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

- pending capacity: 1;
- coalesce related pending tasks by `supersession_key`;
- replacement emits a dropped disposition for the replaced pending task;
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
path advances independently. Both invoke the same scenario adapter contract.

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
retains module-import, Moondream, rewrite/fallback, and unload stages.

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

Numerical jitter and non-inferiority thresholds are frozen after an independent
pilot and before formal data. The current 300 ms unchanged-command refresh
target is a Phase 2 readiness reference; the STM32 1.2 s watchdog is a last
defense and must not be used as the Jetson scheduling success target.

## Phase 1 completion boundary

Phase 1 is complete only when:

1. host-only concurrency and lifecycle replay pass;
2. safe Jetson simulation passes with valid telemetry;
3. real VLM, ASR, and LLM slices pass their Gates;
4. a pre-registered synchronous/asynchronous comparison is completed;
5. results and limitations are published without hard-real-time claims;
6. at least one opt-in, motion-disabled application slice uses the validated
   runtime while the synchronous baseline remains available.
