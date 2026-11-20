#!/usr/bin/env python3
"""Native app copy of ps4-worker/tools/prepare_engine.py: same exact-anchor overlay, written to native-app/build/engine."""
from pathlib import Path
import hashlib,json
root=Path(__file__).resolve().parents[2]  # repository root; reference sources are only read
import os
GARLIC=Path(os.environ.get('GARLIC_DIR') or next((q for q in (root/'sources/garlic-llm',root.parent,root) if (q/'chat/engine.c').exists()),root/'sources/garlic-llm'))
src=GARLIC/'chat'
out=Path(__file__).resolve().parents[1]/'build/engine';out.mkdir(parents=True,exist_ok=True)
for p in src.glob('engine*'):
 if p.is_file(): (out/p.name).write_bytes(p.read_bytes())
for name in ['tokenizer.c','tokenizer.h']:(out/name).write_bytes((src/name).read_bytes())
s=(src/'engine.c').read_text()
a=s.index('static void weights_locate(char* path) {');b=s.index('\nstatic uint8_t* read_file',a)
s=s[:a]+'''extern const char* worker_bundle_root;
static void weights_locate(char* path) {
    const char* base = strrchr(path, '/');
    char resolved[129];
    int n = snprintf(resolved, sizeof(resolved), "%s/%s", worker_bundle_root, base ? base+1 : path);
    if (n < 0 || n >= (int)sizeof(resolved)) { path[0] = 0; return; }
    strcpy(path, resolved);
}
''' + s[b:]
# Upstream GPU alloc callers sometimes dereference failure. Do not return a null GPU address.
s=s.replace('if (off + size > e->mem_size) return 0;', 'if (off > e->mem_size || size > e->mem_size - off) { extern void worker_memory_fatal(void); worker_memory_fatal(); }')
# Exact, checked edits: never inject a fatal hook after ordinary debug output.
def once(old, new):
 global s
 assert s.count(old)==1, 'Upstream overlay anchor changed: '+old[:100]
 s=s.replace(old,new,1)
a=s.index('static void fail(Engine* e, const char* fmt, ...) {')
b=s.index('\n}\n',a)+2
failure=s[a:b]
assert failure.count('sceKernelDebugOutText(0, line);')==1
s=s[:a]+failure.replace('sceKernelDebugOutText(0, line);',
    'sceKernelDebugOutText(0, "%s", line);\n    extern void worker_gpu_fatal(void); worker_gpu_fatal();')+s[b:]
# The observer wrappers retain the real SDK operations/arguments/results.
# Resource accounting distinguishes an actual pre-reservation failure from
# an outstanding allocation whose final map or release failed.
# Q3s deliberately omits the 16-wave variant; upstream clears this optional
# lookup's error and tuning skips NULL entries. Do not turn discovery into a
# fatal error. Required kernel() calls and actual dispatch still fail closed.
once('                e->k_swar3[n][v] = kernel(e, nm);',
     '                e->k_swar3[n][v] = NULL;\n                for (int ki = 0; ki < e->nk; ++ki)\n                    if (!strcmp(e->k[ki].name, nm)) e->k_swar3[n][v] = &e->k[ki];')
once('static void dispatch(Engine* e, const GpuKernel* k, uint64_t kargs, uint32_t groups, uint32_t threads) {',
     'static void dispatch(Engine* e, const GpuKernel* k, uint64_t kargs, uint32_t groups, uint32_t threads) {\n    if (!k) { fail(e, "required dispatch kernel missing"); return; }')
once('#define SH_START_X', '''
extern void worker_engine_event(const char*, int);
extern void worker_memory_event(const char*, int, uint64_t);
extern void worker_submit_begin(void);
extern void worker_submit_result(int);
static int observed_allocate(off_t start, off_t end, size_t size, size_t align, int type, off_t* off) {
    int rc = sceKernelAllocateDirectMemory(start, end, size, align, type, off);
    worker_memory_event("allocate", rc, size); return rc;
}
static int observed_map(void** addr, size_t size, int prot, int flags, off_t off, size_t align) {
    int rc = sceKernelMapDirectMemory(addr, size, prot, flags, off, align);
    worker_memory_event("map", rc, size); return rc;
}
static int observed_unmap(void* addr, size_t size) {
    int rc = sceKernelMunmap(addr, size);
    worker_memory_event("unmap", rc, size); return rc;
}
static int observed_release(off_t off, size_t size) {
    int rc = sceKernelReleaseDirectMemory(off, size);
    worker_memory_event("release", rc, size); return rc;
}
#define sceKernelAllocateDirectMemory observed_allocate
#define sceKernelMapDirectMemory observed_map
#define sceKernelMunmap observed_unmap
#define sceKernelReleaseDirectMemory observed_release
#define SH_START_X''')
once('            sceKernelReleaseDirectMemory(off, size);\n            continue;',
     '            if (sceKernelReleaseDirectMemory(off, size)) return -1;\n            continue;')
once('        sceKernelMunmap(addr, size);   // found the largest block; take it again with a 64 MiB margin\n        sceKernelReleaseDirectMemory(off, size);',
     '        if (sceKernelMunmap(addr, size)) return -1;\n        if (sceKernelReleaseDirectMemory(off, size)) return -1;')
once('        if (sceKernelMapDirectMemory(&addr, size, 0x33, 0, off, ALIGN_2MB)) return -1;',
     '        if (sceKernelMapDirectMemory(&addr, size, 0x33, 0, off, ALIGN_2MB)) {\n            sceKernelReleaseDirectMemory(off, size); // no mapped GPU work; release the failed final allocation\n            return -1;\n        }')
once('    if (load_index(e, dir)) return -1;',
     '    worker_engine_event("load_index_begin", 0);\n    if (load_index(e, dir)) return -1;\n    worker_engine_event("load_index_ok", 0);\n    worker_engine_event("reserve_garlic_begin", 0);')
once('    e->dcb = (uint32_t*)(uintptr_t)gpu_alloc(e, DCB_BYTES);',
     '    worker_engine_event("reserve_garlic_ok", 0);\n    e->dcb = (uint32_t*)(uintptr_t)gpu_alloc(e, DCB_BYTES);')
once('    if (load_kernels(e, dir)) return -1;',
     '    worker_engine_event("load_kernels_begin", 0);\n    if (load_kernels(e, dir)) return -1;\n    worker_engine_event("load_kernels_ok", 0);')
once('    sceGnmDrawInitDefaultHardwareState350(init, 256);',
     '    int init_rc = sceGnmDrawInitDefaultHardwareState350(init, 256);\n    worker_engine_event("hardware_state_result", init_rc);')
once('    if (gpu_selftest(e)) return -1;',
     '    worker_engine_event("gpu_selftest_begin", 0);\n    if (gpu_selftest(e)) return -1;\n    worker_engine_event("gpu_selftest_returned", 0);')
once('    int r = sceGnmSubmitCommandBuffers(1, dcbs, sizes, NULL, NULL);',
     '    worker_submit_begin();\n    int r = sceGnmSubmitCommandBuffers(1, dcbs, sizes, NULL, NULL);\n    worker_submit_result(r);')
once('        say("GPU self-test: skipped (/data/garlic_selftest.txt)");',
     '        worker_engine_event("gpu_selftest_skipped", 0);\n        say("GPU self-test: skipped (/data/garlic_selftest.txt)");')
once('    say("GPU self-test: ok (%u values, 4 dispatches in 2 submissions, %.3f ms GPU)", (unsigned)N, gpu * 1e3);',
     '    worker_engine_event("gpu_selftest_passed", 0);\n    say("GPU self-test: ok (%u values, 4 dispatches in 2 submissions, %.3f ms GPU)", (unsigned)N, gpu * 1e3);')
assert s.count('worker_gpu_fatal();')==1
(out/'engine.c').write_text(s)
print('Generated native engine overlay (own copy under native-app/build/engine); reference sources and ps4-worker unchanged')
