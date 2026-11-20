// SPDX-License-Identifier: GPL-3.0-or-later
// Ollama / OpenAI compatible HTTP API plus native management endpoints over a NativeBackend.
#pragma once
#include "nbackend.hpp"
#include "nconfig.hpp"
#include "nclock.hpp"
#include "boot_log.hpp"
#include "nauth.hpp"
#include "nhttp.hpp"
#include "ntools.hpp"
#include <atomic>
#include <chrono>
#include <ctime>
#include <algorithm>
#include <cstdint>
#include <deque>
#include <functional>
#include <mutex>
#include <set>
#include <thread>
#define NATIVE_VERSION "0.9.0"
#define NATIVE_BUILD "nicklabs-native-0.1.5"
namespace napi {
inline double mono() { return nclock::mono_s(); }
inline std::string rfc3339() {
    auto now = std::chrono::system_clock::now();
    time_t t = std::chrono::system_clock::to_time_t(now);
    auto ns = std::chrono::duration_cast<std::chrono::nanoseconds>(now.time_since_epoch()).count() % 1000000000;
    tm g{}; gmtime_r(&t, &g);
    char b[48]; strftime(b, sizeof b, "%Y-%m-%dT%H:%M:%S", &g);
    char o[80]; snprintf(o, sizeof o, "%s.%09lldZ", b, (long long)ns);
    return o;
}
inline long long ns(double s) { return (long long)(s * 1e9); }
inline std::string jdump(const Json& j) { return j.dump(-1, ' ', false, Json::error_handler_t::replace); }
struct ApiError : NativeError { using NativeError::NativeError; };
struct EventLog {
    std::mutex mu; std::deque<std::string> ring; std::string file; size_t written = 0;
    void add(const std::string& level, const std::string& msg) {
        // Request-derived text (paths, model names, error details) must not inject control/escape bytes or forge lines in the log.
        std::string clean = msg;
        for (auto& c : clean) { unsigned char u = (unsigned char)c; if (u < 0x20 || u >= 0x7f) c = '?'; }
        if (clean.size() > 600) clean.resize(600);
        std::string line = rfc3339() + " " + level + " " + clean;
        std::lock_guard<std::mutex> l(mu);
        ring.push_back(line); if (ring.size() > 400) ring.pop_front();
        fprintf(stderr, "%s\n", line.c_str());
        if (!file.empty()) {   // capped: restart the file after 1 MiB (the console data dir has no rotation)
            written += line.size() + 1;
            FILE* f = fopen(file.c_str(), written > (1u << 20) ? "w" : "a");
            if (f) { if (written > (1u << 20)) written = line.size() + 1; fputs((line + "\n").c_str(), f); fclose(f); }
        }
    }
    Json tail(size_t n) { std::lock_guard<std::mutex> l(mu); Json a = Json::array(); size_t s = ring.size() > n ? ring.size() - n : 0; for (size_t i = s; i < ring.size(); i++) a.push_back(ring[i]); return a; }
};
// Decides which generated text may be released to the client: applies stop sequences, holds back possible stop/tool-call prefixes.
class TextGate {
    std::vector<std::string> stops; bool tools; std::string buf, released; size_t hold = 0; bool call_seen = false;
public:
    bool stopped = false;
    std::string hit;   // the stop sequence that ended generation
    std::string all;   // everything accepted (before stop sequence), used for tool parsing and non-streaming output
    TextGate(std::vector<std::string> s, bool t) : stops(std::move(s)), tools(t) { for (auto& x : stops) hold = std::max(hold, x.size() > 0 ? x.size() - 1 : 0); }
    std::string push(const std::string& piece) {
        if (stopped) return "";
        buf += piece;
        size_t cut = std::string::npos;
        for (auto& s : stops) { if (s.empty()) continue; size_t p = buf.find(s); if (p != std::string::npos && p < cut) { cut = p; hit = s; } }
        if (cut != std::string::npos) { stopped = true; std::string out = buf.substr(0, cut); all += out; buf.clear(); return call_seen ? "" : out; }
        size_t keep = hold;
        if (tools) {
            size_t p = buf.find("<tool_call");
            if (p != std::string::npos) { call_seen = true; all += buf; std::string out = buf.substr(0, p); buf.clear(); return out; }
            static const std::string tag = "<tool_call>";
            for (size_t k = std::min(tag.size() - 1, buf.size()); k > 0; k--) if (buf.compare(buf.size() - k, k, tag, 0, k) == 0) { keep = std::max(keep, k); break; }
        }
        if (call_seen) { all += piece; buf.clear(); return ""; }
        if (buf.size() <= keep) return "";
        size_t n = buf.size() - keep;
        while (n > 0 && (buf[n] & 0xC0) == 0x80) n--;
        std::string out = buf.substr(0, n);
        all += out; buf.erase(0, n);
        return out;
    }
    std::string finish() { std::string out = call_seen ? "" : buf; if (!call_seen) all += buf; buf.clear(); return out; }
};
struct Spec {
    std::string model; bool chat = true, stream = true, raw_mode = false, think_req = false, think_present = false;
    std::vector<Turn> turns; std::string raw; Json tools = Json::array(); Json messages = Json::array();
    std::vector<std::string> stop; GenOpts o; Json format; std::vector<std::string> ignored; bool empty_request = false; double keep_alive_s = -1;
    bool clamped = false; bool include_usage = false; std::string tool_choice = "auto";
};
// Admission gate for the single GPU. FIFO within one agent; across agents the one served longest ago goes first (round-robin), so no agent can
// starve another and one console never speaks for two agents at once. (std::timed_mutex is unfair: under contention a waiter can starve.)
class FairGate {
    struct Waiter { uint64_t t; std::string agent; };
    std::mutex m; std::deque<Waiter> q; bool busy = false; uint64_t next = 0, served_seq = 0; std::string current;
    std::map<std::string, uint64_t> last_served;   // agent -> sequence number of its last admission
    size_t pick() const {   // index in q of the next waiter to admit
        size_t best = 0; uint64_t best_ls = ~0ull;
        for (size_t i = 0; i < q.size(); i++) {
            auto it = last_served.find(q[i].agent); uint64_t ls = it == last_served.end() ? 0 : it->second;
            if (ls < best_ls) { best_ls = ls; best = i; }   // strict <: the earliest ticket wins ties, and FIFO holds within an agent
        }
        return best;
    }
public:
    // 0 = acquired, 1 = timed out, 2 = abandoned
    int acquire(int timeout_s, const std::function<bool()>& abandoned, const std::string& agent = "") {
        // Sleep-polling instead of condition_variable::wait_for: the timed condition-variable path of the console's libc++/pthread is unproven.
        uint64_t t;
        { std::lock_guard<std::mutex> l(m); t = next++; q.push_back({t, agent}); }
        const double deadline = nclock::mono_s() + timeout_s;
        for (;;) {
            { std::lock_guard<std::mutex> l(m);
              if (!busy && !q.empty() && q[pick()].t == t) { q.erase(q.begin() + pick()); busy = true; last_served[agent] = ++served_seq; current = agent; if (last_served.size() > 256) last_served.erase(last_served.begin()); return 0; } }
            bool gone = abandoned && abandoned();
            if (gone || nclock::mono_s() >= deadline) {
                std::lock_guard<std::mutex> l(m);
                for (auto it = q.begin(); it != q.end(); ++it) if (it->t == t) { q.erase(it); break; }
                return gone ? 2 : 1;
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(20));
        }
    }
    void release() { std::lock_guard<std::mutex> l(m); busy = false; current.clear(); }
    // {"speaking": agent or "", "queued": {agent: count}} (the empty name is "anonymous")
    Json snapshot() { std::lock_guard<std::mutex> l(m); Json qd = Json::object(); for (auto& w : q) { auto& c = qd[w.agent.empty() ? "anonymous" : w.agent]; c = c.is_null() ? 1 : c.get<int>() + 1; } return {{"speaking", busy ? (current.empty() ? "anonymous" : current) : ""}, {"queued", qd}}; }
};
class Api {
public:
    Api(NativeBackend& b, NativeConfig& c, std::string cfg_path = "") : be(b), cfg(c), cfg_path_(std::move(cfg_path)) { log.file = c.log_file; start_t = mono(); started_at_ = nclock::epoch_s(); }
    EventLog log;
    std::function<Json()> net_stats;   // set by main: the HTTP server's connection counters
    void handle(const nhttp::Request& q, nhttp::Responder& r) {
        requests_++;
        { std::string cb = "req " + q.method + " " + q.path; boot::crumb(cb.c_str()); }
        tl_anthropic = q.path.rfind("/v1/messages", 0) == 0;
        set_agent(clean_agent(q.header("x-agent-id")));
        try { route(q, r); }
        catch (const NativeError& e) { fail(r, e.http, e.code, e.what()); }
        catch (const Json::exception& e) { fail(r, 400, "bad_request", std::string("invalid JSON: ") + e.what()); }
        catch (const std::invalid_argument& e) { fail(r, 400, "bad_request", e.what()); }
        catch (const std::exception& e) { fail(r, 500, "internal", e.what()); }
    }
    void cancel_all() { cancel_epoch_++; }
    // Startup/recovery load: holds the GPU lease so requests queue behind the load. Returns false (and logs) on failure.
    bool preload(const std::string& name) {
        try { Lease l(*this); acquire(l); ensure_loaded(name); return true; }
        catch (const NativeError& e) { errors_++; log.add("ERROR", "preload " + name + ": " + e.code + ": " + e.what()); std::lock_guard<std::mutex> g(last_mu); last_error = {{"time", rfc3339()}, {"code", e.code}, {"error", e.what()}}; return false; }
        catch (const std::exception& e) { errors_++; log.add("ERROR", "preload " + name + ": unexpected " + e.what()); return false; }
        catch (...) { errors_++; log.add("ERROR", "preload " + name + ": unexpected unknown exception"); return false; }
    }
    NativeBackend& backend() { return be; }
private:
    NativeBackend& be; NativeConfig& cfg; std::string cfg_path_;
    FairGate gpu; std::atomic<int> waiting{0}, active{0}; std::atomic<unsigned> cancel_epoch_{0};
    std::atomic<unsigned long long> requests_{0}, errors_{0}, prompt_total{0}, completion_total{0}; double start_t; double started_at_ = 0;
    std::mutex last_mu; Json last_error = nullptr; std::set<std::string> ignored_seen;

    void fail(nhttp::Responder& r, int http, const std::string& code, const std::string& msg) {
        errors_++;
        { std::lock_guard<std::mutex> l(last_mu); last_error = {{"time", rfc3339()}, {"code", code}, {"error", msg}}; }
        log.add("ERROR", code + ": " + msg);
        if (http == 503) r.add_header("Retry-After", "30");
        if (r.started()) return;
        if (tl_anthropic) {
            const char* t = http == 404 ? "not_found_error" : http == 401 || http == 403 ? "authentication_error" : http == 429 ? "rate_limit_error" : http >= 500 ? (http == 503 ? "overloaded_error" : "api_error") : "invalid_request_error";
            r.send(http, "application/json", jdump({{"type", "error"}, {"error", {{"type", t}, {"message", msg}}}}));
        } else r.send(http, "application/json", jdump({{"error", msg}, {"code", code}}));
    }
    static inline thread_local bool tl_anthropic = false;
    static inline thread_local char tl_agent_buf[72] = {0};   // (POD: the console's libc has no destructor support for thread_local objects) the agent (campaign character, tool, user) a request belongs to: x-agent-id header, OpenAI `user`, Anthropic metadata.user_id
    static std::string agent_name() { return std::string(tl_agent_buf); }
    static void set_agent(const std::string& s) { size_t n = std::min<size_t>(s.size(), 64); memcpy(tl_agent_buf, s.data(), n); tl_agent_buf[n] = 0; }
    static std::string clean_agent(const std::string& s) { std::string o; for (char c : s) { if (o.size() >= 64) break; if (isalnum((unsigned char)c) || c == '.' || c == '_' || c == ':' || c == '-' || c == '@') o += c; } return o; }
    void json_reply(nhttp::Responder& r, const Json& j, int status = 200) { r.send(status, "application/json", jdump(j)); }
    static Json parse_body(const nhttp::Request& q) {
        if (q.body.empty()) return Json::object();
        Json j = Json::parse(q.body);
        if (!j.is_object()) throw ApiError(400, "bad_request", "request body must be a JSON object");
        if (agent_name().empty()) {   // the agent can also be named in the body: OpenAI `user`, Anthropic metadata.user_id
            if (j.contains("user") && j["user"].is_string()) set_agent(clean_agent(j["user"].get<std::string>()));
            else if (j.contains("metadata") && j["metadata"].is_object() && j["metadata"].contains("user_id") && j["metadata"]["user_id"].is_string()) set_agent(clean_agent(j["metadata"]["user_id"].get<std::string>()));
        }
        return j;
    }
    // ---------------------------------------------------------------- auth / routing
    bool authorized(const nhttp::Request& q) {
        if (cfg.trusted(q.peer_ip)) return true;
        if (cfg.api_key.empty()) return false;
        std::string tok;
        if (!parse_bearer(q.header("authorization"), tok)) tok = q.header("x-api-key");   // Anthropic SDK convention
        return !tok.empty() && ct_equal(tok, cfg.api_key);   // constant-time: the key is a secret on a LAN-reachable port
    }
    void route(const nhttp::Request& q, nhttp::Responder& r) {
        const std::string& p = q.path; const std::string& m = q.method;
        if (!authorized(q)) throw ApiError(403, "forbidden", "peer is outside the trusted network and no valid API key was supplied");
        if (p == "/" && (m == "GET" || m == "HEAD")) { r.send(200, "text/plain; charset=utf-8", "Ollama is running", m == "HEAD"); return; }
        if (p == "/api/version" && m == "GET") return json_reply(r, {{"version", NATIVE_VERSION}, {"build", NATIVE_BUILD}});
        if (p == "/api/tags" && (m == "GET" || m == "HEAD")) return json_reply(r, tags());
        if (p == "/api/ps" && m == "GET") return json_reply(r, ps());
        if (p == "/api/show" && m == "POST") return show(r, parse_body(q));
        if (p == "/api/chat" && m == "POST") return infer(q, r, parse_body(q), "ollama-chat");
        if (p == "/api/generate" && m == "POST") return infer(q, r, parse_body(q), "ollama-generate");
        if (p == "/v1/models" && m == "GET") return openai_models(r, "");
        if (p.rfind("/v1/models/", 0) == 0 && m == "GET") return openai_models(r, p.substr(11));
        if (p == "/v1/chat/completions" && m == "POST") return infer(q, r, parse_body(q), "openai-chat");
        if (p == "/v1/completions" && m == "POST") return infer(q, r, parse_body(q), "openai-completion");
        if (p == "/v1/messages" && m == "POST") return infer(q, r, parse_body(q), "anthropic");
        if (p == "/v1/messages/count_tokens" && m == "POST") return count_tokens(r, parse_body(q));
        if ((p == "/api/embed" || p == "/api/embeddings" || p == "/v1/embeddings"))
            throw ApiError(501, "not_supported", "embeddings are not supported by the Garlic engine (no embedding head)");
        if (p == "/api/pull" || p == "/api/push" || p == "/api/create" || p == "/api/copy" || p.rfind("/api/blobs", 0) == 0 || (p == "/api/delete"))
            throw ApiError(501, "not_supported", "this model is not installed on the console, and pull/push/create/copy/delete are not supported: install bundles under /data/nicklabs-native/models (list them in models.json)");
        if (p == "/native/status" && m == "GET") return json_reply(r, status());
        if (p == "/native/health" && m == "GET") return json_reply(r, {{"ok", true}, {"loaded", be.loaded()}, {"busy", active.load() > 0}});
        if (p == "/native/log" && m == "GET") { size_t n = 100; auto i = q.query.find("n="); if (i != std::string::npos) n = std::min<size_t>(400, (size_t)atoi(q.query.c_str() + i + 2)); return json_reply(r, {{"log", log.tail(n)}}); }
        if (p == "/native/config" && m == "GET") return json_reply(r, cfg.to_json());
        if (p == "/native/config" && m == "POST") return set_config(r, parse_body(q));
        if (p == "/native/load" && m == "POST") return native_load(r, parse_body(q));
        if (p == "/native/unload" && m == "POST") return native_unload(r, parse_body(q));
        if (p == "/native/cancel" && m == "POST") { cancel_all(); log.add("INFO", "cancel requested"); return json_reply(r, {{"cancelled", true}}); }
        throw ApiError(p.rfind("/api/", 0) == 0 || p.rfind("/v1/", 0) == 0 || p.rfind("/native/", 0) == 0 ? 404 : 404, "not_found", "no such endpoint: " + m + " " + p);
    }
    // ---------------------------------------------------------------- catalog
    std::vector<ModelInfo> models() { return be.discover(); }
    static std::string bare_digest(std::string d) { if (d.compare(0, 7, "sha256:") == 0) d.erase(0, 7); return d; }
    static Json details(const ModelInfo& m) { return {{"parent_model", ""}, {"format", "garlic"}, {"family", m.family}, {"families", {m.family}}, {"parameter_size", m.parameter_size}, {"quantization_level", m.quantization}}; }
    Json tags() {
        Json a = Json::array();
        for (auto& m : models()) a.push_back({{"name", m.name}, {"model", m.name}, {"modified_at", m.modified_at.empty() ? rfc3339() : m.modified_at}, {"size", m.size}, {"digest", bare_digest(m.digest)}, {"details", details(m)}});
        return {{"models", a}};
    }
    Json ps() {
        Json a = Json::array(); std::string cur = be.loaded();
        if (!cur.empty()) for (auto& m : models()) if (m.name == cur)
            a.push_back({{"name", m.name}, {"model", m.name}, {"size", m.size}, {"digest", bare_digest(m.digest)}, {"details", details(m)}, {"expires_at", "2999-12-31T23:59:59Z"}, {"size_vram", m.size}, {"context_length", be.ctx_limit()}});
        return {{"models", a}};
    }
    const ModelInfo* find_model(const std::vector<ModelInfo>& ms, std::string name) {
        if (name.empty()) name = cfg.default_model;
        if (name.empty()) { std::string cur = be.loaded(); name = cur; }
        if (name.empty() && !ms.empty()) name = ms[0].name;
        std::string base = name; if (base.size() > 7 && base.compare(base.size() - 7, 7, ":latest") == 0) base.resize(base.size() - 7);
        for (auto& m : ms) if (m.name == name || m.name == base || m.name + ":latest" == name) return &m;
        return nullptr;
    }
    void show(nhttp::Responder& r, const Json& b) {
        std::string name = b.value("model", b.value("name", std::string()));
        auto ms = models(); auto* m = find_model(ms, name);
        if (!m) throw ApiError(404, "model_not_found", "model '" + name + "' not found");
        Json caps = Json::array({"completion"}); if (m->tools) caps.push_back("tools"); if (m->thinking) caps.push_back("thinking");
        json_reply(r, {{"modelfile", "# NickLabs native Garlic bundle " + m->name + "\nFROM " + m->dir + "\n"}, {"parameters", "num_ctx " + std::to_string(m->ctx_max) + "\ntemperature " + std::to_string(cfg.default_temperature)},
                       {"template", m->template_name}, {"details", details(*m)}, {"capabilities", caps}, {"modified_at", m->modified_at.empty() ? rfc3339() : m->modified_at},
                       {"model_info", {{"general.architecture", m->family}, {"general.parameter_count", 9000000000ull}, {m->family + ".context_length", m->ctx_max}, {"native.speculative_decoding", m->mtp}}}});
    }
    void openai_models(nhttp::Responder& r, const std::string& one) {
        Json a = Json::array();
        for (auto& m : models()) { if (!one.empty() && m.name != one) continue; a.push_back({{"id", m.name}, {"object", "model"}, {"created", 0}, {"owned_by", "nicklabs-native"}}); }
        if (!one.empty()) { if (a.empty()) throw ApiError(404, "model_not_found", "model '" + one + "' not found"); return json_reply(r, a[0]); }
        json_reply(r, {{"object", "list"}, {"data", a}});
    }
    Json status() {
        return {{"app", "nicklabs-native"}, {"build", NATIVE_BUILD}, {"version", NATIVE_VERSION}, {"uptime_s", mono() - start_t}, {"started_at", started_at_}, {"http", net_stats ? net_stats() : Json::object()}, {"agents", gpu.snapshot()}, {"loaded", be.loaded()}, {"ctx_limit", be.ctx_limit()},
                {"active_requests", active.load()}, {"queued_requests", waiting.load()}, {"requests", (unsigned long long)requests_}, {"errors", (unsigned long long)errors_},
                {"prompt_tokens_total", (unsigned long long)prompt_total}, {"completion_tokens_total", (unsigned long long)completion_total},
                {"last_error", snapshot_error()}, {"ignored_options_seen", ignored_list()}, {"backend", be.telemetry()}, {"config", cfg.to_json()}};
    }
    Json snapshot_error() { std::lock_guard<std::mutex> l(last_mu); return last_error; }
    Json ignored_list() { std::lock_guard<std::mutex> l(last_mu); return Json(ignored_seen); }
    void set_config(nhttp::Responder& r, const Json& b) {
        std::vector<std::string> rk;
        std::string e = cfg.apply(b, &rk);
        if (!e.empty()) throw ApiError(400, "bad_config", e);
        if (!cfg_path_.empty() && !write_file_atomic(cfg_path_, cfg.to_persist().dump(2))) throw ApiError(500, "config_write_failed", "could not persist configuration to " + cfg_path_);
        log.add("INFO", "config updated");
        json_reply(r, {{"config", cfg.to_json()}, {"restart_required_for", rk}});
    }
    // ---------------------------------------------------------------- GPU ownership
    struct Lease { Api& a; bool held = false; explicit Lease(Api& x) : a(x) {} ~Lease() { if (held) { a.active--; a.gpu.release(); } } };
    // FIFO admission to the single GPU owner. `abandoned` (client disconnected) drops a queued request without spending GPU time.
    void acquire(Lease& l, const std::function<bool()>& abandoned = nullptr) {
        if (active.load() > 0 && waiting.load() >= cfg.max_queue) throw ApiError(503, "queue_full", "too many requests are already waiting for the PS4 GPU; retry shortly");
        waiting++;
        int rc = gpu.acquire(cfg.queue_wait_s, abandoned, agent_name());
        waiting--;
        if (rc == 2) throw ApiError(499, "client_gone", "client disconnected while queued");
        if (rc != 0) throw ApiError(503, "busy", "the PS4 GPU is busy with another request; retry later");
        l.held = true; active++;
    }
    void ensure_loaded(const std::string& name) {
        if (be.loaded() == name) return;
        bool restarting = false;
        log.add("INFO", "loading model " + name);
        if (!be.load(name, &restarting)) {
            if (restarting) throw ApiError(503, "swapping", "switching to model '" + name + "' requires relaunching the app; it is restarting now, retry in a few minutes");
            throw ApiError(500, "load_failed", "model '" + name + "' failed to load");
        }
    }
    void native_load(nhttp::Responder& r, const Json& b) {
        std::string name = b.value("model", std::string());
        auto ms = models(); auto* m = find_model(ms, name);
        if (!m) throw ApiError(404, "model_not_found", "model '" + name + "' not found");
        std::string n = m->name;
        Lease l(*this); acquire(l);
        double t0 = mono();
        ensure_loaded(n);
        json_reply(r, {{"status", "loaded"}, {"model", n}, {"ctx_limit", be.ctx_limit()}, {"load_s", mono() - t0}});
    }
    void native_unload(nhttp::Responder& r, const Json& b) {
        bool release = b.value("release", false);
        if (release && !cfg.allow_release_restart) throw ApiError(409, "release_disabled", "release (relaunch) is disabled: set allow_release_restart=true");
        Lease l(*this); acquire(l);
        bool restarting = false;
        be.unload(release, &restarting);
        json_reply(r, {{"status", restarting ? "restarting" : "unloaded"}, {"loaded", be.loaded()}}, restarting ? 202 : 200);
    }
    // ---------------------------------------------------------------- Anthropic Messages dialect
    static std::string block_text(const Json& c) {
        if (c.is_string()) return c.get<std::string>();
        std::string o;
        if (c.is_array()) for (auto& b : c) { if (b.is_object() && b.value("type", std::string()) == "text") o += b.value("text", std::string()); else if (b.is_string()) o += b.get<std::string>(); }
        return o;
    }
    void anthropic_messages(Spec& s, const Json& b) {
        if (!b.contains("messages") || !b["messages"].is_array() || b["messages"].empty()) throw ApiError(400, "bad_request", "messages: at least one message is required");
        if (b.contains("system")) { std::string sys = block_text(b["system"]); if (!sys.empty()) s.messages.push_back({{"role", "system"}, {"content", sys}}); }
        for (auto& m : b["messages"]) {
            std::string role = m.value("role", std::string());
            if (role == "developer") role = "system";   // Claude Code injects system-role updates mid-conversation; they fold into the leading system prompt
            if (role != "user" && role != "assistant" && role != "system") throw ApiError(400, "bad_request", "messages: role must be user, assistant or system");
            const Json c = m.value("content", Json(""));
            if (role == "system") { std::string t = block_text(c); if (!t.empty()) s.messages.push_back({{"role", "system"}, {"content", t}}); continue; }
            if (c.is_string()) { s.messages.push_back({{"role", role}, {"content", c}}); continue; }
            if (!c.is_array()) throw ApiError(400, "bad_request", "messages: content must be a string or an array of blocks");
            std::string text; Json calls = Json::array(); Json results = Json::array();
            for (auto& blk : c) {
                std::string t = blk.value("type", std::string());
                if (t == "text") text += blk.value("text", std::string());
                else if (t == "tool_use") calls.push_back({{"name", blk.value("name", std::string())}, {"arguments", blk.value("input", Json::object())}});
                else if (t == "tool_result") results.push_back(block_text(blk.value("content", Json(""))) + (blk.value("is_error", false) ? "\n[tool reported an error]" : ""));
                else if (t == "thinking" || t == "redacted_thinking") continue;   // reasoning of earlier turns is not replayed (Qwen template drops it too)
                else throw ApiError(400, "unsupported_content", "unsupported content block type '" + t + "' (text, tool_use and tool_result are supported)");
            }
            for (auto& rs : results) s.messages.push_back({{"role", "tool"}, {"content", rs}});
            if (role == "assistant") { Json mm = {{"role", "assistant"}, {"content", text}}; if (!calls.empty()) mm["tool_calls"] = calls; s.messages.push_back(mm); }
            else if (!text.empty() || results.empty()) s.messages.push_back({{"role", "user"}, {"content", text}});
        }
    }
    void count_tokens(nhttp::Responder& r, const Json& b) {
        Spec s; anthropic_messages(s, b);
        Json tools = b.contains("tools") ? anthropic_tools(b["tools"]) : Json::array();
        Json chat = ntools::messages_to_chat(s.messages, tools);
        std::string all; for (auto& m : chat) all += "<|im_start|>" + m["role"].get<std::string>() + "\n" + m["content"].get<std::string>() + "<|im_end|>\n";
        Lease l(*this); acquire(l);
        if (be.loaded().empty()) throw ApiError(409, "no_model", "no model is loaded: token counting needs the model's tokenizer");
        json_reply(r, {{"input_tokens", be.count_tokens(all) + 3}});
    }
    static Json anthropic_tools(const Json& t) {
        Json out = Json::array();
        if (!t.is_array()) throw ApiError(400, "bad_request", "tools must be an array");
        for (auto& e : t) {
            if (!e.is_object() || !e.contains("name") || !e["name"].is_string()) throw ApiError(400, "bad_request", "each tool needs a name");
            if (e.contains("type") && e["type"].is_string() && e["type"] != "custom") continue;   // server tools (web_search, ...) cannot run on the console
            Json o = {{"name", e["name"]}}; if (e.contains("description")) o["description"] = e["description"];
            o["parameters"] = e.contains("input_schema") ? e["input_schema"] : Json{{"type", "object"}, {"properties", Json::object()}};
            out.push_back(o);
        }
        return out;
    }
    // ---------------------------------------------------------------- request normalization
    static std::string text_of(const Json& c, const char* what) {
        if (c.is_null()) return "";
        if (c.is_string()) return c.get<std::string>();
        if (c.is_array()) {
            std::string o;
            for (auto& p : c) {
                if (p.is_string()) { o += p.get<std::string>(); continue; }
                std::string t = p.value("type", std::string());
                if (t == "text" || t == "input_text") o += p.value("text", std::string());
                else throw ApiError(400, "unsupported_content", std::string("only text content is supported (got '") + t + "' in " + what + "); this model has no vision input");
            }
            return o;
        }
        throw ApiError(400, "bad_request", std::string(what) + " must be a string");
    }
    static Json norm_calls(const Json& tc) {
        Json out = Json::array();
        if (!tc.is_array()) return out;
        for (auto& c : tc) {
            Json f = c.contains("function") ? c["function"] : c;
            Json args = f.value("arguments", Json::object());
            if (args.is_string()) { args = Json::parse(args.get<std::string>(), nullptr, false); if (args.is_discarded()) args = Json::object(); }
            out.push_back({{"name", f.value("name", std::string())}, {"arguments", args}});
        }
        return out;
    }
    static Json norm_tools(const Json& t) {
        Json out = Json::array();
        if (t.is_null()) return out;
        if (!t.is_array()) throw ApiError(400, "bad_request", "tools must be an array");
        for (auto& e : t) {
            Json f = e.contains("function") ? e["function"] : e;
            if (!f.is_object() || !f.contains("name") || !f["name"].is_string()) throw ApiError(400, "bad_request", "each tool needs a function name");
            Json o = {{"name", f["name"]}}; if (f.contains("description")) o["description"] = f["description"];
            o["parameters"] = f.contains("parameters") ? f["parameters"] : Json{{"type", "object"}, {"properties", Json::object()}};
            out.push_back(o);
        }
        return out;
    }
    static std::vector<std::string> parse_stop(const Json& s) {
        std::vector<std::string> v;
        if (s.is_null()) return v;
        if (s.is_string()) v.push_back(s); else if (s.is_array()) { for (auto& e : s) { if (!e.is_string()) throw ApiError(400, "bad_request", "stop must be strings"); v.push_back(e); } } else throw ApiError(400, "bad_request", "stop must be a string or array");
        if (v.size() > 16) throw ApiError(400, "bad_request", "at most 16 stop sequences");
        return v;
    }
    // Strict integer field: floats, strings, bools, null are refused with a clear 400 (a float -> long long conversion is undefined behaviour).
    static long long int_field(const Json& v, const char* name) {
        if (!v.is_number_integer()) throw ApiError(400, "bad_request", std::string(name) + " must be an integer");
        if (v.is_number_unsigned() && v.get<unsigned long long>() > (unsigned long long)INT64_MAX) return INT64_MAX;
        return v.get<long long>();
    }
    static double num(const Json& v, double lo, double hi, const char* name) {
        if (!v.is_number() || v.get<double>() < lo || v.get<double>() > hi) throw ApiError(400, "bad_request", std::string(name) + " must be a number in [" + std::to_string(lo) + ", " + std::to_string(hi) + "]");
        return v.get<double>();
    }
    static double parse_keep_alive(const Json& k) {
        if (k.is_null()) return -1;
        if (k.is_number()) return k.get<double>();
        if (k.is_string()) {
            std::string s = k; if (s.empty()) return -1; char* e = nullptr; double v = strtod(s.c_str(), &e);
            double mult = 1; std::string u = e;
            if (u == "m") mult = 60; else if (u == "h") mult = 3600; else if (u == "ms") mult = 0.001; else if (!(u.empty() || u == "s")) throw ApiError(400, "bad_request", "invalid keep_alive");
            return v * mult;
        }
        throw ApiError(400, "bad_request", "invalid keep_alive");
    }
    void apply_think(Spec& s, const Json& t) {
        s.think_present = !t.is_null();
        if (t.is_null()) { s.o.think = cfg.default_think; s.o.think_budget = cfg.think_budget_medium; return; }
        if (t.is_boolean()) { s.o.think = t; s.o.think_budget = cfg.think_budget_medium; }
        else if (t.is_string()) {
            std::string v = t;
            if (v == "low") s.o.think_budget = cfg.think_budget_low; else if (v == "medium") s.o.think_budget = cfg.think_budget_medium;
            else if (v == "high") s.o.think_budget = cfg.think_budget_high;
            else if (v == "none" || v == "off" || v == "minimal") { s.o.think = false; s.o.think_budget = cfg.think_budget_low; return; }
            else throw ApiError(400, "bad_request", "think must be true/false or low|medium|high");
            s.o.think = true;
        } else throw ApiError(400, "bad_request", "think must be true/false or low|medium|high");
    }
    static const std::set<std::string>& known_ignored() {
        static const std::set<std::string> k = {"top_p", "top_k", "min_p", "typical_p", "tfs_z", "repeat_penalty", "repeat_last_n", "presence_penalty", "frequency_penalty", "penalize_newline",
            "mirostat", "mirostat_tau", "mirostat_eta", "num_keep", "num_batch", "num_gpu", "main_gpu", "num_thread", "use_mmap", "use_mlock", "numa", "low_vram", "vocab_only", "f16_kv", "logits_all",
            "logit_bias", "logprobs", "top_logprobs", "user", "n_probs", "ignore_eos", "cache_prompt"};
        return k;
    }
    void options(Spec& s, const Json& o, const std::set<std::string>& skip = {}) {
        if (o.is_null()) return;
        if (!o.is_object()) throw ApiError(400, "bad_request", "options must be an object");
        for (auto it = o.begin(); it != o.end(); ++it) {
            const std::string& k = it.key(); const Json& v = it.value();
            if (v.is_null() || skip.count(k)) continue;
            if (k == "temperature") s.o.temperature = (float)num(v, 0, 2, "temperature");
            else if (k == "seed") { if (!v.is_number_integer()) throw ApiError(400, "bad_request", "seed must be an integer"); s.o.seed = (uint32_t)(v.get<long long>() & 0xffffffff); }
            else if (k == "num_predict" || k == "max_tokens") { if (!v.is_number_integer()) throw ApiError(400, "bad_request", k + " must be an integer"); long long n = v.get<long long>(); s.o.max_tokens = n <= 0 ? -1 : (int)std::min<long long>(n, 1 << 20); }
            else if (k == "num_ctx") { if (!v.is_number_integer() || v.get<long long>() < 0) throw ApiError(400, "bad_request", "num_ctx must be a non-negative integer"); s.o.num_ctx = (uint32_t)std::min<long long>(v.get<long long>(), 1 << 24); }
            else if (k == "stop") { s.stop = parse_stop(v); }
            else s.ignored.push_back(k);
        }
    }
    void note_ignored(const Spec& s) {
        if (s.ignored.empty()) return;
        std::lock_guard<std::mutex> l(last_mu);
        for (auto& k : s.ignored) ignored_seen.insert(k);
    }
    static void format_note(Spec& s) {
        if (s.format.is_null()) return;
        std::string ins = s.format.is_string() && s.format == "json" ? "Respond with a single valid JSON object only. No prose, no markdown fences."
            : "Respond with a single valid JSON value that conforms exactly to this JSON Schema, and nothing else (no prose, no markdown fences):\n" + jdump(s.format);
        bool done = false;
        for (auto& t : s.turns) if (t.role == "system") { t.content += (t.content.empty() ? "" : "\n\n") + ins; done = true; break; }
        if (!done && s.chat) s.turns.insert(s.turns.begin(), Turn{"system", ins});
    }
    Spec build(const Json& b, const std::string& dialect) {
        Spec s; s.o.temperature = (float)cfg.default_temperature; s.o.max_tokens = cfg.default_max_tokens;
        bool oa = dialect.rfind("openai", 0) == 0, an = dialect == "anthropic";
        s.model = b.value("model", std::string());
        s.stream = b.contains("stream") && !b["stream"].is_null() ? b["stream"].get<bool>() : !oa;
        if (b.contains("keep_alive")) s.keep_alive_s = parse_keep_alive(b["keep_alive"]);
        if (an) {
            s.stream = b.value("stream", false);
            if (b.contains("temperature") && !b["temperature"].is_null()) s.o.temperature = (float)num(b["temperature"], 0, 2, "temperature");
            if (!b.contains("max_tokens") || !b["max_tokens"].is_number_integer() || b["max_tokens"].get<long long>() < 1) throw ApiError(400, "bad_request", "max_tokens: required, a positive integer");
            s.o.max_tokens = (int)std::min<long long>(b["max_tokens"].get<long long>(), 1 << 20);
            if (b.contains("stop_sequences")) s.stop = parse_stop(b["stop_sequences"]);
            for (auto k : {"top_p", "top_k", "metadata", "service_tier"}) if (b.contains(k) && !b[k].is_null()) s.ignored.push_back(k);
            s.o.think = false; s.o.think_budget = cfg.think_budget_medium;
            if (b.contains("thinking") && b["thinking"].is_object() && b["thinking"].value("type", std::string()) == "enabled") {
                s.o.think = true; long long bt = b["thinking"].contains("budget_tokens") ? int_field(b["thinking"]["budget_tokens"], "thinking.budget_tokens") : 0; if (bt >= 16) s.o.think_budget = (int)std::min<long long>(bt, 65536);
            } else if (!b.contains("thinking")) s.o.think = cfg.default_think;
            if (b.contains("tool_choice") && b["tool_choice"].is_object() && b["tool_choice"].value("type", std::string()) == "none") s.tool_choice = "none";
            anthropic_messages(s, b);
            if (b.contains("tools") && s.tool_choice != "none") s.tools = anthropic_tools(b["tools"]);
            Json chat = ntools::messages_to_chat(s.messages, s.tools);
            for (auto& m : chat) s.turns.push_back({m["role"], m["content"]});
            return s;
        }
        apply_think(s, oa ? (b.contains("reasoning_effort") ? b["reasoning_effort"] : Json()) : (b.contains("think") ? b["think"] : Json()));
        if (b.contains("reasoning") && b["reasoning"].is_object() && b["reasoning"].contains("effort")) apply_think(s, b["reasoning"]["effort"]);
        s.o.think = s.o.think && true;
        if (oa) {
            std::set<std::string> skip;
            if (b.contains("temperature")) s.o.temperature = (float)num(b["temperature"], 0, 2, "temperature");
            if (b.contains("max_completion_tokens") && !b["max_completion_tokens"].is_null()) s.o.max_tokens = (int)std::min<long long>(int_field(b["max_completion_tokens"], "max_completion_tokens"), 1 << 20);
            else if (b.contains("max_tokens") && !b["max_tokens"].is_null()) s.o.max_tokens = (int)std::min<long long>(int_field(b["max_tokens"], "max_tokens"), 1 << 20);
            if (b.contains("seed") && b["seed"].is_number_integer()) s.o.seed = (uint32_t)(b["seed"].get<long long>() & 0xffffffff);
            if (b.contains("stop")) s.stop = parse_stop(b["stop"]);
            if (b.contains("n") && b["n"].is_number_integer() && b["n"] != 1) throw ApiError(400, "not_supported", "n>1 is not supported");
            if (b.contains("stream_options") && b["stream_options"].is_object()) s.include_usage = b["stream_options"].value("include_usage", false);
            if (b.contains("response_format") && b["response_format"].is_object()) {
                std::string t = b["response_format"].value("type", std::string());
                if (t == "json_object") s.format = "json";
                else if (t == "json_schema" && b["response_format"].contains("json_schema")) s.format = b["response_format"]["json_schema"].value("schema", Json("json"));
            }
            if (b.contains("tool_choice")) { if (b["tool_choice"].is_string()) s.tool_choice = b["tool_choice"]; else s.tool_choice = "function"; }
            for (auto k : {"top_p", "presence_penalty", "frequency_penalty", "logit_bias", "logprobs", "top_logprobs", "user"}) if (b.contains(k) && !b[k].is_null()) s.ignored.push_back(k);
        } else {
            if (b.contains("options")) options(s, b["options"]);
            if (b.contains("format")) s.format = b["format"];
        }
        if (dialect == "openai-completion" || dialect == "ollama-generate") {
            s.chat = false;
            std::string prompt = b.contains("prompt") ? text_of(b["prompt"], "prompt") : "";
            if (b.contains("images") && b["images"].is_array() && !b["images"].empty()) throw ApiError(400, "unsupported_content", "images are not supported by this model");
            if (b.contains("suffix") && b["suffix"].is_string() && !b["suffix"].get<std::string>().empty()) throw ApiError(400, "not_supported", "suffix (fill-in-the-middle) is not supported");
            s.raw_mode = b.value("raw", false);
            if (s.raw_mode) s.raw = prompt;
            else {
                std::string sys = b.contains("system") && b["system"].is_string() ? b["system"].get<std::string>() : "";
                if (!sys.empty()) s.turns.push_back({"system", sys});
                s.turns.push_back({"user", prompt});
            }
            s.empty_request = prompt.empty();
            if (b.contains("tools")) throw ApiError(400, "not_supported", "tools are supported on chat endpoints only");
        } else {
            if (!b.contains("messages") || !b["messages"].is_array()) { if (b.contains("messages") || !b.contains("model")) throw ApiError(400, "bad_request", "messages must be an array"); s.empty_request = true; }
            else {
                s.empty_request = b["messages"].empty();
                for (auto& m : b["messages"]) {
                    if (!m.is_object()) throw ApiError(400, "bad_request", "each message must be an object");
                    if (m.contains("images") && m["images"].is_array() && !m["images"].empty()) throw ApiError(400, "unsupported_content", "images are not supported by this model");
                    std::string role = m.value("role", std::string());
                    if (role == "developer") role = "system";
                    if (role != "system" && role != "user" && role != "assistant" && role != "tool") throw ApiError(400, "bad_request", "unsupported message role '" + role + "'");
                    Json mm = {{"role", role}, {"content", text_of(m.value("content", Json()), "content")}};
                    if (role == "assistant" && m.contains("tool_calls")) mm["tool_calls"] = norm_calls(m["tool_calls"]);
                    s.messages.push_back(mm);
                }
            }
            if (b.contains("tools") && s.tool_choice != "none") s.tools = norm_tools(b["tools"]);
            if (!s.messages.empty() || !s.tools.empty()) {
                Json chat = ntools::messages_to_chat(s.messages, s.tools);
                for (auto& m : chat) s.turns.push_back({m["role"], m["content"]});
            }
        }
        if (s.turns.size() && s.turns.back().role == "assistant" && s.chat) { /* prefill of the last assistant turn is not supported: generation continues as a new turn */ }
        format_note(s);
        return s;
    }
    // ---------------------------------------------------------------- inference
    struct Out { Json msg_calls = Json::array(); std::string content, thinking; GenStats st; std::string done_reason = "stop"; };
    void infer(const nhttp::Request&, nhttp::Responder& r, const Json& body, const std::string& dialect) {
        Spec s = build(body, dialect);
        bool oa = dialect.rfind("openai", 0) == 0, an = dialect == "anthropic";
        auto ms = models(); auto* mi = find_model(ms, s.model);
        if (!mi) throw ApiError(404, "model_not_found", "model '" + s.model + "' not found");
        std::string model = mi->name;
        Lease lease(*this);
        // Ollama's load/unload idioms: empty prompt/messages.
        if (s.empty_request && !s.raw_mode) {
            acquire(lease);
            std::string reason = "load";
            if (s.keep_alive_s == 0) {
                bool restarting = false;
                be.unload(cfg.allow_release_restart, &restarting);
                reason = "unload";
            } else ensure_loaded(model);
            if (oa || an) throw ApiError(400, "bad_request", "empty prompt");
            Json j = dialect == "ollama-chat" ? Json{{"model", model}, {"created_at", rfc3339()}, {"message", {{"role", "assistant"}, {"content", ""}}}, {"done", true}, {"done_reason", reason}}
                                              : Json{{"model", model}, {"created_at", rfc3339()}, {"response", ""}, {"done", true}, {"done_reason", reason}};
            return json_reply(r, j);
        }
        acquire(lease, [&r] { return r.peer_closed(); });
        double t_start = mono();
        ensure_loaded(model);
        double load_s = mono() - t_start;
        if (s.o.max_tokens <= 0) s.o.max_tokens = 1 << 20;
        uint32_t lim = be.ctx_limit();
        uint32_t want = s.o.num_ctx ? s.o.num_ctx : (cfg.default_num_ctx ? cfg.default_num_ctx : lim);
        if (lim && want > lim) { want = lim; s.clamped = true; }
        s.o.num_ctx = want;
        note_ignored(s);
        if (!s.ignored.empty()) { std::string h; for (auto& k : s.ignored) h += (h.empty() ? "" : ",") + k; r.add_header("X-Native-Ignored-Options", h); }
        if (s.clamped) r.add_header("X-Native-Context-Clamped", std::to_string(lim));
        r.add_header("X-Native-Context", std::to_string(want));
        std::string id = (an ? "msg_" : oa ? "chatcmpl-" : "nl-") + std::to_string((long long)(mono() * 1000));
        bool tools = !s.tools.empty();
        TextGate gate(s.stop, tools);
        std::string thinking;
        unsigned epoch0 = cancel_epoch_.load();
        bool stream = s.stream, begun = false, client_gone = false;
        auto begin = [&]() -> bool {
            if (begun) return true;
            begun = true;
            return r.begin_stream(200, oa || an ? "text/event-stream" : "application/x-ndjson");
        };
        auto chunk = [&](const Json& j) -> bool {
            if (!begin()) return false;
            return r.write_chunk(oa ? "data: " + jdump(j) + "\n\n" : jdump(j) + "\n");
        };
        auto oa_delta = [&](const Json& delta, const Json& fin) {
            Json c = {{"index", 0}, {"delta", delta}, {"finish_reason", fin}};
            if (dialect == "openai-completion") c = {{"index", 0}, {"text", delta.value("content", std::string())}, {"finish_reason", fin}};
            return Json{{"id", id}, {"object", dialect == "openai-chat" ? "chat.completion.chunk" : "text_completion"}, {"created", (long long)time(nullptr)}, {"model", model}, {"choices", {c}}};
        };
        // ---- Anthropic SSE: message_start, then typed content blocks (thinking | text | tool_use), message_delta, message_stop
        bool an_open = false, an_started = false; int an_index = -1; std::string an_kind;
        auto an_event = [&](const char* name, const Json& j) { return begin() && r.write_chunk(std::string("event: ") + name + "\ndata: " + jdump(j) + "\n\n"); };
        auto an_start = [&]() -> bool {
            if (an_started) return true;
            an_started = true;
            return an_event("message_start", {{"type", "message_start"}, {"message", {{"id", id}, {"type", "message"}, {"role", "assistant"}, {"model", model}, {"content", Json::array()}, {"stop_reason", nullptr}, {"stop_sequence", nullptr}, {"usage", {{"input_tokens", 0}, {"output_tokens", 0}}}}}});
        };
        auto an_close = [&]() -> bool { if (!an_open) return true; an_open = false; return an_event("content_block_stop", {{"type", "content_block_stop"}, {"index", an_index}}); };
        auto an_text = [&](const std::string& t, bool think) -> bool {
            if (!an_start()) return false;
            std::string kind = think ? "thinking" : "text";
            if (!an_open || an_kind != kind) {
                if (!an_close()) return false;
                an_index++; an_open = true; an_kind = kind;
                if (!an_event("content_block_start", {{"type", "content_block_start"}, {"index", an_index}, {"content_block", think ? Json{{"type", "thinking"}, {"thinking", ""}} : Json{{"type", "text"}, {"text", ""}}}})) return false;
            }
            return an_event("content_block_delta", {{"type", "content_block_delta"}, {"index", an_index}, {"delta", think ? Json{{"type", "thinking_delta"}, {"thinking", t}} : Json{{"type", "text_delta"}, {"text", t}}}});
        };
        auto send_text = [&](const std::string& t, bool think) -> bool {
            if (t.empty() || !stream) return true;
            if (an) return an_text(t, think);
            if (oa) return chunk(oa_delta(think ? Json{{"reasoning_content", t}} : Json{{"content", t}}, nullptr));
            if (s.chat) return chunk({{"model", model}, {"created_at", rfc3339()}, {"message", {{"role", "assistant"}, {think ? "thinking" : "content", t}}}, {"done", false}});
            return chunk({{"model", model}, {"created_at", rfc3339()}, {think ? "thinking" : "response", t}, {"done", false}});
        };
        if (stream && oa && dialect == "openai-chat" && !chunk(oa_delta({{"role", "assistant"}, {"content", ""}}, nullptr))) client_gone = true;
        auto cancelled = [&] { return client_gone || cancel_epoch_.load() != epoch0 || r.peer_closed(); };
        auto emit = [&](const std::string& text, Piece kind) -> bool {
            if (kind == Piece::Think) { thinking += text; if (!send_text(text, true)) { client_gone = true; return false; } return true; }
            std::string out = gate.push(text);
            if (!out.empty() && !send_text(out, false)) { client_gone = true; return false; }
            return !gate.stopped;
        };
        GenStats st;
        try {
            st = s.raw_mode ? be.generate(nullptr, &s.raw, s.o, cancelled, emit) : be.generate(&s.turns, nullptr, s.o, cancelled, emit);
        } catch (const NativeError& e) {
            if (begun) {   // error after the stream started: report in-band
                log.add("ERROR", e.code + ": " + e.what()); errors_++;
                chunk(oa ? Json{{"error", {{"message", e.what()}, {"type", e.code}}}} : Json{{"error", e.what()}});
                if (oa) r.write_chunk("data: [DONE]\n\n");
                r.end_stream();
                return;
            }
            throw;
        }
        std::string tail = gate.finish();
        if (!tail.empty() && !send_text(tail, false)) client_gone = true;
        if (client_gone || st.done_reason == "cancelled") { log.add("INFO", "request cancelled by client"); if (begun) r.end_stream(); else r.abandon(); return; }
        st.load_s += load_s;
        std::string content = gate.all;
        Json calls = Json::array();
        if (tools) { auto p = ntools::parse_calls(content, s.tools); content = p.content; calls = p.calls; }
        else if (s.format.is_string() || s.format.is_object()) content = strip_fences(content);
        std::string reason = st.done_reason == "length" && calls.empty() ? "length" : "stop";
        if (gate.stopped) reason = "stop";
        prompt_total += st.prompt_tokens; completion_total += st.completion_tokens;
        log.add("INFO", "done " + model + " prompt=" + std::to_string(st.prompt_tokens) + " cached=" + std::to_string(st.cached_tokens) + " gen=" + std::to_string(st.completion_tokens) + " think=" + std::to_string(st.think_tokens) + " reason=" + reason + (agent_name().empty() ? "" : " agent=" + agent_name()));
        // ---- final / non-stream replies
        if (an) {
            std::string sr = !calls.empty() ? "tool_use" : gate.stopped ? "stop_sequence" : reason == "length" ? "max_tokens" : "end_turn";
            Json stopseq = sr == "stop_sequence" ? Json(gate.hit) : Json(nullptr);
            Json blocks = Json::array(); int i = 0;
            if (!stream) {
                if (!thinking.empty()) blocks.push_back({{"type", "thinking"}, {"thinking", thinking}, {"signature", ""}});
                if (!content.empty()) blocks.push_back({{"type", "text"}, {"text", content}});
                for (auto& c : calls) blocks.push_back({{"type", "tool_use"}, {"id", "toolu_" + std::to_string(i++) + "_" + id}, {"name", c["name"]}, {"input", c["arguments"]}});
                return json_reply(r, {{"id", id}, {"type", "message"}, {"role", "assistant"}, {"model", model}, {"content", blocks}, {"stop_reason", sr}, {"stop_sequence", stopseq}, {"usage", {{"input_tokens", st.prompt_tokens}, {"output_tokens", st.completion_tokens}}}});
            }
            an_start();
            an_close();
            for (auto& c : calls) {
                an_index++;
                an_event("content_block_start", {{"type", "content_block_start"}, {"index", an_index}, {"content_block", {{"type", "tool_use"}, {"id", "toolu_" + std::to_string(i++) + "_" + id}, {"name", c["name"]}, {"input", Json::object()}}}});
                an_event("content_block_delta", {{"type", "content_block_delta"}, {"index", an_index}, {"delta", {{"type", "input_json_delta"}, {"partial_json", jdump(c["arguments"])}}}});
                an_event("content_block_stop", {{"type", "content_block_stop"}, {"index", an_index}});
            }
            an_event("message_delta", {{"type", "message_delta"}, {"delta", {{"stop_reason", sr}, {"stop_sequence", stopseq}}}, {"usage", {{"input_tokens", st.prompt_tokens}, {"output_tokens", st.completion_tokens}}}});
            an_event("message_stop", {{"type", "message_stop"}});
            r.end_stream();
            return;
        }
        if (oa) {
            Json usage = {{"prompt_tokens", st.prompt_tokens}, {"completion_tokens", st.completion_tokens}, {"total_tokens", st.prompt_tokens + st.completion_tokens}};
            Json tc = Json::array(); int i = 0;
            for (auto& c : calls) tc.push_back({{"index", i}, {"id", "call_" + std::to_string(i++) + "_" + id}, {"type", "function"}, {"function", {{"name", c["name"]}, {"arguments", jdump(c["arguments"])}}}});
            std::string fr = !tc.empty() ? "tool_calls" : reason;
            if (!stream) {
                Json msg = {{"role", "assistant"}, {"content", content}};
                if (!thinking.empty()) msg["reasoning_content"] = thinking;
                if (!tc.empty()) { msg["tool_calls"] = tc; if (content.empty()) msg["content"] = nullptr; }
                Json ch = dialect == "openai-chat" ? Json{{"index", 0}, {"message", msg}, {"finish_reason", fr}} : Json{{"index", 0}, {"text", content}, {"finish_reason", fr}};
                return json_reply(r, {{"id", id}, {"object", dialect == "openai-chat" ? "chat.completion" : "text_completion"}, {"created", (long long)time(nullptr)}, {"model", model}, {"choices", {ch}}, {"usage", usage}});
            }
            if (!tc.empty()) chunk(oa_delta({{"tool_calls", tc}}, nullptr));
            Json last = oa_delta(Json::object(), fr);
            chunk(last);
            if (s.include_usage) { Json u = {{"id", id}, {"object", "chat.completion.chunk"}, {"created", (long long)time(nullptr)}, {"model", model}, {"choices", Json::array()}, {"usage", usage}}; chunk(u); }
            r.write_chunk("data: [DONE]\n\n"); r.end_stream();
            return;
        }
        Json fin = {{"model", model}, {"created_at", rfc3339()}, {"done", true}, {"done_reason", reason}, {"total_duration", ns(mono() - t_start)}, {"load_duration", ns(st.load_s)},
                    {"prompt_eval_count", st.prompt_tokens}, {"prompt_eval_duration", ns(st.prefill_s)}, {"eval_count", st.completion_tokens}, {"eval_duration", ns(st.decode_s)}};
        Json tcs = Json::array();
        for (auto& c : calls) tcs.push_back({{"function", {{"name", c["name"]}, {"arguments", c["arguments"]}}}});
        if (s.chat) {
            if (stream) {
                if (!tcs.empty()) chunk({{"model", model}, {"created_at", rfc3339()}, {"message", {{"role", "assistant"}, {"content", ""}, {"tool_calls", tcs}}}, {"done", false}});
                fin["message"] = {{"role", "assistant"}, {"content", ""}};
                chunk(fin); r.end_stream(); return;
            }
            Json msg = {{"role", "assistant"}, {"content", content}};
            if (!thinking.empty()) msg["thinking"] = thinking;
            if (!tcs.empty()) msg["tool_calls"] = tcs;
            fin["message"] = msg;
            return json_reply(r, fin);
        }
        if (stream) { fin["response"] = ""; chunk(fin); r.end_stream(); return; }
        fin["response"] = content; if (!thinking.empty()) fin["thinking"] = thinking;
        json_reply(r, fin);
    }
    static std::string strip_fences(std::string t) {
        size_t a = t.find("```");
        if (a == std::string::npos) return t;
        size_t nl = t.find('\n', a), b = t.rfind("```");
        if (nl == std::string::npos || b <= nl) return t;
        return t.substr(nl + 1, b - nl - 1);
    }
};
}
