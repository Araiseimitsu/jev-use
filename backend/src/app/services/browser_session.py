"""Playwright でページを読み、選ばれた要素を操作する。

操作の判断には使わない。回答を文字情報から確認できない場合は画像も撮影する。
"""

import base64
import logging
from typing import Any

from app.core.config import settings
from app.services.page_state import PageView, page_view_from_raw

logger = logging.getLogger(__name__)

# 画面上の操作できる要素を id 付きで返す。パスワード欄も見つけ、入力は人が行う。
READ_SCRIPT = """
() => {
  const selector = [
    'a[href]', 'button', 'input:not([type=hidden])', 'textarea', 'select',
    '[role="button"]', '[role="link"]', '[role="textbox"]', '[role="searchbox"]',
    '[role="tab"]', '[role="menuitem"]', '[contenteditable="true"]'
  ].join(',');
  const skipType = new Set(['hidden', 'file']);
  const out = [];
  for (const el of document.querySelectorAll(selector)) {
    if (out.length >= 40) break;
    const style = getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    if (rect.width < 2 || rect.height < 2) continue;
    if (style.visibility === 'hidden' || style.display === 'none') continue;
    const tag = el.tagName.toLowerCase();
    const inputType = (el.getAttribute('type') || '').toLowerCase();
    if (skipType.has(inputType)) continue;
    const role = el.getAttribute('role') || (
      tag === 'a' ? 'link' : tag === 'button' ? 'button' : tag === 'select' ? 'combobox' : tag
    );
    const textLike = tag === 'textarea' || el.isContentEditable || role === 'textbox' || role === 'searchbox'
      || (tag === 'input' && ['', 'text', 'search', 'email', 'url', 'tel', 'number', 'password'].includes(inputType));
    const kind = textLike ? 'type' : 'click';
    const labelled = (el.labels && el.labels.length)
      ? Array.from(el.labels).map(node => node.innerText).join(' ')
      : '';
    const name = (
      el.getAttribute('aria-label') || el.getAttribute('placeholder') || labelled || el.innerText
      || el.getAttribute('title') || el.getAttribute('name') || ''
    ).replace(/\\s+/g, ' ').trim().slice(0, 80);
    const autocomplete = (el.getAttribute('autocomplete') || '').toLowerCase();
    const label = name || (
      inputType === 'password' || autocomplete.includes('password') ? 'パスワード'
      : autocomplete === 'username' || autocomplete === 'email' ? 'ユーザー名'
      : kind === 'type' ? '入力欄' : ''
    );
    if (!label) continue;
    const id = 'e' + (out.length + 1);
    el.setAttribute('data-jev-use-id', id);
    const disabled = el.matches(':disabled') || el.getAttribute('aria-disabled') === 'true';
    const filled = textLike && Boolean(String(el.isContentEditable ? el.textContent : el.value || '').trim());
    const form = el.form || el.closest('form');
    const formId = form ? 'f' + (Array.from(document.forms).indexOf(form) + 1) : '';
    out.push({ id, role, name: label, kind, inputType, autocomplete, disabled, filled, formId });
  }
  return {
    url: location.href,
    title: document.title || '',
    excerpt: (() => {
      // ナビゲーションが長いページでも、主要領域の本文を先に判断へ渡す。
      const main = document.querySelector('main, [role="main"], article');
      const scope = main || document.body;
      const text = (scope && scope.innerText || '').replace(/\\s+/g, ' ').trim();
      const labels = [];
      for (const el of (scope || document).querySelectorAll('[aria-label], [title], img[alt], svg text')) {
        if (labels.length >= 80) break;
        const rect = el.getBoundingClientRect();
        const style = getComputedStyle(el);
        if (rect.width < 2 || rect.height < 2 || style.display === 'none' || style.visibility === 'hidden') continue;
        const label = (el.getAttribute('aria-label') || el.getAttribute('title')
          || el.getAttribute('alt') || el.textContent || '').replace(/\\s+/g, ' ').trim();
        if (label && !text.includes(label) && !labels.includes(label)) labels.push(label);
      }
      const labelledText = labels.join(' ').slice(0, 600);
      return [labelledText, text.slice(0, 1500 - labelledText.length - 1)].filter(Boolean).join(' ');
    })(),
    feedback: Array.from(document.querySelectorAll(
      '[role="status"], [role="alert"], [aria-live="polite"], [aria-live="assertive"]'
    )).filter(el => {
      const style = getComputedStyle(el);
      const rect = el.getBoundingClientRect();
      return rect.width >= 2 && rect.height >= 2 && style.visibility !== 'hidden' && style.display !== 'none';
    }).map(el => el.innerText || '').join(' ').replace(/\\s+/g, ' ').trim().slice(0, 300),
    elements: out,
  };
}
"""


class BrowserSession:
    """Chromium を 1 タスクのあいだ開いておく。"""

    def __init__(self, headless: bool) -> None:
        self.headless = headless
        self._playwright: Any = None
        self._browser: Any = None
        self._page: Any = None

    async def start(self, url: str) -> None:
        """Chromium を起動して URL を開く。"""
        try:
            from playwright.async_api import async_playwright
        except ImportError as e:
            raise RuntimeError(
                "Playwright が入っていません。backend で `uv add playwright` を実行してください。"
            ) from e

        try:
            manager = async_playwright()
            self._playwright = await manager.start()
            context = await self._open_context()
            self._browser = context
            page = context.pages[0] if context.pages else await context.new_page()
            self._page = page
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            await self.close()
            if "Executable doesn't exist" in str(e):
                raise RuntimeError(
                    "Chromium が見つかりません。backend で `uv run playwright install chromium` を実行してください。"
                ) from e
            raise

    async def _open_context(self) -> Any:
        """普段の Chrome を、確認結果を覚えておく専用のプロフィールで開く。

        入っていなければ Playwright の Chromium に戻す。利用中の Chrome のプロフィールは使わない。
        """
        settings.browser_profile_dir.mkdir(parents=True, exist_ok=True)
        options: dict[str, Any] = {
            "user_data_dir": str(settings.browser_profile_dir),
            "headless": self.headless,
            "viewport": {"width": 1280, "height": 800},
            "locale": "ja-JP",
        }
        try:
            return await self._playwright.chromium.launch_persistent_context(channel="chrome", **options)
        except Exception:
            logger.info("Google Chrome が見つからないため、Playwright の Chromium を使います")
            return await self._playwright.chromium.launch_persistent_context(**options)

    async def peek(self) -> str:
        """ページの中身を読まず、いまのアドレスだけを返す。"""
        return self._page.url

    async def read(self) -> PageView:
        """いまのページから操作候補を読む。"""
        raw = await self._page.evaluate(READ_SCRIPT)
        return page_view_from_raw(raw)

    async def click(self, element_id: str) -> None:
        await self._locator(element_id).click(timeout=8000)
        await self._settle()

    async def fill(self, element_id: str, text: str, submit: bool) -> None:
        locator = self._locator(element_id)
        await locator.fill(text, timeout=8000)
        if submit:
            await locator.press("Enter")
        await self._settle()
        if not submit:
            try:
                actual = await locator.evaluate(
                    "el => el.isContentEditable ? el.innerText : el.value", timeout=2000
                )
            except Exception as e:
                raise RuntimeError("入力欄の値を確認できませんでした。") from e
            if actual != text:
                raise RuntimeError("入力欄に指定した文字列が反映されませんでした。")

    async def scroll(self) -> bool:
        # wheel はポインター直下へ届くため、ページ中央のスクロール領域を狙う。
        position_script = """() => {
          const el = document.elementFromPoint(innerWidth / 2, innerHeight / 2);
          const positions = [];
          for (let node = el; node; node = node.parentElement) positions.push(node.scrollTop);
          return {x: innerWidth / 2, y: innerHeight / 2, page: scrollY, positions};
        }"""
        before = await self._page.evaluate(position_script)
        await self._page.mouse.move(before["x"], before["y"])
        await self._page.mouse.wheel(0, 900)
        await self._settle()
        after = await self._page.evaluate(position_script)
        return before != after

    async def back(self) -> None:
        await self._page.go_back(wait_until="domcontentloaded", timeout=8000)
        await self._settle()

    async def wait(self) -> None:
        await self._page.wait_for_timeout(1000)

    async def preview_jpeg(self) -> str | None:
        """人が見るプレビュー。失敗しても操作は続ける。"""
        try:
            data = await self._page.screenshot(type="jpeg", quality=60)
        except Exception:
            logger.warning("プレビューの取得に失敗しました", exc_info=True)
            return None
        return base64.standard_b64encode(data).decode("ascii")

    async def vision_jpeg(self) -> str | None:
        """回答確認用に、画面上の小さな文字も読める品質で撮影する。"""
        try:
            data = await self._page.screenshot(type="jpeg", quality=85)
        except Exception:
            logger.warning("回答確認用の画像を取得できませんでした", exc_info=True)
            return None
        return base64.standard_b64encode(data).decode("ascii")

    async def close(self) -> None:
        browser = self._browser
        playwright = self._playwright
        self._page = None
        self._browser = None
        self._playwright = None
        # 利用者がウィンドウを閉じた後などは close が失敗する。Playwright の停止は必ず行う。
        try:
            if browser is not None:
                await browser.close()
        except Exception:
            logger.warning("ブラウザを閉じられませんでした", exc_info=True)
        finally:
            if playwright is not None:
                await playwright.stop()

    def _locator(self, element_id: str) -> Any:
        return self._page.locator(f'[data-jev-use-id="{element_id}"]')

    async def _settle(self) -> None:
        try:
            await self._page.wait_for_load_state("domcontentloaded", timeout=8000)
        except Exception:
            logger.debug("ページの再読み込み待ちがタイムアウトしました")
        await self._page.wait_for_timeout(300)
