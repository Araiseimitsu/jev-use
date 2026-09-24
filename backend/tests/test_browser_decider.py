"""Gemini が選んだ操作に、Jev の危険度を合わせて Decision にする処理のテスト。"""

import asyncio

import pytest

from app.services.jev.browser_decider import BrowserDecider, build_risk_questions, risk_state
from app.services.page_state import ActionOption, Element, PageView, action_catalog
from app.services.planner import Plan
from tests.fakes import disabled_asker, fake_asker, jev_response

ELEMENTS = (
    Element("e1", "button", "発注", "click", form_id="f1"),
    Element("e2", "textarea", "メモ", "type", form_id="f1", filled=True, submit_id="e1"),
    Element("e3", "button", "戻る", "click"),
)
OPTIONS = action_catalog(ELEMENTS)
VIEW = PageView("https://shop.example", "在庫", "製品C 在庫 0", ELEMENTS)
RISK_VALUES = {"send": 0.1, "financial": 0.8, "change": 0.2, "access": 0.1}


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
    state = risk_state("在庫を補充して", VIEW, "click:e1", "「発注」（button）をクリックする", "")

    assert set(questions) == {"send", "financial", "change", "access"}
    assert state["action"]["description"] == "「発注」（button）をクリックする"
    assert state["related_fields"] == [{"name": "メモ", "kind": "type", "filled": True}]
    assert state["nearby_elements"] == ["メモ", "戻る"]
    assert "製品C" not in str(state)
    assert "image" not in state


def test_risk_state_uses_the_select_element_for_a_numbered_choice() -> None:
    view = PageView(
        "https://shop.example", "商品", "", (Element("e7", "combobox", "都道府県", "select"),)
    )

    state = risk_state("東京を選ぶ", view, "select:e7#13", "東京を選ぶ", "")

    assert state["element"]["name"] == "都道府県"


def test_decision_takes_the_plan_and_jevs_risk() -> None:
    asker, calls = fake_asker(jev_response(nouls=RISK_VALUES))
    planner = FakePlanner(_plan())

    decision = _decide(planner, asker, ("前の操作",))

    assert decision.action_id == "click:e1"
    assert decision.reason == "在庫が 0 なので発注する"
    assert decision.risk_probability == 0.8
    assert decision.risk_factors == RISK_VALUES
    assert decision.text is None
    assert planner.histories == [("前の操作",)]
    assert calls[0]["state"]["action"]["description"] == "「発注」（button）をクリックする"


@pytest.mark.parametrize("risk_type", ("send", "financial", "change", "access"))
def test_each_risk_type_can_require_approval(risk_type: str) -> None:
    values = {key: 0.1 for key in RISK_VALUES}
    values[risk_type] = 0.9
    asker, _ = fake_asker(jev_response(nouls=values))

    decision = _decide(FakePlanner(_plan()), asker)

    assert decision.risk_probability == 0.9
    assert decision.risk_factors == values


def test_typed_text_comes_from_the_plan_and_is_shown_to_jev() -> None:
    asker, calls = fake_asker(jev_response(nouls=RISK_VALUES))

    decision = _decide(FakePlanner(_plan("type:e2", "10 個")), asker)

    assert decision.text == "10 個"
    assert calls[0]["state"]["action"]["text"] == "10 個"


def test_safe_controls_do_not_ask_jev() -> None:
    asker, calls = fake_asker(jev_response(nouls=RISK_VALUES))

    decision = _decide(FakePlanner(_plan("done")), asker)

    assert decision.action_id == "done"
    assert decision.done_probability == 1.0
    assert decision.risk_probability == 0.0
    assert calls == []


def test_failed_plan_falls_back_without_acting() -> None:
    asker, _ = fake_asker(jev_response(nouls=RISK_VALUES))

    decision = _decide(FakePlanner(None), asker)

    assert decision.used_fallback is True
    assert decision.action_id == "wait"


def test_unavailable_jev_asks_for_approval() -> None:
    """危険度を判定できないときは、取り消しにくい操作とみなして承認を求める。"""
    decision = _decide(FakePlanner(_plan()), disabled_asker())

    assert decision.risk_probability == 1.0
    assert decision.used_fallback is False


def test_missing_risk_answer_asks_for_approval() -> None:
    asker, _ = fake_asker(jev_response(nouls={"send": 0.1}))

    decision = _decide(FakePlanner(_plan()), asker)

    assert decision.risk_probability == 1.0
    assert decision.risk_factors == {}
