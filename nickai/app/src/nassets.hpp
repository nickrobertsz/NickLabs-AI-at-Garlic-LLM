// SPDX-License-Identifier: GPL-3.0-or-later
// Bundle validation for the native app (derived from the C07 worker's assets.hpp, GPL-3.0-or-later).
// Differences: no fixed 16K ceiling (the engine arbiter reports the real limit), several trusted sets, verified-cache.
#pragma once
#include "boot_log.hpp"
#include "json.hpp"
#include <fcntl.h>
#include <iomanip>
#include <openssl/sha.h>
#include <sstream>
#include <vector>
using Json = nlohmann::json;
struct AssetError : std::runtime_error { using std::runtime_error::runtime_error; };
namespace nassets {
inline void check(bool b, const char* what = "asset_invalid") { if (!b) throw AssetError(what); }
struct FD {
    int fd;
    explicit FD(const std::string& p) : fd(open(p.c_str(), O_RDONLY | O_NOFOLLOW | O_NONBLOCK)) {
        struct stat st{};
        if (fd < 0) throw AssetError("cannot open " + p);
        if (fstat(fd, &st) || !S_ISREG(st.st_mode)) { close(fd); fd = -1; throw AssetError("not a regular file: " + p); }
    }
    ~FD() { if (fd >= 0) close(fd); }
    FD(const FD&) = delete;
};
inline std::string read_file(const std::string& p, size_t limit) {
    FD f(p); struct stat st{};
    if (fstat(f.fd, &st) || st.st_size < 0 || uint64_t(st.st_size) > limit) throw AssetError("bad size: " + p);
    std::string d(size_t(st.st_size), '\0'); size_t off = 0;
    while (off < d.size()) { auto n = read(f.fd, d.data() + off, d.size() - off); if (n <= 0) throw AssetError("short read: " + p); off += size_t(n); }
    return d;
}
inline uint64_t file_size(const std::string& p) { FD f(p); struct stat st{}; if (fstat(f.fd, &st)) throw AssetError("fstat: " + p); return uint64_t(st.st_size); }
inline std::string hex(const unsigned char* h) { std::ostringstream s; for (int i = 0; i < 32; i++) s << std::hex << std::setw(2) << std::setfill('0') << int(h[i]); return s.str(); }
inline std::string sha_bytes(const std::string& d) { unsigned char h[32]; SHA256((const unsigned char*)d.data(), d.size(), h); return hex(h); }
inline std::string sha_file(const std::string& p, uint64_t expected, bool progress = false) {
    FD f(p); SHA256_CTX c; SHA256_Init(&c); std::vector<char> b(64 << 10) /* the buffer size the Worker used for the same 4.6 GB hash on this console */; uint64_t n = 0, rep = 0;
    for (;;) {
        auto k = read(f.fd, b.data(), b.size());
        if (k < 0) throw AssetError("read error: " + p);
        if (!k) break;
        n += uint64_t(k);
        if (n > expected) throw AssetError("file larger than manifest: " + p);
        if (progress && (n >> 26) != (rep >> 26)) { char cb[160]; snprintf(cb, sizeof cb, "hashing ...%s at %llu MiB", p.c_str() + (p.size() > 40 ? p.size() - 40 : 0), (unsigned long long)(n >> 20)); boot::crumb(cb); }
        SHA256_Update(&c, b.data(), size_t(k));
        if (progress && n - rep >= (256ull << 20)) { boot::record("hash_progress_mib", int(n >> 20)); rep = n; }
    }
    if (n != expected) throw AssetError("file size differs from manifest: " + p);
    unsigned char h[32]; SHA256_Final(h, &c); return hex(h);
}
inline uint32_t u32(const std::string& d, size_t p) {
    check(p <= d.size() && d.size() - p >= 4);
    auto b = (const unsigned char*)d.data() + p; return uint32_t(b[0]) | (uint32_t(b[1]) << 8) | (uint32_t(b[2]) << 16) | (uint32_t(b[3]) << 24);
}
inline uint64_t u64(const std::string& d, size_t p) { return u32(d, p) | (uint64_t(u32(d, p + 4)) << 32); }
inline void validate_tokenizer(const std::string& d) {
    check(d.size() >= 1036 && d.substr(0, 4) == "PTOK"); auto nv = u32(d, 4), nm = u32(d, 8);
    check(nv > 256 && nv <= 300000 && nm <= 1000000);
    for (size_t i = 0; i < 256; i++) check(u32(d, 12 + 4 * i) < nv);
    size_t p = 1036;
    for (uint32_t i = 0; i < nv; i++) {
        check(p <= d.size() && d.size() - p >= 3); check((unsigned char)d[p] <= 1);
        size_t len = (unsigned char)d[p + 1] + 256u * (unsigned char)d[p + 2]; p += 3; check(len <= d.size() - p); p += len;
    }
    check(nm <= (d.size() - p) / 12 && p + size_t(nm) * 12 == d.size());
    for (; p < d.size(); p += 4) check(u32(d, p) < nv);
}
inline void validate_kernels(const std::string& d) {
    check(d.size() >= 12 && d.substr(0, 4) == "PKRN"); auto n = u32(d, 4), sz = u32(d, 8);
    check(n > 0 && n <= 200 && 12ull + n * 52ull + sz == d.size());
    for (size_t i = 0; i < n; i++) { size_t p = 12 + i * 52; check(d.substr(p, 32).find('\0') != std::string::npos); check(u32(d, p + 32) < sz && u32(d, p + 32) % 256 == 0); }
}
// Returns model.idx max_seq. The engine decides what it can actually run; this only bounds sanity.
inline uint32_t validate_index(const std::string& d, uint64_t file_size) {
    check(d.size() >= 224 && d.substr(0, 4) == "PMQ5"); auto n = u32(d, 188); auto gpu = u64(d, 180);
    check(n > 2 && n < 1024 && 224ull + 36ull * n == d.size() && gpu <= file_size);
    check(u32(d, 4) > 0 && u32(d, 4) <= 64 && u32(d, 32) >= 256 && u32(d, 32) <= (1u << 20));
    check(d.substr(52, 128).find('\0') != std::string::npos);
    for (size_t i = 0; i < n; i++) {
        size_t p = 224 + i * 36; auto off = u64(d, p), sz = u64(d, p + 8); auto fmt = u32(d, p + 32);
        check(fmt <= 8);
        if (fmt != 0 && fmt != 7) check(u32(d, p + 16) > 0 && u32(d, p + 20) > 0);
        check(off <= file_size && sz <= file_size - off);
        if (fmt != 4 && fmt != 7) check(off <= gpu && sz <= gpu - off);
        check(u64(d, p + 24) <= sz);
    }
    return u32(d, 32);
}
struct Bundle {
    Json manifest; std::string root, digest; uint64_t total = 0; uint32_t ctx_max = 0; std::string weights;
    bool mtp = false;   // qwen3.5-9b ships an MTP speculative-decoding head (6 files); qwen3.5-4b has none (4 files)
    // "qwen3.5-<size>b": the 9B ships an MTP speculative-decoding head (6 files); every other size (0.8B, 2B, 4B, ...) has none (4 files).
    static bool qwen35_arch(const std::string& a) {
        if (a.size() < 10 || a.compare(0, 8, "qwen3.5-") != 0) return false;
        std::string sz = a.substr(8); if (sz.size() < 2 || sz.size() > 8 || sz.back() != 'b') return false;
        bool digit = false; for (size_t i = 0; i + 1 < sz.size(); i++) { if (sz[i] >= '0' && sz[i] <= '9') digit = true; else if (sz[i] != '.') return false; }
        return digit;
    }
    // Cheap structural validation: manifest, pinned small files, sizes by fstat. Weights are hashed only if `hash_weights`.
    Bundle(const std::string& dir, const Json& trusted_sets, bool hash_weights, bool progress = false) : root(dir) {
        auto mtext = read_file(root + "/bundle.json", 65536);
        manifest = Json::parse(mtext);
        digest = sha_bytes(mtext);
        auto arch = manifest.at("architecture").get<std::string>();
        check(manifest.at("version") == 1 && qwen35_arch(arch));
        mtp = arch == "qwen3.5-9b";
        auto files = manifest.at("files"); check(files.is_object() && files.size() == (mtp ? 6u : 4u));
        for (auto it = files.begin(); it != files.end(); ++it) {
            auto name = it.key();
            check(!name.empty() && name.find('/') == std::string::npos && name.find("..") == std::string::npos);
            check(file_size(root + "/" + name) == it.value().at("bytes").get<uint64_t>());
            total += it.value().at("bytes").get<uint64_t>();
        }
        weights = manifest.at("weights").get<std::string>();
        std::string mw;
        if (mtp) { mw = manifest.at("mtp_weights").get<std::string>(); check(files.contains(weights) && files.contains(mw)); }
        else check(files.contains(weights) && !manifest.contains("mtp_weights"));
        std::vector<const char*> small = {"model.idx", "kernels.bin", "tokenizer.bin"}; if (mtp) small.push_back("mtp.idx");
        // small files carry GPU code/shape: always hash and pin them
        for (auto name : small) check(files.contains(name) && sha_file(root + "/" + name, files.at(name).at("bytes")) == files.at(name).at("sha256"));
        bool pinned = false;
        auto sets = trusted_sets.is_array() ? trusted_sets : Json::array({trusted_sets});
        for (auto& t : sets) {
            if (!t.is_object()) continue;
            bool ok = true;
            for (auto name : small) ok = ok && t.contains(name) && files.at(name).at("sha256") == t[name];
            ok = ok && t.contains("weights") && files.at(weights).at("sha256") == t["weights"];
            if (mtp) ok = ok && t.contains("mtp_weights") && files.at(mw).at("sha256") == t["mtp_weights"];
            if (ok) { pinned = true; break; }
        }
        check(pinned, "bundle is not in the trusted asset list (GPU kernels must be pinned)");
        auto index = read_file(root + "/model.idx", 65536);
        ctx_max = validate_index(index, files.at(weights).at("bytes"));
        auto idxname = std::string(index.c_str() + 52); idxname = idxname.substr(idxname.find_last_of('/') + 1);
        check(weights == idxname);
        validate_tokenizer(read_file(root + "/tokenizer.bin", 32 << 20)); validate_kernels(read_file(root + "/kernels.bin", 32 << 20));
        if (mtp) {
            auto mi = read_file(root + "/mtp.idx", 65536); check(mi.size() == 156 + 12 * 36 && mi.substr(0, 4) == "PMTP");
            auto mtpname = std::string(mi.c_str() + 8); mtpname = mtpname.substr(mtpname.find_last_of('/') + 1); check(mw == mtpname);
            check(u64(mi, 136) + u64(mi, 144) == files.at(mw).at("bytes").get<uint64_t>());
        }
        if (hash_weights) {
            check(sha_file(root + "/" + weights, files.at(weights).at("bytes"), progress) == files.at(weights).at("sha256"), "weights hash mismatch");
            if (mtp) check(sha_file(root + "/" + mw, files.at(mw).at("bytes"), progress) == files.at(mw).at("sha256"), "mtp weights hash mismatch");
        }
    }
};
}
