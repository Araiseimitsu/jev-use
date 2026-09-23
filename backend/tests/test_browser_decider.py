"""Gemini が選んだ操作に、Jev の危険度を合わせて Decision にする処理のテスト。"""

import asyncio

from app.services.jev.browser_decider import BrowserDecider, build_risk_questions, risk_state
from app.services.page_state import ActionOption, Element, PageView, action_catalog
from app.services.planner import Plan
from tests.fakes import disabled_asker, fake_asker, jev_response

OPTIONS = action_catalog((Element("e1", "button", "発注", "click"), Element("e2", "textarea", "メモ", "type")))
VIEW = PageView("https://shop.example", "在庫", "製品C 在庫 0", ())


class FakePlanner:
    """決まった Plan を返し、受け取った履歴を記録する。"""

    enabled = True

    def __init__(self, plan: Plan | None) -> None:
        self.result = plan
        self.histories: list[tuple[str, ...]] = []

    async def plan(self, client, task, view, options, history=()) -> Plan | None:
        self.histories.append(history)
        return self.result


def _plan(action_id: str = "click:e1", text: str = "") -> Plan:
    return Plan(action_id=action_id, text=text, reason="在庫が 0 なので発注する", model="gemini-test", latency_ms=5)


def _decide(planner: FakePlanner, asker, history: tuple[str, ...] = ()):
    decider = BrowserDecider(asker=asker, planner=planner)  # type: ignore[arg-type]
    return asyncio.run(decider.decide("在庫を補充して", VIEW, OPTIONS, history))


def test_risk_question_is_only_about_the_chosen_action() -> None:
    questions = build_risk_questions()
    state = risk_state("在庫を補充して", VIEW, "「発注」（button）をクリックする", "")

    assert set(questions) == {"risk"}
    assert state["action"] == "「発注」（button）をクリックする"
    assert "image" not in state


def test_decision_takes_the_plan_and_jevs_risk() -> None:
    asker, calls = fake_asker(jev_response(nouls={"risk": 0.8}))
    planner = FakePlanner(_plan())

    decision = _decide(planner, asker, ("前の操作",))

    assert decision.action_id == "click:e1"
    assert decision.reason == "在庫が 0 なので発注する"
    assert decision.risk_probability == 0.8
    assert decision.text is None
    assert planner.histories == [("前の操作",)]
    assert calls[0]["state"]["action"] == "「発注」（button）をクリックする"


def test_typed_text_comes_from_the_plan_and_is_shown_to_jev() -> None:
    asker, calls = fake_asker(jev_response(nouls={"risk": 0.1}))

    decision = _decide(FakePlanner(_plan("type:e2", "10 個")), asker)

    assert decision.text == "10 個"
    assert calls[0]["state"]["text"] == "10 個"


def test_safe_controls_do_not_ask_jev() -> None:
    asker, calls = fake_asker(jev_response(nouls={"risk": 0.9}))

    decision = _decide(FakePlanner(_plan("done")), asker)

    assert decision.action_id == "done"
    assert decision.done_probability == 1.0
    assert decision.risk_probability == 0.0
    assert calls == []


def test_failed_plan_falls_back_without_acting() -> None:
    asker, _ = fake_asker(jev_response(nouls={"risk": 0.1}))

    decision = _decide(FakePlanner(None), asker)

    assert decision.used_fallback is True
    assert decision.action_id == "wait"


def test_unavailable_jev_asks_for_approval() -> None:
    """危険度を判定できないときは、取り消しにくい操作とみなして承認を求める。"""
    decision = _decide(FakePlanner(_plan()), disabled_asker())

    assert decision.risk_probability == 1.0
    assert decision.used_fallback is False
