"""Request and residency contract for the local VLM pipeline."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping


VLM_REQUEST_CONTRACT_VERSION = "0.1.0"
VLM_REQUEST_SEED = 20_260_906
MOONDREAM_REQUEST_TEMPERATURE = 0.0
MOONDREAM_REQUEST_NUM_PREDICT = 100
MOONDREAM_REQUEST_TIMEOUT_S = 180
QWEN_REQUEST_TEMPERATURE = 0.0
QWEN_REQUEST_MAX_TOKENS = 96
QWEN_REQUEST_TIMEOUT_S = 60
MODEL_UNLOAD_TIMEOUT_S = 20
MODEL_UNLOAD_POLL_INTERVAL_S = 0.1

MOONDREAM_PROMPTS = (
    "Describe this image briefly.",
    "What is in this image?",
    "Describe the main objects and scene in this image in one sentence.",
)
QWEN_SYSTEM_PROMPT = (
    "你是一个中文视觉描述助手。"
    "你的任务是把英文图像描述改写成自然、简短、适合语音播报的中文。"
    "不要逐字硬翻译，不要解释。"
    "如果英文里出现 urn、vase、container、cup、bottle 等不确定物体，"
    "不要翻译成骨灰、骨灰盒；优先翻译成容器、杯子、瓶子或物体。"
    "如果英文描述不确定，就用保守说法。"
)
QWEN_USER_PREFIX = "请把下面这句英文图像描述改写成自然中文，控制在一到两句话：\n"


def build_moondream_payload(
    model: str,
    prompt: str,
    image_base64: str,
) -> dict[str, object]:
    """Build one deterministic Ollama chat request."""

    return {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": prompt,
                "images": [image_base64],
            }
        ],
        "stream": False,
        "options": {
            "temperature": MOONDREAM_REQUEST_TEMPERATURE,
            "seed": VLM_REQUEST_SEED,
            "num_predict": MOONDREAM_REQUEST_NUM_PREDICT,
        },
    }


def build_qwen_payload(english_text: str) -> dict[str, object]:
    """Build one deterministic OpenAI-compatible rewrite request."""

    return {
        "model": "qwen",
        "messages": [
            {"role": "system", "content": QWEN_SYSTEM_PROMPT},
            {"role": "user", "content": QWEN_USER_PREFIX + english_text},
        ],
        "temperature": QWEN_REQUEST_TEMPERATURE,
        "seed": VLM_REQUEST_SEED,
        "max_tokens": QWEN_REQUEST_MAX_TOKENS,
        "stream": False,
    }


def ollama_model_running(response: Mapping[str, object], model: str) -> bool:
    """Return whether an Ollama process record contains the requested model."""

    models = response.get("models")
    if not isinstance(models, list):
        raise ValueError("Ollama process response does not contain a model list")
    expected = model.split(":", 1)[0]
    for item in models:
        if not isinstance(item, Mapping):
            continue
        for key in ("name", "model"):
            value = item.get(key)
            if isinstance(value, str) and value.split(":", 1)[0] == expected:
                return True
    return False


def wait_for_ollama_model_unload(
    model: str,
    query: Callable[[], Mapping[str, object]],
    *,
    timeout_s: float = MODEL_UNLOAD_TIMEOUT_S,
    poll_interval_s: float = MODEL_UNLOAD_POLL_INTERVAL_S,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> bool:
    """Poll Ollama until the selected model is absent or the deadline expires."""

    if timeout_s <= 0 or poll_interval_s <= 0:
        raise ValueError("unload timing values must be positive")
    deadline = clock() + timeout_s
    while True:
        if not ollama_model_running(query(), model):
            return True
        remaining = deadline - clock()
        if remaining <= 0:
            return False
        sleeper(min(poll_interval_s, remaining))


def current_vlm_workload_contract() -> dict[str, object]:
    """Return the privacy-safe contract recorded by Phase 1 VLM diagnostics."""

    return {
        "request_contract_version": VLM_REQUEST_CONTRACT_VERSION,
        "source": "jetson.vision_vlm",
        "moondream": {
            "temperature": MOONDREAM_REQUEST_TEMPERATURE,
            "seed": VLM_REQUEST_SEED,
            "num_predict": MOONDREAM_REQUEST_NUM_PREDICT,
            "request_timeout_s": MOONDREAM_REQUEST_TIMEOUT_S,
        },
        "qwen_rewrite": {
            "temperature": QWEN_REQUEST_TEMPERATURE,
            "seed": VLM_REQUEST_SEED,
            "max_tokens": QWEN_REQUEST_MAX_TOKENS,
            "request_timeout_s": QWEN_REQUEST_TIMEOUT_S,
        },
        "translation_fallback": "argos_en_zh",
        "unload_before_qwen": True,
        "cleanup_unload_on_failure": True,
        "unload_confirmation": {
            "method": "ollama_process_list_absence",
            "timeout_s": MODEL_UNLOAD_TIMEOUT_S,
            "poll_interval_ms": int(MODEL_UNLOAD_POLL_INTERVAL_S * 1000),
        },
        "raw_output_recorded": False,
    }
