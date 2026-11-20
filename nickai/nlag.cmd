@echo off
rem Terminal control of NickLabs AI on Garlic LLM (Windows):  nlag status | nlag setup | nlag --help
rem Runs the command inside the hub container, so nothing but Docker Desktop is needed.
cd /d "%~dp0"
if /I "%~1"=="setup" (
  docker compose exec hub python -m nlag_hub.cli %*
) else (
  docker compose exec -T hub python -m nlag_hub.cli %*
)
