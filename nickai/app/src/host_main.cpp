// SPDX-License-Identifier: GPL-3.0-or-later
// Host fixture server: ./native-host <port> [config.json]
#include "fixture_backend.hpp"
#include "napi.hpp"
#include <csignal>
static volatile sig_atomic_t quit = 0;
int main(int argc, char** argv) {
    signal(SIGINT, [](int) { quit = 1; }); signal(SIGTERM, [](int) { quit = 1; }); signal(SIGPIPE, SIG_IGN);
    NativeConfig cfg; cfg.port = argc > 1 ? atoi(argv[1]) : 0; cfg.log_file = ""; cfg.trusted_cidrs = {"127.0.0.0/8"};
    std::string cpath = argc > 2 ? argv[2] : "";
    if (!cpath.empty()) { std::string t; if (read_file_small(cpath, t)) { auto e = cfg.apply(Json::parse(t)); if (!e.empty()) { fprintf(stderr, "config: %s\n", e.c_str()); return 2; } } }
    cfg.log_file = "";
    FixtureBackend be; napi::Api api(be, cfg, cpath);
    nhttp::Server srv(cfg.port, [&](const nhttp::Request& q, nhttp::Responder& r) { api.handle(q, r); });
    srv.on_event = [&](const std::string& m) { api.log.add("WARN", "http: " + m); };
    api.net_stats = [&] { auto t = srv.stats(); return Json{{"live_connections", t.live}, {"max_connections", t.max_conns}, {"accepted", t.accepted}, {"accept_errors", t.accept_errors}, {"last_accept_errno", t.last_accept_errno}, {"spawn_failures", t.spawn_failures}, {"rejected_too_many", t.rejected}}; };
    std::string err; if (!srv.start(err)) { fprintf(stderr, "%s\n", err.c_str()); return 1; }
    printf("PORT %d\n", srv.port()); fflush(stdout);
    while (!quit) std::this_thread::sleep_for(std::chrono::milliseconds(100));
    srv.stop();
}
