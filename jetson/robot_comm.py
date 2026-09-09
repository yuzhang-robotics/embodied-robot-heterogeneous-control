#!/usr/bin/env python3
"""UART transport for motion commands sent from Jetson to STM32."""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable
from typing import Protocol

from .config import BAUD_RATE, ENABLE_MOTION, RUNTIME_DIR, SERIAL_PORT


MOTION_LOG = RUNTIME_DIR / "motion_commands.log"
LOG_ONLY = not ENABLE_MOTION
_SERIAL_LOCK = threading.RLock()

STM32_RESPONSE_TIMEOUT_S = 0.8
STM32_RESPONSE_MAX_BYTES = 64


class SerialConnection(Protocol):
    is_open: bool

    def reset_input_buffer(self) -> None: ...

    def reset_output_buffer(self) -> None: ...

    def write(self, data: bytes) -> int: ...

    def flush(self) -> None: ...

    def read(self, size: int) -> bytes: ...

    def close(self) -> None: ...


_ser: SerialConnection | None = None

_COMMAND_FRAMES = {
    "forward": "F,20",
    "backward": "B,20",
    "turn_left": "L,15",
    "turn_right": "R,15",
    "stop": "S,0",
    "search": "L,15",
}


class MotionCommunicationError(RuntimeError):
    """One bounded UART transaction failed or was rejected."""

    def __init__(self, code: str, message: str, *, raw: bytes = b"") -> None:
        super().__init__(message)
        self.code = code
        self.raw = bytes(raw)


def _close_connection(connection: SerialConnection) -> None:
    global _ser

    if _ser is connection:
        _ser = None
    try:
        connection.close()
    except Exception:
        pass


def command_to_serial_text(command: str) -> str:
    if not isinstance(command, str):
        raise TypeError("motion command must be a string")
    try:
        return _COMMAND_FRAMES[command]
    except KeyError as exc:
        raise ValueError(f"unsupported motion command: {command!r}") from exc


def bytes_to_hex(data: bytes) -> str:
    return " ".join(f"{b:02X}" for b in data)


def get_serial() -> SerialConnection | None:
    """Open and cache the STM32 serial connection."""
    global _ser

    with _SERIAL_LOCK:
        if LOG_ONLY:
            return None

        if _ser is not None:
            try:
                if _ser.is_open:
                    return _ser
            except Exception:
                pass
            _close_connection(_ser)

        try:
            import serial
        except ImportError as exc:
            raise MotionCommunicationError(
                "serial_dependency_missing",
                "没有安装 pyserial。请先执行：python3 -m pip install --user pyserial",
            ) from exc

        candidate = None
        try:
            candidate = serial.Serial(
                port=SERIAL_PORT,
                baudrate=BAUD_RATE,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=0.05,
                write_timeout=0.2,
            )

            time.sleep(0.3)
            candidate.reset_input_buffer()
            candidate.reset_output_buffer()
        except Exception as exc:
            if candidate is not None:
                _close_connection(candidate)
            raise MotionCommunicationError(
                "serial_open_failed",
                f"无法打开串口 {SERIAL_PORT}",
            ) from exc

        _ser = candidate
        print(f"[串口] 已打开 {SERIAL_PORT}, baud={BAUD_RATE}")
        return _ser


def _positive_finite(value: object, name: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError(f"{name} must be a finite positive number")
    return float(value)


def read_stm32_response(
    ser: SerialConnection,
    timeout: float = STM32_RESPONSE_TIMEOUT_S,
    *,
    max_response_bytes: int = STM32_RESPONSE_MAX_BYTES,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> tuple[str, bytes]:
    """Read one exact, newline-terminated ``A`` or ``E`` response."""

    checked_timeout = _positive_finite(timeout, "response timeout")
    if (
        isinstance(max_response_bytes, bool)
        or not isinstance(max_response_bytes, int)
        or not 2 <= max_response_bytes <= 4096
    ):
        raise ValueError("max_response_bytes must be an integer from 2 to 4096")
    if not callable(clock) or not callable(sleeper):
        raise TypeError("clock and sleeper must be callable")

    deadline = clock() + checked_timeout
    raw = bytearray()

    while True:
        remaining = deadline - clock()
        if remaining <= 0:
            return "", bytes(raw)

        try:
            chunk = ser.read(min(16, max_response_bytes - len(raw)))
        except Exception as exc:
            raise MotionCommunicationError(
                "response_read_failed",
                "读取 STM32 响应失败",
                raw=bytes(raw),
            ) from exc
        if not isinstance(chunk, bytes):
            raise MotionCommunicationError(
                "response_invalid_type",
                "STM32 响应不是字节数据",
                raw=bytes(raw),
            )

        if chunk:
            remaining_capacity = max_response_bytes - len(raw)
            if len(chunk) > remaining_capacity:
                raw.extend(chunk[:remaining_capacity])
                raise MotionCommunicationError(
                    "response_too_large",
                    "STM32 响应超过长度上限",
                    raw=bytes(raw),
                )
            raw.extend(chunk)
            response = bytes(raw)
            if response == b"A\n":
                return "A", response
            if response == b"E\n":
                return "E", response
            if b"\n" in response:
                raise MotionCommunicationError(
                    "response_invalid_frame",
                    "STM32 返回了无效响应帧",
                    raw=response,
                )
            if len(raw) >= max_response_bytes:
                raise MotionCommunicationError(
                    "response_too_large",
                    "STM32 响应超过长度上限",
                    raw=response,
                )
            continue

        sleeper(min(0.01, remaining))


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
    """Send one command and return only after an exact STM32 acknowledgement."""
    serial_text = command_to_serial_text(command)

    with _SERIAL_LOCK:
        if LOG_ONLY:
            print(f"[运动命令-安全模式] {serial_text}")
            write_log(command, serial_text, response="LOG_ONLY")
            return serial_text

        ser = get_serial()
        if ser is None:
            raise MotionCommunicationError(
                "serial_unavailable",
                "真实运动模式下串口不可用",
            )
        print(f"[发送运动命令] {serial_text}")

        data = (serial_text + "\n").encode("ascii")
        try:
            ser.reset_input_buffer()
            written = ser.write(data)
            if written != len(data):
                raise OSError("partial serial write")
            ser.flush()
        except Exception as exc:
            _close_connection(ser)
            write_log(command, serial_text, response="SEND_FAIL")
            raise MotionCommunicationError(
                "serial_write_failed",
                "串口发送失败",
            ) from exc

        try:
            response, raw = read_stm32_response(ser)
        except MotionCommunicationError as exc:
            _close_connection(ser)
            write_log(command, serial_text, response=exc.code.upper(), raw=exc.raw)
            raise

        write_log(command, serial_text, response=response, raw=raw)
        if response == "A":
            print("[STM32确认] A")
            return serial_text
        if response == "E":
            print("[STM32错误] E")
            raise MotionCommunicationError(
                "command_rejected",
                "STM32 拒绝了运动命令",
                raw=raw,
            )

        if raw:
            print(f"[STM32未确认，HEX] {bytes_to_hex(raw)}")
        else:
            print("[STM32无响应]")
        _close_connection(ser)
        raise MotionCommunicationError(
            "response_timeout",
            "未在期限内收到完整的 STM32 响应",
            raw=raw,
        )


def close_serial():
    global _ser

    with _SERIAL_LOCK:
        current = _ser
        _ser = None
        if current is not None:
            try:
                was_open = bool(current.is_open)
            except Exception:
                was_open = False
            _close_connection(current)
            if was_open:
                print("[串口] 已关闭")


def motion_enabled():
    return not LOG_ONLY
