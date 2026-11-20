#pragma once
// One monotonic clock for the whole app (deadlines, queue waits, load/decode timing).
// HARDWARE FINDING (FAT, 2026-11-18): std::chrono::steady_clock does NOT advance with wall time on the console: /native/status uptime_s stayed 0.0 over 10 s of wall time and a 64 s model
// load was reported as 0.55 s. Everything timed with it (request deadline, queue wait, tokens/s) was wrong. On the console use the kernel's process time (microseconds since launch).
#include <chrono>
#ifdef __ORBIS__
#include <orbis/libkernel.h>
#endif
namespace nclock {
inline double mono_s() {
#ifdef __ORBIS__
    return double(sceKernelGetProcessTime()) / 1e6;
#else
    return std::chrono::duration<double>(std::chrono::steady_clock::now().time_since_epoch()).count();
#endif
}
// Wall-clock seconds since the Unix epoch (fractional). system_clock is correct on the console (native.log timestamps are right).
inline double epoch_s() { return std::chrono::duration<double>(std::chrono::system_clock::now().time_since_epoch()).count(); }
}
