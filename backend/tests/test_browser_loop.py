"""ブラウザ操作ループのテスト。実ブラウザは起動しない。"""

import asyncio

from app.core.config import settings
from app.services.browser_agent import BrowserAgentRunner
from app.services.jev.browser_decider import Decision
from app.services.page_state import Element, PageView


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

    async def scroll(self) -> None:
        self.actions.append(("scroll",))

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


def test_login_fields_receive_credentials_not_the_request() -> None:
    import json

    login = PageView(
        url="https://example.com/login",
        title="ログイン",
        excerpt="ログイン",
        elements=(
            Element("e1", "textbox", "ユーザー名", "type", "text"),
            Element("e2", "textbox", "パスワード", "type", "password"),
        ),
    )
    session = FakeSession(login)
    writer = FakeWriter()
    decider = ScriptedDecider([_decision("type:e1"), _decision("type:e2"), _decision("done", done=0.9)])
    runner = BrowserAgentRunner(
        task=(
            "https://example.com/login\n"
            "ユーザー名: from-task\n"
            "パスワード: pasted-secret\n"
            "- スケジュールが面で今日のすべての予定を教えて"
        ),
        username="alice",
        password="field-secret",
        max_steps=4,
        decider=decider,  # type: ignore[arg-type]
        writer=writer,  # type: ignore[arg-type]
        session_factory=lambda: session,
    )

    events = _events(runner)
    dumped = json.dumps(events, ensure_ascii=False)

    assert session.actions == [("fill", "e1", "alice", False), ("fill", "e2", "field-secret", False)]
    assert writer.phrase_tasks == []
    assert "field-secret" not in dumped
    assert "pasted-secret" not in dumped
    assert all("field-secret" not in task and "pasted-secret" not in task for task in decider.tasks)


def test_other_fields_get_the_request_line() -> None:
    session = FakeSession(VIEW)
    writer = FakeWriter()
    runner = BrowserAgentRunner(
        task="https://example.com\nパスワード: pasted-secret\n- 今日の予定を教えて",
        password="pasted-secret",
        max_steps=3,
        decider=ScriptedDecider([_decision("type:e1"), _decision("done", done=0.9)]),  # type: ignore[arg-type]
        writer=writer,  # type: ignore[arg-type]
        session_factory=lambda: session,
    )

    _events(runner)

    assert writer.phrase_tasks == ["今日の予定を教えて"]
    assert session.actions[0] == ("fill", "e1", "東京 天気", True)


def test_missing_password_stops_without_typing() -> None:
    login = PageView(
        url="https://example.com/login",
        title="ログイン",
        excerpt="",
        elements=(Element("e1", "textbox", "パスワード", "type", "password"),),
    )
    session = FakeSession(login)
    events = _events(_runner(session, [_decision("type:e1")]))

    assert session.actions == []
    assert events[-1]["data"]["result"] == "パスワードが指定されていません。画面の入力欄に入れてから実行してください。"


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
        _runner(session, [_decision("wait", used_fallback=True, message="選べませんでした。")])
    )

    assert session.actions == []
    assert events[-1]["data"]["result"] == "選べませんでした。"


def test_closed_browser_is_explained_in_japanese() -> None:
    from app.services.browser_agent import _public_error

    message = _public_error(RuntimeError("Target page, context or browser has been closed"))

    assert message == "ブラウザが閉じられたため停止しました。"


def test_same_action_three_times_stops() -> None:
    session = FakeSession(VIEW)
    click = _decision("click:e2")
    events = _events(_runner(session, [click, click, click, click]))

    assert session.actions == [("click", "e2"), ("click", "e2")]
    assert "繰り返して" in events[-1]["data"]["result"]
