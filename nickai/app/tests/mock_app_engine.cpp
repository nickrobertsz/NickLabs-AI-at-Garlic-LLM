// Mock GPU engine for running the REAL console main.cpp + garlic_backend.cpp on the host (tests/test_app.py). No GPU is involved.
// Replies with MOCK_REPLY (default "mock reply"); returns a GPU failure while the file named by MOCK_FAULT_FILE exists.
#include <cstdlib>
#include <cstring>
#include <string>
#include <sys/stat.h>
#include <vector>
extern "C" {
#include "engine.h"
#include "tokenizer.h"
}
static std::vector<uint32_t> g_reply; static size_t g_cur = 0; static long g_pos = 0; static int g_steps = 0;
static Tokenizer g_tok{}; static std::string g_tokdata; static uint32_t g_eos = 248046;
static const int PATTERN[] = {0, 1, 2, 0, 3};
static bool fault() { const char* f = getenv("MOCK_FAULT_FILE"); struct stat st; return f && stat(f, &st) == 0; }
static uint32_t g_last_next = 0; static bool g_have_next = false;
static uint32_t next_tok() { return g_cur < g_reply.size() ? g_reply[g_cur++] : g_eos; }
int eng_margin_mb = 0;
extern "C" {
int engine_init(Engine* e, const char* dir, ProgressFn, void*) {
    FILE* f = fopen((std::string(dir) + "/tokenizer.bin").c_str(), "rb"); if (!f) return -1;
    char buf[65536]; size_t n; g_tokdata.clear(); while ((n = fread(buf, 1, sizeof buf, f)) > 0) g_tokdata.append(buf, n); fclose(f);
    if (tok_load(&g_tok, (const uint8_t*)g_tokdata.data(), g_tokdata.size())) return -1;
    std::string text = getenv("MOCK_REPLY") ? getenv("MOCK_REPLY") : "mock reply"; std::vector<uint32_t> ids(text.size() + 1);
    int k = tok_encode(&g_tok, text.c_str(), ids.data(), (int)ids.size()); ids.resize(k > 0 ? k : 0); g_reply = ids;
    *e = Engine{}; e->mem = (uint8_t*)calloc(1, 8 << 20); e->mem_size = 8 << 20; e->next = 4096;
    e->ctx_limit = e->max_seq = getenv("MOCK_CTX") ? (uint32_t)atoi(getenv("MOCK_CTX")) : 16384;
    e->eos = 248046; e->eos2 = 248044; e->im_start = 248045; e->im_end = 248046; g_eos = e->eos; return 0;
}
void engine_reset(Engine*) { g_pos = 0; g_have_next = false; }
int engine_run(Engine*, const uint32_t* tk, int n, int pos0, float, uint32_t, uint32_t* next) {
    if (fault()) return -1;
    if (pos0 != g_pos) return -2;   // position desync is a hard failure the tests will see
    // A plain decode step (the 4B has no MTP head) feeds back the one token it just drew; anything else is prefill, which restarts the scripted reply.
    bool decode = n == 1 && g_have_next && tk[0] == g_last_next;
    g_pos += n; if (!decode) g_cur = 0;
    *next = next_tok(); g_last_next = *next; g_have_next = true; return 0;
}
int engine_spec_step(Engine*, uint32_t, int pos, float, uint32_t, int max_new, uint32_t* out, int* ne, uint32_t* next) {
    if (fault()) return -1;
    if (pos != g_pos) return -2;
    g_pos++; int a = PATTERN[g_steps++ % 5]; if (a > max_new - 1) a = max_new - 1; if (a < 0) a = 0;
    if (g_cur >= g_reply.size()) a = 0; else if (a > (int)(g_reply.size() - g_cur)) a = (int)(g_reply.size() - g_cur);
    for (int i = 0; i < a; i++) out[i] = next_tok();
    g_pos += a; *ne = a; *next = next_tok();
    return 0;
}
int spec_init(Engine*, const char* dir) {   // the real engine fails here without mtp.idx: a bundle without an MTP head must never reach it
    struct stat st; return stat((std::string(dir) + "/mtp.idx").c_str(), &st) == 0 ? 0 : -1;
}
int spec_load(Engine*, uint64_t, uint64_t, int) { return 0; }
int spec_active(const Engine*) { return 1; }
void spec_stats_reset(void) {}
}
