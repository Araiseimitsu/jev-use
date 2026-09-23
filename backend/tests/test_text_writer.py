"""入力文と最終回答のテキスト生成のテスト。"""

import asyncio

from app.services.text_writer import (
    TextWriter, fallback_phrase, fallback_summary, parse_text_payload, parse_vision_payload,
)


def test_parse_text_payload_reads_json_text() -> None:
    payload = {"candidates": [{"content": {"parts": [{"text": '{"text": "晴れ"}'}]}}]}

    assert parse_text_payload(payload) == "晴れ"


def test_fallback_phrase_prefers_quotes() -> None:
    assert fallback_phrase("「東京 天気」を調べて") == "東京 天気"
    assert fallback_phrase("東京の天気") == "東京の天気"


def test_fallback_phrase_skips_login_lines() -> None:
    task = (
        "https://example.com/login\n"
        "ユーザー名: alice\n"
        "パスワード: secret-value\n"
        "- スケジュールが面で今日のすべての予定を教えて"
    )

    assert fallback_phrase(task) == "スケジュールが面で今日のすべての予定を教えて"


def test_summary_without_key_uses_excerpt() -> None:
    writer = TextWriter(api_key="")

    text = asyncio.run(writer.summary(None, "天気", "https://example.com", "例", "本文です"))  # type: ignore[arg-type]

    assert text == "本文です"
    assert fallback_summary("例", "") == "例"


def test_phrase_without_key_does_not_call_the_network() -> None:
    writer = TextWriter(api_key="")

    text = asyncio.run(writer.phrase(None, "「富士山」の標高", "検索", "Google"))  # type: ignore[arg-type]

    assert text == "富士山"


def test_parse_vision_payload_requires_visible_evidence() -> None:
    def payload(found: bool, answer: str, evidence: str) -> dict:
        return {"candidates": [{"content": {"parts": [{"text": (
            f'{{"found": {str(found).lower()}, "answer": "{answer}", "evidence": "{evidence}"}}'
        )}]}}]}

    assert parse_vision_payload(payload(True, "14時に会議", "14:00 会議")) == "14時に会議"
    assert parse_vision_payload(payload(True, "14時に会議", "")) is None
    assert parse_vision_payload(payload(False, "14時に会議", "14:00 会議")) is None


def test_visual_answer_sends_jpeg_and_task_to_gemini() -> None:
    class FakeResponse:
        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict:
            return {"candidates": [{"content": {"parts": [{"text": (
                '{"found": true, "answer": "14時に会議", "evidence": "14:00 会議"}'
            )}]}}]}

    class FakeClient:
        def __init__(self) -> None:
            self.body: dict = {}

        async def post(self, url: str, **kwargs: object) -> FakeResponse:
            self.body = kwargs["json"]  # type: ignore[assignment]
            return FakeResponse()

    client = FakeClient()
    writer = TextWriter(api_key="test-key", model="test-model")

    answer = asyncio.run(writer.visual_answer(client, "今日の予定", "https://example.com", "予定", "YQ=="))  # type: ignore[arg-type]

    assert answer == "14時に会議"
    parts = client.body["contents"][0]["parts"]
    assert parts[0]["inline_data"] == {"mime_type": "image/jpeg", "data": "YQ=="}
    assert "今日の予定" in parts[1]["text"]


def test_visual_answer_without_key_does_not_send_image() -> None:
    writer = TextWriter(api_key="")

    assert asyncio.run(writer.visual_answer(None, "予定", "https://example.com", "例", "YQ==")) is None  # type: ignore[arg-type]
