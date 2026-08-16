#ifndef __BSP_MOTOR_H
#define __BSP_MOTOR_H

#include "stm32f4xx.h"

typedef enum
{
    MOTOR_LF = 0,   /* left front */
    MOTOR_RF = 1,   /* right front */
    MOTOR_LB = 2,   /* left back */
    MOTOR_RB = 3    /* right back */
} MotorID_t;

void Motor_Init(void);
void Motor_SetSpeed(MotorID_t id, int16_t pwm_signed);
void Motor_Stop(MotorID_t id);
void Motor_StopAll(void);

#endif
