# Run the hub directly with Python (no Docker). Needs Python 3.10+ (https://www.python.org/downloads/ , tick "Add to PATH"). Data goes to .\data.
$ErrorActionPreference = 'Stop'
Set-Location -Path $PSScriptRoot
$py = (Get-Command python -ErrorAction SilentlyContinue); if (-not $py) { $py = Get-Command py -ErrorAction SilentlyContinue }
if (-not $py) { Write-Host 'Python 3.10 or newer is required: https://www.python.org/downloads/' -ForegroundColor Red; exit 1 }
if (-not (Test-Path '.venv')) { & $py.Source -m venv .venv }
& .\.venv\Scripts\python.exe -m pip install -q -r hub\requirements.txt
New-Item -ItemType Directory -Force -Path data, import | Out-Null
$env:NLAG_DATA = "$PSScriptRoot\data"; $env:NLAG_ASSETS = "$PSScriptRoot\hub\assets"; $env:NLAG_DIST = "$PSScriptRoot\hub\dist"; $env:PYTHONPATH = "$PSScriptRoot\hub"
$port = if ($env:NLAG_PORT) { $env:NLAG_PORT } else { '8080' }
Write-Host "Hub: http://localhost:$port   (terminal: set NLAG_URL=http://localhost:$port ; python -m nlag_hub.cli status)"
Start-Process "http://localhost:$port"
& .\.venv\Scripts\python.exe -m nlag_hub --port $port
