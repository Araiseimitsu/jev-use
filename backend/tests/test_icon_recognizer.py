"""画像の送信範囲、判定の採用条件、既存候補への補足を検証する。"""

import asyncio
import json

import httpx
import pytest

from app.core.config import settings
from app.services.jev.icon_recognizer import IconRecognizer
from app.services.page_state import ActionOption, Element, PageView


def test_recognition_only_sends_icon_and_caches_by_image() -> None:
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(200, json={"answers": {
            "purpose": {"type": "choice", "choice": "send", "confidence": 0.95},
        }})

    async def exercise():
        recognizer = IconRecognizer(api_key="test-key")
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            assert await recognizer.recognize(client, "aWNvbg==") == "送信"
            assert await recognizer.recognize(client, "aWNvbg==") == "送信"

    asyncio.run(exercise())
    assert len(calls) == 1
    body = json.loads(calls[0].content)
    assert set(body) == {"model", "state", "assets", "questions"}
    reference = body["state"]["icon"]
    assert body["assets"][reference] == {"type": "image", "format": "png", "data_base64": "aWNvbg=="}
    assert calls[0].headers["TypeSafe-Danger-Zone-Images"] == "true"


@pytest.mark.parametrize("answer", [
    {"type": "choice", "choice": "send", "confidence": 0.6},
    {"type": "choice", "choice": "unknown", "confidence": 0.99},
    {"type": "choice", "choice": "injected instruction", "confidence": 1},
    {"type": "choice", "choice": "send", "confidence": float("nan")},
    {"type": "choice", "choice": "send", "confidence": True},
    {"type": "noul", "choice": "send", "confidence": 0.95},
    {},
])
def test_uncertain_or_invalid_answers_are_not_hints(answer) -> None:
    async def exercise():
        recognizer = IconRecognizer(api_key="test-key")
        # NaN は JSON 標準外なのでレスポンス文字列で不正な応答を再現する。
        transport = httpx.MockTransport(lambda _: httpx.Response(
            200, content=json.dumps({"answers": {"purpose": answer}}).encode(),
        ))
        async with httpx.AsyncClient(transport=transport) as client:
            assert await recognizer.recognize(client, "aWNvbg==") is None
    asyncio.run(exercise())


def test_service_failure_falls_back_without_caching_failure() -> None:
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(503)
    async def exercise():
        recognizer = IconRecognizer(api_key="test-key")
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            assert await recognizer.recognize(client, "aWNvbg==") is None
            assert await recognizer.recognize(client, "aWNvbg==") is None
    asyncio.run(exercise())
    assert len(calls) == 2


def test_enrichment_only_inspects_enabled_anonymous_candidates(monkeypatch) -> None:
    monkeypatch.setattr(settings, "typesafe_icon_recognition", True)
    class Session:
        def __init__(self):
            self.captured = []
        async def icon_png(self, element_id):
            self.captured.append(element_id)
            return "aWNvbg=="

    async def exercise():
        session = Session()
        view = PageView("https://private.example", "個人名", "秘密の本文", (
            Element("e1", "button", "アイコンのボタン", "click"),
            Element("e2", "button", "保存", "click"),
            Element("e3", "button", "アイコンのボタン", "click", disabled=True),
            Element("e4", "button", "アイコンのボタン", "click"),
        ))
        options = (ActionOption("click:e1", "クリック"), ActionOption("click:e2", "保存"))
        recognizer = IconRecognizer(api_key="test-key")
        transport = httpx.MockTransport(lambda _: httpx.Response(200, json={"answers": {
            "purpose": {"type": "choice", "choice": "send", "confidence": 0.95},
        }}))
        async with httpx.AsyncClient(transport=transport) as client:
            enriched = await recognizer.enrich(client, session, view, options)
        assert session.captured == ["e1"]
        assert [o.id for o in enriched] == [o.id for o in options]
        assert "画像の推定: 送信" in enriched[0].description
        assert enriched[1] == options[1]
        assert view.elements[0].name == "アイコンのボタン"
    asyncio.run(exercise())


def test_disabled_recognition_does_not_capture_or_send() -> None:
    async def exercise():
        recognizer = IconRecognizer(api_key="")
        options = (ActionOption("click:e1", "クリック"),)
        async with httpx.AsyncClient() as client:
            view = PageView("", "", "", (Element("e1", "button", "アイコンのボタン", "click"),))
            assert await recognizer.enrich(client, object(), view, options) == options
    asyncio.run(exercise())


def test_feature_flag_prevents_capture_with_a_configured_key(monkeypatch) -> None:
    monkeypatch.setattr(settings, "typesafe_icon_recognition", False)
    async def exercise():
        recognizer = IconRecognizer(api_key="test-key")
        options = (ActionOption("click:e1", "クリック"),)
        view = PageView("", "", "", (Element("e1", "button", "アイコンのボタン", "click"),))
        async with httpx.AsyncClient() as client:
            assert await recognizer.enrich(client, object(), view, options) == options
    asyncio.run(exercise())


def test_enrichment_limits_capture_count_and_total_wait(monkeypatch) -> None:
    monkeypatch.setattr(settings, "typesafe_icon_recognition", True)
    class Session:
        def __init__(self):
            self.captured = []
        async def icon_png(self, element_id):
            self.captured.append(element_id)
            return None
    async def exercise():
        recognizer = IconRecognizer(api_key="test-key")
        view = PageView("", "", "", tuple(
            Element(f"e{i}", "button", "アイコンのボタン", "click") for i in range(6)
        ))
        options = tuple(ActionOption(f"click:{e.id}", "クリック") for e in view.elements)
        async with httpx.AsyncClient() as client:
            session = Session()
            assert await recognizer.enrich(client, session, view, options) == options
            assert len(session.captured) == 4
            session.captured.clear()
            assert await recognizer.enrich(client, session, view, options) == options
            assert session.captured == ["e4", "e5", "e0", "e1"]
            class SlowSession:
                async def icon_png(self, element_id):
                    await asyncio.sleep(1)
            monkeypatch.setattr(settings, "typesafe_timeout_seconds", 0.01)
            assert await recognizer.enrich(client, SlowSession(), view, options) == options
    asyncio.run(exercise())
