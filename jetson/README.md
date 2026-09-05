# Jetson Runtime

The Jetson package contains the high-level runtime from the bachelor's thesis: offline speech interaction, local language and vision inference, color-target motion planning, and UART communication with the STM32. It also contains the host-testable Phase 1 task-runtime kernel. The experiment layer now connects that kernel to the fixed Phase 0 VLM, ASR and LLM inputs without changing the robot application.

The thesis application was validated on a Jetson Orin Nano Super 8GB running Ubuntu 22.04 and Python 3.10.12. It remains the synchronous reference implementation. The Phase 1 package currently covers host-safe task identity, bounded ownership, a single-worker observable executor and a software periodic probe. The experiment harness under `experiments/phase1/` has completed one motion-disabled simulation pilot, threaded and spawned-process fixed-input VLM correctness pilots, one native-subprocess ASR correctness pilot and one local-server LLM correctness pilot on the Jetson. The threaded real-model pilot recorded skipped probe releases during lazy Python module import. The process VLM pilot completed both lifecycle conditions with normally reaped children and no skipped probe releases. The ASR pilot completed nominal transcript-identity consumption and stale cancellation with local Whisper termination and reaping. The LLM pilot completed nominal response-identity consumption and rejected the old-generation response while keeping prompt and response text private; it does not claim that state invalidation stopped the blocking HTTP wait or server-side inference. These three real-workload components close G5. The amended G6 v2 protocol fixes the formal paired comparison. Its protocol-bound formal runner and independent analyzer are implemented. Commissioning exposed an LLM empty-history mismatch before measurement and then a resource-trace tail race after one complete session. An outcome-independent schedule audit also found a repeated cross-session order relationship in v1. Neither collection is admissible formal evidence; v2 replaces only the fixed condition-order matrix. Jetson formal collection and application integration remain research work.

> 中文简介：本目录包含“章鱼号”的 Jetson 端运行程序，负责离线语音交互、本地大模型与视觉模型调用、颜色目标接近和 STM32 串口通信。当前整机应用仍使用已验证的同步路径；Phase 1 已完成线程版与进程隔离版固定输入 VLM、固定输入 ASR 以及固定输入 LLM 的 Jetson correctness pilot，G5 已关闭；修订后的 G6 v2 正式协议冻结交叉平衡的正式对照，协议绑定的正式 runner 与独立分析器已实现；commissioning 先后发现正式测量前的 LLM 空历史身份不一致、一个完整 session 后的资源轨迹尾部竞态，结果无关顺序审计还发现 v1 的跨 session 顺序关系重复；两次 collection 均不作为正式证据，v2 仅替换条件顺序矩阵，运动控制接入尚未开始。

## Modules

| Path | Role |
| --- | --- |
| `app.py` | Wake word, recording, ASR, intent routing, dialogue and TTS orchestration |
| `config.py` | Device, model, service and runtime paths |
| `vision_vlm.py` | Camera capture, Moondream scene description and Chinese post-processing |
| `vision_color.py` | HSV segmentation and target extraction |
| `motion_planner.py` | Discrete visual feedback loop for approaching a colored object |
| `robot_comm.py` | Command mapping, UART transport, STM32 responses and motion log |
| `phase1_runtime/` | Host-testable contracts, bounded broker, observable worker and periodic probe |
| `assets/` | sherpa-onnx keyword configuration and wake acknowledgment audio |
| `scripts/` | One-time environment setup helpers |

## Tested environment

- Ubuntu 22.04 on Jetson Orin Nano Super 8GB
- Python 3.10.12
- OpenCV 4.5.4 from the Jetson system image
- `arecord` and `aplay` from ALSA utilities
- CUDA-enabled `whisper-cli` from whisper.cpp
- llama.cpp server for Qwen dialogue and Chinese rewriting
- Ollama with the `moondream` vision model
- Piper Chinese TTS model
- Python versions recorded in [`requirements.txt`](requirements.txt)

The repository does not redistribute model weights. Paths in `config.py` reflect the tested machine and can be overridden with environment variables.

## Installation

From the repository root on the Jetson:

```bash
python3 -m pip install --user -r jetson/requirements.txt
python3 jetson/scripts/install_argos_en_zh.py
```

OpenCV is expected to come from the Jetson/Ubuntu installation. Install ALSA utilities separately if `arecord` or `aplay` is missing.

The default model locations are:

```text
~/whisper.cpp/build-cuda/bin/whisper-cli
~/whisper.cpp/models/ggml-small.bin
~/zh_CN-huayan-medium.onnx
~/sherpa_onnx_models/sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01/
```

## Local inference services

The application expects two services to be started separately:

| Service | Default endpoint | Purpose |
| --- | --- | --- |
| llama.cpp | `http://127.0.0.1:8080/v1/chat/completions` | Qwen dialogue and Chinese scene-description rewriting |
| Ollama | `http://127.0.0.1:11434/api/chat` | Moondream image description |

The exact llama.cpp launch flags depend on the local model and build. Bind both
services to loopback rather than a wildcard interface. Confirm the listeners
and model identities before starting the robot application:

```bash
ss -ltnp | grep -E ':(8080|11434)'
curl --max-time 5 http://127.0.0.1:8080/v1/models
curl --max-time 5 http://127.0.0.1:11434/api/tags
```

## Run safely

Run the package from the repository root:

```bash
python3 -m jetson.app
```

Motion output is disabled by default. Commands are printed and appended to `jetson/runtime/motion_commands.log`, but the serial port is not opened.

Only enable the physical base after checking the 3.3 V UART wiring, STM32 firmware, command watchdog and physical motor-power switch. Keep all four wheels off the ground for the first test:

```bash
ROBOT_ENABLE_MOTION=1 python3 -m jetson.app
```

Press `Ctrl+C` to exit. The application sends a final stop command when leaving an active motion routine.

## Configuration

| Environment variable | Default |
| --- | --- |
| `ROBOT_RUNTIME_DIR` | `jetson/runtime/` |
| `ROBOT_MIC_DEVICE` | `plughw:1,0` |
| `ROBOT_CAMERA_INDEX` | `0` |
| `ROBOT_LLAMA_API_URL` | `http://127.0.0.1:8080/v1/chat/completions` |
| `ROBOT_OLLAMA_CHAT_URL` | `http://127.0.0.1:11434/api/chat` |
| `ROBOT_VLM_MODEL` | `moondream` |
| `ROBOT_WHISPER_DIR` | `~/whisper.cpp` |
| `ROBOT_WHISPER_BIN` | `<WHISPER_DIR>/build-cuda/bin/whisper-cli` |
| `ROBOT_WHISPER_MODEL` | `<WHISPER_DIR>/models/ggml-small.bin` |
| `ROBOT_PIPER_MODEL` | `~/zh_CN-huayan-medium.onnx` |
| `ROBOT_KWS_MODEL_DIR` | tested sherpa-onnx KWS directory under `~/sherpa_onnx_models/` |
| `ROBOT_SERIAL_PORT` | `/dev/ttyTHS1` |
| `ROBOT_BAUD_RATE` | `115200` |
| `ROBOT_ENABLE_MOTION` | disabled |

Audio, ASR text, captured images and communication logs are written beneath `ROBOT_RUNTIME_DIR` and ignored by Git.

## Baseline limitations

- The orchestration path is blocking and single-process; inference tasks do not have explicit deadlines or cancellation.
- The Phase 1 VLM, ASR and LLM experiments use fixed inputs and are not connected to live acquisition, TTS or the synchronous application loop.
- The spawned-process VLM path is an experiment runner boundary, not an application worker pool; its two Jetson observations are descriptive correctness evidence rather than a formal timing result.
- llama.cpp and Ollama are managed outside the application.
- The color tracker uses fixed HSV ranges and discrete motion commands tuned on the thesis robot.
- The Jetson sends speed percentages, while the STM32 applies open-loop PWM rather than closed-loop wheel velocity.
- The application has no ROS 2 interface or formal experiment recorder yet.

These limitations define the starting point for the asynchronous inference and real-time control study described in the [architecture notes](../docs/architecture/README.md).
