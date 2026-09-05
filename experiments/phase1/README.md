# Phase 1 Asynchronous Runtime Research

This directory contains the host tests, simulated-condition runner, trace
recorder, event schema, independent lifecycle replay, run validation, Jetson
pilot orchestration, deterministic analysis, fixed-input VLM/ASR/LLM integration
and descriptive summaries for the Phase 1 asynchronous runtime study. The first
Jetson simulation pilot and fixed-input VLM correctness pilot are complete.
A spawned-process VLM adapter and the fixed-input ASR subprocess slice have also
completed independently validated Jetson correctness pilots. The fixed-input
LLM HTTP slice has now completed its independently validated Jetson correctness
pilot, closing G5. The amended G6 v2 protocol preregistered the formal paired
comparison. Its protocol-bound session runner and independent analyzer are
implemented. Commissioning exposed an LLM empty-history identity mismatch
before measurement and a resource-trace tail race after a complete session. An
outcome-independent schedule audit also found a repeated cross-session order
relationship in v1. The first v2 formal attempt then stopped on a VLM Qwen
timeout and failed its required translation-route Gate. G6 v2 is closed without
a confirmatory claim; a residency-order correction awaits descriptive Jetson
validation before any new protocol is preregistered.

> 中文简介：本目录用于 Phase 1 异步运行时研究。当前已实现 host-only 有界 broker、
> 单 worker 执行层、100 ms 周期探针、独立 trace replay、模拟条件运行器和 Jetson
> pilot 证据链，并完成 Jetson simulation pilot 与固定输入 VLM correctness pilot；
> 当前已验证真实 VLM 接入、陈旧结果拒绝与子进程正常回收，VLM 进程隔离路径已完成
> Jetson correctness pilot；固定输入 ASR 子进程切片也已完成 Jetson correctness pilot；
> 固定输入 LLM HTTP 切片也已完成 Jetson correctness pilot，G5 已关闭；修订后的 G6 v2
> 正式协议冻结交叉平衡顺序，协议绑定的正式 runner 与独立分析器已实现；commissioning
> 先后发现正式测量前的 LLM 空历史身份不一致、一个完整 session 后的资源轨迹尾部竞态，
> 随后的结果无关顺序审计发现 v1 的跨 session 顺序关系重复；首次 v2 正式尝试又在第 18
> 个条目因 VLM 的 Qwen 30 秒超时而停止。v2 已关闭且不支持正式结论，当前先通过描述性
> Jetson 诊断隔离 Moondream/Qwen 驻留顺序因素，再决定后续协议版本。

## Current status

- Phase 0 synchronous baseline: complete
- Phase 1 host runtime contract: frozen for the current host-only boundary
- Host-only task/result model and bounded broker: implemented and tested
- Observable worker and simulated adapter: implemented and tested
- Independent periodic probe: implemented and tested
- Inline synchronous-path probe: implemented and tested
- Phase 1 schema `0.2.0`, JSONL recorder and lifecycle replay: implemented
- Reproducible R0--R4 simulated-condition runner: implemented and host-tested
- Jetson preflight, continuous resource telemetry and pilot runner: implemented
- Jetson simulation pilot: completed and independently validated
- Deterministic pilot analysis and public descriptive report: implemented
- Fixed-input VLM adapter, single-request runner and validator: completed one
  independently validated Jetson correctness pilot
- Deterministic VLM pilot analysis and listener-binding preflight: implemented
- Spawned-process VLM adapter, cleanup evidence and runner mode: completed one
  independently validated Jetson correctness pilot
- Deterministic process-pilot reconstruction and descriptive thread reference:
  implemented
- Fixed-input ASR adapter, Whisper process supervision, runner and validator:
  completed one independently validated Jetson correctness pilot
- Deterministic ASR-pilot reconstruction and public descriptive report:
  implemented; ASR component of G5 satisfied
- Fixed-input LLM adapter, local-server preflight, runner and validator:
  completed one independently validated Jetson correctness pilot
- Deterministic LLM-pilot reconstruction and public descriptive report:
  implemented; LLM component and G5 overall satisfied
- Machine-validated G6 formal preregistration: v1 retained as history; amended
  v2 activated through its reviewed merge and is now closed after a system-
  under-test failure
- Protocol-bound formal session runner and independent analyzer: implemented
  and reviewed; LLM history binding corrected against the frozen adapter
- Formal Phase 1 evidence: no confirmatory claim permitted; two v1 commissioning
  collections and the failed v2 attempt are retained without replacement
- Deterministic v2 failed-attempt reconstruction: implemented; all 42 manifest
  artifacts, 18 run records, the ledger prefix and service log correlation
  independently verified
- VLM residency-order correction: implemented for review; descriptive Jetson
  validation pending with the existing 30 s Qwen timeout
- Physical motion and UART: excluded

The detailed contract is documented in
[`docs/architecture/phase1-runtime-contract.md`](../../docs/architecture/phase1-runtime-contract.md).

Run the current host-only tests from the repository root:

```bash
python3 -m unittest discover -s experiments/phase1/tests -p "test_*.py"
```

Replay a completed trace without importing the runtime implementation:

```bash
python3 -m experiments.phase1.replay_lifecycle \
  /path/to/events.jsonl \
  --profile runtime_threaded_probe
```

## Simulated conditions

The portable runner separates fast-path isolation from the additional broker
semantics:

| Condition | Probe path | Slow work | Purpose |
| --- | --- | --- | --- |
| `r0_idle` | independent thread | none | probe and recorder timing baseline |
| `r1_inline_sync` | caller thread | direct adapter call | blocking synchronous reference |
| `r2_threaded_sync` | independent thread | direct adapter call | timing-domain isolation without the broker |
| `r3_async` | independent thread | bounded worker | nominal asynchronous runtime |
| `r4_stale` | independent thread | worker plus state advance | old-generation result rejection |
| `r4_overflow` | independent thread | worker plus excess arrivals | bounded overflow behavior |

R0--R3 are the responsiveness decomposition. R4-stale and R4-overflow are
separate correctness stress conditions; their probe ticks are not pooled into
the primary responsiveness comparison. R4 uses a short claim barrier so the
state change or excess arrivals are injected at a deterministic lifecycle
location. Its task timings are therefore correctness evidence, not runtime-
overhead measurements.

Use one explicit session ID for conditions that will later be compared:

```bash
python3 -m experiments.phase1.run_simulation \
  --condition r1_inline_sync \
  --service-time-s 2 \
  --session-id 20260827T120000Z_phase1_simulation_pilot \
  --repetition 1
```

The default runner refuses a dirty Git tree. `--allow-dirty` is available for
development checks only; a run created with that flag is not formal
reproducibility evidence, and its manifest records the development override
and formal-evidence eligibility separately. The motion setting must be unset or
explicitly false. Enabled or unrecognized values fail before a run directory
is created.

Each run is written beneath the ignored Phase 1 root:

```text
experiments/runs/phase1-simulation/
└── <session_id>/
    └── <condition>/
        └── <run_id>/
            ├── manifest.json
            ├── scenario.json
            ├── events.jsonl
            └── summary.json
```

`manifest.json` moves from `running` to `completed` only after event replay,
condition-specific Gates, artifact hashing and final directory validation all
pass. A failed or interrupted attempt remains marked `failed` or `running` and
cannot be accepted by the validator.

Validate a completed directory independently:

```bash
python3 -m experiments.phase1.validate_run /path/to/run_dir
```

The summary reports nearest-rank p50/p95/p99 timing statistics and lifecycle
counts. It is explicitly marked descriptive-only and cannot be used as a
formal improvement claim.

## Jetson simulation pilot

The Jetson pilot reuses the validated single-run protocol. One non-daemon
`tegrastats` reader remains active across the complete session, avoiding a
resource-sampler restart between conditions. Samples are assigned to each run
using the run's monotonic start and finish timestamps.

The preflight refuses to create a session unless all of the following hold:

- the process runs on Linux ARM64 with an L4T release identity;
- `tegrastats` is available;
- the source tree is clean, on `main`, synchronized with `origin/main`, and has
  a complete Git identity;
- motion is unset or explicitly disabled;
- robot application, motion-planner and UART modules are not loaded.

Run the descriptive pilot on a clean, synchronized Jetson `main` branch with
explicit simulated service durations:

```bash
export ROBOT_ENABLE_MOTION=0
python3 -m experiments.phase1.run_jetson_pilot \
  --service-times-s 2 5 70 \
  --correctness-service-time-s 2 \
  --repetitions 1
```

The responsiveness matrix runs R0--R3 at each service duration. R4-stale and
R4-overflow run once per repetition at the separate correctness duration. The
order, durations, capacities, probe settings and 200 ms resource interval are
frozen in `pilot_plan.json` before the first condition starts. Resource rows use
the separate [`0.1.0` JSON Schema](schemas/resource.schema.json).

The ignored session directory contains:

```text
experiments/runs/phase1-jetson-pilot/
└── <session_id>/
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

The session manifest becomes `completed` only after every single-run validator,
the continuous resource trace, per-run resource coverage, sampler shutdown,
artifact hashes and an independent pilot-summary rebuild pass. Validate it
again with:

```bash
python3 -m experiments.phase1.validate_jetson_pilot /path/to/session_dir
```

Build deterministic JSON and Markdown derivatives outside the ignored session
directory:

```bash
python3 -m experiments.phase1.analyze_jetson_pilot /path/to/session_dir \
  --source-archive-sha256 <sha256> \
  --json-output /path/to/analysis.json \
  --markdown-output /path/to/report.md
```

The analyzer reruns the independent session validator before reading results.
It preserves the non-inferential claim boundary, reports missing resource
capabilities rather than converting them to zero, and applies a declared CPU
activity screen without excluding samples. It refuses to write into the source
session because an extra file would invalidate the evidence directory.

The first public derived result is the
[`20260828T121142Z` Jetson simulation pilot](results/20260828T121142Z_phase1_jetson_pilot/).
It contains one fixed-order repetition and a simulated workload. The report is
therefore descriptive evidence for protocol and runtime behavior, not a causal
resource comparison or heterogeneous-inference result.

The pilot is descriptive evidence used to design the later formal protocol. It
does not authorize an asynchronous-performance or hard-real-time claim.
Condition-level power summaries use instantaneous rail samples; the
`tegrastats`-reported average is retained only as a session-window diagnostic.

The second public derived result is the
[`20260830T073825Z` fixed-input VLM pilot](results/20260830T073825Z_phase1_vlm_pilot/).
Both real-model conditions passed their correctness Gates, but the periodic
probe recorded 85 and 63 skipped releases respectively. All skipped releases
were scheduled during lazy module import. The result therefore validates
nominal consumption and stale-result rejection while explicitly withholding a
thread-level timing-isolation claim.

The third public derived result is the
[`20260830T122541Z` process-isolated VLM pilot](results/20260830T122541Z_phase1_vlm_process_reaping/).
Both spawned children completed the bounded protocol, exited with code zero
and were reaped without forced termination. The 100 ms probe recorded zero
skipped releases and zero deadline misses in both conditions. The previously
published thread pilot recorded 148 skipped releases, but the two sessions are
single, fixed-order observations from different commits. The public report
therefore labels the contrast as a descriptive mitigation signal and continues
to prohibit causal performance and timing-isolation claims.

## Fixed-input VLM slice

The first real-workload integration reuses the exact Phase 0 C100 JPEG, the
Moondream request path, Qwen rewrite with Argos fallback, output normalization
and the per-request unload policy. The current adapter requests Moondream unload
after description and before Qwen rewriting, with cleanup on earlier failure.
`vlm_adapter.py` imports the model-facing
module only inside the worker call. Importing the experiment package on a host
does not load OpenCV, Argos, a camera or either model service.

The initial protocol has two separate single-request correctness conditions:

| Condition | State action | Required disposition |
| --- | --- | --- |
| `vlm_async` | none | one result consumed |
| `vlm_stale` | advance generation after Moondream starts | one `rejected_state`, zero consumed |

State invalidation does not claim that Ollama stopped GPU inference. The
adapter allows the backend path to finish, records
`backend_stop_confirmed=null`, and relies on the broker's generation check to
reject the completed result. Public artifacts retain only input identity,
output hash and length, translation route, stage durations and lifecycle
facts. Model text, prompts and the private input path are not serialized.

Reproduce each condition from a clean, synchronized Jetson `main` branch:

```bash
export ROBOT_ENABLE_MOTION=0
python3 -m experiments.phase1.run_vlm_slice \
  --condition vlm_async \
  --session-id 20260829T000000Z_phase1_vlm_pilot \
  --repetition 1

python3 -m experiments.phase1.run_vlm_slice \
  --condition vlm_stale \
  --session-id 20260829T000000Z_phase1_vlm_pilot \
  --repetition 1
```

The runner refuses to create a directory unless platform, Git, motion, module,
fixed-input, dependency, Ollama CLI, Moondream and Qwen checks all pass. VLM
preflight schema `0.2.0` also records the TCP listener addresses and rejects a
service bound to a wildcard or non-loopback address. Each run contains
`preflight.json`, the event and resource JSONL traces, `scenario.json`,
`summary.json` and an atomic manifest. Validate either directory again with:

```bash
python3 -m experiments.phase1.validate_vlm_slice /path/to/run_dir
```

Build deterministic derivatives from a two-condition ignored session with:

```bash
python3 -m experiments.phase1.analyze_vlm_pilot \
  /path/to/session_dir \
  --source-archive-sha256 <archive_sha256> \
  --json-output /path/to/analysis.json \
  --markdown-output /path/to/README.md
```

For a process-isolated session, add the published thread analysis as a frozen
workload-identity reference:

```bash
python3 -m experiments.phase1.analyze_vlm_pilot \
  /path/to/process_session_dir \
  --source-archive-sha256 <process_archive_sha256> \
  --thread-reference-analysis \
    experiments/phase1/results/20260830T073825Z_phase1_vlm_pilot/analysis.json \
  --json-output /path/to/analysis.json \
  --markdown-output /path/to/README.md
```

The first VLM pilot was collected under preflight schema `0.1.0`. Its request
URLs were loopback addresses, and the operator checked the llama.cpp listener
before execution, but actual listener bindings were not stored in the archive.
The public analysis retains that evidence gap rather than converting the
manual observation into a reproducible claim. The validator remains able to
read the original schema while new runs fail closed under `0.2.0`.

These two runs establish integration and stale-result correctness only. They
do not form a balanced synchronous/asynchronous comparison, prove backend
preemption, measure visual accuracy or authorize a heterogeneous-performance
claim.

### Spawned-process VLM variant

The process-isolated variant keeps the broker, state generations, result
freshness checks, event recorder and periodic probe in the parent process. It
moves only the lazy VLM adapter call into one child created with `spawn`. The
child receives one task through bounded private IPC and returns the same
hash-only result and stage facts as the thread adapter. It cannot mutate the
broker or choose a final disposition.

Each process run adds a distinct condition directory and `process.json`. The
process summary is independently rebuilt from supervisor facts in
`scenario.json` and requires a complete protocol, ordered boundaries, correct
cancellation forwarding, exit code zero and a normally reaped child. A forced
child termination is recorded separately and never becomes a claim that the
Ollama backend stopped inference.

The child closes its bounded protocol and exits from inside the process after
adapter cleanup, preventing imported inference runtimes from delaying process
reaping during interpreter shutdown. If a final Gate still fails, the run is
marked failed after its completed scenario, process and slice diagnostics are
written and hashed.

The two motion-disabled pilot conditions can be reproduced with:

```bash
export ROBOT_ENABLE_MOTION=0
python3 -m experiments.phase1.run_vlm_slice \
  --condition vlm_async \
  --adapter-isolation spawned_process \
  --process-execution-timeout-s 600 \
  --completion-timeout-s 720 \
  --session-id 20260830T000000Z_phase1_vlm_process_pilot \
  --repetition 1

python3 -m experiments.phase1.run_vlm_slice \
  --condition vlm_stale \
  --adapter-isolation spawned_process \
  --process-execution-timeout-s 600 \
  --completion-timeout-s 720 \
  --session-id 20260830T000000Z_phase1_vlm_process_pilot \
  --repetition 1
```

These commands define a correctness pilot, not a formal thread/process
comparison. Session `20260830T122541Z_phase1_vlm_process_reaping` completed
both conditions on `main@1818c83`; the Jetson and Windows independent validators
and every slice and process Gate passed. The result is published as descriptive
evidence and does not freeze a formal timing threshold.

## Fixed-input ASR slice

The first Phase 1D workload extension reuses the exact formal Phase 0 WAV
identity (`114136` bytes, SHA-256
`3fffeee1e04250faa483174a423878bf220b95f6706684f6e109ed8f9b731440`),
the `ggml-small.bin` model identity, whisper.cpp source version and command
arguments. The nominal transcript was identical across all 30 measured Phase 0
runs, so the correctness slice verifies its SHA-256 and character count while
never serializing the transcript itself.

Whisper already executes as a native subprocess. The Phase 1 worker therefore
does not add another Python process layer: it starts `whisper-cli`, waits with a
bounded poll interval, and owns timeout/cancellation termination and process
reaping. The broker, state generation, accepted-result mailbox, event recorder
and periodic probe remain in the parent process. Unlike an HTTP cancellation
request, a terminated and reaped Whisper child permits
`backend_stop_confirmed=true` for that specific process invocation.

The two host-tested correctness conditions are:

| Condition | State action | Required disposition and process fact |
| --- | --- | --- |
| `asr_async` | none | one transcript identity consumed; Whisper exits 0 and is reaped |
| `asr_stale` | observe active Whisper for 0.5 s, then advance generation | one `rejected_state`, zero consumed; Whisper is stopped and reaped |

The stale observation window is a correctness-pilot control, not a cancellation
latency or performance threshold. It is longer than the default 200 ms resource
sampling interval so at least one sample can fall inside the active adapter
interval. The runner rejects a stale window that does not exceed the configured
resource interval or that reaches either the adapter or slice completion
timeout. A first Jetson attempt without this control cancelled Whisper in about
6.5 ms: every lifecycle and process Gate passed, but the resource-coverage Gate
correctly failed because no 200 ms sample could fall inside that interval.

Run them only from a clean, synchronized Jetson `main` branch after the fixed
WAV has been restored beneath the ignored Phase 0 input root:

```bash
export ROBOT_ENABLE_MOTION=0
python3 -m experiments.phase1.run_asr_slice \
  --condition asr_async \
  --session-id 20260831T000000Z_phase1_asr_pilot \
  --repetition 1

python3 -m experiments.phase1.run_asr_slice \
  --condition asr_stale \
  --stale-observation-s 0.5 \
  --session-id 20260831T000000Z_phase1_asr_pilot \
  --repetition 1
```

The ASR preflight independently verifies the fixed input, model identity,
whisper.cpp source version, frozen inference arguments and absence of a
pre-existing `whisper-cli` process. Each successful run has the same atomic
manifest, event/resource traces, scenario, deterministic summary and
independent validation boundary as the VLM slice. Revalidate one run with:

```bash
python3 -m experiments.phase1.validate_asr_slice /path/to/run_dir
```

Session `20260831T140705Z_phase1_asr_pilot_v2` completed both real Jetson
conditions on synchronized `main@bc1ca35`. The Jetson and Windows validators and
all eleven per-run Gates passed. Its independently derived
[descriptive report](results/20260831T140705Z_phase1_asr_pilot_v2/) records the
archive identity, process facts, privacy boundary, observation control, probe
continuity and resource coverage. This satisfies the ASR component of G5. G5
was subsequently closed by the real LLM correctness slice. The separate G6
preregistration now freezes the formal design; no numerical threshold, sample
size, performance result or cancellation-latency result is inferred from this
pilot.

## Fixed-input LLM slice

The LLM slice reuses the tracked Phase 0 prompt, Qwen GGUF identity, empty
conversation history, system-prompt identity and chat request fields. It sends
one request to the pre-existing loopback llama.cpp server and records only the
response hash, character count, served response model and token usage. Prompt,
history and response text, raw HTTP data and private filesystem paths are not
serialized.

| Condition | State action | Required disposition and boundary fact |
| --- | --- | --- |
| `llm_async` | none | one response identity consumed; no cancellation requested |
| `llm_stale` | observe the active request for 0.5 s, then advance generation | one `rejected_state`, zero consumed; cancellation observed without a backend-stop claim |

The llama-server is resident before the run and remains externally managed.
State invalidation prevents an old response from entering conversation history,
but the Python worker continues its blocking HTTP wait until the server responds
or the request timeout expires. Consequently the stale condition requires
`client_wait_stopped=false` and `backend_stop_confirmed=null`; the adapter sends
no stop or unload request. The 0.5 s window is only a correctness and telemetry-
coverage control, not a cancellation-latency or performance threshold.

The preflight hashes the Phase 0 prompt and Qwen model, records a clean
llama.cpp source identity, requires exactly one llama-server with the frozen
launch arguments and model path, verifies a loopback-only listener and confirms
the expected served model ID. Optional private path overrides are
`PHASE0_QWEN_MODEL` and `PHASE0_LLAMA_DIR`; their values never enter the public
adapter artifacts.

Run both conditions only from a clean, synchronized Jetson `main` branch:

```bash
export ROBOT_ENABLE_MOTION=0
export SESSION_ID="$(date -u +%Y%m%dT%H%M%SZ)_phase1_llm_pilot"

python3 -m experiments.phase1.run_llm_slice \
  --condition llm_async \
  --session-id "$SESSION_ID" \
  --repetition 1

python3 -m experiments.phase1.run_llm_slice \
  --condition llm_stale \
  --stale-observation-s 0.5 \
  --session-id "$SESSION_ID" \
  --repetition 1
```

Revalidate either completed run independently with:

```bash
python3 -m experiments.phase1.validate_llm_slice /path/to/run_dir
```

Each run must pass its lifecycle, fixed-identity, request-contract, token-usage,
privacy, residency, cancellation-boundary, thread-closure and resource-coverage
Gates. Session `20260901T143315Z_phase1_llm_pilot` completed both conditions on
synchronized `main@6e83ede`. The Jetson and Windows validators and all fourteen
per-run Gates passed. Reconstruct a two-condition session with:

```bash
python3 -m experiments.phase1.analyze_llm_pilot /path/to/session_dir \
  --source-archive-sha256 889debda235c475ad70362980c6a85e90b9a4c782937f2bb5b0c128cecb0797e \
  --json-output /path/to/analysis.json \
  --markdown-output /path/to/README.md
```

Its independently derived
[descriptive report](results/20260901T143315Z_phase1_llm_pilot/) records the
archive and frozen identities, nominal consumption, stale rejection, token
usage, server-residency boundary, observation control, probe continuity and
resource coverage. The LLM component and G5 overall are satisfied. The separate
G6 preregistration freezes the numerical thresholds, balanced order, sample
size, exclusions and statistical methods. The two single-run durations and
resource summaries remain descriptive; they do not establish performance
superiority, cancellation latency, backend cancellation or heterogeneous
inference.

## G6 formal preregistration

The G6 v2 confirmatory Phase 1 comparison is preserved in the amended
[human-readable preregistration](../../docs/architecture/phase1-formal-preregistration.md)
and the tracked
[`phase1-g6-v2-preregistration.json`](formal/phase1-g6-v2-preregistration.json).
The superseded v1 JSON remains tracked as
[`phase1-g6-preregistration.json`](formal/phase1-g6-preregistration.json).
Validate the machine-readable protocol with:

```bash
python3 -m experiments.phase1.formal_protocol --print-sha256
```

The expected SHA-256 is
`5aa995a563234429ae7fca513e89bd64e2f75130e6d0502591dfb427134fab0a`.
The protocol became active through its reviewed merge to `main`. Its first
formal attempt subsequently produced a system-under-test failure, closing v2.

The design fixes five sessions, six paired blocks per workload and session, 30
pairs per workload and 180 measured runs overall. Every session uses each of the
six workload orders once and balances sync-first and async-first order three
times per workload. Its fixed v2 matrix also balances each workload/block across
sessions two/three, each workload/position five/five, and each measured
preceding-workload context as closely as its even or odd count permits. Every
session/block contains both pair orders. The asynchronous p95 per-run
maximum-gap bound is 300 ms. The upper confidence bound for the geometric mean
paired workload-performance ratio must not exceed `1.10`. Confidence intervals
use 100,000 paired hierarchical bootstrap resamples with seed `20260902`.

No post-hoc outlier exclusion, imputation, measured-run replacement or runtime
reordering is permitted. Warm-ups and idle epochs are excluded only by their
predeclared roles. Every planned attempt remains in the completion denominator,
and lifecycle or safety failure prevents the overall formal claim.

The formal tools load this exact protocol and refuse schedule, identity,
threshold or analysis-method drift. The runner records one complete protocol
session at a time beneath the ignored `experiments/runs/phase1-formal/` root.
It requires a clean synchronized `main`, exact workload and service identities,
dynamic DVFS, restarted model services, ten consecutive Tj samples no greater
than 55 C, continuous 200 ms resource telemetry and `ROBOT_ENABLE_MOTION=0`.
Each measured `formal_sync` call uses the inline same-thread probe; each
`formal_async` call uses the one-worker bounded runtime and independent probe.
Both VLM conditions retain the same spawned-process adapter. Every run binds
the adapter record to a separate privacy-preserving result envelope. The Gates
also enforce the expected ASR transcript identity, the frozen LLM request and
token/residency facts, and VLM child reaping and per-invocation unload request.

Collection `20260905T062312Z_phase1_formal_g6` stopped after its three ASR
warm-ups and before the first LLM request, pre-measurement idle epoch or measured
matrix. The formal task builder used the SHA-256 of an empty byte string where
the frozen LLM contract requires the identity of an empty JSON history. The
attempt remains diagnostic and is not retried or analyzed as formal evidence.
The correction reuses the adapter's frozen empty-history constant and adds
sync/async integration coverage with the real adapter boundary. It changes no
protocol identity, schedule, threshold or analysis method. Formal collection
therefore restarts from session 1 under a new collection identifier after the
correction is reviewed on `main`.

Collection `20260905T065922Z_phase1_formal_g6` then completed session 1's five
warm-ups, two idle epochs and 36 measured invocations. Before any outcome
analysis or subsequent session, the independent integrity check rejected the
session because `resources.jsonl` stopped before the post-measurement idle
interval ended. The final sample preceded that boundary by 170.607 ms, within
the frozen 200 ms sampling period. The runner stopped the sampler immediately
after the idle probe returned, allowing the most recent sample to precede the
recorded finish boundary while still marking the manifest complete. The
collection is retained unchanged as diagnostic evidence and is not analyzed or
continued.
The resource-tail correction requires a positive resource sample at or after the final
activity boundary before sampler shutdown and makes absence of that sample an
infrastructure failure. It changes no protocol identity, hypothesis, schedule,
threshold or statistical method. Admissible collection again restarts from
session 1 under a new identifier after review on `main`.

A subsequent audit used only the serialized v1 order, not timing, resource,
model-output or endpoint values. It found that the workload-order and shared
condition-order cycles both reset at each session, repeating the same
condition/predecessor relationship five times. G6 v2 therefore replaces only
the condition-order matrix with the fixed cross-balanced schedule documented in
the preregistration. It retains all sample sizes, inputs, conditions,
environment constraints, thresholds, statistical methods, exclusions and
stopping rules. Both v1 commissioning collections remain diagnostic and no v1
run can enter the v2 analysis.

Collection `20260905T140816Z_phase1_formal_g6_v2` was the first admissible v2
attempt. It passed the frozen preflight, completed five warm-ups, the
pre-measurement idle epoch and 12 measured runs, then stopped at measured
ordinal 18. The VLM Qwen rewrite ran for 30029.203 ms at its 30 s client
boundary, used the disallowed Argos fallback and failed
`translation_route_verified`. The VLM child exited normally, llama-server
cancelled the corresponding request and returned its slot to idle, all 3,558
resource samples validated, and the maximum Tj was 55.093 C. The independently
derived
[failed-attempt report](results/20260905T140816Z_phase1_formal_g6_v2/)
records 179 passed Gates and the single failure while retaining all raw model
text, service logs and private paths outside Git.

The recorded stage order placed the Moondream unload request after the failed
Qwen rewrite and Argos fallback. This is a residency-order confound, not proof
that residency caused the timeout. The implementation correction now requests
Moondream unload before Qwen, records privacy-safe exception classes, and keeps
cleanup on earlier failures. The process and summary schemas advance to `0.2.0`;
the validator still reconstructs retained `0.1.0` pilot artifacts by their
original contracts.

Reconstruct the closed attempt from a private collection and service log with:

```bash
python3 -m experiments.phase1.analyze_formal_failure \
  /path/to/20260905T140816Z_phase1_formal_g6_v2 \
  --llama-log /path/to/phase1_formal_v2_llama.log \
  --source-archive-sha256 0306a0c9e5e2746b9da37c15db3189c51cc131771d515dfe97d420b1f829a892 \
  --llama-log-archive-sha256 67352addf8dcb67c57eeaa19cd5b5e90afd6e819bddeab42ed3d669e2af6ab40 \
  --json-output /tmp/phase1-g6-v2-failure.json \
  --markdown-output /tmp/phase1-g6-v2-failure.md
```

The default formal runner refuses further v2 collection. The failed attempt is
not rerun or replaced, and the incomplete matrix is not used for performance
estimation. After this correction is reviewed on `main`, a repeated fixed-input
VLM pilot will test the new residency order descriptively with the 30 s timeout
unchanged. That diagnostic determines whether a future G6 protocol can retain
the timeout or needs a separately justified value. Only a newly preregistered
and reviewed protocol may restart formal collection from session 1.

## Planned implementation order

1. freeze the task, result, lifecycle, queue, cancellation, and freshness
   contract — complete;
2. implement and stress-test the host-only runtime kernel — complete;
3. add the observable worker, periodic probe, Phase 1 event schema, and
   independent trace replay — complete;
4. implement the portable R0--R4 simulation protocol and run artifacts —
   complete;
5. implement and host-test Jetson preflight, continuous resource telemetry and
   pilot session validation — complete;
6. run and independently analyze the safe Jetson simulation pilot with
   `ROBOT_ENABLE_MOTION=0` — complete;
7. implement, run and independently analyze the fixed-input VLM correctness
   slice — complete;
8. record actual model-service listener bindings and qualify the simulated
   thread-isolation result with real-workload evidence — complete;
9. implement process-level VLM isolation and independently analyze its Jetson
   correctness pilot — complete;
10. extend the adapter/runtime boundary to ASR and LLM, then independently
    analyze both Jetson correctness pilots — complete; G5 closed;
11. preregister formal thresholds, balanced order, sample size, exclusions and
    statistical methods — complete;
12. implement and review the protocol-bound formal runner and independent
    analyzer — complete;
13. collect, validate and publish the formal synchronous/asynchronous comparison
    — v2 stopped on a system-under-test VLM timeout and is closed; residency-
    order diagnostic and a new protocol version are required;
14. add an opt-in motion-disabled application slice after the research Gates
    pass.

Contract changes are reviewed before implementation, and the formal protocol
is frozen before data collection.

## Safety rules

Phase 1 automated tests and initial experiments:

- do not import or call `jetson.robot_comm`;
- do not open `/dev/ttyTHS1`;
- refuse to run if physical motion is enabled or the motion setting is
  unrecognized;
- do not require the STM32, motor power, or the assembled chassis;
- do not modify Phase 0 schemas, run directories, reports, or archives;
- use a new Phase 1 schema and ignored run root;
- keep raw audio, images, prompts, model outputs, and private paths out of event
  details.

Any violation stops the experiment regardless of its performance result.

## Code boundary

The reusable, hardware-independent kernel lives under
`jetson/phase1_runtime/`. The current experiment-specific files are:

```text
experiments/phase1/
├── analyze_asr_pilot.py
├── analyze_formal_failure.py
├── analyze_formal_runs.py
├── analyze_llm_pilot.py
├── analyze_vlm_pilot.py
├── asr_adapter.py
├── asr_preflight.py
├── asr_slice.py
├── formal/
│   ├── phase1-g6-preregistration.json
│   └── phase1-g6-v2-preregistration.json
├── formal_protocol.py
├── formal_preflight.py
├── formal_run.py
├── jetson_preflight.py
├── jetson_telemetry.py
├── llm_adapter.py
├── llm_preflight.py
├── llm_slice.py
├── manifest.py
├── pilot.py
├── replay_lifecycle.py
├── run_asr_slice.py
├── run_formal_session.py
├── run_jetson_pilot.py
├── run_llm_slice.py
├── run_simulation.py
├── run_vlm_slice.py
├── schemas/
│   ├── event.schema.json
│   └── resource.schema.json
├── simulation.py
├── summarize_asr_slice.py
├── summarize_llm_slice.py
├── summarize_run.py
├── summarize_vlm_process_slice.py
├── summarize_vlm_slice.py
├── tests/
├── telemetry.py
├── validate_asr_slice.py
├── validate_jetson_pilot.py
├── validate_llm_slice.py
├── validate_run.py
├── validate_vlm_slice.py
├── vlm_adapter.py
├── vlm_preflight.py
├── vlm_process_adapter.py
├── vlm_slice.py
└── README.md
```

The pilot analyzers validate ignored raw sessions and produce tracked,
deterministic derivatives under `results/`. Raw sessions remain outside Git.

The experiment layer owns condition scheduling, run directories, manifests,
validation and summaries. The executor continues to own all traced broker
mutations, while result consumption remains explicit so result-mailbox
capacity and the second freshness check stay measurable.

The kernel package must remain import-safe on a host without Jetson models,
camera, microphone, serial device, or NVIDIA tools. Real workload dependencies
are imported lazily only by their explicit Jetson experiment adapters.

## Evidence standard

Phase 1 correctness is not established by console output or runtime counters
alone. `replay_lifecycle.py` reconstructs the append-only event trace without
importing runtime classes and proves:

- queue and result-mailbox bounds were respected;
- each admitted task reached exactly one final disposition;
- cancellation and state changes produced legal transitions;
- no stale, cancelled, superseded, or mismatched result was consumed;
- shutdown closed all live lifecycle locations;
- event sequence and monotonic timestamps remained valid.

Replay uses an explicit trace profile. A probe-only condition cannot pass as a
runtime trace, an inline probe cannot claim a worker join, and a runtime-plus-
probe condition must close both lifecycles.

Pilot runs are descriptive. Numerical success thresholds, sample sizes,
condition order, exclusions, and statistical methods are frozen before formal
data collection.
