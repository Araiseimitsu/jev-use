"""Windows 標準通知（notifier）のテスト。実際のトーストは出さず、送信内容だけを検証する。"""

import asyncio
from typing import Any

import pytest

from app.core.config import settings
from app.services.notifier import MESSAGE_LIMIT, TaskNotifier, notify_browser_event, summarize


class _Recorder:
    """送信された通知を記録するテスト用バックエンド。"""

    def __init__(self, error: Exception | None = None) -> None:
        self.sent: list[tuple[str, str]] = []
        self.error = error

    def __call__(self, title: str, message: str) -> None:
        if self.error:
            raise self.error
        self.sent.append((title, message))


def test_summarize_flattens_and_truncates() -> None:
    assert summarize("結果\nです") == "結果 です"
    long_text = "あ" * (MESSAGE_LIMIT + 50)
    shortened = summarize(long_text)
    assert len(shortened) == MESSAGE_LIMIT
    assert shortened.endswith("…")


def test_build_title_includes_status_and_mode() -> None:
    assert TaskNotifier.build_title("browser", True) == "✅ タスク完了 - ブラウザ操作"
    assert TaskNotifier.build_title("browser", False) == "⚠️ エラーが発生しました - ブラウザ操作"
    # 未知のモードはタイトルに付けない
    assert TaskNotifier.build_title("unknown", True) == "✅ タスク完了"
    assert TaskNotifier.build_title("", True) == "✅ タスク完了"


def test_notify_sends_summarized_result(monkeypatch) -> None:
    monkeypatch.setattr("app.services.notifier.sys.platform", "win32")
    recorder = _Recorder()

    TaskNotifier(enabled=True, sender=recorder).notify(
        task="メモ帳に買い物リストを書く",
        result="メモ帳に 3 行入力しました。\n保存はしていません。",
        mode="browser",
    )

    assert len(recorder.sent) == 1
    title, message = recorder.sent[0]
    assert title == "✅ タスク完了 - ブラウザ操作"
    assert message == "メモ帳に 3 行入力しました。 保存はしていません。"


def test_notify_uses_task_when_result_is_empty(monkeypatch) -> None:
    monkeypatch.setattr("app.services.notifier.sys.platform", "win32")
    recorder = _Recorder()

    TaskNotifier(enabled=True, sender=recorder).notify(task="メモ帳を開く", result="", mode="")

    assert recorder.sent[0][1] == "メモ帳を開く"


def test_notify_skips_when_disabled(monkeypatch) -> None:
    monkeypatch.setattr("app.services.notifier.sys.platform", "win32")
    recorder = _Recorder()

    TaskNotifier(enabled=False, sender=recorder).notify(task="テスト", result="結果")

    assert recorder.sent == []


def test_notify_skips_on_non_windows(monkeypatch) -> None:
    monkeypatch.setattr("app.services.notifier.sys.platform", "linux")
    recorder = _Recorder()

    TaskNotifier(enabled=True, sender=recorder).notify(task="テスト", result="結果")

    assert recorder.sent == []


def test_notify_never_raises(monkeypatch) -> None:
    """通知の失敗がタスクの失敗にならないこと。"""
    monkeypatch.setattr("app.services.notifier.sys.platform", "win32")
    recorder = _Recorder(error=RuntimeError("toast failed"))

    TaskNotifier(enabled=True, sender=recorder).notify(task="テスト", result="結果")


def test_route_notifies_on_complete(monkeypatch) -> None:
    """完了とエラーで通知し、開始では通知しない。"""
    from app.api import routes

    recorded: list[dict[str, Any]] = []

    class _FakeNotifier:
        def notify(self, **kwargs: Any) -> None:
            recorded.append(kwargs)

        def notify_waiting(self, **kwargs: Any) -> None:
            recorded.append({"waiting": True, **kwargs})

    monkeypatch.setattr("app.services.notifier.TaskNotifier", lambda: _FakeNotifier())

    routes.notify_browser_event(
        {"event": "complete", "data": {"result": "完了しました"}}, "テスト", "browser"
    )
    routes.notify_browser_event({"event": "start", "data": {}}, "テスト", "browser")
    routes.notify_browser_event(
        {"event": "error", "data": {"message": "失敗"}}, "テスト", "browser"
    )

    assert len(recorded) == 2
    assert recorded[0]["success"] is True
    assert recorded[0]["mode"] == "browser"
    assert recorded[1]["success"] is False
    assert recorded[1]["mode"] == "browser"


def test_waiting_notifies_once_until_the_person_continues(monkeypatch) -> None:
    monkeypatch.setattr(settings, "enable_notifications", True)
    monkeypatch.setattr("app.services.notifier.sys.platform", "win32")
    recorder = _Recorder()
    monkeypatch.setattr("app.services.notifier._show_windows_toast", recorder)

    notify_browser_event(
        {"event": "user_input", "data": {"active": True, "message": "ログインしてください"}},
        "予定を教えて",
        "browser",
    )
    notify_browser_event(
        {"event": "user_input", "data": {"active": False, "message": ""}},
        "予定を教えて",
        "browser",
    )
    notify_browser_event(
        {"event": "human_check", "data": {"active": True, "message": "確認してください"}},
        "予定を教えて",
        "browser",
    )
    notify_browser_event(
        {"event": "confirm_request", "data": {"reason": "購入ボタンを押します"}},
        "予定を教えて",
        "browser",
    )

    assert recorder.sent == [
        ("入力が必要です - ブラウザ操作", "ログインしてください"),
        ("入力が必要です - ブラウザ操作", "確認してください"),
        ("入力が必要です - ブラウザ操作", "購入ボタンを押します"),
    ]


def test_notifications_default_enabled(monkeypatch) -> None:
    monkeypatch.setattr(settings, "enable_notifications", True)
    assert TaskNotifier().enabled is True

    monkeypatch.setattr(settings, "enable_notifications", False)
    assert TaskNotifier().enabled is False
