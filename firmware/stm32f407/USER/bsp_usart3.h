#ifndef __BSP_USART3_H
#define __BSP_USART3_H

#include "stm32f4xx.h"

void USART3_BspInit(void);
void USART3_SendString(const char *str);
uint8_t USART3_ReadByteNonBlocking(uint8_t *data);

#endif
