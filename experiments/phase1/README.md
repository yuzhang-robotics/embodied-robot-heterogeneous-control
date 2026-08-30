# Phase 1 Asynchronous Runtime Research

This directory contains the host tests, simulated-condition runner, trace
recorder, event schema, independent lifecycle replay, run validation, Jetson
pilot orchestration, deterministic analysis, fixed-input VLM integration and
descriptive summaries for the Phase 1 asynchronous runtime study. The first
Jetson simulation pilot and fixed-input VLM correctness pilot are complete.
Formal synchronous/asynchronous data remain uncollected.

> 中文简介：本目录用于 Phase 1 异步运行时研究。当前已实现 host-only 有界 broker、
> 单 worker 执行层、100 ms 周期探针、独立 trace replay、模拟条件运行器和 Jetson
> pilot 证据链，并完成 Jetson simulation pilot 与固定输入 VLM correctness pilot；
> 当前已验证真实模型接入和陈旧结果拒绝，正式对比数据尚未采集。

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
- Real ASR/LLM slices: not started
- Formal Phase 1 data: not collected
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

## Fixed-input VLM slice

The first real-workload integration reuses the exact Phase 0 C100 JPEG, the
Moondream request path, Qwen rewrite with Argos fallback, output normalization
and the per-request unload policy. `vlm_adapter.py` imports the model-facing
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
9. evaluate process-level isolation, then extend the adapter/runtime boundary
   to ASR and LLM;
10. freeze formal thresholds and collect balanced synchronous/asynchronous data;
11. add an opt-in motion-disabled application slice after the research Gates
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
├── analyze_vlm_pilot.py
├── jetson_preflight.py
├── jetson_telemetry.py
├── manifest.py
├── pilot.py
├── run_jetson_pilot.py
├── run_simulation.py
├── run_vlm_slice.py
├── schemas/
│   ├── event.schema.json
│   └── resource.schema.json
├── simulation.py
├── summarize_run.py
├── summarize_vlm_slice.py
├── tests/
├── telemetry.py
├── validate_jetson_pilot.py
├── validate_run.py
├── validate_vlm_slice.py
├── vlm_adapter.py
├── vlm_preflight.py
├── vlm_slice.py
├── replay_lifecycle.py
└── README.md
```

`analyze_jetson_pilot.py` validates an ignored raw session and produces the
tracked, deterministic derivatives under `results/`. The raw session remains
outside Git.

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
