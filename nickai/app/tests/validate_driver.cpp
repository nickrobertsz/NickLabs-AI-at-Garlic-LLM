// Fuzz driver for the bundle validators (nassets.hpp): stdin protocol "kind size\n" + <size> raw bytes; replies one line per request:
//   OK | REJECT <what> (AssetError or any std::exception) . A crash/hang is detected by the caller (ASan aborts the process).
#include "nassets.hpp"
#include <iostream>
int main() {
    std::string head;
    while (std::getline(std::cin, head)) {
        char kind[16]; unsigned long long size = 0, fsize = 0;
        if (sscanf(head.c_str(), "%15s %llu %llu", kind, &size, &fsize) < 2) break;
        std::string data(size, '\0');
        if (size) std::cin.read(&data[0], std::streamsize(size));
        if (!std::cin) break;
        try {
            std::string k = kind;
            if (k == "tok") nassets::validate_tokenizer(data);
            else if (k == "krn") nassets::validate_kernels(data);
            else if (k == "idx") nassets::validate_index(data, fsize);
            else if (k == "mtp") { nassets::check(data.size() == 156 + 12 * 36 && data.substr(0, 4) == "PMTP"); (void)nassets::u64(data, 136); (void)nassets::u64(data, 144); }
            std::cout << "OK\n";
        } catch (const std::exception& e) { std::cout << "REJECT " << e.what() << "\n"; }
        std::cout.flush();
    }
}
