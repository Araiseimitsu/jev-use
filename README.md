# jev-use

Web ブラウザのページを読み、次の操作を選んで実行するアプリです。
FastAPI が画面と API を出し、ターミナルからも実行できます。

## 動き

1. Playwright がページ上のボタンと入力欄を読む。
2. TypeSafe Jev が、その候補のどれを実行するかを選ぶ。
3. Playwright が選ばれた要素をクリックするか、文字を入れる。

座標は使いません。操作の選択はページの文字情報で行います。文字情報だけでは答えを確認できないときは、表示中のスクリーンショットを Gemini に送って回答を試みます（1 回の実行につき最大 2 回）。
Gemini は入力する文字列と最後の答えの文章にも使います。画像から答えるには `GEMINI_API_KEY` が必要です。画像にも答えが見つからなければ、達成したとは表示しません。

危険度が `TYPESAFE_RISK_THRESHOLD`（既定 0.7）以上の操作は、承認があるまで実行しません。

## 構成

```txt
.
├─ backend/src/app/
│  ├─ api/routes.py
│  ├─ cli.py
│  └─ services/
│     ├─ browser_agent.py      # 操作ループ
│     ├─ browser_session.py    # Playwright
│     ├─ page_state.py         # DOM から操作候補を作る
│     ├─ text_writer.py        # 入力文と最終回答
│     └─ jev/                  # Jev への質問
└─ frontend/                   # 画面
```

## セットアップ

```bash
cd backend
uv sync
uv run playwright install chromium
```

`backend/.env.example` を `backend/.env` にコピーし、`TYPESAFE_API_KEY` を設定します。
`GEMINI_API_KEY` は任意です。その他の項目と既定値は `.env.example` のコメントを参照してください。

## 起動

```bash
cd backend
uv run uvicorn app.main:app --reload --app-dir src
```

ブラウザで [http://127.0.0.1:8000](http://127.0.0.1:8000) を開きます。
Windows ではプロジェクトルートの `start.cmd`（または `start.ps1`）でも起動できます（ポート 8010、同じ LAN の端末からも接続できる設定で起動します）。バックグラウンドで常駐するため、ウィンドウはすぐ閉じます。

### 停止

`start.cmd` で起動した Backend はタスクマネージャーではなく、以下のいずれかで停止します。

```powershell
# ポート 8010 を使っているプロセスを停止
Get-NetTCPConnection -LocalPort 8010 -State Listen | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }
```

またはタスクマネージャーで `python.exe`（uvicorn を起動しているもの）を終了します。

### ターミナルから実行

```powershell
cd backend
$env:PYTHONPATH="src"
uv run python -m app.cli "Googleで東京の明日の天気を調べて"
uv run python -m app.cli "https://example.com の内容を教えて" --headless --yes
```

`--yes` は実行前の確認を省略します。操作の途中で危険と判断されたときは、その場で聞きます。
`--max-steps` は 1〜50 で指定します（画面の API と同じ上限）。

## 検証

```bash
cd backend
uv run pytest
```

## 補足

- 操作するのは、このアプリが起動する Chrome（無ければ Chromium）です。普段使っているブラウザのウィンドウは操作しません。
- 「人間ですか？」と出たら、そのウィンドウで確認を済ませてください。アプリはそこをクリックせず、終わるまで待ちます。確認後の状態は `backend/.browser-profile` に残ります。
- 指示の中に `https://` の URL があればそのページから始め、無ければ Google を開きます。
- ログインや、自動では進めない入力は、開いているブラウザで入れてください。そのときは Windows の通知も出ます。終わったら画面の「続行」を押すと、その続きから動きます。終わったときの結果は、この画面の「結果」と通知の両方に出ます。指示文にパスワードが書いてあっても、経過や通知には出しません。
- `file://` や `javascript:` は開きません。
- Jev のキーが無いと実行できません。次の操作を選ぶ役だからです。
