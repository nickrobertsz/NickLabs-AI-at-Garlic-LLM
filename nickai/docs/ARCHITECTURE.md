# Architecture

```
 browser / nlag CLI / OpenAI, Claude, Ollama clients
              │  HTTP :8080
        ┌─────▼──────────────────────────── hub (Docker, Python/aiohttp) ───────────────────────────┐
        │ guard (trusted networks, keys)  → compat API  /api /v1  → Fleet: routing, per-console       │
        │ dashboard + /hub/v1/* management            agent scheduler (round-robin), streaming proxy   │
        │ discovery (LAN scan) · model manager (Ollama/HF → convert → send) · permanent capture        │
        └───┬───────────────┬───────────────────────┬───────────────────────────────────────────────┘
            │ HTTP :11434   │ FTP :2121             │ TCP :3232 (klog)
        console app      package + model upload    live system log
```

* **Console app** (`app/`, C++ on the OpenOrbis toolchain): HTTP server, Ollama/OpenAI/Anthropic APIs, per-agent fair admission to the single GPU, model discovery/validation (pinned SHA-256), load/unload/switch (the app relaunches itself to free GPU memory),
  Garlic's GPU engine (overlaid unmodified except for memory/fault hooks).
* **Hub** (`hub/nlag_hub/`): `core` (registry, settings), `fleet` (poller, gate, router, proxy), `discovery`, `models` + `sourcing` + `convert/` (GGUF/safetensors → console format), `ftp` (package and model upload), `capture` + `support` (logs, bundle), `keys`, `server`, `cli`, `static/` (dashboard).
* **Ports**: hub 8080 (published), consoles 11434 (app), 2121 (FTP), 3232 (klog).
* **Data** (`data/`): `consoles.json`, `settings.json`, `keys.json` (hashes only), `jobs.json`, `library/` (converted models), `cache/` (downloads), `logs/`.

Model conversion: tensors are read from the source (HuggingFace safetensors or an Ollama GGUF: K-quants are dequantized, llama.cpp's V-head reordering, RMSNorm +1 and A = -exp(A_log) conventions are undone), quantized to Garlic's q4 with the engine's own quantizer, relaid out as q4i, and described by a `model.idx`
that is byte-identical to the one Garlic ships for Qwen3.5-4B (the check that the layout is right).
