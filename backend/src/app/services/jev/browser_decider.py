"""次の 1 操作を決める。Gemini が手順を考えて操作を選び、Jev が操作の影響を判定する。

Jev は分類器で手順を考えられないため、操作の選択は planner（Gemini）に任せる。
Jev には選ばれた操作の影響を種類別に問い、承認の要否に使う。座標も画像も渡さない。
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
    risk_factors: dict[str, float] = field(default_factory=dict)
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
    """選ばれた操作の独立した影響を一度に問う。"""
    from typesafe_sdk import Noul

    return {
        "send": Noul(
            instructions="`action` を実行すると、メッセージ・投稿・フォーム内容などが相手やサイトへ送信・公開されるか。",
            criteria={
                "true": "送信または公開を確定する操作",
                "false": "閲覧、検索、下書きへの入力など、送信を確定しない操作",
            },
        ),
        "financial": Noul(
            instructions="`action` を実行すると、購入・支払い・契約が成立または確定するか。",
            criteria={
                "true": "金銭の支払いや契約を確定する操作",
                "false": "商品や条件の閲覧、カートへの追加など、確定前の操作",
            },
        ),
        "change": Noul(
            instructions="`action` を実行すると、既存のデータや設定が保存・変更・削除されるか。",
            criteria={
                "true": "保存、変更、削除を確定する操作",
                "false": "閲覧や一時的な入力など、永続的な変更を確定しない操作",
            },
        ),
        "access": Noul(
            instructions="`action` を実行すると、ログイン・認証情報・アクセス権限が変更または確定するか。",
            criteria={
                "true": "認証の実行、認証情報や権限の変更を確定する操作",
                "false": "ログイン画面への移動や入力欄への記入など、確定前の操作",
            },
        ),
    }


def risk_state(task: str, view: PageView, action_id: str, description: str, text: str) -> dict[str, Any]:
    """選択要素とフォームの関係を渡す。本文や入力済みの値は含めない。"""
    element_id = action_id.partition(":")[2].partition("#")[0]
    selected_index = next((i for i, element in enumerate(view.elements) if element.id == element_id), None)
    selected = view.elements[selected_index] if selected_index is not None else None
    related = [
        {"name": element.name, "kind": element.kind, "filled": element.filled}
        for element in view.elements
        if selected is not None
        and element.id != selected.id
        and element.kind in {"type", "select"}
        and ((selected.form_id and element.form_id == selected.form_id) or element.submit_id == selected.id)
    ][:12]
    nearby: list[str] = []
    if selected_index is not None:
        neighbors = view.elements[max(0, selected_index - 2) : selected_index + 3]
        nearby = [element.name for element in neighbors if element.id != element_id]
    return {
        "task": task,
        "url": view.url,
        "title": view.title,
        "action": {"id": action_id, "description": description, "text": text},
        "element": (
            {"role": selected.role, "kind": selected.kind, "name": selected.name, "form_id": selected.form_id}
            if selected else None
        ),
        "related_fields": related,
        "nearby_elements": nearby,
    }


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
        text = plan.text if plan.action_id.startswith("type:") else None
        if plan.action_id in SAFE_ACTIONS:
            return Decision(
                action_id=plan.action_id,
                done_probability=1.0 if plan.action_id == "done" else 0.0,
                model=plan.model,
                latency_ms=plan.latency_ms,
                reason=plan.reason,
            )

        description = next((option.description for option in options if option.id == plan.action_id), plan.action_id)
        questions = build_risk_questions()

        def interpret(response: Any, latency_ms: int) -> tuple[dict[str, float], str, int]:
            factors = {key: float(response.nouls[key].noul) for key in questions}
            if any(not 0 <= value <= 1 for value in factors.values()):
                raise ValueError("危険度の確率が不正です")
            return factors, model_of(response), latency_ms

        def unknown_risk(message: str) -> tuple[dict[str, float], str, int]:
            # 危険度が分からない操作は、取り消しにくいものとみなして承認を求める。
            return {}, "", 0

        factors, jev_model, jev_latency = await ask_or_fallback(
            self.asker,
            state=risk_state(task, view, plan.action_id, description, plan.text),
            questions=questions,
            interpret=interpret,
            fallback=unknown_risk,
            subject="危険度の判定",
        )
        return Decision(
            action_id=plan.action_id,
            risk_probability=max(factors.values()) if factors else 1.0,
            risk_factors=factors,
            model=f"{plan.model} + {jev_model}" if jev_model else plan.model,
            latency_ms=plan.latency_ms + jev_latency,
            text=text,
            reason=plan.reason,
        )
