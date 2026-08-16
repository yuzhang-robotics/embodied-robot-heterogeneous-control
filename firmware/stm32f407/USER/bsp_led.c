#include "bsp_led.h"
#include "app_config.h"

void LED_Init(void)
{
    GPIO_InitTypeDef GPIO_InitStructure;

    RCC_AHB1PeriphClockCmd(LED_GPIO_CLK, ENABLE);

    GPIO_InitStructure.GPIO_Pin = LED0_PIN | LED1_PIN;
    GPIO_InitStructure.GPIO_Mode = GPIO_Mode_OUT;
    GPIO_InitStructure.GPIO_OType = GPIO_OType_PP;
    GPIO_InitStructure.GPIO_Speed = GPIO_Speed_50MHz;
    GPIO_InitStructure.GPIO_PuPd = GPIO_PuPd_UP;
    GPIO_Init(LED_GPIO, &GPIO_InitStructure);

    GPIO_SetBits(LED_GPIO, LED0_PIN | LED1_PIN);
}

void LED0_Toggle(void) { GPIO_ToggleBits(LED_GPIO, LED0_PIN); }
void LED1_Toggle(void) { GPIO_ToggleBits(LED_GPIO, LED1_PIN); }
void LED0_On(void)     { GPIO_ResetBits(LED_GPIO, LED0_PIN); }
void LED0_Off(void)    { GPIO_SetBits(LED_GPIO, LED0_PIN); }
void LED1_On(void)     { GPIO_ResetBits(LED_GPIO, LED1_PIN); }
void LED1_Off(void)    { GPIO_SetBits(LED_GPIO, LED1_PIN); }
