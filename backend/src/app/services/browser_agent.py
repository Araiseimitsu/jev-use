"""DOM を読んで Jev が次の一手を選び、Playwright が実行するブラウザ操作ランナー。

スクショは人が見るプレビューにだけ使い、次の操作の決定には使わない。
"""

import asyncio
import logging
import sys
import threading
import time
from collections.abc import AsyncGenerator, Callable
from typing import Any

import httpx

from app.core.config import settings
from app.services.browser_session import BrowserSession
from app.services.jev.asker import MISSING_KEY_MESSAGE
from app.services.jev.browser_decider import BrowserDecider, Decision
from app.services.page_state import (
    ActionOption,
    Element,
    PageView,
    action_catalog,
    field_purpose,
    is_allowed_url,
    is_human_check,
    needs_user_input,
    rank_elements,
    redact_secrets,
    request_line,
    start_url,
    submits_on_enter,
)
from app.services.text_writer import TextWriter

logger = logging.getLogger(__name__)

# ページに答えが揃ったとみなす done の確率
DONE_THRESHOLD = 0.75
# 同じ操作が続いたら止める回数
REPEAT_LIMIT = 3
# 確認なしで実行してよい操作
SAFE_ACTIONS = frozenset({"scroll_down", "back", "wait", "done"})
# 「人間ですか？」の画面を見にいく間隔（秒）
HUMAN_CHECK_POLL_SECONDS = 1.0
HUMAN_CHECK_MESSAGE = (
    "開いているブラウザで確認を済ませてください。"
    "「車を選ぶ」などの画像が出ても、こちらはページを読み直さないので、その画面のまま押せます。"
    "確認が終わってページが移ると、続きから動きます。"
)
# 人に入力を頼んでよい回数。超えたら止める。
USER_ASSIST_LIMIT = 2
LOGIN_MESSAGE = (
    "ログインが必要なため、ここは自動では入力しません。"
    "開いているブラウザで必要な項目を入力し、終わったら「続行」を押してください。"
    "入力が終わると、続きから動きます。"
)
STUCK_MESSAGE = (
    "この先は自動では進めません。"
    "開いているブラウザで必要な入力や操作をして、終わったら「続行」を押してください。"
)
HEADLESS_INPUT_MESSAGE = (
    "人が入力しないと先に進めません。"
    "ブラウザを表示して実行し、そのウィンドウで入力してください。"
)

Emit = Callable[[dict[str, Any] | None], None]


class BrowserAgentRunner:
    """ブラウザを開き、操作候補の選択と実行を繰り返す。"""

    mode = "browser"

    def __init__(
        self,
        task: str,
        max_steps: int | None = None,
        headless: bool | None = None,
        decider: BrowserDecider | None = None,
        writer: TextWriter | None = None,
        session_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.task = task.strip()
        self.max_steps = max_steps or settings.default_max_steps
        self.headless = settings.browser_headless if headless is None else headless
        self.decider = decider or BrowserDecider()
        self.writer = writer or TextWriter()
        self._session_factory = session_factory or (lambda: BrowserSession(headless=self.headless))
        self._stop_event = threading.Event()
        self._approval_event = threading.Event()
        self._approval_granted: bool | None = None
        self._human_ready = threading.Event()
        self._last_action = ""
        self._repeat = 0
        self._assist_count = 0
        self._skip_login_url = ""

    def task_for_model(self) -> str:
        """判断と文章生成に渡す指示。パスワードの実値は含めない。"""
        return redact_secrets(self.task)

    def request_stop(self) -> None:
        """実行中のループに停止要求を出す。"""
        self._stop_event.set()

    def is_stop_requested(self) -> bool:
        return self._stop_event.is_set()

    def resolve_approval(self, granted: bool) -> None:
        """人間の承認または拒否をループへ伝える。"""
        self._approval_granted = granted
        self._approval_event.set()

    def mark_human_ready(self) -> None:
        """確認や手入力が終わったと伝えて、待ちを終わらせる。"""
        self._human_ready.set()

    def validation_error(self) -> str | None:
        if not self.decider.enabled:
            return MISSING_KEY_MESSAGE
        url = start_url(self.task)
        if not is_allowed_url(url):
            return "開いてよい URL ではありません。http または https を指定してください。"
        return None

    async def run_stream(self) -> AsyncGenerator[dict[str, Any], None]:
        """操作ループを実行し、SSE 用のイベントを返す。"""
        if not self.task:
            yield {"event": "error", "data": {"message": "実行するタスクを指定してください。"}}
            return
        problem = self.validation_error()
        if problem:
            yield {"event": "error", "data": {"message": problem}}
            return

        yield {
            "event": "start",
            "data": {
                "task": self.task_for_model(),
                "max_steps": self.max_steps,
                "mode": self.mode,
                "headless": self.headless,
                "start_url": start_url(self.task),
            },
        }

        main_loop = asyncio.get_running_loop()
        queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

        def emit(item: dict[str, Any] | None) -> None:
            try:
                main_loop.call_soon_threadsafe(queue.put_nowait, item)
            except RuntimeError:
                # 受け取り側のループが先に閉じた。イベントは捨て、後片付けは続ける。
                pass

        def run_worker() -> None:
            if sys.platform == "win32":
                worker_loop = asyncio.ProactorEventLoop()
            else:
                worker_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(worker_loop)
            try:
                worker_loop.run_until_complete(self._execute(emit))
            except Exception:
                logger.exception("browser agent worker failed")
                emit({"event": "error", "data": {"message": "エージェント実行中にエラーが発生しました。"}})
            finally:
                emit(None)
                worker_loop.close()

        thread = threading.Thread(target=run_worker, daemon=True)
        thread.start()
        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                yield item
        finally:
            # 画面の停止や切断で受け取りが途中で終わっても、ループは止めずに停止要求だけ出す。
            # ループを強制的に止めるとブラウザを閉じる処理が走らず、プロフィールが使用中のまま残る。
            if thread.is_alive():
                self.request_stop()

    async def _execute(self, emit: Emit) -> None:
        started = time.monotonic()
        session = self._session_factory()
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(60)) as client:
                result, steps = await self._run(emit, session, client)
            emit(
                {
                    "event": "complete",
                    "data": {
                        "result": result,
                        "steps_count": steps,
                        "duration_seconds": round(time.monotonic() - started, 1),
                    },
                }
            )
        except Exception as e:
            logger.exception("browser agent failed")
            emit({"event": "error", "data": {"message": _public_error(e)}})
        finally:
            await session.close()

    async def _run(
        self, emit: Emit, session: Any, client: httpx.AsyncClient
    ) -> tuple[str, int]:
        url = start_url(self.task)
        await session.start(url)
        view = PageView(url=url, title="", excerpt="", elements=())
        step = 0
        for step in range(1, self.max_steps + 1):
            if self.is_stop_requested():
                return "ユーザーの操作で停止しました。", step

            view = await session.read()
            held = await self._hold_for_human(emit, session, view, step)
            if isinstance(held, str):
                return held, step
            view = held
            handed = await self._hold_for_login(emit, session, view, step)
            if isinstance(handed, str):
                return handed, step
            view = handed

            ranked = rank_elements(view.elements, self.task)
            options = action_catalog(ranked, defer_login=needs_user_input(view))
            decision = await self.decider.decide(self.task_for_model(), view, options)
            if decision.used_fallback:
                reason = await self._hand_off_or_stop(
                    emit,
                    session,
                    view,
                    step,
                    decision.message or "次の操作を選べなかったため停止しました。",
                    decision,
                    options,
                )
                if reason:
                    return reason, step
                continue

            if decision.action_id == "done" or decision.done_probability >= DONE_THRESHOLD:
                self._emit_step(emit, step, decision, options, "")
                return await self.writer.summary(
                    client, self.task_for_model(), view.url, view.title, view.excerpt
                ), step

            if await self._needs_stop_for_risk(emit, step, decision, options):
                return "承認が得られなかったため停止しました。", step

            if self._repeated(decision.action_id):
                reason = await self._hand_off_or_stop(
                    emit, session, view, step, "同じ操作を繰り返して進展しないため停止しました。"
                )
                if reason:
                    return reason, step
                continue

            typed, error = await self._perform(session, decision.action_id, ranked, client, view.title)
            self._emit_step(emit, step, decision, options, typed, error)

        summary = await self.writer.summary(
            client, self.task_for_model(), view.url, view.title, view.excerpt
        )
        return f"最大ステップ数（{self.max_steps}）に達しました。\n{summary}", step

    async def _hold_for_human(
        self, emit: Emit, session: Any, view: PageView, step: int
    ) -> PageView | str:
        """確認画面なら、人が済ませるまで待つ。終わればそのページ、止めれば理由の文字列。"""
        if not is_human_check(view.title, view.excerpt, view.url):
            await self._show_page(emit, session, step, view)
            return view
        if self.headless:
            return (
                "人間かどうかの確認が出ました。"
                "ブラウザを表示して実行し、そのウィンドウで確認を済ませてください。"
            )

        await self._show_page(emit, session, step, view)
        self._human_ready.clear()
        emit({"event": "human_check", "data": {"active": True, "message": HUMAN_CHECK_MESSAGE}})
        blocked_url = view.url
        deadline = time.monotonic() + settings.human_check_timeout_seconds
        while time.monotonic() < deadline:
            if self.is_stop_requested():
                return "ユーザーの操作で停止しました。"
            if self._human_ready.is_set():
                self._human_ready.clear()
                break
            await asyncio.sleep(HUMAN_CHECK_POLL_SECONDS)
            # 画像の選択中に DOM を読むと、確認画面が描き直されて押せなくなる。
            # アドレスが変わるまでページには触らない。
            current_url = await session.peek()
            if current_url != blocked_url and not is_human_check("", "", current_url):
                break
        else:
            return (
                "ブラウザに出ている確認が終わらなかったため停止しました。"
                "開いているウィンドウで確認を済ませてから、もう一度実行してください。"
            )

        view = await session.read()
        if is_human_check(view.title, view.excerpt, view.url):
            return await self._hold_for_human(emit, session, view, step)
        await self._show_page(emit, session, step, view)
        emit({"event": "human_check", "data": {"active": False, "message": ""}})
        return view

    async def _hold_for_login(
        self, emit: Emit, session: Any, view: PageView, step: int
    ) -> PageView | str:
        """ログイン画面なら、人が入力し終えるまで待つ。"""
        if not needs_user_input(view) or view.url == self._skip_login_url:
            return view
        if self.headless:
            return HEADLESS_INPUT_MESSAGE
        if self._assist_count >= USER_ASSIST_LIMIT:
            return (
                "ログインが必要なため停止しました。"
                "開いているブラウザで入力してから、もう一度実行してください。"
            )
        self._assist_count += 1
        return await self._pause_for_person(emit, session, view, step, LOGIN_MESSAGE)

    async def _hand_off_or_stop(
        self,
        emit: Emit,
        session: Any,
        view: PageView,
        step: int,
        stop_message: str,
        decision: Decision | None = None,
        options: tuple[ActionOption, ...] | None = None,
    ) -> str | None:
        """先に進めないとき、人に入力を頼む。止めるときは理由、続けるときは None。"""
        if decision is not None and options is not None:
            self._emit_step(emit, step, decision, options, "")
        if self.headless or self._assist_count >= USER_ASSIST_LIMIT:
            return stop_message
        self._assist_count += 1
        paused = await self._pause_for_person(emit, session, view, step, STUCK_MESSAGE)
        if isinstance(paused, str):
            return paused
        self._last_action = ""
        self._repeat = 0
        return None

    async def _pause_for_person(
        self, emit: Emit, session: Any, view: PageView, step: int, message: str
    ) -> PageView | str:
        """開いているブラウザで人が入力するまで待つ。終わればそのページ、止めれば理由。"""
        await self._show_page(emit, session, step, view)
        self._human_ready.clear()
        emit({"event": "user_input", "data": {"active": True, "message": message}})
        blocked_url = view.url
        resumed_by_user = False
        deadline = time.monotonic() + settings.human_check_timeout_seconds
        try:
            while time.monotonic() < deadline:
                if self.is_stop_requested():
                    return "ユーザーの操作で停止しました。"
                if await asyncio.to_thread(self._human_ready.wait, HUMAN_CHECK_POLL_SECONDS):
                    self._human_ready.clear()
                    resumed_by_user = True
                    break
                current_url = await session.peek()
                if current_url != blocked_url:
                    break
            else:
                return (
                    "必要な入力が終わらなかったため停止しました。"
                    "開いているウィンドウで入力してから、もう一度実行してください。"
                )
            if resumed_by_user:
                self._skip_login_url = blocked_url
            view = await session.read()
            await self._show_page(emit, session, step, view)
            return view
        finally:
            emit({"event": "user_input", "data": {"active": False, "message": ""}})

    @staticmethod
    async def _show_page(emit: Emit, session: Any, step: int, view: PageView) -> None:
        BrowserAgentRunner._emit_observation(emit, step, view)
        preview = await session.preview_jpeg()
        if preview:
            emit({"event": "screenshot", "data": {"image": preview}})

    async def _needs_stop_for_risk(
        self,
        emit: Emit,
        step: int,
        decision: Decision,
        options: tuple[ActionOption, ...],
    ) -> bool:
        """危険な操作は承認が無いと止める。止めるとき True。"""
        if decision.action_id in SAFE_ACTIONS:
            return False
        if decision.risk_probability < settings.typesafe_risk_threshold:
            return False
        self._approval_granted = None
        self._approval_event.clear()
        description = _description(options, decision.action_id)
        emit(
            {
                "event": "confirm_request",
                "data": {
                    "kind": "action",
                    "step": step,
                    "action": {"id": decision.action_id, "description": description},
                    "reason": (
                        "取り消しにくい操作の可能性があります"
                        f"（{decision.risk_probability:.0%}）。実行してよいか確認してください。"
                    ),
                    "confidence": decision.confidence,
                    "probabilities": decision.probabilities,
                    "timeout_seconds": settings.approval_timeout_seconds,
                },
            }
        )
        return not await asyncio.to_thread(self._wait_for_approval, settings.approval_timeout_seconds)

    def _wait_for_approval(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._approval_event.wait(0.5):
                return bool(self._approval_granted)
            if self.is_stop_requested():
                return False
        return False

    def _repeated(self, action_id: str) -> bool:
        if action_id == self._last_action:
            self._repeat += 1
        else:
            self._last_action = action_id
            self._repeat = 1
        return self._repeat >= REPEAT_LIMIT

    async def _perform(
        self,
        session: Any,
        action_id: str,
        elements: tuple[Element, ...],
        client: httpx.AsyncClient,
        page_title: str,
    ) -> tuple[str, str]:
        """操作を実行し、(入力した文字列, エラー文) を返す。失敗してもループは続ける。"""
        try:
            return await self._apply(session, action_id, elements, client, page_title), ""
        except Exception as e:
            if _browser_closed(e):
                raise
            logger.warning("操作に失敗しました: %s", action_id, exc_info=True)
            return "", str(e)

    async def _apply(
        self,
        session: Any,
        action_id: str,
        elements: tuple[Element, ...],
        client: httpx.AsyncClient,
        page_title: str,
    ) -> str:
        if action_id == "scroll_down":
            await session.scroll()
            return ""
        if action_id == "back":
            await session.back()
            return ""
        if action_id == "wait":
            await session.wait()
            return ""

        kind, element_id = action_id.split(":", 1)
        element = next((item for item in elements if item.id == element_id), None)
        if element is None:
            raise RuntimeError(f"操作対象が見つかりません: {action_id}")
        if kind == "click":
            await session.click(element_id)
            return ""
        if kind == "type":
            if field_purpose(element) == "password":
                return ""
            ask = request_line(self.task_for_model()) or self.task_for_model()
            text = await self.writer.phrase(client, ask, element.name, page_title)
            await session.fill(element_id, text, submits_on_enter(element))
            return text
        raise RuntimeError(f"未知の操作です: {action_id}")

    @staticmethod
    def _emit_observation(emit: Emit, step: int, view: PageView) -> None:
        emit(
            {
                "event": "observation",
                "data": {
                    "step": step,
                    "url": view.url,
                    "title": view.title,
                    "elements": [element.to_dict() for element in view.elements],
                },
            }
        )

    @staticmethod
    def _emit_step(
        emit: Emit,
        step: int,
        decision: Decision,
        options: tuple[ActionOption, ...],
        typed: str,
        error: str = "",
    ) -> None:
        data: dict[str, Any] = {
            "step": step,
            "action": {
                "id": decision.action_id,
                "description": _description(options, decision.action_id),
            },
            "jev": decision.to_dict(),
        }
        if typed:
            data["action"]["text"] = typed
        if error:
            data["error"] = error
        emit({"event": "step", "data": data})


def _browser_closed(exc: Exception) -> bool:
    """利用者が Chromium を閉じたときに Playwright が出すエラーか。"""
    return "has been closed" in str(exc)


def _public_error(exc: Exception) -> str:
    """利用者に見せるエラー文。Playwright の英語はそのまま出さない。"""
    if _browser_closed(exc):
        return "ブラウザが閉じられたため停止しました。"
    return f"実行中にエラーが発生しました: {exc}"


def _description(options: tuple[ActionOption, ...], action_id: str) -> str:
    for option in options:
        if option.id == action_id:
            return option.description
    return action_id
