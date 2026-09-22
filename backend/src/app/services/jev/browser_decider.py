"""次の 1 操作を Jev に選ばせる。

Jev は候補の id だけを返す。座標も、入力する文字列も、ここでは作らない。
"""

from dataclasses import asdict, dataclass, field
from typing import Any

from app.services.jev.asker import JevAsker, ask_or_fallback, model_of, probabilities_of
from app.services.page_state import ActionOption, PageView, jev_excerpt


@dataclass(frozen=True)
class Decision:
    """1 ステップの判断。"""

    action_id: str
    confidence: float
    probabilities: dict[str, float] = field(default_factory=dict)
    risk_probability: float = 0.0
    done_probability: float = 0.0
    model: str = ""
    latency_ms: int = 0
    used_fallback: bool = False
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_decision_questions(options: tuple[ActionOption, ...] | list[ActionOption]) -> dict[str, Any]:
    """次の操作・危険度・完了を 1 リクエストで問う。"""
    from typesafe_sdk import Choice, Noul

    return {
        "action": Choice(
            instructions=(
                "指示を達成するための次の 1 操作を、候補から 1 つだけ選べ。"
                "ページに答えが既にあるなら done を選べ。"
            ),
            criteria={option.id: option.description for option in options},
        ),
        "risk": Noul(
            instructions="選ぼうとしている操作は、送信・購入・削除・ログイン・契約など、取り消しにくいものか。",
            criteria={
                "true": "取り消しにくい、または外部に影響する",
                "false": "閲覧、スクロール、検索語の入力など、取り消せる",
            },
        ),
        "done": Noul(
            instructions="このページの内容だけで、指示が求める答えや完了状態はすでに揃っているか。",
            criteria={
                "true": "指示への答えがページ上に読み取れる",
                "false": "まだ検索や遷移が必要で、答えは揃っていない",
            },
        ),
    }


def decision_state(task: str, view: PageView, options: list[ActionOption] | tuple[ActionOption, ...]) -> dict[str, Any]:
    """Jev に渡す state。画像も座標も含めない。"""
    return {
        "task": task,
        "url": view.url,
        "title": view.title,
        "page_text": jev_excerpt(view),
        "actions": [{"id": option.id, "description": option.description} for option in options],
    }


def interpret_decision(
    response: Any, latency_ms: int, option_ids: set[str]
) -> Decision:
    """Jev の回答を Decision にする。未知の操作 id は解釈失敗とする。"""
    chosen = response.choices["action"]
    action_id = str(chosen.choice)
    if action_id not in option_ids:
        raise KeyError(f"未知の操作: {action_id}")
    return Decision(
        action_id=action_id,
        confidence=float(chosen.confidence),
        probabilities=probabilities_of(chosen),
        risk_probability=float(response.nouls["risk"].noul),
        done_probability=float(response.nouls["done"].noul),
        model=model_of(response),
        latency_ms=latency_ms,
    )


def fallback_decision(message: str) -> Decision:
    """判断できないときは操作しない。"""
    return Decision(action_id="wait", confidence=0.0, used_fallback=True, message=message)


class BrowserDecider:
    """ページ状態と操作候補から、次の 1 手を選ぶ。"""

    def __init__(self, asker: JevAsker | None = None) -> None:
        self.asker = asker or JevAsker()

    @property
    def enabled(self) -> bool:
        return self.asker.enabled

    async def decide(self, task: str, view: PageView, options: tuple[ActionOption, ...]) -> Decision:
        """候補のどれを実行するかを問う。失敗時は操作しない結論を返す。"""
        option_ids = {option.id for option in options}

        def interpret(response: Any, latency_ms: int) -> Decision:
            return interpret_decision(response, latency_ms, option_ids)

        return await ask_or_fallback(
            self.asker,
            state=decision_state(task, view, options),
            questions=build_decision_questions(options),
            interpret=interpret,
            fallback=fallback_decision,
            subject="操作の選択",
        )
