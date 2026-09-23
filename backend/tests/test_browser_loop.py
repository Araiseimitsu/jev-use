"""ブラウザ操作ループのテスト。実ブラウザは起動しない。"""

import asyncio

import pytest

from app.core.config import settings
from app.services.browser_agent import BrowserAgentRunner
from app.services.jev.browser_decider import Decision
from app.services.page_state import Element, PageView, rank_elements


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

    async def decide(self, task: str, view: PageView, options: tuple) -> Decision:
        self.tasks.append(task)
        decision = self.decisions[min(self.calls, len(self.decisions) - 1)]
        self.calls += 1
        return decision


class FakeWriter:
    def __init__(self, phrase: str = "東京 天気", summary: str = "晴れ、25度") -> None:
        self.phrase_text = phrase
        self.summary_text = summary
        self.phrase_tasks: list[str] = []

    async def phrase(self, client: object, task: str, field_name: str, page_title: str) -> str:
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

        async def decide(self, task: str, view: PageView, options: tuple) -> Decision:
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

    events = _events(runner)

    assert session.actions.count(("click", "e2")) == 1
    assert session.reads >= 2
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
