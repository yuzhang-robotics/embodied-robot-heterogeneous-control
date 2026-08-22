# Embodied Robot Heterogeneous Control

异构计算架构下的具身机器人异步推理与实时控制系统

## 项目目标

本项目基于 **Jetson Orin Nano Super 8GB + STM32F407** 轮式机器人平台，
在本科毕业设计“语音交互与视觉感知轮式机器人”的基础上，进一步研究异构
计算架构下的异步推理与实时控制。

Jetson 负责语音交互、视觉感知、模型推理和任务决策；STM32 负责电机驱动、
编码器采集、运动控制和安全保护。项目的核心目标是将高层智能推理与底层
实时控制解耦，提高机器人运行的实时性、稳定性和可扩展性。

This project studies asynchronous inference and real-time control for a wheeled
embodied robot using a Jetson Orin Nano and STM32 heterogeneous architecture.

## 仓库结构

| 路径 | 作用 |
| --- | --- |
| `jetson/` | Jetson 上运行的语音、视觉、推理、决策与通信代码 |
| `firmware/stm32f407/` | STM32F407 电机、编码器、运动控制与通信固件 |
| `hardware/pcb/` | 自制底层驱动板的可编辑 PCB 设计文件 |
| `protocol/` | Jetson 与 STM32 之间的通信协议说明 |
| `experiments/` | 实验配置、分析代码与整理后的结果 |
| `tools/hil/` | 硬件在环和系统联调工具 |
| `docs/architecture/` | 系统架构与模块关系文档 |
| `docs/hardware/` | 实物平台、引脚分配、接线与安全说明 |

## 当前状态

Jetson 与 STM32 本科毕业设计基线已完成实物联调，并标记为
`v0.1.0-thesis-baseline`。后续工作将围绕异步推理、实时控制和系统实验展开。

## License

软件代码与项目文档采用 [MIT License](LICENSE)。`hardware/` 下的可编辑硬件设计源文件
采用 [CERN-OHL-P-2.0](hardware/LICENSE)，适用声明见 [hardware/NOTICE](hardware/NOTICE)。
第三方模型、数据集、元件库和厂商库遵循各自的许可证。
