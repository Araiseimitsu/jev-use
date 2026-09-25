"""ブラウザ操作ループのテスト。実ブラウザは起動しない。"""

import asyncio

import pytest

from app.core.config import settings
from app.services.browser_agent import BrowserAgentRunner
from app.services.jev.browser_decider import Decision
from app.services.page_state import Element, PageView, SelectChoice, rank_elements


class FakeSession:
    """操作の記録だけを残すページ。"""

    def __init__(self, view: PageView) -> None:
        self.view = view
        self.later: PageView | None = None
        self.started: str | None = None
        self.actions: list[tuple] = []
        self.closed = False
        self.reads = 0
        self.peeks = 0
        self.previews = 0

    async def start(self, url: str) -> None:
        self.started = url

    async def read(self) -> PageView:
        self.reads += 1
        return self.view

    async def peek(self) -> str:
        """確認待ちのあいだはアドレスだけを見る。2 回目で次のページへ進んだことにする。"""
        self.peeks += 1
        if self.peeks >= 2 and self.later is not None:
            self.view = self.later
            self.later = None
        return self.view.url

    async def click(self, element_id: str) -> None:
        self.actions.append(("click", element_id))

    async def fill(self, element_id: str, text: str, submit: bool) -> None:
        self.actions.append(("fill", element_id, text, submit))

    async def press_key(self, element_id: str, key: str) -> None:
        self.actions.append(("key", element_id, key))

    async def select(self, element_id: str, index: int, label: str, value: str) -> None:
        self.actions.append(("select", element_id, index, label, value))

    async def back(self) -> None:
        self.actions.append(("back",))

    async def wait(self) -> None:
        self.actions.append(("wait",))

    async def preview_jpeg(self) -> None:
        self.previews += 1
        return None

    async def close(self) -> None:
        self.closed = True


class ScriptedDecider:
    enabled = True

    def __init__(self, decisions: list[Decision]) -> None:
        self.decisions = decisions
        self.calls = 0
        self.tasks: list[str] = []
        self.histories: list[tuple[str, ...]] = []

    async def decide(
        self, task: str, view: PageView, options: tuple, history: tuple[str, ...] = (), **_: object
    ) -> Decision:
        self.tasks.append(task)
        self.histories.append(history)
        decision = self.decisions[min(self.calls, len(self.decisions) - 1)]
        self.calls += 1
        return decision


class FakeWriter:
    def __init__(self, phrase: str = "東京 天気", summary: str = "晴れ、25度") -> None:
        self.phrase_text = phrase
        self.summary_text = summary
        self.phrase_tasks: list[str] = []

    async def phrase(
        self, client: object, task: str, field_name: str, page_title: str, field_kind: str = ""
    ) -> str:
        self.phrase_tasks.append(task)
        return self.phrase_text

    async def summary(self, client: object, task: str, url: str, title: str, excerpt: str) -> str:
        return self.summary_text


VIEW = PageView(
    url="https://www.google.com",
    title="Google",
    excerpt="検索",
    elements=(
        Element("e1", "searchbox", "検索", "type"),
        Element("e2", "link", "天気", "click"),
    ),
)


def _decision(action_id: str, risk: float = 0.1, done: float = 0.1, **kwargs: object) -> Decision:
    return Decision(action_id=action_id, confidence=0.9, risk_probability=risk, done_probability=done, **kwargs)  # type: ignore[arg-type]


def _runner(session: FakeSession, decisions: list[Decision], **kwargs: object) -> BrowserAgentRunner:
    return BrowserAgentRunner(
        task="東京の天気を調べて",
        max_steps=5,
        decider=ScriptedDecider(decisions),  # type: ignore[arg-type]
        writer=FakeWriter(),  # type: ignore[arg-type]
        session_factory=lambda: session,
        **kwargs,  # type: ignore[arg-type]
    )


def _events(runner: BrowserAgentRunner) -> list[dict]:
    async def collect() -> list[dict]:
        found = []
        async for event in runner.run_stream():
            found.append(event)
            if event["event"] == "confirm_request":
                runner.resolve_approval(False)
        return found

    return asyncio.run(collect())


def _approved_events(runner: BrowserAgentRunner) -> list[dict]:
    async def collect() -> list[dict]:
        found = []
        async for event in runner.run_stream():
            found.append(event)
            if event["event"] == "confirm_request":
                runner.resolve_approval(True)
        return found

    return asyncio.run(collect())


def test_human_check_waits_until_the_person_finishes(monkeypatch) -> None:
    monkeypatch.setattr(settings, "human_check_timeout_seconds", 5)
    challenge = PageView("https://www.google.com/sorry/index", "あなたは人間ですか？", "私はロボットではありません", ())
    session = FakeSession(challenge)
    session.later = VIEW
    events = _events(_runner(session, [_decision("done", done=0.9)], headless=False))

    assert session.actions == []
    assert session.reads == 2
    assert session.previews == 2
    assert any(event["event"] == "human_check" and event["data"]["active"] for event in events)
    assert events[-1]["data"]["result"] == "晴れ、25度"


def test_chosen_click_runs_by_element_id_then_stops_when_done() -> None:
    session = FakeSession(VIEW)
    events = _events(_runner(session, [_decision("click:e2"), _decision("done", done=0.9)]))

    assert session.started == "https://www.google.com"
    assert session.actions == [("click", "e2")]
    assert session.closed is True
    complete = events[-1]
    assert complete["event"] == "complete"
    assert complete["data"]["result"] == "晴れ、25度"


def test_type_action_fills_the_field_and_submits_search() -> None:
    session = FakeSession(VIEW)
    _events(_runner(session, [_decision("type:e1"), _decision("done", done=0.9)]))

    assert session.actions[0] == ("fill", "e1", "東京 天気", True)


def test_message_is_typed_before_send_becomes_an_option() -> None:
    class ChatSession(FakeSession):
        async def fill(self, element_id: str, text: str, submit: bool) -> None:
            await super().fill(element_id, text, submit)
            self.view = PageView(
                self.view.url,
                self.view.title,
                self.view.excerpt,
                (
                    Element("e1", "textarea", "メッセージ", "type", filled=True, form_id="f1", submit_id="e2"),
                    Element("e2", "button", "送信", "click", input_type="submit", form_id="f1"),
                ),
            )

        async def click(self, element_id: str) -> None:
            await super().click(element_id)
            self.view = PageView(
                self.view.url,
                self.view.title,
                self.view.excerpt,
                (
                    Element("e1", "textarea", "メッセージ", "type", form_id="f1", submit_id="e2"),
                    Element("e2", "button", "送信", "click", input_type="submit", disabled=True, form_id="f1"),
                ),
            )

    class ChatDecider:
        enabled = True

        async def decide(
            self, task: str, view: PageView, options: tuple, history: tuple[str, ...] = (), **_: object
        ) -> Decision:
            ids = {option.id for option in options}
            if "type:e1" in ids:
                return _decision("type:e1")
            assert "click:e2" in ids
            return _decision("wait")

    session = ChatSession(
        PageView(
            "https://example.com/chat",
            "会話",
            "",
            (
                Element("e1", "textarea", "メッセージ", "type", form_id="f1", submit_id="e2"),
                Element("e2", "button", "送信", "click", input_type="submit", disabled=True, form_id="f1"),
            ),
        )
    )
    runner = BrowserAgentRunner(
        task="https://example.com/chat でhelloと入力し、送信ボタンを押す",
        max_steps=4,
        headless=True,
        decider=ChatDecider(),  # type: ignore[arg-type]
        writer=FakeWriter(phrase="hello"),  # type: ignore[arg-type]
        session_factory=lambda: session,
    )

    events = _approved_events(runner)

    assert [action for action in session.actions if action[0] != "wait"] == [
        ("fill", "e1", "hello", False), ("click", "e2")
    ]
    assert session.reads >= 3
    assert any(event["event"] == "confirm_request" for event in events)
    assert not any(event["event"] == "user_input" for event in events)
    assert "入力欄がクリア" in events[-1]["data"]["result"]


def test_submit_follow_up_uses_the_fields_structural_button() -> None:
    url = "https://example.com/chat"
    view = PageView(url, "会話", "", (
        Element("e0", "textarea", "メッセージ", "type", filled=True, form_id="f1", submit_id="e2"),
        Element("e1", "button", "送信", "click", input_type="submit", form_id="f2"),
        Element("e2", "button", "Send message", "click", form_id="f1"),
    ))
    # 指示文にボタンの文言が無くても、入力した欄の送信ボタンを押す。
    runner = BrowserAgentRunner(task="これ送って")
    runner._typed_fields.add((url, "f1", "メッセージ"))
    submit = runner._submit_after_input(view)
    assert submit is not None and submit[0].action_id == "click:e2"

    not_requested = BrowserAgentRunner(task="メッセージを入力する。送信ボタンは押さない")
    not_requested._typed_fields.add((url, "f1", "メッセージ"))
    assert not_requested._submit_after_input(view) is None
    english_negative = BrowserAgentRunner(task="Type hello, but do not click Send")
    english_negative._typed_fields.add((url, "f1", "メッセージ"))
    assert english_negative._submit_after_input(view) is None

    # ページ側で送信ボタンを 1 つに決められなかった欄は押さない。
    unknown = PageView(url, "会話", "", (
        Element("e0", "textarea", "メッセージ", "type", filled=True, form_id="f1"),
        Element("e2", "button", "送信", "click", input_type="submit", form_id="f1"),
    ))
    assert runner._submit_after_input(unknown) is None
    # 自分が入力していない欄の送信ボタンは押さない。
    untouched = BrowserAgentRunner(task="送信して")
    assert untouched._submit_after_input(view) is None


def test_typed_field_puts_its_send_button_into_the_candidates() -> None:
    url = "https://example.com/chat"
    links = tuple(Element(f"l{i}", "link", f"メニュー{i}", "click") for i in range(20))
    view = PageView(url, "会話", "", links + (
        Element("e1", "textarea", "メッセージ", "type", filled=True, submit_id="e2"),
        Element("e2", "button", "↑", "click"),
    ))
    runner = BrowserAgentRunner(task="helloと書いて")
    runner._typed_fields.add((url, "", "メッセージ"))

    ranked = rank_elements(view.elements, runner.task, prefer=runner._submit_targets(view))
    assert ranked[0].id == "e2"


def test_submit_waits_for_button_to_become_enabled() -> None:
    class DelayedChat(FakeSession):
        async def fill(self, element_id: str, text: str, submit: bool) -> None:
            await super().fill(element_id, text, submit)
            self.view = PageView(self.view.url, self.view.title, "", (
                Element("e1", "textarea", "メッセージ", "type", filled=True, form_id="f1", submit_id="e2"),
                Element("e2", "button", "送信", "click", input_type="submit", disabled=True, form_id="f1"),
            ))

        async def wait(self) -> None:
            await super().wait()
            if any(action[0] == "click" for action in self.actions):
                return
            self.view = PageView(self.view.url, self.view.title, "", (
                Element("e1", "textarea", "メッセージ", "type", filled=True, form_id="f1", submit_id="e2"),
                Element("e2", "button", "送信", "click", input_type="submit", form_id="f1"),
            ))

        async def click(self, element_id: str) -> None:
            await super().click(element_id)
            self.view = PageView(self.view.url, self.view.title, "", (
                Element("e1", "textarea", "メッセージ", "type", form_id="f1", submit_id="e2"),
                Element("e2", "button", "送信", "click", input_type="submit", disabled=True, form_id="f1"),
            ))

    session = DelayedChat(PageView("https://example.com/chat", "会話", "", (
        Element("e1", "textarea", "メッセージ", "type", form_id="f1", submit_id="e2"),
        Element("e2", "button", "送信", "click", input_type="submit", disabled=True, form_id="f1"),
    )))
    runner = BrowserAgentRunner(
        task="https://example.com/chat でhelloと入力し、送信ボタンを押す",
        max_steps=5,
        headless=True,
        decider=ScriptedDecider([_decision("type:e1"), _decision("wait")]),  # type: ignore[arg-type]
        writer=FakeWriter(phrase="hello"),  # type: ignore[arg-type]
        session_factory=lambda: session,
    )

    events = _approved_events(runner)

    assert [action[0] for action in session.actions[:3]] == ["fill", "wait", "click"]
    assert "入力欄がクリア" in events[-1]["data"]["result"]


def test_submit_without_visible_change_is_not_retried() -> None:
    class UnchangedChat(FakeSession):
        async def fill(self, element_id: str, text: str, submit: bool) -> None:
            await super().fill(element_id, text, submit)
            self.view = PageView(
                self.view.url, self.view.title, self.view.excerpt,
                (
                    Element("e1", "textarea", "メッセージ", "type", filled=True, form_id="f1", submit_id="e2"),
                    Element("e2", "button", "送信", "click", input_type="submit", form_id="f1"),
                ),
            )

    session = UnchangedChat(PageView(
        "https://example.com/chat", "会話", "",
        (
            Element("e1", "textarea", "メッセージ", "type", form_id="f1", submit_id="e2"),
            Element("e2", "button", "送信", "click", input_type="submit", disabled=True, form_id="f1"),
        ),
    ))
    runner = BrowserAgentRunner(
        task="https://example.com/chat でhelloと入力し、送信ボタンを押す",
        max_steps=5,
        headless=True,
        decider=ScriptedDecider([_decision("type:e1"), _decision("wait")]),  # type: ignore[arg-type]
        writer=FakeWriter(phrase="hello"),  # type: ignore[arg-type]
        session_factory=lambda: session,
    )

    events = _approved_events(runner)

    assert session.actions.count(("click", "e2")) == 1
    assert events[-1]["event"] == "error"
    assert "確認できません" in events[-1]["data"]["message"]


def test_submit_click_error_is_not_retried() -> None:
    class ClickErrorChat(FakeSession):
        async def fill(self, element_id: str, text: str, submit: bool) -> None:
            await super().fill(element_id, text, submit)
            self.view = PageView(self.view.url, self.view.title, "", (
                Element("e1", "textarea", "メッセージ", "type", filled=True, form_id="f1", submit_id="e2"),
                Element("e2", "button", "送信", "click", input_type="submit", form_id="f1"),
            ))

        async def click(self, element_id: str) -> None:
            await super().click(element_id)
            raise RuntimeError("click timed out")

    session = ClickErrorChat(PageView("https://example.com/chat", "会話", "", (
        Element("e1", "textarea", "メッセージ", "type", form_id="f1", submit_id="e2"),
        Element("e2", "button", "送信", "click", input_type="submit", disabled=True, form_id="f1"),
    )))
    runner = BrowserAgentRunner(
        task="https://example.com/chat でhelloと入力し、送信ボタンを押してください",
        max_steps=5,
        headless=True,
        decider=ScriptedDecider([_decision("type:e1")]),  # type: ignore[arg-type]
        writer=FakeWriter(phrase="hello"),  # type: ignore[arg-type]
        session_factory=lambda: session,
    )

    events = _approved_events(runner)

    assert session.actions.count(("click", "e2")) == 1
    assert events[-1]["event"] == "error"
    assert "自動再送信はしていません" in events[-1]["data"]["message"]


def test_post_submit_error_takes_precedence_over_cleared_input() -> None:
    before = PageView("https://example.com/chat", "会話", "", (
        Element("e1", "textarea", "メッセージ", "type", filled=True, form_id="f1", submit_id="e2"),
    ))
    after = PageView("https://example.com/chat", "会話", "", (
        Element("e1", "textarea", "メッセージ", "type", form_id="f1", submit_id="e2"),
    ), feedback="送信に失敗しました")
    session = FakeSession(after)
    runner = BrowserAgentRunner(task="送信ボタンを押す")

    with pytest.raises(RuntimeError, match="送信失敗"):
        asyncio.run(runner._verify_submission(lambda event: None, session, before, "e2", 1))


def test_jev_selected_send_on_prefilled_form_is_verified() -> None:
    class PrefilledChat(FakeSession):
        async def click(self, element_id: str) -> None:
            await super().click(element_id)
            self.view = PageView(self.view.url, self.view.title, "", (
                Element("e1", "textarea", "メッセージ", "type", form_id="f1", submit_id="e2"),
                Element("e2", "button", "送信", "click", input_type="submit", disabled=True, form_id="f1"),
            ))

    session = PrefilledChat(PageView("https://example.com/chat", "会話", "", (
        Element("e1", "textarea", "メッセージ", "type", filled=True, form_id="f1", submit_id="e2"),
        Element("e2", "button", "送信", "click", input_type="submit", form_id="f1"),
    )))
    runner = BrowserAgentRunner(
        task="https://example.com/chat で送信ボタンを押す",
        max_steps=3,
        headless=True,
        decider=ScriptedDecider([_decision("click:e2")]),  # type: ignore[arg-type]
        writer=FakeWriter(),  # type: ignore[arg-type]
        session_factory=lambda: session,
    )

    events = _approved_events(runner)

    assert session.actions.count(("click", "e2")) == 1
    assert session.reads >= 2
    # 選んだ側の危険度に関わらず、送信は承認を経る。
    assert any(event["event"] == "confirm_request" for event in events)
    assert "入力欄がクリア" in events[-1]["data"]["result"]


def _login_view() -> PageView:
    return PageView(
        url="https://example.com/login",
        title="ログイン",
        excerpt="ログイン",
        elements=(
            Element("e1", "textbox", "ユーザー名", "type", "text"),
            Element("e2", "textbox", "パスワード", "type", "password"),
        ),
    )


def test_login_page_waits_for_the_person_then_continues(monkeypatch) -> None:
    import json

    monkeypatch.setattr(settings, "human_check_timeout_seconds", 5)
    session = FakeSession(_login_view())
    session.later = PageView("https://example.com/home", "予定", "今日の予定", ())
    runner = BrowserAgentRunner(
        task=(
            "https://example.com/login\n"
            "ユーザー名: from-task\n"
            "パスワード: pasted-secret\n"
            "- スケジュールが面で今日のすべての予定を教えて"
        ),
        max_steps=4,
        headless=False,
        decider=ScriptedDecider([_decision("done", done=0.9)]),  # type: ignore[arg-type]
        writer=FakeWriter(),  # type: ignore[arg-type]
        session_factory=lambda: session,
    )

    events = _events(runner)
    dumped = json.dumps(events, ensure_ascii=False)

    assert session.actions == []
    assert any(event["event"] == "user_input" and event["data"]["active"] for event in events)
    assert "pasted-secret" not in dumped
    assert events[-1]["data"]["result"] == "晴れ、25度"


def test_headless_login_stops_without_typing() -> None:
    session = FakeSession(_login_view())
    events = _events(_runner(session, [_decision("type:e1")], headless=True))

    assert session.actions == []
    assert "人が入力しないと先に進めません" in events[-1]["data"]["result"]


def test_other_fields_get_the_request_line() -> None:
    session = FakeSession(VIEW)
    writer = FakeWriter()
    runner = BrowserAgentRunner(
        task="https://example.com\nパスワード: pasted-secret\n- 今日の予定を教えて",
        max_steps=3,
        decider=ScriptedDecider([_decision("type:e1"), _decision("done", done=0.9)]),  # type: ignore[arg-type]
        writer=writer,  # type: ignore[arg-type]
        session_factory=lambda: session,
    )

    _events(runner)

    assert writer.phrase_tasks == ["今日の予定を教えて"]
    assert session.actions[0] == ("fill", "e1", "東京 天気", True)


def test_stuck_page_resumes_after_the_person_continues(monkeypatch) -> None:
    monkeypatch.setattr(settings, "human_check_timeout_seconds", 5)
    session = FakeSession(VIEW)
    runner = _runner(session, [_decision("wait", used_fallback=True, message="選べませんでした。"), _decision("done", done=0.9)], headless=False)

    async def collect() -> list[dict]:
        found = []

        async def continue_when_asked() -> None:
            for _ in range(50):
                await asyncio.sleep(0.05)
                if runner._human_ready.is_set() is False and any(
                    event["event"] == "user_input" and event["data"]["active"] for event in found
                ):
                    runner.mark_human_ready()
                    return
            runner.mark_human_ready()

        waiter = asyncio.create_task(continue_when_asked())
        async for event in runner.run_stream():
            found.append(event)
        await waiter
        return found

    events = asyncio.run(collect())

    assert any(event["event"] == "user_input" and event["data"]["active"] for event in events)
    assert events[-1]["data"]["result"] == "晴れ、25度"


def test_high_risk_without_approval_does_not_click(monkeypatch) -> None:
    monkeypatch.setattr(settings, "typesafe_risk_threshold", 0.7)
    monkeypatch.setattr(settings, "approval_timeout_seconds", 5)
    session = FakeSession(VIEW)

    events = _events(_runner(session, [_decision("click:e2", risk=0.95)]))

    assert session.actions == []
    assert any(event["event"] == "confirm_request" for event in events)
    assert "承認" in events[-1]["data"]["result"]


def test_fallback_stops_without_an_action() -> None:
    session = FakeSession(VIEW)
    events = _events(
        _runner(
            session,
            [_decision("wait", used_fallback=True, message="選べませんでした。")],
            headless=True,
        )
    )

    assert session.actions == []
    assert events[-1]["data"]["result"] == "選べませんでした。"


def test_closed_browser_is_explained_in_japanese() -> None:
    from app.services.browser_agent import _public_error

    message = _public_error(RuntimeError("Target page, context or browser has been closed"))

    assert message == "ブラウザが閉じられたため停止しました。"


def test_closing_the_stream_early_still_closes_the_browser(monkeypatch) -> None:
    """画面の停止や切断で受信側が途中で抜けても、ブラウザを閉じてから終わる。"""
    import threading

    monkeypatch.setattr(settings, "approval_timeout_seconds", 30)
    session = FakeSession(VIEW)
    closed = threading.Event()
    original_close = session.close

    async def close() -> None:
        await original_close()
        closed.set()

    session.close = close  # type: ignore[method-assign]
    runner = _runner(session, [_decision("click:e2", risk=0.95)])

    async def leave_at_confirm() -> None:
        stream = runner.run_stream()
        async for event in stream:
            if event["event"] == "confirm_request":
                break
        await stream.aclose()

    asyncio.run(leave_at_confirm())

    assert closed.wait(5)
    assert session.actions == []


def test_same_action_three_times_stops() -> None:
    session = FakeSession(VIEW)
    click = _decision("click:e2")
    events = _events(_runner(session, [click, click, click, click], headless=True))

    assert session.actions == [("click", "e2"), ("click", "e2")]
    assert "繰り返して" in events[-1]["data"]["result"]


def test_actions_that_cycle_on_the_same_page_stop() -> None:
    """A→B→A→B→A のように、連続しなくても同じページで同じ操作を繰り返したら止める。"""
    session = FakeSession(VIEW)
    decisions = [_decision("back"), _decision("click:e2")] * 3
    events = _events(_runner(session, decisions, headless=True))

    assert session.actions == [("back",), ("click", "e2"), ("back",), ("click", "e2")]
    assert "繰り返して" in events[-1]["data"]["result"]


def test_previous_actions_are_passed_to_jev() -> None:
    """Jev が同じ入力を繰り返さないよう、実行済みの操作と入力した文字列を渡す。"""
    session = FakeSession(VIEW)
    decider = ScriptedDecider([_decision("type:e1"), _decision("done", done=0.9)])
    runner = BrowserAgentRunner(
        task="東京の天気を調べて",
        max_steps=5,
        decider=decider,  # type: ignore[arg-type]
        writer=FakeWriter(),  # type: ignore[arg-type]
        session_factory=lambda: session,
    )
    _events(runner)

    assert decider.histories[0] == ()
    assert len(decider.histories[1]) == 1
    entry = decider.histories[1][0]
    assert "検索" in entry and "東京 天気" in entry
    # FakeSession は操作しても画面が変わらない。その事実も伝える。
    assert "変化なし" in entry


def test_task_ending_with_bare_send_noun_asks_to_submit() -> None:
    assert BrowserAgentRunner(task="ルームを開いてメッセージにtestを入れて送信")._ends_with_submit
    assert BrowserAgentRunner(task="testと入力して送信。")._ends_with_submit
    assert not BrowserAgentRunner(task="送信履歴を見て")._ends_with_submit


def test_field_without_send_button_submits_with_enter_after_approval() -> None:
    """送信ボタンの無いチャット欄（Enter で送る）は、承認を経て Enter を押し、欄が空いたことを確かめる。"""

    class EnterChat(FakeSession):
        async def fill(self, element_id: str, text: str, submit: bool) -> None:
            await super().fill(element_id, text, submit)
            self.view = PageView(self.view.url, self.view.title, "", (
                Element("e1", "textarea", "メッセージを入力", "type", filled=True),
            ))

        async def press_key(self, element_id: str, key: str) -> None:
            await super().press_key(element_id, key)
            self.view = PageView(self.view.url, self.view.title, "test", (
                Element("e1", "textarea", "メッセージを入力", "type"),
            ))

    session = EnterChat(PageView("https://example.com/room/1", "test", "", (
        Element("e1", "textarea", "メッセージを入力", "type"),
    )))
    runner = BrowserAgentRunner(
        task="https://example.com/room/1 のメッセージにtestを入れて送信",
        max_steps=4,
        headless=True,
        decider=ScriptedDecider([_decision("type:e1")]),  # type: ignore[arg-type]
        writer=FakeWriter(phrase="test"),  # type: ignore[arg-type]
        session_factory=lambda: session,
    )

    events = _approved_events(runner)

    assert [action for action in session.actions if action[0] != "wait"] == [
        ("fill", "e1", "test", False), ("key", "e1", "Enter")
    ]
    confirm = next(event for event in events if event["event"] == "confirm_request")
    assert "Enter" in confirm["data"]["action"]["description"]
    assert "入力欄がクリア" in events[-1]["data"]["result"]


def test_enter_is_not_pressed_in_a_form_or_without_request() -> None:
    url = "https://example.com/chat"
    loose = PageView(url, "会話", "", (Element("e1", "textarea", "メッセージ", "type", filled=True),))
    runner = BrowserAgentRunner(task="testと入れて送信して")
    runner._typed_fields.add((url, "", "メッセージ"))
    submit = runner._submit_after_input(loose)
    assert submit is not None and submit[0].action_id == "key:e1"

    # form の中で送信ボタンを決められない欄は、Enter でも送らない。
    in_form = PageView(url, "会話", "", (
        Element("e1", "textarea", "メッセージ", "type", filled=True, form_id="f1"),
        Element("e2", "button", "送信", "click", form_id="f1"),
    ))
    runner._typed_fields.add((url, "f1", "メッセージ"))
    assert runner._submit_after_input(in_form) is None

    not_requested = BrowserAgentRunner(task="testと入れて")
    not_requested._typed_fields.add((url, "", "メッセージ"))
    assert not_requested._submit_after_input(loose) is None


def test_field_typed_once_is_not_offered_again_even_after_it_is_cleared() -> None:
    """入力後に欄が空に戻っても（Enter で検索、描き直し、別ページから戻るなど）、同じ欄には入れ直さない。"""

    class ClearingSearch(FakeSession):
        async def fill(self, element_id: str, text: str, submit: bool) -> None:
            await super().fill(element_id, text, submit)
            # 検索すると URL が変わり、欄は空に戻り、結果の行が出る。
            self.view = PageView("https://example.com/rooms?q=test", "ルーム", "", (
                Element("e1", "input", "ルームを検索...", "type", input_type="search"),
                Element("e2", "item", "test", "click"),
            ))

    class PrefersTyping:
        enabled = True

        def __init__(self) -> None:
            self.offered: list[set[str]] = []

        async def decide(self, task, view, options, history=(), **_) -> Decision:
            ids = {option.id for option in options}
            self.offered.append(ids)
            if "type:e1" in ids:
                return _decision("type:e1")
            return _decision("click:e2") if "click:e2" in ids else _decision("done", done=0.9)

    session = ClearingSearch(PageView("https://example.com/rooms", "ルーム", "", (
        Element("e1", "input", "ルームを検索...", "type", input_type="search"),
    )))
    decider = PrefersTyping()
    runner = BrowserAgentRunner(
        task="ルーム検索にtestと入れてルームを開く",
        max_steps=3,
        headless=True,
        decider=decider,  # type: ignore[arg-type]
        writer=FakeWriter(phrase="test"),  # type: ignore[arg-type]
        session_factory=lambda: session,
    )
    _events(runner)

    assert [action[:2] for action in session.actions][:2] == [("fill", "e1"), ("click", "e2")]
    assert all("type:e1" not in offered for offered in decider.offered[1:])


def test_failed_input_can_be_tried_again() -> None:
    class FailingOnce(FakeSession):
        failed = False

        async def fill(self, element_id: str, text: str, submit: bool) -> None:
            if not self.failed:
                self.failed = True
                raise RuntimeError("入力欄に指定した文字列が反映されませんでした。")
            await super().fill(element_id, text, submit)

    session = FailingOnce(VIEW)
    _events(_runner(session, [_decision("type:e1"), _decision("type:e1"), _decision("done", done=0.9)]))

    assert [action[0] for action in session.actions] == ["fill"]


def test_field_with_nothing_to_enter_is_skipped_and_not_offered_again() -> None:
    """依頼に合う内容が無い欄（メッセージを送りたいのに検索欄しか無いなど）には入れず、別の操作へ進ませる。"""
    view = PageView("https://example.com/chat", "チャット", "", (
        Element("e1", "input", "ルームを検索...", "type", input_type="search"),
        Element("e2", "item", "test", "click"),
    ))
    session = FakeSession(view)
    decider = ScriptedDecider([_decision("type:e1"), _decision("click:e2"), _decision("done", done=0.9)])
    runner = BrowserAgentRunner(
        task="ルームのメッセージにtestと入れて送信",
        max_steps=4,
        headless=True,
        decider=decider,  # type: ignore[arg-type]
        writer=FakeWriter(phrase=""),  # type: ignore[arg-type]
        session_factory=lambda: session,
    )
    events = _events(runner)

    assert session.actions == [("click", "e2")]
    assert "入力しなかった" in decider.histories[1][0]
    step = next(event for event in events if event["event"] == "step")
    assert "入力しない" in step["data"]["action"]["text"]


def test_back_is_not_offered_on_the_first_page() -> None:
    """開いた直後のページで「戻る」を選ぶと about:blank に出てしまうため、候補に出さない。"""

    class Recording(ScriptedDecider):
        def __init__(self, decisions) -> None:
            super().__init__(decisions)
            self.offered: list[set[str]] = []

        async def decide(self, task, view, options, history=(), **_):
            self.offered.append({option.id for option in options})
            return await super().decide(task, view, options, history)

    class Moving(FakeSession):
        async def click(self, element_id: str) -> None:
            await super().click(element_id)
            self.view = PageView("https://www.google.com/next", "次", "", self.view.elements)

    session = Moving(VIEW)
    decider = Recording([_decision("click:e2"), _decision("done", done=0.9)])
    runner = BrowserAgentRunner(
        task="東京の天気を調べて",
        max_steps=3,
        decider=decider,  # type: ignore[arg-type]
        writer=FakeWriter(),  # type: ignore[arg-type]
        session_factory=lambda: session,
    )
    _events(runner)

    assert "back" not in decider.offered[0]
    assert "back" in decider.offered[1]


def test_send_request_does_not_click_the_search_fields_button() -> None:
    """送信の依頼はメッセージなどの欄に向ける。検索欄は入力時の Enter で検索済みなので、横のボタンは押さない。"""
    url = "https://example.com/chat"
    view = PageView(url, "チャット", "", (
        Element("e1", "input", "ルームを検索...", "type", input_type="search", filled=True, submit_id="e2"),
        Element("e2", "button", "検索語をクリア", "click"),
    ))
    runner = BrowserAgentRunner(task="test2 を開いて hello と送信して")
    runner._typed_fields.add((url, "", "ルームを検索..."))

    assert runner._submit_after_input(view) is None
    assert runner._submit_targets(view) == frozenset()


def test_unsure_done_before_sending_is_not_reported_as_complete() -> None:
    """送信を頼まれて何も送っていないのに、確信の低い done で終わると、失敗が成功に見えてしまう。"""
    session = FakeSession(VIEW)
    runner = BrowserAgentRunner(
        task="helloと送信して",
        max_steps=4,
        headless=True,
        decider=ScriptedDecider([_decision("done", done=0.1)]),  # type: ignore[arg-type]
        writer=FakeWriter(),  # type: ignore[arg-type]
        session_factory=lambda: session,
    )
    events = _events(runner)

    assert "送信まで進めなかった" in events[-1]["data"]["result"]

    # 送信の依頼が無ければ、従来どおり Jev の done で終える。
    plain = FakeSession(VIEW)
    done_events = _events(_runner(plain, [_decision("done", done=0.1)]))
    assert done_events[-1]["data"]["result"] == "晴れ、25度"


def test_gemini_question_waits_for_finished_reply(monkeypatch) -> None:
    monkeypatch.setattr("app.services.browser_agent.REPLY_STABLE_SECONDS", 0)

    class GeminiPage(FakeSession):
        async def fill(self, element_id: str, text: str, submit: bool) -> None:
            await super().fill(element_id, text, submit)
            self.view = PageView(self.view.url, self.view.title, "質問", (
                Element("e1", "textarea", "質問", "type", filled=True, submit_id="e2"),
                Element("e2", "button", "送信", "click"),
            ), reply="前の回答")

        async def click(self, element_id: str) -> None:
            await super().click(element_id)
            self.view = PageView(self.view.url, self.view.title, "質問", (), reply="前の回答")

        async def wait(self) -> None:
            await super().wait()
            waits = sum(action[0] == "wait" for action in self.actions)
            if waits == 2:
                self.view = PageView(self.view.url, self.view.title, "質問", (), reply="新しい答えの途中", reply_busy=True)
            elif waits >= 3:
                self.view = PageView(self.view.url, self.view.title, "質問と回答", (), reply="新しい答えの全文")

    class RecordingWriter(FakeWriter):
        async def summary(self, client: object, task: str, url: str, title: str, excerpt: str) -> str:
            assert excerpt == "新しい答えの全文"
            return excerpt

    session = GeminiPage(PageView("https://gemini.google.com/app", "Gemini", "", (
        Element("e1", "textarea", "質問", "type", submit_id="e2"),
        Element("e2", "button", "送信", "click", disabled=True),
    ), reply="前の回答"))
    runner = BrowserAgentRunner(
        task="https://gemini.google.com/app で質問をして返答を教えて",
        max_steps=5,
        headless=True,
        decider=ScriptedDecider([_decision("type:e1", text="質問"), _decision("done")]),  # type: ignore[arg-type]
        writer=RecordingWriter(),  # type: ignore[arg-type]
        session_factory=lambda: session,
    )

    events = _approved_events(runner)

    assert runner._expects_reply
    assert session.actions.count(("click", "e2")) == 1
    assert sum(action[0] == "wait" for action in session.actions) >= 3
    assert events[-1]["data"]["result"] == "新しい答えの全文"


def _form_view(name: bool = False, email: bool = False, body: bool = False) -> PageView:
    return PageView("https://example.com/contact", "お問い合わせ", "", (
        Element("e1", "input", "お名前", "type", filled=name, form_id="f1", submit_id="e4"),
        Element("e2", "input", "メールアドレス", "type", "email", filled=email, form_id="f1", submit_id="e4"),
        Element("e3", "textarea", "内容", "type", filled=body, form_id="f1", submit_id="e4"),
        Element("e4", "button", "確認画面へ", "click", input_type="submit", form_id="f1"),
    ))


def test_form_is_not_submitted_until_all_its_fields_are_filled() -> None:
    url = "https://example.com/contact"
    runner = BrowserAgentRunner(task="山田で問い合わせを送信して")
    runner._typed_fields.add((url, "f1", "お名前"))

    assert runner._submit_after_input(_form_view(name=True)) is None
    submit = runner._submit_after_input(_form_view(name=True, email=True, body=True))
    assert submit is not None and submit[0].action_id == "click:e4"


def test_form_with_a_confirm_page_goes_on_to_the_final_send() -> None:
    """確認画面へ進んだら終わらずに続け、確認画面の「送信する」を押して完了を確かめる。"""

    class ContactSite(FakeSession):
        async def fill(self, element_id: str, text: str, submit: bool) -> None:
            await super().fill(element_id, text, submit)
            filled = {action[1] for action in self.actions if action[0] == "fill"}
            self.view = _form_view("e1" in filled, "e2" in filled, "e3" in filled)

        async def click(self, element_id: str) -> None:
            await super().click(element_id)
            if element_id == "e4":
                self.view = PageView("https://example.com/contact", "確認", "内容の確認", (
                    Element("c1", "button", "修正する", "click"),
                    Element("c2", "button", "送信する", "click"),
                ))
            elif element_id == "c2":
                self.view = PageView(
                    "https://example.com/contact", "完了", "送信しました", (), feedback="お問い合わせを受け付けました"
                )

    session = ContactSite(_form_view())
    decider = ScriptedDecider([
        _decision("type:e1"), _decision("type:e2"), _decision("type:e3"),
        _decision("click:c2", risk=0.9), _decision("done", done=1.0),
    ])
    runner = BrowserAgentRunner(
        task="https://example.com/contact で山田として問い合わせを送信して",
        max_steps=8,
        headless=True,
        decider=decider,  # type: ignore[arg-type]
        writer=FakeWriter(phrase="山田", summary="お問い合わせを受け付けました"),  # type: ignore[arg-type]
        session_factory=lambda: session,
    )

    events = _approved_events(runner)

    clicks = [action for action in session.actions if action[0] == "click"]
    assert clicks == [("click", "e4"), ("click", "c2")]
    assert events[-1]["event"] == "complete"
    assert events[-1]["data"]["result"] == "お問い合わせを受け付けました"


def test_send_key_follows_the_fields_hint() -> None:
    """欄の案内に「Ctrl+Enter で送信 / Enter で改行」とあれば、Enter ではなく Ctrl+Enter で送る。"""
    url = "https://example.com/chat"
    runner = BrowserAgentRunner(task="testと入れて送信して")

    ctrl = PageView(url, "会話", "", (
        Element("e1", "textarea", "メッセージを入力… (Ctrl+Enter で送信 / Enter で改行)", "type", filled=True),
    ))
    runner._typed_fields.add((url, "", ctrl.elements[0].name))
    submit = runner._submit_after_input(ctrl)
    assert submit is not None and submit[0].action_id == "key:e1"

    # Enter が改行で、送信のキーが書かれていない欄は、キーでは送らない（送信ボタンを探させる）。
    newline = PageView(url, "会話", "", (
        Element("e1", "textarea", "メッセージ（Enter で改行）", "type", filled=True),
    ))
    runner._typed_fields.add((url, "", newline.elements[0].name))
    assert runner._submit_after_input(newline) is None


def test_ctrl_enter_is_pressed_for_a_ctrl_enter_field() -> None:
    class CtrlChat(FakeSession):
        async def fill(self, element_id: str, text: str, submit: bool) -> None:
            await super().fill(element_id, text, submit)
            self.view = PageView(self.view.url, self.view.title, "", (Element("e1", "textarea", NAME, "type", filled=True),))

        async def press_key(self, element_id: str, key: str) -> None:
            await super().press_key(element_id, key)
            self.view = PageView(self.view.url, self.view.title, "test", (Element("e1", "textarea", NAME, "type"),))

    NAME = "メッセージを入力… (Ctrl+Enter で送信 / Enter で改行)"
    session = CtrlChat(PageView("https://example.com/room/1", "test", "", (Element("e1", "textarea", NAME, "type"),)))
    runner = BrowserAgentRunner(
        task="https://example.com/room/1 でtestと送信して",
        max_steps=4,
        headless=True,
        decider=ScriptedDecider([_decision("type:e1")]),  # type: ignore[arg-type]
        writer=FakeWriter(phrase="test"),  # type: ignore[arg-type]
        session_factory=lambda: session,
    )
    events = _approved_events(runner)

    assert ("key", "e1", "ControlOrMeta+Enter") in session.actions
    confirm = next(event for event in events if event["event"] == "confirm_request")
    assert "Ctrl+Enter" in confirm["data"]["action"]["description"]
    assert "入力欄がクリア" in events[-1]["data"]["result"]


def test_select_action_picks_the_chosen_option() -> None:
    view = PageView(
        "https://shop.example/s", "結果", "",
        (Element("e1", "combobox", "並べ替え", "select", choices=(SelectChoice(0, "おすすめ順", "default"), SelectChoice(1, "価格の安い順", "cheap"))),),
    )
    session = FakeSession(view)
    _events(_runner(session, [_decision("select:e1#1"), _decision("done", done=0.9)]))

    assert session.actions[0] == ("select", "e1", 1, "価格の安い順", "cheap")
