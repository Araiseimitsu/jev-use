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
