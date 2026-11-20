#include "nauth.hpp"
#include <iostream>
int fails = 0;
#define EXPECT(c, m) do { if (!(c)) { std::cout << "FAIL " << m << "\n"; fails++; } } while (0)
int main() {
    // ct_equal: includes the case the Fat worker's draft got wrong (trailing NUL)
    EXPECT(ct_equal("", "") && ct_equal("abc", "abc") && ct_equal(std::string("a\0b", 3), std::string("a\0b", 3)), "equal strings");
    EXPECT(!ct_equal("abc", "abd") && !ct_equal("abc", "ab") && !ct_equal("ab", "abc") && !ct_equal("", "a") && !ct_equal("a", ""), "different strings");
    EXPECT(!ct_equal("abc", std::string("abc\0", 4)) && !ct_equal(std::string("abc\0", 4), "abc") && !ct_equal("", std::string(1, '\0')), "trailing NUL must differ (draft bug)");
    EXPECT(!ct_equal(std::string(300, 'x'), std::string(300, 'x') + "y") && ct_equal(std::string(10000, 'k'), std::string(10000, 'k')), "long");
    std::string t;
    struct C { const char* in; bool ok; const char* tok; } cs[] = {
        {"Bearer abc", true, "abc"}, {"bearer abc", true, "abc"}, {"BEARER abc", true, "abc"}, {"BeArEr   abc", true, "abc"}, {"Bearer a.b-c_d~e+f/g=", true, "a.b-c_d~e+f/g="},
        {"Bearer", false, ""}, {"Bearer ", false, ""}, {"Bearer    ", false, ""}, {"Bearertoken", false, ""}, {"Bearer\tabc", false, ""}, {"Basic abc", false, ""}, {"", false, ""}, {"Bearer abc ", false, ""},
        {"Bearer ab c", false, ""}, {"Bearer abc\r", false, ""}, {"Bearer abc\n", false, ""}, {"Bearer \xc3\xa9", false, ""}, {"Bearer a\x01", false, ""}, {"Bearer a\x7f", false, ""}, {" Bearer abc", false, ""},
        {"Bearerx abc", false, ""}, {"Bear abc", false, ""}, {"Token abc", false, ""}};
    for (auto& c : cs) { bool r = parse_bearer(c.in, t); EXPECT(r == c.ok && (!r || t == c.tok) && (r || t.empty()), std::string("bearer '") + c.in + "' -> " + (r ? "ok:" + t : "reject")); }
    EXPECT(parse_bearer("Bearer " + std::string(512, 'a'), t) && t.size() == 512, "512 ok"); EXPECT(!parse_bearer("Bearer " + std::string(513, 'a'), t) && t.empty(), "513 rejected");
    std::cout << (fails ? "UNIT AUTH FAILED\n" : "PASS unit auth (ct_equal 4 groups, parse_bearer 25 cases)\n"); return fails ? 1 : 0;
}
