# デスクトップ操作特化 × TypeSafe Jev 全面活用 設計

この設計は 2026-09-22 のブラウザ操作への刷新で置き換えた。現行の構成は `.docs/update.md` を参照する。



## 目的

- アプリをデスクトップ操作専用にする（Browser Use / ブラウザ版 Computer Use / おまかせモードを廃止）。
- 操作の生成は Gemini Computer Use（desktop 環境）に任せ、ループ内のあらゆる分岐判断を TypeSafe Jev の型付き質問で行う。
- Jev は画像を受け取れない（`state` はテキスト / JSON のみ）ため、Gemini Vision が画面を構造化 JSON（ScreenState）に変換して Jev に渡す。

## 構成

```txt
backend/src/app/services/
├─ computer_use_base.py      # Interactions API 呼び出し・スレッド実行（既存）
├─ desktop_use_agent.py      # ループの制御（部品を呼び出すだけ）
├─ desktop_actions.py        # pyautogui によるアクション実行
├─ screen_reader.py          # Gemini Vision → ScreenState
├─ jev/
│  ├─ asker.py               # Jev 呼び出し・計時の共通部品
│  ├─ task_assessor.py       # 開始前の見積り
│  ├─ action_gate.py         # 操作ごとの審査
│  ├─ outcome_checker.py     # 操作後の結果判定・回復方針
│  ├─ step_review.py         # 結果判定と次アクション審査の統合（1 リクエスト）
│  └─ completion_checker.py  # 完了の検証
└─ notifier.py
```

削除: `browser_agent.py` / `computer_use_agent.py` / `decision_engine.py`（jev/ へ分割移設）、
API `/api/browser/run` `/api/computer/run` `/api/route`、CLI `--mode` `--headless`、
依存 `browser-use` `playwright` `langchain-google-genai` `google-genai`、設定 `DEFAULT_HEADLESS`。
`GEMINI_MODEL` は画面解析モデルとして再利用する。

## Jev への質問

| 場面 | 質問 | 型 | 使い方 |
| --- | --- | --- | --- |
| 開始前 | `risk` | noul | 閾値（`TYPESAFE_RISK_THRESHOLD`）以上で実行前確認 |
| 開始前 | `steps` | score | `max_steps` の提案 |
| 開始前 | `clarity` | noul | 指示が具体的か。低ければ実行前確認 |
| 開始前 | `feasibility` | noul | デスクトップ操作で完結するか。低ければ実行前確認 |
| 操作ごと | `verdict` | choice | proceed / ask_human / stop（ScreenState も判断材料） |
| 操作ごと | `progress` | noul | 連続で低ければ停滞として停止 |
| 操作後 | `outcome` | choice | as_expected / no_change / unexpected_dialog / error_shown |
| 操作後 | `recovery` | choice | continue / retry_differently / ask_human / abort（outcome が as_expected 以外のときだけ採用） |
| 完了主張時 | `completed` | noul | 低ければ Gemini に未完了として続行を指示（上限あり） |

「操作ごと」の `verdict` / `progress` と「操作後」の `outcome` / `recovery` は、
前ステップの結果判定と次アクションの審査として `step_review` が 1 リクエストにまとめて問う。

## 1 ステップの流れ

1. Gemini Computer Use が次の操作を返す（操作がなければ「完了の主張」→ 完了検証へ）。
2. Jev へ「前ステップの結果判定」と「その先頭操作の審査」を 1 リクエストでまとめて問う（`step_review`）。
   審査が無効・実行済み操作なし・画面状態なし・完了主張のときは結果判定だけを問い、操作は `action_gate` が個別に審査する。
3. 結果判定の回復方針を適用 → proceed は実行、ask_human は人間の承認待ち、stop は中止。
4. 実行後にキャプチャ → `screen_reader` が ScreenState を作る。
5. 結果判定の所見を Gemini への function_result に添え、次の操作を依頼する。
6. 完了主張時は `completion_checker` が最終画面で検証。未完了なら所見を添えて続行を指示（最大 2 回）。

統合判断は Jev の往復を 1 ステップあたり 2 回から 1 回に減らす。結果判定は次のステップの先頭で
行うため、その所見は次に受け取る操作（1 操作後）の依頼に添えられる。中止・人間確認は次の操作を
実行する前に適用する。

## 失敗時の扱い

- Jev のキー未設定・API 失敗・解釈失敗: 各判断は「作業を止めない側」へ倒す（proceed / as_expected / completed）。理由はイベントに残す。
- Gemini Vision の失敗: ScreenState なしで続行（Jev は操作内容のみで判断）。
- 安全装置（`ENABLE_DESKTOP_CONTROL`、FAILSAFE、停止 API、`require_confirmation` の保留、承認タイムアウト）は維持する。

## API / UI / CLI

- API: `GET /api/health` `GET /api/config` `POST /api/desktop/assess` `POST /api/desktop/run` `POST /api/desktop/stop/{run_id}` `POST /api/desktop/approve/{run_id}`。
- SSE の `step` に `jev`（審査）、`observation` イベントに ScreenState と outcome、`complete` に `verification` を追加する。
- `confirm_request` に `kind`（`action` / `recovery`）を追加し、同じ承認 API で応答する。
- UI: モード選択とヘッドレスを削除。事前判断、画面プレビュー、ステップごとの Jev 判定、完了検証を表示する。
- CLI: 常にデスクトップ操作。開始前に Jev の見積りを表示し、確認が必要なら `--yes` なしでは実行しない。

## テスト

- Jev 各判断: 解釈・フォールバック（フェイククライアント）。
- ScreenReader: レスポンス解析と失敗時の None（HTTP はモック）。
- ランナー: 結果判定による所見付与・停止、完了検証による続行（Gemini / Jev / pyautogui をフェイク化）。
- API: 削除したエンドポイントが 404、assess・run のバリデーション。
