// SPDX-License-Identifier: GPL-3.0-or-later
// NickLabs AI on Garlic LLM (title NLNA00001): PS4-hosted Ollama/OpenAI compatible server on the Garlic GPU engine.
#include "boot_log.hpp"
#include "garlic_backend.hpp"
#include "napi.hpp"
#include <chrono>
#include <csignal>
#include <thread>
#ifdef __ORBIS__
extern "C" {
#include <orbis/Net.h>
#include <orbis/NetCtl.h>
#include <orbis/Sysmodule.h>
#include <orbis/SystemService.h>
}
#endif
#ifdef __ORBIS__
__attribute__((constructor(101))) static void early_entry() { sceKernelDebugOutText(0, "NLN01 crt_constructor nicklabs-native 00.06\n"); }
#endif
// Host builds (tests only) may relocate the data directory and tune the watchdog through the environment; the console build has none of this.
static std::string data_dir_path() {
#ifndef __ORBIS__
    if (const char* e = getenv("NATIVE_DATA_DIR")) return e;
#endif
    return "/data/nicklabs-native";
}
static const std::string kDataDir = data_dir_path();
static int env_ms(const char* name, int dflt) {
#ifndef __ORBIS__
    if (const char* e = getenv(name)) return atoi(e);
#else
    (void)name;
#endif
    return dflt;
}
static bool platform_init(std::string& err) {
#ifdef __ORBIS__
    struct Step { const char* name; int rc; };
    auto chk = [&](const char* name, int rc) { boot::record(name, rc); if (rc) { err = std::string(name) + " rc=" + std::to_string(rc); return false; } return true; };
    boot::enter("net_init");
    if (!chk("net_module", sceSysmoduleLoadModuleInternal(ORBIS_SYSMODULE_INTERNAL_NET))) return false;
    if (!chk("netctl_module", sceSysmoduleLoadModuleInternal(ORBIS_SYSMODULE_INTERNAL_NETCTL))) return false;
    if (!chk("net_init", sceNetInit())) return false;
    if (!chk("netctl_init", sceNetCtlInit())) return false;
    OrbisNetCtlInfo info{}; bool ready = false;
    for (int i = 0; i < 90 && !ready; i++) {
        if (sceNetCtlGetInfo(ORBIS_NET_CTL_INFO_IP_ADDRESS, &info) == 0 && info.ip_address[0] && std::string(info.ip_address) != "0.0.0.0") { ready = true; break; }
        std::this_thread::sleep_for(std::chrono::seconds(1));
    }
    if (!ready) { err = "network_not_ready"; return false; }
    boot::line(std::string("ip_address=") + info.ip_address);
    sceSystemServiceHideSplashScreen();
#else
    (void)err;
#endif
    return true;
}
static bool relaunch_app(std::string& why) {
#ifdef __ORBIS__
    const char* args[] = {nullptr};
    boot::line("relaunch requested: sceSystemServiceLoadExec(/app0/eboot.bin)");
    int rc = sceSystemServiceLoadExec("/app0/eboot.bin", args);
    why = "sceSystemServiceLoadExec rc=" + std::to_string(rc);
    return false;   // success never returns
#else
    if (FILE* f = fopen((kDataDir + "/relaunch.marker").c_str(), "a")) { fputs("relaunch requested\n", f); fclose(f); }   // host tests observe the request here
    why = "relaunch is only available on the console"; return false;
#endif
}
static std::string trusted_path() {
#ifndef __ORBIS__
    if (const char* e = getenv("NATIVE_TRUSTED")) return e;
#endif
    return "/app0/trusted-assets.json";
}
static void load_config(NativeConfig& cfg) {
    for (const char* name : {"config.json", "config.local.json"}) {   // operator-staged (FTP, root-owned) then app-written overrides
        std::string t; if (!read_file_small(kDataDir + "/" + name, t)) continue;
        Json j = Json::parse(t, nullptr, false);
        if (!j.is_object()) { boot::line(std::string("config ignored (not JSON): ") + name); continue; }
        auto e = cfg.apply(j);
        boot::line(e.empty() ? std::string("config loaded: ") + name : std::string("config rejected: ") + name + ": " + e);
    }
}
int main(int, char**) {
    boot::init(); boot::handlers(); boot::install_terminate();
    std::signal(SIGPIPE, SIG_IGN);
    boot::record("build_nicklabs_native_00_06");
    // Hardware lesson: steady_clock did not tick on the console. Prove the app clock against the wall clock on every start (visible in boot.log).
    nhttp::spawn([] { double w0 = nclock::epoch_s(), m0 = nclock::mono_s(); std::this_thread::sleep_for(std::chrono::seconds(3));
                      char b[120]; snprintf(b, sizeof b, "clock_check wall_dt=%.2f app_clock_dt=%.2f (must match)", nclock::epoch_s() - w0, nclock::mono_s() - m0); boot::line(b); }, 1u << 20);
    try {
        NativeConfig cfg; cfg.data_dir = kDataDir; cfg.log_file = kDataDir + "/native.log";
        std::string err;
        if (!platform_init(err)) { boot::line("startup_failed " + err); return boot::finish(1); }
        boot::enter("config"); load_config(cfg);
        { std::string probe = kDataDir + "/.write-probe"; bool ok = write_file_atomic(probe, "ok\n"); boot::line(std::string("data_dir_writable=") + (ok ? "yes" : "no")); }
        Json trusted; { std::string t; if (read_file_small(trusted_path(), t)) trusted = Json::parse(t, nullptr, false); }
        if (trusted.is_discarded() || trusted.is_null()) { boot::line("startup_failed trusted-assets.json missing"); return boot::finish(1); }
        napi::Api* apip = nullptr;
        GarlicDeps deps; deps.cfg = &cfg; deps.trusted = trusted; deps.relaunch = [&](std::string& why) {
            std::thread([] { std::this_thread::sleep_for(std::chrono::milliseconds(1500)); std::string w; relaunch_app(w); boot::line("relaunch failed: " + w); }).detach();
            (void)why; return true;   // the response is sent first; a failed relaunch is reported by the watchdog/journal
        };
        deps.log = [&](const std::string& lvl, const std::string& m) { if (apip) apip->log.add(lvl, m); };
        auto backend = make_garlic_backend(deps);
        napi::Api api(*backend, cfg, kDataDir + "/config.local.json"); apip = &api;
        boot::enter("http_server");
        nhttp::Server* srv = nullptr; std::string serr; int used = 0;
        for (int p : {cfg.port, 11435, 8080, 18080}) {
            srv = new nhttp::Server(p, [&](const nhttp::Request& q, nhttp::Responder& r) { api.handle(q, r); });
            srv->on_event = [&](const std::string& m) { api.log.add("WARN", "http: " + m); };
            if (srv->start(serr)) { used = p; break; }
            boot::line("bind " + std::to_string(p) + " failed: " + serr); delete srv; srv = nullptr;
        }
        if (!srv) { boot::line("startup_failed no listening port"); return boot::finish(1); }
        api.net_stats = [srv] { auto t = srv->stats(); return Json{{"live_connections", t.live}, {"max_connections", t.max_conns}, {"accepted", t.accepted}, {"accept_errors", t.accept_errors}, {"last_accept_errno", t.last_accept_errno}, {"spawn_failures", t.spawn_failures}, {"rejected_too_many", t.rejected}}; };
        boot::line("listening on port " + std::to_string(used));
        api.log.add("INFO", "NickLabs AI on Garlic LLM " NATIVE_BUILD " listening on " + std::to_string(used));
        // Desired model: persisted selection (swap/release) > config default > first discovered.
        nhttp::spawn([&] { try {
            boot::crumb("autoload thread");
            Json st; { std::string t; if (read_file_small(cfg.data_dir + "/state.json", t)) st = Json::parse(t, nullptr, false); }
            if (!st.is_object()) st = Json::object();
            std::string want = st.contains("desired_model") ? st["desired_model"].get<std::string>() : cfg.default_model;
            if (!cfg.autoload || (st.contains("desired_model") && want.empty())) { api.log.add("INFO", "idle: no model loaded at startup"); return; }
            if (want.empty()) { auto ms = backend->discover(); if (ms.empty()) { api.log.add("ERROR", "no model bundles found in model_dirs"); return; } want = ms[0].name; }
            api.log.add("INFO", "autoload " + want);
            if (!api.preload(want)) {   // the persisted choice failed (invalid bundle, memory...): fall back to the last model that worked
                std::string good = st.contains("last_good_model") && st["last_good_model"].is_string() ? st["last_good_model"].get<std::string>() : "";
                if (!good.empty() && good != want) { api.log.add("WARN", "falling back to last known-good model " + good); api.preload(good); }
            }
        } catch (const std::exception& e) { boot::line(std::string("autoload thread exception: ") + e.what()); } catch (...) { boot::line("autoload thread: unknown exception"); } }, 16u << 20);   // explicit large stack (the Worker hashed on the main thread)
        // Watchdog: a fenced GPU (uncertain completion) is only recoverable by relaunching the process.
        for (;;) {
            std::this_thread::sleep_for(std::chrono::milliseconds(env_ms("NATIVE_WATCHDOG_MS", 5000)));
            Json t = backend->telemetry();
            if (t.value("gpu_fenced", false) && !t.value("restarting", false) && cfg.auto_recover) {
                Json st; { std::string x; if (read_file_small(cfg.data_dir + "/state.json", x)) st = Json::parse(x, nullptr, false); }
                if (!st.is_object()) st = Json::object();
                int n = st.value("restarts_since_ok", 0);
                if (n >= 3) { static bool said = false; if (!said) { said = true; api.log.add("ERROR", "GPU fenced and 3 automatic relaunches already failed: manual action required"); } continue; }
                st["restarts_since_ok"] = n + 1; write_file_atomic(cfg.data_dir + "/state.json", st.dump(2));
                api.log.add("ERROR", "GPU fenced (" + t.value("fence_reason", std::string("?")) + "): automatic relaunch " + std::to_string(n + 1) + "/3");
                std::string w; relaunch_app(w); api.log.add("ERROR", "automatic relaunch failed: " + w);
                std::this_thread::sleep_for(std::chrono::milliseconds(env_ms("NATIVE_RELAUNCH_BACKOFF_MS", 60000)));
            }
        }
    } catch (const std::exception& e) { boot::line(std::string("startup_exception ") + e.what()); }
    catch (...) { boot::line("startup_exception unknown"); }
    return boot::finish(1);
}
