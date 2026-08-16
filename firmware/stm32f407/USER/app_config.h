#ifndef __APP_CONFIG_H
#define __APP_CONFIG_H

#include "stm32f4xx.h"

/* ============================================================
 * Octopus STM32F407ZGT6 chassis configuration
 * SPL: STM32F4 Standard Peripheral Library
 * Board: ALIENTEK STM32F407ZGT6 minimum system board on the robot carrier PCB
 * Chassis: four mecanum wheels in X configuration; L/R commands rotate in place
 * ============================================================ */

/* ---------------- Motor PWM: TIM8 CH1~CH4 ----------------
 * LF: PC6  TIM8_CH1
 * RF: PC7  TIM8_CH2
 * LB: PC8  TIM8_CH3
 * RB: PC9  TIM8_CH4
 */
#define MOTOR_PWM_GPIO              GPIOC
#define MOTOR_PWM_GPIO_CLK          RCC_AHB1Periph_GPIOC
#define MOTOR_PWM_AF                GPIO_AF_TIM8
#define MOTOR_PWM_TIM               TIM8
#define MOTOR_PWM_TIM_CLK           RCC_APB2Periph_TIM8

#define MOTOR_LF_PWM_PIN            GPIO_Pin_6
#define MOTOR_RF_PWM_PIN            GPIO_Pin_7
#define MOTOR_LB_PWM_PIN            GPIO_Pin_8
#define MOTOR_RB_PWM_PIN            GPIO_Pin_9

#define MOTOR_LF_PWM_SRC            GPIO_PinSource6
#define MOTOR_RF_PWM_SRC            GPIO_PinSource7
#define MOTOR_LB_PWM_SRC            GPIO_PinSource8
#define MOTOR_RB_PWM_SRC            GPIO_PinSource9

/* 168MHz / (0+1) / (8399+1) = 20kHz */
#define MOTOR_PWM_PSC               0
#define MOTOR_PWM_ARR               8399
#define MOTOR_PWM_MAX               1000

/* ---------------- Motor direction pins ----------------
 * LF: AIN1 PG6, AIN2 PG7
 * RF: BIN1 PC0, BIN2 PC1
 * LB: CIN1 PC2, CIN2 PC3
 * RB: DIN1 PC4, DIN2 PC5
 */
#define MOTOR_LF_DIR_GPIO           GPIOG
#define MOTOR_LF_DIR_GPIO_CLK       RCC_AHB1Periph_GPIOG
#define MOTOR_LF_IN1_PIN            GPIO_Pin_6
#define MOTOR_LF_IN2_PIN            GPIO_Pin_7

#define MOTOR_RFLBRB_DIR_GPIO       GPIOC
#define MOTOR_RFLBRB_DIR_GPIO_CLK   RCC_AHB1Periph_GPIOC
#define MOTOR_RF_IN1_PIN            GPIO_Pin_0
#define MOTOR_RF_IN2_PIN            GPIO_Pin_1
#define MOTOR_LB_IN1_PIN            GPIO_Pin_2
#define MOTOR_LB_IN2_PIN            GPIO_Pin_3
#define MOTOR_RB_IN1_PIN            GPIO_Pin_4
#define MOTOR_RB_IN2_PIN            GPIO_Pin_5

/* Motor direction correction.
 * After real wiring test, change 1 to -1 for any motor whose forward direction is reversed.
 */
#define MOTOR_LF_DIR_SIGN           1
#define MOTOR_RF_DIR_SIGN           -1
#define MOTOR_LB_DIR_SIGN           1
#define MOTOR_RB_DIR_SIGN           -1

/* Jetson sends speed 0~100. STM32 maps it to PWM 0~1000. */
#define JETSON_SPEED_MAX            100

/* If no valid command is received for this long, stop motors.
 * 0 disables watchdog. For target tracking, Jetson repeats commands about every 0.3s.
 */
#define CHASSIS_CMD_TIMEOUT_MS      1200

/* ---------------- Encoders ----------------
 * LF: TIM4 CH1 PB6, CH2 PB7
 * RF: TIM3 CH1 PA6, CH2 PA7
 * LB: TIM2 CH1 PA5, CH2 PA1
 * RB: TIM1 CH1 PE9, CH2 PE11
 */
#define ENCODER_LINE                500.0f
#define MOTOR_REDUCTION_RATIO       30.0f
#define ENCODER_QUADRATURE          4.0f

/* User note: 23.5619 is wheel circumference.
 * Here it is treated as centimeter: 23.5619 cm = 0.235619 m.
 * If your unit is millimeter, change it to 0.0235619f.
 */
#define WHEEL_CIRCUMFERENCE_M       0.235619f
#define ENCODER_SAMPLE_DT_S         0.01f

#define ENCODER_COUNTS_PER_WHEEL_REV  (ENCODER_LINE * ENCODER_QUADRATURE * MOTOR_REDUCTION_RATIO)

/* Encoder direction correction.
 * If wheel rotates forward but encoder speed is negative, change that wheel's sign.
 */
#define ENCODER_LF_DIR_SIGN         -1
#define ENCODER_RF_DIR_SIGN         1
#define ENCODER_LB_DIR_SIGN         -1
#define ENCODER_RB_DIR_SIGN         1

/* ---------------- TIM13 encoder read timer ----------------
 * TIM13 is on APB1. In the common 168MHz system clock configuration:
 * PCLK1 = 42MHz, APB1 timer clock = 84MHz.
 * 84MHz / 8400 / 100 = 100Hz -> 10ms.
 */
#define TIM13_SAMPLE_PSC            (8400 - 1)
#define TIM13_SAMPLE_ARR            (100 - 1)

/* ---------------- USART3 Jetson communication ----------------
 * PB10 USART3_TX -> Jetson RX
 * PB11 USART3_RX <- Jetson TX
 */
#define UART_RX_BUF_SIZE            32
#define JETSON_USART                USART3
#define JETSON_USART_CLK            RCC_APB1Periph_USART3
#define JETSON_USART_GPIO           GPIOB
#define JETSON_USART_GPIO_CLK       RCC_AHB1Periph_GPIOB
#define JETSON_USART_TX_PIN         GPIO_Pin_10
#define JETSON_USART_RX_PIN         GPIO_Pin_11
#define JETSON_USART_TX_SRC         GPIO_PinSource10
#define JETSON_USART_RX_SRC         GPIO_PinSource11
#define JETSON_USART_AF             GPIO_AF_USART3
#define JETSON_USART_BAUD           115200

/* ---------------- LEDs on common ALIENTEK F407 board ---------------- */
#define LED_GPIO                    GPIOF
#define LED_GPIO_CLK                RCC_AHB1Periph_GPIOF
#define LED0_PIN                    GPIO_Pin_9
#define LED1_PIN                    GPIO_Pin_10

#endif
