# Phase 1 G6 Formal Preregistration

This document records the preregistered fixed-input synchronous/asynchronous
comparison for the Phase 1 runtime under the amended G6 v3 protocol. V2 remains
closed after its first formal attempt stopped on a system-under-test failure.
V3 retains the complete v2 scientific design and freezes the corrected VLM
residency order after a separate descriptive diagnostic. Its first formal
attempt also stopped on a system-under-test failure and permanently closes the
protocol without a confirmatory comparison.

The machine-readable protocol is
[`phase1-g6-v3-preregistration.json`](../../experiments/phase1/formal/phase1-g6-v3-preregistration.json).
It is generated and validated by
[`formal_protocol.py`](../../experiments/phase1/formal_protocol.py). The tracked
protocol uses schema `0.2.0`, protocol ID
`phase1-g6-fixed-input-sync-async-v3`, and SHA-256
`070ec2d571c957a413567a2d2bd92d3dddd2e9fb07a7b1ef8c0c0c89bcdcfc4b`.

> 中文简介：本文记录 Phase 1 固定输入同步/异步正式对照的 G6 v3 修订协议。v2 因首次
> 正式尝试出现系统被测对象失败而永久关闭；独立的描述性诊断随后验证了修正后的 VLM
> 驻留顺序。v3 保留 v2 的五个 session、交叉平衡条件顺序、样本量、成功阈值、失败处理
> 和分层配对 bootstrap 方法，仅冻结 `Moondream -> 卸载请求 -> Qwen` 顺序及进程协议。
> 首次 v3 正式尝试又因 VLM Qwen 30 秒超时触发两个系统被测对象 Gate 失败；v3 与
> Phase 1 均以负结果关闭，不重跑、不替换、不进行正式性能比较，也不进入整机应用切片。

## Protocol amendment history

G6 v1, protocol ID `phase1-g6-fixed-input-sync-async-v1` and SHA-256
`022df6af4bb3236a28b2e47f0edb9afbc6078131441a1c1f9e8730920c660761`,
was activated before the formal runner was commissioned. Its exact JSON remains
tracked as
[`phase1-g6-preregistration.json`](../../experiments/phase1/formal/phase1-g6-preregistration.json).
No collection under v1 is eligible for confirmatory analysis.

An outcome-independent audit of the serialized v1 schedule found that its
workload-order cycle and shared condition-order cycle both reset at every
session. Although every workload had three sync-first and three async-first
pairs per session, the same workload/condition relationship repeated across all
five sessions. For example, an LLM pair following ASR was always sync-first,
while an LLM pair following VLM was always async-first. This unnecessary
coupling was identified before admissible collection. No timing, resource,
model-output or endpoint value was used to construct the replacement schedule.

G6 v2 changes only the frozen condition-order matrix and adds explicit amendment
metadata. It preserves the research questions, hypotheses, sample size,
workloads, inputs, conditions, environment, safety rules, endpoints, thresholds,
bootstrap method, exclusions, missing-data rules and stopping rules. All v1
commissioning artifacts remain diagnostic and are excluded from v2.

The first v2 collection,
`20260905T140816Z_phase1_formal_g6_v2`, passed preflight and completed all five
warm-ups, the pre-measurement idle reference and 12 measured runs. Measured
ordinal 18 then failed `translation_route_verified`: the VLM Qwen rewrite
reached its 30 s client timeout and the adapter used Argos. The child process
exited normally, llama-server returned its slot to idle, resource telemetry
remained valid and no thermal stop occurred. The deterministic
[failed-attempt report](../../experiments/phase1/results/20260905T140816Z_phase1_formal_g6_v2/)
preserves the archive identities and derived evidence without publishing raw
model text, paths or logs.

The v2 implementation requested Moondream unload after the Qwen attempt, so the
failure contains a model-residency-order confound but does not establish that
residency caused the timeout. Under the frozen failure rules, a system-under-
test failure cannot be replaced. The collection is not continued, the attempt
is not rerun, and no v2 timing result enters confirmatory analysis. The isolated
correction moves the unload request before Qwen while keeping the 30 s timeout
unchanged for a separate descriptive Jetson diagnostic. Any later formal
collection requires a newly preregistered protocol version and restarts from
session 1.

The separate diagnostic,
`20260905T160805Z_phase1_vlm_residency_diag`, executed one process-isolated
`vlm_async` run and one `vlm_stale` run from commit `08e262d`. Both independently
validated, used the Qwen route, requested Moondream unload before Qwen, completed
the process protocol and exited normally. Qwen took 18400.091 ms and 18864.649
ms, both inside the retained 30 s request boundary; the bound llama-server log
contains two completed requests and no cancellation record. The
[residency-order report](../../experiments/phase1/results/20260905T160805Z_phase1_vlm_residency_diag/)
publishes only derived evidence and binds both transferred archive hashes.

This single fixed-order run per lifecycle condition establishes implementation
readiness, not residency causality or performance superiority. No diagnostic
outcome was used to alter the v2 schedule, hypotheses, sample size, endpoints,
thresholds or analysis. G6 v3 changes only the VLM residency-order contract,
binds spawned-process protocol `0.2.0`, records amendment provenance and
restarts formal collection from session 1. V2 remains immutable and cannot be
reopened, rerun, replaced or reclassified.

The first v3 collection, `20260906T055511Z_phase1_formal_g6_v3`, passed the
frozen preflight and completed five warm-ups, the pre-measurement idle reference
and four measured runs. Measured ordinal 10 then reached the synchronous VLM
Qwen rewrite's 30 s client timeout, used the Argos fallback and failed
`translation_route_verified` and `residency_contract_verified`. Independent
reconstruction verified 26 manifest artifacts, 10 run records, 1,724 resource
samples, 97 passed Gates and the two failures. The child process exited normally,
all five llama-server requests released their slots, the server returned to idle,
and no thermal, sampler or model-service failure was observed. The
[v3 failed-attempt report](../../experiments/phase1/results/20260906T055511Z_phase1_formal_g6_v3/)
publishes only hash-bound derived evidence.

The server completed the failed request in 30117.120 ms, 117.120 ms beyond the
client boundary, without a cancellation record. The warm-up and failed requests
used 161/32 and 171/37 prompt/generated tokens respectively, but these two
observations do not establish a timeout cause. The unload request returned before
Qwen, while actual Ollama unload completion remains unobservable. Neither prompt
length nor residency is therefore assigned as causal. A separate console-only
`multiprocessing.resource_tracker` semaphore warning appeared during runner
shutdown; it is not present in the hash-bound collection or service log, did not
fail a Gate, and does not override the recorded normal child-process closure.

Under the frozen rules this system-under-test failure is not replaceable. V3 is
closed, no later session is collected, the partial timings do not enter
confirmatory analysis, and no v4 is implied as an automatic retry. The G6 success
criterion is not met. Phase 1 closes with a negative result and its application
slice is not authorized.

## Research questions and hypotheses

The comparison addresses two confirmatory questions while retaining the
already validated lifecycle invariants:

1. Does moving the same slow ASR, LLM or VLM operation behind the bounded Phase
   1 execution boundary reduce disruption of a 100 ms periodic task?
2. Does that boundary preserve fixed-input workload performance within a 10%
   noninferiority margin?

The corresponding hypotheses are falsifiable:

- **Responsiveness:** the asynchronous condition must improve the paired
  periodic-probe maximum-gap metric and remain within the absolute 300 ms
  practical bound for every workload.
- **Workload noninferiority:** the upper confidence bound of the paired
  asynchronous/synchronous performance ratio must not exceed `1.10` for any
  workload.
- **Lifecycle:** every measured run must preserve capacity, freshness and
  cleanup invariants, including zero stale consumption.

All three hypothesis families must pass for all three workloads. A partial pass
does not authorize the overall Phase 1 formal claim.

## Scope and claim boundary

The study uses one Jetson Orin Nano Super 8GB, fixed Phase 0 inputs and the
reviewed Phase 1 workload adapters. It is a controlled systems comparison, not
a model-quality evaluation or a population-wide hardware claim.

Physical motion and UART access remain prohibited. The study does not compare
CPU affinity, real-time priority, clock locking, model upgrades, alternative
residency policies or simultaneous slow-model inference. Device-wide resource
measurements remain descriptive and are not attributed to a specific process,
processor or runtime component. The protocol does not permit hard-real-time or
heterogeneous-inference claims.

## Frozen environment

| Field | Frozen value |
| --- | --- |
| Device | Jetson Orin Nano Super 8GB; aarch64 |
| Software baseline | Python 3.10.12; JetPack 6.2.2+b24; nvidia-l4t-core 36.5.0-20260115194252 |
| Power mode | `MAXN_SUPER`, mode `2` |
| Clock policy | Dynamic DVFS; `jetson_clocks` is not enabled |
| Probe period/deadline | 100 ms / 100 ms |
| Resource sampling | Continuous `tegrastats`, 200 ms interval |
| Session start | Tj no greater than 55 C for 10 consecutive samples |
| Measurement start | Last 10 samples of the pre-measurement idle epoch no greater than 55 C |
| Thermal stop | Tj at or above 85 C |
| Session separation | At least 30 minutes, with model services restarted |
| Slow-workload concurrency | Exactly one slow workload at a time |
| Motion | `ROBOT_ENABLE_MOTION=0` |

Every run records the protocol commit, runner commit, environment, input,
model, service and argument identities. A dirty tree, mixed identity or
unexpected inference process stops collection.

## Frozen workload identities

### ASR

- Phase 0 WAV: 114136 bytes, SHA-256
  `3fffeee1e04250faa483174a423878bf220b95f6706684f6e109ed8f9b731440`.
- Whisper model: 487601967 bytes, SHA-256
  `1be3a9b2063867b937e64e2ec7483364a79917e157fa98c5d94b5c1fffea987b`.
- whisper.cpp source: `v1.8.4-326-gafa2ea54`.
- The reviewed command arguments and per-invocation model residency remain
  unchanged.

### LLM

- Phase 0 prompt: 124 bytes, SHA-256
  `15ee277f4140cb3c2bca3d4762e6462e098787e5b5843245760d9f40da2ea7f2`.
- Qwen GGUF: 1117320736 bytes, SHA-256
  `6a1a2eb6d15622bf3c96857206351ba97e1af16c30d7a74ee38970e434e9407e`.
- llama.cpp source: `b9246-2-g585080d31`.
- The served model, loopback llama-server arguments, empty history and chat
  request contract remain unchanged.

### VLM

- Phase 0 C100 image: 9009 bytes, SHA-256
  `607c9faf3ea03b8b032d8c1d9e86c697d9fb48ca3c2f278e453941da6b871be7`.
- Moondream digest:
  `55fc3abd386771e5b5d1bbcc732f3c3f4df6e9f9f08f1131f9cc27ba2d1eec5b`.
- Ollama version `0.24.0`; executable SHA-256
  `6273a99e321b5e69741aa024cc22e0ce2803aa2bdf20185ea19627b4d891c87a`.
  The executable path is not recorded.
- The three Moondream prompt identities, temperature `0.1`, `num_predict=100`,
  non-streaming mode and 180 s timeout are fixed without serializing prompt
  text.
- The Qwen rewrite uses the same GGUF and llama.cpp source as the LLM workload.
  Its system-prompt and user-prefix identities, model alias `qwen`, temperature
  `0.2`, `max_tokens=96`, non-streaming mode and 30 s timeout are fixed.
- The successful stage order is fixed as input verification, module import,
  Moondream inference, Moondream unload request, Qwen rewrite, output
  normalization and final input verification. Cleanup also requests unload if
  an earlier stage fails; independent unload confirmation is unavailable.
- Both conditions bind spawned-process protocol `0.2.0`. The Qwen route and
  30 s request timeout are unchanged from v2.
- Both formal conditions use the spawned-process VLM execution path. The only
  intended difference is whether the calling control flow waits synchronously
  or delegates the same operation to the Phase 1 worker.

Raw audio, prompt, image, model response and private filesystem paths remain
outside tracked artifacts.

## Conditions

| Condition | Slow workload | Probe | Runtime broker | Interpretation |
| --- | --- | --- | --- | --- |
| `formal_idle` | none | independent | no | session-local descriptive reference |
| `formal_sync` | calling thread | inline on the same thread | no | blocking control-flow reference |
| `formal_async` | Phase 1 single worker | independent | yes | bounded asynchronous condition |

The sync and async conditions invoke the same workload-specific adapter and
residency policy. In particular, VLM process isolation is held constant so the
comparison does not confound scheduling with Python module-import placement.

## Sample size and fixed order

The confirmatory dataset contains five complete sessions. Each session has six
paired blocks per workload, producing:

- 30 paired blocks per workload;
- 30 measured runs per workload and condition;
- 36 measured runs per session;
- 180 measured runs overall.

Each session retains but excludes three ASR, one LLM and one VLM warm-up from
the confirmatory analysis. A 30 s `formal_idle` epoch occurs before and after
the measured matrix, giving ten descriptive idle epochs overall. Every measured
run has a 1 s prelude and 1 s postlude.

The order is generated before collection and serialized in the tracked JSON:

- every session uses each of the six possible workload orders exactly once;
- each workload uses sync-first and async-first three times per session and 15
  times overall;
- within every workload/block combination, the five sessions split pair order
  two/three or three/two;
- within every workload/position combination, the two pair orders split five/five;
- immediate preceding-workload contexts split five/five when they occur ten
  times and seven/eight when they occur fifteen times;
- every session/block contains one or two async-first workloads, so no block
  gives all three workloads the same pair order;
- no runtime randomization or operator-selected reordering is permitted.

The frozen matrix below uses `0` for sync-first and `1` for async-first. Digits
correspond to blocks 1 through 6.

| Session | ASR | LLM | VLM |
| --- | --- | --- | --- |
| 1 | `100110` | `100101` | `011001` |
| 2 | `101001` | `011010` | `010110` |
| 3 | `010101` | `101001` | `101010` |
| 4 | `011010` | `010110` | `100101` |
| 5 | `001101` | `100011` | `011010` |

The preceding-workload check follows the measured pair sequence across block
boundaries and excludes the first pair of each session, which has no measured
predecessor.

The experimental unit is the sync/async pair identified by session, block and
workload. The runner must refuse missing, duplicated or reordered entries.

## Confirmatory endpoints

### Responsiveness

The run-level metric is the periodic probe's maximum observed gap. For every
workload, both criteria must pass:

1. the nearest-rank p95 of `formal_async` run maxima is no greater than 300 ms;
2. the upper endpoint of the paired mean `async - sync` 95% confidence interval
   is below 0 ms.

The 300 ms bound is the existing Phase 2 unchanged-command refresh reference,
not the STM32 1.2 s watchdog limit. The formal study does not treat the watchdog
as a scheduling target.

### Workload noninferiority

For each pair, the positive-valued performance ratio is `formal_async /
formal_sync`. The confirmatory estimand is the geometric mean of the 30 pair
ratios. The upper endpoint of its 95% confidence interval must be no greater
than `1.10` for every workload.

| Workload | Confirmatory performance metric |
| --- | --- |
| ASR | adapter total time |
| LLM | request milliseconds per completion token |
| VLM | adapter total time |

LLM end-to-end time and token counts remain secondary because sampled response
length drives request duration. VLM stage durations and warm-state order remain
diagnostics rather than exclusion criteria.

### Lifecycle

Every measured run must pass its workload and lifecycle Gates. The complete
dataset must contain zero stale consumption, capacity violations, unreaped
processes and unjoined threads. Any failure prevents the overall confirmatory
claim even if the timing criteria pass.

## Statistical analysis

The analysis reports count, mean, median, sample standard deviation, CV,
nearest-rank p95, minimum and maximum for each condition. Paired effects are
reported as both `async - sync` and percentage change.

Confidence intervals use a paired hierarchical percentile bootstrap:

1. resample the five sessions with replacement;
2. resample the six paired blocks within each selected session;
3. retain sync/async pairing throughout;
4. use 100,000 resamples, 95% confidence and seed `20260902`.

The confirmatory decision is intersection-union: every preregistered criterion
must pass for every workload. No isolated significant result can pass the
overall decision, so no additional multiplicity correction is applied.
Resource summaries, idle epochs, order trends, probe lateness, deadline misses,
skipped releases, queue wait, result age and shutdown latency are secondary or
descriptive outputs.

## Exclusions, missing data and failures

- No post-hoc outlier exclusion or imputation is permitted.
- Warm-ups and idle epochs are excluded by their predeclared roles only.
- Every planned measured attempt remains in the completion denominator.
- A failed measured run is retained and is not rerun or replaced.
- A missing half of a pair invalidates the confirmatory analysis.
- Every session attempt remains in a chronological ledger. A session aborted by
  recorded host power loss, device reboot, unrecoverable model-service crash or
  resource-sampler failure may be replaced under a new session identifier only
  before outcome review; its partial runs remain reported but do not enter the
  confirmatory timing analysis.
- A failure of the system under test is a result, not infrastructure failure. It
  is never replaced and fails the lifecycle decision.
- Safety, lifecycle or identity failures stop collection and require protocol
  review rather than automatic replacement.

The final report lists all planned attempts, failures, aborted sessions and
condition order. Formal claims are prohibited until the complete dataset and
independent reconstruction both validate.

## Stop conditions

Collection stops immediately for physical motion or UART access, stale-result
consumption, a queue or lifecycle invariant violation, a worker or child cleanup
failure, an unexplained telemetry parse error, identity drift or the thermal
stop threshold. Performance results cannot override a failed safety or
correctness Gate.

## Activation and closed-collection boundary

Merging this amendment to `main` superseded v2 and activated protocol version
`phase1-g6-fixed-input-sync-async-v3`. The exact v1 and v2 JSON artifacts remain
immutable, and the v2 failure analyzer remains bound to the v2 ID and SHA-256.

The formal session runner and independent analyzer load the exact v3 JSON and
fail closed on any schedule, identity, VLM process protocol, residency order,
threshold or analysis-parameter difference. Before the reviewed merge, the
clean synchronized-`main` preflight prevented formal collection. After
activation, collection started at session 1 under a new v3 collection identifier.
The first attempt produced the non-replaceable system-under-test failure recorded
above. The default runner now rejects further v3 collection with status
`closed_after_system_under_test_failure`.
