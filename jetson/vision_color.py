#!/usr/bin/env python3
"""HSV color segmentation used by the visual motion controller."""

import cv2
import numpy as np

from .config import RUNTIME_DIR

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


def color_to_cn(color):
    mapping = {
        "red": "红色",
        "blue": "蓝色",
        "green": "绿色",
        "yellow": "黄色",
    }
    return mapping.get(color, color)


def detect_target_color_from_frame(frame, target_color, save_debug=False):
    """Detect one target color in a frame already owned by the control loop."""
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
