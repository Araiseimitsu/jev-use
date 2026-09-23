"""Playwright でページを読み、選ばれた要素を操作する。

判断には使わない。スクショは人が見るプレビューだけで、モデルには送らない。
"""

import base64
import logging
from typing import Any

from app.core.config import settings
from app.services.page_state import PageView, page_view_from_raw

logger = logging.getLogger(__name__)

# 打ち直すときの 1 文字ごとの間隔（ミリ秒）
TYPE_DELAY_MS = 20

# ページ全体の操作できる要素を id 付きで返す。パスワード欄も見つけ、入力は人が行う。
# 画面外の要素も読む。クリックと入力は Playwright が要素まで自動でスクロールする。
# 上限を超えたときは 入力欄 → ボタン類 → リンク の順に残し、出力はページ上の順に戻す。
# a や button でなくても、onclick・tabindex・指の形のカーソルを持つ div や li は押せる行として読む。
READ_SCRIPT = r"""
() => {
  const MAX_ELEMENTS = 150;
  // 送信ボタンを探して入力欄から遡る祖先の段数
  const MAX_SUBMIT_DEPTH = 8;
  const selector = [
    'a[href]', 'button', 'input:not([type=hidden])', 'textarea', 'select',
    '[role="button"]', '[role="link"]', '[role="textbox"]', '[role="searchbox"]',
    '[role="tab"]', '[role="menuitem"]', '[role="menuitemradio"]', '[role="menuitemcheckbox"]',
    '[role="option"]', '[role="treeitem"]', '[role="checkbox"]', '[role="radio"]', '[role="switch"]',
    'summary', '[contenteditable]:not([contenteditable="false"])'
  ].join(',');
  const skipType = new Set(['hidden', 'file']);
  const textTypes = ['', 'text', 'search', 'email', 'url', 'tel', 'number', 'password'];
  const buttonTypes = ['submit', 'button', 'image', 'reset'];
  const searchName = /^(q|query|search|keywords?)$/i;
  const sendName = /送信|送る|投稿|検索|\b(send|submit|post|search)\b/i;
  // id や class は send-button、btnSend のように単語がつながるため、境界を問わない。
  const sendHint = /send|submit|送信/i;
  // 名前に「検索」「送信」を含んでも、取り消し・消去のボタンは送信ボタンにしない（「検索語をクリア」など）。
  const undoName = /クリア|消去|削除|取り消|キャンセル|閉じる|\b(clear|cancel|close|reset|delete)\b/i;
  const EXCERPT_LIMIT = 1500;
  // 長い本文は先頭と末尾を残す。チャットの最新の回答や送信結果は末尾に出る。
  const EXCERPT_HEAD = 600;
  const clean = text => String(text || '').replace(/\s+/g, ' ').trim();
  const clip = (text, limit) => text.length <= limit
    ? text
    : text.slice(0, EXCERPT_HEAD) + ' … ' + text.slice(text.length - (limit - EXCERPT_HEAD - 3));
  // 本文は main があればそこから読む。サイドバーの履歴などで本文の枠が埋まるのを避ける。
  const pageText = () => {
    const main = document.querySelector('main, [role="main"]');
    return (main && clean(main.innerText)) || clean(document.body && document.body.innerText);
  };

  // open な shadow DOM の中も読む。Playwright のロケーターも中まで届く。
  const roots = [document];
  for (let i = 0; i < roots.length; i++) {
    for (const el of roots[i].querySelectorAll('*')) {
      if (el.shadowRoot) roots.push(el.shadowRoot);
    }
  }
  // 前回の id を消す。残すと別の要素と同じ id になり、操作先が二つになる。
  for (const root of roots) {
    for (const el of root.querySelectorAll('[data-jev-use-id]')) el.removeAttribute('data-jev-use-id');
  }

  const visible = el => {
    const rect = el.getBoundingClientRect();
    if (rect.width < 2 || rect.height < 2) return false;
    if (el.closest('[aria-hidden="true"], [inert]')) return false;
    if (el.checkVisibility) return el.checkVisibility({ visibilityProperty: true });
    const style = getComputedStyle(el);
    return style.visibility !== 'hidden' && style.display !== 'none';
  };
  // 表示されている文字を、要素の区切りごとに空白を挟んで読む。
  // innerText は横に並んだ span をつなげてしまい、アイコンの頭文字と名前が「ttest」のようになるため。
  const spacedText = node => {
    if (node.nodeType === 3) return node.textContent;
    if (node.nodeType !== 1 || getComputedStyle(node).display === 'none') return '';
    return Array.from(node.childNodes).map(spacedText).join(' ');
  };
  const labelledBy = el => {
    const root = el.getRootNode();
    return (el.getAttribute('aria-labelledby') || '').split(/\s+/)
      .map(id => (id && root.getElementById ? root.getElementById(id) : null))
      .filter(Boolean).map(node => node.innerText || node.textContent).join(' ');
  };

  const pointer = el => Boolean(el) && el.nodeType === 1 && getComputedStyle(el).cursor === 'pointer';
  // 決まったタグや role を持たないが押せる要素か。カーソルは子に受け継がれるため、指の形が始まる要素だけを取る。
  // ボタンやリンクの中の span、1 つのリンクを包むだけの div は、同じ操作を二重に数えるので除く。
  const looseClickable = el => {
    if (el === document.body || el === document.documentElement) return false;
    if (el.parentElement && el.parentElement.closest(selector)) return false;
    const tabindex = el.getAttribute('tabindex');
    const marked = el.hasAttribute('onclick') || (tabindex !== null && Number(tabindex) >= 0)
      || (pointer(el) && !pointer(el.parentElement));
    if (!marked) return false;
    const text = clean(el.innerText);
    return Boolean(text) && !Array.from(el.querySelectorAll(selector)).some(node => clean(node.innerText) === text);
  };

  const seenLinks = new Set();
  const found = [];
  let order = 0;
  for (const root of roots) {
    for (const el of root.querySelectorAll('*')) {
      const loose = !el.matches(selector);
      if (loose && !looseClickable(el)) continue;
      order += 1;
      if (!visible(el)) continue;
      const tag = el.tagName.toLowerCase();
      const inputType = (el.getAttribute('type') || '').toLowerCase();
      if (skipType.has(inputType)) continue;
      const explicitRole = el.getAttribute('role') || '';
      const textLike = tag === 'textarea' || el.isContentEditable
        || explicitRole === 'textbox' || explicitRole === 'searchbox'
        || (tag === 'input' && textTypes.includes(inputType));
      const role = explicitRole || (
        loose ? 'item'
        : tag === 'a' ? 'link' : tag === 'button' ? 'button' : tag === 'select' ? 'combobox'
        : el.isContentEditable ? 'textbox' : tag
      );
      const kind = textLike ? 'type' : 'click';
      const labelled = (el.labels && el.labels.length)
        ? Array.from(el.labels).map(node => node.innerText).join(' ')
        : '';
      // 入力欄の innerText は入力済みの中身なので名前に使わない。
      const inner = textLike ? '' : spacedText(el);
      const image = el.querySelector('img[alt]');
      const svgTitle = el.querySelector('svg title');
      const buttonValue = tag === 'input' && buttonTypes.includes(inputType) ? el.value : '';
      const name = clean(
        el.getAttribute('aria-label') || labelledBy(el) || el.getAttribute('placeholder')
        || el.getAttribute('aria-placeholder') || el.getAttribute('data-placeholder') || labelled
        || inner || buttonValue || el.getAttribute('title') || (image ? image.alt : '')
        || (svgTitle ? svgTitle.textContent : '') || el.getAttribute('name')
      ).slice(0, 80);
      const buttonLike = !textLike && (
        tag === 'button' || role === 'button' || (tag === 'input' && buttonTypes.includes(inputType))
      );
      // 文字の無いアイコンのボタン。チャットの送信ボタンに多いため捨てずに、id や class の手がかりで名前を付ける。
      const iconHint = [el.id, el.getAttribute('class'), el.getAttribute('data-testid')].join(' ');
      const autocomplete = (el.getAttribute('autocomplete') || '').toLowerCase();
      const label = name || (
        inputType === 'password' || autocomplete.includes('password') ? 'パスワード'
        : autocomplete === 'username' || autocomplete === 'email' ? 'ユーザー名'
        : kind === 'type' ? '入力欄'
        : buttonLike ? (sendHint.test(iconHint) ? '送信（アイコン）' : 'アイコンのボタン') : ''
      );
      if (!label) continue;
      if (role === 'link') {
        // 画像と文字で同じ先を二重に指すリンクは 1 つにまとめる。
        const key = label + '\n' + (el.getAttribute('href') || '');
        if (seenLinks.has(key)) continue;
        seenLinks.add(key);
      }
      const form = el.form || el.closest('form');
      const search = textLike && (
        inputType === 'search' || role === 'searchbox' || Boolean(el.closest('[role="search"]'))
        || searchName.test(el.getAttribute('name') || '')
        || /search|検索/i.test(form ? form.getAttribute('action') || '' : '')
      );
      const priority = textLike ? 0 : role === 'link' ? 2 : 1;
      found.push({ el, order, priority, role, label, kind, inputType, autocomplete, form, search, buttonLike });
    }
  }

  const kept = found
    .sort((a, b) => a.priority - b.priority || a.order - b.order)
    .slice(0, MAX_ELEMENTS)
    .sort((a, b) => a.order - b.order);
  kept.forEach((item, index) => {
    item.id = 'e' + (index + 1);
    item.el.setAttribute('data-jev-use-id', item.id);
  });

  // 入力欄ごとの送信ボタンを、指示文ではなくページの構造とボタン自身の名前で決める。
  // 1) 同じ form に type=submit が 1 つだけならそれ。
  // 2) 入力欄から祖先へ遡り、送信らしいボタン（type=submit か、名前が送信・send など）が 1 つだけ入った祖先があればそれ。
  //    チャット欄は添付・音声入力・送信などのボタンが並ぶため、個数だけでは決まらない。
  // 3) 送信らしいボタンが無ければ、最初にボタンを含んだ祖先にボタンが 1 つだけのときそれ。
  const buttons = kept.filter(item => item.buttonLike);
  // type 属性の無い button は form の外でも type が submit になるので、form に属するものだけ数える。
  const sendLike = b => !undoName.test(b.label) && ((b.el.form && b.el.type === 'submit') || sendName.test(b.label));
  const submitFor = field => {
    if (field.form) {
      const submits = buttons.filter(b => field.form.contains(b.el) && b.el.type === 'submit');
      if (submits.length === 1) return submits[0].id;
    }
    let lone = null;
    let node = field.el.parentElement;
    for (let depth = 0; node && node !== document.body && depth < MAX_SUBMIT_DEPTH; depth++) {
      const inside = buttons.filter(b => node.contains(b.el));
      const sends = inside.filter(sendLike);
      if (sends.length) return sends.length === 1 ? sends[0].id : '';
      if (lone === null && inside.length) lone = inside.length === 1 ? inside[0].id : '';
      if (node === field.form) break;
      node = node.parentElement;
    }
    return lone || '';
  };

  const elements = kept.map(item => {
    const el = item.el;
    const textLike = item.kind === 'type';
    return {
      id: item.id,
      role: item.role,
      name: item.label,
      kind: item.kind,
      inputType: item.inputType,
      autocomplete: item.autocomplete,
      disabled: el.matches(':disabled') || el.getAttribute('aria-disabled') === 'true',
      filled: textLike && Boolean(clean(el.isContentEditable ? el.innerText : el.value)),
      formId: item.form ? 'f' + (Array.from(document.forms).indexOf(item.form) + 1) : '',
      search: item.search,
      submitId: textLike ? submitFor(item) : '',
    };
  });
  return {
    url: location.href,
    title: document.title || '',
    excerpt: clip(pageText(), EXCERPT_LIMIT),
    feedback: clean(Array.from(document.querySelectorAll(
      '[role="status"], [role="alert"], [aria-live="polite"], [aria-live="assertive"]'
    )).filter(visible).map(el => el.innerText).join(' ')).slice(0, 300),
    elements,
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
        """値を入れて反映を確かめる。戻されたら打鍵で入れ直し、それでも違えば例外にする。"""
        locator = self._locator(element_id)
        await locator.fill(text, timeout=8000)
        await self._settle()
        if not await self._holds(locator, text):
            # React やリッチエディタは、値を直接入れても自分の状態で上書きし直すことがある。
            await locator.press("ControlOrMeta+a", timeout=8000)
            await locator.press("Backspace")
            await locator.press_sequentially(text, delay=TYPE_DELAY_MS)
            await self._settle()
            if not await self._holds(locator, text):
                raise RuntimeError("入力欄に指定した文字列が反映されませんでした。")
        if submit:
            await locator.press("Enter")
            await self._settle()

    async def press_key(self, element_id: str, key: str) -> None:
        """入力欄でキーを押す。送信ボタンの無いチャット欄の送信（Enter、Ctrl+Enter など）に使う。"""
        await self._locator(element_id).press(key, timeout=8000)
        await self._settle()

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

    @staticmethod
    async def _holds(locator: Any, text: str) -> bool:
        """入力欄の値が text と一致するか。リッチエディタの改行や空白の差は無視する。"""
        try:
            actual = await locator.evaluate(
                "el => el.isContentEditable ? el.innerText : el.value", timeout=2000
            )
        except Exception as e:
            raise RuntimeError("入力欄の値を確認できませんでした。") from e
        return " ".join(str(actual).split()) == " ".join(text.split())

    async def _settle(self) -> None:
        try:
            await self._page.wait_for_load_state("domcontentloaded", timeout=8000)
        except Exception:
            logger.debug("ページの再読み込み待ちがタイムアウトしました")
        await self._page.wait_for_timeout(300)
