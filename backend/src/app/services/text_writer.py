"""入力欄に入れる文字列と、最後の答えの文章を作る。

Jev は候補を選ぶだけで、自由な文章は返さない。文章が要るときだけ Gemini のテキスト生成を使う。
画像は送らない。
"""

import json
import logging
import re
from typing import Any

import httpx

from app.core.config import settings
from app.services.page_state import request_line

logger = logging.getLogger(__name__)

GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta"

_QUOTED = re.compile(r"「([^」]{1,200})」|\"([^\"]{1,200})\"")

_TEXT_SCHEMA: dict[str, Any] = {
    "type": "OBJECT",
    "properties": {"text": {"type": "STRING"}},
    "required": ["text"],
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


def parse_text_payload(payload: dict[str, Any]) -> str:
    """generateContent の JSON から text を取り出す。"""
    parts = payload["candidates"][0]["content"]["parts"]
    raw = "".join(part.get("text", "") for part in parts)
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("文章が JSON オブジェクトではありません")
    text = str(data.get("text", "")).strip()
    if not text:
        raise ValueError("文章が空です")
    return text


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
    ) -> str:
        """入力欄に入れる文字列。無効・失敗時は指示から作る。"""
        if not self.enabled:
            return fallback_phrase(task)
        prompt = (
            "指定の入力欄へ入れる文字列だけを JSON で返せ。"
            "ユーザー名、パスワード、指示文そのもの、行頭の記号は入れない。"
            "検索欄なら検索語だけにする。\n"
            f"依頼: {task}\nページ: {page_title}\n入力欄: {field_name}"
        )
        text = await self._generate(client, prompt)
        return (text or fallback_phrase(task))[:200]

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
            "ページの抜粋だけを根拠に、指示への答えを日本語で短く書け。"
            "抜粋に答えが無ければ、その旨を書く。\n"
            f"指示: {task}\nURL: {url}\nタイトル: {title}\n抜粋: {excerpt[:1500]}"
        )
        text = await self._generate(client, prompt)
        return text or fallback_summary(title, excerpt)

    async def _generate(self, client: httpx.AsyncClient, prompt: str) -> str | None:
        body = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseSchema": _TEXT_SCHEMA,
                "temperature": 0,
            },
        }
        try:
            response = await client.post(
                f"{GEMINI_API_BASE}/models/{self.model}:generateContent",
                headers={"x-goog-api-key": self.api_key},
                json=body,
            )
            response.raise_for_status()
            return parse_text_payload(response.json())
        except Exception:
            logger.warning("テキスト生成に失敗しました", exc_info=True)
            return None
