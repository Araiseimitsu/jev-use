"""実ブラウザで入力欄への反映を確認する。"""

import asyncio

import pytest
from playwright.async_api import async_playwright

from app.services.browser_session import BrowserSession
from app.services.page_state import SelectChoice


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
        assert len(view.excerpt) <= 4000

    _with_page(html, check)


def test_read_separates_latest_gemini_reply_from_question() -> None:
    html = (
        '<main><p>送信した質問</p>'
        '<model-response>以前の回答</model-response>'
        '<model-response aria-busy="true">新しい回答の途中</model-response></main>'
    )

    async def check(session: BrowserSession, page) -> None:
        view = await session.read()
        assert view.reply == "新しい回答の途中"
        assert view.reply_busy is True
        await page.locator("model-response").last.evaluate(
            "el => { el.textContent = '新しい回答の全文'; el.removeAttribute('aria-busy'); }"
        )
        view = await session.read()
        assert view.reply == "新しい回答の全文"
        assert view.reply_busy is False

    _with_page(html, check)


def test_read_finds_clickable_rows_without_link_or_button_tags() -> None:
    """div や li で作られた一覧の行も、押せる見た目や onclick があれば候補にする。"""
    html = (
        '<ul><li style="cursor:pointer"><span>test ルーム</span><span>3 件</span></li>'
        '<li onclick="void 0">営業部</li><li>押せない行</li></ul>'
        '<div role="option">候補A</div>'
        '<button style="cursor:pointer"><span style="cursor:pointer">送信</span></button>'
        '<div style="cursor:pointer"><a href="/x">中のリンク</a></div>'
    )

    async def check(session: BrowserSession, page) -> None:
        view = await session.read()
        names = [element.name for element in view.elements]
        assert any(name.startswith("test ルーム") for name in names)
        assert "営業部" in names
        assert "候補A" in names
        assert "押せない行" not in names
        # ボタンの中の span や、リンクを包む div を二重に数えない。
        assert names.count("送信") == 1
        assert names.count("中のリンク") == 1
        assert len(names) == 5
        row = next(element for element in view.elements if element.name == "営業部")
        assert row.kind == "click"
        await session.click(row.id)

    _with_page(html, check)


def test_icon_only_buttons_are_named_and_become_the_send_button() -> None:
    """文字も aria-label も無いアイコンのボタンも候補にし、チャット欄の送信ボタンにする。"""
    html = (
        '<input placeholder="ルームを検索...">'
        '<div class="composer"><textarea placeholder="メッセージを入力"></textarea>'
        '<button class="icon"><svg width="20" height="20"><path d="M0 0L20 10L0 20z"/></svg></button></div>'
        '<div class="toolbar"><button class="btn-send-message"><svg width="20" height="20"></svg></button></div>'
        '<button><svg width="20" height="20"><title>Attach</title></svg></button>'
    )

    async def check(session: BrowserSession, page) -> None:
        view = await session.read()
        by_name = {element.name: element for element in view.elements}
        assert by_name["メッセージを入力"].submit_id == by_name["アイコンのボタン"].id
        assert "送信（アイコン）" in by_name
        assert "Attach" in by_name

    _with_page(html, check)


def test_names_keep_spaces_between_inline_parts() -> None:
    """アイコンの頭文字と名前のように並んだ span は、つなげず空白で区切る（「ttest」にしない）。"""
    html = (
        '<div class="room" style="cursor:pointer"><span>t</span><span>test</span></div>'
        '<a href="/r"><span>営</span><span>営業部</span></a>'
        '<button><span style="display:none">隠し</span><span>送信</span></button>'
    )

    async def check(session: BrowserSession, page) -> None:
        names = [element.name for element in (await session.read()).elements]
        assert names == ["t test", "営 営業部", "送信"]

    _with_page(html, check)


def test_clear_button_is_not_taken_for_the_send_button() -> None:
    """「検索語をクリア」「キャンセル」などは、名前に検索や送信を含んでも送信ボタンにしない。"""
    html = (
        '<aside><input type="search" placeholder="ルームを検索...">'
        '<button aria-label="検索語をクリア">×</button><button>1対1チャットを開始</button></aside>'
        '<div class="composer"><textarea placeholder="メッセージ"></textarea>'
        '<button>送信をキャンセル</button><button>送信</button></div>'
    )

    async def check(session: BrowserSession, page) -> None:
        by_name = {element.name: element for element in (await session.read()).elements}
        assert by_name["ルームを検索..."].submit_id != by_name["検索語をクリア"].id
        assert by_name["メッセージ"].submit_id == by_name["送信"].id

    _with_page(html, check)


def test_excerpt_without_main_skips_header_and_footer() -> None:
    menu = "".join(f"<a href='#'>カテゴリ{i}</a>" for i in range(200))
    html = (
        f"<header>{menu}</header><div><p>Dell XPS 13 ￥31,800</p>"
        f"<p style='display:none'>隠れた文</p><select><option>価格: 安い順</option></select></div>"
        f"<footer>{menu}</footer>"
    )

    async def check(session: BrowserSession, page) -> None:
        view = await session.read()
        assert "￥31,800" in view.excerpt
        assert "カテゴリ" not in view.excerpt
        assert "隠れた文" not in view.excerpt
        select = next(element for element in view.elements if element.kind == "select")
        assert select.choices == (SelectChoice(0, "価格: 安い順", "価格: 安い順"),)

    _with_page(html, check)


def test_select_uses_original_index_and_checks_stale_options() -> None:
    regions = "".join(f'<option value="r{i}">地域{i}</option>' for i in range(47))
    html = (
        '<select aria-label="地域"><option value="blank"></option>'
        '<option disabled>利用不可</option><option value="a">その他</option>'
        f'<option value="b">その他</option>{regions}</select>'
    )

    async def check(session: BrowserSession, page) -> None:
        field = next(element for element in (await session.read()).elements if element.kind == "select")
        assert [choice.index for choice in field.choices[:2]] == [2, 3]
        assert field.choices[-1].index == 50

        await session.select(field.id, 3, "その他", "b")
        assert await page.locator("select").input_value() == "b"

        await page.locator("select").evaluate("el => { el.options[3].label = '変更済み'; }")
        with pytest.raises(RuntimeError, match="選択肢が変わりました"):
            await session.select(field.id, 3, "その他", "b")

        await session.select(field.id, 50, "地域46", "r46")
        assert await page.locator("select").input_value() == "r46"

    _with_page(html, check)
