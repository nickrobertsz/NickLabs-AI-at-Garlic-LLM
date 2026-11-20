# Troubleshooting

Always start with: **Logs → Download support bundle** (or `nlag logs bundle`). Everything below can be answered from it.

| Symptom | Likely cause | What to do |
|---|---|---|
| No console is found automatically | the console is off / not on the same network; the app is not running there; the launcher could not detect your network | add it by IP (**Add a console**); check the card's *More → Check connection*; `nlag consoles scan` |
| *FTP not reachable* when adding | the jailbreak's FTP server is not running | enable the FTP server on the console (GoldHEN, port 2121) |
| *app not running yet* | the app is not installed or not started | *More → Send app package*, install it with Package Installer, start it |
| Card says **offline** though the app runs | network hiccup (a console's network re-initialises sometimes); the app froze | wait a minute; fully close the app (Options → Close Application) and start it again |
| Load takes very long the first time | the app hashes the weights once (1-3 min for a large model) | wait; later loads are faster |
| *model not loaded on any console* (HTTP 409) | every console is busy with another model | press Load on a console, or address one (`model@ps4-1`) |
| *bundle_invalid* | files changed or the transfer was incomplete | send the model again (it resumes) |
| *not enough memory / model too large* | weights above ~4.6 GB | pick a smaller model, see [LARGE-MODELS.md](LARGE-MODELS.md) |
| Log page: *another program is reading this console's log port* | two programs read the console log (it allows one) | close the other log tool / second hub |
| The dashboard asks for a key | you opened it from outside the trusted networks, or *Require keys* is on | enter the admin key, or add your network in Settings (from a trusted computer) |
| `nlag` says *cannot reach the hub* | the hub container is not running | `./launch.sh` |
| Converting an Ollama model fails: *architecture ... not supported* | only Qwen3.5 runs on the consoles | use a Qwen3.5 model |

Hub messages: `docker compose logs hub` (or the terminal where `run-without-docker` runs). Console messages: Logs page → `<console>/events-*.log` and `boot.log`.
