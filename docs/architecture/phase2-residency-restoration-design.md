# Phase 2 Bounded Whisper Residency Restoration

This document records the reviewed design direction for Phase 2. It activates
design work only: it is not a machine-readable preregistration, does not
authorize confirmatory data collection and does not authorize application,
UART or physical-motion integration.

> 中文简介：Phase 2 已进入设计阶段。首个研究问题固定为：在相同 VLM 路径之后，
> 一个有界、可验证且完整计费的 Whisper 模型文件预取，能否恢复下一次 ASR 的就绪状态。
> 本文把实验有效性、机制证据、运行收益和应用准入分开，避免把研究变成无限性能调优。

## Status and frozen stage decisions

| Item | Reviewed decision |
| --- | --- |
| Phase name | Phase 2: Bounded Whisper Residency Restoration |
| Status | P2-D3 correctness-pilot contract and runner prepared and host-tested; target pair not yet executed |
| Primary system boundary | Fixed-input, motion-disabled Jetson experiment |
| Treatment | One bounded, verified sequential prefetch of the frozen Whisper model file |
| Control | The unchanged VLM-to-ASR path with no residency action |
| Cost boundary | Charge the treatment from the common post-VLM observation through ASR result availability |
| Phase 1 relationship | New study; G6 v4 and carryover evidence remain closed and immutable |
| Application authority | None; a later reviewed decision is required even after a positive result |

The following choices are excluded from the first study: a complete ASR
rewarm, a persistent Whisper service, model replacement, quantization changes,
CPU affinity or priority changes, frequency locking, concurrent model
scheduling and operating-system changes. Each would alter more than the
targeted file-residency mechanism.

## Research question

> On the fixed Jetson configuration, after the frozen VLM path has displaced
> the warmed Whisper model file, can a bounded and verified sequential file
> prefetch reduce the fully charged time to the next ASR result while
> preserving the existing fast-path responsiveness and lifecycle boundaries?

The study is not a general storage benchmark and does not ask whether ASR can
be made arbitrarily faster. It tests whether explicit residency restoration is
a useful resource-management primitive for this robot runtime.

## Why this follows Phase 1

Phase 1 established bounded ownership, freshness, process cleanup and periodic
probe responsiveness. Its G6 v4 comparison did not establish workload
noninferiority. The closing carryover diagnostic then found the following
repeatable sequence on the tested system:

~~~text
VLM execution
  -> near-complete loss of warmed Whisper model-file residency
  -> storage-backed major faults in the next ASR process
  -> approximately cold ASR latency
  -> immediate ASR recovery after the file became resident again
~~~

Control-flow isolation therefore remains necessary but is not sufficient.
Phase 2 adds one explicit resource-state action without changing the frozen
workload identities or reopening the Phase 1 comparison.

## Causal model

~~~mermaid
flowchart LR
    VLM["Frozen VLM path"] --> Pressure["Unified-memory and page-cache pressure"]
    Pressure --> Loss["Whisper file residency loss"]
    Loss --> Faults["Storage-backed major faults"]
    Faults --> ASR["Next-ASR duration"]
    Prefetch["Verified sequential prefetch"] --> Restore["Whisper residency restoration"]
    Restore --> Faults
    Prefetch --> Cost["Time, file I/O, CPU, memory and energy cost"]
    Cost --> Charged["Post-VLM to ASR-result charged time"]
    ASR --> Charged
~~~

The treatment is expected to reduce fault-driven ASR delay, but it also pays
its own time and I/O cost. A faster ASR invocation alone is therefore not the
primary operational result.

Important potential confounders are held fixed or recorded:

- JetPack, L4T, CUDA, power mode and dynamic-frequency policy;
- repository, Python, NumPy, whisper.cpp, llama.cpp and Ollama identities;
- fixed ASR audio, LLM prompt, VLM image, models and request parameters;
- Moondream unload confirmation and spawned VLM process cleanup;
- model-service restart policy and service process identities;
- session order, elapsed position, starting thermal state and background
  process screen;
- model-file identity, size, page size and storage/filesystem identity;
- telemetry interval and exact endpoint-boundary timestamps.

The non-touching `mmap` plus `mincore` observation reports file-backed page
residency, not total process memory. ASR process major faults are also
process-wide and cannot by themselves attribute every fault to the model file.
The mechanism claim therefore requires the two observations to agree.

## Treatment and unchanged control

### `control_vlm_then_asr`

The unit executes the frozen VLM path, records the common post-VLM memory
observation and starts measured ASR without a residency action.

### `prefetch_vlm_then_asr`

After the same post-VLM observation, one owned child process sequentially
reads the complete, preflight-verified Whisper model file using a bounded,
reusable buffer. The child reports bounded metadata only and is joined and
reaped before measured ASR starts. A second non-touching residency observation
verifies the achieved state.

The implementation candidate uses a 4 MiB reusable buffer and a 20 s action
timeout. These values become frozen evidence parameters only in the later
machine protocol. Any pre-formal change must be justified as a visible design
amendment and cannot be selected from confirmatory outcomes.

The treatment must record at least:

- start, read-completion, verification and final boundaries;
- exact bytes and chunk count;
- completion, timeout, termination, exit and reaping facts;
- process CPU time, maximum RSS, faults and context switches;
- pre-action and post-action Whisper file residency;
- no file contents, private paths or process command line.

`posix_fadvise`, `madvise` or another hint-only API is not sufficient as the
sole treatment because it does not provide the required synchronous completion
semantics. The first study also does not hash the model during every treatment;
the exact identity is verified by preflight, while the action proves that the
expected byte count was read.

## Experimental unit and time accounting

The planned unit is:

~~~text
pre-unit memory observation
  -> ASR primer 1
  -> ASR primer 2 and warm-state observation
  -> frozen VLM interposer
  -> confirmed Moondream unload and VLM child reaping
  -> common post-VLM observation
  -> prefetch or unchanged control
  -> measured ASR
  -> post-ASR observation
  -> immediate recovery ASR for diagnosis
~~~

The carryover diagnostic's 150 s primer-to-ASR boundary is not retained. It
was appropriate for mechanism localization but could hide prefetch time inside
planned idle slack. The Phase 2 operational estimand begins at the common
post-VLM observation and ends when the measured ASR result is available.

Primer, recovery and invocation-only durations remain useful secondary
measurements. They do not replace the fully charged boundary.

## Planned design

The current planning target is six independent sessions. Each session contains
four adjacent treatment-control pairs, for eight units per session, 48 units
and 24 paired contrasts overall. Odd and even sessions use complementary
`prefetch-control` and `control-prefetch` orders so that each condition is
balanced across pair position and elapsed session time.

Each unit reprimes ASR. Ollama and llama-server are restarted before each
session, their identities must differ from the prior session and sessions are
separated by at least 30 minutes. The final order, sample size, bootstrap seed
and protocol schedule remain protocol-level decisions until commissioning has
demonstrated that the evidence path is executable. Commissioning observations
will never enter confirmatory analysis.

The planned formal analysis uses adjacent within-session paired contrasts and
a fixed-seed hierarchical bootstrap that samples sessions and then pairs
within sessions. No valid unusual observation is removed, no missing value is
imputed and no additional sample is collected because a confidence interval
is inconvenient.

## Decision model

Phase 2 deliberately separates study validity, mechanism evidence, operational
benefit, responsiveness and application authority. It does not use one large
intersection of every recorded resource statistic.

### `study_valid`

This is an evidence and safety Gate, not a performance Gate. It requires:

- the exact reviewed protocol, commit, environment, input, model and service
  identities;
- the complete planned ledger, artifact inventory and resource coverage;
- no undeclared replacement, post-hoc exclusion or confirmatory reuse of pilot
  data;
- closed task, process, observation and sampler lifecycles;
- no stale result consumption or unbounded ownership;
- motion disabled, no UART access and no private data in tracked artifacts;
- adherence to the thermal and stop contract.

An incomplete treatment, action timeout or less-than-expected residency is a
system-under-test outcome when its lifecycle remains valid. It is retained and
is not converted into missing data. Evidence corruption, an unclosed process
or a safety-boundary violation makes the collection uninterpretable under the
affected protocol.

### `operational_benefit_supported`

The single primary performance estimand is the paired ratio of fully charged
time:

~~~text
post-VLM observation -> treatment/control -> measured ASR result available
~~~

The proposed confirmatory criterion is that the upper bound of the two-sided
95% confidence interval for the treatment/control geometric-mean ratio is
below `1.0`. Crossing `1.0` is an inconclusive result, not a reason to add
samples or change the threshold. A ratio above `1.0` supports no net latency
benefit for the tested treatment.

### `residency_mechanism_supported`

The proposed mechanism criteria are evaluated at collection level rather than
requiring every unit to cross an arbitrary residency threshold:

- the lower 95% confidence bound for the paired post-action resident-fraction
  difference, prefetch minus control, is above zero; and
- the upper 95% confidence bound for the paired measured-ASR major-fault
  difference, prefetch minus control, is below zero.

Pre-action residency, ASR-only duration and recovery behavior provide
supporting interpretation. Units are not excluded because VLM displacement or
post-prefetch residency was smaller than expected.

### `responsiveness_preserved`

The 100 ms absolute-schedule software probe remains active across the action
boundary. The proposed compatibility criterion retains the existing 300 ms
p95 maximum-gap reference, together with valid probe shutdown and no
unaccounted skipped releases. This is a runtime coexistence requirement, not a
request to optimize small timing differences.

### Mandatory secondary cost endpoints

The following are always reported but do not independently erase a valid
mechanism result:

- ASR-only and primer-relative duration;
- prefetch duration and effective sequential throughput;
- resident pages, minor and major faults, CPU time, RSS and context switches;
- RAM, swap, MemAvailable, Cached, SReclaimable and largest-free-block data;
- CPU/GPU frequencies and utilization;
- exact-window VDD_IN energy from boundary-interpolated integration;
- temperature and recovery-ASR behavior.

Thermal stop, OOM, unrecoverable service loss and lifecycle violations remain
hard safety or validity failures. A precise energy or memory-efficiency margin
is not invented for the first mechanism study. Those observations inform a
later, separately reviewed application-readiness decision.

### `application_slice_authorized`

A positive Phase 2 result does not automatically authorize integration. The
formal report will separately publish:

- `study_valid`;
- `operational_benefit_supported`;
- `residency_mechanism_supported`;
- `responsiveness_preserved`;
- `application_slice_authorized`.

The final field remains false until the completed cost results and a later
review explicitly authorize a new motion-disabled application study. UART and
physical motion require still later safety evidence.

## Resource and energy analysis requirement

Existing telemetry provides 200 ms Jetson resource observations but its common
summary reports means and peaks rather than exact-window energy. Phase 2 must
add deterministic slicing at the action and result boundaries and integrate
instantaneous VDD_IN power with boundary interpolation and the trapezoidal
rule. The analyzer must test the interpolation, incomplete-coverage and rail-
missing cases and must fail closed when a required window cannot be rebuilt.

Device-wide power and GPU observations are not attributed to one process.
They describe the complete treatment window and must be worded accordingly.

## Failure, stopping and anti-tuning rules

- Correctness pilots and commissioning runs are nonconfirmatory and kept in
  separate collection namespaces.
- Formal data begin only after the machine protocol and analyzer are reviewed
  and merged.
- Valid treatment failures remain outcomes; they are not silently retried or
  replaced.
- Infrastructure, operator, service and system-under-test failures must be
  distinguished by rules frozen before collection.
- Missing pairs are not imputed and partial collections do not silently become
  a smaller formal design.
- The formal collection is not extended to obtain a preferred confidence
  interval.
- Thresholds are not changed after confirmatory data are observed.
- Full ASR rewarm, persistent ASR and alternative prefetch techniques are not
  substituted under the same protocol.
- A valid positive, negative or inconclusive result closes the first Phase 2
  comparison. Another mitigation would require a separate reviewed question
  and protocol.

These rules keep the project focused on a causal systems question rather than
an open-ended latency benchmark.

## Work packages

| Work package | Deliverable | Authority after completion |
| --- | --- | --- |
| P2-D0 | Reviewed design decision, causal model, decision layers and scope | Host-only implementation may begin |
| P2-D1 | New Phase 2 package, exact protocol/schema foundations, prefetch process and host tests | Nonformal target correctness pilot may begin |
| P2-D2 | Windowed energy integration, observation validation, privacy scans and injected end-to-end tests | Jetson evidence path is testable |
| P2-D3 | Motion-disabled Jetson correctness pilot with one control and one treatment | Commissioning may begin after review |
| P2-D4 | Separate commissioning sessions and deterministic reconstruction | Formal protocol parameters may be frozen |
| P2-D5 | Reviewed machine preregistration, fixed analyzer and confirmatory collection | Phase 2 result may be interpreted |
| P2-D6 | Independent reconstruction and privacy-preserving derived report | Later application-readiness review may begin |
| P2-D7 | Separate retention and reverse-interference study if justified | Motion-disabled application design may be considered |

## Out of scope

- reopening, extending or changing Phase 1 G6 v4;
- claiming a general Jetson or Linux cache-management law;
- live microphone/camera acquisition or TTS;
- application integration, UART access or physical motion;
- STM32, motor, PID, odometry or mecanum-kinematics changes;
- multi-model concurrency, global CPU/GPU scheduling or ROS 2;
- hard-real-time or WCET claims.

## Current implementation boundary

P2-D0 through P2-D2 established the reviewed design, bounded prefetch action and
host-tested evidence path. The P2-D3 package adds a fixed one-control/one-
treatment nonformal contract, strict motion-disabled target preflight, complete
unit orchestration and injected lifecycle/failure tests. No target data were
collected. The reviewed package must be merged to synchronized `main` before
the Jetson correctness pair is executed.
