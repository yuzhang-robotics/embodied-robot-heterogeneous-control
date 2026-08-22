#include "bsp_usart3.h"
#include "app_config.h"

static void USART3_SendByte(uint8_t data);

void USART3_BspInit(void)
{
    GPIO_InitTypeDef GPIO_InitStructure;
    USART_InitTypeDef USART_InitStructure;

    RCC_AHB1PeriphClockCmd(JETSON_USART_GPIO_CLK, ENABLE);
    RCC_APB1PeriphClockCmd(JETSON_USART_CLK, ENABLE);

    GPIO_PinAFConfig(JETSON_USART_GPIO, JETSON_USART_TX_SRC, JETSON_USART_AF);
    GPIO_PinAFConfig(JETSON_USART_GPIO, JETSON_USART_RX_SRC, JETSON_USART_AF);

    GPIO_InitStructure.GPIO_Pin = JETSON_USART_TX_PIN | JETSON_USART_RX_PIN;
    GPIO_InitStructure.GPIO_Mode = GPIO_Mode_AF;
    GPIO_InitStructure.GPIO_Speed = GPIO_Speed_100MHz;
    GPIO_InitStructure.GPIO_OType = GPIO_OType_PP;
    GPIO_InitStructure.GPIO_PuPd = GPIO_PuPd_UP;
    GPIO_Init(JETSON_USART_GPIO, &GPIO_InitStructure);

    USART_InitStructure.USART_BaudRate = JETSON_USART_BAUD;
    USART_InitStructure.USART_WordLength = USART_WordLength_8b;
    USART_InitStructure.USART_StopBits = USART_StopBits_1;
    USART_InitStructure.USART_Parity = USART_Parity_No;
    USART_InitStructure.USART_HardwareFlowControl = USART_HardwareFlowControl_None;
    USART_InitStructure.USART_Mode = USART_Mode_Rx | USART_Mode_Tx;
    USART_Init(JETSON_USART, &USART_InitStructure);

    USART_Cmd(JETSON_USART, ENABLE);
}

static void USART3_SendByte(uint8_t data)
{
    while (USART_GetFlagStatus(JETSON_USART, USART_FLAG_TXE) == RESET) {}
    USART_SendData(JETSON_USART, data);
}

void USART3_SendString(const char *str)
{
    while (*str)
    {
        USART3_SendByte((uint8_t)(*str++));
    }
    while (USART_GetFlagStatus(JETSON_USART, USART_FLAG_TC) == RESET) {}
}

uint8_t USART3_ReadByteNonBlocking(uint8_t *data)
{
    if (USART_GetFlagStatus(JETSON_USART, USART_FLAG_RXNE) != RESET)
    {
        *data = (uint8_t)USART_ReceiveData(JETSON_USART);
        return 1;
    }
    return 0;
}
