#!/usr/bin/env bash
# NickLabs AI on Garlic LLM - one-command launcher for Linux and macOS.
#   ./launch.sh            start (or update and start) everything, then guide the first-time setup
#   ./launch.sh --stop     stop the hub        ./launch.sh --update   force an update and rebuild
# What it does: checks Docker (installs it, with your OK, if missing; offers an update if it is old), builds and starts the hub container,
# finds your Ollama models, opens the dashboard, and runs the console setup wizard (FTP, sending the app, adding a model).
set -u
cd "$(cd "$(dirname "$0")" && pwd)"
say()  { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m  %s\n' "$*"; }
die()  { printf '\033[1;31mxx\033[0m  %s\n' "$*" >&2; exit 1; }
ask()  { local a; read -r -p "$1 [Y/n] " a || a=n; [[ -z "$a" || "$a" =~ ^[Yy] ]]; }
OS="$(uname -s)"
SUDO=""; [ "$(id -u)" -ne 0 ] && command -v sudo >/dev/null 2>&1 && SUDO="sudo"

if [ "${1:-}" = "--stop" ]; then docker compose down; exit 0; fi
FORCE_UPDATE=0; [ "${1:-}" = "--update" ] && FORCE_UPDATE=1

# ---- 1. Docker --------------------------------------------------------------------------------------------------------------------------
have_docker() { command -v docker >/dev/null 2>&1; }
compose_ok()  { docker compose version >/dev/null 2>&1; }
install_docker() {
  case "$OS" in
    Linux)
      say "Docker is not installed. I can install it with Docker's official script (needs sudo)."
      ask "Install Docker now?" || die "Docker is required. Install it from https://docs.docker.com/engine/install/ and run ./launch.sh again."
      command -v curl >/dev/null 2>&1 || die "curl is needed to download the installer (install curl first)."
      curl -fsSL https://get.docker.com -o /tmp/get-docker.sh || die "could not download the Docker installer"
      $SUDO sh /tmp/get-docker.sh || die "the Docker installer failed"
      $SUDO systemctl enable --now docker 2>/dev/null || true
      if ! id -nG "$USER" | grep -qw docker; then $SUDO usermod -aG docker "$USER" 2>/dev/null && warn "added you to the docker group: log out and in again if the next step says 'permission denied'"; fi ;;
    Darwin)
      if command -v brew >/dev/null 2>&1 && ask "Install Docker Desktop with Homebrew?"; then brew install --cask docker || die "brew could not install Docker Desktop"
      else die "Install Docker Desktop from https://www.docker.com/products/docker-desktop/ then run ./launch.sh again."; fi ;;
    *) die "Unsupported system $OS. Use Linux, macOS, or launch.bat on Windows." ;;
  esac
}
if ! have_docker; then install_docker; fi
have_docker || die "Docker still not found on PATH."
DOCKER="docker"
if ! docker info >/dev/null 2>&1; then
  if [ "$OS" = "Darwin" ]; then say "Starting Docker Desktop..."; open -a Docker; else say "Starting the Docker service..."; $SUDO systemctl start docker 2>/dev/null || true; fi
  for i in $(seq 1 60); do docker info >/dev/null 2>&1 && break; sleep 2; done
  if ! docker info >/dev/null 2>&1; then
    if $SUDO docker info >/dev/null 2>&1; then DOCKER="$SUDO docker"; warn "using sudo for docker (log out/in to use it without sudo)"; else die "Docker is installed but not running (or you are not allowed to use it). Start Docker and run ./launch.sh again."; fi
  fi
fi
compose_ok || [ "$DOCKER" != "docker" ] || die "Docker Compose v2 is missing. Update Docker (https://docs.docker.com/compose/install/) and run ./launch.sh again."
VER="$($DOCKER version --format '{{.Server.Version}}' 2>/dev/null || echo 0)"; MAJOR="${VER%%.*}"
say "Docker $VER is ready."
if [ "${MAJOR:-0}" -lt 24 ] 2>/dev/null; then
  warn "Your Docker is old ($VER). Newer is recommended."
  if [ "$OS" = "Linux" ] && ask "Update Docker now with the official script (needs sudo)?"; then curl -fsSL https://get.docker.com | $SUDO sh || warn "the update failed; continuing with the current version"; fi
fi

# ---- 2. update this project --------------------------------------------------------------------------------------------------------------
if [ -d .git ] || [ -d ../.git ]; then
  if git pull --ff-only >/dev/null 2>&1; then say "Project is up to date."; else warn "could not update the project with git (local changes?): continuing with what is here."; fi
fi

# ---- 3. settings: Ollama models folder ----------------------------------------------------------------------------------------------------
mkdir -p data import/no-ollama
[ -f .env ] || cp .env.example .env
if ! grep -q '^OLLAMA_MODELS_DIR=.\+' .env; then
  for d in "${OLLAMA_MODELS:-}" "$HOME/.ollama/models" /usr/share/ollama/.ollama/models /var/lib/ollama/.ollama/models; do
    if [ -n "$d" ] && [ -d "$d/manifests" ]; then sed -i.bak "s#^OLLAMA_MODELS_DIR=.*#OLLAMA_MODELS_DIR=$d#" .env && rm -f .env.bak; say "Found your Ollama models: $d"; break; fi
  done
fi
if [ "$OS" = "Linux" ]; then
  for kv in "NLAG_UID=$(id -u)" "NLAG_GID=$(id -g)"; do k="${kv%%=*}"; if grep -q "^$k=" .env; then sed -i "s#^$k=.*#$kv#" .env; else echo "$kv" >> .env; fi; done
fi
set -a; . ./.env; set +a
PORT="${NLAG_PORT:-8080}"
# this computer's LAN address: consoles are searched on its network, and the dashboard examples show it
LAN_IP=""
if command -v ip >/dev/null 2>&1; then LAN_IP="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src") {print $(i+1); exit}}')"; fi
[ -z "$LAN_IP" ] && [ "$OS" = "Darwin" ] && LAN_IP="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null)"
if [ -n "$LAN_IP" ]; then
  LAN_NET="${LAN_IP%.*}.0/24"
  setenv() { if grep -q "^$1=" .env; then sed -i.bak "s#^$1=.*#$1=$2#" .env && rm -f .env.bak; else echo "$1=$2" >> .env; fi; }
  setenv NLAG_SCAN_NETS "$LAN_NET"; setenv NLAG_PUBLIC_URL "http://$LAN_IP:$PORT"
  say "Your network: $LAN_NET (consoles on it are found automatically)."
  set -a; . ./.env; set +a
else warn "could not detect this computer's network address: add consoles by IP in the dashboard."; fi

# ---- 4. start -----------------------------------------------------------------------------------------------------------------------------
say "Building and starting the hub (the first time takes a few minutes)..."
BUILD_ARGS="--build"; [ "$FORCE_UPDATE" = 1 ] && BUILD_ARGS="--build --pull always"
$DOCKER compose up -d $BUILD_ARGS || die "docker compose could not start the hub. Run: $DOCKER compose logs hub"
for i in $(seq 1 60); do curl -fsS "http://localhost:$PORT/healthz" >/dev/null 2>&1 && break; sleep 1; done
curl -fsS "http://localhost:$PORT/healthz" >/dev/null 2>&1 || die "the hub did not answer on port $PORT. Run: $DOCKER compose logs hub"
say "The hub is running: http://localhost:$PORT"
chmod +x nlag 2>/dev/null || true

# ---- 5. first-time setup ------------------------------------------------------------------------------------------------------------------
N="$(curl -fsS "http://localhost:$PORT/hub/v1/state" 2>/dev/null | python3 -c 'import sys,json;print(len(json.load(sys.stdin)["consoles"]))' 2>/dev/null || echo 0)"
if [ "${N:-0}" = "0" ]; then
  say "No console is registered yet. Starting the guided setup (you can also do this later with ./nlag setup)."
  if [ -t 0 ]; then $DOCKER compose exec hub python -m nlag_hub.cli setup || true; else warn "not an interactive terminal: run ./nlag setup later (consoles on your network are also added automatically)."; fi
fi
if command -v xdg-open >/dev/null 2>&1; then xdg-open "http://localhost:$PORT" >/dev/null 2>&1 || true; elif [ "$OS" = "Darwin" ]; then open "http://localhost:$PORT" || true; fi
say "Open http://localhost:$PORT  -  terminal control: ./nlag status | ./nlag --help  -  stop: ./launch.sh --stop"
