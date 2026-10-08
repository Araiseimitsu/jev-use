# jev-use の起動ショートカット（アイコン付き）を、この PC のデスクトップに作る。
# .lnk は絶対パスを持つため、リポジトリには含めず、PC ごとにこのスクリプトで作る。
# 配置場所は $PSScriptRoot から求めるので、フォルダをどこへ置いても動く。
param(
    # 作成先のフォルダ。省略時はデスクトップ（OneDrive 等でリダイレクトされていても追従する）
    [string]$Folder = [Environment]::GetFolderPath("Desktop")
)
$ErrorActionPreference = "Stop"

$target = Join-Path $PSScriptRoot "start.cmd"
$icon = Join-Path $PSScriptRoot "frontend\icons\icon.ico"
foreach ($path in $target, $icon) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "必要なファイルが見つかりません: $path"
    }
}
if (-not (Test-Path -LiteralPath $Folder -PathType Container)) {
    throw "作成先のフォルダがありません: $Folder"
}

$linkPath = Join-Path $Folder "jev-use.lnk"
$shell = New-Object -ComObject WScript.Shell
$link = $shell.CreateShortcut($linkPath)
$link.TargetPath = $target
$link.WorkingDirectory = $PSScriptRoot
$link.IconLocation = "$icon,0"
$link.Description = "jev-use を起動してブラウザで開く"
# 7 = 最小化。起動中の黒いウィンドウを目立たせない
$link.WindowStyle = 7
$link.Save()

Write-Host "ショートカットを作成しました: $linkPath" -ForegroundColor Green
