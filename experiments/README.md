# Experiments

This directory contains the reproducible measurement and analysis code used to
move from the synchronous thesis baseline to the bounded asynchronous runtime
study. Raw inputs and complete device runs are kept outside Git; the repository
tracks protocols, validators, analyzers and privacy-preserving derived results.

> 中文简介：本目录集中索引 Phase 0 同步基线与 Phase 1 异步运行时研究。原始输入、完整
> 运行目录和服务日志不进入 Git；公开目录只保存协议、分析程序以及经过校验的派生结果。

## Study map

| Study | Purpose | Current state |
| --- | --- | --- |
| [`phase0/`](phase0/) | Measure fixed-input synchronous ASR, LLM and VLM workloads on the target Jetson | Complete baseline tooling and formal-analysis path |
| [`phase1/`](phase1/) | Evaluate bounded task ownership, lifecycle, freshness, responsiveness and workload cost | Closed with a valid negative formal result; overall success Gate not met |
| [Phase 2 design](../docs/architecture/phase2-residency-restoration-design.md) | Test bounded, fully charged Whisper residency restoration after VLM | P2-D0 complete; host-only experiment package is next |

Phase 0 establishes workload identities and the synchronous reference. Phase 1
reuses those identities so that an architecture comparison does not silently
become a model, prompt or input comparison. The complete project progression is
described in the [research roadmap](../docs/research-roadmap.md).

## Phase 1 evidence index

The result directories below are ordered by research role rather than treated
as a sequence of interchangeable trials.

### Runtime and workload correctness

| Evidence | Role | Status |
| --- | --- | --- |
| [Jetson simulation pilot](phase1/results/20260828T121142Z_phase1_jetson_pilot/) | Validate the device evidence chain with simulated workload durations | Descriptive pilot complete |
| [Fixed-input VLM pilot](phase1/results/20260830T073825Z_phase1_vlm_pilot/) | Test nominal consumption and stale rejection on the real VLM path | Correctness pilot complete; thread probe gaps observed |
| [Process-isolated VLM pilot](phase1/results/20260830T122541Z_phase1_vlm_process_reaping/) | Move blocking VLM work behind bounded IPC and verify child cleanup | Correctness pilot complete |
| [Fixed-input ASR pilot](phase1/results/20260831T140705Z_phase1_asr_pilot_v2/) | Verify supervised Whisper execution, output identity and stale rejection | ASR component of G5 complete |
| [Fixed-input LLM pilot](phase1/results/20260901T143315Z_phase1_llm_pilot/) | Verify the frozen llama.cpp request path, output identity and stale rejection | LLM component complete; G5 closed |

### Formal commissioning and repair evidence

| Evidence | Role | Status |
| --- | --- | --- |
| [G6 v2 failed formal attempt](phase1/results/20260905T140816Z_phase1_formal_g6_v2/) | Preserve the first v2 system-under-test failure and its validated artifact prefix | Closed; no formal claim |
| [VLM residency-order diagnostic](phase1/results/20260905T160805Z_phase1_vlm_residency_diag/) | Validate the unload-before-Qwen correction outside the formal protocol | Descriptive diagnostic complete |
| [G6 v3 failed formal attempt](phase1/results/20260906T055511Z_phase1_formal_g6_v3/) | Preserve the v3 timeout and route-Gate failure | Closed; no formal claim |
| [VLM timeout-repair diagnostic](phase1/results/20260906T082627Z_phase1_vlm_timeout_diag/) | Test deterministic requests, unload confirmation and a 60 s boundary | Descriptive diagnostic complete |
| [VLM timeout-repair target validation](phase1/results/20260906T101723Z_phase1_vlm_timeout_repair_validation/) | Exercise the repaired repository path on the target Jetson | Correctness validation complete |

### Formal result and mechanism follow-up

| Evidence | Role | Status |
| --- | --- | --- |
| [G6 v4 formal comparison](phase1/results/20260907T051448Z_phase1_formal_g6_v4/) | Frozen synchronous/asynchronous comparison across ASR, LLM and VLM | Valid negative result; G6 failed |
| [ASR/VLM carryover diagnostic](phase1/results/20260908T072640Z_phase1_asr_vlm_carryover_v2/) | Isolate Whisper residency loss and next-invocation ASR cost after VLM | Exploratory mechanism evidence complete |

Failed attempts and diagnostics are retained because they answer different
questions. They are not pooled with the confirmatory v4 dataset, and the
carryover result does not alter the closed G6 decision.

This index is the frozen public record of Phase 1. New mitigation experiments
must use a new stage, protocol and result namespace rather than appending runs
to any Phase 1 collection. The reviewed Phase 2 design activates host-only
design and implementation work in ordered work packages; it does not yet
authorize a target collection.

## Data boundary

The following paths are intentionally ignored:

- `experiments/raw/` for fixed binary inputs;
- `experiments/runs/` for complete run artifacts and telemetry;
- `docs/private/` for transferred archives, service logs and reconstruction
  workspaces.

Tracked result directories contain independently validated derived JSON and a
short human-readable report. Hashes in those reports bind the public result to
the retained private evidence without publishing model output, prompts, local
paths or device logs.

## Reproduction entry points

Run host-safe tests from the repository root:

~~~bash
python3 -m unittest \
  experiments.phase0.test_formal_analysis \
  experiments.phase0.test_runner_support \
  experiments.phase0.test_telemetry

python3 -m unittest discover -s experiments/phase1/tests -t .
~~~

Device experiments require the exact environment, model and service identities
declared by their protocol. Start with the
[Phase 0 instructions](phase0/README.md),
[Phase 1 instructions](phase1/README.md) and the applicable protocol rather
than copying commands from a result report.
