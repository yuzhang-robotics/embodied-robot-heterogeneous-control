#!/usr/bin/env python3
"""Camera scene description through Ollama VLM and local Chinese translation."""

import base64
import json
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import argostranslate.translate
import cv2

from .config import (
    CAMERA_INDEX,
    FRAME_HEIGHT,
    FRAME_WIDTH,
    LLAMA_API_URL,
    OLLAMA_CHAT_URL,
    RUNTIME_DIR,
    VLM_MODEL,
)
from .vlm_request_contract import (
    MODEL_UNLOAD_TIMEOUT_S,
    MOONDREAM_PROMPTS,
    MOONDREAM_REQUEST_TIMEOUT_S,
    QWEN_REQUEST_TIMEOUT_S,
    build_moondream_payload,
    build_qwen_payload,
    wait_for_ollama_model_unload,
)


SCENE_IMAGE_PATH = RUNTIME_DIR / "scene_vlm.jpg"


def translate_with_qwen(english_text):
    """Rewrite an English VLM description as concise spoken Chinese."""
    payload = build_qwen_payload(english_text)

    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    req = urllib.request.Request(
        LLAMA_API_URL,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    with urllib.request.urlopen(req, timeout=QWEN_REQUEST_TIMEOUT_S) as resp:
        result = json.loads(resp.read().decode("utf-8"))

    return result["choices"][0]["message"]["content"].strip()


def capture_scene_image(output_path=SCENE_IMAGE_PATH):
    """Capture one image for scene description."""
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    cap = cv2.VideoCapture(CAMERA_INDEX, cv2.CAP_V4L2)

    if not cap.isOpened():
        cap = cv2.VideoCapture(CAMERA_INDEX)

    if not cap.isOpened():
        print("[VLM] 无法打开摄像头")
        return None

    cap.set(cv2.CAP_PROP_FRAME_WIDTH, FRAME_WIDTH)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, FRAME_HEIGHT)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

    for _ in range(8):
        cap.read()
        time.sleep(0.03)

    ret, frame = cap.read()
    cap.release()

    if not ret or frame is None:
        print("[VLM] 摄像头打开成功，但读取画面失败")
        return None

    cv2.imwrite(str(output_path), frame)
    return output_path


def image_to_base64(image_path):
    return base64.b64encode(Path(image_path).read_bytes()).decode("utf-8")


def ask_moondream_english(image_path):
    """Request an English description, retrying empty responses with new prompts."""
    image_b64 = image_to_base64(image_path)

    last_raw = ""

    for idx, prompt in enumerate(MOONDREAM_PROMPTS, start=1):
        payload = build_moondream_payload(VLM_MODEL, prompt, image_b64)

        data = json.dumps(payload).encode("utf-8")

        req = urllib.request.Request(
            OLLAMA_CHAT_URL,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        print(f"[VLM] 第{idx}次请求 moondream，prompt={prompt}")

        try:
            with urllib.request.urlopen(
                req,
                timeout=MOONDREAM_REQUEST_TIMEOUT_S,
            ) as resp:
                raw = resp.read().decode("utf-8")
                last_raw = raw
                result = json.loads(raw)
        except urllib.error.URLError as e:
            raise RuntimeError("无法连接 Ollama，请确认 Ollama 服务正在运行。") from e

        message = result.get("message", {})
        content = message.get("content", "").strip()

        if content:
            return content

        print(f"[VLM] 第{idx}次 moondream 返回空，准备重试。")

    print("[VLM] moondream 多次返回空，最后一次原始返回如下：")
    print(last_raw)

    return ""


def translate_en_to_zh(english_text):
    """Translate English to Chinese with the offline Argos package."""
    if not english_text:
        return ""

    try:
        zh = argostranslate.translate.translate(english_text, "en", "zh")
    except Exception as e:
        print(f"[VLM] Argos 翻译失败：{e}")
        return ""

    return zh.strip()


def translate_en_to_zh_better(english_text):
    """Prefer Qwen rewriting and fall back to Argos translation."""
    try:
        chinese = translate_with_qwen(english_text)
        if chinese:
            print(f"[VLM] Qwen中文润色：{chinese}")
            return chinese
    except Exception as e:
        print(f"[VLM] Qwen翻译润色失败，退回 Argos：{e}")

    chinese = translate_en_to_zh(english_text)
    if chinese:
        print(f"[VLM] Argos中文翻译：{chinese}")
    return chinese


def make_speech_friendly(chinese_text, english_text=""):
    """Normalize punctuation and recurring VLM wording before speech synthesis."""
    text = chinese_text.strip() if chinese_text else ""

    if not text:
        text = english_text.strip()

    replacements = {
        ",": "，",
        ".": "。",
        "，。": "。",
        "。。": "。",
        " .": "。",
        " ,": "，",
        "开放的网页": "打开的网页",
        "黑色电话": "黑色手机",
        "笔记本屏幕": "笔记本电脑屏幕",
        "坐在桌子上": "坐在桌前",
        "木质桌子": "木桌",
        "骨灰盒": "容器",
        "骨灰": "容器",
    }

    for old, new in replacements.items():
        text = text.replace(old, new)

    while "。。" in text:
        text = text.replace("。。", "。")

    while "，，" in text:
        text = text.replace("，，", "，")

    text = text.strip(" ，。")

    if not text.endswith(("。", "！", "？")):
        text += "。"

    return text


def _ollama_processes():
    base_url = OLLAMA_CHAT_URL.split("/api/", 1)[0]
    request = urllib.request.Request(f"{base_url}/api/ps", method="GET")
    with urllib.request.urlopen(request, timeout=2) as response:
        value = json.loads(response.read().decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Ollama process response is not an object")
    return value


def unload_moondream():
    """Release the VLM and confirm that Ollama no longer reports it loaded."""
    try:
        print(f"[VLM] 正在释放 {VLM_MODEL} 模型...")
        completed = subprocess.run(
            ["ollama", "stop", VLM_MODEL],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=MODEL_UNLOAD_TIMEOUT_S,
            check=False,
        )
        if completed.returncode != 0:
            print(f"[VLM] {VLM_MODEL} 释放请求失败")
            return False
        confirmed = wait_for_ollama_model_unload(
            VLM_MODEL,
            _ollama_processes,
        )
        if confirmed:
            print(f"[VLM] {VLM_MODEL} 已确认释放")
        else:
            print(f"[VLM] {VLM_MODEL} 释放未确认")
        return confirmed
    except Exception as e:
        print(f"[VLM] 释放 moondream 时出现异常：{e}")
        return False


def describe_scene_with_vlm():
    """Capture, describe, translate, and release the VLM after each request."""
    image_path = capture_scene_image()

    if image_path is None:
        return "我尝试打开摄像头，但是没有成功读取画面。"

    print(f"[VLM] 已保存图片：{image_path}")

    try:
        english_desc = ask_moondream_english(image_path)
        print(f"[VLM] 英文描述：{english_desc}")

        if not english_desc:
            return "我看到了画面，但是视觉模型没有生成有效描述。"

        chinese_desc = translate_en_to_zh_better(english_desc)

        reply = make_speech_friendly(chinese_desc, english_desc)
        print(f"[VLM] 最终播报：{reply}")

        return reply

    except Exception as e:
        print(f"[VLM] moondream 调用失败：{e}")
        return "我已经拍摄了当前画面，但是视觉语言模型暂时不可用。"

    finally:
        unload_moondream()
