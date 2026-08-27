# Phase 1 Asynchronous Runtime Research

This directory contains the host tests, trace recorder, event schema and
independent lifecycle replay for the Phase 1 asynchronous runtime study. The
reusable package now includes a bounded single-worker executor, simulated
adapter and periodic probe. Jetson pilots and Phase 1 performance results have
not been completed yet.

> 中文简介：本目录用于 Phase 1 异步运行时研究。当前已实现 host-only 有界 broker、
> 单 worker 执行层、100 ms 周期探针和独立 trace replay；尚未开展 Jetson pilot、
> 真实模型接入或 Phase 1 正式数据采集。

## Current status

- Phase 0 synchronous baseline: complete
- Phase 1 host runtime contract: frozen for the current host-only boundary
- Host-only task/result model and bounded broker: implemented and tested
- Observable worker and simulated adapter: implemented and tested
- Independent periodic probe: implemented and tested
- Phase 1 schema `0.2.0`, JSONL recorder and lifecycle replay: implemented
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
  experiments.phase1.tests.test_broker \
  experiments.phase1.tests.test_executor \
  experiments.phase1.tests.test_probe \
  experiments.phase1.tests.test_trace
```

Replay a completed trace without importing the runtime implementation:

```bash
python3 -m experiments.phase1.replay_lifecycle /path/to/events.jsonl
```

## Planned implementation order

1. freeze the task, result, lifecycle, queue, cancellation, and freshness
   contract — complete;
2. implement and stress-test the host-only runtime kernel — complete;
3. add the observable worker, periodic probe, Phase 1 event schema, and
   independent trace replay — complete;
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

## Code boundary

The reusable, hardware-independent kernel lives under
`jetson/phase1_runtime/`. The current experiment-specific files are:

```text
experiments/phase1/
├── schemas/event.schema.json
├── tests/
├── telemetry.py
├── replay_lifecycle.py
└── README.md
```

Future simulation runners, workload adapters, manifests, validation,
summaries and formal analysis remain experiment-layer work. The executor owns
all traced broker mutations, while result consumption remains explicit so the
result-mailbox capacity and second freshness check stay measurable.

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

Pilot runs are descriptive. Numerical success thresholds, sample sizes,
condition order, exclusions, and statistical methods are frozen before formal
data collection.
