#include "bsp_tim13.h"
#include "app_config.h"
#include "bsp_encoder.h"
#include "chassis.h"
#include "bsp_led.h"

void TIM13_EncoderSample_Init(void)
{
    TIM_TimeBaseInitTypeDef TIM_TimeBaseStructure;
    NVIC_InitTypeDef NVIC_InitStructure;

    RCC_APB1PeriphClockCmd(RCC_APB1Periph_TIM13, ENABLE);

    TIM_TimeBaseStructure.TIM_Period = TIM13_SAMPLE_ARR;
    TIM_TimeBaseStructure.TIM_Prescaler = TIM13_SAMPLE_PSC;
    TIM_TimeBaseStructure.TIM_ClockDivision = TIM_CKD_DIV1;
    TIM_TimeBaseStructure.TIM_CounterMode = TIM_CounterMode_Up;
    TIM_TimeBaseInit(TIM13, &TIM_TimeBaseStructure);

    TIM_ClearITPendingBit(TIM13, TIM_IT_Update);
    TIM_ITConfig(TIM13, TIM_IT_Update, ENABLE);

    NVIC_InitStructure.NVIC_IRQChannel = TIM8_UP_TIM13_IRQn;
    NVIC_InitStructure.NVIC_IRQChannelPreemptionPriority = 2;
    NVIC_InitStructure.NVIC_IRQChannelSubPriority = 1;
    NVIC_InitStructure.NVIC_IRQChannelCmd = ENABLE;
    NVIC_Init(&NVIC_InitStructure);

    TIM_Cmd(TIM13, ENABLE);
}

static uint16_t g_heartbeat_ticks = 0;
static uint16_t g_heartbeat_phase = 0;

void TIM8_UP_TIM13_IRQHandler(void)
{
    if (TIM_GetITStatus(TIM13, TIM_IT_Update) != RESET)
    {
        g_heartbeat_ticks++;

        if (g_heartbeat_ticks >= 100)
        {
            g_heartbeat_ticks = 0;
            if (g_heartbeat_phase == 0)
            {
                LED0_On();
                LED1_Off();
                g_heartbeat_phase = 1;
            }
            else
            {
                LED0_Off();
                LED1_On();
                g_heartbeat_phase = 0;
            }
        }

        TIM_ClearITPendingBit(TIM13, TIM_IT_Update);
        Encoder_Update10ms();
        Chassis_Task10ms();
    }
}
