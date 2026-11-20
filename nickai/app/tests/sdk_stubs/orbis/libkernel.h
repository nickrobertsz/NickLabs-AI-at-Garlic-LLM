#pragma once
#include <stdint.h>
#include <stddef.h>
#include <sys/types.h>
#ifdef __cplusplus
extern "C" {
#endif
size_t sceKernelGetDirectMemorySize(void);
int sceKernelAllocateDirectMemory(off_t,off_t,size_t,size_t,int,off_t*);
int sceKernelMapDirectMemory(void**,size_t,int,int,off_t,size_t);
int sceKernelReleaseDirectMemory(off_t,size_t);
int sceKernelMunmap(void*,size_t);
uint64_t sceKernelGetTscFrequency(void);
uint64_t sceKernelReadTsc(void);
int sceKernelUsleep(unsigned);
int sceKernelDebugOutText(int,const char*,...);
int sceKernelAvailableFlexibleMemorySize(size_t*);
int sceKernelMapFlexibleMemory(void**,size_t,int,int);
#ifdef __cplusplus
}
#endif
