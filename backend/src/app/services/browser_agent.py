"""DOM を読んで Jev が次の一手を選び、Playwright が実行するブラウザ操作ランナー。

スクショは人が見るプレビューにだけ使い、次の操作の決定には使わない。
"""

import asyncio
import logging
import re
import sys
import threading
import time
from collections.abc import AsyncGenerator, Callable
from dataclasses import replace
from typing import Any

import httpx

from app.core.config import settings
from app.services.browser_session import BrowserSession
from app.services.jev.asker import MISSING_KEY_MESSAGE
from app.services.jev.browser_decider import SAFE_ACTIONS, BrowserDecider, Decision
from app.services.page_state import (
    ActionOption,
    Element,
    PageView,
    action_catalog,
    field_kind,
    field_purpose,
    is_allowed_url,
    is_human_check,
    is_search_field,
    needs_user_input,
    rank_elements,
    redact_secrets,
    send_key,
    request_line,
    start_url,
    submits_on_enter,
)
from app.services.text_writer import TextWriter

logger = logging.getLogger(__name__)
SUBMIT_END = re.compile(
    r"(?:送信(?:ボタン)?を(?:押す|押して|押してください|クリック(?:する|して|してください)?)"
    r"|送信(?:する|して|してください)?"
    r"|送って(?:ください)?|投稿(?:する|して|してください)"
    r"|(?:click|press) (?:the )?(?:send|submit)(?: button)?)[。.!！\s]*$",
    re.IGNORECASE,
)

# ページに答えが揃ったとみなす done の確率
DONE_THRESHOLD = 0.75
# 生成が止まったことを確認するため、最後の回答が連続して変わらない時間。
REPLY_STABLE_SECONDS = 3.0
REPLY_TIMEOUT_SECONDS = 90.0
# 同じページで同じ操作をこの回数選んだら止める。連続していなくても数える（A→B→A→B→A も止める）。
REPEAT_LIMIT = 3
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
UNSENT_MESSAGE = "送信を頼まれましたが、送信まで進めなかったため停止しました。送信はしていません。"
# 入力欄に入れる内容が依頼に無く、入力しなかったときに経過へ出す文
SKIPPED_INPUT_NOTE = "入力しない：依頼にこの欄へ入れる内容がない"
HEADLESS_INPUT_MESSAGE = (
    "人が入力しないと先に進めません。"
    "ブラウザを表示して実行し、そのウィンドウで入力してください。"
)

Emit = Callable[[dict[str, Any] | None], None]


class SubmissionNotConfirmed(RuntimeError):
    """送信後の画面で完了を確認できなかった。"""


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
        self._action_counts: dict[tuple[str, str], int] = {}
        # Jev に渡す実行済み操作。結果（画面が変わったか）は次に読んだときに書き足す。
        self._history: list[str] = []
        self._pending: tuple[str, tuple[Any, ...]] | None = None
        self._assist_count = 0
        self._skip_login_url = ""
        self._typed_fields: set[tuple[str, str, str]] = set()
        # このタスクで入力を終えた欄（名前と種類）。欄が空に戻っても、ページが変わっても入れ直さない。
        # Jev は指示文に書かれた欄を選び直しがちで、履歴を渡すだけでは再入力が止まらなかったため。
        self._finished_inputs: set[tuple[str, str]] = set()
        # このタスクで通ったページの URL。戻り先があるかを知るために、戻ったら末尾を外す。
        self._trail: list[str] = []
        submit_instruction = SUBMIT_END.search(self.task)
        prefix = self.task[:submit_instruction.start()].rstrip().casefold() if submit_instruction else ""
        self._ends_with_submit = bool(
            submit_instruction and not prefix.endswith(("do not", "don't", "never", "without"))
        )
        self._expects_reply = bool(re.search(r"(?:返答|回答|返事|答え).{0,8}(?:教えて|見せて|伝えて|要約して)", self.task))
        if self._expects_reply:
            self._ends_with_submit = True
        self._submit_waits = 0
        # 送信操作をして、確認画面など次のページへ進んだか。進んだ後は done で終えてよい。
        self._submitted = False

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
            return getattr(self.decider, "missing_message", MISSING_KEY_MESSAGE)
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
        except SubmissionNotConfirmed as e:
            emit({"event": "error", "data": {"message": str(e)}})
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
            self._close_history(view)
            self._follow_trail(view.url)

            # 入力済みの同じ欄を Jev が再選択しても、次の操作へ進めるようにする。
            for element in view.elements:
                if element.kind == "type" and not element.filled:
                    self._typed_fields.discard((view.url, element.form_id, element.name))
            available = tuple(
                element for element in view.elements
                if not (element.kind == "type" and _input_key(element) in self._finished_inputs)
            )
            ranked = rank_elements(available, self.task, prefer=self._submit_targets(view))
            options = action_catalog(
                ranked, defer_login=needs_user_input(view), can_go_back=len(self._trail) > 1
            )
            buttons = self._submit_buttons(view)
            if len(buttons) == 1 and buttons[0].disabled:
                if self._submit_waits >= 3 or step == self.max_steps:
                    raise SubmissionNotConfirmed(
                        "入力後も送信ボタンが有効にならなかったため、送信していません。"
                    )
                self._submit_waits += 1
                await session.wait()
                continue
            submit = self._submit_after_input(view)
            decision = submit[0] if submit else await self.decider.decide(
                self.task_for_model(), view, options, tuple(self._history), client=client
            )
            if submit is None and self._ends_with_submit:
                # Jev が自分で送信ボタンを選んだときも、押した後の確認を同じように行う。
                sent_to = _clicked_id(decision.action_id)
                if sent_to and sent_to in self._submit_targets(view, typed_only=False):
                    # 選んだ側の危険度に関わらず、送信は取り消せないので承認を求める。
                    submit = (replace(decision, risk_probability=1.0), _fields_sent_by(view, sent_to))
                    decision = submit[0]
            if submit is not None and submit[0].action_id.startswith("key:"):
                # キーでの送信は候補に無い操作なので、承認と経過の表示のために説明を足す。
                field = _element(view, submit[0].action_id)
                key = send_key(field) if field else None
                label = (key or "Enter").replace("ControlOrMeta", "Ctrl")
                options = options + (
                    ActionOption(submit[0].action_id, f"「{field.name if field else ''}」で {label} を押して送信する"),
                )
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

            if (
                decision.action_id == "done" and self._ends_with_submit and not self._submitted
            ):
                # 送信を頼まれていて、送信操作はまだしていない（送信すればその確認で終わる）。
                # ここで要約を返すと、送れていないのに完了に見えるため、人に引き継ぐか理由を示して止める。
                reason = await self._hand_off_or_stop(
                    emit, session, view, step, UNSENT_MESSAGE, decision, options
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

            if self._repeated(view.url, _description(options, decision.action_id)):
                reason = await self._hand_off_or_stop(
                    emit, session, view, step, "同じ操作を繰り返して進展しないため停止しました。"
                )
                if reason:
                    return reason, step
                continue

            typed, error = await self._perform(
                session, decision.action_id, view.elements, client, view.title, decision.text
            )
            field = _element(view, decision.action_id) if decision.action_id.startswith("type:") else None
            # 依頼にこの欄へ入れる内容が無く、入力しなかった。入れた欄と同じく候補から外し、別の操作へ進ませる。
            skipped = field is not None and not typed and not error
            if field is not None and not error:
                self._finished_inputs.add(_input_key(field))
                if typed:
                    self._typed_fields.add((view.url, field.form_id, field.name))
            self._emit_step(emit, step, decision, options, SKIPPED_INPUT_NOTE if skipped else typed, error)
            self._open_history(
                _description(options, decision.action_id), typed, error, view, skipped=skipped
            )
            if submit is not None:
                if error:
                    raise SubmissionNotConfirmed(
                        "送信操作でエラーが発生し、完了を確認できませんでした。自動再送信はしていません。"
                    )
                if self._expects_reply and "gemini.google.com" in view.url:
                    reply = await self._wait_for_reply(emit, session, view, step)
                    return await self.writer.summary(
                        client, self.task_for_model(), view.url, view.title, reply
                    ), step
                outcome = await self._verify_submission(emit, session, view, submit[1], step)
                if outcome is not None:
                    return outcome, step
                # 確認画面などへ進んだ。最後の送信や完了の確認は、続くステップで行う。
                self._submitted = True

        summary = await self.writer.summary(
            client, self.task_for_model(), view.url, view.title, view.excerpt
        )
        return f"最大ステップ数（{self.max_steps}）に達しました。\n{summary}", step

    async def _wait_for_reply(self, emit: Emit, session: Any, before: PageView, step: int) -> str:
        """Gemini への送信後、新しい回答が現れて生成が落ち着くまで待つ。"""
        deadline = time.monotonic() + REPLY_TIMEOUT_SECONDS
        last_reply = ""
        stable_since = 0.0
        while time.monotonic() < deadline:
            if self.is_stop_requested():
                raise SubmissionNotConfirmed("ユーザーの操作で停止しました。")
            await session.wait()
            after = await session.read()
            self._emit_observation(emit, step, after)
            reply = after.reply.strip()
            if reply and reply != before.reply.strip():
                if reply != last_reply:
                    last_reply = reply
                    stable_since = time.monotonic()
                if not after.reply_busy and time.monotonic() - stable_since >= REPLY_STABLE_SECONDS:
                    return reply
        raise SubmissionNotConfirmed("Gemini の新しい返答を確認できませんでした。送信を繰り返していません。")

    def _submit_after_input(self, view: PageView) -> tuple[Decision, frozenset[tuple[str, str]]] | None:
        """送信が依頼されていて、入力した欄を送れる状態なら次の送信操作と、送信後に空くはずの欄を返す。

        送信ボタンが 1 つに決まればそれを押す。form に属さずボタンも無い欄（キーで送るチャット欄）は、
        欄の案内に従って Enter や Ctrl+Enter を押す。
        どちらも risk を最大にして、実行前に承認を求める。
        """
        candidates = [element for element in self._submit_buttons(view) if not element.disabled]
        if len(candidates) == 1:
            button = candidates[0]
            return _submit_decision(f"click:{button.id}"), _fields_sent_by(view, button.id)
        field = self._enter_field(view)
        if field is None:
            return None
        return _submit_decision(f"key:{field.id}"), frozenset({(field.form_id, field.name)})

    def _enter_field(self, view: PageView) -> Element | None:
        """キーで送る入力欄。送信が依頼され、自分が入れた欄が 1 つで、送信ボタンも form も無く、
        欄の案内から送信のキーが決まるときだけ（「Enter で改行」とだけある欄は送らない）。"""
        if not self._ends_with_submit or self._submit_targets(view):
            return None
        typed = [
            element for element in view.elements
            if element.kind == "type" and element.filled and not submits_on_enter(element)
            and (view.url, element.form_id, element.name) in self._typed_fields
        ]
        if len(typed) != 1 or typed[0].submit_id or typed[0].form_id or send_key(typed[0]) is None:
            return None
        return typed[0]

    def _submit_buttons(self, view: PageView) -> list[Element]:
        """送信が依頼されていれば、入力した欄に対応する送信ボタンを返す。対応先が 1 つに決まらなければ空。"""
        if not self._ends_with_submit:
            return []
        targets = self._submit_targets(view)
        if len(targets) != 1:
            return []
        # 同じ送信ボタンに属する欄が空いていれば、まだ入力の途中。押すのは全部埋めてから。
        if any(
            element.kind == "type" and element.submit_id in targets and not element.filled
            and not is_search_field(element)
            for element in view.elements
        ):
            return []
        return [element for element in view.elements if element.id in targets and element.kind == "click"]

    def _submit_targets(self, view: PageView, *, typed_only: bool = True) -> frozenset[str]:
        """入力済みの欄の送信ボタンの id。ボタンはページの構造から決まり、文言には依らない。

        typed_only のときは、このタスクで自分が入力した欄だけを見る。
        検索欄は除く。入力時の Enter で検索済みで、送信の依頼はメッセージなど別の欄に向けたものだから。
        """
        return frozenset(
            element.submit_id for element in view.elements
            if element.kind == "type" and element.filled and element.submit_id
            and not is_search_field(element)
            and (not typed_only or (view.url, element.form_id, element.name) in self._typed_fields)
        )

    async def _verify_submission(
        self, emit: Emit, session: Any, before: PageView, fields: frozenset[tuple[str, str]], step: int
    ) -> str | None:
        """送信操作の後の画面を確認する。fields は送信で空くはずの欄。判断できない送信は送り直さない。

        完了が分かれば結果の文を返す。確認画面など別の画面へ進んだときは None を返し、続きのステップに任せる。
        """
        cleared = False
        for _ in range(3):
            await session.wait()
            after = await session.read()
            self._emit_observation(emit, step, after)
            feedback = after.feedback if after.feedback != before.feedback else ""
            if re.search(r"送信.{0,12}(?:失敗|できません|エラー)|failed to send|send failed", feedback, re.IGNORECASE):
                raise SubmissionNotConfirmed("画面に送信失敗が表示されました。自動再送信はしていません。")
            if re.search(r"送信(?:しました|完了|済み)|message sent|sent successfully", feedback, re.IGNORECASE):
                return "画面に送信完了が表示されました。"
            remaining = [
                element for element in after.elements
                if element.kind == "type" and (element.form_id, element.name) in fields
            ]
            if after.url != before.url or (fields and not remaining):
                return None
            cleared = any(not element.filled for element in remaining)
        if cleared:
            return "送信操作をし、入力欄がクリアされたことを確認しました。"
        raise SubmissionNotConfirmed(
            "送信操作をしましたが、完了を確認できませんでした。自動再送信はしていません。"
        )

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
        # 人が操作した後は状況が変わるため、繰り返しの数え直しをする。
        self._action_counts.clear()
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

    def _repeated(self, url: str, description: str) -> bool:
        """同じページで同じ操作を選んだ回数を数え、上限に達したら True。

        要素の id は読み直すたびに振り直されるため、id ではなく操作の説明（要素の名前と種類）で数える。
        """
        key = (url, description)
        self._action_counts[key] = self._action_counts.get(key, 0) + 1
        return self._action_counts[key] >= REPEAT_LIMIT

    def _follow_trail(self, url: str) -> None:
        """通ったページの列を更新する。1 つ前の URL に来たら戻ったとみなして末尾を外す。"""
        if self._trail and self._trail[-1] == url:
            return
        if len(self._trail) >= 2 and self._trail[-2] == url:
            self._trail.pop()
            return
        self._trail.append(url)

    def _open_history(
        self, description: str, typed: str, error: str, view: PageView, *, skipped: bool = False
    ) -> None:
        """実行した操作を履歴に仮置きする。結果は次に読んだページで決める。"""
        if skipped:
            self._history.append(f"{description} → 依頼に合う内容が無いため入力しなかった")
            self._pending = None
            return
        entry = description + (f"（入力:「{typed}」）" if typed else "")
        if error:
            self._history.append(f"{entry} → 失敗")
            self._pending = None
            return
        self._pending = (entry, _page_signature(view))

    def _close_history(self, view: PageView) -> None:
        """仮置きした操作に、画面が変わったかを書き足して確定する。"""
        if self._pending is None:
            return
        entry, before = self._pending
        self._pending = None
        changed = _page_signature(view) != before
        self._history.append(f"{entry} → {'画面が変わった' if changed else '画面の変化なし'}")

    async def _perform(
        self,
        session: Any,
        action_id: str,
        elements: tuple[Element, ...],
        client: httpx.AsyncClient,
        page_title: str,
        text: str | None = None,
    ) -> tuple[str, str]:
        """操作を実行し、(入力した文字列, エラー文) を返す。失敗してもループは続ける。"""
        try:
            return await self._apply(session, action_id, elements, client, page_title, text), ""
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
        text: str | None = None,
    ) -> str:
        """操作を実行する。text は選んだ側が決めた入力文字列で、None なら依頼から作る。"""
        if action_id == "back":
            await session.back()
            return ""
        if action_id == "wait":
            await session.wait()
            return ""

        kind, element_ref = action_id.split(":", 1)
        element_id, _, choice_index = element_ref.partition("#")
        element = next((item for item in elements if item.id == element_id), None)
        if element is None:
            raise RuntimeError(f"操作対象が見つかりません: {action_id}")
        if kind == "click":
            await session.click(element_id)
            return ""
        if kind == "select":
            if not choice_index.isdecimal():
                raise RuntimeError("プルダウンで選ぶ選択肢が決まっていません。")
            choice = next((item for item in element.choices if item.index == int(choice_index)), None)
            if choice is None:
                raise RuntimeError("プルダウンの選択肢が見つかりません。")
            await session.select(element_id, choice.index, choice.label, choice.value)
            return choice.label
        if kind == "key":
            key = send_key(element)
            if key is None:
                raise RuntimeError("この入力欄を送信するキーが分かりません。")
            await session.press_key(element_id, key)
            return ""
        if kind == "type":
            if field_purpose(element) == "password":
                return ""
            if text is None:
                ask = request_line(self.task_for_model()) or self.task_for_model()
                text = await self.writer.phrase(client, ask, element.name, page_title, field_kind(element))
            if not text:
                return ""
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


def _page_signature(view: PageView) -> tuple[Any, ...]:
    """操作の前後で画面が変わったかを比べるための値。要素の id は読み直しで変わるため使わない。"""
    return (
        view.url,
        view.title,
        view.excerpt,
        tuple((element.kind, element.name, element.filled, element.disabled) for element in view.elements),
    )


def _input_key(element: Element) -> tuple[str, str]:
    """入力済みかを覚えるための欄の鍵。id はページを読むたびに変わるため、名前と種類で見分ける。"""
    return (element.name, field_kind(element))


def _submit_decision(action_id: str) -> Decision:
    """構造から決めた送信操作。取り消せないので risk を最大にし、必ず承認を求める。"""
    return Decision(
        action_id=action_id,
        confidence=1.0,
        risk_probability=1.0,
        model="form-submit",
        reason="入力した欄の送信操作。送信を頼まれているため、承認のうえで実行する。",
    )


def _fields_sent_by(view: PageView, button_id: str) -> frozenset[tuple[str, str]]:
    """送信ボタンで送られる、入力済みの欄。"""
    return frozenset(
        (element.form_id, element.name) for element in view.elements
        if element.kind == "type" and element.submit_id == button_id and element.filled
    )


def _element(view: PageView, action_id: str) -> Element | None:
    """click:e3 や key:e1 の操作対象の要素。"""
    _, _, element_id = action_id.partition(":")
    element_id = element_id.partition("#")[0]
    return next((element for element in view.elements if element.id == element_id), None)


def _clicked_id(action_id: str) -> str:
    """click:e3 のような操作から要素の id を取り出す。クリック以外は空。"""
    kind, _, element_id = action_id.partition(":")
    return element_id if kind == "click" else ""


def _description(options: tuple[ActionOption, ...], action_id: str) -> str:
    for option in options:
        if option.id == action_id:
            return option.description
    return action_id
