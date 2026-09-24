"""次の 1 操作を決める。Gemini が手順を考えて操作を選び、Jev がその操作の危険度を判定する。

Jev は分類器で手順を考えられないため、操作の選択は planner（Gemini）に任せる。
Jev には「選ばれた操作が取り消しにくいか」だけを問い、承認の要否に使う。座標も画像も渡さない。
"""

from dataclasses import asdict, dataclass, field
from typing import Any

import httpx

from app.services.jev.asker import JevAsker, MISSING_KEY_MESSAGE, ask_or_fallback, model_of
from app.services.page_state import ActionOption, PageView
from app.services.planner import MISSING_GEMINI_MESSAGE, GeminiPlanner

# 確認なしで実行してよく、Jev に危険度を問わない操作
SAFE_ACTIONS = frozenset({"back", "wait", "done"})


@dataclass(frozen=True)
class Decision:
    """1 ステップの判断。"""

    action_id: str
    confidence: float = 0.0
    probabilities: dict[str, float] = field(default_factory=dict)
    risk_probability: float = 0.0
    done_probability: float = 0.0
    model: str = ""
    latency_ms: int = 0
    used_fallback: bool = False
    message: str = ""
    # type 操作で入れる文字列。None なら実行側が依頼から作る。空なら入力しない。
    text: str | None = None
    # その操作を選んだ理由（Gemini の説明）
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_risk_questions() -> dict[str, Any]:
    """選ばれた操作の危険度だけを問う。"""
    from typesafe_sdk import Noul

    return {
        "risk": Noul(
            instructions="action の操作は、送信・購入・削除・ログイン・契約など、取り消しにくいものか。",
            criteria={
                "true": "取り消しにくい、または外部に影響する",
                "false": "閲覧、ページの移動、検索語の入力など、取り消せる",
            },
        ),
    }


def risk_state(task: str, view: PageView, description: str, text: str) -> dict[str, Any]:
    """Jev に渡す state。画像も座標も含めない。"""
    return {"task": task, "url": view.url, "title": view.title, "action": description, "text": text}


def fallback_decision(message: str) -> Decision:
    """操作を選べないときは操作しない。"""
    return Decision(action_id="wait", used_fallback=True, message=message)


class BrowserDecider:
    """ページ状態と操作候補から、次の 1 手と危険度を決める。"""

    def __init__(self, asker: JevAsker | None = None, planner: GeminiPlanner | None = None) -> None:
        self.asker = asker or JevAsker()
        self.planner = planner or GeminiPlanner()

    @property
    def enabled(self) -> bool:
        return self.planner.enabled and self.asker.enabled

    @property
    def missing_message(self) -> str:
        """実行に必要なキーが欠けているときの案内。"""
        return MISSING_GEMINI_MESSAGE if not self.planner.enabled else MISSING_KEY_MESSAGE

    async def decide(
        self,
        task: str,
        view: PageView,
        options: tuple[ActionOption, ...],
        history: tuple[str, ...] = (),
        client: httpx.AsyncClient | None = None,
    ) -> Decision:
        """次の操作を選び、危険度を付ける。選べなければ操作しない結論を返す。"""
        if client is None:
            async with httpx.AsyncClient(timeout=httpx.Timeout(60)) as own:
                return await self.decide(task, view, options, history, own)

        plan = await self.planner.plan(client, task, view, options, history)
        if plan is None:
            return fallback_decision("Gemini が次の操作を選べませんでした。")
        text = plan.text if plan.action_id.startswith(("type:", "select:")) else None
        if plan.action_id in SAFE_ACTIONS:
            return Decision(
                action_id=plan.action_id,
                done_probability=1.0 if plan.action_id == "done" else 0.0,
                model=plan.model,
                latency_ms=plan.latency_ms,
                reason=plan.reason,
            )

        description = next((option.description for option in options if option.id == plan.action_id), plan.action_id)

        def interpret(response: Any, latency_ms: int) -> tuple[float, str, int]:
            return float(response.nouls["risk"].noul), model_of(response), latency_ms

        def unknown_risk(message: str) -> tuple[float, str, int]:
            # 危険度が分からない操作は、取り消しにくいものとみなして承認を求める。
            return 1.0, "", 0

        risk, jev_model, jev_latency = await ask_or_fallback(
            self.asker,
            state=risk_state(task, view, description, plan.text),
            questions=build_risk_questions(),
            interpret=interpret,
            fallback=unknown_risk,
            subject="危険度の判定",
        )
        return Decision(
            action_id=plan.action_id,
            risk_probability=risk,
            model=f"{plan.model} + {jev_model}" if jev_model else plan.model,
            latency_ms=plan.latency_ms + jev_latency,
            text=text,
            reason=plan.reason,
        )
