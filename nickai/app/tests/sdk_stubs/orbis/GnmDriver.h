#pragma once
#include <stdint.h>
#ifdef __cplusplus
extern "C" {
#endif
int sceGnmDrawInitDefaultHardwareState350(uint32_t*,uint32_t);
int sceGnmSubmitCommandBuffers(uint32_t,void**,uint32_t*,void**,uint32_t*);
int sceGnmSubmitDone(void);
#ifdef __cplusplus
}
#endif
