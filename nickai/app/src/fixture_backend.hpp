// SPDX-License-Identifier: GPL-3.0-or-later
// Deterministic host-test backend. Not part of the PS4 package.
#pragma once
#include "nbackend.hpp"
#include <chrono>
#include <sstream>
#include <thread>
struct FixtureBackend : NativeBackend {
    std::string cur; bool fail_next = false; int delay_ms = 2; uint32_t limit = 8192;
    std::vector<ModelInfo> discover() override {
        ModelInfo a; a.name = "fixture-a"; a.dir = "/fixture/a"; a.digest = "sha256:" + std::string(64, 'a'); a.size = 1000; a.ctx_max = limit; a.mtp = true; a.template_name = "chatml";
        ModelInfo b = a; b.name = "fixture-b"; b.dir = "/fixture/b"; b.digest = "sha256:" + std::string(64, 'b');
        return {a, b};
    }
    std::string loaded() override { return cur; }
    uint32_t ctx_limit() override { return cur.empty() ? 0 : limit; }
    bool load(const std::string& n, bool*) override { if (fail_next) { fail_next = false; throw NativeError(500, "load_failed", "fixture load failure"); } cur = n; return true; }
    void unload(bool, bool*) override { cur.clear(); }
    int count_tokens(const std::string& t) override { std::istringstream is(t); std::string w; int n = 0; while (is >> w) n++; return n; }
    GenStats generate(const std::vector<Turn>* chat, const std::string* raw, const GenOpts& o, const std::function<bool()>& cancelled, const EmitFn& emit) override {
        GenStats st; std::string prompt, last, sys;
        if (raw) prompt = last = *raw; else for (auto& t : *chat) { prompt += t.role + ":" + t.content + "\n"; if (t.role == "user") last = t.content; if (t.role == "system") sys = t.content; }
        st.prompt_tokens = count_tokens(prompt);
        if ((uint32_t)st.prompt_tokens + 1 > o.num_ctx) throw NativeError(400, "context_overflow", "prompt of " + std::to_string(st.prompt_tokens) + " tokens does not fit the " + std::to_string(o.num_ctx) + "-token context");
        std::string reply;
        if (last.find("<tool_response>") != std::string::npos) reply = "The tool said: done.";
        else if (sys.find("<tools>") != std::string::npos && last.find("weather") != std::string::npos)
            reply = "Let me check.\n<tool_call>\n<function=get_weather>\n<parameter=city>\nParis\n</parameter>\n</function>\n</tool_call>";
        else if (last.find("JSON") != std::string::npos) reply = "```json\n{\"ok\": true}\n```";
        else if (last.find("long") != std::string::npos) { for (int i = 0; i < 400; i++) reply += "word" + std::to_string(i) + " "; }
        else reply = "Echo (T=" + std::to_string(o.temperature).substr(0, 4) + ", ctx=" + std::to_string(o.num_ctx) + "): " + last;
        auto pieces = [](const std::string& s) { std::vector<std::string> v; size_t i = 0; while (i < s.size()) { size_t j = s.find(' ', i); if (j == std::string::npos) j = s.size() - 1; v.push_back(s.substr(i, j - i + 1)); i = j + 1; } return v; };
        if (o.think) for (auto& p : pieces("Reasoning about it step by step. ")) { if (cancelled()) { st.done_reason = "cancelled"; return st; } st.think_tokens++; if (!emit(p, Piece::Think)) { st.done_reason = "stop"; return st; } }
        for (auto& p : pieces(reply)) {
            if (cancelled()) { st.done_reason = "cancelled"; return st; }
            if (st.completion_tokens >= o.max_tokens) { st.done_reason = "length"; return st; }
            if (delay_ms) std::this_thread::sleep_for(std::chrono::milliseconds(delay_ms));
            st.completion_tokens++;
            if (!emit(p, Piece::Answer)) return st;
        }
        st.decode_s = 0.01; st.prefill_s = 0.001;
        return st;
    }
    Json telemetry() override { return {{"fixture", true}}; }
};
