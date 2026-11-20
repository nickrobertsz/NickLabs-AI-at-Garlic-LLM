// SPDX-License-Identifier: GPL-3.0-or-later
// Configuration for the NickLabs native app (separate identity: /data/nicklabs-native). Pure JSON, validated, persisted atomically-ish.
#pragma once
#include "json.hpp"
#include <cstdio>
#include <cstring>
#include <fcntl.h>
#include <string>
#include <unistd.h>
#include <vector>
#include <arpa/inet.h>
using Json = nlohmann::json;
struct NativeConfig {
    int port = 11434;
    std::string api_key;                                   // optional; required from peers outside trusted_cidrs
    std::vector<std::string> trusted_cidrs = {"10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8"};
    std::string default_model;                             // "" = first discovered
    uint32_t default_num_ctx = 0;                          // 0 = engine ceiling of the resident model
    int default_max_tokens = 2048;
    double default_temperature = 0.7;
    bool default_think = false;
    int think_budget_low = 256, think_budget_medium = 1024, think_budget_high = 4096;
    int max_queue = 8;                                     // inference requests allowed to wait for the GPU (more => immediate 503)
    int queue_wait_s = 600;                                // how long a request waits for the GPU before 503
    std::vector<std::string> model_dirs = {"/data/nicklabs-native/models", "/data/nicklabs-worker/9b"};
    bool verify_full_hash = false;                         // SHA-256 the weights on every load (slow: minutes on a PS4)
    int margin_mb = 128;                                   // direct memory left to the system by engine_init
    bool autoload = true;                                  // load the desired/default model at startup
    bool auto_recover = true;                              // relaunch the app (max 3x) after a GPU fence
    bool allow_release_restart = false;                    // keep_alive:0 / unload(release) may relaunch the app to free the GPU
    std::string data_dir = "/data/nicklabs-native";
    std::string log_file = "/data/nicklabs-native/native.log";
    Json to_json() const {
        return {{"port", port}, {"api_key_set", !api_key.empty()}, {"trusted_cidrs", trusted_cidrs}, {"default_model", default_model},
                {"default_num_ctx", default_num_ctx}, {"default_max_tokens", default_max_tokens}, {"default_temperature", default_temperature},
                {"default_think", default_think}, {"think_budget_low", think_budget_low}, {"think_budget_medium", think_budget_medium},
                {"think_budget_high", think_budget_high}, {"queue_wait_s", queue_wait_s}, {"max_queue", max_queue}, {"model_dirs", model_dirs},
                {"verify_full_hash", verify_full_hash}, {"margin_mb", margin_mb}, {"allow_release_restart", allow_release_restart}, {"autoload", autoload}, {"auto_recover", auto_recover},
                {"data_dir", data_dir}, {"log_file", log_file}};
    }
    // Applies known keys from j; returns "" or an error message. `restart_keys` collects keys that only take effect after relaunch.
    std::string apply(const Json& j, std::vector<std::string>* restart_keys = nullptr) {
        if (!j.is_object()) return "config must be an object";
        NativeConfig n = *this;
        auto bad = [](const std::string& k) { return "invalid value for " + k; };
        for (auto it = j.begin(); it != j.end(); ++it) {
            const std::string& k = it.key(); const Json& v = it.value();
            auto geti = [&](int lo, int hi, int& out) { if (!v.is_number_integer() || v.get<long long>() < lo || v.get<long long>() > hi) return false; out = v.get<int>(); return true; };
            auto getb = [&](bool& out) { if (!v.is_boolean()) return false; out = v; return true; };
            if (k == "port") { if (!geti(1, 65535, n.port)) return bad(k); if (restart_keys) restart_keys->push_back(k); }
            else if (k == "api_key") { if (!v.is_string() || v.get<std::string>().size() > 256) return bad(k); n.api_key = v; }
            else if (k == "trusted_cidrs") { if (!v.is_array()) return bad(k); n.trusted_cidrs.clear(); for (auto& e : v) { if (!e.is_string() || !cidr_ok(e)) return bad(k); n.trusted_cidrs.push_back(e); } }
            else if (k == "default_model") { if (!v.is_string()) return bad(k); n.default_model = v; }
            else if (k == "default_num_ctx") { if (!v.is_number_integer() || v.get<long long>() < 0 || v.get<long long>() > 1048576) return bad(k); n.default_num_ctx = v.get<uint32_t>(); }
            else if (k == "default_max_tokens") { if (!geti(1, 1048576, n.default_max_tokens)) return bad(k); }
            else if (k == "default_temperature") { if (!v.is_number() || v < 0 || v > 2) return bad(k); n.default_temperature = v; }
            else if (k == "default_think") { if (!getb(n.default_think)) return bad(k); }
            else if (k == "think_budget_low") { if (!geti(16, 65536, n.think_budget_low)) return bad(k); }
            else if (k == "think_budget_medium") { if (!geti(16, 65536, n.think_budget_medium)) return bad(k); }
            else if (k == "think_budget_high") { if (!geti(16, 65536, n.think_budget_high)) return bad(k); }
            else if (k == "max_queue") { if (!geti(0, 256, n.max_queue)) return bad(k); }
            else if (k == "queue_wait_s") { if (!geti(0, 86400, n.queue_wait_s)) return bad(k); }
            else if (k == "model_dirs") { if (!v.is_array()) return bad(k); n.model_dirs.clear(); for (auto& e : v) { if (!e.is_string() || e.get<std::string>().empty() || e.get<std::string>()[0] != '/' || e.get<std::string>().find("..") != std::string::npos) return bad(k); n.model_dirs.push_back(e); } }
            else if (k == "verify_full_hash") { if (!getb(n.verify_full_hash)) return bad(k); }
            else if (k == "margin_mb") { if (!geti(16, 2048, n.margin_mb)) return bad(k); if (restart_keys) restart_keys->push_back(k); }
            else if (k == "autoload") { if (!getb(n.autoload)) return bad(k); if (restart_keys) restart_keys->push_back(k); }
            else if (k == "auto_recover") { if (!getb(n.auto_recover)) return bad(k); }
            else if (k == "allow_release_restart") { if (!getb(n.allow_release_restart)) return bad(k); }
            else if (k == "data_dir" || k == "log_file" || k == "api_key_set") { if (k != "api_key_set" && v != Json(k == "data_dir" ? n.data_dir : n.log_file)) return k + " is fixed"; }
            else return "unknown key " + k;
        }
        *this = n;
        return "";
    }
    // Strict, platform-independent IPv4 parsing for the trust boundary: exactly four decimal octets 0..255, no leading zeros (except "0"), no signs,
    // whitespace or hex. (inet_pton/atoi differ between Linux and the console's libc, and an empty or garbled prefix must never become "/0" = trust everyone.)
    static bool parse_ip4(const std::string& s, uint32_t& out) {
        uint32_t v = 0; size_t i = 0;
        for (int part = 0; part < 4; part++) {
            size_t start = i; uint32_t o = 0;
            while (i < s.size() && s[i] >= '0' && s[i] <= '9' && i - start < 3) { o = o * 10 + uint32_t(s[i] - '0'); i++; }
            size_t len = i - start;
            if (len == 0 || o > 255 || (len > 1 && s[start] == '0')) return false;
            v = (v << 8) | o;
            if (part < 3) { if (i >= s.size() || s[i] != '.') return false; i++; }
        }
        if (i != s.size()) return false;
        out = v; return true;
    }
    // "a.b.c.d" or "a.b.c.d/N" with N = 0 or 1-2 digits without a leading zero, <= 32.
    static bool parse_cidr(const std::string& c, uint32_t& net, int& bits) {
        size_t sl = c.find('/');
        if (!parse_ip4(c.substr(0, sl), net)) return false;
        if (sl == std::string::npos) { bits = 32; return true; }
        std::string p = c.substr(sl + 1);
        if (p.empty() || p.size() > 2 || (p.size() == 2 && p[0] == '0')) return false;
        int n = 0; for (char ch : p) { if (ch < '0' || ch > '9') return false; n = n * 10 + (ch - '0'); }
        if (n > 32) return false; bits = n; return true;
    }
    static bool cidr_ok(const std::string& c) { uint32_t n; int b; return parse_cidr(c, n, b); }
    static bool in_cidr(const std::string& ip, const std::string& c) {
        uint32_t a, n; int bits;
        if (!parse_ip4(ip, a) || !parse_cidr(c, n, bits)) return false;   // fail closed
        uint32_t mask = bits == 0 ? 0 : (~uint32_t(0) << (32 - bits));
        return (a & mask) == (n & mask);
    }
    bool trusted(const std::string& ip) const { for (auto& c : trusted_cidrs) if (in_cidr(ip, c)) return true; return false; }
    // Persisted form includes the key (file is private to the app data dir).
    Json to_persist() const { Json j = to_json(); j.erase("api_key_set"); j["api_key"] = api_key; return j; }
};
inline bool write_all_fd(int fd, const std::string& data) {
    size_t off = 0;
    while (off < data.size()) { ssize_t w = ::write(fd, data.data() + off, data.size() - off); if (w <= 0) return false; off += size_t(w); }
    return ::fsync(fd) == 0 || true;
}
inline bool write_in_place(const std::string& path, const std::string& data) {
    int fd = ::open(path.c_str(), O_WRONLY | O_CREAT | O_TRUNC, 0600);
    if (fd < 0) return false;
    bool ok = write_all_fd(fd, data);
    ::close(fd);
    return ok;
}
// Host: write-then-rename. Console: the app has no proven rename/unlink in its data directory, so write the small file in place.
inline bool write_file_atomic(const std::string& path, const std::string& data) {
#ifndef __ORBIS__
    std::string tmp = path + ".tmp";
    int fd = ::open(tmp.c_str(), O_WRONLY | O_CREAT | O_TRUNC, 0600);
    if (fd >= 0) {
        bool ok = write_all_fd(fd, data);
        ::close(fd);
        if (ok && ::rename(tmp.c_str(), path.c_str()) == 0) return true;
        ::unlink(tmp.c_str());
    }
#endif
    return write_in_place(path, data);
}
inline bool read_file_small(const std::string& path, std::string& out, size_t limit = 1 << 20) {
    int fd = ::open(path.c_str(), O_RDONLY);
    if (fd < 0) return false;
    char buf[4096]; out.clear();
    for (;;) { ssize_t n = ::read(fd, buf, sizeof buf); if (n < 0) { ::close(fd); return false; } if (n == 0) break; out.append(buf, size_t(n)); if (out.size() > limit) { ::close(fd); return false; } }
    ::close(fd);
    return true;
}
