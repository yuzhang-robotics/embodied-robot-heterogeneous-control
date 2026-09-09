from __future__ import annotations

import tempfile
import threading
import time
import unittest
from collections import deque
from pathlib import Path
from unittest import mock

from jetson import robot_comm


class StepClock:
    def __init__(self, step: float = 0.01) -> None:
        self.value = 0.0
        self.step = step

    def __call__(self) -> float:
        self.value += self.step
        return self.value


class ScriptedSerial:
    def __init__(self, chunks: list[bytes] | None = None) -> None:
        self.chunks = deque(chunks or [])
        self.writes: list[bytes] = []
        self.input_resets = 0
        self.flushed = 0
        self.is_open = True
        self.closed = False

    def reset_input_buffer(self) -> None:
        self.input_resets += 1

    def reset_output_buffer(self) -> None:
        pass

    def write(self, data: bytes) -> int:
        self.writes.append(data)
        return len(data)

    def flush(self) -> None:
        self.flushed += 1

    def read(self, _size: int) -> bytes:
        return self.chunks.popleft() if self.chunks else b""

    def close(self) -> None:
        self.closed = True
        self.is_open = False


class ConcurrentSerial(ScriptedSerial):
    def __init__(self) -> None:
        super().__init__()
        self._state_lock = threading.Lock()
        self._transaction_active = False
        self.overlap_observed = False

    def write(self, data: bytes) -> int:
        with self._state_lock:
            if self._transaction_active:
                self.overlap_observed = True
            self._transaction_active = True
        time.sleep(0.01)
        return super().write(data)

    def read(self, _size: int) -> bytes:
        with self._state_lock:
            self._transaction_active = False
        return b"A\n"


class FailingReadSerial(ScriptedSerial):
    def read(self, _size: int) -> bytes:
        raise OSError("private serial failure")


class RobotCommunicationTests(unittest.TestCase):
    def setUp(self) -> None:
        print_patcher = mock.patch("builtins.print")
        print_patcher.start()
        self.addCleanup(print_patcher.stop)

    def tearDown(self) -> None:
        robot_comm.close_serial()

    def test_command_mapping_rejects_unknown_values(self) -> None:
        self.assertEqual(robot_comm.command_to_serial_text("forward"), "F,20")
        self.assertEqual(robot_comm.command_to_serial_text("stop"), "S,0")
        with self.assertRaisesRegex(ValueError, "unsupported"):
            robot_comm.command_to_serial_text("sideways")
        with self.assertRaises(TypeError):
            robot_comm.command_to_serial_text(None)  # type: ignore[arg-type]

    def test_response_requires_an_exact_complete_line(self) -> None:
        response, raw = robot_comm.read_stm32_response(
            ScriptedSerial([b"A", b"\n"]),
            timeout=0.2,
            clock=StepClock(),
            sleeper=lambda _seconds: None,
        )
        self.assertEqual((response, raw), ("A", b"A\n"))

        response, raw = robot_comm.read_stm32_response(
            ScriptedSerial([b"A"]),
            timeout=0.05,
            clock=StepClock(),
            sleeper=lambda _seconds: None,
        )
        self.assertEqual((response, raw), ("", b"A"))

        with self.assertRaisesRegex(
            robot_comm.MotionCommunicationError,
            "无效响应帧",
        ) as caught:
            robot_comm.read_stm32_response(
                ScriptedSerial([b"noise\nA\n"]),
                timeout=0.2,
                clock=StepClock(),
                sleeper=lambda _seconds: None,
            )
        self.assertEqual(caught.exception.code, "response_invalid_frame")
        self.assertEqual(caught.exception.raw, b"noise\nA\n")

    def test_response_buffer_and_read_failures_are_bounded(self) -> None:
        with self.assertRaises(robot_comm.MotionCommunicationError) as oversized:
            robot_comm.read_stm32_response(
                ScriptedSerial([b"x" * 8]),
                timeout=0.2,
                max_response_bytes=8,
                clock=StepClock(),
                sleeper=lambda _seconds: None,
            )
        self.assertEqual(oversized.exception.code, "response_too_large")
        self.assertEqual(len(oversized.exception.raw), 8)

        failing = FailingReadSerial()
        with self.assertRaises(robot_comm.MotionCommunicationError) as read_error:
            robot_comm.read_stm32_response(
                failing,
                timeout=0.2,
                clock=StepClock(),
                sleeper=lambda _seconds: None,
            )
        self.assertEqual(read_error.exception.code, "response_read_failed")
        self.assertNotIn("private serial failure", str(read_error.exception))

    def test_send_reports_success_only_after_acknowledgement(self) -> None:
        serial = ScriptedSerial([b"A\n"])
        with (
            mock.patch.object(robot_comm, "LOG_ONLY", False),
            mock.patch.object(robot_comm, "get_serial", return_value=serial),
            mock.patch.object(robot_comm, "write_log") as write_log,
        ):
            sent = robot_comm.send_motion_command("forward")

        self.assertEqual(sent, "F,20")
        self.assertEqual(serial.writes, [b"F,20\n"])
        self.assertEqual(serial.input_resets, 1)
        self.assertEqual(serial.flushed, 1)
        write_log.assert_called_once_with(
            "forward",
            "F,20",
            response="A",
            raw=b"A\n",
        )

    def test_rejection_and_timeout_are_not_reported_as_success(self) -> None:
        rejected_serial = ScriptedSerial([b"E\n"])
        with (
            mock.patch.object(robot_comm, "LOG_ONLY", False),
            mock.patch.object(
                robot_comm,
                "get_serial",
                return_value=rejected_serial,
            ),
            mock.patch.object(robot_comm, "write_log"),
        ):
            with self.assertRaises(robot_comm.MotionCommunicationError) as rejected:
                robot_comm.send_motion_command("forward")
        self.assertEqual(rejected.exception.code, "command_rejected")

        timeout_serial = ScriptedSerial()
        with (
            mock.patch.object(robot_comm, "LOG_ONLY", False),
            mock.patch.object(robot_comm, "get_serial", return_value=timeout_serial),
            mock.patch.object(
                robot_comm,
                "read_stm32_response",
                return_value=("", b""),
            ),
            mock.patch.object(robot_comm, "write_log"),
        ):
            with self.assertRaises(robot_comm.MotionCommunicationError) as timeout:
                robot_comm.send_motion_command("forward")
        self.assertEqual(timeout.exception.code, "response_timeout")
        self.assertTrue(timeout_serial.closed)

    def test_motion_disabled_mode_never_opens_serial(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "motion.log"
            with (
                mock.patch.object(robot_comm, "LOG_ONLY", True),
                mock.patch.object(robot_comm, "MOTION_LOG", log_path),
                mock.patch.object(robot_comm, "get_serial") as get_serial,
            ):
                sent = robot_comm.send_motion_command("stop")

            self.assertEqual(sent, "S,0")
            get_serial.assert_not_called()
            self.assertIn("response=LOG_ONLY", log_path.read_text(encoding="utf-8"))

    def test_concurrent_sends_do_not_interleave_transactions(self) -> None:
        serial = ConcurrentSerial()
        barrier = threading.Barrier(3)
        errors: list[BaseException] = []

        def send(command: str) -> None:
            try:
                barrier.wait()
                robot_comm.send_motion_command(command)
            except BaseException as exc:
                errors.append(exc)

        with (
            mock.patch.object(robot_comm, "LOG_ONLY", False),
            mock.patch.object(robot_comm, "get_serial", return_value=serial),
            mock.patch.object(robot_comm, "write_log"),
        ):
            threads = [
                threading.Thread(target=send, args=("forward",)),
                threading.Thread(target=send, args=("stop",)),
            ]
            for thread in threads:
                thread.start()
            barrier.wait()
            for thread in threads:
                thread.join(timeout=1.0)

        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertFalse(serial.overlap_observed)
        self.assertCountEqual(serial.writes, [b"F,20\n", b"S,0\n"])


if __name__ == "__main__":
    unittest.main()
