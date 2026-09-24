"""次の 1 操作を Gemini に選ばせる。

Jev は候補から 1 つを選ぶ分類器で、「探す → 開く → 入力 → 送信」のような手順を考えられない。
手順の判断は推論できるモデルに任せ、Jev は選ばれた操作の危険度の判定だけを担う。
画面の画像は送らない。ページの文字、操作の候補、実行済みの操作だけを渡す。
"""

import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

from app.core.config import settings
from app.services.gemini import generate_json
from app.services.page_state import ActionOption, PageView

logger = logging.getLogger(__name__)

# 次の操作を選ぶのに Gemini が必須なため、キーが無いと実行を始めない
MISSING_GEMINI_MESSAGE = "Gemini API キーが未設定です。backend/.env に GEMINI_API_KEY を設定してください。"
# プロンプトに載せる実行済み操作の件数
HISTORY_FOR_PLANNER = 12

_RULES = """あなたはブラウザを操作して、利用者の依頼を達成する。いまのページと実行済みの操作を見て、次の 1 操作を候補から選べ。
規則:
- action は候補の id から 1 つだけ選ぶ。
- 依頼を達成する手順を考え（例: 相手やルームを探す → 開く → 文を入力 → 送信ボタンを押す）、いまどの段階で、なぜその操作かを reason に短く書く。
- history の操作は実行済み。同じ入力やクリックを繰り返さない。「画面の変化なし」だった操作の後は別の手を選ぶ。
- 入力（type:）を選んだら、text にその欄へ入れる文字列だけを書く。
  - 検索欄: 開く・探す・送る相手の名前（ルーム名、人名、商品名など）や調べたい語だけ。送る文章は入れない。
  - 文章の入力欄（メッセージ・本文・コメントなど）: 送る・投稿する・書き込む文だけ。
  - 依頼にその欄へ入れるものが無い欄は選ばない。
- 文を入力した後は、その欄の送信・確定のボタン（名前の無いアイコンのボタンを含む）を選ぶ。
- 依頼への答えがページに出ている、または依頼が済んだら done を選ぶ。
- ログイン、パスワード、購入や削除などは、依頼に明記されていなければ選ばない。
- プルダウン（select:）を選んだら、text に選ぶ選択肢の名前を候補の説明どおりに書く（例: 最安値を探すなら価格の安い順）。
- 入力と選択以外を選んだときは text を空にする。"""


@dataclass(frozen=True)
class Plan:
    """Gemini が選んだ次の操作。"""

    action_id: str
    # type 操作で入れる文字列。入力以外では空。
    text: str
    reason: str
    model: str
    latency_ms: int


def plan_schema(options: tuple[ActionOption, ...] | list[ActionOption]) -> dict[str, Any]:
    """操作を候補の id に限る JSON スキーマ。理由を先に書かせてから選ばせる。"""
    return {
        "type": "OBJECT",
        "properties": {
            "reason": {"type": "STRING"},
            "action": {"type": "STRING", "enum": [option.id for option in options]},
            "text": {"type": "STRING"},
        },
        "required": ["reason", "action", "text"],
        "propertyOrdering": ["reason", "action", "text"],
    }


def plan_prompt(
    task: str,
    view: PageView,
    options: tuple[ActionOption, ...] | list[ActionOption],
    history: tuple[str, ...] = (),
) -> str:
    """Gemini に渡す文。画像も座標も含めない。"""
    recent = history[-HISTORY_FOR_PLANNER:]
    done = "\n".join(f"{index}. {entry}" for index, entry in enumerate(recent, 1)) or "（まだ無い）"
    candidates = "\n".join(f"- {option.id}: {option.description}" for option in options)
    return (
        f"{_RULES}\n\n"
        f"依頼: {task}\n"
        f"URL: {view.url}\n"
        f"タイトル: {view.title}\n"
        f"ページの文字: {view.excerpt}\n"
        f"history:\n{done}\n"
        f"候補:\n{candidates}"
    )


class GeminiPlanner:
    """ページと実行済みの操作から、次の 1 操作を Gemini に選ばせる。"""

    def __init__(self, api_key: str | None = None, model: str | None = None) -> None:
        self.api_key = (api_key if api_key is not None else settings.gemini_api_key).strip()
        self.model = model or settings.gemini_model

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    async def plan(
        self,
        client: httpx.AsyncClient,
        task: str,
        view: PageView,
        options: tuple[ActionOption, ...],
        history: tuple[str, ...] = (),
    ) -> Plan | None:
        """次の操作を選ぶ。通信・解釈に失敗したり候補に無い操作が返ったりしたら None。"""
        started = time.perf_counter()
        try:
            data = await generate_json(
                client, self.api_key, self.model, plan_prompt(task, view, options, history), plan_schema(options)
            )
        except Exception:
            logger.warning("次の操作の選択に失敗しました", exc_info=True)
            return None
        action_id = str(data.get("action", ""))
        if action_id not in {option.id for option in options}:
            logger.warning("候補に無い操作が返りました: %s", action_id)
            return None
        text = str(data.get("text", "")).strip() if action_id.startswith(("type:", "select:")) else ""
        return Plan(
            action_id=action_id,
            text=text[:200],
            reason=str(data.get("reason", "")).strip()[:300],
            model=self.model,
            latency_ms=round((time.perf_counter() - started) * 1000),
        )
