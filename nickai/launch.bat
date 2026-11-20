@echo off
rem NickLabs AI on Garlic LLM - double-click launcher for Windows. It runs launch.ps1 (PowerShell) with the needed permission for this script only.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0launch.ps1" %*
if errorlevel 1 pause
