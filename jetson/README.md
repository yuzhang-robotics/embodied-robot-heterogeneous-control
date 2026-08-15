# Jetson

该目录存放在 Jetson Orin Nano 上运行的毕设基线上位机代码。

## 文件说明

| 文件 | 作用 |
| --- | --- |
| `app.py` | 语音助手主程序与意图调度 |
| `config.py` | 设备、模型、服务和运行路径配置 |
| `vision_vlm.py` | 摄像头场景描述与中文转写 |
| `vision_color.py` | HSV 颜色目标检测 |
| `motion_planner.py` | 目标靠近视觉闭环 |
| `robot_comm.py` | Jetson 与 STM32 串口通信 |
| `assets/` | 唤醒词配置和提示音 |
| `scripts/` | 环境安装辅助脚本 |

## 运行

基线环境为 Ubuntu 22.04、Python 3.10.12 和系统 OpenCV 4.5.4，并需要
`arecord`、`aplay`、`ollama` 以及 CUDA 版 `whisper-cli`。

从仓库根目录运行：

```bash
python3 -m jetson.app
```

程序默认处于安全日志模式，不会向 STM32 发送运动命令。完成离地测试并确认
串口与急停措施后，才可显式启用真实运动：

```bash
ROBOT_ENABLE_MOTION=1 python3 -m jetson.app
```

模型路径和设备编号可以通过 `ROBOT_*` 环境变量覆盖，默认值见 `config.py`。
录音、识别文本、调试图片和运动日志统一写入被 Git 忽略的 `runtime/`。
