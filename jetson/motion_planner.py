#!/usr/bin/env python3
"""Color-target approach controller used by the thesis baseline."""

import time

import cv2

from .config import CAMERA_INDEX, FRAME_HEIGHT, FRAME_WIDTH
from .vision_color import (
    detect_target_color_from_frame,
    color_to_cn,
)

from .robot_comm import send_motion_command


# Controller thresholds tuned on the physical robot.
CENTER_TOLERANCE = 70
STOP_AREA = 35000
NEAR_STOP_CENTER_TOLERANCE = 120
CONTROL_INTERVAL = 0.10
COMMAND_REPEAT_INTERVAL = 0.30
MAX_CONTROL_SECONDS = 12.0
STOP_CONFIRM_FRAMES = 2
SAVE_DEBUG_EVERY_N = 10
PRINT_EVERY_N = 3


def decide_motion_from_target(target):
    """Choose a discrete motion command from target position and area."""
    if not target.get("found"):
        return "search"

    cx, _ = target["center"]
    area = target["area"]

    center_x = FRAME_WIDTH / 2
    error = cx - center_x
    abs_error = abs(error)

    if area >= STOP_AREA and abs_error <= NEAR_STOP_CENTER_TOLERANCE:
        return "stop"

    if abs_error > CENTER_TOLERANCE:
        if error < 0:
            return "turn_left"
        return "turn_right"

    if area < STOP_AREA:
        return "forward"

    return "stop"


def command_to_cn(command):
    mapping = {
        "search": "寻找",
        "turn_left": "左转",
        "turn_right": "右转",
        "forward": "前进",
        "backward": "后退",
        "stop": "停止",
    }
    return mapping.get(command, command)


def open_camera():
    """Open one low-buffer camera stream for the control loop."""
    cap = cv2.VideoCapture(CAMERA_INDEX, cv2.CAP_V4L2)

    if not cap.isOpened():
        cap = cv2.VideoCapture(CAMERA_INDEX)

    if not cap.isOpened():
        return None

    cap.set(cv2.CAP_PROP_FRAME_WIDTH, FRAME_WIDTH)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, FRAME_HEIGHT)

    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

    for _ in range(5):
        cap.read()

    return cap


def should_print_step(step, command, last_command, found):
    """Limit repeated control-loop messages without hiding state changes."""
    if command != last_command:
        return True

    if command == "stop":
        return True

    if not found:
        return True

    return step % PRINT_EVERY_N == 0


def move_to_color_object_fast(target_color):
    """Run the visual closed loop until arrival, timeout, or camera failure."""
    color_cn = color_to_cn(target_color)

    print(f"\n[快速目标移动] 开始寻找{color_cn}物体")

    cap = open_camera()

    if cap is None:
        send_motion_command("stop")
        return {
            "success": False,
            "reason": "camera_failed",
            "reply": "我尝试打开摄像头，但是没有成功读取画面。"
        }

    start_time = time.time()
    step = 0

    last_command = None
    last_send_time = 0.0
    same_stop_count = 0
    already_sent_final_stop = False

    try:
        while True:
            now = time.time()

            if now - start_time > MAX_CONTROL_SECONDS:
                print("[快速目标移动] 超时，停止")
                send_motion_command("stop")
                already_sent_final_stop = True
                return {
                    "success": False,
                    "reason": "timeout",
                    "reply": f"我已经尝试靠近{color_cn}物体，但还没有确认到达。"
                }

            ret, frame = cap.read()

            if not ret or frame is None:
                print("[快速目标移动] 读取画面失败")
                send_motion_command("stop")
                already_sent_final_stop = True
                return {
                    "success": False,
                    "reason": "read_failed",
                    "reply": "我读取摄像头画面失败，已经停止。"
                }

            step += 1
            save_debug = step % SAVE_DEBUG_EVERY_N == 0

            result = detect_target_color_from_frame(
                frame,
                target_color,
                save_debug=save_debug
            )

            found = False
            command = "stop"

            if not result["ok"]:
                command = "stop"
                print(f"[快速运动决策] 第{step}步：图像处理失败，决策=停止")

            elif not result["found"]:
                found = False
                command = "search"

                if should_print_step(step, command, last_command, found):
                    print(
                        f"[快速运动决策] 第{step}步："
                        f"未找到{color_cn}物体，决策={command_to_cn(command)}"
                    )

            else:
                found = True
                target = result["target"]

                cx, cy = target["center"]
                area = int(target["area"])
                position = target["position"]

                command = decide_motion_from_target(target)

                if should_print_step(step, command, last_command, found):
                    print(
                        f"[快速运动决策] 第{step}步："
                        f"{color_cn}物体 位置={position} 中心=({cx},{cy}) 面积={area} "
                        f"决策={command_to_cn(command)}"
                    )

            now_send = time.time()

            if command != last_command or (now_send - last_send_time) >= COMMAND_REPEAT_INTERVAL:
                send_motion_command(command)
                last_command = command
                last_send_time = now_send

            if command == "stop":
                same_stop_count += 1
            else:
                same_stop_count = 0

            if same_stop_count >= STOP_CONFIRM_FRAMES:
                if last_command != "stop":
                    send_motion_command("stop")

                already_sent_final_stop = True

                return {
                    "success": True,
                    "reason": "arrived",
                    "reply": f"我已经到达{color_cn}物体前方。"
                }

            time.sleep(CONTROL_INTERVAL)

    finally:
        cap.release()

        if not already_sent_final_stop:
            send_motion_command("stop")
