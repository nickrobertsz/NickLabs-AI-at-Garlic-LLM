// SPDX-License-Identifier: GPL-3.0-or-later
// Abstract inference backend for the NickLabs native app. The Garlic GPU engine implements it on the console;
// a deterministic fixture implements it for host tests. All calls are serialized by the API layer (one GPU owner).
#pragma once
#include "json.hpp"
#include <functional>
#include <stdexcept>
#include <string>
#include <vector>
using Json = nlohmann::json;
struct NativeError : std::runtime_error {
    int http; std::string code;
    NativeError(int h, std::string c, const std::string& m) : std::runtime_error(m), http(h), code(std::move(c)) {}
};
struct ModelInfo {
    std::string name;        // API name, e.g. "qwen3.5-9b-garlic"
    std::string dir;         // bundle directory
    std::string digest;      // sha256 of bundle.json
    uint64_t size = 0;       // total bytes of bundle files
    uint32_t ctx_max = 0;    // positions the bundle supports (model.idx max_seq)
    std::string family = "qwen35", parameter_size = "9B", quantization = "Q4_S/Q3_S", template_name;
    bool mtp = false, thinking = true, tools = true;
    std::string modified_at;
};
struct Turn { std::string role, content; };
enum class Piece { Answer, Think };
struct GenOpts {
    float temperature = 0.7f;
    uint32_t seed = 0;
    int max_tokens = 1024;       // hard cap on generated tokens (<=0 resolved by API layer)
    uint32_t num_ctx = 0;        // requested context (0 = configured default)
    bool think = false;
    int think_budget = 1024;     // max reasoning tokens before the closing sentence is forced
};
struct GenStats {
    int prompt_tokens = 0, cached_tokens = 0, completion_tokens = 0, think_tokens = 0;
    double load_s = 0, prefill_s = 0, decode_s = 0;
    std::string done_reason = "stop";   // stop | length | cancelled | context
};
// emit returns false to stop generation (client gone / stop sequence hit).
using EmitFn = std::function<bool(const std::string& text, Piece kind)>;
struct NativeBackend {
    virtual ~NativeBackend() = default;
    virtual std::vector<ModelInfo> discover() = 0;                          // never throws; empty on failure
    virtual std::string loaded() = 0;                                       // "" when no model resident
    virtual uint32_t ctx_limit() = 0;                                       // engine ceiling of the resident model (0 = none)
    // Make `name` resident. Returns true if resident now. Returns false (without throwing) when the engine cannot swap
    // in-process and a relaunch was arranged: *restarting is set. Throws NativeError on failure.
    virtual bool load(const std::string& name, bool* restarting) = 0;
    virtual void unload(bool release, bool* restarting) = 0;                 // drop conversation state; release => free GPU by relaunch
    virtual GenStats generate(const std::vector<Turn>* chat, const std::string* raw, const GenOpts& o,
                              const std::function<bool()>& cancelled, const EmitFn& emit) = 0;
    virtual int count_tokens(const std::string& text) = 0;
    virtual Json telemetry() = 0;
};
