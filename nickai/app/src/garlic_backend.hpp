// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include "nbackend.hpp"
#include "nconfig.hpp"
#include <memory>
struct GarlicDeps {
    NativeConfig* cfg = nullptr;
    Json trusted;                                           // trusted asset digests (array or object)
    std::function<bool(std::string&)> relaunch;             // relaunch the app; false + reason if impossible
    std::function<void(const std::string&, const std::string&)> log;   // level, message
};
std::unique_ptr<NativeBackend> make_garlic_backend(const GarlicDeps& d);
