# jev-use

Web ブラウザのページを読み、次の操作を選んで実行するアプリです。
FastAPI が画面と API を出し、ターミナルからも実行できます。

## 動き

1. Playwright がページ上のボタン・リンク・入力欄・押せる行を読む。
2. Gemini が、依頼・ページの文字・これまでの操作を見て手順を考え、次の 1 操作と入力する文字列を候補から選ぶ。
3. TypeSafe Jev が、選ばれた操作の送信・公開、購入・契約、保存・削除、認証・権限への影響を判定する。判断には対象要素と関連フォームの構造を使い、入力済みの値やページ本文は追加で渡さない。
4. Playwright が選ばれた要素をクリックするか、文字を入れる。

プルダウンは選択肢ごとの候補から選びます。同名の選択肢も区別し、実行前に選択肢の位置と内容を再確認します。

座標は使いません。スクリーンショットは、実行中の様子を画面に出すためだけに使い、判断には渡しません。

4種類の判定の最大値が `TYPESAFE_RISK_THRESHOLD`（既定 0.7）以上の操作と、構造から特定した送信操作は、承認があるまで実行しません。
送信の後は、完了の表示や入力欄のクリアを確かめます。確認画面へ進んだときは、最後の送信まで続けます。
Gemini のページで返答を求めた場合は、新しい回答の生成が落ち着くまで待ってから結果を表示します。90 秒以内に回答を確認できなければ、その旨を表示します。

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
│     ├─ planner.py            # Gemini が次の操作を選ぶ
│     ├─ gemini.py             # Gemini の呼び出し
│     ├─ text_writer.py        # 最終回答（と入力文の補助）
│     └─ jev/                  # Jev への質問（危険度など）
└─ frontend/                   # 画面
```

## セットアップ

```bash
cd backend
uv sync
uv run playwright install chromium
```

`backend/.env.example` を `backend/.env` にコピーし、`GEMINI_API_KEY` と `TYPESAFE_API_KEY` を設定します。
その他の項目と既定値は `.env.example` のコメントを参照してください。
危険度判定のしきい値を適用する Jev は `jev-1.13.0` に固定しています。

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
- Gemini と Jev のキーが無いと実行できません。Gemini は次の操作を選び、Jev は操作の危険度を判定するためです。
