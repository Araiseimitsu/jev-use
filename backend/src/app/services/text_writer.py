"""入力欄に入れる文字列と、最後の答えの文章を作る。

次の操作を選ぶ Gemini（planner）が入力文を決めなかったときの補助と、最後の答えの文章を担う。
画像は送らない。
"""

import logging
import re
from collections.abc import Callable
from typing import Any

import httpx

from app.core.config import settings
from app.services.gemini import generate_json, parse_json_payload
from app.services.page_state import request_line

logger = logging.getLogger(__name__)

_QUOTED = re.compile(r"「([^」]{1,200})」|\"([^\"]{1,200})\"")

_TEXT_SCHEMA: dict[str, Any] = {
    "type": "OBJECT",
    "properties": {"text": {"type": "STRING"}},
    "required": ["text"],
}

# 入力欄の文字列。依頼にこの欄へ入れる内容が無ければ fits を false にさせる。
_PHRASE_SCHEMA: dict[str, Any] = {
    "type": "OBJECT",
    "properties": {"fits": {"type": "BOOLEAN"}, "text": {"type": "STRING"}},
    "required": ["fits", "text"],
}


def fallback_phrase(task: str) -> str:
    """モデルが使えないとき、依頼文の引用符内か依頼文そのものを入力文字にする。

    ログイン行と URL は入れない。
    """
    line = request_line(task)
    match = _QUOTED.search(line or task)
    if match:
        return next(group for group in match.groups() if group)
    return (line or task.strip())[:80]


def fallback_summary(title: str, excerpt: str) -> str:
    """モデルが使えないとき、ページの抜粋を結果にする。"""
    body = excerpt.strip()
    if body:
        return body[:500]
    return title.strip() or "ページから文章を取り出せませんでした。"


def _pick_text(data: dict[str, Any]) -> str:
    """応答の text を取り出す。空なら失敗とする。"""
    text = str(data.get("text", "")).strip()
    if not text:
        raise ValueError("文章が空です")
    return text


def _pick_phrase(data: dict[str, Any]) -> str:
    """入力欄向けの応答から文字列を取り出す。この欄に入れる内容が無ければ空。"""
    if data.get("fits") is False:
        return ""
    return _pick_text(data)


def parse_text_payload(payload: dict[str, Any]) -> str:
    """generateContent の JSON から text を取り出す。"""
    return _pick_text(parse_json_payload(payload))


class TextWriter:
    """入力文と最終回答を作る。失敗しても例外は投げず、抜粋へ倒す。"""

    def __init__(self, api_key: str | None = None, model: str | None = None) -> None:
        self.api_key = (api_key if api_key is not None else settings.gemini_api_key).strip()
        self.model = model or settings.gemini_model

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    async def phrase(
        self,
        client: httpx.AsyncClient,
        task: str,
        field_name: str,
        page_title: str,
        field_kind: str = "",
    ) -> str:
        """入力欄に入れる文字列。依頼にこの欄へ入れる内容が無ければ空。無効・失敗時は指示から作る。"""
        if not self.enabled:
            return fallback_phrase(task)
        prompt = (
            "依頼の中に、指定の入力欄へ入れるべき内容があるかを欄の種類ごとに判断し、JSON で返せ。\n"
            "- 検索欄: 依頼に、開く・探す・送る相手の名前（ルーム名、人名、商品名など）や調べたい語があれば、"
            "その名前や語だけを入れる。送る文章そのものは入れない。\n"
            "- 文章の入力欄（メッセージ・本文・コメントなど）: 依頼に送る・投稿する・書き込む文があれば、"
            "欄の名前が依頼に書かれていなくても、その文だけを入れる。\n"
            "- その他の欄: 依頼にその欄の値がはっきり書かれているときだけ入れる。\n"
            "入れる場合は fits を true、text に入れる文字列だけを書く。入れるものが無ければ fits を false、text を空にする。"
            "ユーザー名、パスワード、指示文そのもの、行頭の記号は入れない。\n"
            f"依頼: {task}\nページ: {page_title}\n入力欄: {field_name}\n欄の種類: {field_kind or '不明'}"
        )
        text = await self._generate(client, prompt, _PHRASE_SCHEMA, _pick_phrase)
        if text is None:
            return fallback_phrase(task)[:200]
        return text[:200]

    async def summary(
        self,
        client: httpx.AsyncClient,
        task: str,
        url: str,
        title: str,
        excerpt: str,
    ) -> str:
        """ページの抜粋から、指示への答えを短く書く。"""
        if not self.enabled:
            return fallback_summary(title, excerpt)
        prompt = (
            "ページの抜粋だけを根拠に、日本語で短く書け。"
            "指示が質問なら答えを書く。抜粋に答えが無ければ、その旨を書く。"
            "最安値・最高値などを探す指示なら、抜粋の一覧の価格をすべて比べて答え、商品名も書く。"
            "先頭の広告（スポンサー）の商品だけで決めない。"
            "指示が送信・登録などの操作の依頼なら、抜粋から分かる結果（完了やエラーの表示など）を書く。\n"
            f"指示: {task}\nURL: {url}\nタイトル: {title}\n抜粋: {excerpt[:4000]}"
        )
        text = await self._generate(client, prompt)
        return text or fallback_summary(title, excerpt)

    async def _generate(
        self,
        client: httpx.AsyncClient,
        prompt: str,
        schema: dict[str, Any] = _TEXT_SCHEMA,
        pick: Callable[[dict[str, Any]], str] = _pick_text,
    ) -> str | None:
        """schema に沿って生成し、pick で文字列にする。失敗時は None。"""
        try:
            data = await generate_json(client, self.api_key, self.model, prompt, schema)
            return pick(data)
        except Exception:
            logger.warning("テキスト生成に失敗しました", exc_info=True)
            return None
