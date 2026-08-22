# STM32F407 Chassis Firmware

This firmware is the low-level controller from the bachelor's thesis. It runs on an ALIENTEK STM32F407ZGT6 minimum-system board connected through the custom base-driver PCB to four MG513P30_12V motors, four GMR encoders and two TB6612FNG drivers.

The chassis uses four mecanum wheels in an X layout. In the current protocol, `L` and `R` mean in-place rotation; lateral translation and full mecanum kinematics have not yet been implemented.

> 中文简介：本目录是“章鱼号”的 STM32F407 底盘固件，负责串口协议解析、四路电机 PWM、编码器采样和通信超时停车。工程已使用 Keil MDK 与 CMSIS-DAP 在实车上完成编译、烧录和运动验证。

## Responsibilities

- receive newline-terminated motion frames from the Jetson on USART3;
- validate direction and speed fields and return `A` or `E`;
- drive four direction channels and four 20 kHz PWM outputs;
- sample four quadrature encoders every 10 ms;
- stop all motors if no valid command is received for about 1.2 seconds;
- start with motor direction pins and PWM outputs in a safe stopped state.

## Project structure

| Path | Contents |
| --- | --- |
| `USER/main.c` | Initialization order and protocol polling loop |
| `USER/app_config.h` | Pin assignments, timing, motor and encoder calibration |
| `USER/protocol.*` | ASCII frame parser and acknowledgments |
| `USER/chassis.*` | Command-to-wheel mapping and communication watchdog |
| `USER/bsp_motor.*` | TIM8 PWM and TB6612 direction control |
| `USER/bsp_encoder.*` | TIM1/2/3/4 quadrature encoders and speed calculation |
| `USER/bsp_tim13.*` | 10 ms encoder/watchdog tick and heartbeat LEDs |
| `USER/bsp_usart3.*` | Jetson UART transport |
| `CORE/` | Cortex-M4 startup and CMSIS support |
| `FWLIB/` | Only the STM32F4 SPL modules used by this project |

The retained Standard Peripheral Library modules are GPIO, RCC, TIM, USART and `misc` (NVIC). Unused vendor drivers and the unused board-template `SYSTEM` layer were removed so that the public tree matches the actual build.

## Build and flash

The validated toolchain was Keil MDK V5.35 with ARM Compiler 5.06 update 7.

1. Open [`USER/Octopus_STM32F407.uvprojx`](USER/Octopus_STM32F407.uvprojx) in Keil µVision.
2. Select the `Octopus_STM32F407` target.
3. Rebuild all target files.
4. Flash and debug with a CMSIS-DAP adapter.

The target device is STM32F407ZG and the system clock is 168 MHz. A clean build should report zero errors and zero warnings.

## Command behavior

| Frame | Chassis action |
| --- | --- |
| `F,<speed>` | all four wheels forward |
| `B,<speed>` | all four wheels backward |
| `L,<speed>` | rotate left in place |
| `R,<speed>` | rotate right in place |
| `S,0` | stop all four wheels |

`speed` is an integer from 0 to 100 and is mapped linearly to a PWM value from 0 to 1000. The motor direction signs in `app_config.h` reflect the tested PCB wiring and should not be changed without an off-ground wheel-direction test.

The encoder layer calculates wheel speed and linear speed, but those measurements are not fed back into motor output in this baseline. The controller is therefore open-loop even though encoder acquisition is active.

See the [protocol specification](../../protocol/README.md) for framing, responses and timing.

## Hardware validation record

The thesis baseline was tested on the assembled robot, first with the wheels raised and then on the floor:

- Keil full rebuild: 0 errors and 0 warnings;
- `F/B/L/R/S` wheel directions matched the intended X-layout motion;
- valid commands returned `A`, and an invalid `X,99` frame returned `E` without motion;
- stopping the Jetson command stream stopped the wheels after approximately 1.2 seconds;
- all four encoders produced feedback with calibrated forward signs;
- the Jetson color-target loop produced acknowledged motion commands through the custom PCB.

Keep the wheels raised during initial flashing and serial tests. The firmware watchdog is a final fallback, not a replacement for a physical power switch or proper battery protection.

## Third-party code

CMSIS, device support and STM32F4 Standard Peripheral Library files retain their original copyright headers and license terms. See [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).
