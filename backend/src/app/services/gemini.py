"""Gemini の generateContent を JSON 出力で呼ぶ共通部品。

判断や文章の中身は持たず、「プロンプトと JSON スキーマを渡し、辞書を受け取る」ことだけを担う。
次の操作の選択（planner）と、入力文・最終回答の生成（text_writer）が共有する。
"""

import json
from typing import Any

import httpx

GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta"


def parse_json_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """generateContent の JSON 応答の本文を辞書にする。"""
    parts = payload["candidates"][0]["content"]["parts"]
    raw = "".join(part.get("text", "") for part in parts)
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("応答が JSON オブジェクトではありません")
    return data


async def generate_json(
    client: httpx.AsyncClient,
    api_key: str,
    model: str,
    prompt: str,
    schema: dict[str, Any],
) -> dict[str, Any]:
    """schema に沿った JSON を生成して辞書で返す。通信・解釈の失敗は例外のまま呼び出し側へ返す。"""
    body = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": schema,
            "temperature": 0,
        },
    }
    response = await client.post(
        f"{GEMINI_API_BASE}/models/{model}:generateContent",
        headers={"x-goog-api-key": api_key},
        json=body,
    )
    response.raise_for_status()
    return parse_json_payload(response.json())
