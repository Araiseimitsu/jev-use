"""Jev が操作候補から次の一手を選ぶ処理のテスト。"""

import asyncio

from app.services.jev.browser_decider import (
    BrowserDecider,
    build_decision_questions,
    decision_state,
    interpret_decision,
)
from app.services.page_state import ActionOption, Element, PageView, action_catalog
from tests.fakes import choice, disabled_asker, fake_asker, jev_response

OPTIONS = action_catalog((Element("e1", "button", "発注", "click"),))
VIEW = PageView("https://shop.example", "在庫", "製品C 在庫 0", ())


def _response(action_id: str = "click:e1", risk: float = 0.1, done: float = 0.2):
    return jev_response(
        choices={"action": choice(action_id, 0.91, tuple(option.id for option in OPTIONS))},
        nouls={"risk": risk, "done": done},
    )


def test_questions_offer_only_catalog_ids() -> None:
    questions = build_decision_questions(OPTIONS)

    assert set(questions["action"].criteria) == {option.id for option in OPTIONS}
    assert set(questions) == {"action", "risk", "done"}


def test_state_is_text_only() -> None:
    state = decision_state("在庫を補充して", VIEW, OPTIONS)

    assert "image" not in state
    assert "x" not in state
    assert state["actions"][0]["id"] == "click:e1"
    assert state["page_text"] == "製品C 在庫 0"


def test_decide_returns_the_chosen_action() -> None:
    asker, calls = fake_asker(_response())

    decision = asyncio.run(BrowserDecider(asker).decide("在庫を補充して", VIEW, OPTIONS))

    assert decision.action_id == "click:e1"
    assert decision.confidence == 0.91
    assert decision.risk_probability == 0.1
    assert decision.done_probability == 0.2
    assert calls[0]["state"]["url"] == "https://shop.example"


def test_unknown_action_falls_back_without_clicking() -> None:
    asker, _ = fake_asker(_response("click:missing"))

    decision = asyncio.run(BrowserDecider(asker).decide("在庫を補充して", VIEW, OPTIONS))

    assert decision.used_fallback is True
    assert decision.action_id == "wait"


def test_missing_key_does_not_choose_an_action() -> None:
    decision = asyncio.run(BrowserDecider(disabled_asker()).decide("在庫を補充して", VIEW, OPTIONS))

    assert decision.used_fallback is True
    assert "未設定" in decision.message


def test_interpret_rejects_unknown_id() -> None:
    try:
        interpret_decision(_response("nope"), 10, {"click:e1"})
    except KeyError as e:
        assert "nope" in str(e)
    else:
        raise AssertionError("未知の id は拒否する")
