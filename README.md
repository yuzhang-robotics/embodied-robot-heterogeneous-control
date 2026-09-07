# Embodied Robot Heterogeneous Control

**A hardware-validated Jetson–STM32 wheeled robot, evolving from a bachelor's thesis prototype into a research platform for asynchronous inference and real-time control.**

![Octopus wheeled robot](docs/hardware/assets/robot-overview.png)

This repository began with my bachelor's thesis on speech interaction and visual perception for a wheeled robot. The thesis system runs fully local speech and vision pipelines on a Jetson Orin Nano, while an STM32F407 executes motion commands and enforces a communication watchdog. The same platform will now be used to study how long-running perception and inference can coexist with predictable control timing.

> 中文简介：本仓库记录“章鱼号”轮式机器人的本科毕设基线，并在同一平台上继续研究异构计算架构下的异步推理、实时控制与系统评测。当前版本已经完成 Jetson、STM32 和自制底层驱动板的整机验证；Phase 1 已建立有界运行时、可观测 worker、周期探针和模拟实验运行器，并完成 VLM、ASR 与 LLM 的固定输入 Jetson correctness pilot。G6 v3 的首次正式尝试在第 10 个条目因 VLM Qwen 30 秒超时而触发两个系统被测对象 Gate 失败；该协议永久关闭且不重跑或替换。修正后的 VLM 仓库路径已在 Jetson 上完成直接验证，G6 v4 现冻结该修复并在评审合并后从 session 1 开始全新正式采集。Phase 1 仍未完成，在 v4 对照通过前不进入整机应用切片。

## Project status

| Item | Status |
| --- | --- |
| Bachelor's thesis software and firmware | Complete and hardware validated |
| Custom base-driver PCB | Published and tested on the physical robot |
| Jetson–STM32 command link | Validated with ACK/error responses and a 1.2 s command watchdog |
| Asynchronous inference runtime | Runtime and fixed-input correctness Gates complete; G6 v3 closed after a system-under-test VLM failure; repaired VLM path validated and frozen in G6 v4; formal comparison and application slice pending |

The validated Jetson–STM32 code is preserved at [`v0.1.0-thesis-baseline`](https://github.com/yuzhang-robotics/embodied-robot-heterogeneous-control/tree/v0.1.0-thesis-baseline). The current `main` branch also includes the reviewed hardware documentation and editable PCB export.

## System at a glance

```mermaid
flowchart LR
    Inputs["Microphone + camera"] --> Jetson["Jetson Orin Nano Super 8GB<br/>speech, vision, local inference, task logic"]
    Jetson <-->|"UART · 115200 8N1"| STM32["STM32F407ZGT6<br/>command parsing, PWM, encoders, watchdog"]
    STM32 --> Driver["Custom PCB · 2 × TB6612FNG"]
    Driver --> Base["Four-motor X-layout mecanum base"]
```

| Layer | Baseline implementation |
| --- | --- |
| Speech input | sherpa-onnx keyword spotting and CUDA-enabled whisper.cpp ASR |
| Dialogue | Qwen2.5-1.5B served locally by llama.cpp |
| Scene description | Moondream through Ollama, followed by local Chinese rewriting or Argos translation |
| Speech output | Piper TTS |
| Target approach | OpenCV HSV detection and a discrete visual feedback loop |
| Motion transport | Line-based ASCII commands over 3.3 V TTL UART |
| Low-level execution | STM32F407, 20 kHz motor PWM, 10 ms encoder sampling and command-loss stop |
| Hardware | Custom two-layer base-driver PCB and four MG513P30_12V geared motors |

Model weights and device-specific runtime data are intentionally not stored in Git.

## What has been validated

The baseline has been exercised on the assembled robot rather than only in simulation:

- offline wake word, recording, speech recognition, local dialogue and speech synthesis;
- camera scene description with a local vision-language model;
- voice-triggered color-target approach (red tested end to end; red, blue, green and yellow classes implemented);
- `F/B/L/R/S` chassis commands, with `L/R` defined as in-place rotation;
- STM32 `A` acknowledgment for valid frames and `E` for malformed frames;
- automatic motor stop about 1.2 seconds after valid commands cease;
- four encoder channels, calibrated so forward wheel motion has a consistent sign;
- Keil MDK build, CMSIS-DAP flashing and end-to-end Jetson–STM32 testing on the custom PCB.

The STM32 currently applies open-loop PWM commands. Encoder measurements are available, but closed-loop wheel-speed control and full mecanum kinematics are not part of this thesis baseline.

## From the thesis baseline to the research platform

The current Jetson application is synchronous: wake-word detection, recording, ASR, dialogue or VLM inference, speech output and target tracking run as blocking stages. This is straightforward to reproduce, but inference latency can delay unrelated work and there is no explicit deadline or data-age policy.

Phase 1 now has an immutable task/result model, bounded task ownership, scoped
state generations, a single-worker observable executor, an absolute-schedule
periodic probe, independent lifecycle trace replay and a reproducible
simulated-condition runner. A fail-closed Jetson preflight, continuous
`tegrastats` recorder and pilot-session validator are also implemented. A
motion-disabled Jetson simulation pilot completed all session Gates; its
[descriptive report](experiments/phase1/results/20260828T121142Z_phase1_jetson_pilot/)
separates simulated timing behavior from the additional bounded-runtime
semantics. A fixed-input Moondream/Qwen correctness pilot also completed both
nominal consumption and old-generation rejection. Its
[descriptive report](experiments/phase1/results/20260830T073825Z_phase1_vlm_pilot/)
shows that all skipped releases occurred during lazy module import in the
independent Python probe, so thread-level timing isolation does not generalize
from the simulated sleep workload. A spawned-process VLM adapter now keeps the
broker, freshness checks and periodic probe in the parent process while moving
the lazy adapter path behind bounded IPC. A subsequent
[process-isolated correctness pilot](experiments/phase1/results/20260830T122541Z_phase1_vlm_process_reaping/)
completed both real-model conditions with normally reaped children, zero stale
consumption and no skipped 100 ms probe releases. The earlier thread reference
recorded 148 skipped releases, but this cross-session single-run contrast is a
descriptive mitigation signal rather than a causal comparison. No pilot
authorizes a performance-superiority, hard-real-time or heterogeneous-inference
claim. The subsequent fixed-input ASR slice reuses the exact Phase 0 WAV and
Whisper identities, supervises `whisper-cli` as the backend process, confirms
termination and reaping after state invalidation, and keeps transcript text out
of artifacts. Its independently derived
[correctness-pilot report](experiments/phase1/results/20260831T140705Z_phase1_asr_pilot_v2/)
records one nominal consumption and one stale rejection with all Gates passing.
This closes the ASR component of G5. The subsequent fixed-input LLM slice
reuses the Phase 0 prompt, Qwen model and llama.cpp request contract, keeps
prompt and response text out of artifacts, and rejects old-generation output
without claiming that the blocking HTTP wait or resident server was preempted.
Its independently derived
[correctness-pilot report](experiments/phase1/results/20260901T143315Z_phase1_llm_pilot/)
records one nominal consumption and one stale rejection with all Gates passing.
This closes the LLM component and G5 overall. The
[G6 formal preregistration](docs/architecture/phase1-formal-preregistration.md)
freezes the numerical thresholds, cross-balanced order, sample size, exclusions
and statistical methods. V1 and the failed v2 and v3 attempts remain immutable.
The amended v4 protocol retains the complete v3 scientific design while freezing
the deterministic VLM request, 60 s Qwen boundary and positive unload-confirmation
contract. A protocol-bound formal session runner and
independent analyzer reject protocol, schedule, environment, artifact or
statistical-method drift. Jetson commissioning exposed an LLM empty-history
identity mismatch before measurement and a resource-trace tail race after one
complete session. An outcome-independent schedule audit then found that v1
repeated the same condition/predecessor relationship in all five sessions. The
two collections remain diagnostic; v2 changes only the fixed condition-order
matrix while retaining the sample size, hypotheses, thresholds and analysis.

The first v2 formal attempt stopped at measured ordinal 18 when a VLM Qwen
rewrite reached its 30 s client timeout and used the disallowed Argos fallback.
Its independently reconstructed
[failure report](experiments/phase1/results/20260905T140816Z_phase1_formal_g6_v2/)
verifies all 42 manifest artifacts, the ledger prefix, 3,558 resource samples,
17 completed runs and the single failed Gate. The VLM child exited normally,
the llama-server slot returned to idle and no thermal or telemetry failure was
observed. The recorded implementation requested Moondream unload only after
the Qwen attempt, leaving a residency-order confound. G6 v2 is closed: the
attempt will not be rerun or replaced and cannot support a formal claim. The
isolated correction moves the unload request before Qwen while retaining the
30 s timeout. A separate
[residency-order diagnostic](experiments/phase1/results/20260905T160805Z_phase1_vlm_residency_diag/)
then completed both Qwen paths in about 18.4--18.9 s with all slice/process
Gates passing and no llama-server cancellation record. This single fixed-order
diagnostic supports implementation readiness, not causality or performance
superiority. G6 v3 bound that order and process protocol `0.2.0` while retaining
the v2 schedule, hypotheses, sample size, thresholds and analysis. Its first
formal attempt stopped at measured ordinal 10 when the synchronous VLM Qwen
request crossed the 30 s boundary and the Argos fallback failed
`translation_route_verified` and `residency_contract_verified`. The
independently reconstructed
[v3 failure report](experiments/phase1/results/20260906T055511Z_phase1_formal_g6_v3/)
verifies the preserved artifacts, ledger prefix, resource trace, child-process
closure and five llama-server request lifecycles. G6 v3 is closed without a
rerun or replacement. The incomplete matrix permits no synchronous/asynchronous
performance conclusion, does not meet G6, and does not authorize the Phase 1
application slice. A subsequent three-repetition
[timeout-repair diagnostic](experiments/phase1/results/20260906T082627Z_phase1_vlm_timeout_diag/)
held the llama-server arguments fixed, made both requests deterministic, polled
for Moondream absence and extended the Qwen client boundary to 60 s. All three
Qwen requests completed in 10.2--21.8 s with matching server releases and no
cancellation. This descriptive evidence supported the repair design but used
an inline harness. A subsequent
[target validation](experiments/phase1/results/20260906T101723Z_phase1_vlm_timeout_repair_validation/)
directly exercised the modified repository path in both VLM lifecycle
conditions. Both unloads were confirmed, both Qwen rewrites completed in
23.7--26.9 s, and all slice and process Gates passed. This single fixed-order
correctness validation is not a formal performance result. G6 v4 freezes the
reviewed repair without reusing or reclassifying any v3 run; its merge activates
a fresh collection from session 1. Phase 1 remains incomplete until that formal
comparison and the subsequent application slice are completed.

All pilot timings are descriptive, not formal performance or cancellation-
latency data. Future research stages may investigate:

- independent acquisition, inference, planning and control workers;
- timestamped bounded queues, cancellation and stale-result rejection;
- CPU/GPU resource arbitration between ASR, language and vision models;
- a safety supervisor that remains responsive while inference is busy;
- measurements of latency, jitter, control frequency, missed deadlines, memory and power;
- encoder-based feedback and a broader mecanum motion interface after the runtime boundary is stable.

These are research objectives, not claims about the current implementation. The present code and hardware form the reproducible comparison baseline.

## Repository map

| Path | Contents |
| --- | --- |
| [`jetson/`](jetson/) | Speech, vision, local inference, task logic and STM32 communication |
| [`firmware/stm32f407/`](firmware/stm32f407/) | Keil/SPL firmware for motor drive, encoders, protocol handling and safety timeout |
| [`protocol/`](protocol/) | UART frame format, responses and timing contract |
| [`hardware/pcb/`](hardware/pcb/) | Editable EasyEDA Pro export of the custom base-driver PCB |
| [`docs/hardware/`](docs/hardware/) | Physical platform, wiring, pin assignments, power and safety notes |
| [`docs/architecture/`](docs/architecture/) | Current timing model and the boundary of the planned asynchronous architecture |
| [`experiments/phase0/`](experiments/phase0/) | Synchronous fixed-input measurement, validation and formal analysis tools |
| [`experiments/phase1/`](experiments/phase1/) | Host tests, simulation, fixed-input workload runners, Jetson pilots, the G6 formal session runner and independent analysis, trace schemas and validation |

## Reproducing the baseline

Start with the component-specific notes:

1. [Jetson runtime and model services](jetson/README.md)
2. [STM32 firmware build and flashing](firmware/stm32f407/README.md)
3. [UART protocol](protocol/README.md)
4. [Hardware wiring and power safety](docs/hardware/README.md)
5. [Editable PCB source](hardware/pcb/README.md)

Physical motion is disabled by default in the Jetson code. Keep the wheels off the ground during first tests and review the power and UART voltage requirements before enabling motor output.

## License

Original software and documentation are released under the [MIT License](LICENSE). Editable hardware sources under `hardware/` use [CERN-OHL-P-2.0](hardware/LICENSE); see [hardware/NOTICE](hardware/NOTICE). STMicroelectronics and Arm support files retain their own notices, documented in [`firmware/stm32f407/THIRD_PARTY_NOTICES.md`](firmware/stm32f407/THIRD_PARTY_NOTICES.md). Models, datasets and external tools follow their respective licenses.
