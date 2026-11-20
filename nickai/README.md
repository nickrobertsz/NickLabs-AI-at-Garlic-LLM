# NickLabs AI on Garlic LLM

Turn your jailbroken PlayStation 4 consoles into local AI servers. Each console runs a Qwen3.5 model on its own GPU (through the [Garlic LLM](https://github.com/leonardo-alegria/garlic-llm) engine);
a small **hub** on your computer gives you one dashboard, one terminal command and one address that speaks **Ollama, OpenAI and Claude CLI** to all of them.

* **Any number of consoles.** Plug in a third one and it is found automatically.
* **Many agents on one model.** Consoles speak in parallel; one console speaks for one agent at a time and rotates fairly between waiting agents.
* **Your Ollama models, no re-download.** `qwen3.5` models already on your computer are converted and sent to the console you pick.
* **Load / unload / switch models** from the dashboard or the terminal.
* **Permanent logs** on your computer and a one-click, privacy-scrubbed **support bundle**.
* **Linux, macOS and Windows** (Docker).

## Quick start

1. Make sure each PlayStation 4 is on the same network as your computer, jailbroken (GoldHEN) with its **FTP server enabled** (port 2121).
2. Get this folder and run the launcher:

   | Linux / macOS | Windows |
   |---|---|
   | `./launch.sh` | double-click `launch.bat` (or run it in PowerShell / CMD) |

   It checks Docker (and offers to install or update it), starts the hub, finds your Ollama models and your consoles, and opens the dashboard.
3. For a console that does not have the app yet, the launcher sends the installer to it and tells you what to press on the PS4
   (*Settings → Debug Settings → Game → Package Installer → NickLabs AI on Garlic LLM*), then start the app.
4. Open the dashboard (`http://localhost:8080`), add a model on the **Models** page (or accept the offered Qwen3.5-4B), send it to a console and press **Load**.

Nothing asks you for keys on your own network.

## Where to go next

| I want to... | Read |
|---|---|
| use everything (dashboard, models, agents, tools) | [docs/USER-GUIDE.md](docs/USER-GUIDE.md) |
| connect the OpenAI CLI / Claude CLI / Ollama, create keys, give each agent its own key | [docs/USER-GUIDE.md#connect-your-tools](docs/USER-GUIDE.md#connect-your-tools) |
| run a campaign with several agents per console | [docs/AGENTS.md](docs/AGENTS.md) |
| know how Windows works (not tested by the author) | [docs/WINDOWS.md](docs/WINDOWS.md) |
| understand model size limits, the 27B question and splitting a model over consoles | [docs/LARGE-MODELS.md](docs/LARGE-MODELS.md) |
| something is wrong | [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) (and the **support bundle** on the Logs page) |
| see how it fits together / build the console app from source | [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), [docs/BUILDING.md](docs/BUILDING.md) |

## Terminal

```
./nlag status                      # consoles, loaded models, who is speaking     (Windows: nlag status)
./nlag setup                       # guided first-time setup
./nlag pull qwen3.5:4b --to ps4-1  # download from the Ollama registry, convert, send to a console
./nlag load qwen3.5-4b-ollama ps4-1
./nlag chat qwen3.5-4b-ollama "Hello" --agent narrator
./nlag logs bundle                 # support bundle
```

## Licence and credits

GPL-3.0, like Garlic LLM, which this project builds on and ships a modified copy of its engine inside the console app. See [CREDITS.md](CREDITS.md). Not affiliated with Sony or Alibaba;
use only on consoles and with software you are entitled to run.
