// SPDX-License-Identifier: GPL-3.0-or-later
// Garlic GPU engine adapter (Qwen3.5-9B Q4s/Q3s + MTP speculative decoding). Adapted from the C07 worker's garlic.cpp.
// Engine ownership rules (learned on hardware): one GPU owner thread at a time (the API layer serializes), GPU memory is never
// released inside the process, any uncertain GPU completion fences the engine until the app is relaunched.
#include "garlic_backend.hpp"
#include "nassets.hpp"
#include "nclock.hpp"
#include "nutf8.hpp"
#include <algorithm>
#include <atomic>
#include <chrono>
#include <cmath>
#include <cstdarg>
#include <ctime>
#ifndef __ORBIS__
#include <dirent.h>
#endif
#include <mutex>
#include <thread>
extern "C" {
#include "engine.h"
#include "tokenizer.h"
const char* worker_bundle_root = nullptr;
#ifdef __ORBIS__
size_t sceKernelGetDirectMemorySize();
#endif
void say(const char*, ...) {}   // upstream diagnostics can contain token text: suppressed
}
struct GpuFault : std::runtime_error { using std::runtime_error::runtime_error; };
static const char* g_step = "idle";
static unsigned g_alloc = 0, g_map = 0, g_submit = 0; static bool g_unknown = false;   // GPU resource accounting: a load may only be retried if nothing was mapped/submitted
static void gpu_event(const char* name, int rc) { char b[200]; snprintf(b, sizeof b, "NLN01 stage=gpu_engine event=%s rc=%d rc_hex=0x%08x step=%s\n", name, rc, unsigned(rc), g_step); boot::raw(b, strlen(b)); }
extern "C" {
void worker_gpu_fatal(void) { throw GpuFault("gpu_timeout"); }
void worker_memory_fatal(void) { gpu_event("gpu_arena_exhausted", -1); throw GpuFault("gpu_arena_exhausted"); }
void worker_engine_event(const char* e, int rc) { gpu_event(e, rc); }
void worker_memory_event(const char* e, int rc, uint64_t bytes) {
    if (!rc) {
        if (!strcmp(e, "allocate")) ++g_alloc; else if (!strcmp(e, "map")) ++g_map;
        else if (!strcmp(e, "release")) { if (g_alloc) --g_alloc; else g_unknown = true; }
        else if (!strcmp(e, "unmap")) { if (g_map) --g_map; else g_unknown = true; }
    }
    char b[200]; snprintf(b, sizeof b, "NLN01 stage=gpu_engine event=%s rc=%d size_mib=%llu\n", e, rc, (unsigned long long)(bytes >> 20)); boot::raw(b, strlen(b)); }
void worker_submit_begin(void) { ++g_submit; }
void worker_submit_result(int rc) { if (rc) gpu_event("submit_failed", rc); }
}
namespace {
double now_s() { return nclock::mono_s(); }
constexpr uint32_t TOK_THINK = 248068u, TOK_THINK_END = 248069u;
const char* THINK_CLOSE = "\n\nConsidering the limited time by the user, I have to give the solution based on the thinking directly now.\n";

class Garlic : public NativeBackend {
    GarlicDeps d; NativeConfig& cfg;
    std::vector<ModelInfo> models_cache; double models_at = -1e9;
    std::unique_ptr<nassets::Bundle> bundle;      // the resident bundle
    Engine* e = new Engine{}; Tokenizer tok{}; std::string tokdata;
    std::string resident;                         // API name of the resident model
    std::string engine_bundle_dir;                // bundle dir the engine was initialised with ("" = engine untouched)
    std::mutex mu_;                               // guards resident/fence_reason/models_cache (read by connection threads)
    std::atomic<bool> fenced_{false}; std::string fence_reason; std::atomic<uint32_t> ctx_cached_{0}; double rate = 0; unsigned seq = 1;
    std::vector<uint32_t> fed;                    // tokens already processed by the engine (prefix cache)
    bool cache_ok = false; bool restarting_ = false;
    bool spec_ = false;                           // the resident model has an MTP head (qwen3.5-9b); the 4B decodes one token per engine step
    unsigned long long gen_total = 0; int last_prompt = 0, last_cached = 0;
    void log(const char* lvl, const std::string& m) { if (d.log) d.log(lvl, m); boot::line(std::string(lvl) + " " + m); }
    void gpu(int rc) { if (rc) throw GpuFault("gpu_timeout"); }
    void fence(const std::string& why) { { std::lock_guard<std::mutex> l(mu_); fence_reason = why; } fenced_ = true; cache_ok = false; log("ERROR", "GPU fenced: " + why + " (app relaunch required)"); }
    std::string fence_reason_copy() { std::lock_guard<std::mutex> l(mu_); return fence_reason; }
    std::string state_path() { return cfg.data_dir + "/state.json"; }
    std::string verified_path() { return cfg.data_dir + "/verified.json"; }
    Json read_state() { std::string t; Json j; if (read_file_small(state_path(), t)) j = Json::parse(t, nullptr, false); return j.is_object() ? j : Json::object(); }
    void write_state(const Json& j) { if (!write_file_atomic(state_path(), j.dump(2))) log("WARN", "could not write " + state_path()); }
    Json trusted_all() {
        Json sets = d.trusted.is_array() ? d.trusted : Json::array({d.trusted});
        std::string t;
        if (read_file_small(cfg.data_dir + "/trusted-extra.json", t)) {   // operator-staged (root-owned via FTP) additional pinned bundles
            Json x = Json::parse(t, nullptr, false);
            if (x.is_array()) for (auto& s : x) sets.push_back(s); else if (x.is_object()) sets.push_back(x);
        }
        return sets;
    }
    // Directory enumeration is host-only: the console's dirent ABI (FreeBSD 9) is unverified for this SDK, so on the console bundles
    // are named explicitly (model_dirs entries that are bundles themselves, plus <data_dir>/models.json {"bundles":[...]}).
    std::vector<std::string> list_children(const std::string& dir) {
        std::vector<std::string> out;
#ifndef __ORBIS__
        if (DIR* dp = opendir(dir.c_str())) { while (dirent* de = readdir(dp)) { std::string n = de->d_name; if (n != "." && n != "..") out.push_back(n); } closedir(dp); }
#else
        (void)dir;
#endif
        return out;
    }
    std::vector<std::string> manifest_bundles() {
        std::vector<std::string> out; std::string t;
        if (!read_file_small(cfg.data_dir + "/models.json", t)) return out;
        Json j = Json::parse(t, nullptr, false);
        if (j.is_object() && j.contains("bundles") && j["bundles"].is_array())
            for (auto& b : j["bundles"]) if (b.is_string() && !b.get<std::string>().empty() && b.get<std::string>()[0] == '/' && b.get<std::string>().find("..") == std::string::npos) out.push_back(b);
        return out;
    }
public:
    explicit Garlic(const GarlicDeps& deps) : d(deps), cfg(*deps.cfg) {}
    std::vector<ModelInfo> discover() override {
        { std::lock_guard<std::mutex> l(mu_); if (now_s() - models_at < 5 && !models_cache.empty()) return models_cache; }
        std::vector<ModelInfo> out; std::vector<std::string> dirs;
        // Order is the default-model order: the operator's model_dirs first, then models.json additions (so "first discovered" is deterministic).
        for (auto& m : cfg.model_dirs) {
            dirs.push_back(m);
            for (auto& c : list_children(m)) dirs.push_back(m + "/" + c);   // best effort: enumeration may be unavailable on the console
        }
        for (auto& b : manifest_bundles()) dirs.push_back(b);
        for (auto& dir : dirs) {
            try {
                std::string mt; nassets::FD probe(dir + "/bundle.json"); (void)probe;
                mt = nassets::read_file(dir + "/bundle.json", 65536);
                Json m = Json::parse(mt, nullptr, false);
                auto arch = m.value("architecture", std::string());
                if (!m.is_object() || !nassets::Bundle::qwen35_arch(arch) || !m.contains("files")) continue;
                bool nine = arch == "qwen3.5-9b";
                ModelInfo mi; mi.dir = dir; mi.digest = "sha256:" + nassets::sha_bytes(mt);
                std::string base = dir.substr(dir.find_last_of('/') + 1);
                mi.name = m.value("name", std::string());
                if (mi.name.empty()) mi.name = arch + "-garlic";
                for (auto& o : out) if (o.name == mi.name && o.digest != mi.digest) mi.name += "-" + base;
                bool dup = false; for (auto& o : out) if (o.name == mi.name) dup = true;
                if (dup) continue;
                for (auto it = m["files"].begin(); it != m["files"].end(); ++it) mi.size += it.value().value("bytes", 0ull);
                mi.ctx_max = m.value("context_length", 0u); mi.mtp = nine && m.value("mtp", false); mi.template_name = m.value("template", std::string());
                mi.quantization = nine ? "Q4_S/Q3_S" : "Q4_I"; { std::string sz = arch.substr(8); sz.back() = 'B'; mi.parameter_size = sz; }
                out.push_back(mi);
            } catch (const std::exception&) { /* not a bundle */ }
        }
        { std::lock_guard<std::mutex> l(mu_); models_cache = out; models_at = now_s(); }
        return out;
    }
    std::string loaded() override { std::lock_guard<std::mutex> l(mu_); return resident; }
    uint32_t ctx_limit() override { return ctx_cached_; }
    // ---------------------------------------------------------------- load / unload
    // Structural validation always; the weights are SHA-256'd only if this bundle was never verified (or verification is forced). Caches the result.
    std::unique_ptr<nassets::Bundle> validate_bundle(const ModelInfo& mi) {
        Json ver; { std::string t; if (read_file_small(verified_path(), t)) ver = Json::parse(t, nullptr, false); }
        bool known = ver.is_object() && ver.contains(mi.digest) && ver[mi.digest].value("size", 0ull) == mi.size;
        bool hash = cfg.verify_full_hash || !known;
        log("INFO", std::string("validating bundle ") + mi.name + (hash ? " (full hash)" : " (cached hash)"));
        std::unique_ptr<nassets::Bundle> b(new nassets::Bundle(mi.dir, trusted_all(), hash, true));
        if (hash) { if (!ver.is_object()) ver = Json::object(); ver[mi.digest] = {{"size", mi.size}, {"verified_at", (long long)time(nullptr)}}; write_file_atomic(verified_path(), ver.dump()); }
        return b;
    }
    bool load(const std::string& name, bool* restarting) override {
        if (fenced_) throw NativeError(503, "gpu_fenced", "the GPU is fenced (" + fence_reason_copy() + "); the app must be relaunched");
        if (restarting_) { *restarting = true; return false; }
        if (resident == name) return true;
        ModelInfo* mi = nullptr; auto ms = discover();
        for (auto& m : ms) if (m.name == name) mi = &m;
        if (!mi) throw NativeError(404, "model_not_found", "model '" + name + "' is not installed");
        if (!engine_bundle_dir.empty() && engine_bundle_dir != mi->dir) {
            // The engine keeps its GPU arena for the life of the process: switching bundles needs a fresh process. Validate the target FIRST:
            // a broken bundle must be refused now, not after a relaunch that would leave the app with no usable model.
            try { validate_bundle(*mi); }
            catch (const AssetError& x) { throw NativeError(422, "bundle_invalid", std::string("bundle validation failed (no relaunch performed): ") + x.what()); }
            Json st = read_state(); st["desired_model"] = name; st["pending_swap"] = true; write_state(st);
            std::string why;
            if (!d.relaunch || !d.relaunch(why)) { Json s2 = read_state(); s2.erase("pending_swap"); write_state(s2); throw NativeError(501, "swap_unavailable", "cannot switch models in-process and relaunch failed: " + why); }
            restarting_ = true; *restarting = true; log("INFO", "relaunching to switch to " + name); return false;
        }
        double t0 = now_s();
        try {
            boot::enter("bundle_validation");
            // Full SHA of the 4.6 GB weights only when never verified (or forced): minutes on a console.
            bundle = validate_bundle(*mi);
            boot::enter("engine_load");
            worker_bundle_root = bundle->root.c_str(); eng_margin_mb = cfg.margin_mb;
            if (bundle->root.size() >= 80) throw NativeError(500, "load_failed", "bundle path too long for the engine (max 79 chars)");
            g_alloc = g_map = g_submit = 0; g_unknown = false;
            if (engine_bundle_dir.empty()) engine_bundle_dir = mi->dir;
            g_step = "engine_init";
#ifdef __ORBIS__
            { char b[160]; snprintf(b, sizeof b, "NLN01 stage=gpu_load event=env direct_memory_mib=%llu margin_mib=%d\n", (unsigned long long)(sceKernelGetDirectMemorySize() >> 20), cfg.margin_mb); boot::raw(b, strlen(b)); }
#endif
            gpu(engine_init(e, bundle->root.c_str(), nullptr, nullptr));
            g_step = "tokenizer";
            tokdata = nassets::read_file(bundle->root + "/tokenizer.bin", 32 << 20);
            if (tok_load(&tok, (const uint8_t*)tokdata.data(), tokdata.size())) throw GpuFault("tokenizer_load_failed");
            spec_ = bundle->mtp;
            if (spec_) {
                g_step = "spec_init"; gpu(spec_init(e, bundle->root.c_str())); g_step = "spec_load";
                auto off = (e->next + 255) & ~255ull; if (off >= e->mem_size) throw GpuFault("gpu_arena_exhausted");
                gpu(spec_load(e, (uint64_t)(uintptr_t)(e->mem + off), e->mem_size - off, 0));
                if (!spec_active(e)) throw GpuFault("mtp_inactive");
                g_step = "ready"; e->next = e->mem_size;   // nothing may allocate over the MTP head
            } else g_step = "ready";                        // no MTP head: nothing is reserved past the model, the KV/activation arena stays as the engine laid it out
        } catch (const AssetError& x) {
            bundle.reset();
            throw NativeError(422, "bundle_invalid", std::string("bundle validation failed: ") + x.what());
        } catch (const GpuFault& x) {
            // Same rule as the C07 worker: only a failure before anything was reserved/mapped/submitted is safely retryable.
            bool retry = !strcmp(g_step, "engine_init") && !e->mem && !g_alloc && !g_map && !g_submit && !g_unknown;
            if (retry) {
                free(e->t); free(e->layer_base); *e = Engine{}; engine_bundle_dir.clear(); bundle.reset();
                log("WARN", std::string("engine load failed before any GPU reservation (") + x.what() + "); retry is safe");
                throw NativeError(500, "load_failed", std::string("engine load failed: ") + x.what() + " (nothing was reserved; retry is safe)");
            }
            fence(x.what()); throw NativeError(500, "load_failed", std::string("engine load failed: ") + x.what() + "; app relaunch required");
        }
        catch (const NativeError&) { throw; }
        catch (const std::exception& x) { fence(x.what()); throw NativeError(500, "load_failed", std::string("engine load failed: ") + x.what()); }
        { std::lock_guard<std::mutex> l(mu_); resident = mi->name; models_at = -1e9; } ctx_cached_ = e->ctx_limit; mi->ctx_max = std::min(mi->ctx_max ? mi->ctx_max : e->ctx_limit, e->ctx_limit);
        fed.clear(); cache_ok = false;
        Json st = read_state(); st["desired_model"] = resident; st["last_good_model"] = resident; st.erase("pending_swap"); st["restarts_since_ok"] = 0; write_state(st);
        log("INFO", "model ready: " + resident + " ctx_limit=" + std::to_string(e->ctx_limit) + " gpu_reserved_mib=" + std::to_string(e->mem_size >> 20) + " load_s=" + std::to_string(now_s() - t0));
        return true;
    }
    void unload(bool release, bool* restarting) override {
        cache_ok = false; fed.clear();
        if (!resident.empty()) engine_reset(e);
        if (!release) { log("INFO", "conversation state cleared (GPU weights stay resident; use release to free the GPU)"); return; }
        Json st = read_state(); st["desired_model"] = ""; write_state(st);
        std::string why;
        if (!d.relaunch || !d.relaunch(why)) throw NativeError(501, "release_unavailable", "cannot relaunch to release the GPU: " + why);
        restarting_ = true; *restarting = true;
    }
    int count_tokens(const std::string& text) override {
        if (resident.empty()) return 0;
        std::vector<uint32_t> p(text.size() + 1); int n = tok_encode(&tok, text.c_str(), p.data(), (int)p.size()); return n < 0 ? 0 : n;
    }
    // ---------------------------------------------------------------- prompt
    void text(std::vector<uint32_t>& ids, const std::string& s) {
        if (s.find('\0') != std::string::npos) throw NativeError(400, "bad_request", "NUL bytes are not allowed in prompts");
        if (s.size() > (8u << 20)) throw NativeError(400, "bad_request", "single message too large");
        std::vector<uint32_t> part(s.size() + 1); int n = tok_encode(&tok, s.c_str(), part.data(), (int)part.size());
        if (n < 0 || size_t(n) > s.size()) throw NativeError(500, "tokenizer_error", "tokenizer overflow");
        ids.insert(ids.end(), part.begin(), part.begin() + n);
    }
    std::vector<uint32_t> encode_prompt(const std::vector<Turn>* chat, const std::string* raw, bool think) {
        std::vector<uint32_t> ids;
        if (raw) {   // map the control markers to ids; everything else is plain text (user text can never forge a marker elsewhere)
            struct M { const char* s; uint32_t id; } ms[] = {{"<|im_start|>", e->im_start}, {"<|im_end|>", e->im_end}, {"<think>", TOK_THINK}, {"</think>", TOK_THINK_END}};
            size_t p = 0;
            while (p < raw->size()) {
                size_t best = std::string::npos; int bi = -1;
                for (int i = 0; i < 4; i++) { size_t q = raw->find(ms[i].s, p); if (q < best) { best = q; bi = i; } }
                if (bi < 0) { text(ids, raw->substr(p)); break; }
                text(ids, raw->substr(p, best - p)); ids.push_back(ms[bi].id); p = best + strlen(ms[bi].s);
            }
            return ids;
        }
        for (auto& m : *chat) {
            if (m.role != "system" && m.role != "user" && m.role != "assistant") throw NativeError(400, "bad_request", "unsupported role " + m.role);
            ids.push_back(e->im_start); text(ids, m.role + "\n"); text(ids, m.content); ids.push_back(e->im_end); text(ids, "\n");
        }
        ids.push_back(e->im_start); text(ids, "assistant\n");
        ids.push_back(TOK_THINK);
        if (think) text(ids, "\n");   // open reasoning: the model thinks until </think>
        else { text(ids, "\n\n"); ids.push_back(TOK_THINK_END); text(ids, "\n\n"); }
        return ids;
    }
    // ---------------------------------------------------------------- generation
    GenStats generate(const std::vector<Turn>* chat, const std::string* raw, const GenOpts& o, const std::function<bool()>& cancelled, const EmitFn& emit) override {
        if (fenced_) throw NativeError(503, "gpu_fenced", "the GPU is fenced (" + fence_reason_copy() + "); the app must be relaunched");
        if (resident.empty()) throw NativeError(409, "no_model", "no model is loaded");
        GenStats st; double t0 = now_s();
        bool think = o.think && !raw;
        auto ids = encode_prompt(chat, raw, think);
        uint32_t lim = std::min<uint32_t>(o.num_ctx ? o.num_ctx : e->ctx_limit, e->ctx_limit);
        if (ids.size() + 2 > lim) throw NativeError(400, "context_overflow", "prompt is " + std::to_string(ids.size()) + " tokens but the context holds " + std::to_string(lim) + " (limit of this engine/model: " + std::to_string(e->ctx_limit) + "); shorten the conversation or raise num_ctx");
        int room = int(lim - ids.size() - 1);
        int max = std::min(o.max_tokens > 0 ? o.max_tokens : room, room);
        st.prompt_tokens = (int)ids.size();
        float temperature = std::max(0.f, std::min(2.f, o.temperature));
        uint32_t seed = o.seed ? o.seed : (uint32_t)std::chrono::steady_clock::now().time_since_epoch().count() | 1u; seed += seq++ * 7919u;
        try {
            // prefix reuse: only when the previous sequence is a strict prefix (the recurrent state cannot be rewound)
            size_t pos = 0;
            if (cache_ok && !fed.empty() && ids.size() > fed.size() && std::equal(fed.begin(), fed.end(), ids.begin())) pos = fed.size();
            else { engine_reset(e); fed.clear(); }
            cache_ok = false; st.cached_tokens = (int)pos;
            uint32_t next = 0;
            while (pos < ids.size()) {   // 32-token prefill batches bound cancellation latency to one engine batch
                if (cancelled()) { st.done_reason = "cancelled"; fed.clear(); return st; }
                int n = (int)std::min<size_t>(32, ids.size() - pos);
                gpu(engine_run(e, ids.data() + pos, n, (int)pos, temperature, seed++, &next));
                for (int i = 0; i < n; i++) fed.push_back(ids[pos + i]);
                pos += n;
            }
            double t_pre = now_s(); st.prefill_s = t_pre - t0;
            if (spec_) spec_stats_reset();
            bool in_think = think, answer_started = !think, stopped = false, client_stop = false;
            Utf8 u8; int generated = 0, think_n = 0;
            Piece kind = in_think ? Piece::Think : Piece::Answer;
            auto flush8 = [&] { u8.finish([&](const std::string& s) { if (!emit(s, kind)) client_stop = true; }); };
            auto token = [&](uint32_t t) {
                generated++;
                if (t == e->eos || t == e->eos2) { stopped = true; return; }
                if (t == TOK_THINK_END && in_think) { flush8(); in_think = false; kind = Piece::Answer; return; }
                if (in_think) think_n++;
                const uint8_t* bytes = nullptr; int n = tok_piece(&tok, t, &bytes);
                if (n <= 0) return;
                u8.piece(bytes, (size_t)n, [&](const std::string& s0) {
                    std::string s = s0;
                    if (kind == Piece::Answer && !answer_started) {   // drop the blank lines that follow </think>
                        size_t k = s.find_first_not_of(" \n\t\r"); if (k == std::string::npos) return; s.erase(0, k); answer_started = true;
                    }
                    if (!emit(s, kind)) client_stop = true;
                });
            };
            while (generated < max && !client_stop) {
                if (cancelled()) { st.done_reason = "cancelled"; fed.clear(); return st; }
                // reasoning budget: feed Qwen's closing sentence + </think> and continue with the answer
                if (in_think && think_n >= o.think_budget) {
                    std::vector<uint32_t> close; text(close, THINK_CLOSE); close.push_back(TOK_THINK_END); text(close, "\n\n");
                    if (pos + close.size() + 2 > lim) break;
                    // `next` (not yet fed) was drawn in thinking mode; it is discarded in favour of the forced closing
                    size_t cp = 0;
                    while (cp < close.size()) { int n = (int)std::min<size_t>(32, close.size() - cp); gpu(engine_run(e, close.data() + cp, n, (int)pos, temperature, seed++, &next)); for (int i = 0; i < n; i++) fed.push_back(close[cp + i]); pos += n; cp += n; }
                    flush8(); in_think = false; kind = Piece::Answer; log("INFO", "reasoning budget reached; forced </think>");
                    st.think_tokens = think_n; continue;
                }
                token(next);
                if (stopped || client_stop || generated >= max) break;
                uint32_t current = next, extra[8]; int ne = 0;
                if (spec_) gpu(engine_spec_step(e, current, (int)pos, temperature, seed++, std::min(max - generated, 8), extra, &ne, &next));
                else gpu(engine_run(e, &current, 1, (int)pos, temperature, seed++, &next));   // one token per step (identical distribution, no drafts)
                fed.push_back(current); pos++;
                // every accepted draft token is already inside the engine state: it must enter `fed` whether or not it is emitted
                for (int i = 0; i < ne; i++) { if (!stopped && !client_stop && generated < max) token(extra[i]); fed.push_back(extra[i]); pos++; }
                if (stopped) break;
                if (pos + 2 >= lim) break;   // context full
            }
            flush8();
            double t_end = now_s(); st.decode_s = t_end - t_pre; st.completion_tokens = generated; st.think_tokens = think_n;
            if (generated > 1 && st.decode_s > 0) rate = (generated - 1) / st.decode_s;
            st.done_reason = stopped ? "stop" : client_stop ? "stop" : "length";
            cache_ok = true;   // `fed` mirrors exactly what the engine processed
            gen_total++; last_prompt = st.prompt_tokens; last_cached = st.cached_tokens;
            Json s = read_state(); if (s.value("restarts_since_ok", 0) != 0) { s["restarts_since_ok"] = 0; write_state(s); }
            return st;
        } catch (const GpuFault& x) { fence(x.what()); throw NativeError(500, "gpu_fault", std::string("GPU fault during generation: ") + x.what() + "; the app will need a relaunch"); }
    }
    Json telemetry() override {
        Json t = {{"engine", "garlic-gfx701"}, {"gpu_fenced", fenced_.load()}, {"restarting", restarting_}, {"generations", gen_total}, {"last_prompt_tokens", last_prompt}, {"last_cached_tokens", last_cached}};
        if (fenced_) t["fence_reason"] = fence_reason_copy();
        if (!loaded().empty()) { t["gpu_reserved_bytes"] = e->mem_size; t["engine_ctx_limit"] = e->ctx_limit; t["mtp"] = spec_; }
        if (rate > 0 && std::isfinite(rate)) t["tokens_per_second"] = rate;
        return t;
    }
};
}
std::unique_ptr<NativeBackend> make_garlic_backend(const GarlicDeps& d) { return std::make_unique<Garlic>(d); }
