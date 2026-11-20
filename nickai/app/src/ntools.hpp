// SPDX-License-Identifier: GPL-3.0-or-later
// Qwen3.5 tool calling: prompt block, call rendering, tool-result folding, and call parsing (port of gateway/ollama.py semantics).
#pragma once
#include "json.hpp"
#include <stdexcept>
#include <string>
using Json = nlohmann::json;
namespace ntools {
// Python json.dumps(ensure_ascii=False) style (", " and ": " separators), which is the format Qwen's template shows the model.
inline std::string dumpu(const Json& j) {
    if (j.is_object()) {
        std::string o = "{"; bool f = true;
        for (auto it = j.begin(); it != j.end(); ++it) { if (!f) o += ", "; f = false; o += Json(it.key()).dump(-1, ' ', false, Json::error_handler_t::replace) + ": " + dumpu(*it); }
        return o + "}";
    }
    if (j.is_array()) {
        std::string o = "["; bool f = true;
        for (auto& e : j) { if (!f) o += ", "; f = false; o += dumpu(e); }
        return o + "]";
    }
    return j.dump(-1, ' ', false, Json::error_handler_t::replace);
}
inline std::string tool_system_block(const Json& tools) {
    std::string lines;
    for (auto& t : tools) {
        if (!lines.empty()) lines += "\n";
        lines += "{\"type\": \"function\", \"function\": " + dumpu(t) + "}";
    }
    return "# Tools\n\nYou have access to the following functions:\n\n<tools>\n" + lines + "\n</tools>\n\n"
           "If you choose to call a function ONLY reply in the following format with NO suffix:\n\n"
           "<tool_call>\n<function=example_function_name>\n<parameter=example_parameter_1>\nvalue_1\n</parameter>\n"
           "<parameter=example_parameter_2>\nThis is the value for the second parameter\nthat can span\nmultiple lines\n</parameter>\n"
           "</function>\n</tool_call>\n\n<IMPORTANT>\nReminder:\n"
           "- Function calls MUST follow the specified format: an inner <function=...></function> block must be nested within <tool_call></tool_call> XML tags\n"
           "- Required parameters MUST be specified\n"
           "- You may call several functions by emitting several <tool_call> blocks one after another\n"
           "- You may provide optional reasoning for your function call in natural language BEFORE the function call, but NOT after\n"
           "- If there is no function call available, answer the question like normal with your current knowledge and do not tell the user about function calls\n"
           "</IMPORTANT>";
}
inline std::string render_call(const std::string& name, const Json& args) {
    std::string o = "<tool_call>\n<function=" + name + ">";
    if (args.is_object())
        for (auto it = args.begin(); it != args.end(); ++it)
            o += "\n<parameter=" + it.key() + ">\n" + (it->is_string() ? it->get<std::string>() : dumpu(*it)) + "\n</parameter>";
    return o + "\n</function>\n</tool_call>";
}
// messages: [{role, content(string), tool_calls?:[{name,arguments(object)}]}] -> [{role, content}] with tool results folded
// into user turns (<tool_response>) and the tool block prepended to the system prompt.
inline Json messages_to_chat(const Json& messages, const Json& tools) {
    std::string sys;
    auto addsys = [&](const std::string& s) { if (!s.empty()) sys += (sys.empty() ? "" : "\n\n") + s; };
    if (tools.is_array() && !tools.empty()) addsys(tool_system_block(tools));
    for (auto& m : messages)
        if (m.value("role", "") == "system" && m.value("content", Json("")).is_string()) addsys(m.value("content", std::string()));
    Json out = Json::array();
    if (!sys.empty()) out.push_back({{"role", "system"}, {"content", sys}});
    std::string pending;
    auto flush = [&] { if (!pending.empty()) { out.push_back({{"role", "user"}, {"content", pending}}); pending.clear(); } };
    bool any = false;
    for (auto& m : messages) {
        std::string role = m.value("role", "");
        std::string content = m.contains("content") && m["content"].is_string() ? m["content"].get<std::string>() : std::string();
        if (role == "system") continue;
        if (role == "tool") { pending += (pending.empty() ? "" : "\n") + std::string("<tool_response>\n") + content + "\n</tool_response>"; any = true; continue; }
        flush();
        if (role != "user" && role != "assistant") throw std::invalid_argument("unsupported role: " + role);
        if (role == "assistant" && m.contains("tool_calls") && m["tool_calls"].is_array() && !m["tool_calls"].empty()) {
            std::string blocks;
            for (auto& c : m["tool_calls"]) blocks += (blocks.empty() ? "" : "\n") + render_call(c.value("name", ""), c.value("arguments", Json::object()));
            bool nonblank = content.find_first_not_of(" \t\r\n") != std::string::npos;
            while (!content.empty() && isspace((unsigned char)content.back())) content.pop_back();
            content = (nonblank ? content + "\n\n" : "") + blocks;
        }
        out.push_back({{"role", role}, {"content", content}});
        any = true;
    }
    flush();
    if (!any) throw std::invalid_argument("At least one non-system message is required");
    return out;
}
struct Parsed { std::string content; Json calls = Json::array(); };
inline bool maybe_partial_call(const std::string& t) {
    static const std::string tag = "<tool_call>";
    if (t.find(tag) != std::string::npos) return true;
    for (size_t k = std::min(tag.size() - 1, t.size()); k > 0; k--) if (t.compare(t.size() - k, k, tag, 0, k) == 0) return true;
    return false;
}
namespace detail {
inline std::string trim(const std::string& s) {
    size_t a = 0, b = s.size();
    while (a < b && isspace((unsigned char)s[a])) a++;
    while (b > a && isspace((unsigned char)s[b - 1])) b--;
    return s.substr(a, b - a);
}
inline Json coerce(const std::string& v, const Json& schema) {
    if (schema.is_object() && schema.contains("type")) {
        const Json& k = schema["type"];
        if (k.is_string() && k == "string") return v;
        if (k.is_array() && k.size() == 1 && k[0] == "string") return v;
    }
    Json j = Json::parse(v, nullptr, false);
    return j.is_discarded() ? Json(v) : j;
}
inline const Json* find_tool(const Json& tools, const std::string& name) {
    if (!tools.is_array()) return nullptr;
    for (auto& t : tools) if (t.is_object() && t.contains("name") && t["name"] == name) return &t;
    return nullptr;
}
// Parses "<function=NAME>...</function>" at the start of s; returns false when s does not start with it.
inline bool parse_function(const std::string& s, const Json& tools, Json& call) {
    static const std::string ft = "<function=";
    if (s.compare(0, ft.size(), ft) != 0) return false;
    size_t ne = s.find('>', ft.size());
    if (ne == std::string::npos) return false;
    std::string name = s.substr(ft.size(), ne - ft.size());
    size_t fe = s.find("</function>", ne);
    if (fe == std::string::npos) return false;   // truncated call (e.g. max tokens): never execute a partial call
    std::string body = s.substr(ne + 1, (fe == std::string::npos ? s.size() : fe) - ne - 1);
    const Json* tool = find_tool(tools, name);
    if (!tool) return false;
    Json props = tool->contains("parameters") && (*tool)["parameters"].is_object() ? (*tool)["parameters"].value("properties", Json::object()) : Json::object();
    Json args = Json::object();
    size_t p = 0;
    while ((p = body.find("<parameter=", p)) != std::string::npos) {
        size_t ke = body.find('>', p + 11);
        if (ke == std::string::npos) break;
        std::string key = body.substr(p + 11, ke - p - 11);
        size_t ve = body.find("</parameter>", ke);
        if (ve == std::string::npos) break;
        std::string val = body.substr(ke + 1, ve - ke - 1);
        if (!val.empty() && val.front() == '\n') val.erase(0, 1);
        if (!val.empty() && val.back() == '\n') val.pop_back();
        args[key] = coerce(val, props.is_object() && props.contains(key) ? props[key] : Json());
        p = ve + 12;
    }
    call = {{"name", name}, {"arguments", args}};
    return true;
}
}
inline Parsed parse_calls(const std::string& text, const Json& tools) {
    using namespace detail;
    Parsed r;
    std::vector<std::pair<size_t, size_t>> spans;
    auto accept_obj = [&](const Json& item, size_t a, size_t b) {
        if (!item.is_object() || !item.contains("name") || !item["name"].is_string()) return;
        Json args = item.contains("arguments") ? item["arguments"] : item.value("parameters", Json());
        if (args.is_string()) { args = Json::parse(args.get<std::string>(), nullptr, false); if (args.is_discarded()) return; }
        if (!args.is_object() || !find_tool(tools, item["name"].get<std::string>())) return;
        r.calls.push_back({{"name", item["name"]}, {"arguments", args}});
        spans.emplace_back(a, b);
    };
    static const std::string open = "<tool_call>", close = "</tool_call>";
    size_t p = 0;
    while ((p = text.find(open, p)) != std::string::npos) {
        size_t e = text.find(close, p + open.size());
        size_t end = e == std::string::npos ? text.size() : e + close.size();
        std::string inner = trim(text.substr(p + open.size(), (e == std::string::npos ? text.size() : e) - p - open.size()));
        Json call;
        if (parse_function(inner, tools, call)) { r.calls.push_back(call); spans.emplace_back(p, end); }
        else {
            Json j = Json::parse(inner, nullptr, false);
            if (!j.is_discarded()) { if (j.is_array()) for (auto& it : j) accept_obj(it, p, end); else accept_obj(j, p, end); }
        }
        p = end;
    }
    if (r.calls.empty()) {
        p = 0;
        while ((p = text.find("<function=", p)) != std::string::npos) {
            size_t e = text.find("</function>", p);
            if (e == std::string::npos) break;
            Json call;
            if (parse_function(text.substr(p, e + 11 - p), tools, call)) { r.calls.push_back(call); spans.emplace_back(p, e + 11); }
            p = e + 11;
        }
    }
    size_t last = 0;
    for (auto& s : spans) { if (s.first >= last) r.content += text.substr(last, s.first - last); last = std::max(last, s.second); }
    r.content += text.substr(last);
    r.content = trim(r.content);
    return r;
}
}
