# Windows

The Windows version uses the same hub (in Docker) as Linux. **The author could not test it on Windows**: this page says what should happen, what to check, and which fallback to use. Please report what you find (attach the support bundle).

## Easiest path

1. Windows 10/11 64-bit, with virtualization enabled in the BIOS and WSL 2 (Docker Desktop sets it up).
2. Double-click **`launch.bat`**. It runs `launch.ps1` with PowerShell, *for this script only* (no system policy is changed). Works the same when you start it from **PowerShell** or **CMD**; you do not need XAMPP, WSL commands or a web server.
3. If Docker Desktop is missing, the launcher offers `winget install Docker.DockerDesktop` (asks first). A restart and Docker Desktop's first-run terms may be needed; run `launch.bat` again afterwards.
4. The dashboard opens at `http://localhost:8080`. Terminal control: `nlag status`, `nlag setup`, ... (`nlag.cmd` in the same folder; run it from that folder, or add the folder to PATH).

## Without Docker

If Docker Desktop cannot run (Windows Home without WSL 2, virtualization off, company policy): install Python 3.10+ ("Add to PATH"), then run **`run-without-docker.ps1`** (right-click → *Run with PowerShell*, or `powershell -ExecutionPolicy Bypass -File run-without-docker.ps1`).
It creates a virtual environment, installs the three Python packages and starts the same hub. Terminal: `set NLAG_URL=http://localhost:8080` then `python -m nlag_hub.cli status` (in the `hub` folder with the virtual environment active).

## Things that are different on Windows

* **Finding consoles**: the launcher detects your network (`Get-NetIPConfiguration`) and tells the hub which network to scan. If auto-discovery finds nothing, add the console by its IP in the dashboard.
* **Firewall**: Windows Defender Firewall may ask whether Docker/Python may accept network connections: allow **private networks** so other computers can reach the hub (port 8080). The consoles are contacted outbound, which is normally allowed.
* **Your Ollama models**: Ollama on Windows keeps them in `%USERPROFILE%\.ollama\models`; the launcher finds that folder and mounts it read-only. If you moved it, set `OLLAMA_MODELS_DIR` in `.env` (use forward slashes, e.g. `D:/ollama/models`).
* **Network trust**: with Docker Desktop, requests from other computers can appear to come from Docker's internal address. That address is inside the default trusted networks (172.16.0.0/12), so no key is asked on your private network either way.
* **Files**: `data\` (logs, models) lives next to the launcher. Docker Desktop bind mounts are slower than Linux; converting a model can take longer.
* **Line endings**: if you edit the `.sh` files on Windows, keep LF line endings.

## If something fails

1. `docker compose logs hub` (in the folder) shows the hub's own messages.
2. `launch.bat` prints what it was doing; copy that text.
3. Dashboard → **Logs** → *Download support bundle*.
