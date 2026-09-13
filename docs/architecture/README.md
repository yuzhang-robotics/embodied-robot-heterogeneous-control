# System Architecture

This page separates the hardware-validated thesis architecture from the closed
Phase 1 research runtime and the active Phase 2 resource-management design.
The synchronous application remains the reproducible robot baseline; the
experimental runtime has not replaced it.

> 中文简介：本页只描述三类边界：已验证的同步整机架构、已经关闭的 Phase 1 实验运行时，
> 以及已经进入设计阶段的 Phase 2 驻留恢复研究。Phase 1 的正式成功 Gate 未通过，因此
> 异步运行时尚未接入整机应用；Phase 2 的实现、预注册和数据采集也尚未开始。

## Hardware-validated baseline

The Jetson application is one Python process. It moves through wake-word
detection, recording, ASR and one selected task before returning to the
wake-word loop.

~~~mermaid
flowchart LR
    Mic["USB microphone"] --> KWS["sherpa-onnx KWS"]
    KWS --> Record["ALSA recording"]
    Record --> ASR["whisper.cpp ASR"]
    ASR --> Intent["Rule-based intent routing"]
    Intent --> Chat["llama.cpp / Qwen"]
    Intent --> VLM["Camera + Ollama / Moondream"]
    Intent --> Tracker["OpenCV color tracker"]
    Chat --> TTS["Piper TTS"]
    VLM --> TTS
    Tracker --> UART["UART command + response"]
    Intent --> UART
    UART <--> STM32["STM32 protocol and chassis layer"]
    STM32 --> PWM["20 kHz PWM + direction outputs"]
    Encoders["Four quadrature encoders"] --> STM32
~~~

Dialogue and scene-description services are local, but the application waits
for their responses synchronously. The color-target routine also owns the
speech loop until it arrives, times out or fails.

The STM32 executes independently of Jetson inference. Its main loop parses
USART3 frames, TIM13 samples encoders and advances the command watchdog every
10 ms, and TIM8 generates motor PWM in hardware.

## Timing domains

| Domain | Baseline timing | Responsibility |
| --- | --- | --- |
| Jetson conversation path | Event-driven and blocking | KWS, recording, ASR, dialogue/VLM and TTS |
| Jetson color controller | Nominal 100 ms loop | Target detection and discrete motion decision |
| Jetson command refresh | At most 300 ms between unchanged commands | Keep the STM32 watchdog alive during tracking |
| STM32 periodic interrupt | 10 ms | Encoder update, heartbeat and watchdog tick |
| STM32 command timeout | About 1.2 s | Stop motors after loss of valid commands |
| Motor PWM | 20 kHz | Four driver channels |

These are configuration targets, not measured Jetson worst-case guarantees.
Camera I/O, model calls, Python execution and operating-system scheduling can
delay the high-level loop.

## Data, control and safety boundaries

| Boundary | Owner | Contract |
| --- | --- | --- |
| Raw audio and camera frames | Jetson application | Consumed locally; runtime files are not published |
| Model state and requests | whisper.cpp, llama.cpp and Ollama | Managed through subprocess or loopback HTTP interfaces |
| Motion decision | Jetson application | One discrete direction and speed command |
| Frame validation | STM32 | Strict line parser with <code>A</code>/<code>E</code> response |
| Communication-loss stop | STM32 | Independent valid-command watchdog |
| Physical-motion opt-in | Jetson configuration | Disabled by default; invalid settings fail closed |
| Encoder samples | STM32 | Updated every 10 ms; not yet part of closed-loop PWM |

The wire contract is documented in
[protocol/README.md](../../protocol/README.md). Phase 1 experiments do not
import the chassis communication path, open <code>/dev/ttyTHS1</code> or
require motor power.

## Phase 1 research runtime

Phase 1 introduced a hardware-independent path for fixed-input systems
experiments. It makes ownership and validity observable rather than assuming
that moving work to a thread is sufficient.

~~~mermaid
flowchart LR
    Submit["Task + state generation + deadline"] --> Broker["Bounded broker"]
    Broker --> Worker["Single observable executor"]
    Worker --> Adapter["Simulated or real workload adapter"]
    Adapter --> Mailbox["Bounded result mailbox"]
    Mailbox --> Check["Freshness and state check"]
    Check --> Consume["Explicit consumption or rejection"]
    Probe["100 ms absolute-schedule probe"] -.-> Worker
    Trace["Append-only events + resource telemetry"] -.-> Broker
    Trace -.-> Worker
    Trace -.-> Check
~~~

The implemented boundary includes:

- immutable task, result, state and payload-reference records;
- bounded pending, active and accepted-result ownership;
- explicit cancellation, terminal states and finite worker shutdown;
- state generations and freshness checks before both publication and
  consumption;
- independent lifecycle replay from schema-validated events;
- a periodic probe and continuous Jetson resource sampling;
- simulated and fixed-input VLM, ASR and LLM adapters;
- fail-closed session protocols, manifests, validators and independent
  analyzers.

The reusable kernel is in
[jetson/phase1_runtime/](../../jetson/phase1_runtime/). Experiment orchestration
is in [experiments/phase1/](../../experiments/phase1/). The full semantic
contract remains in
[phase1-runtime-contract.md](phase1-runtime-contract.md).

## What the architecture evidence means

The completed G6 v4 comparison showed that the asynchronous path met the frozen
lifecycle and software probe-responsiveness criteria. It did not establish the
workload-performance noninferiority margin for ASR, LLM or VLM. This means
control-flow isolation worked at the tested boundary, but it was not sufficient
to authorize application integration.

The later carryover diagnostic identified a shared-resource path that the
runtime did not manage: VLM work nearly eliminated warmed Whisper model-file
residency, so the next ASR invocation incurred storage-backed page faults and a
cold-start delay. The
[formal result](../../experiments/phase1/results/20260907T051448Z_phase1_formal_g6_v4/)
and
[carryover result](../../experiments/phase1/results/20260908T072640Z_phase1_asr_vlm_carryover_v2/)
retain the numerical evidence and limitations.

These two records close Phase 1. Its runtime and evidence remain reusable, but
its formal collection will not be rerun and its unexecuted application slice is
not carried forward as unfinished Phase 1 work.

## Active Phase 2 design boundary

Phase 2 is active as a separate bounded Whisper residency-restoration study.
The P2-D3 package prepares one nonformal control/treatment correctness pair,
strict target preflight and closed process, probe, sampler and evidence
lifecycles. It charges the action through measured ASR result availability.
No target data have been collected; machine preregistration and commissioning
remain inactive until their preceding work packages are reviewed.

~~~mermaid
flowchart LR
    Requests["Bounded workload requests"] --> Policy["Residency and resource policy"]
    Policy --> ASR["ASR process"]
    Policy --> LLM["LLM service"]
    Policy --> VLM["VLM service"]
    ASR --> Observe["Latency, residency, faults, memory and power"]
    LLM --> Observe
    VLM --> Observe
    Observe --> Policy
~~~

The design accounts for the action's own time, memory pressure and power cost.
It separates evidence validity, mechanism support, operational benefit and
application authority rather than making every secondary resource statistic
part of one performance Gate. The complete contract is in the
[Phase 2 residency-restoration design](phase2-residency-restoration-design.md).

Only after the completed cost evidence and a later application-readiness
review provide explicit authority should live acquisition, inference and
supervision be separated into timestamped bounded lanes. The STM32 watchdog
remains the independent last line of defense. Encoder feedback and full
mecanum kinematics belong to a later control study and should not be added to
the resource experiment.

## Reference documents

- [Research roadmap](../research-roadmap.md)
- [Phase 1 runtime contract](phase1-runtime-contract.md)
- [G6 formal preregistration](phase1-formal-preregistration.md)
- [ASR/VLM carryover design](phase1-asr-vlm-carryover-diagnostic.md)
- [Phase 2 residency-restoration design](phase2-residency-restoration-design.md)
- [Experiment and evidence index](../../experiments/README.md)
