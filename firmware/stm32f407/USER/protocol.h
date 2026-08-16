#ifndef __PROTOCOL_H
#define __PROTOCOL_H

#include "stm32f4xx.h"
#include "chassis.h"

uint8_t ParseMotionCommand(const char *line, MotionCommand_t *cmd);
void Protocol_PollReceive(void);

#endif
