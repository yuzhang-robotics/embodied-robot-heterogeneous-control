# Research Roadmap

This project studies how a resource-limited Jetson–STM32 robot can run local
ASR, language and vision models without losing control-path responsiveness or
acting on obsolete results. It began as a complete bachelor's thesis system;
the research work keeps that working baseline intact and introduces new
runtime mechanisms only through measured, reviewable stages.

> 中文简介：本页集中说明项目从本科毕设基线到异步运行时研究的演进。Phase 1 已正式
> 关闭：运行时语义、实模型正确性验证和正式对照均已完成，但未通过冻结的工作负载
> 性能非劣效 Gate，因此不会直接进入整机异步应用。Phase 2 的首个问题固定为有界且
> 完整计费的 Whisper 文件驻留恢复；P2-D3 correctness-pilot contract 与 runner 已完成
> host-only 测试，尚未运行 Jetson pilot 或授权应用接入。

## Research question

The thesis demonstrated that speech, local language inference, scene
description and motion control can coexist on one robot. The research question
is narrower and more demanding:

> Can long-running local inference be isolated from time-constrained robot work
> with bounded ownership and fresh-result semantics, while keeping workload
> performance acceptable on an 8 GB Jetson?

This separates three concerns that a functional demonstration alone cannot
answer:

- **correctness:** tasks terminate in one accountable state and stale results
  cannot reach a consumer;
- **responsiveness:** time-constrained work remains observable while inference
  is active;
- **resource interference:** CPU, GPU and unified-memory pressure do not make
  the workload itself unpredictably expensive.

## Project evolution

| Stage | Question | Outcome | Why it matters |
| --- | --- | --- | --- |
| Thesis baseline | Can the complete robot operate with fully local speech, vision and motion? | Hardware-validated Jetson application, STM32 firmware, UART watchdog and custom driver PCB | Establishes a real system and a preserved comparison point rather than a simulation-only project |
| Phase 0 | What does each synchronous model workload cost under fixed inputs? | Reproducible ASR, LLM and VLM runners, telemetry, validation and formal-analysis tooling | Creates workload identities and measurement discipline before changing the architecture |
| Phase 1 runtime kernel | Can slow work have bounded ownership, cancellation and freshness semantics? | Immutable task/result model, bounded broker, observable worker, lifecycle replay and periodic probe | Makes asynchronous behavior testable instead of relying on threads and console timing |
| Phase 1 Jetson pilots | Do the runtime contracts survive real model paths? | Simulation pilot plus VLM, ASR and LLM correctness pilots; process isolation added for VLM | Closed G5 and exposed where host simulations did not represent Python import and process behavior |
| G6 formal comparison | Does the bounded asynchronous path preserve responsiveness without unacceptable workload cost? | G6 v4 completed in full; lifecycle and responsiveness passed, but workload-performance noninferiority was not established | Provides a valid negative result and prevents unsupported application claims |
| Phase 1 closing diagnostic | What caused the large ASR position effect seen around VLM work? | Six-session diagnostic isolated near-complete loss of warmed Whisper file residency after VLM, followed by storage-backed page faults and repeatable next-invocation ASR cold starts | Converts an unexplained formal effect into a focused systems hypothesis for a separate next study |
| Phase 2 design | Can a bounded, verified Whisper file prefetch restore post-VLM ASR readiness after charging its own cost? | P2-D0 complete; one mitigation, unchanged control and layered decision model reviewed | Turns residency into an explicit resource-management primitive without reopening Phase 1 |

The detailed, immutable evidence for each Jetson study is indexed in
[`experiments/README.md`](../experiments/README.md). The thesis implementation
remains available at
[`v0.1.0-thesis-baseline`](https://github.com/yuzhang-robotics/embodied-robot-heterogeneous-control/tree/v0.1.0-thesis-baseline).

## What Phase 1 established

Phase 1 was not an attempt to prove that “asynchronous is faster.” It tested a
compound systems claim. The runtime part of that claim advanced substantially:

- queue capacity, active ownership and terminal states are explicit;
- state generations and two-stage freshness checks prevent obsolete results
  from being consumed;
- native and spawned processes have recorded termination and reaping facts;
- a 100 ms absolute-schedule probe measures gaps and lateness independently of
  the slow adapter;
- manifests, append-only ledgers, resource traces and independent analyzers
  make the experiments reconstructable.

The formal result also showed the limit of this design. Passing lifecycle and
probe-responsiveness criteria was insufficient to establish acceptable
workload performance. On a unified-memory device, work that is isolated in
control flow can still interfere through shared memory and model residency.
That is the principal finding carried forward from Phase 1.

The v2 and v3 attempts are closed system-under-test failure evidence, not
partial performance evidence or replacement formal results. Each attempt was
closed under its predeclared rules, the repair was validated separately, and
v4 began from a fresh collection. Keeping these boundaries is essential: it
distinguishes debugging the experiment from changing a hypothesis after seeing
its outcome.

## Current conclusion

The current repository supports two statements:

1. The bounded runtime correctly enforces the tested lifecycle, capacity and
   freshness rules, and its asynchronous path met the preregistered software
   probe-responsiveness endpoint for the three fixed workloads.
2. The study did not establish the preregistered workload-performance
   noninferiority margin. Phase 1 therefore did not meet its overall success
   Gate, and the motion-disabled application slice is not authorized.

The follow-up carryover experiment is exploratory mechanism evidence. It does
not reopen G6 v4, convert the negative result into a pass or establish a general
Jetson memory-management claim. Phase 1 is closed with this valid negative
decision; an unmet success Gate is part of the result, not unfinished data
collection.

## Active Phase 2 design boundary

Phase 2 is a separate bounded Whisper residency-restoration study, not an
extension or rerun of Phase 1. The P2-D3 package now adds a frozen nonformal
pair contract, strict target preflight, a lifecycle-closing runner and
host-injected failure tests to the P2-D2 evidence path. No Jetson pilot data
have been collected, and machine preregistration remains inactive. The reviewed
direction is intentionally narrow:

1. compare one bounded, verified sequential prefetch of the frozen Whisper
   model file with an unchanged post-VLM control;
2. charge the prefetch's own time between the common post-VLM boundary and ASR
   result availability;
3. keep study validity, mechanism evidence, operational benefit,
   responsiveness and application authority as separate decisions;
4. measure page residency, faults, recovery, memory, power and thermal cost,
   without turning every secondary statistic into a formal success Gate;
5. close the first comparison after one valid positive, negative or
   inconclusive result rather than tuning until it passes.

The complete decision, causal model and anti-tuning rules are recorded in the
[Phase 2 design](architecture/phase2-residency-restoration-design.md).

An application slice becomes reasonable only after the completed cost evidence
and a later application-readiness review explicitly authorize it. Later work
can then connect timestamped acquisition, bounded inference lanes and a safety
supervisor to the live robot while preserving the STM32 watchdog. Encoder
feedback, closed-loop wheel-speed control and full mecanum motion are separate
control research topics and should not be mixed into the resource-management
experiment.

## Evidence and claim boundaries

- Fixed-input experiments measure systems behavior, not model quality.
- One physical Jetson does not establish population-wide hardware behavior.
- Python and Linux measurements are not hard-real-time guarantees.
- Raw inputs, model text, private paths, service logs and complete run archives
  remain outside Git; tracked reports contain validated derived evidence.
- Negative and failed-attempt records remain part of the research history and
  are not rewritten to simplify the narrative.

The architectural boundary is summarized in
[`docs/architecture/README.md`](architecture/README.md). Formal runtime and G6
details remain in the
[`Phase 1 runtime contract`](architecture/phase1-runtime-contract.md) and
[`G6 preregistration`](architecture/phase1-formal-preregistration.md).
