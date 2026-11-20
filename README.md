# NickLabs AI at Garlic LLM

**Your jailbroken PlayStation 4 consoles as local AI servers, behind one address that speaks Ollama, OpenAI and Claude CLI.**

[Get started](nickai/README.md) · [User guide](nickai/docs/USER-GUIDE.md) · [Report a problem](https://github.com/nickrobertsz/NickLabs-AI-at-Garlic-LLM/issues) · [Test status](https://github.com/nickrobertsz/NickLabs-AI-at-Garlic-LLM/actions)

> **Built on Garlic LLM, by Leonardo Alegría.** The engine that runs the model on the PS4 GPU is [Garlic LLM](https://github.com/leonardo-alegria/garlic-llm),
> which describes itself as *"A 9-billion-parameter language model running on a PS4. Offline."* Garlic LLM is his project, not ours, and NickLabs AI is not an official part of it.
> This repository is a fork of his: **our project is in [`nickai/`](nickai/)**. The other folders are his Garlic LLM source, kept so that our console app can be built against it (GPL-3.0).
> Please report problems with NickLabs AI [here](https://github.com/nickrobertsz/NickLabs-AI-at-Garlic-LLM/issues), not to Garlic LLM.

## What it is

Garlic LLM is a chat app you use on the console itself. NickLabs AI turns the console into a **network server**, so your tools on a computer can use the PS4's GPU:

```
 your tools ──► hub (on your computer) ──► console 1 ─ NickLabs AI app ─ Qwen3.5 on the PS4 GPU (Garlic engine)
 OpenAI CLI/SDK   dashboard, terminal,   ├► console 2 ─ NickLabs AI app ─ Qwen3.5 on the PS4 GPU
 Claude CLI       model manager,         └► console N ...
 Ollama clients   agent scheduler,
 your scripts     permanent logs
```

It has two parts:

* **The console app** (title `NLNA00001`, C++ on the OpenOrbis toolchain) runs on each PS4. It serves the Ollama, OpenAI and Anthropic APIs on port 11434 and runs the model through Garlic's GPU engine.
* **The hub** (Python, runs in Docker on your computer) puts any number of consoles behind one address (`http://localhost:8080`). It adds the dashboard, the `nlag` terminal command, model import, agent scheduling, keys and permanent logs.

## What it can do

* **Any number of consoles.** The hub searches your network and registers consoles that run the app on its own, with no IP addresses to type. A console with GoldHEN's FTP server but no app yet is offered as a *possible PlayStation 4*.
* **One address for your tools.** Ollama clients, the OpenAI CLI/SDK and the Claude CLI all point at the hub. On your own network no key is needed. Pick a console with `model@ps4-1`.
* **Many agents on one model.** Consoles speak in parallel. One console speaks for one agent at a time, and waiting agents are served round-robin so no agent starves another. An agent stays on its console to keep its conversation warm in that console's cache. See [AGENTS.md](nickai/docs/AGENTS.md).
* **Your Ollama models, no re-download.** `qwen3.5` models already on your computer are converted to the console format and sent to the console you choose. The hub can also download models from the Ollama registry or HuggingFace.
* **Load, unload and switch models** from the dashboard, the terminal or the API.
* **Permanent logs** on your computer (console system log, app journals, crash dumps, hub events). A one-click **support bundle** hides IP and MAC addresses, tokens, account ids and your home folder.
* **Optional keys.** You can give each tool its own key, or a key that always speaks as one agent. You can also require keys on a shared network.
* **One launcher.** `./launch.sh` (Linux and macOS) or `launch.bat` (Windows) checks Docker, starts the hub, finds your Ollama models and your consoles, sends the app to them and opens the dashboard.

Measured on real consoles: about **40 tokens/s** with Qwen3.5-4B and **35 tokens/s** with Qwen3.5-9B.

## What we implemented

All of this is NickLabs work and lives in [`nickai/`](nickai/):

| Part | Where | What |
|---|---|---|
| Console app | `nickai/app/` | HTTP server; Ollama, OpenAI and Anthropic APIs; fair admission of agents to the single GPU; model discovery and validation against pinned SHA-256 hashes; load, unload and switch (the app relaunches itself to free GPU memory); boot and API journals. Host tests use sanitizers, a mock GPU engine and fuzzers. |
| Hub | `nickai/hub/nlag_hub/` | Console registry and network discovery; fleet poller, router and streaming proxy; agent scheduler; model manager that converts GGUF or safetensors into Garlic's 4-bit format; FTP upload of packages and models; permanent log capture and support bundle; keys; dashboard; `nlag` CLI. |
| Launchers | `nickai/launch.*`, `nickai/run-without-docker.*`, `nickai/nlag*` | One-command start on Linux, macOS and Windows, with Docker or without. |
| Installer package | `nickai/hub/dist/` | `IV0000-NLNA00001_00-NICKLABSNATIVE01.pkg`, built from `nickai/app/` plus Garlic's engine. Checksum in [`nickai/SHA256SUMS`](nickai/SHA256SUMS). |
| Tests and CI | `nickai/tests/`, `.github/workflows/nickai-tests.yml` | Hub tests with fake consoles and a browser engine for the dashboard. They run on every push. |
| Docs | `nickai/docs/` | User guide, agents, Windows, large models, architecture, building, troubleshooting. |

## What we use from Garlic LLM

From Leonardo Alegría's [Garlic LLM](https://github.com/leonardo-alegria/garlic-llm) (GPL-3.0), unchanged except where noted:

* **The GPU inference engine** (`chat/engine.c`). The console app includes it with small added hooks for memory and fault handling, and otherwise unmodified.
* **The compiled GPU kernels and the tokenizer** (`nickai/hub/assets/kernels.bin`, `tokenizer.bin`).
* **The 4-bit quantization format and its quantizer.** Our converter uses the engine's own quantizer and writes a `model.idx` that is byte-identical to the one Garlic ships for Qwen3.5-4B.
* **The Qwen3.5-9B build** (mixed 4/3-bit with its speed-up head) that Garlic ships.

The full list, including Qwen3.5, the OpenOrbis toolchain, OpenSSL and the Python packages, is in [`nickai/CREDITS.md`](nickai/CREDITS.md).

## What has been tested, and what has not

**Verified by the author:**

* The Linux Docker flow on a clean machine: build, start, network detection, automatic console registration, and chat through the hub.
* Every feature on **two real PS4 consoles**.
* The hub tests (26 tests, also run by CI on every push) and the console app's host tests in this fork layout.

**Not tested yet. Reports are welcome, so please attach the support bundle from Dashboard → Logs:**

* **Windows**: `launch.bat`, `launch.ps1`, `run-without-docker.ps1`, `nlag.cmd`. See [WINDOWS.md](nickai/docs/WINDOWS.md).
* **macOS**: `launch.sh` on macOS.

**Not supported (known limits):**

* One model per console, at most about 4.6 GB of weights. Qwen3.5 only, with a 16K-token context.
* Qwen3.5-0.8B and 2B do not fit the console's tile layout. 27B and larger need more memory than one console has. Splitting one model across several consoles is not implemented yet; [LARGE-MODELS.md](nickai/docs/LARGE-MODELS.md) explains what it would take.
* Sampling supports temperature and seed only. No embeddings. A console answers one request at a time.

## Quick start

1. Each PS4 needs GoldHEN with its **FTP server enabled** (port 2121) and must be on the same network as your computer.
2. Run `./launch.sh` in `nickai/` (Windows: double-click `nickai\launch.bat`).
3. Install the app on each console when the launcher asks you to, open `http://localhost:8080` and load a model.

The full steps are in [`nickai/README.md`](nickai/README.md) and the [user guide](nickai/docs/USER-GUIDE.md).

## Licence

GPL-3.0, the same as Garlic LLM ([LICENSE](LICENSE)). Not affiliated with Sony Interactive Entertainment, Alibaba, Docker or Ollama. Use this only on consoles and with software you are entitled to run.
