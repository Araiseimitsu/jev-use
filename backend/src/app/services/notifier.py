"""タスクの完了と、人の操作待ちを Windows の標準トースト通知で知らせる。

通知はあくまで補助なので、失敗してもタスクの成否に影響させない
（例外はログに残して握りつぶす）。Windows 以外では何もしない。
"""

import logging
import sys
from collections.abc import Callable
from typing import Any

from app.core.config import settings

logger = logging.getLogger(__name__)

#: トーストに載せる本文の最大文字数（長すぎると Windows 側で省略されるため）
MESSAGE_LIMIT = 140

#: 通知のタイトル（アプリ名）
APP_ID = "Jev"

#: 実行モードの日本語表示名
MODE_LABELS: dict[str, str] = {
    "browser": "ブラウザ操作",
}

#: 通知送信関数の型（テストで差し替えられるようにする）
ToastSender = Callable[[str, str], None]


def _show_windows_toast(title: str, message: str) -> None:
    """Windows のトースト通知を表示する。入力待ちは、画面を離れていても気づけるよう長く出す。"""
    from winotify import Notification, audio

    duration = "long" if "入力が必要" in title else "short"
    toast = Notification(app_id=APP_ID, title=title, msg=message, duration=duration)
    toast.set_audio(audio.Default, loop=False)
    toast.show()


def summarize(text: str, limit: int = MESSAGE_LIMIT) -> str:
    """通知本文用に 1 行へ整形して切り詰める。"""
    flattened = " ".join((text or "").split())
    if len(flattened) <= limit:
        return flattened
    return flattened[: limit - 1] + "…"


class TaskNotifier:
    """タスクの終了と、人の操作待ちを Windows 標準通知で知らせる。"""

    def __init__(
        self,
        enabled: bool | None = None,
        sender: ToastSender | None = None,
    ) -> None:
        self.enabled = settings.enable_notifications if enabled is None else enabled
        self._sender = sender or _show_windows_toast

    def notify(
        self,
        task: str,
        result: str,
        mode: str = "",
        success: bool = True,
    ) -> None:
        """完了（またはエラー）を通知する。失敗しても例外は投げない。"""
        title = self.build_title(mode, success)
        self._send(title, summarize(result) or task)

    def notify_waiting(self, message: str, mode: str = "") -> None:
        """ログインや確認など、人の操作を待っていることを通知する。"""
        self._send(self.build_waiting_title(mode), summarize(message) or "開いているブラウザでの操作を待っています")

    def _send(self, title: str, message: str) -> None:
        if not self.enabled:
            return
        if sys.platform != "win32":
            logger.debug("Windows 以外のため通知をスキップします")
            return
        try:
            self._sender(title, message)
        except Exception:
            # 通知できないことを理由にタスクを失敗させない
            logger.exception("通知の送信に失敗しました")

    @staticmethod
    def build_title(mode: str, success: bool) -> str:
        """通知タイトルを組み立てる（例: ✅ タスク完了 - ブラウザ操作）。"""
        mode_label = MODE_LABELS.get(mode, "")
        status = "✅ タスク完了" if success else "⚠️ エラーが発生しました"
        return f"{status} - {mode_label}" if mode_label else status

    @staticmethod
    def build_waiting_title(mode: str) -> str:
        """入力待ちのタイトルを組み立てる。"""
        mode_label = MODE_LABELS.get(mode, "")
        status = "入力が必要です"
        return f"{status} - {mode_label}" if mode_label else status


def notify_browser_event(event: dict[str, Any], task: str, mode: str) -> None:
    """ブラウザ操作のイベントに応じて通知する。待ちが終わった通知は出さない。"""
    kind = str(event.get("event") or "")
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    notifier = TaskNotifier()
    if kind in {"human_check", "user_input"} and data.get("active"):
        notifier.notify_waiting(str(data.get("message") or ""), mode=mode)
    elif kind == "confirm_request":
        notifier.notify_waiting(
            str(data.get("reason") or "この操作を実行してよいか確認してください"),
            mode=mode,
        )
    elif kind == "complete":
        notifier.notify(task=task, result=str(data.get("result") or ""), mode=mode, success=True)
    elif kind == "error":
        notifier.notify(task=task, result=str(data.get("message") or ""), mode=mode, success=False)
