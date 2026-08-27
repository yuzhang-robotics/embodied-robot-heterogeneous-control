# Phase 1 Asynchronous Runtime Research

This directory contains the host tests, simulated-condition runner, trace
recorder, event schema, independent lifecycle replay, run validation and
descriptive summaries for the Phase 1 asynchronous runtime study. Jetson
pilots and Phase 1 performance results have not been completed yet.

> 中文简介：本目录用于 Phase 1 异步运行时研究。当前已实现 host-only 有界 broker、
> 单 worker 执行层、100 ms 周期探针、独立 trace replay 和模拟条件运行器；尚未开展
> Jetson pilot、真实模型接入或 Phase 1 正式数据采集。

## Current status

- Phase 0 synchronous baseline: complete
- Phase 1 host runtime contract: frozen for the current host-only boundary
- Host-only task/result model and bounded broker: implemented and tested
- Observable worker and simulated adapter: implemented and tested
- Independent periodic probe: implemented and tested
- Inline synchronous-path probe: implemented and tested
- Phase 1 schema `0.2.0`, JSONL recorder and lifecycle replay: implemented
- Reproducible R0--R4 simulated-condition runner: implemented and host-tested
- Jetson resource telemetry and simulation pilot: not started
- Real VLM/ASR/LLM slices: not started
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

## Planned implementation order

1. freeze the task, result, lifecycle, queue, cancellation, and freshness
   contract — complete;
2. implement and stress-test the host-only runtime kernel — complete;
3. add the observable worker, periodic probe, Phase 1 event schema, and
   independent trace replay — complete;
4. implement the portable R0--R4 simulation protocol and run artifacts —
   complete;
5. add Jetson resource telemetry and run the safe simulation pilot with
   `ROBOT_ENABLE_MOTION=0`;
6. integrate the fixed-input VLM slice;
7. extend the same adapter/runtime boundary to ASR and LLM;
8. freeze formal thresholds and collect balanced synchronous/asynchronous data;
9. add an opt-in motion-disabled application slice after the research Gates
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
├── manifest.py
├── run_simulation.py
├── schemas/event.schema.json
├── simulation.py
├── summarize_run.py
├── tests/
├── telemetry.py
├── validate_run.py
├── replay_lifecycle.py
└── README.md
```

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
