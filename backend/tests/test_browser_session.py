"""実ブラウザで入力欄への反映を確認する。"""

import asyncio

import pytest
from playwright.async_api import async_playwright

from app.services.browser_session import BrowserSession


def test_fill_checks_the_value_after_the_page_updates() -> None:
    async def exercise() -> None:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.set_content(
                    '<form><input data-jev-use-id="e1" aria-label="メッセージ">'
                    '<button type="submit" disabled>送信</button></form>'
                    '<p role="status">送信しました</p>'
                )
                session = BrowserSession(headless=True)
                session._page = page

                await session.fill("e1", "hello", False)
                assert await page.locator("input").input_value() == "hello"
                view = await session.read()
                assert view.feedback == "送信しました"
                assert view.elements[0].filled is True
                assert view.elements[0].form_id == "f1"
                assert view.elements[1].disabled is True

                await page.locator("input").evaluate(
                    "el => el.addEventListener('input', () => { el.value = ''; })"
                )
                with pytest.raises(RuntimeError, match="入力欄に指定した文字列"):
                    await session.fill("e1", "again", False)
            finally:
                await browser.close()

    asyncio.run(exercise())


def _with_page(html: str, check) -> None:
    """HTML を開いた BrowserSession を check に渡す。"""

    async def exercise() -> None:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.set_content(html)
                session = BrowserSession(headless=True)
                session._page = page
                await check(session, page)
            finally:
                await browser.close()

    asyncio.run(exercise())


def test_read_keeps_inputs_below_many_links_and_off_screen() -> None:
    links = "".join(f'<a href="/p{i}">ナビ{i}</a>' for i in range(200))
    html = (
        f"<nav>{links}</nav>"
        '<div style="height:3000px"></div>'
        '<form><input aria-label="お名前"><button>確認</button></form>'
        '<input aria-label="隠し" style="display:none">'
    )

    async def check(session: BrowserSession, page) -> None:
        view = await session.read()
        names = [element.name for element in view.elements]
        assert "お名前" in names
        assert "確認" in names
        assert "隠し" not in names
        assert len(view.elements) == 150
        # 読み直しても id は重複しない。
        await session.read()
        assert await page.locator('[data-jev-use-id="e1"]').count() == 1

    _with_page(html, check)


def test_read_finds_the_send_button_by_structure() -> None:
    html = (
        '<form><input aria-label="件名"><button type="button">戻る</button>'
        '<button type="submit">確認する</button></form>'
        '<div class="composer"><div contenteditable="true" data-placeholder="メッセージ"></div>'
        '<button aria-label="Send message">↑</button></div>'
        '<form action="/search"><input name="q"></form>'
    )

    async def check(session: BrowserSession, page) -> None:
        view = await session.read()
        by_name = {element.name: element for element in view.elements}
        assert by_name["件名"].submit_id == by_name["確認する"].id
        assert by_name["メッセージ"].submit_id == by_name["Send message"].id
        assert by_name["メッセージ"].role == "textbox"
        assert by_name["q"].search is True

        # 添付・音声入力・送信が並ぶチャット欄でも、送信ボタンを名前で決める。
        await page.set_content(
            '<nav><a href="/new">新しいチャット</a></nav>'
            '<div class="input-area"><div class="main"><div class="wrap">'
            '<div contenteditable="true" role="textbox" aria-label="プロンプトを入力"></div></div>'
            '<button aria-label="アップロード">+</button></div>'
            '<div class="trailing"><button aria-label="音声入力">m</button>'
            '<button aria-label="プロンプトを送信">↑</button></div></div>'
        )
        view = await session.read()
        by_name = {element.name: element for element in view.elements}
        assert by_name["プロンプトを入力"].submit_id == by_name["プロンプトを送信"].id

    _with_page(html, check)


def test_fill_retypes_when_the_page_rejects_a_direct_value() -> None:
    # キー入力を伴わない値の変更を戻す欄（リッチエディタの代わり）。
    html = (
        '<input data-jev-use-id="e1" aria-label="メッセージ">'
        "<script>"
        "const el = document.querySelector('input'); let typed = false;"
        "el.addEventListener('keydown', () => { typed = true; });"
        "el.addEventListener('input', () => { if (!typed) el.value = ''; });"
        "</script>"
    )

    async def check(session: BrowserSession, page) -> None:
        await session.fill("e1", "hello", False)
        assert await page.locator("input").input_value() == "hello"

    _with_page(html, check)


def test_excerpt_reads_main_and_keeps_the_latest_text() -> None:
    history = "".join(f"<li>過去の会話{i}</li>" for i in range(300))
    answer = "最新の回答です"
    html = f"<nav><ul>{history}</ul></nav><main><p>{'前の話。' * 800}</p><p>{answer}</p></main>"

    async def check(session: BrowserSession, page) -> None:
        view = await session.read()
        assert "過去の会話" not in view.excerpt
        assert answer in view.excerpt
        assert len(view.excerpt) <= 1500

    _with_page(html, check)
