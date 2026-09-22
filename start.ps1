# FastAPI single server launcher (Windows)
$ErrorActionPreference = "Stop"
$projectRoot = $PSScriptRoot
$backendPath = Join-Path $projectRoot "backend"
$port = 8010

Write-Host "=== Starting ===" -ForegroundColor Cyan

Write-Host "[1/2] Syncing Backend dependencies..." -ForegroundColor Green
Push-Location $backendPath
try {
    uv sync
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to sync Backend dependencies."
    }
}
finally {
    Pop-Location
}

$backendPython = Join-Path $backendPath ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $backendPython -PathType Leaf)) {
    throw "Backend Python environment not found."
}

$running = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
if ($running) {
    Write-Host "Port $port is already in use. Backend is probably running." -ForegroundColor Yellow
    Start-Process "http://localhost:$port"
    exit 0
}

Write-Host "[2/2] Starting Backend (:$port)..." -ForegroundColor Green
Start-Process `
    -FilePath $backendPython `
    -ArgumentList "-m uvicorn app.main:app --host 0.0.0.0 --port $port --app-dir src" `
    -WorkingDirectory $backendPath `
    -WindowStyle Hidden

Start-Sleep -Seconds 2
Start-Process "http://localhost:$port"
Write-Host ""
Write-Host "=== Started ===" -ForegroundColor Cyan
Write-Host "PC/Browser: http://localhost:$port" -ForegroundColor Yellow
Write-Host "iPhone (LAN): http://<PC-IP>:$port" -ForegroundColor Yellow
Write-Host "Backend runs in background. Close it via Task Manager or stop.ps1." -ForegroundColor Gray