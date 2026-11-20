// SPDX-License-Identifier: GPL-3.0-or-later
// Incremental UTF-8 reassembly: byte-level BPE tokens may end in the middle of a character.
#pragma once
#include <cstddef>
#include <cstdint>
#include <functional>
#include <string>
namespace {
class Utf8 {
    std::string pending;
public:
    void piece(const uint8_t* p, size_t n, const std::function<void(const std::string&)>& emit) {
        pending.append((const char*)p, n); std::string ready;
        while (!pending.empty()) {
            auto c = (unsigned char)pending[0];
            size_t k = c < 128 ? 1 : (c >= 194 && c <= 223) ? 2 : (c >= 224 && c <= 239) ? 3 : (c >= 240 && c <= 244) ? 4 : 0;
            if (!k) { ready += "\xef\xbf\xbd"; pending.erase(0, 1); continue; }
            // validate every byte that has already arrived (not only once the character is complete): an invalid sequence must not
            // swallow the valid bytes after it just because the stream ended before the character could have been completed
            size_t have = pending.size() < k ? pending.size() : k;
            bool valid = true;
            for (size_t i = 1; i < have; i++) if (((unsigned char)pending[i] & 192) != 128) valid = false;
            if (valid && have >= 2) {
                unsigned char b1 = (unsigned char)pending[1];
                if (k == 3 && ((c == 224 && b1 < 160) || (c == 237 && b1 >= 160))) valid = false;
                if (k == 4 && ((c == 240 && b1 < 144) || (c == 244 && b1 >= 144))) valid = false;
            }
            if (valid && pending.size() < k) break;   // a valid prefix of a longer character: wait for more bytes
            if (!valid) { ready += "\xef\xbf\xbd"; pending.erase(0, 1); } else { ready += pending.substr(0, k); pending.erase(0, k); }
        }
        if (!ready.empty()) emit(ready);
    }
    void finish(const std::function<void(const std::string&)>& emit) { if (!pending.empty()) emit("\xef\xbf\xbd"); pending.clear(); }
};
}
