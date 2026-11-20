// Differential-test driver: JSON lines on stdin {"op":"v","cidr":..} / {"op":"m","ip":..,"cidr":..} -> one "0"/"1" per line (real NativeConfig code).
#include "nconfig.hpp"
#include <iostream>
int main() {
    std::string line;
    while (std::getline(std::cin, line)) {
        Json j = Json::parse(line, nullptr, false);
        if (j.is_discarded()) { std::cout << "E\n"; continue; }
        if (j["op"] == "v") std::cout << (NativeConfig::cidr_ok(j["cidr"].get<std::string>()) ? 1 : 0) << "\n";
        else std::cout << (NativeConfig::in_cidr(j["ip"].get<std::string>(), j["cidr"].get<std::string>()) ? 1 : 0) << "\n";
    }
}
