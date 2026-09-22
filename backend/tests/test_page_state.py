"""ページ状態から操作候補を作る処理のテスト。"""

from app.services.page_state import (
    Element,
    action_catalog,
    credential_value,
    credentials_from_task,
    elements_from_raw,
    field_purpose,
    is_allowed_url,
    is_human_check,
    rank_elements,
    redact_secrets,
    request_line,
    start_url,
    submits_on_enter,
)


def test_start_url_uses_link_in_task_or_search_page() -> None:
    assert start_url("https://example.com/docs を開いて。") == "https://example.com/docs"
    assert start_url("東京の天気を調べて") == "https://www.google.com"


def test_human_check_matches_the_challenge_page_only() -> None:
    assert is_human_check("あなたは人間ですか？", "私はロボットではありません", "") is True
    assert is_human_check("", "", "https://www.google.com/sorry/index") is True
    assert is_human_check("", "車が表示されているタイルをすべて選択してください", "") is True
    assert is_human_check("ロボット工学", "工場ではロボットが組み立てる", "https://ja.wikipedia.org") is False


def test_only_http_urls_are_allowed() -> None:
    assert is_allowed_url("https://example.com") is True
    assert is_allowed_url("file:///c:/secret") is False
    assert is_allowed_url("javascript:alert(1)") is False


def test_elements_drop_incomplete_rows_and_have_no_coordinates() -> None:
    elements = elements_from_raw(
        [
            {"id": "e1", "role": "link", "name": "天気", "kind": "click"},
            {"id": "", "name": "空", "kind": "click"},
            {"role": "button", "name": "送信", "kind": "click"},
        ]
    )

    assert [element.id for element in elements] == ["e1"]
    assert "x" not in elements[0].to_dict()
    assert "y" not in elements[0].to_dict()


def test_rank_prefers_named_match_then_inputs() -> None:
    elements = (
        Element("e1", "link", "ヘルプ", "click"),
        Element("e2", "searchbox", "検索", "type"),
        Element("e3", "link", "天気", "click"),
    )

    ranked = rank_elements(elements, "明日の天気を調べて")

    assert [element.id for element in ranked] == ["e3", "e2", "e1"]


def test_catalog_uses_element_ids_and_control_actions() -> None:
    catalog = action_catalog((Element("e1", "button", "発注", "click"),))
    ids = [option.id for option in catalog]

    assert ids == ["click:e1", "scroll_down", "back", "wait", "done"]
    assert all("x" not in option.id for option in catalog)


def test_search_field_submits_on_enter() -> None:
    assert submits_on_enter(Element("e1", "searchbox", "検索", "type")) is True
    assert submits_on_enter(Element("e2", "link", "検索", "click")) is False


def test_login_lines_stay_out_of_the_request() -> None:
    task = (
        "https://example.com/login\n"
        "ユーザー名: alice\n"
        "パスワード: secret-value\n"
        "- スケジュールが面で今日のすべての予定を教えて"
    )

    assert credentials_from_task(task) == ("alice", "secret-value")
    assert request_line(task) == "スケジュールが面で今日のすべての予定を教えて"
    assert "secret-value" not in redact_secrets(task, "")
    assert field_purpose(Element("e1", "textbox", "ユーザー名", "type")) == "username"
    assert field_purpose(Element("e2", "textbox", "パスワード", "type", "password")) == "password"
    assert credential_value(Element("e1", "textbox", "ユーザー名", "type"), task, "ui-user", "") == "ui-user"
    assert (
        credential_value(Element("e2", "textbox", "", "type", "password"), task, "", "ui-pass")
        == "ui-pass"
    )
    assert credential_value(Element("e3", "searchbox", "検索", "type"), task, "ui-user", "ui-pass") is None


def test_password_input_type_is_kept() -> None:
    elements = elements_from_raw(
        [
            {
                "id": "e1",
                "role": "textbox",
                "name": "入力欄",
                "kind": "type",
                "inputType": "",
                "autocomplete": "username",
            }
        ]
    )

    assert elements[0].autocomplete == "username"
    assert field_purpose(elements[0]) == "username"
    assert credential_value(elements[0], "予定を教えて", "alice", "secret") == "alice"


def test_bullet_login_template_separates_the_request() -> None:
    task = (
        "https://example.com/\n\n"
        "## ログイン情報\n"
        "- ユーザー名: alice\n"
        "- password: secret-value\n\n"
        "- スケジュールが面で今日のすべての予定を教えて"
    )

    assert credentials_from_task(task) == ("alice", "secret-value")
    assert request_line(task) == "スケジュールが面で今日のすべての予定を教えて"
