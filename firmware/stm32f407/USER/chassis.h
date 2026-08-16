#ifndef __CHASSIS_H
#define __CHASSIS_H

#include "stm32f4xx.h"

typedef struct
{
    char direction;  /* F/B/L/R/S */
    int speed;       /* 0~100 */
} MotionCommand_t;

void Chassis_Init(void);
void Chassis_ApplyCommand(const MotionCommand_t *cmd);
void Chassis_Stop(void);
void Chassis_Task10ms(void);

#endif
