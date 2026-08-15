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
| `protocol/` | Jetson 与 STM32 之间的通信协议说明 |
| `experiments/` | 实验配置、分析代码与整理后的结果 |
| `tools/hil/` | 硬件在环和系统联调工具 |
| `docs/architecture/` | 系统架构与模块关系文档 |

## 当前状态

仓库正在进行基础结构初始化，后续将分别导入 Jetson 与 STM32 的本科毕业设计
基线代码。

## License

本项目采用 [MIT License](LICENSE)。第三方模型、数据集和厂商库遵循各自的
许可证。
