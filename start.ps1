# FastAPI single server launcher (Windows)
# バックエンドはこのコンソールの中で動かす。ウィンドウを閉じるか Ctrl+C で停止する。
$ErrorActionPreference = "Stop"
$projectRoot = $PSScriptRoot
$backendPath = Join-Path $projectRoot "backend"
$port = 8010
$url = "http://localhost:$port"

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
    Start-Process $url
    Read-Host "Enter で閉じます"
    exit 0
}

# 起動できたら既定のブラウザを開く。サーバーの待ち受けを待ってから開く
$openBrowser = Start-Job -ScriptBlock {
    param($url, $port)
    for ($i = 0; $i -lt 60; $i++) {
        if (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue) {
            Start-Process $url
            return
        }
        Start-Sleep -Milliseconds 500
    }
} -ArgumentList $url, $port

Write-Host "[2/2] Starting Backend (:$port)..." -ForegroundColor Green
Write-Host ""
Write-Host "PC/Browser: $url" -ForegroundColor Yellow
Write-Host "iPhone (LAN): http://<PC-IP>:$port" -ForegroundColor Yellow
Write-Host "停止するには、このウィンドウで Ctrl+C を押すか、ウィンドウを閉じてください。" -ForegroundColor Gray
Write-Host ""

Push-Location $backendPath
try {
    # 前面で実行するので、ログはこのウィンドウに出続ける。終了時はウィンドウを閉じずに待つ
    & $backendPython -m uvicorn app.main:app --host 0.0.0.0 --port $port --app-dir src
    $exitCode = $LASTEXITCODE
}
finally {
    Pop-Location
    Remove-Job -Job $openBrowser -Force -ErrorAction SilentlyContinue
}

Write-Host ""
Write-Host "=== Backend stopped (exit $exitCode) ===" -ForegroundColor Cyan
Read-Host "Enter で閉じます"
