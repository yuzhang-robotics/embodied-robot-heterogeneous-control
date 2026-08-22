#!/usr/bin/env python3
"""UART transport for motion commands sent from Jetson to STM32."""

import time

from .config import BAUD_RATE, ENABLE_MOTION, RUNTIME_DIR, SERIAL_PORT


MOTION_LOG = RUNTIME_DIR / "motion_commands.log"
LOG_ONLY = not ENABLE_MOTION
_ser = None

_COMMAND_FRAMES = {
    "forward": "F,20",
    "backward": "B,20",
    "turn_left": "L,15",
    "turn_right": "R,15",
    "stop": "S,0",
    "search": "L,15",
}


def command_to_serial_text(command: str) -> str:
    return _COMMAND_FRAMES.get(command, "S,0")


def bytes_to_hex(data: bytes) -> str:
    return " ".join(f"{b:02X}" for b in data)


def get_serial():
    """Open and cache the STM32 serial connection."""
    global _ser

    if LOG_ONLY:
        return None

    if _ser is not None and _ser.is_open:
        return _ser

    try:
        import serial
    except ImportError as e:
        raise RuntimeError(
            "没有安装 pyserial。请先执行：python3 -m pip install --user pyserial"
        ) from e

    try:
        _ser = serial.Serial(
            port=SERIAL_PORT,
            baudrate=BAUD_RATE,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=0.05,
            write_timeout=0.2,
        )

        time.sleep(0.3)

        _ser.reset_input_buffer()
        _ser.reset_output_buffer()

        print(f"[串口] 已打开 {SERIAL_PORT}, baud={BAUD_RATE}")
        return _ser

    except Exception as e:
        raise RuntimeError(f"无法打开串口 {SERIAL_PORT}：{e}") from e


def drain_serial_input(ser, duration=0.15):
    """Discard stale input before waiting for the next acknowledgement."""
    end_time = time.time() + duration

    while time.time() < end_time:
        try:
            data = ser.read(64)
        except Exception:
            break

        if not data:
            time.sleep(0.01)


def read_stm32_response(ser, timeout=0.8):
    """Read until an ``A`` or ``E`` response is received, or timeout expires."""
    end_time = time.time() + timeout
    raw_all = b""

    while time.time() < end_time:
        try:
            chunk = ser.read(16)
        except Exception:
            break

        if chunk:
            raw_all += chunk

            lines = raw_all.splitlines()

            for line in lines:
                s = line.decode("utf-8", errors="ignore").strip()

                if s == "A":
                    return "A", raw_all

                if s == "E":
                    return "E", raw_all

            if raw_all == b"A" or raw_all.endswith(b"\nA"):
                return "A", raw_all

            if raw_all == b"E" or raw_all.endswith(b"\nE"):
                return "E", raw_all

        time.sleep(0.01)

    return "", raw_all


def write_log(command, serial_text, response="", raw=b""):
    MOTION_LOG.parent.mkdir(parents=True, exist_ok=True)

    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")

    raw_hex = bytes_to_hex(raw) if raw else ""

    line = (
        f"{timestamp} "
        f"command={command} "
        f"serial={serial_text} "
        f"response={response if response else 'NO_ACK'} "
        f"raw_hex={raw_hex}\n"
    )

    with MOTION_LOG.open("a", encoding="utf-8") as f:
        f.write(line)


def send_motion_command(command: str) -> str:
    """Send one mapped motion command, or log it when motion is disabled."""
    serial_text = command_to_serial_text(command)

    if LOG_ONLY:
        print(f"[运动命令-安全模式] {serial_text}")
        write_log(command, serial_text, response="LOG_ONLY")
        return serial_text

    ser = get_serial()
    print(f"[发送运动命令] {serial_text}")

    data = (serial_text + "\n").encode("utf-8")
    drain_serial_input(ser, duration=0.08)

    try:
        ser.write(data)
        ser.flush()
    except Exception as e:
        write_log(command, serial_text, response="SEND_FAIL")
        raise RuntimeError(f"串口发送失败：{e}") from e

    resp, raw = read_stm32_response(ser, timeout=0.8)

    if resp == "A":
        print("[STM32确认] A")
    elif resp == "E":
        print("[STM32错误] E")
    else:
        if raw:
            print(f"[STM32未确认，原始bytes] {repr(raw)}")
            print(f"[STM32未确认，HEX] {bytes_to_hex(raw)}")
        else:
            print("[STM32无响应]")

    write_log(command, serial_text, response=resp, raw=raw)

    return serial_text


def close_serial():
    global _ser

    if _ser is not None:
        try:
            _ser.close()
            print("[串口] 已关闭")
        except Exception:
            pass

        _ser = None


def motion_enabled():
    return not LOG_ONLY
