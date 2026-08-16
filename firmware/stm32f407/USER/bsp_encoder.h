#ifndef __BSP_ENCODER_H
#define __BSP_ENCODER_H

#include "stm32f4xx.h"

typedef enum
{
    ENCODER_LF = 0,
    ENCODER_RF = 1,
    ENCODER_LB = 2,
    ENCODER_RB = 3
} EncoderID_t;

typedef struct
{
    volatile int32_t total_count;
    volatile int32_t delta_count;
    volatile float rpm;
    volatile float speed_mps;
} EncoderData_t;

void Encoder_Init(void);
void Encoder_Update10ms(void);
int32_t Encoder_GetTotalCount(EncoderID_t id);
int32_t Encoder_GetDeltaCount(EncoderID_t id);
float Encoder_GetRPM(EncoderID_t id);
float Encoder_GetSpeedMPS(EncoderID_t id);

#endif
