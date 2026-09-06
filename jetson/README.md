# Jetson Runtime

The Jetson package contains the high-level runtime from the bachelor's thesis: offline speech interaction, local language and vision inference, color-target motion planning, and UART communication with the STM32. It also contains the host-testable Phase 1 task-runtime kernel. The experiment layer now connects that kernel to the fixed Phase 0 VLM, ASR and LLM inputs without changing the robot application.

The thesis application was validated on a Jetson Orin Nano Super 8GB running Ubuntu 22.04 and Python 3.10.12. It remains the synchronous reference implementation. The Phase 1 package covers host-safe task identity, bounded ownership, a single-worker observable executor and a software periodic probe. Its motion-disabled simulation and fixed-input VLM, ASR and LLM correctness pilots close G5. G6 v2 stopped on a VLM Qwen timeout before the residency-order correction and is permanently closed. A separate diagnostic then validated both corrected Qwen paths within the retained timeout. G6 v3 bound that corrected order and process protocol `0.2.0`, but its first formal attempt stopped at measured ordinal 10 when the synchronous VLM Qwen request crossed the same 30 s boundary and failed two system-under-test Gates. V3 is closed without rerun or replacement. The incomplete matrix permits no sync/async performance conclusion and G6 is not met. A three-repetition timeout diagnostic supports deterministic requests, explicit Ollama unload polling and a 60 s Qwen client boundary. The modified repository path was then exercised directly on the Jetson in both VLM lifecycle conditions; both unloads were confirmed, both Qwen routes completed and all slice/process Gates passed. The repair is ready for review, but Phase 1 remains incomplete and application integration is not authorized.

> 中文简介：本目录包含“章鱼号”的 Jetson 端运行程序，负责离线语音交互、本地大模型与视觉模型调用、颜色目标接近和 STM32 串口通信。当前整机应用仍使用已验证的同步路径；Phase 1 已完成线程版与进程隔离版固定输入 VLM、固定输入 ASR 以及固定输入 LLM 的 Jetson correctness pilot，G5 已关闭。G6 v3 的首次正式尝试在第 10 个条目因同步 VLM Qwen 请求超过 30 秒而触发两个系统被测对象 Gate 失败；v3 不重跑或替换，不支持同步/异步性能结论。当前已完成三次修复契约诊断，并在 Jetson 上直接验证了修改后的仓库路径；两个 VLM 生命周期条件均确认卸载、完成 Qwen 路径并通过全部切片与进程 Gate。修复已具备评审条件，但 Phase 1 尚未完成，运动控制接入不获授权。

## Modules

| Path | Role |
| --- | --- |
| `app.py` | Wake word, recording, ASR, intent routing, dialogue and TTS orchestration |
| `config.py` | Device, model, service and runtime paths |
| `vision_vlm.py` | Camera capture, Moondream scene description and Chinese post-processing |
| `vlm_request_contract.py` | Deterministic VLM request parameters and bounded Ollama unload confirmation |
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
