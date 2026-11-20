# Building the console app from source

You do not need this to use the project (the installer package is in `hub/dist/`). It is here so that the app can be audited, changed and rebuilt (GPL-3).

Needs: the **Garlic LLM** checkout (this folder sits inside your fork of it; `chat/engine.c` is found automatically, or set `GARLIC_DIR`), the **OpenOrbis PS4 toolchain**, a static **OpenSSL** built for it, `clang`/`ld.lld`, and the .NET **PkgTool** used to wrap the package.
Point `NLAG_BUILD_ROOT` at a folder that contains `build/OpenOrbis/PS4Toolchain`, `build/openssl-3.5.4` (with `libcrypto.a`), `build/dotnet`, `build/pkgtool` and `vendor/json.hpp` (nlohmann/json, MIT; a copy is in `app/vendor/`).

```
python3 app/tools/build_native.py      # compile + link  -> app/build/native/eboot.bin
python3 app/tools/package_native.py    # package         -> app/build/package/*.pkg + package.json
```
Host tests (no console): `app/tools/test.sh` (clang with ASan/UBSan/TSan, a mock GPU engine, fuzzing of the model validators, API end-to-end). Hub tests: `python3 -m unittest discover -s tests` in the project folder.

Known console quirks the code works around: no directory listing for the app user, the SDK's `fstat` ABI, `std::chrono::steady_clock` does not advance (the app uses the process clock), `thread_local` objects with destructors do not link, files the app creates are mode 0600 (the hub sets 644 over FTP).
