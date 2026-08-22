# Jetson–STM32 Motion Protocol

The thesis baseline uses a small ASCII protocol over the robot's 3.3 V TTL UART link. It is intentionally easy to inspect during bring-up and will serve as the compatibility reference if a timestamped asynchronous protocol is introduced later.

> 中文简介：本页定义 Jetson 与 STM32 之间的运动指令、应答格式和超时约束。当前协议使用 115200 baud、3.3 V TTL 串口；有效帧返回 `A`，无效帧返回 `E`，连续约 1.2 秒未收到有效指令时 STM32 会主动停车。

## Serial configuration

| Setting | Value |
| --- | --- |
| Baud rate | 115200 |
| Data bits | 8 |
| Parity | none |
| Stop bits | 1 |
| Flow control | none |
| Line ending | `\n` (`\r` is ignored) |

Jetson TX connects to STM32 USART3 RX on PB11. STM32 USART3 TX on PB10 connects to Jetson RX. Both sides share ground; see the [hardware wiring notes](../docs/hardware/README.md#jetson-to-stm32-uart-link).

## Command frame

```text
<direction>,<speed>\n
```

| Field | Allowed values |
| --- | --- |
| `direction` | `F` forward, `B` backward, `L` rotate left, `R` rotate right, `S` stop |
| `speed` | decimal integer from `0` to `100` |

Examples:

```text
F,20
L,15
S,0
```

The current X-layout mecanum baseline only exposes forward, backward and in-place rotation. `L/R` do not mean lateral translation. Although the parser accepts a speed field with `S`, senders should use `S,0`.

## Response

The STM32 emits exactly one short response after a non-empty line is processed:

| Response | Meaning |
| --- | --- |
| `A\n` | the frame was valid and the command was applied |
| `E\n` | the frame was malformed, unsupported, out of range or exceeded the receive buffer |

An acknowledgment confirms that the firmware parsed and applied the command. It does not prove from encoder feedback that the chassis reached a requested velocity.

## Timing and safety contract

- The Jetson target tracker runs at a nominal 100 ms interval and repeats an unchanged command at least every 300 ms.
- The STM32 resets its watchdog only after a valid frame is applied.
- If no valid frame arrives for approximately 1.2 seconds, the STM32 sets all four motors to stop.
- Invalid traffic does not keep the chassis watchdog alive.
- The Jetson waits up to 800 ms for `A` or `E` and records missing responses in its local motion log.
- Opening, closing or losing the serial port must never be treated as a stop command; the STM32 watchdog provides the independent fallback.

Physical motion remains disabled in the Jetson package unless `ROBOT_ENABLE_MOTION=1` is explicitly set. This software guard and the STM32 timeout supplement, but do not replace, a physical motor-power switch.

## Current Jetson command mapping

| Internal command | Wire frame |
| --- | --- |
| `forward` | `F,20` |
| `backward` | `B,20` |
| `turn_left` | `L,15` |
| `turn_right` | `R,15` |
| `search` | `L,15` |
| `stop` | `S,0` |

The mapping lives in [`jetson/robot_comm.py`](../jetson/robot_comm.py); the parser and watchdog live in [`firmware/stm32f407/USER/protocol.c`](../firmware/stm32f407/USER/protocol.c) and [`chassis.c`](../firmware/stm32f407/USER/chassis.c).
