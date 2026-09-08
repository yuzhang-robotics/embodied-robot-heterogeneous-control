# Phase 1 Bounded Runtime Study

Phase 1 investigates whether long-running ASR, LLM and VLM work can be removed
from a time-constrained Jetson path without creating unbounded backlog,
unaccounted work or obsolete results. It combines a host-testable runtime
kernel with fixed-input Jetson experiments and independently reconstructed
evidence.

> 中文简介：Phase 1 研究本地长时推理与时限敏感任务之间的运行时边界。任务生命周期、
> 队列容量、取消和结果新鲜度机制已实现，VLM、ASR、LLM 实模型正确性 Gate 已完成；
> G6 v4 正式对照也已完成，但三个工作负载均未证明冻结的 10% 性能非劣效界限。因此
> Phase 1 得到了有效阴性结果，整体成功 Gate 未通过，整机应用切片未获授权。

## Final status

| Work item | State |
| --- | --- |
| Host-only runtime and lifecycle replay | Complete |
| Simulated-condition runner and Jetson telemetry path | Complete |
| VLM, ASR and LLM correctness pilots | Complete; G5 closed |
| Preregistered synchronous/asynchronous comparison | Complete under G6 v4 |
| Lifecycle and software probe-responsiveness criteria | Passed for all three workloads |
| Workload-performance noninferiority | Not established for ASR, LLM or VLM |
| Overall Phase 1 success Gate | Not met |
| Motion-disabled application slice | Not authorized |
| ASR/VLM carryover mechanism follow-up | Complete; exploratory evidence only |

The formal experiment is finished and will not be rerun, extended or evaluated
with a changed margin. “Complete” therefore describes the research execution,
not a successful G6 decision.

## Research boundary

Phase 1 tests three separate properties:

1. **Lifecycle correctness:** every admitted task has bounded ownership and one
   accountable terminal disposition.
2. **Result validity:** cancelled, expired, superseded, mismatched or
   old-generation results are rejected before consumption.
3. **Timing and cost:** an independent 100 ms software probe remains responsive
   and the same workload adapter does not exceed the frozen performance margin
   when used through the asynchronous path.

The runtime is not a hard-real-time scheduler. It reports observed periods,
lateness, gaps and deadline misses without claiming a worst-case execution
time or operating-system guarantee.

## Implemented system

### Runtime kernel

The reusable package in
[jetson/phase1_runtime/](../../jetson/phase1_runtime/) provides:

- immutable task, result, state and payload-reference records;
- bounded pending, active, result-mailbox and state-scope ownership;
- reject-new, drop-oldest and coalesce-by-key overflow policies;
- one non-daemon observable worker with cooperative cancellation and finite
  join reports;
- result freshness checks at publication and consumption;
- inline and independent absolute-schedule probes;
- schema-shaped append-only events.

The kernel remains import-safe without a camera, microphone, serial device,
Jetson models or NVIDIA utilities.

### Experiment layer

The package is grouped by experimental responsibility:

| Path | Responsibility |
| --- | --- |
| [common/](common/) | Manifests, preflight, telemetry and independent lifecycle replay |
| [simulation/](simulation/) | Host simulation, Jetson pilot, summaries, validation and analysis |
| [workloads/asr/](workloads/asr/) | Fixed-input ASR adapter, slice, preflight, summary, validation and analysis |
| [workloads/llm/](workloads/llm/) | Fixed-input LLM adapter, slice, preflight, summary, validation and analysis |
| [workloads/vlm/](workloads/vlm/) | Fixed-input VLM adapters, slice, diagnostics, summaries, validation and analysis |
| [formal/](formal/) | G6 protocols, preflight, execution and formal analysis |
| [carryover/](carryover/) | ASR/VLM carryover protocol, observation, preflight and analysis |
| [diagnostic/](diagnostic/) and [schemas/](schemas/) | Additional machine-readable contracts |
| [tests/](tests/) | Host regression tests and fixtures |
| [results/](results/) | Claim-bounded public evidence |

The seven `run_*.py` modules remain at the Phase 1 root as stable device entry
points. `formal_protocol.py`, `carryover_protocol.py` and
`analyze_carryover_diagnostic.py` remain as compatibility entry points for
published commands; their implementations live in the corresponding packages.
Shared fixed-input slice, runner, summary and validation plumbing is internal to
[workloads/](workloads/), while workload-specific contracts remain separated.

Real workload dependencies are imported lazily by explicit Jetson adapters.
The experiment layer owns scheduling, run directories, manifests, validation
and analysis; the runtime kernel owns broker mutations and executor lifecycle.

## Evidence progression

The work advanced through distinct evidence levels:

- host tests established queue, ownership, cancellation and replay invariants;
- simulated R0–R4 conditions exercised the measurement path without claiming
  real-model timing behavior;
- the Jetson simulation pilot validated preflight, telemetry and session
  reconstruction on the target device;
- fixed-input VLM, ASR and LLM pilots validated real adapter behavior, nominal
  consumption, stale rejection and process cleanup, closing G5;
- G6 v4 applied a frozen five-session paired design to the synchronous and
  bounded asynchronous paths;
- the carryover diagnostic investigated one mechanism suggested by the closed
  formal evidence.

The v2 and v3 formal attempts stopped under their predeclared system-under-test
failure rules. Their partial evidence remains immutable and was not pooled with
v4. Repair diagnostics were conducted outside the closed protocols before v4
started from a new collection.

All public reports, including failed attempts, are indexed in
[experiments/README.md](../README.md).

## G6 v4 result

The final collection
[20260907T051448Z_phase1_formal_g6_v4](results/20260907T051448Z_phase1_formal_g6_v4/)
completed all five sessions and 180 planned measurements. Every run Gate,
lifecycle criterion and asynchronous 100 ms probe-responsiveness endpoint
passed.

The upper 95% confidence bound of the paired workload-performance ratio
exceeded the preregistered 1.10 limit for ASR, LLM and VLM. Because the
intersection-union decision required every workload to pass every criterion,
the overall G6 result is <code>FAIL</code> and no formal performance claim is
permitted.

This is failure to establish noninferiority, not evidence that every
asynchronous invocation was slower. It is also not an execution failure: the
complete dataset, Gates and independent reconstruction are valid.

## Carryover finding

The follow-up
[ASR/VLM carryover diagnostic](results/20260908T072640Z_phase1_asr_vlm_carryover_v2/)
covered all six orders of duration-matched idle, LLM and VLM work. It found a
repeatable sequence after VLM:

- the warmed Whisper model file was almost entirely non-resident;
- the next ASR invocation incurred substantially more major faults and a large
  cold-start delay;
- an immediate recovery invocation returned to the warm latency range;
- the idle and LLM controls did not reproduce the pattern.

This identifies a plausible memory-residency mechanism behind the strong ASR
position effect. It is configuration-specific exploratory evidence and does
not alter, replace or reopen G6 v4.

## Reproduction entry points

Run the Phase 1 host suite from the repository root:

~~~bash
python3 -m unittest discover -s experiments/phase1/tests -t .
~~~

Check the tracked machine-readable protocols:

~~~bash
python3 -m experiments.phase1.formal_protocol
python3 -m experiments.phase1.carryover_protocol
~~~

The principal device entry points are:

| Command module | Purpose |
| --- | --- |
| <code>experiments.phase1.run_jetson_pilot</code> | Motion-disabled simulated-condition Jetson session |
| <code>experiments.phase1.run_vlm_slice</code> | Fixed-input VLM correctness slice |
| <code>experiments.phase1.run_asr_slice</code> | Fixed-input ASR correctness slice |
| <code>experiments.phase1.run_llm_slice</code> | Fixed-input LLM correctness slice |
| <code>experiments.phase1.run_formal_session</code> | Protocol-bound G6 session |
| <code>experiments.phase1.run_carryover_session</code> | Protocol-bound carryover session |

Do not start a device runner from this table alone. Each formal or diagnostic
collection binds a repository state, protocol hash, model/service identity,
session order, thermal rule and private input identity. Review the applicable
contract and the command's <code>--help</code> output first.

## Safety rules

Phase 1 tests and experiments:

- refuse to run when <code>ROBOT_ENABLE_MOTION</code> is true or unrecognized;
- do not import or call <code>jetson.robot_comm</code>,
  <code>jetson.motion_planner</code> or <code>jetson.app</code>;
- do not open <code>/dev/ttyTHS1</code>;
- do not require the STM32, motor power or assembled chassis;
- do not modify Phase 0 schemas, run directories, reports or archives;
- store new raw artifacts only under ignored Phase 1 paths;
- keep raw inputs, prompts, model outputs, service logs and private paths out
  of public event details.

Any safety, protocol, environment, artifact or lifecycle violation invalidates
the affected run regardless of its timing.

## Evidence model

Console output is not treated as sufficient evidence. A complete study binds:

- a reviewed human-readable contract and machine-readable protocol;
- fail-closed environment and input preflight;
- atomic manifests and artifact hashes;
- append-only schedule or lifecycle records;
- continuous resource and thermal sampling;
- validators that operate on completed artifacts;
- an independent analyzer that reconstructs the declared endpoints;
- a claim-bounded public report and derived JSON.

Raw sessions remain under ignored <code>experiments/runs/</code>; complete
archives and service logs remain private. Published results contain no raw
audio, images, prompts, model text or local filesystem paths.

## Next decision

The next experiment should test a narrowly defined Whisper-residency mitigation
or post-VLM rewarm policy, including the policy's own latency, memory and power
cost. Its endpoints and stopping rules must be frozen before confirmatory
collection. Broader resource arbitration and live application integration
follow only if a new Gate explicitly authorizes them.

Encoder feedback, closed-loop PWM and full mecanum control are separate research
questions and remain outside this runtime study.

## Reference documents

- [Research roadmap](../../docs/research-roadmap.md)
- [Architecture overview](../../docs/architecture/README.md)
- [Phase 1 runtime contract](../../docs/architecture/phase1-runtime-contract.md)
- [G6 formal preregistration](../../docs/architecture/phase1-formal-preregistration.md)
- [ASR/VLM carryover design](../../docs/architecture/phase1-asr-vlm-carryover-diagnostic.md)
- [Experiment and result index](../README.md)
