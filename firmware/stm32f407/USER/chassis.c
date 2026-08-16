#include "chassis.h"
#include "bsp_motor.h"
#include "app_config.h"
#include "delay.h"

static volatile uint16_t g_no_cmd_ticks = 0;

static int16_t SpeedPercentToPWM(int speed_percent)
{
    if (speed_percent < 0) speed_percent = 0;
    if (speed_percent > JETSON_SPEED_MAX) speed_percent = JETSON_SPEED_MAX;
    return (int16_t)((speed_percent * MOTOR_PWM_MAX) / JETSON_SPEED_MAX);
}

void Chassis_Init(void)
{
		delay_init(168);
    Motor_Init();
    Chassis_Stop();
    g_no_cmd_ticks = 0;
}

void Chassis_Stop(void)
{
    Motor_StopAll();
}

void Chassis_ApplyCommand(const MotionCommand_t *cmd)
{
    int16_t pwm;

    if (cmd == 0)
    {
        Chassis_Stop();
        return;
    }

    pwm = SpeedPercentToPWM(cmd->speed);

    switch (cmd->direction)
    {
        case 'F':
            Motor_SetSpeed(MOTOR_LF,  pwm);
            Motor_SetSpeed(MOTOR_RF,  pwm);
            Motor_SetSpeed(MOTOR_LB,  pwm);
            Motor_SetSpeed(MOTOR_RB,  pwm);
            break;

        case 'B':
            Motor_SetSpeed(MOTOR_LF, -pwm);
            Motor_SetSpeed(MOTOR_RF, -pwm);
            Motor_SetSpeed(MOTOR_LB, -pwm);
            Motor_SetSpeed(MOTOR_RB, -pwm);
            break;

        case 'L':
            /* X-layout mecanum: rotate left in place. */
            Motor_SetSpeed(MOTOR_LF, -pwm);
            Motor_SetSpeed(MOTOR_LB, -pwm);
            Motor_SetSpeed(MOTOR_RF,  pwm);
            Motor_SetSpeed(MOTOR_RB,  pwm);
            break;

        case 'R':
            /* X-layout mecanum: rotate right in place. */
            Motor_SetSpeed(MOTOR_LF,  pwm);
            Motor_SetSpeed(MOTOR_LB,  pwm);
            Motor_SetSpeed(MOTOR_RF, -pwm);
            Motor_SetSpeed(MOTOR_RB, -pwm);
            break;

        case 'S':
        default:
            Chassis_Stop();
            break;
    }

    g_no_cmd_ticks = 0;
}

void Chassis_Task10ms(void)
{
#if (CHASSIS_CMD_TIMEOUT_MS > 0)
    const uint16_t timeout_ticks = (uint16_t)(CHASSIS_CMD_TIMEOUT_MS / 10);

    if (g_no_cmd_ticks < timeout_ticks)
    {
        g_no_cmd_ticks++;
    }
    else
    {
        Chassis_Stop();
    }
#endif
}
