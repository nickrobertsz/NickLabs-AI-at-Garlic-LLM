# NickLabs AI on Garlic LLM: user guide

## 1. What you get

```
 your tools ──► hub (on your computer) ──► console 1 ─ app ─ Qwen3.5 on the PS4 GPU
 OpenAI CLI/SDK   dashboard, terminal,   ├► console 2 ─ app ─ Qwen3.5 on the PS4 GPU
 Claude CLI       model manager,         └► console N ...
 Ollama clients   agent scheduler,
 your scripts     permanent logs
```

* The **app** (title *NickLabs AI on Garlic LLM*) runs on each PlayStation 4 and serves Ollama, OpenAI and Anthropic (Claude CLI) APIs itself, on port 11434.
* The **hub** runs in Docker on your computer. It is optional for a single console (you can talk to the console directly) but it is what gives you the dashboard, N consoles behind one address,
  agents, model import from Ollama, key management and the permanent log.
* A console keeps **one model in GPU memory at a time** (about 4.6 GB of weights). Different consoles run different models, or the same one, at the same time.

## 2. Install and first start

Requirements: a computer on the same network as your consoles, Docker (the launcher installs it for you if you agree), and PlayStation 4 consoles that run GoldHEN with the **FTP server enabled** (port 2121).

| System | Start |
|---|---|
| Linux, macOS | `./launch.sh` |
| Windows | double-click `launch.bat` (works from PowerShell and CMD too). Details: [WINDOWS.md](WINDOWS.md) |
| no Docker | `./run-without-docker.sh` / `run-without-docker.ps1` (needs Python 3.10+) |

The launcher: checks Docker and offers to install/update it → updates the project when it is a git checkout → finds your Ollama models folder → detects your network → builds and starts the hub → opens the dashboard →
runs the setup wizard when no console is registered. Stop with `./launch.sh --stop` (Windows: `launch.bat -Stop`). Your data (logs, converted models, settings) lives in the `data/` folder next to the launcher.

### Consoles are found automatically
The hub searches your network every minute for consoles that run the app and **registers them by itself**: no IP address to type, no keys. A console that is on and has the jailbreak's FTP and log servers
open but not the app yet shows up on the **Consoles** page as a *possible PlayStation 4*; press **Add this console**. You can still add one by address (**Add a console**). If you remove a console, discovery does not
add it back until you press **Re-add consoles I removed** (terminal: `nlag consoles forget`; `nlag consoles scan` searches right now).

### Installing the app on a console
On a console card press **More → Send app package** (terminal: `nlag consoles install ps4-1`). The package is copied to the console and verified byte for byte. Then, on the PlayStation 4:
*Settings → Debug Settings → Game → Package Installer → NickLabs AI on Garlic LLM → install*, and start the app from the home screen (it shows no picture; the dashboard card turns green).
Installing over an older version keeps your models and settings. Do not install anything while a generation is running.

## 3. The dashboard

| Page | What it does |
|---|---|
| **Consoles** | one card per console: state, loaded model, speed, GPU memory, who is speaking, who waits. Choose a model and press **Load**; **Unload** frees the GPU memory. *More*: check connection, send the app, remove. |
| **Models** | the models on your computer; **add a model** from your Ollama, the Ollama registry or HuggingFace; send a model to the console you choose; job progress. |
| **Playground** | any number of chats side by side. Each chat picks a console and speaks as its own agent. Warnings explain what will happen when two chats share a model. |
| **Agents** | who is speaking on each console, who waits, recent agents. |
| **Logs** | the permanent log files (live view) and the support bundle. |
| **Settings** | connect your tools (OpenAI CLI, Claude CLI, Ollama), keys, networks, discovery, agent routing, log retention. |

### Loading and unloading
* **Load** a model that is not loaded: the app restarts into it (a few seconds) and loads it (20 s – 2 min; the first load of a new model also checks its hash, a minute or two longer).
* **Unload**: the app restarts empty, which is the only way to give the GPU memory back. It takes seconds.
* Asking for another model on a loaded console from the terminal or an API also switches it (`model@console`).
* The first request for a model nobody has loaded may load it on an **idle** console automatically (Settings → *Loading models on demand*); a console that is busy with another model is never taken over: you get a clear message instead.

## 4. Models

### Which models work
The Garlic engine runs the **Qwen3.5** family. A console holds the whole model in GPU memory, so a model must be about **4.6 GB or less** of 4-bit weights:

| Model | Works | Notes |
|---|---|---|
| Qwen3.5-4B | yes | tested on real consoles, ~40 tokens/s |
| Qwen3.5-0.8B and 2B | **no** | their weight matrices (8224 rows) do not fit the console's 64-row tile layout; the hub refuses them with that reason |
| Qwen3.5-9B | the build shipped with the app | one converted by you without the speed-up head is larger than a console can hold; see [LARGE-MODELS.md](LARGE-MODELS.md) |
| Qwen3.5-27B and larger | **not yet** | needs more memory than one console has; the hub refuses it with an explanation. See [LARGE-MODELS.md](LARGE-MODELS.md) |
| other families (Llama, Mistral, Gemma...) | no | the engine is Qwen3.5 only |

### Use the models you already have in Ollama
Models Ollama downloaded on this computer appear on **Models → From your Ollama** (the launcher mounts your Ollama folder read-only). Press **Convert and use**, choose the target console(s) in *Send to*, and the hub reads the GGUF,
converts it to the console format (a few minutes, no re-download, about 2 GB of temporary space) and uploads it. Terminal: `nlag pull ollama-local:qwen3.5:4b --to ps4-1`.
Ollama's 4-bit "K" quantization is converted to the console's 4-bit format, so quality is close to, but not identical with, converting the original weights.

### Download a model
* **Ollama registry** (no Ollama needed): `nlag pull qwen3.5:4b` or the dashboard's *Download an Ollama model*.
* **HuggingFace** original weights (best quality, larger download): `nlag pull hf:Qwen/Qwen3.5-4B`.
* **Your own file**: put a `.gguf` (or a HuggingFace folder) in the `import/` folder and `nlag pull path:/import/your-model.gguf`.

Models land in your **library** (`data/library/`). **Send** puts one on a console you choose (several consoles are fine); the console verifies the files against pinned hashes before it runs them. Delete a model from a console with
`nlag rm MODEL CONSOLE`, from your computer with `nlag rm MODEL`.

## 5. Agents (several characters on one model)

An *agent* is anything that talks: a campaign character, a tool, a script. Name it with the `X-Agent-Id` header (or the OpenAI `user` field, or Anthropic `metadata.user_id`). Rules:

* **Consoles run in parallel.** Two consoles speak at the same moment.
* **One agent per console at a time.** A console never speaks for two agents at once.
* **Fair rotation.** Agents waiting for a console are served round-robin (the one served longest ago first), so a talkative agent cannot starve the others.
* **Sticky consoles.** With several consoles serving the same model, an agent keeps its console (its conversation stays warm in that console's cache); new agents go to the least busy one. (Settings → *Agent routing*.)
* Three agents on each of two consoles is simply six agent names: three queue behind one console, three behind the other. See [AGENTS.md](AGENTS.md).

## 6. Connect your tools

**On your own network no key is needed.** Wherever a tool insists on an API key, type any text, for example `local`. The dashboard shows ready-to-paste commands: **Settings → Connect your tools** (tabs *OpenAI CLI / SDK*, *Claude CLI*, *Ollama*, *curl*),
already filled with this hub's address and your model.

| Tool | Setting |
|---|---|
| OpenAI CLI / SDK | `OPENAI_BASE_URL=http://<hub>:8080/v1` and `OPENAI_API_KEY=local` |
| Claude CLI | `ANTHROPIC_BASE_URL=http://<hub>:8080`, `ANTHROPIC_AUTH_TOKEN=local`, `ANTHROPIC_MODEL=<model>` (+ the `ANTHROPIC_DEFAULT_*_MODEL` variables, `MAX_THINKING_TOKENS=0`) |
| Ollama CLI / clients | `OLLAMA_HOST=http://<hub>:8080` |
| a specific console | add `@<console id>` to the model: `qwen3.5-4b-ollama@ps4-1` |

`<hub>` is the address of the computer that runs the hub (`localhost` on that computer; its network address from any other).

### Keys (optional)
Create a key in **Settings → Create a key** when you want one per tool, or a key that **always speaks as one agent**:

* *Name* tells you which tool uses it. *Only for this agent* (optional) ties the key to an agent: whatever the client sends, its requests are attributed to that agent, so a campaign character cannot speak as another.
* The key (`nlag-…`) is shown **once**; only its hash is stored. Revoke it any time from the table.
* A key may use the AI endpoints from anywhere, but cannot manage the hub.
* **Require keys** (Settings → Network and access) makes every tool present a key even on your own network. It needs an **admin key** (also in Settings) because the dashboard itself then needs one. Ollama's own CLI cannot send a key, so leave *require keys* off if you use it.
* Outside your trusted networks, tools need a key, and the dashboard needs the admin key. Trusted by default: loopback and the private ranges (10.x, 172.16-31.x, 192.168.x). Change them in Settings.

## 7. Terminal control

`./nlag COMMAND` on Linux/macOS, `nlag COMMAND` on Windows (they run inside the hub container, so Docker is all you need). `nlag --help` lists everything:

```
nlag status                                    consoles, models, speakers
nlag setup                                     guided setup (add a console, send the app, add a model)
nlag consoles [add IP | remove ID | check ID | install ID | scan | forget]
nlag models | nlag ollama                      what is where
nlag pull SOURCE [--to ps4-1,ps4-2] [--name N] download/convert/send (qwen3.5:4b | hf:Qwen/Qwen3.5-4B | ollama-local:NAME | path:/import/x.gguf)
nlag push MODEL CONSOLE | nlag load MODEL CONSOLE | nlag unload CONSOLE | nlag rm MODEL [CONSOLE]
nlag jobs [watch ID | cancel ID]
nlag chat MODEL[@CONSOLE] "text" [--agent NAME]
nlag agents
nlag logs [FILE] [-n 200] | nlag logs bundle [--no-redact] [-o FILE]
nlag settings [key=value ...]
```

## 8. Logs and support

The hub **always** records, on your computer (`data/logs/`, rotated daily, kept 30 days / 500 MB by default):

* `<console>/klog-DATE.log`: everything the console prints (system, kernel, app),
* `<console>/events-DATE.log`: the important subset (app start/stop, crashes, GPU, memory),
* `<console>/boot.log`, `native.log`: the app's own journals, copied continuously (they survive the console overwriting them), `api.jsonl`, `coredumps/`,
* `hub/hub-DATE.log`: what the hub did (consoles online/offline, loads, jobs, discovery).

A console's log server serves **one reader**. Do not run another log tool against the same console (a warning appears on the Logs page if another program competes).

**Support bundle**: Logs page → *Download support bundle* (or `nlag logs bundle`). One zip with the logs, the settings, the state and system info. IP addresses, MAC addresses, tokens, account ids and your home folder are replaced by placeholders (`<ip-1>`...).
Read it before you publish it; send it to support or give it to your AI agent and ask why something failed.

## 9. Updating, stopping, removing

* Update: `./launch.sh --update` (Windows: `launch.bat -Update`). A git checkout is updated with `git pull` first. The app on the consoles is updated by sending the new package (*Send app package*) and installing it again.
* Stop: `./launch.sh --stop`. Remove everything: stop, delete the folder (your `data/` goes with it), and uninstall the app on the console (Settings → Storage → Applications).

## 10. Security notes

The hub and the consoles are meant for a private network. By default anyone on your private network can use and manage the hub. Do not expose port 8080 or 11434 to the internet. For a shared network: set an admin key, turn on *Require keys*, narrow the trusted networks.
Only the console app's own folders are written on a console (`/data/nicklabs-native`, `/data/pkg`). Models are checked against pinned SHA-256 hashes before the GPU runs them.

## 11. Limits you should know

* One model per console, at most ~4.6 GB of weights; Qwen3.5 only; context 16K tokens (the engine's limit for these builds).
* Sampling is temperature and seed (other options are accepted and ignored); no embeddings; `pull`/`create` on the Ollama API are not supported (use the hub).
* A console answers one request at a time.
* Speeds (measured): about 40 tokens/s for the 4B, 35 tokens/s for the 9B.
* Windows and macOS launchers are untested by the author: see [WINDOWS.md](WINDOWS.md) and please report what you find.
