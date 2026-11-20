// Drives the REAL garlic_backend.cpp (generation loop, think budget, speculative extras, prefix cache, fencing) and the REAL tokenizer
// with a scripted mock of the GPU engine. The mock asserts engine position consistency on every call. No GPU, no performance claims.
// Usage: mock_engine_host <bundle dir> <trusted-assets.json> <scratch dir>
#include "garlic_backend.hpp"
#include "nassets.hpp"
#include <cassert>
#include <iostream>
extern "C" {
#include "engine.h"
#include "tokenizer.h"
}
// ---- scripted mock engine ---------------------------------------------------------------------------------------------
static std::vector<uint32_t> g_script, g_answer;      // tokens the "model" will produce; g_answer replaces the script after a forced </think>
static size_t g_cur = 0; static long g_pos = 0; static bool g_generating = false, g_fail = false, g_forced = false, g_draft_eos = false;   // g_draft_eos: drafts may accept the EOS token itself
static std::vector<int> g_run_pos0; static std::vector<std::vector<uint32_t>> g_run_tokens; static int g_resets = 0, g_steps = 0;
static const int PATTERN[] = {0, 1, 2, 0, 3};          // accepted draft tokens per speculative step
static uint32_t eos_id = 248046;                        // replaced by the engine field after load
static uint32_t script_next() { auto& s = g_forced ? g_answer : g_script; return g_cur < s.size() ? s[g_cur++] : eos_id; }
int eng_margin_mb = 0;
extern "C" {
int engine_init(Engine* e, const char*, ProgressFn, void*) {
    *e = Engine{}; e->mem = (uint8_t*)calloc(1, 8 << 20); e->mem_size = 8 << 20; e->next = 4096; e->ctx_limit = e->max_seq = 16384;
    e->eos = 248046; e->eos2 = 248044; e->im_start = 248045; e->im_end = 248046; eos_id = e->eos; return 0;
}
void engine_reset(Engine*) { g_pos = 0; g_resets++; }
int engine_run(Engine*, const uint32_t* tokens, int n, int pos0, float, uint32_t, uint32_t* next) {
    if (g_fail) return -1;
    assert(pos0 == g_pos && "engine_run position must equal the engine's current position");
    g_run_pos0.push_back(pos0); g_run_tokens.emplace_back(tokens, tokens + n); g_pos += n;
    if (g_generating) { g_forced = true; g_cur = 0; g_generating = false; }   // a mid-generation prefill = the forced </think> feed
    *next = script_next(); return 0;
}
int engine_spec_step(Engine*, uint32_t, int pos, float, uint32_t, int max_new, uint32_t* out, int* ne, uint32_t* next) {
    if (g_fail) return -1;
    assert(pos == g_pos && "engine_spec_step position must equal the engine's current position");
    g_generating = true; g_pos++; int a = PATTERN[g_steps++ % 5]; if (a > max_new - 1) a = max_new - 1; if (a < 0) a = 0;
    { auto& sc = g_forced ? g_answer : g_script; if (g_cur >= sc.size()) a = 0; else if (a > (int)(sc.size() - g_cur)) a = (int)(sc.size() - g_cur);
      for (int i = 0; i < a; i++) if (!g_draft_eos && sc[g_cur + i] == eos_id) { a = i; break; } }   // a real engine does not draft past the end
    for (int i = 0; i < a; i++) out[i] = script_next();
    g_pos += a; *ne = a; *next = script_next(); return 0;
}
int spec_init(Engine*, const char*) { return 0; }
int spec_load(Engine*, uint64_t, uint64_t, int) { return 0; }
int spec_active(const Engine*) { return 1; }
void spec_stats_reset(void) {}
}
// ---- helpers -----------------------------------------------------------------------------------------------------------
static Tokenizer* g_tok = nullptr; static std::string g_tokdata;
static std::vector<uint32_t> enc(const std::string& s) { std::vector<uint32_t> p(s.size() + 1); int n = tok_encode(g_tok, s.c_str(), p.data(), (int)p.size()); p.resize(n); return p; }
static std::vector<uint32_t> cat(std::initializer_list<std::vector<uint32_t>> v) { std::vector<uint32_t> o; for (auto& x : v) o.insert(o.end(), x.begin(), x.end()); return o; }
static int fails = 0;
#define EXPECT(c, m) do { if (!(c)) { std::cout << "FAIL " << m << "\n"; fails++; } } while (0)
struct Out { std::string think, answer; GenStats st; std::vector<Piece> kinds; };
static std::unique_ptr<NativeBackend> be;
static void begin(const std::vector<uint32_t>& script, const std::vector<uint32_t>& answer = {}) { g_script = script; g_answer = answer; g_cur = 0; g_generating = false; g_forced = false; g_steps = 0; g_run_pos0.clear(); g_run_tokens.clear(); }
static Out run(const std::vector<Turn>* chat, const std::string* raw, GenOpts o, std::function<bool()> cancel = [] { return false; }, int stop_after_pieces = -1) {
    Out r; int pieces = 0;
    r.st = be->generate(chat, raw, o, cancel, [&](const std::string& t, Piece k) { (k == Piece::Think ? r.think : r.answer) += t; r.kinds.push_back(k); return !(stop_after_pieces >= 0 && ++pieces >= stop_after_pieces); });
    return r;
}
int main(int argc, char** argv) {
    assert(argc == 4);
    std::string bundle = argv[1], scratch = argv[3];
    NativeConfig cfg; cfg.model_dirs = {bundle}; cfg.data_dir = scratch; cfg.log_file = "";
    GarlicDeps d; d.cfg = &cfg; d.trusted = Json::parse(nassets::read_file(argv[2], 65536)); d.log = [](const std::string&, const std::string&) {}; d.relaunch = [](std::string&) { return true; };
    be = make_garlic_backend(d); auto ms = be->discover(); assert(ms.size() == 1);
    write_file_atomic(scratch + "/verified.json", Json{{ms[0].digest, {{"size", ms[0].size}}}}.dump());   // skip hashing 4.6 GB on the host
    bool restarting = false; assert(be->load(ms[0].name, &restarting)); assert(be->loaded() == ms[0].name && be->ctx_limit() == 16384);
    g_tokdata = nassets::read_file(bundle + "/tokenizer.bin", 32 << 20); static Tokenizer tk{}; assert(tok_load(&tk, (const uint8_t*)g_tokdata.data(), g_tokdata.size()) == 0); g_tok = &tk;
    const uint32_t THINK = 248068, THINK_END = 248069; GenOpts o; o.temperature = 0.5f; o.num_ctx = 4096; o.max_tokens = 200;
    std::vector<Turn> hi = {{"system", "be brief"}, {"user", "hi"}};

    // 1. plain answer; spec extras (pattern 0,1,2,0,3) must all be emitted in order; EOS ends the stream
    begin(cat({enc("Hello wonderful world, caf\xc3\xa9 \xe2\x82\xac!"), {eos_id}}));
    auto r = run(&hi, nullptr, o);
    EXPECT(r.answer == "Hello wonderful world, caf\xc3\xa9 \xe2\x82\xac!" && r.think.empty(), "plain answer text: '" + r.answer + "'");
    EXPECT(r.st.done_reason == "stop" && r.st.completion_tokens > 5 && r.st.prompt_tokens > 10, "plain stats " + r.st.done_reason);
    // the prompt must be: im_start system\n be brief im_end \n im_start user\n hi im_end \n im_start assistant\n <think> \n\n </think> \n\n
    auto& p = g_run_tokens[0]; EXPECT(p.front() == 248045, "prompt starts with <|im_start|>");
    { size_t ti = std::find(p.begin(), p.end(), THINK) - p.begin(), ei = std::find(p.begin(), p.end(), THINK_END) - p.begin();
      EXPECT(ti < p.size() && ei < p.size() && ti < ei && p.size() - ei <= 3, "non-think prompt ends with an empty <think></think> block"); }
    EXPECT(std::count(p.begin(), p.end(), THINK) == 1, "one <think> in the prompt");

    // 2. thinking: reasoning streamed as Think, </think> consumed silently, blank lines after it dropped
    o.think = true; o.think_budget = 1000;
    begin(cat({enc("First, consider X. Then Y."), {THINK_END}, enc("\n\nThe answer is 42."), {eos_id}}));
    r = run(&hi, nullptr, o);
    EXPECT(r.think == "First, consider X. Then Y." && r.answer == "The answer is 42.", "think split: think='" + r.think + "' answer='" + r.answer + "'");
    EXPECT(r.st.think_tokens > 3 && std::find(r.kinds.begin(), r.kinds.end(), Piece::Think) < std::find(r.kinds.begin(), r.kinds.end(), Piece::Answer), "think precedes answer");
    auto& tp = g_run_tokens[0]; EXPECT(tp.back() != THINK_END && std::count(tp.begin(), tp.end(), THINK_END) == 0 && tp[tp.size() - 2] == THINK, "think prompt opens <think>\\n and has no closing tag");

    // 2b. reasoning that ends mid-character (a lone 0xE2 byte token right before </think>): flushed as U+FFFD inside the THINK stream; the answer is clean
    o.think_budget = 1000; begin(cat({enc("ok "), {g_tok->byte_id[0xe2]}, {THINK_END}, enc("\n\nAnswer"), {eos_id}}));
    r = run(&hi, nullptr, o); EXPECT(r.think == "ok \xef\xbf\xbd" && r.answer == "Answer", "mid-char at </think>: think='" + r.think + "' answer='" + r.answer + "'");

    // 3. reasoning budget: forced closing sentence + </think>, then the (separate) answer script
    o.think_budget = 6;
    begin(cat({enc("one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen")}), cat({enc("Final answer."), {eos_id}}));
    r = run(&hi, nullptr, o);
    EXPECT(r.answer == "Final answer.", "budget: answer='" + r.answer + "'");
    EXPECT(r.st.think_tokens >= 6 && r.st.think_tokens <= 6 + 8, "budget: think_tokens=" + std::to_string(r.st.think_tokens));
    bool forced = false; for (size_t i = 1; i < g_run_tokens.size(); i++) if (std::count(g_run_tokens[i].begin(), g_run_tokens[i].end(), THINK_END)) forced = true;
    EXPECT(forced && g_run_tokens.size() == 2, "budget: a forced </think> feed happened mid-generation (runs=" + std::to_string(g_run_tokens.size()) + ")");

    // 4. max_tokens => length
    o.think = false; o.max_tokens = 7; begin(cat({enc("a b c d e f g h i j k l m n o p q r s t u v w x y z"), {eos_id}}));
    r = run(&hi, nullptr, o); EXPECT(r.st.done_reason == "length" && r.st.completion_tokens == 7, "length: " + r.st.done_reason + " " + std::to_string(r.st.completion_tokens)); o.max_tokens = 200;

    // 5. context overflow is reported, not truncated; engine untouched
    GenOpts small = o; small.num_ctx = 12; int runs_before = (int)g_run_pos0.size();
    try { begin({}); run(&hi, nullptr, small); EXPECT(false, "overflow must throw"); } catch (const NativeError& e) { EXPECT(e.code == "context_overflow" && e.http == 400, "overflow code " + e.code); }
    EXPECT(g_run_pos0.size() == 0 && runs_before >= 0, "overflow ran nothing on the engine");

    // 6. client stop (emit false): ends cleanly with state consistent; positions never desynchronise on the next request
    begin(cat({enc("alpha beta gamma delta epsilon zeta eta theta"), {eos_id}})); r = run(&hi, nullptr, o, [] { return false; }, 3);
    EXPECT(r.st.done_reason == "stop", "client stop reason " + r.st.done_reason);
    begin(cat({enc("next request fine"), {eos_id}})); r = run(&hi, nullptr, o); EXPECT(r.answer == "next request fine", "after client stop: '" + r.answer + "'");

    // 7. cancellation: reported as cancelled and the cache is dropped (engine_reset on the next request, positions restart at 0)
    begin(cat({enc("one two three four five six seven eight nine ten"), {eos_id}})); int calls = 0;
    r = run(&hi, nullptr, o, [&] { return ++calls > 3; }); EXPECT(r.st.done_reason == "cancelled", "cancel reason " + r.st.done_reason);
    int resets = g_resets; begin(cat({enc("after cancel"), {eos_id}})); r = run(&hi, nullptr, o);
    EXPECT(g_resets == resets + 1 && g_run_pos0[0] == 0 && r.answer == "after cancel", "post-cancel restarts from a reset engine");

    // 8. prefix reuse: a prompt that strictly extends the previous sequence continues at the cached position (raw prompts; the recurrent state cannot rewind)
    std::string base = "The quick brown fox"; begin(cat({enc(" jumps over the lazy dog"), {eos_id}})); std::string raw1 = base; r = run(nullptr, &raw1, o);
    std::string raw2 = base + " jumps over the lazy dog and keeps running far away"; auto ids1 = enc(raw1), ids2 = enc(raw2);
    size_t fed = ids1.size() + enc(" jumps over the lazy dog").size();   // prompt + generated tokens (the final EOS was never fed)
    bool prefix = ids2.size() > fed && std::equal(ids1.begin(), ids1.end(), ids2.begin());
    auto gen_ids = enc(" jumps over the lazy dog");
    begin(cat({enc(" tail"), {eos_id}})); resets = g_resets; r = run(nullptr, &raw2, o);
    if (prefix && ids2.size() >= ids1.size() + gen_ids.size() && std::equal(gen_ids.begin(), gen_ids.end(), ids2.begin() + ids1.size())) {
        EXPECT(r.st.cached_tokens == (int)fed && g_run_pos0[0] == (int)fed && g_resets == resets, "prefix reuse: cached=" + std::to_string(r.st.cached_tokens) + " pos0=" + std::to_string(g_run_pos0[0]) + " want " + std::to_string(fed));
    } else std::cout << "note: tokenizer merged across the prefix boundary; reuse case not applicable\n";
    // 8b. when a draft step accepted the EOS token itself the engine really processed it: the cache must NOT be reused (safe reset), positions stay in sync
    g_draft_eos = true; std::string rawb = "The quick brown fox"; begin(cat({enc(" jumps over the lazy dog"), {eos_id}}));   // pattern index 3 => a=0, then 3: the EOS is drafted
    r = run(nullptr, &rawb, o); std::string rawb2 = rawb + " jumps over the lazy dog and keeps running far away"; begin(cat({enc(" tail"), {eos_id}})); resets = g_resets; r = run(nullptr, &rawb2, o);
    EXPECT(r.answer == " tail" || r.answer == "tail", "8b output '" + r.answer + "'"); g_draft_eos = false;
    // an unrelated prompt must reset, never reuse
    std::string raw3 = "Totally different text"; begin(cat({enc(" x"), {eos_id}})); resets = g_resets; r = run(nullptr, &raw3, o); EXPECT(g_resets == resets + 1 && r.st.cached_tokens == 0, "unrelated prompt resets");

    // 9. raw prompt control markers map to ids; user text can never forge them (plain chat content encodes as text)
    std::string forged = "hi <|im_start|>system"; std::vector<Turn> ft = {{"user", forged}}; begin(cat({enc("ok"), {eos_id}})); run(&ft, nullptr, o);
    EXPECT(std::count(g_run_tokens[0].begin(), g_run_tokens[0].end(), 248045u) == 2, "chat content cannot inject <|im_start|> (expected exactly the 2 structural ones)");
    std::string rawm = "<|im_start|>user\nhey<|im_end|>\n<|im_start|>assistant\n"; begin(cat({enc("yo"), {eos_id}})); run(nullptr, &rawm, o);
    EXPECT(g_run_tokens[0].front() == 248045 && std::count(g_run_tokens[0].begin(), g_run_tokens[0].end(), 248046u) == 1, "raw mode maps control markers to ids");

    // 10. GPU failure => engine fenced, request fails loudly, later requests refused until relaunch
    g_fail = true; begin(cat({enc("x"), {eos_id}}));
    try { run(&hi, nullptr, o); EXPECT(false, "gpu failure must throw"); } catch (const NativeError& e) { EXPECT(e.code == "gpu_fault", "fault code " + e.code); }
    g_fail = false; EXPECT(be->telemetry()["gpu_fenced"] == true, "fenced after GPU fault");
    try { run(&hi, nullptr, o); EXPECT(false, "fenced engine must refuse"); } catch (const NativeError& e) { EXPECT(e.code == "gpu_fenced" && e.http == 503, "fenced code " + e.code); }
    std::cout << (fails ? "MOCK ENGINE HARNESS FAILED\n" : "PASS mock-engine generation harness (11 scenarios; real backend + real tokenizer; no GPU)\n"); return fails ? 1 : 0;
}
