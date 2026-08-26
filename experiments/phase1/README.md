# Phase 1 Asynchronous Runtime Research

This directory contains the host tests and will contain the experiment harness,
workload adapters, validation tools, and analysis for the Phase 1 asynchronous
runtime study. The immutable task/result model and bounded broker are the first
implementation milestone. Worker threads, Jetson pilots, and Phase 1
performance results have not been completed yet.

> 中文简介：本目录用于 Phase 1 异步运行时研究。当前已冻结首个运行时契约并实现
> host-only 数据模型和有界 broker；尚未接入工作线程、Jetson 模型或 Phase 1 正式数据。

## Current status

- Phase 0 synchronous baseline: complete
- Phase 1.0 runtime contract: frozen for the first implementation milestone
- Host-only task/result model and bounded broker: implemented and tested
- Worker and periodic probe: not implemented
- Jetson simulation pilot: not started
- Real VLM/ASR/LLM slices: not started
- Formal Phase 1 data: not collected
- Physical motion and UART: excluded

The detailed contract is documented in
[`docs/architecture/phase1-runtime-contract.md`](../../docs/architecture/phase1-runtime-contract.md).

Run the current host-only tests from the repository root:

```bash
python3 -m unittest \
  experiments.phase1.tests.test_model \
  experiments.phase1.tests.test_broker
```

## Planned implementation order

1. freeze the task, result, lifecycle, queue, cancellation, and freshness
   contract;
2. implement and stress-test the host-only runtime kernel;
3. add the periodic probe, Phase 1 event schema, and independent trace replay;
4. run safe simulated loads on the Jetson with `ROBOT_ENABLE_MOTION=0`;
5. integrate the fixed-input VLM slice;
6. extend the same adapter/runtime boundary to ASR and LLM;
7. freeze formal thresholds and collect balanced synchronous/asynchronous data;
8. add an opt-in motion-disabled application slice after the research Gates
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

## Planned code boundary

The reusable, hardware-independent kernel will live under
`jetson/phase1_runtime/`. Experiment-specific code will remain here:

```text
experiments/phase1/
├── schemas/
├── workloads/
├── tests/
├── manifest.py
├── telemetry.py
├── scenarios.py
├── run_simulation.py
├── run_workload.py
├── validate_run.py
├── replay_lifecycle.py
├── summarize_run.py
└── analyze_formal_runs.py
```

The kernel package must remain import-safe on a host without Jetson models,
camera, microphone, serial device, or NVIDIA tools. Real workload dependencies
are imported lazily only by their explicit Jetson experiment adapters.

## Evidence standard

Phase 1 correctness is not established by console output or runtime counters
alone. A separate validator must replay the append-only event trace and prove:

- queue and result-mailbox bounds were respected;
- each admitted task reached exactly one final disposition;
- cancellation and state changes produced legal transitions;
- no stale, cancelled, superseded, or mismatched result was consumed;
- shutdown closed all live lifecycle locations;
- event sequence and monotonic timestamps remained valid.

Pilot runs are descriptive. Numerical success thresholds, sample sizes,
condition order, exclusions, and statistical methods are frozen before formal
data collection.
