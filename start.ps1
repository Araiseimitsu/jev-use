# FastAPI 単一サーバーの起動スクリプト（Windows）
$ErrorActionPreference = "Stop"
$projectRoot = $PSScriptRoot
$backendPath = Join-Path $projectRoot "backend"
$port = 8010

Write-Host "=== 起動中 ===" -ForegroundColor Cyan

Write-Host "[1/2] Backend の依存関係を同期中..." -ForegroundColor Green
Push-Location $backendPath
try {
    uv sync
    if ($LASTEXITCODE -ne 0) {
        throw "Backend の依存関係の同期に失敗しました。"
    }
}
finally {
    Pop-Location
}

$backendPython = Join-Path $backendPath ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $backendPython -PathType Leaf)) {
    throw "Backend の Python 実行環境が見つかりません。"
}

Write-Host "[2/2] Backend (:$port) を起動中..." -ForegroundColor Green
$backendProcess = Start-Process `
    -FilePath $backendPython `
    -ArgumentList "-m uvicorn app.main:app --host 0.0.0.0 --port $port --app-dir src" `
    -WorkingDirectory $backendPath `
    -WindowStyle Hidden `
    -PassThru

try {
    Start-Sleep -Seconds 2
    if ($backendProcess.HasExited) {
        throw "Backend の起動に失敗しました。"
    }

    Start-Process "http://localhost:$port"
    Write-Host "`n=== 起動完了 ===" -ForegroundColor Cyan
    Write-Host "PC/ブラウザ: http://localhost:$port" -ForegroundColor Yellow
    Write-Host "iPhone (LAN): http://<このPCのIPアドレス>:$port" -ForegroundColor Yellow
    Write-Host "終了するには Enter を押してください。"
    Read-Host "Press Enter to stop"
}
finally {
    if (-not $backendProcess.HasExited) {
        Stop-Process -Id $backendProcess.Id -Force -ErrorAction SilentlyContinue
    }
    Write-Host "停止しました。" -ForegroundColor Gray
}
