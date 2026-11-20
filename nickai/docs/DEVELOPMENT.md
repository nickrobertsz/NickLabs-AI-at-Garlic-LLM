# Development notes

## Layout
```
launch.sh / launch.ps1 / launch.bat    one-command launchers          nlag / nlag.cmd     terminal control (runs inside the hub container)
docker-compose.yml, .env.example       the hub service                run-without-docker.*  hub without Docker
hub/                                   Dockerfile, requirements, nlag_hub/ (the hub), assets/ (Garlic kernels + tokenizer), dist/ (installer package)
app/                                   the console app (C++): src/, tests/, tools/ (build, package, host tests), vendor/
tests/                                 hub tests (fake consoles, a browser engine for the dashboard)
docs/                                  guides
```

## Tests
* Hub: `python3 -m unittest discover -s tests` (needs `pip install -r hub/requirements.txt`; the dashboard test also needs Node.js and jsdom, and is skipped without them).
  The tests use fake consoles on localhost and never scan the network (`NLAG_DISCOVERY=0`, `NLAG_CAPTURE=0`).
* App: `app/tools/test.sh` (needs clang). It builds the app's HTTP server and API with sanitizers, runs a mock GPU engine, fuzzers and end-to-end API checks.
* On real consoles: load/unload/switch with `nlag load` / `nlag unload`, chat with `nlag chat`; a console's `/native/status` shows build, loaded model, agents and connection counters.

## Conventions worth keeping
* Nothing is hard-coded to a number of consoles or to particular addresses; ids come from the registry (`ps4-1`, `ps4-2`, ...).
* A console's log port allows ONE reader: only the hub's recorder (`capture.py`) may connect to it. Do not add "probes" that connect to port 3232.
* Uploads that can legitimately have the same size as the previous version (the installer package) must replace the file and be read back; model files resume by size and are validated by the app against pinned hashes.
* Console builds: no `thread_local` objects with destructors, no `std::chrono::steady_clock`, no directory listing, files the app creates are 0600.
* Contributions: do not commit addresses, names, account ids, console serial numbers, logs or model files. Run `python3 -m unittest discover -s tests` before sending a change.
