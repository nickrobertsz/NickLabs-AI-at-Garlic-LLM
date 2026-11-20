# NickLabs AI on Garlic LLM - launcher for Windows (PowerShell 5.1 or newer).
#   launch.bat            start (or update and start) everything, then guide the first-time setup
#   launch.bat -Stop      stop the hub          launch.bat -Update   force an update and rebuild
# Checks Docker Desktop (installs it with winget, with your OK, if missing), builds and starts the hub container, finds your Ollama models,
# opens the dashboard and runs the console setup wizard (FTP, sending the app, adding a model).
param([switch]$Stop, [switch]$Update)
$ErrorActionPreference = 'Continue'   # native commands (docker, git) write warnings to stderr: judge them by exit code, not by exceptions
Set-Location -Path $PSScriptRoot
function Say($m)  { Write-Host "==> $m" -ForegroundColor Green }
function Warn($m) { Write-Host "!!  $m" -ForegroundColor Yellow }
function Die($m)  { Write-Host "xx  $m" -ForegroundColor Red; exit 1 }
function Ask($q)  { $a = Read-Host "$q [Y/n]"; return ($a -eq '' -or $a -match '^[Yy]') }
function HaveDocker { return [bool](Get-Command docker -ErrorAction SilentlyContinue) }
function DockerUp { docker info *> $null; return ($LASTEXITCODE -eq 0) }

if ($Stop) { docker compose down; exit 0 }

# ---- 1. Docker Desktop -----------------------------------------------------------------------------------------------------------------
if (-not (HaveDocker)) {
  Say 'Docker Desktop is not installed.'
  if (-not (Get-Command winget -ErrorAction SilentlyContinue)) { Die 'Install Docker Desktop from https://www.docker.com/products/docker-desktop/ (it needs Windows 10/11 with WSL 2), then run launch.bat again. Without Docker you can use run-without-docker.ps1.' }
  if (-not (Ask 'Install Docker Desktop now with winget? (a restart may be needed afterwards)')) { Die 'Docker is required. Install Docker Desktop and run launch.bat again, or use run-without-docker.ps1.' }
  winget install -e --id Docker.DockerDesktop --accept-package-agreements --accept-source-agreements
  $env:Path = [System.Environment]::GetEnvironmentVariable('Path','Machine') + ';' + [System.Environment]::GetEnvironmentVariable('Path','User')
  if (-not (HaveDocker)) { Die 'Docker Desktop was installed. Restart Windows (or sign out and in), start Docker Desktop once, accept its terms, then run launch.bat again.' }
}
if (-not (DockerUp)) {
  Say 'Starting Docker Desktop...'
  $exe = Join-Path $env:ProgramFiles 'Docker\Docker\Docker Desktop.exe'
  if (Test-Path $exe) { Start-Process $exe }
  for ($i = 0; $i -lt 90; $i++) { if (DockerUp) { break }; Start-Sleep -Seconds 2 }
  if (-not (DockerUp)) { Die 'Docker Desktop is not running. Start it, wait until it says "running", then run launch.bat again.' }
}
$ver = (docker version --format '{{.Server.Version}}') 2>$null
Say "Docker $ver is ready."
try { if ([int]($ver.Split('.')[0]) -lt 24) { Warn 'Your Docker is old. Update it from Docker Desktop (Settings > Software updates) or: winget upgrade Docker.DockerDesktop'; if (Ask 'Update with winget now?') { winget upgrade -e --id Docker.DockerDesktop } } } catch {}

# ---- 2. update this project --------------------------------------------------------------------------------------------------------------
if ((Test-Path '.git') -or (Test-Path '..\.git')) { if (Get-Command git -ErrorAction SilentlyContinue) { git pull --ff-only *> $null; if ($LASTEXITCODE -eq 0) { Say 'Project is up to date.' } else { Warn 'could not update the project with git: continuing with what is here.' } } }

# ---- 3. settings: Ollama models folder ----------------------------------------------------------------------------------------------------
New-Item -ItemType Directory -Force -Path 'data','import','import\no-ollama' | Out-Null
if (-not (Test-Path '.env')) { Copy-Item '.env.example' '.env' }
$envText = Get-Content '.env' -Raw
if ($envText -notmatch '(?m)^OLLAMA_MODELS_DIR=.+') {
  $cands = @($env:OLLAMA_MODELS, (Join-Path $env:USERPROFILE '.ollama\models'), (Join-Path $env:LOCALAPPDATA 'Ollama\models'))
  foreach ($d in $cands) { if ($d -and (Test-Path (Join-Path $d 'manifests'))) { $fwd = $d -replace '\\','/'; $envText = $envText -replace '(?m)^OLLAMA_MODELS_DIR=.*', "OLLAMA_MODELS_DIR=$fwd"; Set-Content -Path '.env' -Value $envText -NoNewline; Say "Found your Ollama models: $d"; break } }
}
$port = 8080
foreach ($line in Get-Content '.env') { if ($line -match '^NLAG_PORT=(\d+)') { $port = [int]$Matches[1] } }
# this computer's LAN address: consoles are searched on its network, and the dashboard examples show it
try {
  $lan = (Get-NetIPConfiguration | Where-Object { $_.IPv4DefaultGateway -ne $null -and $_.NetAdapter.Status -eq 'Up' } | Select-Object -First 1).IPv4Address.IPAddress
  if ($lan) {
    $net = ($lan -replace '\.\d+$', '.0') + '/24'
    $t = Get-Content '.env' -Raw
    foreach ($kv in @(@('NLAG_SCAN_NETS', $net), @('NLAG_PUBLIC_URL', "http://${lan}:$port"))) {
      if ($t -match "(?m)^$($kv[0])=") { $t = $t -replace "(?m)^$($kv[0])=.*", "$($kv[0])=$($kv[1])" } else { $t += "`n$($kv[0])=$($kv[1])" }
    }
    Set-Content -Path '.env' -Value $t -NoNewline
    Say "Your network: $net (consoles on it are found automatically)."
  }
} catch { Warn 'could not detect this computer''s network address: add consoles by IP in the dashboard.' }

# ---- 4. start -----------------------------------------------------------------------------------------------------------------------------
Say 'Building and starting the hub (the first time takes a few minutes)...'
$composeArgs = @('compose','up','-d','--build'); if ($Update) { $composeArgs += @('--pull','always') }
& docker @composeArgs
if ($LASTEXITCODE -ne 0) { Die 'docker compose could not start the hub. Run: docker compose logs hub' }
$ok = $false
for ($i = 0; $i -lt 60; $i++) { try { Invoke-RestMethod -Uri "http://localhost:$port/healthz" -TimeoutSec 2 | Out-Null; $ok = $true; break } catch { Start-Sleep -Seconds 1 } }
if (-not $ok) { Die "The hub did not answer on port $port. Run: docker compose logs hub" }
Say "The hub is running: http://localhost:$port"

# ---- 5. first-time setup ------------------------------------------------------------------------------------------------------------------
$n = 0
try { $n = (Invoke-RestMethod -Uri "http://localhost:$port/hub/v1/state").consoles.Count } catch {}
if ($n -eq 0) { Say 'No console is registered yet. Starting the guided setup (you can also do this later with nlag setup).'; docker compose exec hub python -m nlag_hub.cli setup }
Start-Process "http://localhost:$port"
Say "Open http://localhost:$port  -  terminal control: nlag status | nlag --help  -  stop: launch.bat -Stop"
