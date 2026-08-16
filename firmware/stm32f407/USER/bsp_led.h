#ifndef __BSP_LED_H
#define __BSP_LED_H

#include "stm32f4xx.h"

void LED_Init(void);
void LED0_Toggle(void);
void LED1_Toggle(void);
void LED0_On(void);
void LED0_Off(void);
void LED1_On(void);
void LED1_Off(void);

#endif
