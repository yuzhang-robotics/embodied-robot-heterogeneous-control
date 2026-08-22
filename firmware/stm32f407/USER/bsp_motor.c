#include "bsp_motor.h"
#include "app_config.h"

static void Motor_AllControlPinsAsLowGPIO(void);
static void Motor_DirGPIOInit(void);
static void Motor_PWMGPIOInit(void);
static void Motor_TIM8PWMInit(void);
static void Motor_SetPWM(MotorID_t id, uint16_t pwm);
static void Motor_SetDirPins(GPIO_TypeDef *gpio, uint16_t in1, uint16_t in2, int16_t speed);

void Motor_Init(void)
{
    /* TB6612 STBY is tied high, so force all control pins low before PWM setup. */
    Motor_AllControlPinsAsLowGPIO();
    Motor_DirGPIOInit();
    Motor_PWMGPIOInit();
    Motor_TIM8PWMInit();
    Motor_StopAll();
}

static void Motor_AllControlPinsAsLowGPIO(void)
{
    GPIO_InitTypeDef GPIO_InitStructure;

    RCC_AHB1PeriphClockCmd(RCC_AHB1Periph_GPIOC | RCC_AHB1Periph_GPIOG, ENABLE);

    /* PC0-PC5: direction; PC6-PC9: PWM. */
    GPIO_InitStructure.GPIO_Pin = GPIO_Pin_0 | GPIO_Pin_1 | GPIO_Pin_2 | GPIO_Pin_3 |
                                  GPIO_Pin_4 | GPIO_Pin_5 | GPIO_Pin_6 | GPIO_Pin_7 |
                                  GPIO_Pin_8 | GPIO_Pin_9;
    GPIO_InitStructure.GPIO_Mode = GPIO_Mode_OUT;
    GPIO_InitStructure.GPIO_OType = GPIO_OType_PP;
    GPIO_InitStructure.GPIO_Speed = GPIO_Speed_100MHz;
    GPIO_InitStructure.GPIO_PuPd = GPIO_PuPd_DOWN;
    GPIO_Init(GPIOC, &GPIO_InitStructure);
    GPIO_ResetBits(GPIOC, GPIO_InitStructure.GPIO_Pin);

    /* PG6-PG7: left-front direction. */
    GPIO_InitStructure.GPIO_Pin = GPIO_Pin_6 | GPIO_Pin_7;
    GPIO_Init(GPIOG, &GPIO_InitStructure);
    GPIO_ResetBits(GPIOG, GPIO_InitStructure.GPIO_Pin);
}

static void Motor_DirGPIOInit(void)
{
    GPIO_InitTypeDef GPIO_InitStructure;

    RCC_AHB1PeriphClockCmd(MOTOR_LF_DIR_GPIO_CLK | MOTOR_RFLBRB_DIR_GPIO_CLK, ENABLE);

    GPIO_InitStructure.GPIO_Mode = GPIO_Mode_OUT;
    GPIO_InitStructure.GPIO_OType = GPIO_OType_PP;
    GPIO_InitStructure.GPIO_Speed = GPIO_Speed_100MHz;
    GPIO_InitStructure.GPIO_PuPd = GPIO_PuPd_DOWN;

    GPIO_InitStructure.GPIO_Pin = MOTOR_LF_IN1_PIN | MOTOR_LF_IN2_PIN;
    GPIO_Init(MOTOR_LF_DIR_GPIO, &GPIO_InitStructure);
    GPIO_ResetBits(MOTOR_LF_DIR_GPIO, MOTOR_LF_IN1_PIN | MOTOR_LF_IN2_PIN);

    GPIO_InitStructure.GPIO_Pin = MOTOR_RF_IN1_PIN | MOTOR_RF_IN2_PIN |
                                  MOTOR_LB_IN1_PIN | MOTOR_LB_IN2_PIN |
                                  MOTOR_RB_IN1_PIN | MOTOR_RB_IN2_PIN;
    GPIO_Init(MOTOR_RFLBRB_DIR_GPIO, &GPIO_InitStructure);
    GPIO_ResetBits(MOTOR_RFLBRB_DIR_GPIO, GPIO_InitStructure.GPIO_Pin);
}

static void Motor_PWMGPIOInit(void)
{
    GPIO_InitTypeDef GPIO_InitStructure;

    RCC_AHB1PeriphClockCmd(MOTOR_PWM_GPIO_CLK, ENABLE);

    GPIO_PinAFConfig(MOTOR_PWM_GPIO, MOTOR_LF_PWM_SRC, MOTOR_PWM_AF);
    GPIO_PinAFConfig(MOTOR_PWM_GPIO, MOTOR_RF_PWM_SRC, MOTOR_PWM_AF);
    GPIO_PinAFConfig(MOTOR_PWM_GPIO, MOTOR_LB_PWM_SRC, MOTOR_PWM_AF);
    GPIO_PinAFConfig(MOTOR_PWM_GPIO, MOTOR_RB_PWM_SRC, MOTOR_PWM_AF);

    GPIO_InitStructure.GPIO_Pin = MOTOR_LF_PWM_PIN | MOTOR_RF_PWM_PIN |
                                  MOTOR_LB_PWM_PIN | MOTOR_RB_PWM_PIN;
    GPIO_InitStructure.GPIO_Mode = GPIO_Mode_AF;
    GPIO_InitStructure.GPIO_OType = GPIO_OType_PP;
    GPIO_InitStructure.GPIO_Speed = GPIO_Speed_100MHz;
    GPIO_InitStructure.GPIO_PuPd = GPIO_PuPd_DOWN;
    GPIO_Init(MOTOR_PWM_GPIO, &GPIO_InitStructure);
}

static void Motor_TIM8PWMInit(void)
{
    TIM_TimeBaseInitTypeDef TIM_TimeBaseStructure;
    TIM_OCInitTypeDef TIM_OCInitStructure;

    RCC_APB2PeriphClockCmd(MOTOR_PWM_TIM_CLK, ENABLE);

    TIM_TimeBaseStructure.TIM_Period = MOTOR_PWM_ARR;
    TIM_TimeBaseStructure.TIM_Prescaler = MOTOR_PWM_PSC;
    TIM_TimeBaseStructure.TIM_ClockDivision = TIM_CKD_DIV1;
    TIM_TimeBaseStructure.TIM_CounterMode = TIM_CounterMode_Up;
    TIM_TimeBaseStructure.TIM_RepetitionCounter = 0;
    TIM_TimeBaseInit(MOTOR_PWM_TIM, &TIM_TimeBaseStructure);

    TIM_OCStructInit(&TIM_OCInitStructure);
    TIM_OCInitStructure.TIM_OCMode = TIM_OCMode_PWM1;
    TIM_OCInitStructure.TIM_OutputState = TIM_OutputState_Enable;
    TIM_OCInitStructure.TIM_OutputNState = TIM_OutputNState_Disable;
    TIM_OCInitStructure.TIM_Pulse = 0;
    TIM_OCInitStructure.TIM_OCPolarity = TIM_OCPolarity_High;
    TIM_OCInitStructure.TIM_OCIdleState = TIM_OCIdleState_Reset;

    TIM_OC1Init(MOTOR_PWM_TIM, &TIM_OCInitStructure);
    TIM_OC2Init(MOTOR_PWM_TIM, &TIM_OCInitStructure);
    TIM_OC3Init(MOTOR_PWM_TIM, &TIM_OCInitStructure);
    TIM_OC4Init(MOTOR_PWM_TIM, &TIM_OCInitStructure);

    TIM_OC1PreloadConfig(MOTOR_PWM_TIM, TIM_OCPreload_Enable);
    TIM_OC2PreloadConfig(MOTOR_PWM_TIM, TIM_OCPreload_Enable);
    TIM_OC3PreloadConfig(MOTOR_PWM_TIM, TIM_OCPreload_Enable);
    TIM_OC4PreloadConfig(MOTOR_PWM_TIM, TIM_OCPreload_Enable);
    TIM_ARRPreloadConfig(MOTOR_PWM_TIM, ENABLE);

    TIM_SetCompare1(MOTOR_PWM_TIM, 0);
    TIM_SetCompare2(MOTOR_PWM_TIM, 0);
    TIM_SetCompare3(MOTOR_PWM_TIM, 0);
    TIM_SetCompare4(MOTOR_PWM_TIM, 0);

    /* TIM8 is an advanced timer; main output enable is required. */
    TIM_CtrlPWMOutputs(MOTOR_PWM_TIM, ENABLE);
    TIM_Cmd(MOTOR_PWM_TIM, ENABLE);
}

static void Motor_SetPWM(MotorID_t id, uint16_t pwm)
{
    uint32_t compare;

    if (pwm > MOTOR_PWM_MAX) pwm = MOTOR_PWM_MAX;
    compare = ((uint32_t)pwm * (MOTOR_PWM_ARR + 1)) / MOTOR_PWM_MAX;
    if (compare > MOTOR_PWM_ARR) compare = MOTOR_PWM_ARR;

    switch (id)
    {
        case MOTOR_LF: TIM_SetCompare1(MOTOR_PWM_TIM, compare); break;
        case MOTOR_RF: TIM_SetCompare2(MOTOR_PWM_TIM, compare); break;
        case MOTOR_LB: TIM_SetCompare3(MOTOR_PWM_TIM, compare); break;
        case MOTOR_RB: TIM_SetCompare4(MOTOR_PWM_TIM, compare); break;
        default: break;
    }
}

static void Motor_SetDirPins(GPIO_TypeDef *gpio, uint16_t in1, uint16_t in2, int16_t speed)
{
    if (speed > 0)
    {
        GPIO_SetBits(gpio, in1);
        GPIO_ResetBits(gpio, in2);
    }
    else if (speed < 0)
    {
        GPIO_ResetBits(gpio, in1);
        GPIO_SetBits(gpio, in2);
    }
    else
    {
        GPIO_ResetBits(gpio, in1 | in2);
    }
}

void Motor_SetSpeed(MotorID_t id, int16_t pwm_signed)
{
    uint16_t pwm_abs;

    if (pwm_signed > MOTOR_PWM_MAX) pwm_signed = MOTOR_PWM_MAX;
    if (pwm_signed < -MOTOR_PWM_MAX) pwm_signed = -MOTOR_PWM_MAX;

    switch (id)
    {
        case MOTOR_LF:
            pwm_signed *= MOTOR_LF_DIR_SIGN;
            Motor_SetDirPins(MOTOR_LF_DIR_GPIO, MOTOR_LF_IN1_PIN, MOTOR_LF_IN2_PIN, pwm_signed);
            break;

        case MOTOR_RF:
            pwm_signed *= MOTOR_RF_DIR_SIGN;
            Motor_SetDirPins(MOTOR_RFLBRB_DIR_GPIO, MOTOR_RF_IN1_PIN, MOTOR_RF_IN2_PIN, pwm_signed);
            break;

        case MOTOR_LB:
            pwm_signed *= MOTOR_LB_DIR_SIGN;
            Motor_SetDirPins(MOTOR_RFLBRB_DIR_GPIO, MOTOR_LB_IN1_PIN, MOTOR_LB_IN2_PIN, pwm_signed);
            break;

        case MOTOR_RB:
            pwm_signed *= MOTOR_RB_DIR_SIGN;
            Motor_SetDirPins(MOTOR_RFLBRB_DIR_GPIO, MOTOR_RB_IN1_PIN, MOTOR_RB_IN2_PIN, pwm_signed);
            break;

        default:
            return;
    }

    pwm_abs = (pwm_signed >= 0) ? (uint16_t)pwm_signed : (uint16_t)(-pwm_signed);
    Motor_SetPWM(id, pwm_abs);
}

void Motor_StopAll(void)
{
    Motor_SetSpeed(MOTOR_LF, 0);
    Motor_SetSpeed(MOTOR_RF, 0);
    Motor_SetSpeed(MOTOR_LB, 0);
    Motor_SetSpeed(MOTOR_RB, 0);
}
