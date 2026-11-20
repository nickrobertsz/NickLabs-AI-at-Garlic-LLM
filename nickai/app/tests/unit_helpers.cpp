// Unit tests for the pure helpers: Utf8 reassembly (nutf8.hpp) and the stop/tool TextGate (napi.hpp).
// Inputs for the Utf8 table come from the Fat PS4 worker; its expected outputs were invalid hex, so expectations here are derived from the rules by Claude.
// TextGate cases follow the Slim PS4 worker's categories with corrected expectations.
#include "napi.hpp"
#include "nutf8.hpp"
#include <cassert>
#include <iostream>
static std::string unhex(const std::string& h) { std::string o; for (size_t i = 0; i + 1 < h.size(); i += 2) o += char(std::stoi(h.substr(i, 2), nullptr, 16)); return o; }
static std::string hexs(const std::string& s) { static const char* d = "0123456789abcdef"; std::string o; for (unsigned char c : s) { o += d[c >> 4]; o += d[c & 15]; } return o; }
int fails = 0;
#define EXPECT(cond, msg) do { if (!(cond)) { std::cout << "FAIL " << msg << "\n"; fails++; } } while (0)
static std::string utf8(const std::vector<std::string>& chunks_hex) {
    Utf8 u; std::string out;
    auto emit = [&](const std::string& s) { out += s; };
    for (auto& h : chunks_hex) { std::string b = unhex(h); u.piece((const uint8_t*)b.data(), b.size(), emit); }
    u.finish(emit); return out;
}
struct G { std::string released, all, hit; bool stopped; };
static G gate(std::vector<std::string> stops, bool tools, const std::vector<std::string>& pieces) {
    napi::TextGate g(stops, tools); G r;
    for (auto& p : pieces) r.released += g.push(p);
    r.released += g.finish(); r.all = g.all; r.hit = g.hit; r.stopped = g.stopped; return r;
}
int main() {
    const std::string FFFD = "efbfbd";
    struct U { const char* name; std::vector<std::string> in; std::string want; } us[] = {
        {"ascii", {"41"}, "41"}, {"2byte split", {"c3", "a9"}, "c3a9"}, {"3byte split 1+1+1", {"e2", "82", "ac"}, "e282ac"}, {"4byte split 2+2", {"f09f", "9880"}, "f09f9880"},
        {"stray continuation", {"41", "80", "42"}, "41" + FFFD + "42"}, {"invalid lead ff", {"ff"}, FFFD}, {"truncated at end", {"e282"}, FFFD},
        {"overlong c0 af", {"c0", "af"}, FFFD + FFFD}, {"surrogate ed a0 80", {"ed", "a0", "80"}, FFFD + FFFD + FFFD},
        {"lead then ASCII at end (was: dropped the A)", {"e2", "41"}, FFFD + "41"}, {"lead then ASCII, one chunk", {"e241"}, FFFD + "41"},
        {"valid prefix then ASCII after completion", {"e2", "82", "ac", "41"}, "e282ac41"}, {"f4 90 overlong range", {"f4", "90", "80", "80"}, FFFD + FFFD + FFFD + FFFD},
        {"e0 80 overlong", {"e0", "80", "80"}, FFFD + FFFD + FFFD}, {"empty", {}, ""}, {"byte-at-a-time emoji", {"f0", "9f", "98", "80", "41"}, "f09f988041"},
        {"two truncated leads", {"e2", "e2", "82", "ac"}, FFFD + "e282ac"}};
    for (auto& c : us) { auto got = hexs(utf8(c.in)); EXPECT(got == c.want, std::string("utf8 ") + c.name + ": got " + got + " want " + c.want); }
    struct T { const char* name; std::vector<std::string> stops; bool tools; std::vector<std::string> pieces; G want; } ts[] = {
        {"plain", {}, false, {"Hello world", " is nice."}, {"Hello world is nice.", "Hello world is nice.", "", false}},
        {"stop split across pieces", {"END"}, false, {"Hello", " E", "ND", " later"}, {"Hello ", "Hello ", "END", true}},
        {"stop at very start", {"END"}, false, {"END more"}, {"", "", "END", true}},
        {"earliest stop wins (later-listed occurs first)", {"STOP", "END"}, false, {"The END then STOP"}, {"The ", "The ", "END", true}},
        {"partial stop prefix at end released at finish", {"STOP"}, false, {"abc ST"}, {"abc ST", "abc ST", "", false}},
        {"tools: text before call", {}, true, {"Sure <tool_call>\n<function=x>"}, {"Sure ", "Sure <tool_call>\n<function=x>", "", false}},
        {"tools: tag split across pieces", {}, true, {"Hi <tool", "_call>x"}, {"Hi ", "Hi <tool_call>x", "", false}},
        {"tools: 'a < b' is not a call", {}, true, {"a", " <", " b"}, {"a < b", "a < b", "", false}},
        {"stop after text already released", {"END"}, false, {"Hello", " world", "END"}, {"Hello world", "Hello world", "END", true}},
        {"multibyte next to stop", {"END"}, false, {"Hello w\xc3\xa9", "END"}, {"Hello w\xc3\xa9", "Hello w\xc3\xa9", "END", true}},
        {"multibyte split by holdback boundary", {"STOP"}, false, {"caf\xc3", "\xa9 STOP"}, {"caf\xc3\xa9 ", "caf\xc3\xa9 ", "STOP", true}},
        {"pieces after stop ignored", {"X"}, false, {"aXb", "c"}, {"a", "a", "X", true}},
        {"empty stop entries ignored", {""}, false, {"abc"}, {"abc", "abc", "", false}}};
    for (auto& c : ts) {
        auto g = gate(c.stops, c.tools, c.pieces);
        EXPECT(g.released == c.want.released && g.all == c.want.all && g.hit == c.want.hit && g.stopped == c.want.stopped,
               std::string("gate ") + c.name + ": released='" + g.released + "' all='" + g.all + "' hit='" + g.hit + "' stopped=" + (g.stopped ? "1" : "0"));
    }
    // invariant: released text never contains the stop text and every released chunk is whole UTF-8
    napi::TextGate g({"zz"}, false); std::string all;
    for (const char* p : {"\xe2\x82", "\xac z", "z q"}) { std::string r = g.push(p); for (size_t i = 0; i < r.size(); i++) (void)i; all += r; }
    all += g.finish(); EXPECT(all == "\xe2\x82\xac ", "invariant: got " + hexs(all));
    std::cout << (fails ? "UNIT HELPERS FAILED\n" : "PASS unit helpers (utf8 17 cases, gate 14 cases)\n"); return fails ? 1 : 0;
}
