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
