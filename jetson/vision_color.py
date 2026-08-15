#!/usr/bin/env python3
import cv2
import numpy as np
import time

from .config import CAMERA_INDEX, FRAME_HEIGHT, FRAME_WIDTH, RUNTIME_DIR

VISION_RAW_PATH = RUNTIME_DIR / "vision_raw.jpg"
VISION_DEBUG_PATH = RUNTIME_DIR / "vision_debug.jpg"

MIN_AREA = 800


def get_position_name(cx, width):
    if cx < width * 0.4:
        return "左侧"
    elif cx > width * 0.6:
        return "右侧"
    else:
        return "中间"


def find_color_object(frame, color_name):
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)

    if color_name == "red":
        lower1 = np.array([0, 80, 80])
        upper1 = np.array([10, 255, 255])
        lower2 = np.array([170, 80, 80])
        upper2 = np.array([180, 255, 255])
        mask1 = cv2.inRange(hsv, lower1, upper1)
        mask2 = cv2.inRange(hsv, lower2, upper2)
        mask = mask1 | mask2

    elif color_name == "blue":
        lower = np.array([100, 80, 80])
        upper = np.array([130, 255, 255])
        mask = cv2.inRange(hsv, lower, upper)

    elif color_name == "green":
        lower = np.array([40, 50, 50])
        upper = np.array([85, 255, 255])
        mask = cv2.inRange(hsv, lower, upper)

    elif color_name == "yellow":
        lower = np.array([20, 80, 80])
        upper = np.array([35, 255, 255])
        mask = cv2.inRange(hsv, lower, upper)

    else:
        raise ValueError(f"不支持的颜色：{color_name}")

    kernel = np.ones((5, 5), np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)

    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    if not contours:
        return {
            "found": False,
            "color": color_name
        }

    c = max(contours, key=cv2.contourArea)
    area = cv2.contourArea(c)

    if area < MIN_AREA:
        return {
            "found": False,
            "color": color_name,
            "area": area
        }

    x, y, w, h = cv2.boundingRect(c)
    cx = x + w // 2
    cy = y + h // 2

    height, width = frame.shape[:2]
    position = get_position_name(cx, width)

    return {
        "found": True,
        "color": color_name,
        "area": area,
        "bbox": (x, y, w, h),
        "center": (cx, cy),
        "position": position
    }


def draw_result(frame, result):
    if not result.get("found"):
        return

    color_name = result["color"]
    x, y, w, h = result["bbox"]
    cx, cy = result["center"]

    if color_name == "red":
        box_color = (0, 0, 255)
        label = "red"
    elif color_name == "blue":
        box_color = (255, 0, 0)
        label = "blue"
    elif color_name == "green":
        box_color = (0, 255, 0)
        label = "green"
    elif color_name == "yellow":
        box_color = (0, 255, 255)
        label = "yellow"
    else:
        box_color = (255, 255, 255)
        label = color_name

    cv2.rectangle(frame, (x, y), (x + w, y + h), box_color, 2)
    cv2.circle(frame, (cx, cy), 5, box_color, -1)

    cv2.putText(
        frame,
        f"{label} area={int(result['area'])} pos={result['position']}",
        (x, max(30, y - 10)),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.6,
        box_color,
        2
    )


def capture_frame(camera_index=CAMERA_INDEX):
    cap = cv2.VideoCapture(camera_index, cv2.CAP_V4L2)

    if not cap.isOpened():
        print(f"无法打开摄像头 index={camera_index}")
        return None

    cap.set(cv2.CAP_PROP_FRAME_WIDTH, FRAME_WIDTH)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, FRAME_HEIGHT)

    for _ in range(10):
        ret, frame = cap.read()
        time.sleep(0.05)

    ret, frame = cap.read()
    cap.release()

    if not ret or frame is None:
        print("摄像头打开成功，但读取图像失败")
        return None

    return frame


def detect_scene():
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    frame = capture_frame()

    if frame is None:
        return {
            "ok": False,
            "error": "camera_failed",
            "objects": []
        }

    cv2.imwrite(str(VISION_RAW_PATH), frame)

    debug = frame.copy()

    objects = []
    for color in ["red", "blue", "green", "yellow"]:
        result = find_color_object(frame, color)
        if result.get("found"):
            objects.append(result)
            draw_result(debug, result)

    cv2.imwrite(str(VISION_DEBUG_PATH), debug)

    return {
        "ok": True,
        "objects": objects,
        "raw_path": str(VISION_RAW_PATH),
        "debug_path": str(VISION_DEBUG_PATH)
    }


def color_to_cn(color):
    mapping = {
        "red": "红色",
        "blue": "蓝色",
        "green": "绿色",
        "yellow": "黄色",
    }
    return mapping.get(color, color)


def describe_scene():
    scene = detect_scene()

    if not scene["ok"]:
        return "我尝试打开摄像头，但是没有成功读取画面。"

    objects = scene["objects"]

    if not objects:
        return "我暂时没有看到明显的红色、蓝色、绿色或黄色物体。"

    parts = []
    for obj in objects:
        color_cn = color_to_cn(obj["color"])
        position = obj["position"]
        parts.append(f"一个{color_cn}物体在画面{position}")

    if len(parts) == 1:
        return f"我看到了{parts[0]}。"

    return "我看到了" + "，".join(parts) + "。"


def detect_target_color_from_frame(frame, target_color, save_debug=False):
    """
    对已经读取到的一帧图像进行指定颜色目标检测。
    这个函数不会打开摄像头，因此适合实时控制循环。
    """
    if frame is None:
        return {
            "ok": False,
            "found": False,
            "error": "empty_frame"
        }

    debug = frame.copy()
    result = find_color_object(frame, target_color)

    if result.get("found"):
        draw_result(debug, result)

    if save_debug:
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(VISION_DEBUG_PATH), debug)

    return {
        "ok": True,
        "found": bool(result.get("found")),
        "target": result
    }


def detect_target_color(target_color):
    """
    检测指定颜色目标。
    返回示例：
    {
        "ok": True,
        "found": True,
        "target": {...},
        "debug_path": "..."
    }
    """
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    frame = capture_frame()

    if frame is None:
        return {
            "ok": False,
            "found": False,
            "error": "camera_failed"
        }

    cv2.imwrite(str(VISION_RAW_PATH), frame)

    debug = frame.copy()
    result = find_color_object(frame, target_color)

    if result.get("found"):
        draw_result(debug, result)

    cv2.imwrite(str(VISION_DEBUG_PATH), debug)

    return {
        "ok": True,
        "found": bool(result.get("found")),
        "target": result,
        "debug_path": str(VISION_DEBUG_PATH)
    }


if __name__ == "__main__":
    scene = detect_scene()
    print(scene)
    print(describe_scene())
