#!/usr/bin/env bash
# Run the hub directly with Python (no Docker). Needs Python 3.10+. Data goes to ./data like with Docker.
set -e
cd "$(cd "$(dirname "$0")" && pwd)"
PY="$(command -v python3 || command -v python)" || { echo "Python 3.10 or newer is required"; exit 1; }
[ -d .venv ] || "$PY" -m venv .venv
. .venv/bin/activate
pip install -q -r hub/requirements.txt
mkdir -p data import
export NLAG_DATA="$PWD/data" NLAG_ASSETS="$PWD/hub/assets" NLAG_DIST="$PWD/hub/dist" PYTHONPATH="$PWD/hub"
echo "Hub: http://localhost:${NLAG_PORT:-8080}   (terminal: NLAG_URL=http://localhost:${NLAG_PORT:-8080} python -m nlag_hub.cli status)"
exec python -m nlag_hub --port "${NLAG_PORT:-8080}"
