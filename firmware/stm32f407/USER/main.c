#include "stm32f4xx.h"
#include "bsp_encoder.h"
#include "bsp_led.h"
#include "bsp_tim13.h"
#include "bsp_usart3.h"
#include "chassis.h"
#include "protocol.h"

int main(void)
{
    NVIC_PriorityGroupConfig(NVIC_PriorityGroup_2);

    Chassis_Init();

    LED_Init();
    Encoder_Init();
    TIM13_EncoderSample_Init();
    USART3_BspInit();

    Chassis_Stop();

    while (1)
    {
        Protocol_PollReceive();
    }
}
