#!/usr/bin/env python3
"""Synchronous voice, vision, and motion application from the thesis baseline."""

import audioop
import json
import subprocess
import time
import urllib.error
import urllib.request
import wave
from pathlib import Path

import numpy as np
import sherpa_onnx

from .config import (
    ASSETS_DIR,
    KWS_DECODER,
    KWS_ENCODER,
    KWS_JOINER,
    KWS_KEYWORDS,
    KWS_TOKENS,
    LLAMA_API_URL,
    MIC_DEVICE,
    PIPER_MODEL,
    RUNTIME_DIR,
    WHISPER_ASR_MODEL,
    WHISPER_BIN,
    WHISPER_DIR,
)
from .motion_planner import move_to_color_object_fast
from .robot_comm import motion_enabled, send_motion_command
from .vision_vlm import describe_scene_with_vlm

SAMPLE_RATE = 16000
CHANNELS = 1
SAMPLE_WIDTH = 2

INPUT_WAV = RUNTIME_DIR / "input.wav"
ASR_OUT_BASE = RUNTIME_DIR / "asr_out"
ASR_TXT = RUNTIME_DIR / "asr_out.txt"
TTS_WAV = RUNTIME_DIR / "reply.wav"
WAKE_ACK_WAV = ASSETS_DIR / "wake_ack.wav"

KWS_CHUNK_MS = 100
KWS_CHUNK_SAMPLES = int(SAMPLE_RATE * KWS_CHUNK_MS / 1000)
KWS_CHUNK_BYTES = KWS_CHUNK_SAMPLES * SAMPLE_WIDTH * CHANNELS

RECORD_CHUNK_MS = 200
RECORD_CHUNK_BYTES = int(SAMPLE_RATE * SAMPLE_WIDTH * CHANNELS * RECORD_CHUNK_MS / 1000)

# Voice activity threshold tuned for the thesis microphone and test room.
RMS_THRESHOLD = 700
SILENCE_END_SECONDS = 0.9
MIN_RECORD_SECONDS = 0.8
MAX_RECORD_SECONDS = 6
WAIT_SPEECH_TIMEOUT = 8


SYSTEM_PROMPT = (
    "你是章鱼号，一个由 yuzhang-robotics 开发、运行在 Jetson Orin Nano 上的离线中文语音助手。"
    "请用自然、简短、适合语音播报的中文回答。"
    "不要使用 Markdown 表格。"
    "除非用户要求详细解释，否则回答控制在三到六句话。"
)


def run_cmd(cmd, cwd=None, check=True, timeout=None):
    print("\n[运行命令]", " ".join(str(x) for x in cmd))
    try:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"命令超时：{' '.join(str(x) for x in cmd)}")

    if result.stdout.strip():
        print(result.stdout)

    if check and result.returncode != 0:
        raise RuntimeError(f"命令失败，退出码：{result.returncode}")

    return result.stdout


def check_files():
    missing = []

    for p in [
        WHISPER_BIN,
        WHISPER_ASR_MODEL,
        Path(PIPER_MODEL),
        KWS_TOKENS,
        KWS_ENCODER,
        KWS_DECODER,
        KWS_JOINER,
        KWS_KEYWORDS,
    ]:
        if not p.exists():
            missing.append(str(p))

    if missing:
        print("以下文件不存在：")
        for item in missing:
            print("-", item)
        raise SystemExit(1)


def clean_text(text):
    text = (
        text.replace(" ", "")
        .replace("，", "")
        .replace(",", "")
        .replace("。", "")
        .replace(".", "")
        .replace("？", "")
        .replace("?", "")
        .replace("！", "")
        .replace("!", "")
        .strip()
    )

    # 常见繁体/异体识别结果归一化
    text = (
        text.replace("進", "进")
        .replace("後", "后")
        .replace("轉", "转")
        .replace("紅", "红")
        .replace("藍", "蓝")
        .replace("綠", "绿")
        .replace("黃", "黄")
        .replace("體", "体")
        .replace("麼", "么")
        .replace("誰", "谁")
        .replace("關", "关")
        .replace("閉", "闭")
    )

    return text


def parse_intent(user_text):
    """Map recognized Chinese text to one baseline task intent."""

    text = clean_text(user_text)

    if text in {"退出", "结束", "再见", "关闭", "关机"}:
        return {
            "type": "exit"
        }

    if any(k in text for k in ["停止", "停下", "别动", "不要动", "原地不动"]):
        return {
            "type": "direct_motion",
            "command": "stop",
            "command_cn": "停止"
        }

    move_keywords = ["移动到", "走到", "前往", "去到", "靠近", "移动过去", "过去", "到"]
    has_move_intent = any(k in text for k in move_keywords)

    if has_move_intent:
        target_color = None
        target_color_cn = None

        if "红" in text:
            target_color = "red"
            target_color_cn = "红色"
        elif "蓝" in text:
            target_color = "blue"
            target_color_cn = "蓝色"
        elif "绿" in text:
            target_color = "green"
            target_color_cn = "绿色"
        elif "黄" in text:
            target_color = "yellow"
            target_color_cn = "黄色"

        if target_color is not None:
            return {
                "type": "move_to_object",
                "target_color": target_color,
                "target_color_cn": target_color_cn
            }

    if any(k in text for k in ["前进", "往前", "向前", "向前走"]):
        return {
            "type": "direct_motion",
            "command": "forward",
            "command_cn": "前进"
        }

    if any(k in text for k in ["后退", "往后", "向后", "倒退"]):
        return {
            "type": "direct_motion",
            "command": "backward",
            "command_cn": "后退"
        }

    if any(k in text for k in ["左转", "向左转", "左拐", "向左拐", "往左转"]):
        return {
            "type": "direct_motion",
            "command": "turn_left",
            "command_cn": "左转"
        }

    if any(k in text for k in ["右转", "向右转", "右拐", "向右拐", "往右转"]):
        return {
            "type": "direct_motion",
            "command": "turn_right",
            "command_cn": "右转"
        }

    if any(k in text for k in [
        "看到了什么",
        "看到什么",
        "看见了什么",
        "你看见了什么",
        "你看到了什么",
        "前面有什么",
        "视野里有什么",
        "画面里有什么",
        "摄像头里有什么"
    ]):
        return {
            "type": "vision_qa"
        }

    return {
        "type": "chat"
    }


def handle_vision_qa():
    """Describe the current camera frame through the local VLM pipeline."""
    reply = describe_scene_with_vlm()
    print("\n[视觉问答-VLM]", reply)
    return reply


def handle_move_to_object(intent):
    """Run the color-target motion task selected by voice intent."""
    target_color = intent.get("target_color")
    color_cn = intent.get("target_color_cn", "目标")

    if not target_color:
        return "我知道你想让我移动，但是还没有识别出目标颜色。"

    speak_text(f"好的，我将移动到{color_cn}物体的前方。")

    result = move_to_color_object_fast(target_color)

    print("\n[目标移动结果]", result)

    return result["reply"]


def handle_direct_motion(intent):
    """Execute or log one direct motion intent."""
    command = intent.get("command", "stop")
    command_cn = intent.get("command_cn", "运动")

    serial_text = send_motion_command(command)

    print("\n[直接运动]", intent, "=>", serial_text)

    if not motion_enabled():
        return f"安全模式下已识别{command_cn}指令，但没有驱动底盘。"

    if command == "stop":
        return "收到，已停止。"

    return f"收到，执行{command_cn}指令。"


def create_kws():
    return sherpa_onnx.KeywordSpotter(
        tokens=str(KWS_TOKENS),
        encoder=str(KWS_ENCODER),
        decoder=str(KWS_DECODER),
        joiner=str(KWS_JOINER),
        num_threads=2,
        keywords_file=str(KWS_KEYWORDS),
        provider="cpu",
    )


def start_arecord_raw():
    cmd = [
        "arecord",
        "-D", MIC_DEVICE,
        "-r", str(SAMPLE_RATE),
        "-c", str(CHANNELS),
        "-f", "S16_LE",
        "-t", "raw",
        "-q",
    ]

    return subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        bufsize=0,
    )


def play_wake_ack():
    if WAKE_ACK_WAV.exists():
        run_cmd(["aplay", str(WAKE_ACK_WAV)])
    else:
        speak_text("我在。")


def listen_for_wake_word():
    print("\n正在监听唤醒词：你好章鱼号")
    print("按 Ctrl+C 退出。")

    kws = create_kws()
    stream = kws.create_stream()

    proc = start_arecord_raw()
    last_print = time.time()

    try:
        while True:
            data = proc.stdout.read(KWS_CHUNK_BYTES)
            if not data:
                print("没有读取到音频数据，重新启动监听。")
                break

            samples = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0

            stream.accept_waveform(SAMPLE_RATE, samples)

            while kws.is_ready(stream):
                kws.decode_stream(stream)

            result = kws.get_result(stream)

            if result:
                print(f"\n检测到唤醒词：{result}")
                kws.reset_stream(stream)
                return True

            if time.time() - last_print > 5:
                print("监听中...")
                last_print = time.time()

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            proc.kill()

    return False


def record_until_silence(output_wav):
    print("\n请继续说话，我会在你停顿后自动结束录音。")

    if output_wav.exists():
        output_wav.unlink()

    proc = start_arecord_raw()

    frames = []
    speech_started = False
    silence_chunks = 0

    silence_limit_chunks = int(SILENCE_END_SECONDS * 1000 / RECORD_CHUNK_MS)
    min_chunks = int(MIN_RECORD_SECONDS * 1000 / RECORD_CHUNK_MS)
    max_chunks = int(MAX_RECORD_SECONDS * 1000 / RECORD_CHUNK_MS)
    wait_chunks = int(WAIT_SPEECH_TIMEOUT * 1000 / RECORD_CHUNK_MS)

    total_chunks = 0
    recorded_chunks = 0

    try:
        while True:
            chunk = proc.stdout.read(RECORD_CHUNK_BYTES)
            if not chunk:
                break

            total_chunks += 1
            rms = audioop.rms(chunk, SAMPLE_WIDTH)

            if not speech_started:
                if rms >= RMS_THRESHOLD:
                    speech_started = True
                    frames.append(chunk)
                    recorded_chunks += 1
                    print(f"检测到说话，开始录音... RMS={rms}")
                else:
                    if total_chunks % 5 == 0:
                        print(f"等待说话中... RMS={rms}")
                    if total_chunks >= wait_chunks:
                        print("唤醒后没有检测到说话，返回监听。")
                        return False
                    continue

            else:
                frames.append(chunk)
                recorded_chunks += 1

                if rms < RMS_THRESHOLD:
                    silence_chunks += 1
                else:
                    silence_chunks = 0

                if recorded_chunks % 5 == 0:
                    print(f"录音中... RMS={rms}")

                if recorded_chunks >= min_chunks and silence_chunks >= silence_limit_chunks:
                    print("检测到停顿，结束录音。")
                    break

                if recorded_chunks >= max_chunks:
                    print("达到最长录音时间，结束录音。")
                    break

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            proc.kill()

    if not frames:
        return False

    with wave.open(str(output_wav), "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(SAMPLE_WIDTH)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(b"".join(frames))

    print(f"录音已保存：{output_wav}")
    return True


def transcribe_wav(wav_path):
    if ASR_TXT.exists():
        ASR_TXT.unlink()

    run_cmd([
        str(WHISPER_BIN),
        "-m", str(WHISPER_ASR_MODEL),
        "-f", str(wav_path),
        "-l", "zh",
        "-otxt",
        "-of", str(ASR_OUT_BASE),
        "-nt",
        "-np",
        "-bs", "1",
        "-bo", "1",
    ], cwd=str(WHISPER_DIR))

    if not ASR_TXT.exists():
        return ""

    text = ASR_TXT.read_text(encoding="utf-8", errors="ignore").strip()
    text = " ".join(text.split())

    print(f"\n识别结果：{text}")
    return text


def ask_llama(user_text, history):
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.extend(history[-8:])
    messages.append({"role": "user", "content": user_text})

    payload = {
        "model": "qwen",
        "messages": messages,
        "temperature": 0.4,
        "max_tokens": 80,
        "stream": False,
    }

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        LLAMA_API_URL,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as e:
        raise RuntimeError("无法连接 llama-server。请确认 8080 服务正在运行。") from e

    reply = result["choices"][0]["message"]["content"].strip()

    print(f"\n你说：{user_text}")
    print(f"\n助手：{reply}")

    history.append({"role": "user", "content": user_text})
    history.append({"role": "assistant", "content": reply})

    return reply


def speak_text(text):
    if TTS_WAV.exists():
        TTS_WAV.unlink()

    run_cmd([
        "python3",
        "-m",
        "piper",
        "-m",
        str(PIPER_MODEL),
        "-f",
        str(TTS_WAV),
        "--",
        text,
    ])

    run_cmd(["aplay", str(TTS_WAV)])


def main():
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    check_files()

    print("\n离线语音助手已启动。")
    print("模式：sherpa-onnx 监听“你好章鱼号” → 自动录音 → Whisper CUDA → 意图识别 →")
    print("聊天走 Qwen，视觉问答走 moondream+Argos，语音合成走 Piper")
    print(f"底盘输出：{'真实串口模式' if motion_enabled() else '安全日志模式'}")
    print("按 Ctrl+C 退出。")

    history = []

    while True:
        try:
            ok = listen_for_wake_word()
            if not ok:
                continue

            play_wake_ack()
            time.sleep(0.4)

            ok = record_until_silence(INPUT_WAV)
            if not ok:
                continue

            user_text = transcribe_wav(INPUT_WAV).strip()

            if not user_text:
                speak_text("我没有听清楚。")
                continue

            intent = parse_intent(user_text)
            print(f"\n[意图识别结果] {intent}")

            if intent["type"] == "exit":
                speak_text("好的，再见。")
                break

            elif intent["type"] == "chat":
                reply = ask_llama(user_text, history)
                speak_text(reply)

            elif intent["type"] == "vision_qa":
                reply = handle_vision_qa()
                speak_text(reply)

            elif intent["type"] == "move_to_object":
                reply = handle_move_to_object(intent)
                speak_text(reply)

            elif intent["type"] == "direct_motion":
                reply = handle_direct_motion(intent)
                speak_text(reply)

            else:
                reply = "我还不能理解这个指令。"
                speak_text(reply)

            time.sleep(0.5)

        except KeyboardInterrupt:
            print("\n已退出。")
            break
        except Exception as e:
            print(f"\n发生错误：{e}")
            time.sleep(1)


if __name__ == "__main__":
    main()
