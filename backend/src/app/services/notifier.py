"""タスク終了時に Windows の標準トースト通知を出すサービス。

通知はあくまで補助なので、失敗してもタスクの成否に影響させない
（例外はログに残して握りつぶす）。Windows 以外では何もしない。
"""

import logging
import sys
from collections.abc import Callable

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
    """Windows のトースト通知を表示する。"""
    from winotify import Notification, audio

    toast = Notification(app_id=APP_ID, title=title, msg=message, duration="short")
    toast.set_audio(audio.Default, loop=False)
    toast.show()


def summarize(text: str, limit: int = MESSAGE_LIMIT) -> str:
    """通知本文用に 1 行へ整形して切り詰める。"""
    flattened = " ".join((text or "").split())
    if len(flattened) <= limit:
        return flattened
    return flattened[: limit - 1] + "…"


class TaskNotifier:
    """タスクの終了を Windows 標準通知で知らせる。"""

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
        if not self.enabled:
            return
        if sys.platform != "win32":
            logger.debug("Windows 以外のため通知をスキップします")
            return

        title = self.build_title(mode, success)
        message = summarize(result) or task
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
