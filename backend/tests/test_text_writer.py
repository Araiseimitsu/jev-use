"""入力文と最終回答のテキスト生成のテスト。"""

import asyncio

from app.services.text_writer import TextWriter, fallback_phrase, fallback_summary, parse_text_payload


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


class _FakeResponse:
    def __init__(self, data: dict) -> None:
        import json

        self._payload = {"candidates": [{"content": {"parts": [{"text": json.dumps(data, ensure_ascii=False)}]}}]}

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


class _FakeHttp:
    def __init__(self, data: dict) -> None:
        self.data = data
        self.prompts: list[str] = []

    async def post(self, url: str, headers: dict, json: dict) -> _FakeResponse:
        self.prompts.append(json["contents"][0]["parts"][0]["text"])
        return _FakeResponse(self.data)


def test_phrase_is_empty_when_the_request_has_nothing_for_the_field() -> None:
    """依頼の文字列が別の欄（メッセージ欄など）向けなら、この欄には何も入れない。"""
    http = _FakeHttp({"fits": False, "text": "test"})
    writer = TextWriter(api_key="key")

    text = asyncio.run(writer.phrase(http, "ルームのメッセージにtestと入れて送信", "ルームを検索...", "チャット", "検索欄"))  # type: ignore[arg-type]

    assert text == ""
    assert "検索欄" in http.prompts[0]


def test_phrase_returns_text_that_fits_the_field() -> None:
    http = _FakeHttp({"fits": True, "text": "test"})
    writer = TextWriter(api_key="key")

    text = asyncio.run(writer.phrase(http, "メッセージにtestと入れて送信", "メッセージを入力", "チャット", "文章の入力欄"))  # type: ignore[arg-type]

    assert text == "test"
