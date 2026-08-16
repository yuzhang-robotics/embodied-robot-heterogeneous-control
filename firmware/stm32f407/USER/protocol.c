#include "protocol.h"
#include "app_config.h"
#include "bsp_usart3.h"
#include "bsp_led.h"
#include <string.h>

static char rx_buf[UART_RX_BUF_SIZE];
static uint16_t rx_index = 0;

static uint8_t ParseSpeed(const char *s, int *speed)
{
    int value = 0;

    if (s == 0 || speed == 0 || *s == '\0') return 0;

    while (*s)
    {
        if (*s < '0' || *s > '9') return 0;
        value = value * 10 + (*s - '0');
        if (value > JETSON_SPEED_MAX) return 0;
        s++;
    }

    *speed = value;
    return 1;
}

uint8_t ParseMotionCommand(const char *line, MotionCommand_t *cmd)
{
    char dir;
    int speed;

    if (line == 0 || cmd == 0) return 0;

    if (line[0] == '\0') return 0;

    dir = line[0];
    if (!(dir == 'F' || dir == 'B' || dir == 'L' || dir == 'R' || dir == 'S')) return 0;
    if (line[1] != ',') return 0;

    if (!ParseSpeed(&line[2], &speed)) return 0;

    cmd->direction = dir;
    cmd->speed = speed;
    return 1;
}

static void HandleMotionCommand(MotionCommand_t *cmd)
{
    Chassis_ApplyCommand(cmd);

    if (cmd->direction == 'F' || cmd->direction == 'B')
    {
        LED0_Toggle();
    }
    else
    {
        LED1_Toggle();
    }

    /* Keep Jetson ACK short and clean. Do not print debug text on this UART. */
    USART3_SendString("A\n");
}

static void ProcessLine(char *line)
{
    MotionCommand_t cmd;

    if (ParseMotionCommand(line, &cmd))
    {
        HandleMotionCommand(&cmd);
    }
    else
    {
        USART3_SendString("E\n");
    }
}

void Protocol_PollReceive(void)
{
    uint8_t ch;

    if (USART3_ReadByteNonBlocking(&ch))
    {
        if (ch == '\n')
        {
            rx_buf[rx_index] = '\0';
            if (rx_index > 0)
            {
                ProcessLine(rx_buf);
            }
            rx_index = 0;
            memset(rx_buf, 0, sizeof(rx_buf));
        }
        else if (ch == '\r')
        {
            /* ignore */
        }
        else
        {
            if (rx_index < UART_RX_BUF_SIZE - 1)
            {
                rx_buf[rx_index++] = (char)ch;
            }
            else
            {
                rx_index = 0;
                memset(rx_buf, 0, sizeof(rx_buf));
                USART3_SendString("E\n");
            }
        }
    }
}
