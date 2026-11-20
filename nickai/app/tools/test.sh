#!/usr/bin/env bash
# All host-side verification for NickLabs AI on Garlic LLM (no console contact). Usage: native-app/tools/test.sh
set -euo pipefail
A="$(cd "$(dirname "$0")/.." && pwd)"; R="$(dirname "$A")"
cd "$A"
INC=(-Isrc "-I$A/vendor")
echo "== build host server (ASan+UBSan / TSan)"
clang++ -std=c++17 -O1 -g -fsanitize=address,undefined -Wall -Wextra "${INC[@]}" src/host_main.cpp -o /tmp/native-host -pthread
clang++ -std=c++17 -O1 -g -fsanitize=thread -Wall "${INC[@]}" src/host_main.cpp -o /tmp/native-host-tsan -pthread
ST="$A/tests/sdk_stubs"; python3 tools/prepare_engine.py >/dev/null
echo "== unit helpers (utf8 reassembly, stop/tool gate)"
clang++ -std=c++17 -O1 -g -fsanitize=address,undefined -Wall -Wextra "${INC[@]}" tests/unit_helpers.cpp -o /tmp/unit_helpers -pthread && /tmp/unit_helpers
echo "== unit auth (ct_equal, parse_bearer)"
clang++ -std=c++17 -O1 -g -fsanitize=address,undefined -Wall -Wextra "${INC[@]}" tests/unit_auth.cpp -o /tmp/unit_auth && /tmp/unit_auth
echo "== bundle validators fuzz (mutated REAL tokenizer/kernels/index/mtp files, ASan/UBSan)"
clang++ -std=c++17 -O1 -g -fsanitize=address,undefined -Wno-deprecated-declarations "${INC[@]}" tests/validate_driver.cpp -o /tmp/validate_driver -lcrypto -pthread
if [ -d "$R/ps4-worker/build/bundle-9b" ]; then python3 tests/fuzz_bundle.py "$R/ps4-worker/build/bundle-9b" 5 | tail -2; python3 tests/fuzz_bundle.py "$R/ps4-worker/build/bundle-9b" 99 | tail -1 | grep -q CLEAN || { echo "bundle fuzz FAILED"; exit 1; }; else echo "(bundle absent: skipped)"; fi
echo "== mock-engine generation harness (real backend + tokenizer, scripted engine)"
clang++ -std=c++17 -O1 -g -fsanitize=address,undefined -Wno-deprecated-declarations "${INC[@]}" -I"$ST" -Ibuild/engine -c src/garlic_backend.cpp -o /tmp/gb_asan.o
clang -std=gnu11 -O1 -g -fsanitize=address,undefined -Wno-everything -Ibuild/engine -c build/engine/tokenizer.c -o /tmp/tok_asan.o
clang++ -std=c++17 -O1 -g -fsanitize=address,undefined -Wno-deprecated-declarations "${INC[@]}" -I"$ST" -Ibuild/engine tests/mock_engine_host.cpp /tmp/gb_asan.o /tmp/tok_asan.o -lcrypto -lm -pthread -o /tmp/mock_engine_host
if [ -d "$R/ps4-worker/build/bundle-9b" ]; then mkdir -p /tmp/me_scratch; ASAN_OPTIONS=detect_leaks=0 /tmp/mock_engine_host "$R/ps4-worker/build/bundle-9b" "$R/ps4-worker/trusted-assets.json" /tmp/me_scratch 2>&1 | grep -E "^(PASS|FAIL|MOCK|note)"; ASAN_OPTIONS=detect_leaks=0 /tmp/mock_engine_host "$R/ps4-worker/build/bundle-9b" "$R/ps4-worker/trusted-assets.json" /tmp/me_scratch >/dev/null 2>&1 || { echo "mock-engine harness FAILED"; exit 1; }; else echo "(bundle absent: skipped)"; fi
echo "== real console startup logic on the host (main.cpp + backend + mock engine): autoload, persisted state, swap/release, fence watchdog"
clang++ -std=c++17 -O1 -g -fsanitize=address,undefined -Wno-deprecated-declarations "${INC[@]}" -I"$ST" -Ibuild/engine src/main.cpp src/garlic_backend.cpp tests/mock_app_engine.cpp /tmp/tok_asan.o -lcrypto -lm -pthread -o /tmp/native-app-host
if [ -d "$R/ps4-worker/build/bundle-9b" ]; then timeout 400 python3 -W ignore tests/test_app.py 2>&1 | grep -E "^(Ran|OK|FAILED)"; else echo "(bundle absent: skipped)"; fi
echo "== API e2e (ASan/UBSan)";  python3 -I tests/e2e_api.py /tmp/native-host
echo "== API e2e (TSan)";        python3 -I tests/e2e_api.py /tmp/native-host-tsan
echo "== soak (8 workers, chaos aborts; ASan/UBSan then TSan)"; NATIVE_HOST_BIN=/tmp/native-host python3 -W ignore tests/test_soak.py 2>&1 | tail -3; NATIVE_HOST_BIN=/tmp/native-host-tsan SOAK_SECONDS=10 python3 -W ignore tests/test_soak.py 2>&1 | tail -3
echo "== hostile-JSON fuzzing + regressions (ASan/UBSan)"; NATIVE_HOST_BIN=/tmp/native-host python3 -W ignore tests/test_fuzz.py 2>&1 | tail -3
echo "== trust-boundary CIDR code vs Python ipaddress oracle"; rm -f /tmp/cidr_driver; python3 -W ignore tests/test_cidr.py 2>&1 | tail -3
echo "== garlic backend harness (real bundle + engine overlay vs SDK fault stubs)"
python3 tools/prepare_engine.py >/dev/null
ST="$A/tests/sdk_stubs"; mkdir -p /tmp/gh_scratch
clang++ -std=c++17 -O1 -g -Wall -Wno-deprecated-declarations "${INC[@]}" -I"$ST" -Ibuild/engine -c src/garlic_backend.cpp -o /tmp/gb.o
for f in engine tokenizer; do clang -std=gnu11 -O1 -g -funwind-tables -Wno-everything -I"$ST" -Ibuild/engine -c build/engine/$f.c -o /tmp/$f.o; done
clang++ -std=c++17 -O1 -g -Wno-deprecated-declarations -funwind-tables "${INC[@]}" -I"$ST" -Ibuild/engine tests/garlic_host.cpp /tmp/gb.o /tmp/engine.o /tmp/tokenizer.o -lcrypto -lm -pthread -o /tmp/garlic_host
if [ -d "$R/ps4-worker/build/bundle-9b" ]; then /tmp/garlic_host "$R/ps4-worker/build/bundle-9b" "$R/ps4-worker/trusted-assets.json" /tmp/gh_scratch 2>&1 | grep -E "^(discover|load|tampered|PASS)"; else echo "(bundle not present: skipped)"; fi
echo "== agents: round-robin admission, one speaker per console"; NATIVE_HOST_BIN=/tmp/native-host python3 -W ignore "$A/tests/test_agents.py" 2>&1 | tail -3
echo "ALL HOST CHECKS PASSED"
