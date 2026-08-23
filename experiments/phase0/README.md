# Phase 0 Baseline Measurement

This directory contains host-testable telemetry helpers for the synchronous
baseline study. The current implementation records schema-shaped JSONL events
and parses the `tegrastats` format observed on the validated JetPack 6.2.2
system.

Importing these modules does not open a serial port, start inference services,
or enable physical motion. The component runner refuses to start if
`ROBOT_ENABLE_MOTION` is enabled or contains an unrecognized value. It measures
one fixed-input ASR, LLM or VLM task per invocation and never imports the
chassis communication module.

> 中文简介：本目录提供 Phase 0 同步基线测量工具，用于在 Jetson 上以固定输入记录 ASR、LLM 和 VLM 的时延与资源占用。运行器不会连接底盘串口，并会在运动已启用或配置值无效时拒绝启动。

Run the current tests from the repository root:

```bash
python3 -m unittest experiments.phase0.test_telemetry
python3 -m unittest experiments.phase0.test_runner_support
```

Measure the event recorder before running models:

```bash
python3 -m experiments.phase0.benchmark_recorder --events 1000
```

The command uses a temporary directory, reports per-event p50/p95/p99/max and
flush time, then removes its output. The Phase 0 provisional acceptance
threshold is an event-write p99 below 1 ms on the Jetson.

The runner is intended for the validated Jetson environment. Run number `000`
is a recorded warm-up that is excluded from statistics; `001` through `003`
are the initial measured trials. Run one workload from the repository root:

```bash
ROBOT_ENABLE_MOTION=0 python3 -m experiments.phase0.run_workload \
  --workload <asr|llm|vlm> \
  --input <fixed-input-path> \
  --repetition <0-999>
```

Fixed binary inputs belong under the ignored `experiments/raw/` directory.
Every run records the exact input path, size and SHA-256 in its manifest. ASR
loads Whisper for each invocation. LLM requires the validated llama.cpp server
to be running before the command starts. VLM requires Ollama and the llama.cpp
translation server; it requests that Moondream be unloaded after every run.

Validate a completed run with:

```bash
python3 -m experiments.phase0.validate_run experiments/runs/<run_id>
```

Print a privacy-preserving timing and resource summary with:

```bash
python3 -m experiments.phase0.summarize_run experiments/runs/<run_id>
```

The summary includes output length and hash, but not the model response text.
For LLM runs it also reports saved prompt/completion token counts plus completion
tokens per second and milliseconds per completion token. These rates use the
recorded request wall duration, which includes HTTP handling and prompt
evaluation; they are deliberately not labelled as decode-only throughput.
VLM traces split Python module import, Moondream inference, Qwen rewriting,
Argos fallback and model unload into separate stages. VLM aggregation refuses
to combine runs that used different translation routes.

Aggregate two or more measured runs with:

```bash
python3 -m experiments.phase0.aggregate_runs \
  experiments/runs/<run-001> \
  experiments/runs/<run-002> \
  experiments/runs/<run-003>
```

The command checks workload, input hash, residency policy, baseline commit,
runner commit and timing-stage consistency before reporting descriptive
statistics, including LLM token metrics when present. Pilot output is
explicitly labelled as non-inferential.

Raw experiment inputs and outputs belong under `experiments/raw/` and
`experiments/runs/`; both paths are ignored by Git.
