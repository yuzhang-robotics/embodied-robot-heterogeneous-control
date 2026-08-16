# STM32F407 Firmware

本科毕业设计使用的 STM32F407ZGT6 底盘固件，基于 Keil µVision 和 STM32F4 标准外设库 V1.4.0。

机器人采用 X 型安装的四轮麦克纳姆底盘。当前基线协议中的 `L/R` 表示原地旋转，尚未提供横向平移指令。

## 功能

- 通过 TIM8 输出四路电机 PWM。
- 通过 TIM1、TIM2、TIM3 和 TIM4 采集四路编码器。
- 通过 USART3 接收 Jetson 的运动指令并返回执行状态。
- 通信中断超过 1.2 秒时自动停止底盘。

## 目录

- `USER/`：机器人应用、底盘、协议和板级驱动代码。
- `SYSTEM/`：系统延时与调试串口支持代码。
- `CORE/`：Cortex-M4 启动与 CMSIS 支持文件。
- `FWLIB/`：STM32F4 标准外设库。

## 构建

使用 Keil µVision 打开 `USER/Template.uvprojx`，选择 `Template` 目标后构建。目标器件为 STM32F407ZG，系统时钟为 168 MHz。

第三方组件的版权和许可信息见 `THIRD_PARTY_NOTICES.md`。
