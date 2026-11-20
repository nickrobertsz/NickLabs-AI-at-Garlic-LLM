// SPDX-License-Identifier: GPL-3.0-or-later
// API-key helpers. ct_equal / parse_bearer started as Fat / Slim PS4 worker proposals; both had real defects (ct_equal ignored the length difference so "abc" == "abc\0";
// parse_bearer did not compile, compared 7 bytes against "Bearer" and accepted "Bearertoken") and were rewritten by Claude.
#pragma once
#include <cstddef>
#include <cstdint>
#include <string>
// Constant-time equality for secrets: touches every byte of the longer string, no early exit, length difference folded into the result.
inline bool ct_equal(const std::string& a, const std::string& b) {
    const size_t n = a.size() > b.size() ? a.size() : b.size();
    size_t diff = a.size() ^ b.size();
    for (size_t i = 0; i < n; ++i) {
        unsigned char x = i < a.size() ? (unsigned char)a[i] : 0, y = i < b.size() ? (unsigned char)b[i] : 0;
        diff |= (size_t)(x ^ y);
    }
    return diff == 0;
}
// "Bearer <token>": scheme matched case-insensitively (ASCII only), then one or more spaces, then a 1..512 byte token of visible ASCII (0x21-0x7E).
inline bool parse_bearer(const std::string& header, std::string& token) {
    token.clear();
    static const char scheme[] = "bearer";
    if (header.size() < 8) return false;
    for (size_t i = 0; i < 6; ++i) { char c = header[i]; if (c >= 'A' && c <= 'Z') c = char(c - 'A' + 'a'); if (c != scheme[i]) return false; }
    size_t pos = 6;
    if (header[pos] != ' ') return false;            // the scheme must be followed by at least one space ("Bearertoken" is not a bearer header)
    while (pos < header.size() && header[pos] == ' ') ++pos;
    size_t n = header.size() - pos;
    if (n == 0 || n > 512) return false;
    for (size_t i = pos; i < header.size(); ++i) { unsigned char c = (unsigned char)header[i]; if (c < 0x21 || c > 0x7E) return false; }
    token = header.substr(pos);
    return true;
}
