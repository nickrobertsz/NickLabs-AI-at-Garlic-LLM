// Host harness: the REAL garlic_backend + generated engine against controlled SDK faults (no GPU emulation, no hardware claim).
// Usage: garlic_host <bundle dir> <trusted-assets.json> <scratch dir>
#include "garlic_backend.hpp"
#include "nassets.hpp"
#include <cassert>
#include <cstdarg>
#include <iostream>
extern "C" {
#include "orbis/libkernel.h"
#include "orbis/GnmDriver.h"
}
static int mode = 0;   // 0: direct memory exhausted
extern "C" {
size_t sceKernelGetDirectMemorySize() { return 1280ull << 20; }
int sceKernelAllocateDirectMemory(off_t, off_t, size_t, size_t, int, off_t* off) { if (mode == 0) return -12; *off = 4096; return 0; }
int sceKernelMapDirectMemory(void** a, size_t, int, int, off_t, size_t) { *a = calloc(1, 16 << 20); return 0; }
int sceKernelReleaseDirectMemory(off_t, size_t) { return 0; }
int sceKernelMunmap(void* a, size_t) { free(a); return 0; }
uint64_t sceKernelGetTscFrequency() { return 1000000; }
uint64_t sceKernelReadTsc() { static uint64_t t; return t += 20000000; }
int sceKernelUsleep(unsigned) { return 0; }
int sceKernelDebugOutText(int, const char*, ...) { return 0; }
int sceKernelAvailableFlexibleMemorySize(size_t* n) { *n = 0; return -1; }
int sceKernelMapFlexibleMemory(void**, size_t, int, int) { return -1; }
int sceGnmDrawInitDefaultHardwareState350(uint32_t* p, uint32_t n) { memset(p, 0, n * 4); return 256; }
int sceGnmSubmitCommandBuffers(uint32_t, void**, uint32_t*, void**, uint32_t*) { return int(0x80d11000u); }
int sceGnmSubmitDone() { return 0; }
}
int main(int argc, char** argv) {
    assert(argc == 4);
    std::string bundle = argv[1], scratch = argv[3];
    NativeConfig cfg; cfg.model_dirs = {bundle}; cfg.data_dir = scratch; cfg.log_file = "";
    std::string t = nassets::read_file(argv[2], 65536);
    std::vector<std::string> logs;
    GarlicDeps d; d.cfg = &cfg; d.trusted = Json::parse(t); d.log = [&](const std::string& l, const std::string& m) { logs.push_back(l + " " + m); };
    d.relaunch = [](std::string&) { return true; };
    auto be = make_garlic_backend(d);
    auto ms = be->discover();
    assert(ms.size() == 1 && ms[0].name == "qwen3.5-9b-garlic" && ms[0].ctx_max == 16384 && ms[0].mtp && ms[0].size > 4000000000ull);
    std::cout << "discover ok: " << ms[0].name << " " << ms[0].digest.substr(0, 19) << " size=" << ms[0].size << "\n";
    assert(be->loaded().empty() && be->ctx_limit() == 0);
    // unknown model
    try { bool r = false; be->load("nope", &r); assert(false); } catch (const NativeError& e) { assert(e.http == 404); }
    // generation without a model
    try { std::vector<Turn> c = {{"user", "x"}}; GenOpts o; be->generate(&c, nullptr, o, [] { return false; }, [](const std::string&, Piece) { return true; }); assert(false); } catch (const NativeError& e) { assert(e.code == "no_model"); }
    // engine load with exhausted direct memory: structural validation of the real bundle passes, engine fails before any reservation -> retry-safe, not fenced
    cfg.verify_full_hash = false;
    // mark the bundle as already verified to skip hashing 4.6 GB on the host: a verified.json written exactly as the app would
    Json ver = {{ms[0].digest, {{"size", ms[0].size}}}};
    write_file_atomic(scratch + "/verified.json", ver.dump());
    try { bool r = false; be->load("qwen3.5-9b-garlic", &r); assert(false); }
    catch (const NativeError& e) { std::cout << "load -> " << e.http << " " << e.code << ": " << e.what() << "\n"; assert(e.code == "load_failed"); assert(std::string(e.what()).find("retry is safe") != std::string::npos); }
    assert(be->telemetry()["gpu_fenced"] == false);
    // second attempt behaves identically (engine state was reset)
    try { bool r = false; be->load("qwen3.5-9b-garlic", &r); assert(false); } catch (const NativeError& e) { assert(e.code == "load_failed"); }
    assert(be->telemetry()["gpu_fenced"] == false);
    // a tampered kernel file is rejected before the engine runs
    // O_NOFOLLOW rejects symlinks by design: build the tampered bundle from real small files and sparse placeholders for the big ones
    std::string tmp = scratch + "/tamper";
    (void)!system(("rm -rf '" + tmp + "' && mkdir -p '" + tmp + "' && for f in bundle.json kernels.bin model.idx mtp.idx; do cp '" + bundle + "'/$f '" + tmp + "'/; done && "
                   "for f in qwen3.5-9b-mtp.bin qwen3.5-9b-q4s.bin; do truncate -s $(stat -c %s '" + bundle + "'/$f) '" + tmp + "'/$f; done && head -c 5559675 /dev/zero > '" + tmp + "/tokenizer.bin'").c_str());
    cfg.model_dirs = {tmp};
    auto be2 = make_garlic_backend(d);
    auto ms2 = be2->discover(); assert(ms2.size() == 1);
    try { bool r = false; be2->load(ms2[0].name, &r); assert(false); } catch (const NativeError& e) { std::cout << "tampered -> " << e.http << " " << e.code << "\n"; assert(e.code == "bundle_invalid"); }
    std::cout << "PASS garlic_backend host harness (real bundle validation + engine fault handling; no GPU)\n";
}
