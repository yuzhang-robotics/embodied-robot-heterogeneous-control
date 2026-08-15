"""Runtime configuration shared by the Jetson modules."""

from __future__ import annotations

import os
from pathlib import Path


PACKAGE_DIR = Path(__file__).resolve().parent
ASSETS_DIR = PACKAGE_DIR / "assets"
RUNTIME_DIR = Path(
    os.environ.get("ROBOT_RUNTIME_DIR", str(PACKAGE_DIR / "runtime"))
).expanduser()


def _path_from_env(name: str, default: Path) -> Path:
    return Path(os.environ.get(name, str(default))).expanduser()


def _flag_from_env(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


# Audio and camera devices
MIC_DEVICE = os.environ.get("ROBOT_MIC_DEVICE", "plughw:1,0")
CAMERA_INDEX = int(os.environ.get("ROBOT_CAMERA_INDEX", "0"))
FRAME_WIDTH = 640
FRAME_HEIGHT = 480

# Local inference services
LLAMA_API_URL = os.environ.get(
    "ROBOT_LLAMA_API_URL", "http://127.0.0.1:8080/v1/chat/completions"
)
OLLAMA_CHAT_URL = os.environ.get(
    "ROBOT_OLLAMA_CHAT_URL", "http://127.0.0.1:11434/api/chat"
)
VLM_MODEL = os.environ.get("ROBOT_VLM_MODEL", "moondream")

# Offline model paths
WHISPER_DIR = _path_from_env("ROBOT_WHISPER_DIR", Path.home() / "whisper.cpp")
WHISPER_BIN = _path_from_env(
    "ROBOT_WHISPER_BIN", WHISPER_DIR / "build-cuda/bin/whisper-cli"
)
WHISPER_ASR_MODEL = _path_from_env(
    "ROBOT_WHISPER_MODEL", WHISPER_DIR / "models/ggml-small.bin"
)
PIPER_MODEL = _path_from_env(
    "ROBOT_PIPER_MODEL", Path.home() / "zh_CN-huayan-medium.onnx"
)

KWS_MODEL_DIR = _path_from_env(
    "ROBOT_KWS_MODEL_DIR",
    Path.home()
    / "sherpa_onnx_models"
    / "sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01",
)
KWS_TOKENS = KWS_MODEL_DIR / "tokens.txt"
KWS_ENCODER = KWS_MODEL_DIR / "encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx"
KWS_DECODER = KWS_MODEL_DIR / "decoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx"
KWS_JOINER = KWS_MODEL_DIR / "joiner-epoch-12-avg-2-chunk-16-left-64.int8.onnx"
KWS_KEYWORDS = ASSETS_DIR / "keywords_zhangyuhao.txt"

# Jetson-STM32 communication
SERIAL_PORT = os.environ.get("ROBOT_SERIAL_PORT", "/dev/ttyTHS1")
BAUD_RATE = int(os.environ.get("ROBOT_BAUD_RATE", "115200"))

# Physical motion is disabled unless explicitly enabled for a hardware test.
ENABLE_MOTION = _flag_from_env("ROBOT_ENABLE_MOTION", default=False)
