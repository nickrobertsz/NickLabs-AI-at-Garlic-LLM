// SPDX-License-Identifier: GPL-3.0-or-later
// Console journal for the NickLabs native app (prefix NLN01). Same proven constraints as the C07 worker: no chdir/getcwd,
// no path stat (fstat on descriptors only), fsync after every record, never return from main on the console.
#pragma once
#include <atomic>
#include <cerrno>
#include <csignal>
#include <cstdio>
#include <cstring>
#include <fcntl.h>
#include <cstdlib>
#include <exception>
#include <string>
#include <sys/stat.h>
#include <unistd.h>
#ifdef __ORBIS__
extern "C" {
#include <orbis/libkernel.h>
#include <orbis/SystemService.h>
}
#endif
namespace boot {
inline int fd = -1;
// Breadcrumb: the last thing the app was doing (updated at coarse points; benign race between threads). Printed by the fatal-signal and terminate handlers.
inline char crumb_buf[160] = "start";
inline std::atomic_flag crumb_lock = ATOMIC_FLAG_INIT;   // writers serialize; the crash handlers read lock-free (they run when the process is already dying)
inline void crumb(const char* s) {
    while (crumb_lock.test_and_set(std::memory_order_acquire)) {}
    size_t i = 0; for (; s[i] && i < sizeof crumb_buf - 1; i++) crumb_buf[i] = s[i]; crumb_buf[i] = 0;
    crumb_lock.clear(std::memory_order_release);
}
inline const char* stage = "entry";
inline void raw(const char* s, size_t n) {
#ifdef __ORBIS__
    sceKernelDebugOutText(0, "%.*s", int(n), s);   // never use diagnostic text as a format
#else
    (void)!::write(2, s, n);
#endif
    if (fd >= 0) {
        struct stat st{};
        if (fstat(fd, &st) == 0 && st.st_size + static_cast<off_t>(n) > 262144) { (void)!ftruncate(fd, 0); lseek(fd, 0, SEEK_SET); }
        size_t off = 0;
        while (off < n) { auto k = write(fd, s + off, n - off); if (k <= 0) break; off += size_t(k); }
        fsync(fd);
    }
}
inline void line(const std::string& s) { std::string t = "NLN01 " + s + "\n"; raw(t.data(), t.size()); }
inline void record(const char* event, int rc = 0, int error = 0) {
    char b[384]; int n = snprintf(b, sizeof b, "NLN01 stage=%s event=%s rc=%d errno=%d\n", stage, event, rc, error);
    if (n > 0) raw(b, size_t(n) < sizeof b ? size_t(n) : sizeof b - 1);
}
inline void enter(const char* s) { stage = s; record("begin"); }
inline void init() {
#ifdef __ORBIS__
    record("main_entered");
    for (const char* p : {"/data/nicklabs-native/boot.log", "/data/nicklabs-native-boot.log", "/user/data/nicklabs-native-boot.log", "/mnt/usb0/nicklabs-native-boot.log", "/tmp/nicklabs-native-boot.log"}) {
        fd = open(p, O_WRONLY | O_CREAT | O_APPEND | O_NOFOLLOW | O_NONBLOCK, 0666);   // a diagnostic journal without secrets: readable over FTP
        int e = errno; struct stat st{};
        if (fd >= 0 && (fstat(fd, &st) || !S_ISREG(st.st_mode))) { close(fd); fd = -1; e = EINVAL; }
        record("boot_log_open", fd >= 0 ? 0 : -1, fd >= 0 ? 0 : e);
        if (fd >= 0) { line(std::string("boot_log_path=") + p); break; }
    }
    char b[160]; snprintf(b, sizeof b, "uid=%u euid=%u", unsigned(getuid()), unsigned(geteuid())); line(b);
#endif
}
#ifdef __ORBIS__
[[noreturn]]
#endif
inline int finish(int status) {
#ifdef __ORBIS__
    record(status ? "fatal_park_close_app_to_retry" : "stopped_close_app");
    sceSystemServiceHideSplashScreen();
    for (;;) sceKernelUsleep(1000000);   // never let the CRT return invoke the forbidden exit path
#else
    return status;
#endif
}
inline void fatal_signal(int signo) {
    char b[] = "NLN01 fatal_signal=000 inspect_kernel_coredump\n";
    b[19] = char('0' + signo / 100 % 10); b[20] = char('0' + signo / 10 % 10); b[21] = char('0' + signo % 10);
    if (fd >= 0) { (void)!write(fd, b, sizeof b - 1); (void)!write(fd, "NLN01 crumb=", 12); (void)!write(fd, crumb_buf, strlen(crumb_buf)); (void)!write(fd, "\n", 1); fsync(fd); }
    (void)!write(STDERR_FILENO, b, sizeof b - 1);
}
// std::terminate (an exception nobody caught, a joinable thread destroyed...): say which exception, then park this thread (the HTTP server's other
// threads keep answering, which keeps the journal and /native/status reachable for diagnosis) instead of aborting the whole app.
inline void install_terminate() {
    std::set_terminate([] {
        try { if (auto e = std::current_exception()) std::rethrow_exception(e); line("cpp_terminate: no active exception"); }
        catch (const std::exception& x) { line(std::string("cpp_terminate what=") + x.what()); }
        catch (...) { line("cpp_terminate: unknown exception type"); }
        line(std::string("cpp_terminate crumb=") + crumb_buf);
#ifdef __ORBIS__
        for (;;) sceKernelUsleep(1000000);
#else
        std::abort();
#endif
    });
}
inline void handlers() {
#ifdef __ORBIS__
    struct sigaction a{}; a.sa_handler = fatal_signal; sigemptyset(&a.sa_mask); a.sa_flags = SA_RESETHAND;
    for (int s : {SIGSEGV, SIGBUS, SIGILL, SIGSYS, SIGABRT, SIGFPE}) { int rc = sigaction(s, &a, nullptr); record("sigaction", rc, rc ? errno : 0); }
#endif
}
}
