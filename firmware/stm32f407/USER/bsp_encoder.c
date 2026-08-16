#include "bsp_encoder.h"
#include "app_config.h"

static volatile EncoderData_t g_encoder[4];
static uint16_t g_last16_lf = 0;
static uint16_t g_last16_rf = 0;
static uint16_t g_last16_rb = 0;
static uint32_t g_last32_lb = 0;

static void Encoder_TIM4_LF_Init(void);
static void Encoder_TIM3_RF_Init(void);
static void Encoder_TIM2_LB_Init(void);
static void Encoder_TIM1_RB_Init(void);
static void Encoder_UpdateOne16(EncoderID_t id, TIM_TypeDef *tim, uint16_t *last, int8_t sign);
static void Encoder_UpdateOne32(EncoderID_t id, TIM_TypeDef *tim, uint32_t *last, int8_t sign);

void Encoder_Init(void)
{
    uint8_t i;

    for (i = 0; i < 4; i++)
    {
        g_encoder[i].total_count = 0;
        g_encoder[i].delta_count = 0;
        g_encoder[i].rpm = 0.0f;
        g_encoder[i].speed_mps = 0.0f;
    }

    Encoder_TIM4_LF_Init();
    Encoder_TIM3_RF_Init();
    Encoder_TIM2_LB_Init();
    Encoder_TIM1_RB_Init();

    g_last16_lf = (uint16_t)TIM_GetCounter(TIM4);
    g_last16_rf = (uint16_t)TIM_GetCounter(TIM3);
    g_last32_lb = (uint32_t)TIM_GetCounter(TIM2);
    g_last16_rb = (uint16_t)TIM_GetCounter(TIM1);
}

static void Encoder_CommonICFilter(TIM_TypeDef *TIMx)
{
    TIM_ICInitTypeDef TIM_ICInitStructure;
    TIM_ICStructInit(&TIM_ICInitStructure);
    TIM_ICInitStructure.TIM_Channel = TIM_Channel_1;
    TIM_ICInitStructure.TIM_ICFilter = 6;
    TIM_ICInit(TIMx, &TIM_ICInitStructure);
    TIM_ICInitStructure.TIM_Channel = TIM_Channel_2;
    TIM_ICInitStructure.TIM_ICFilter = 6;
    TIM_ICInit(TIMx, &TIM_ICInitStructure);
}

static void Encoder_TIM4_LF_Init(void)
{
    GPIO_InitTypeDef GPIO_InitStructure;
    TIM_TimeBaseInitTypeDef TIM_TimeBaseStructure;

    RCC_AHB1PeriphClockCmd(RCC_AHB1Periph_GPIOB, ENABLE);
    RCC_APB1PeriphClockCmd(RCC_APB1Periph_TIM4, ENABLE);

    GPIO_PinAFConfig(GPIOB, GPIO_PinSource6, GPIO_AF_TIM4);
    GPIO_PinAFConfig(GPIOB, GPIO_PinSource7, GPIO_AF_TIM4);

    GPIO_InitStructure.GPIO_Pin = GPIO_Pin_6 | GPIO_Pin_7;
    GPIO_InitStructure.GPIO_Mode = GPIO_Mode_AF;
    GPIO_InitStructure.GPIO_OType = GPIO_OType_PP;
    GPIO_InitStructure.GPIO_Speed = GPIO_Speed_100MHz;
    GPIO_InitStructure.GPIO_PuPd = GPIO_PuPd_UP;
    GPIO_Init(GPIOB, &GPIO_InitStructure);

    TIM_TimeBaseStructure.TIM_Period = 0xFFFF;
    TIM_TimeBaseStructure.TIM_Prescaler = 0;
    TIM_TimeBaseStructure.TIM_ClockDivision = TIM_CKD_DIV1;
    TIM_TimeBaseStructure.TIM_CounterMode = TIM_CounterMode_Up;
    TIM_TimeBaseInit(TIM4, &TIM_TimeBaseStructure);

    TIM_EncoderInterfaceConfig(TIM4, TIM_EncoderMode_TI12, TIM_ICPolarity_Rising, TIM_ICPolarity_Rising);
    Encoder_CommonICFilter(TIM4);
    TIM_SetCounter(TIM4, 0);
    TIM_Cmd(TIM4, ENABLE);
}

static void Encoder_TIM3_RF_Init(void)
{
    GPIO_InitTypeDef GPIO_InitStructure;
    TIM_TimeBaseInitTypeDef TIM_TimeBaseStructure;

    RCC_AHB1PeriphClockCmd(RCC_AHB1Periph_GPIOA, ENABLE);
    RCC_APB1PeriphClockCmd(RCC_APB1Periph_TIM3, ENABLE);

    GPIO_PinAFConfig(GPIOA, GPIO_PinSource6, GPIO_AF_TIM3);
    GPIO_PinAFConfig(GPIOA, GPIO_PinSource7, GPIO_AF_TIM3);

    GPIO_InitStructure.GPIO_Pin = GPIO_Pin_6 | GPIO_Pin_7;
    GPIO_InitStructure.GPIO_Mode = GPIO_Mode_AF;
    GPIO_InitStructure.GPIO_OType = GPIO_OType_PP;
    GPIO_InitStructure.GPIO_Speed = GPIO_Speed_100MHz;
    GPIO_InitStructure.GPIO_PuPd = GPIO_PuPd_UP;
    GPIO_Init(GPIOA, &GPIO_InitStructure);

    TIM_TimeBaseStructure.TIM_Period = 0xFFFF;
    TIM_TimeBaseStructure.TIM_Prescaler = 0;
    TIM_TimeBaseStructure.TIM_ClockDivision = TIM_CKD_DIV1;
    TIM_TimeBaseStructure.TIM_CounterMode = TIM_CounterMode_Up;
    TIM_TimeBaseInit(TIM3, &TIM_TimeBaseStructure);

    TIM_EncoderInterfaceConfig(TIM3, TIM_EncoderMode_TI12, TIM_ICPolarity_Rising, TIM_ICPolarity_Rising);
    Encoder_CommonICFilter(TIM3);
    TIM_SetCounter(TIM3, 0);
    TIM_Cmd(TIM3, ENABLE);
}

static void Encoder_TIM2_LB_Init(void)
{
    GPIO_InitTypeDef GPIO_InitStructure;
    TIM_TimeBaseInitTypeDef TIM_TimeBaseStructure;

    RCC_AHB1PeriphClockCmd(RCC_AHB1Periph_GPIOA, ENABLE);
    RCC_APB1PeriphClockCmd(RCC_APB1Periph_TIM2, ENABLE);

    GPIO_PinAFConfig(GPIOA, GPIO_PinSource5, GPIO_AF_TIM2);
    GPIO_PinAFConfig(GPIOA, GPIO_PinSource1, GPIO_AF_TIM2);

    GPIO_InitStructure.GPIO_Pin = GPIO_Pin_5 | GPIO_Pin_1;
    GPIO_InitStructure.GPIO_Mode = GPIO_Mode_AF;
    GPIO_InitStructure.GPIO_OType = GPIO_OType_PP;
    GPIO_InitStructure.GPIO_Speed = GPIO_Speed_100MHz;
    GPIO_InitStructure.GPIO_PuPd = GPIO_PuPd_UP;
    GPIO_Init(GPIOA, &GPIO_InitStructure);

    TIM_TimeBaseStructure.TIM_Period = 0xFFFFFFFF;
    TIM_TimeBaseStructure.TIM_Prescaler = 0;
    TIM_TimeBaseStructure.TIM_ClockDivision = TIM_CKD_DIV1;
    TIM_TimeBaseStructure.TIM_CounterMode = TIM_CounterMode_Up;
    TIM_TimeBaseInit(TIM2, &TIM_TimeBaseStructure);

    TIM_EncoderInterfaceConfig(TIM2, TIM_EncoderMode_TI12, TIM_ICPolarity_Rising, TIM_ICPolarity_Rising);
    Encoder_CommonICFilter(TIM2);
    TIM_SetCounter(TIM2, 0);
    TIM_Cmd(TIM2, ENABLE);
}

static void Encoder_TIM1_RB_Init(void)
{
    GPIO_InitTypeDef GPIO_InitStructure;
    TIM_TimeBaseInitTypeDef TIM_TimeBaseStructure;

    RCC_AHB1PeriphClockCmd(RCC_AHB1Periph_GPIOE, ENABLE);
    RCC_APB2PeriphClockCmd(RCC_APB2Periph_TIM1, ENABLE);

    GPIO_PinAFConfig(GPIOE, GPIO_PinSource9, GPIO_AF_TIM1);
    GPIO_PinAFConfig(GPIOE, GPIO_PinSource11, GPIO_AF_TIM1);

    GPIO_InitStructure.GPIO_Pin = GPIO_Pin_9 | GPIO_Pin_11;
    GPIO_InitStructure.GPIO_Mode = GPIO_Mode_AF;
    GPIO_InitStructure.GPIO_OType = GPIO_OType_PP;
    GPIO_InitStructure.GPIO_Speed = GPIO_Speed_100MHz;
    GPIO_InitStructure.GPIO_PuPd = GPIO_PuPd_UP;
    GPIO_Init(GPIOE, &GPIO_InitStructure);

    TIM_TimeBaseStructure.TIM_Period = 0xFFFF;
    TIM_TimeBaseStructure.TIM_Prescaler = 0;
    TIM_TimeBaseStructure.TIM_ClockDivision = TIM_CKD_DIV1;
    TIM_TimeBaseStructure.TIM_CounterMode = TIM_CounterMode_Up;
    TIM_TimeBaseInit(TIM1, &TIM_TimeBaseStructure);

    TIM_EncoderInterfaceConfig(TIM1, TIM_EncoderMode_TI12, TIM_ICPolarity_Rising, TIM_ICPolarity_Rising);
    Encoder_CommonICFilter(TIM1);
    TIM_SetCounter(TIM1, 0);
    TIM_Cmd(TIM1, ENABLE);
}

static void Encoder_UpdateOne16(EncoderID_t id, TIM_TypeDef *tim, uint16_t *last, int8_t sign)
{
    uint16_t now = (uint16_t)TIM_GetCounter(tim);
    int16_t raw_delta = (int16_t)(now - *last);
    int32_t delta = (int32_t)raw_delta * (int32_t)sign;

    *last = now;
    g_encoder[id].delta_count = delta;
    g_encoder[id].total_count += delta;
    g_encoder[id].rpm = ((float)delta / ENCODER_SAMPLE_DT_S) / ENCODER_COUNTS_PER_WHEEL_REV * 60.0f;
    g_encoder[id].speed_mps = ((float)delta / ENCODER_SAMPLE_DT_S) / ENCODER_COUNTS_PER_WHEEL_REV * WHEEL_CIRCUMFERENCE_M;
}

static void Encoder_UpdateOne32(EncoderID_t id, TIM_TypeDef *tim, uint32_t *last, int8_t sign)
{
    uint32_t now = (uint32_t)TIM_GetCounter(tim);
    int32_t raw_delta = (int32_t)(now - *last);
    int32_t delta = raw_delta * (int32_t)sign;

    *last = now;
    g_encoder[id].delta_count = delta;
    g_encoder[id].total_count += delta;
    g_encoder[id].rpm = ((float)delta / ENCODER_SAMPLE_DT_S) / ENCODER_COUNTS_PER_WHEEL_REV * 60.0f;
    g_encoder[id].speed_mps = ((float)delta / ENCODER_SAMPLE_DT_S) / ENCODER_COUNTS_PER_WHEEL_REV * WHEEL_CIRCUMFERENCE_M;
}

void Encoder_Update10ms(void)
{
    Encoder_UpdateOne16(ENCODER_LF, TIM4, &g_last16_lf, ENCODER_LF_DIR_SIGN);
    Encoder_UpdateOne16(ENCODER_RF, TIM3, &g_last16_rf, ENCODER_RF_DIR_SIGN);
    Encoder_UpdateOne32(ENCODER_LB, TIM2, &g_last32_lb, ENCODER_LB_DIR_SIGN);
    Encoder_UpdateOne16(ENCODER_RB, TIM1, &g_last16_rb, ENCODER_RB_DIR_SIGN);
}

int32_t Encoder_GetTotalCount(EncoderID_t id) { return g_encoder[id].total_count; }
int32_t Encoder_GetDeltaCount(EncoderID_t id) { return g_encoder[id].delta_count; }
float Encoder_GetRPM(EncoderID_t id)           { return g_encoder[id].rpm; }
float Encoder_GetSpeedMPS(EncoderID_t id)      { return g_encoder[id].speed_mps; }
