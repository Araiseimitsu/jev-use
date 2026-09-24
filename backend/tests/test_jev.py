"""開始前の見積り（TaskAssessor）のテスト。"""

import asyncio

import pytest

from app.core.config import settings
from app.services.jev.task_assessor import (
    MAX_STEPS_LIMIT,
    STEP_SCALE,
    TaskAssessor,
    build_task_questions,
    clamp_max_steps,
)
from tests.fakes import disabled_asker, fake_asker, jev_response


def _assessment_response(risk=0.1, clarity=0.9, feasibility=0.9, steps=1.0):
    return jev_response(
        nouls={"risk": risk, "clarity": clarity, "feasibility": feasibility},
        scores={"steps": steps},
    )


def test_task_questions_cover_risk_clarity_feasibility_and_steps() -> None:
    questions = build_task_questions()

    assert set(questions) == {"risk", "clarity", "feasibility", "steps"}
    assert list(questions["steps"].criteria) == list(STEP_SCALE)


def test_assess_returns_suggestion_without_confirmation_for_safe_task() -> None:
    asker, calls = fake_asker(_assessment_response(steps=1.0))

    result = asyncio.run(TaskAssessor(asker).assess("天気を調べる"))

    assert result.requires_confirmation is False
    assert result.reasons == []
    assert result.suggested_max_steps == 8
    assert result.model == "jev-1.13.0"
    assert calls[0]["state"] == "天気を調べる"


@pytest.mark.parametrize(
    ("kwargs", "keyword"),
    [
        ({"risk": 0.95}, "取り消しにくい"),
        ({"clarity": 0.1}, "具体的"),
        ({"feasibility": 0.1}, "ブラウザ操作だけ"),
    ],
)
def test_assess_requires_confirmation_with_reason(monkeypatch, kwargs, keyword) -> None:
    monkeypatch.setattr(settings, "typesafe_risk_threshold", 0.7)
    asker, _ = fake_asker(_assessment_response(**kwargs))

    result = asyncio.run(TaskAssessor(asker).assess("何かする"))

    assert result.requires_confirmation is True
    assert any(keyword in reason for reason in result.reasons)


def test_assess_clamps_out_of_range_step_score() -> None:
    asker, _ = fake_asker(_assessment_response(steps=9.0))

    result = asyncio.run(TaskAssessor(asker).assess("大量の作業"))

    assert result.suggested_max_steps == 25


def test_assess_uses_upper_part_of_step_distribution() -> None:
    response = jev_response(
        nouls={"risk": 0.1, "clarity": 0.9, "feasibility": 0.9},
        scores={"steps": 1.5},
        score_probabilities={"steps": {0: 0.5, 3: 0.5}},
    )
    asker, _ = fake_asker(response)

    result = asyncio.run(TaskAssessor(asker).assess("複数ページを調べる"))

    assert result.suggested_max_steps == 25


def test_assess_falls_back_on_invalid_step_distribution() -> None:
    response = jev_response(
        nouls={"risk": 0.1, "clarity": 0.9, "feasibility": 0.9},
        scores={"steps": 1.0},
        score_probabilities={"steps": {0: 0.4, 1: 0.4}},
    )
    asker, _ = fake_asker(response)

    result = asyncio.run(TaskAssessor(asker).assess("調べる"))

    assert result.used_fallback is True
    assert result.suggested_max_steps == settings.default_max_steps


def test_clamp_max_steps_limits_range() -> None:
    assert clamp_max_steps(0) == 1
    assert clamp_max_steps(MAX_STEPS_LIMIT + 10) == MAX_STEPS_LIMIT


def test_assess_falls_back_without_key() -> None:
    result = asyncio.run(TaskAssessor(disabled_asker()).assess("天気を調べる"))

    assert result.used_fallback is True
    assert result.requires_confirmation is False
    assert result.suggested_max_steps == settings.default_max_steps
    assert "未設定" in result.message


def test_assess_falls_back_on_api_error() -> None:
    asker, _ = fake_asker(error=RuntimeError("boom"))

    result = asyncio.run(TaskAssessor(asker).assess("天気を調べる"))

    assert result.used_fallback is True
    assert "RuntimeError" in result.message


def test_assess_falls_back_on_malformed_response() -> None:
    asker, _ = fake_asker(jev_response(nouls={"risk": 0.1}))

    result = asyncio.run(TaskAssessor(asker).assess("天気を調べる"))

    assert result.used_fallback is True
    assert "解釈" in result.message
