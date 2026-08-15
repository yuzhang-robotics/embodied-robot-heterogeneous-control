#!/usr/bin/env python3
import base64
import json
import time
import urllib.request
import urllib.error
from pathlib import Path

import cv2
import argostranslate.translate
import subprocess

from .config import (
    CAMERA_INDEX,
    FRAME_HEIGHT,
    FRAME_WIDTH,
    LLAMA_API_URL,
    OLLAMA_CHAT_URL,
    RUNTIME_DIR,
    VLM_MODEL,
)


SCENE_IMAGE_PATH = RUNTIME_DIR / "scene_vlm.jpg"


def translate_with_qwen(english_text):
    """
    使用本地 Qwen2.5-1.5B 把 moondream 的英文视觉描述改写成自然中文。
    """
    system_prompt = (
        "你是一个中文视觉描述助手。"
        "你的任务是把英文图像描述改写成自然、简短、适合语音播报的中文。"
        "不要逐字硬翻译，不要解释。"
        "如果英文里出现 urn、vase、container、cup、bottle 等不确定物体，"
        "不要翻译成骨灰、骨灰盒；优先翻译成容器、杯子、瓶子或物体。"
        "如果英文描述不确定，就用保守说法。"
    )

    user_prompt = (
        "请把下面这句英文图像描述改写成自然中文，控制在一到两句话：\n"
        f"{english_text}"
    )

    payload = {
        "model": "qwen",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.2,
        "max_tokens": 96,
        "stream": False,
    }

    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    req = urllib.request.Request(
        LLAMA_API_URL,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    with urllib.request.urlopen(req, timeout=30) as resp:
        result = json.loads(resp.read().decode("utf-8"))

    return result["choices"][0]["message"]["content"].strip()


def capture_scene_image(output_path=SCENE_IMAGE_PATH):
    """
    拍摄一张图片，供 VLM 描述。
    """
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
    """
    使用 moondream 对图片生成英文描述。
    如果一次返回空，就自动换 prompt 重试。
    """
    image_b64 = image_to_base64(image_path)

    prompts = [
        "Describe this image briefly.",
        "What is in this image?",
        "Describe the main objects and scene in this image in one sentence.",
    ]

    last_raw = ""

    for idx, prompt in enumerate(prompts, start=1):
        payload = {
            "model": VLM_MODEL,
            "messages": [
                {
                    "role": "user",
                    "content": prompt,
                    "images": [image_b64]
                }
            ],
            "stream": False,
            "options": {
                "temperature": 0.1,
                "num_predict": 100
            }
        }

        data = json.dumps(payload).encode("utf-8")

        req = urllib.request.Request(
            OLLAMA_CHAT_URL,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        print(f"[VLM] 第{idx}次请求 moondream，prompt={prompt}")

        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
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
    """
    使用 Argos Translate 做真实离线英译中。
    """
    if not english_text:
        return ""

    try:
        zh = argostranslate.translate.translate(english_text, "en", "zh")
    except Exception as e:
        print(f"[VLM] Argos 翻译失败：{e}")
        return ""

    return zh.strip()


def translate_en_to_zh_better(english_text):
    """
    优先使用 Qwen 润色翻译。
    如果 Qwen 不可用，再退回 Argos。
    """
    try:
        chinese = translate_with_qwen(english_text)
        if chinese:
            print(f"[VLM] Qwen中文润色：{chinese}")
            return chinese
    except Exception as e:
        print(f"[VLM] Qwen翻译润色失败，退回 Argos：{e}")

    try:
        chinese = translate_en_to_zh(english_text)
        print(f"[VLM] Argos中文翻译：{chinese}")
        return chinese
    except Exception as e:
        print(f"[VLM] Argos 翻译失败：{e}")
        return ""


def make_speech_friendly(chinese_text, english_text=""):
    """
    清理中文播报文本，让 Piper 读起来更自然。
    """
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


def unload_moondream():
    """
    释放 Ollama 中的 moondream 模型，避免占用统一内存，
    导致后续 whisper.cpp CUDA 申请显存失败。
    """
    try:
        print("[VLM] 正在释放 moondream 模型...")
        subprocess.run(
            ["ollama", "stop", "moondream"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=20,
            check=False,
        )
        print("[VLM] moondream 已请求释放")
    except Exception as e:
        print(f"[VLM] 释放 moondream 时出现异常：{e}")


def describe_scene_with_vlm():
    """
    摄像头拍照 -> moondream 英文描述 -> Argos 离线英译中。
    关键点：moondream 用完后立刻 ollama stop，释放内存给 Whisper CUDA。
    """
    image_path = capture_scene_image()

    if image_path is None:
        return "我尝试打开摄像头，但是没有成功读取画面。"

    print(f"[VLM] 已保存图片：{image_path}")

    try:
        english_desc = ask_moondream_english(image_path)
        print(f"[VLM] 英文描述：{english_desc}")

        if not english_desc:
            return "我看到了画面，但是视觉模型没有生成有效描述。"

        try:
            chinese_desc = translate_en_to_zh_better(english_desc)
        except Exception as e:
            print(f"[VLM] Argos 翻译失败：{e}")
            chinese_desc = ""

        print(f"[VLM] Argos中文翻译：{chinese_desc}")

        reply = make_speech_friendly(chinese_desc, english_desc)
        print(f"[VLM] 最终播报：{reply}")

        return reply

    except Exception as e:
        print(f"[VLM] moondream 调用失败：{e}")
        return "我已经拍摄了当前画面，但是视觉语言模型暂时不可用。"

    finally:
        unload_moondream()


if __name__ == "__main__":
    print(describe_scene_with_vlm())
