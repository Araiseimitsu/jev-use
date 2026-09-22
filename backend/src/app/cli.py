"""コマンドラインからブラウザ操作を実行する。

使用例:
    uv run python -m app.cli "Googleで東京の明日の天気を調べて"
    uv run python -m app.cli "https://example.com を開いて内容を教えて" --max-steps 8
    uv run python -m app.cli "購入手続きを進めて" --yes
"""

import argparse
import asyncio
import sys
from typing import Any

from app.core.config import settings
from app.services.browser_agent import BrowserAgentRunner
from app.services.jev.asker import MISSING_KEY_MESSAGE
from app.services.jev.task_assessor import MAX_STEPS_LIMIT, TaskAssessment, TaskAssessor
from app.services.notifier import notify_browser_event
from app.services.page_state import redact_secrets


def print_assessment(assessment: TaskAssessment) -> None:
    """開始前の見積りを表示する。"""
    print("=== 実行前の判断 ===")
    if assessment.used_fallback:
        print(f"補足: {assessment.message}")
    else:
        print(
            f"危険度: {assessment.risk_probability:.0%} | 具体性: {assessment.clarity_probability:.0%}"
            f" | 実現性: {assessment.feasibility_probability:.0%}"
        )
        print(
            f"推奨ステップ数: {assessment.suggested_max_steps} | モデル: {assessment.model}"
            f" | 判断時間: {assessment.latency_ms}ms"
        )
    print("--------------------------------")


def ask_yes_no(prompt: str) -> bool:
    try:
        return input(prompt).strip().lower() in ("y", "yes")
    except EOFError:
        return False


def print_event(event: dict[str, Any], runner: BrowserAgentRunner) -> None:
    """イベントを 1 件表示する。承認要求にはその場で応答する。"""
    ev_type = event.get("event")
    data = event.get("data", {})

    if ev_type == "start":
        print(f"[INFO] ブラウザを開きます: {data.get('start_url')}")
    elif ev_type == "observation":
        print(f"  ページ: {data.get('title') or data.get('url')}")
    elif ev_type == "step":
        action = data.get("action") or {}
        print(f"\n[Step {data.get('step')}] {action.get('description')}")
        if action.get("text"):
            print(f"  入力: {action['text']}")
        if data.get("error"):
            print(f"  失敗: {data['error']}")
        jev = data.get("jev") or {}
        if jev:
            print(
                f"  確信度 {jev.get('confidence', 0):.0%}"
                f" / 危険度 {jev.get('risk_probability', 0):.0%}"
                f" / {jev.get('latency_ms', 0)}ms"
            )
    elif ev_type == "human_check" and data.get("active"):
        print(f"\n[確認] {data.get('message')}")
    elif ev_type == "user_input" and data.get("active"):
        print(f"\n[入力] {data.get('message')}")
        try:
            input("  終わったら Enter: ")
        except EOFError:
            pass
        runner.mark_human_ready()
    elif ev_type == "confirm_request":
        print(f"\n[確認] Step {data.get('step')}")
        action = data.get("action") or {}
        if action:
            print(f"  対象: {action.get('description')}")
        print(f"  理由: {data.get('reason')}")
        print(f"  {data.get('timeout_seconds')} 秒以内に応答がない場合は実行しません。")
        granted = ask_yes_no("  実行しますか？ [y/N]: ")
        runner.resolve_approval(granted)
        print("  → 承認しました" if granted else "  → 拒否しました")
    elif ev_type == "complete":
        print("\n================ 完了 ================")
        print(f"結果:\n{data.get('result')}")
        print(f"所要時間: {data.get('duration_seconds')} 秒 | 総ステップ: {data.get('steps_count')}")
        print("======================================")
    elif ev_type == "error":
        print(f"\n[ERROR] {data.get('message')}", file=sys.stderr)


async def main() -> None:
    parser = argparse.ArgumentParser(description="ブラウザを読み、次の操作を選んで実行する")
    parser.add_argument("task", type=str, help="実行する指示")
    parser.add_argument("--max-steps", type=int, default=None, help="最大ステップ数")
    parser.add_argument("--headless", action="store_true", help="ブラウザを表示しない")
    parser.add_argument("--yes", action="store_true", help="実行前の確認を省略する")
    args = parser.parse_args()
    # API と同じ範囲に揃える
    if args.max_steps is not None and not 1 <= args.max_steps <= MAX_STEPS_LIMIT:
        parser.error(f"--max-steps は 1〜{MAX_STEPS_LIMIT} で指定してください。")

    if not settings.typesafe_api_key.strip():
        print(f"エラー: {MISSING_KEY_MESSAGE}", file=sys.stderr)
        sys.exit(1)

    assessment = await TaskAssessor().assess(redact_secrets(args.task))
    print_assessment(assessment)
    if assessment.requires_confirmation and not args.yes:
        for reason in assessment.reasons:
            print(f"- {reason}")
        if not ask_yes_no("このまま実行しますか？ [y/N]: "):
            print("中止しました。")
            return

    max_steps = args.max_steps or assessment.suggested_max_steps or settings.default_max_steps
    runner = BrowserAgentRunner(
        task=args.task,
        max_steps=max_steps,
        headless=True if args.headless else None,
    )
    visible_task = redact_secrets(args.task)
    failed = False
    async for event in runner.run_stream():
        print_event(event, runner)
        notify_browser_event(event, visible_task, runner.mode)
        if event.get("event") == "error":
            failed = True
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
