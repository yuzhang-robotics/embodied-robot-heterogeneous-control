#!/usr/bin/env python3
import time
import cv2

from .config import CAMERA_INDEX, FRAME_HEIGHT, FRAME_WIDTH
from .vision_color import (
    detect_target_color_from_frame,
    color_to_cn,
)

from .robot_comm import send_motion_command


# ========== 控制参数 ==========
# 普通对准容差：目标中心偏离画面中心超过这个值，就转向
CENTER_TOLERANCE = 70

# 近距离停止面积：目标面积达到这个值，认为已经接近目标
STOP_AREA = 35000

# 近距离停止容差：
# 目标已经很近时，允许更大的中心偏差，避免贴近目标后还一直转向
NEAR_STOP_CENTER_TOLERANCE = 120

# 控制周期：0.10 秒约等于 10Hz
CONTROL_INTERVAL = 0.10

# 相同命令重复发送间隔：
# 命令变化时立即发送；命令不变时，每隔这个时间重复发一次
COMMAND_REPEAT_INTERVAL = 0.30

# 最长控制时间，防止任务无限执行
MAX_CONTROL_SECONDS = 12.0

# 连续多少帧判断为 stop，才认为真正到达
STOP_CONFIRM_FRAMES = 2

# 每隔多少帧保存一次 debug 图
SAVE_DEBUG_EVERY_N = 10

# 每隔多少帧打印一次普通视觉日志
# 命令变化、停止、未找到目标时仍然会立即打印
PRINT_EVERY_N = 3


def decide_motion_from_target(target):
    """
    根据目标位置和面积生成运动决策。

    控制策略：
    1. 如果目标已经足够近，并且没有严重偏离中心，则停止。
    2. 如果目标偏离中心较多，则先转向对准。
    3. 如果目标居中但还不够近，则前进。
    """
    if not target.get("found"):
        return "search"

    cx, cy = target["center"]
    area = target["area"]

    center_x = FRAME_WIDTH / 2
    error = cx - center_x
    abs_error = abs(error)

    # 目标已经很近时，优先停止，降低碰撞风险
    if area >= STOP_AREA and abs_error <= NEAR_STOP_CENTER_TOLERANCE:
        return "stop"

    # 目标偏离中心明显时，先转向
    if abs_error > CENTER_TOLERANCE:
        if error < 0:
            return "turn_left"
        else:
            return "turn_right"

    # 目标居中但还不够近，继续前进
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
    """
    打开摄像头一次，供快速控制循环持续读取。
    不要在每一轮控制中反复打开摄像头。
    """
    cap = cv2.VideoCapture(CAMERA_INDEX, cv2.CAP_V4L2)

    if not cap.isOpened():
        # 如果 V4L2 后端失败，回退到默认方式
        cap = cv2.VideoCapture(CAMERA_INDEX)

    if not cap.isOpened():
        return None

    cap.set(cv2.CAP_PROP_FRAME_WIDTH, FRAME_WIDTH)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, FRAME_HEIGHT)

    # 尝试降低缓存，减少画面延迟
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

    # 预热几帧，让曝光稳定
    for _ in range(5):
        cap.read()

    return cap


def should_print_step(step, command, last_command, found):
    """
    控制终端日志频率，避免刷屏。
    """
    if command != last_command:
        return True

    if command == "stop":
        return True

    if not found:
        return True

    return step % PRINT_EVERY_N == 0


def move_to_color_object_fast(target_color):
    """
    快速视觉闭环：移动到指定颜色物体前方。

    当前阶段：
    - 打开摄像头
    - 连续读取画面
    - 根据目标位置和面积生成运动命令
    - 调用 send_motion_command()
    - 安全模式下写日志；显式启用运动后通过串口发送给 STM32
    """
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
            save_debug = (step % SAVE_DEBUG_EVERY_N == 0)

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

            # 命令变化时立即发送；
            # 命令不变时，只按 COMMAND_REPEAT_INTERVAL 周期重复发送。
            now_send = time.time()

            if command != last_command or (now_send - last_send_time) >= COMMAND_REPEAT_INTERVAL:
                send_motion_command(command)
                last_command = command
                last_send_time = now_send

            # 连续多帧 stop 才确认到达，避免单帧误判
            if command == "stop":
                same_stop_count += 1
            else:
                same_stop_count = 0

            if same_stop_count >= STOP_CONFIRM_FRAMES:
                # 如果已经发过 stop，就不要重复发很多次
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

        # 只在异常、超时前没有明确停止时补发 stop
        if not already_sent_final_stop:
            send_motion_command("stop")


if __name__ == "__main__":
    result = move_to_color_object_fast("red")
    print(result)
