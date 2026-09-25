"""ページの DOM から、Jev に渡す操作候補を作る。

座標は持たない。候補は要素の id と、クリックするか文字を入れるかだけ。
"""

import re
from dataclasses import asdict, dataclass
from urllib.parse import urlparse

# 次の操作を選ぶ Gemini に渡す要素の上限。制御操作（戻る等）は別に足す。
# ナビやボタンが多いページでも、一覧の行や送信ボタンが漏れにくい数にする。
MAX_CANDIDATES = 40
# Jev に渡す本文の上限（state を小さく保つ）
EXCERPT_FOR_JEV = 800
# 本文を切り詰めるとき先頭に残す文字数。残りは末尾から取る（チャットの最新の回答は末尾にある）。
EXCERPT_HEAD_FOR_JEV = 300

_URL_IN_TASK = re.compile(r"https?://[^\s<>\"']+")
_DEFAULT_START = "https://www.google.com"
_USER_LINE = re.compile(
    r"(?:ユーザー名|ユーザ名|username|user(?:name)?|login(?:\s*id)?)\s*[:：]\s*(\S+)",
    re.IGNORECASE,
)
_PASS_LINE = re.compile(
    r"(?:password|passwd|パスワード)\s*[:：]\s*(\S+)",
    re.IGNORECASE,
)
_USERNAME_HINTS = ("ユーザー", "ユーザ", "username", "user name", "login", "ログイン", "アカウント", "email", "e-mail", "メール")
_PASSWORD_HINTS = ("password", "passwd", "パスワード")
_SECRET_MASK = "［パスワード］"
# 入力欄の案内に書かれた送信キー（「Ctrl+Enter で送信」「Cmd+Enter to send」など）
_SEND_WORD = r"(?:で|to|for)?\s*(?:送信|投稿|send|post|submit)"
_CTRL_ENTER_SEND = re.compile(r"(?:ctrl|control|cmd|command|⌘)\s*\+\s*enter\s*" + _SEND_WORD, re.IGNORECASE)
_SHIFT_ENTER_SEND = re.compile(r"shift\s*\+\s*enter\s*" + _SEND_WORD, re.IGNORECASE)
# 修飾キーの付かない Enter が改行だと書かれているか
_ENTER_NEWLINE = re.compile(r"(?<![+\w])\s*enter\s*(?:で|to|for)?\s*(?:改行|new\s*line|line\s*break)", re.IGNORECASE)
_MIN_SECRET_LENGTH = 4

# サイトが人の操作を求めて出している確認画面。本文の偶然の一致を避けるため、文言は確認画面に特有なものだけにする。
_HUMAN_CHECK_PHRASES = (
    "あなたは人間ですか",
    "あなたはロボットではありません",
    "私はロボットではありません",
    "異常なトラフィック",
    "人であることを確認",
    "unusual traffic",
    "verify you are human",
    "are you a robot",
    "i'm not a robot",
    "checking your browser",
    "google.com/sorry",
    "recaptcha",
    "hcaptcha",
    "をすべて選択してください",
    "該当するものがない場合",
)

@dataclass(frozen=True)
class SelectChoice:
    """DOM 上の元の位置を保ったプルダウンの選択肢。"""

    index: int
    label: str
    value: str


@dataclass(frozen=True)
class Element:
    """クリックまたは入力できる要素。"""

    id: str
    role: str
    name: str
    kind: str
    input_type: str = ""
    autocomplete: str = ""
    disabled: bool = False
    filled: bool = False
    form_id: str = ""
    # 検索欄か（type=search、role=search の中など）。入力後に Enter で検索する。
    search: bool = False
    # 入力欄に対応する送信ボタンの id。ページの構造から決め、無ければ空。
    submit_id: str = ""
    # 同名の選択肢を区別するため、元の DOM index も保持する。
    choices: tuple[SelectChoice, ...] = ()

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        # value は実行前の照合にだけ使い、画面の観測イベントには載せない。
        data["choices"] = [{"index": choice.index, "label": choice.label} for choice in self.choices]
        return data


@dataclass(frozen=True)
class PageView:
    """1 回の読み取り結果。"""

    url: str
    title: str
    excerpt: str
    elements: tuple[Element, ...]
    feedback: str = ""
    reply: str = ""
    reply_busy: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "url": self.url,
            "title": self.title,
            "excerpt": self.excerpt,
            "elements": [element.to_dict() for element in self.elements],
        }


@dataclass(frozen=True)
class ActionOption:
    """Jev が選ぶ 1 操作。id は実行側がセレクタへ引き当てる。"""

    id: str
    description: str


CONTROL_ACTIONS: tuple[ActionOption, ...] = (
    ActionOption("back", "前のページに戻る"),
    ActionOption("wait", "何もせず、読み込みを待つ"),
    ActionOption("done", "指示は達成できたので操作を終える"),
)


def start_url(task: str) -> str:
    """指示に URL があればそこを開き、無ければ検索ページを開く。"""
    match = _URL_IN_TASK.search(task)
    if not match:
        return _DEFAULT_START
    return match.group(0).rstrip(".,);]")


def is_human_check(title: str, excerpt: str, url: str = "") -> bool:
    """「人間ですか？」のような確認画面か。Jev には進ませず、人がブラウザで済ませる。"""
    text = f"{title}\n{excerpt}\n{url}".casefold()
    return any(phrase.casefold() in text for phrase in _HUMAN_CHECK_PHRASES)


def is_allowed_url(url: str) -> bool:
    """ブラウザで開いてよい URL か。http と https だけを許可する。"""
    parsed = urlparse(url)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def elements_from_raw(raw: object) -> tuple[Element, ...]:
    """ブラウザが返した要素配列を Element にする。壊れた項目は捨てる。"""
    if not isinstance(raw, list):
        return ()
    elements: list[Element] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        kind = item.get("kind")
        element_id = str(item.get("id", "")).strip()
        name = str(item.get("name", "")).strip()
        if kind not in ("click", "type", "select") or not element_id or not name:
            continue
        role = str(item.get("role", "")).strip() or kind
        input_type = str(item.get("inputType") or item.get("input_type") or "").strip()
        autocomplete = str(item.get("autocomplete") or "").strip()
        disabled = item.get("disabled") is True
        filled = item.get("filled") is True
        form_id = str(item.get("formId") or "")
        search = item.get("search") is True
        submit_id = str(item.get("submitId") or "")
        raw_choices = item.get("choices")
        choices = tuple(
            SelectChoice(choice["index"], choice["label"].strip(), choice["value"])
            for choice in raw_choices
            if isinstance(choice, dict)
            and type(choice.get("index")) is int
            and choice["index"] >= 0
            and isinstance(choice.get("label"), str)
            and choice["label"].strip()
            and isinstance(choice.get("value"), str)
        ) if isinstance(raw_choices, list) else ()
        if kind == "select" and not choices:
            continue
        elements.append(
            Element(
                element_id, role, name, kind, input_type, autocomplete, disabled, filled, form_id,
                search, submit_id, choices,
            )
        )
    return tuple(elements)


def page_view_from_raw(raw: object) -> PageView:
    """ブラウザが返した JSON を PageView にする。"""
    data = raw if isinstance(raw, dict) else {}
    excerpt = " ".join(str(data.get("excerpt", "")).split())
    return PageView(
        url=str(data.get("url", "")),
        title=str(data.get("title", "")),
        excerpt=excerpt[:4000],
        elements=elements_from_raw(data.get("elements")),
        feedback=str(data.get("feedback", ""))[:300],
        reply=str(data.get("reply", ""))[:4000].strip(),
        reply_busy=bool(data.get("replyBusy", False)),
    )


def rank_elements(
    elements: tuple[Element, ...] | list[Element],
    task: str,
    limit: int = MAX_CANDIDATES,
    prefer: frozenset[str] = frozenset(),
) -> tuple[Element, ...]:
    """指示に関係しそうな要素を前に出す。同点ならページ上の順を保つ。

    prefer は入力済みの欄の送信ボタンなど、次に押す見込みが高い要素の id。
    """
    folded = task.casefold()
    scored: list[tuple[int, int, Element]] = []
    for index, element in enumerate(elements):
        if element.disabled:
            continue
        name = element.name.casefold()
        score = 3 if element.kind == "type" else 0
        if element.id in prefer:
            score += 4
        if name and name in folded:
            score += 5
        elif any(part and part in folded for part in name.split()):
            score += 2
        scored.append((score, index, element))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return tuple(item[2] for item in scored[:limit])


def needs_user_input(view: PageView) -> bool:
    """ログインなど、人の入力がないと先に進めないページか。"""
    if any(field_purpose(element) == "password" for element in view.elements):
        return True
    folded = f"{view.title}\n{view.url}".casefold()
    if not any(token in folded for token in ("login", "signin", "sign-in", "ログオン", "ログイン")):
        return False
    return any(field_purpose(element) == "username" for element in view.elements)


def _leave_to_user(element: Element, defer_login: bool) -> bool:
    """パスワード欄と、ログイン中のユーザー名欄は人が入れる。"""
    purpose = field_purpose(element)
    if purpose == "password":
        return True
    return defer_login and purpose == "username"


def action_catalog(
    elements: tuple[Element, ...] | list[Element], *, defer_login: bool = False, can_go_back: bool = True
) -> tuple[ActionOption, ...]:
    """要素と制御操作を候補にする。戻り先が無いページでは「戻る」を出さない（about:blank に出てしまう）。"""
    options: list[ActionOption] = []
    for element in elements:
        if element.disabled:
            continue
        if element.kind == "type":
            if _leave_to_user(element, defer_login):
                continue
            description = f"「{element.name}」（{field_kind(element)}）に、依頼に合う文字列を入力する"
            options.append(ActionOption(f"type:{element.id}", description))
        elif element.kind == "select":
            options.extend(
                ActionOption(
                    f"select:{element.id}#{choice.index}",
                    f"「{element.name}」（プルダウン）の {choice.index} 番「{choice.label}」を選ぶ",
                )
                for choice in element.choices
            )
        else:
            description = f"「{element.name}」（{element.role}）をクリックする"
            options.append(ActionOption(f"click:{element.id}", description))
    options.extend(option for option in CONTROL_ACTIONS if can_go_back or option.id != "back")
    return tuple(options)


def is_search_field(element: Element) -> bool:
    """検索欄か。ページの構造を優先し、名前の「検索」「search」でも補う。"""
    if element.kind != "type":
        return False
    name = element.name.casefold()
    return element.search or element.input_type == "search" or "検索" in name or "search" in name


def submits_on_enter(element: Element) -> bool:
    """検索欄なら入力後に Enter を押す。

    それ以外の欄では押さない。チャット欄の Enter は送信になり、送信ボタンの承認を経ずに送ってしまうため。
    """
    return is_search_field(element)


def send_key(element: Element) -> str | None:
    """送信ボタンの無い欄で、送信に使うキー。欄の名前や案内（placeholder）に書かれたものに従う。

    「Ctrl+Enter で送信 / Enter で改行」のような欄で Enter を押すと、送らずに改行してしまうため。
    Enter が改行だと書かれ、送信のキーが書かれていなければ None（キーでは送らない）。
    """
    name = element.name
    if _CTRL_ENTER_SEND.search(name):
        return "ControlOrMeta+Enter"
    if _SHIFT_ENTER_SEND.search(name):
        return "Shift+Enter"
    if _ENTER_NEWLINE.search(name):
        return None
    return "Enter"


def field_kind(element: Element) -> str:
    """Jev が欄を見分けるための種類。type と autocomplete から決める。"""
    auto = element.autocomplete.casefold()
    if is_search_field(element):
        return "検索欄"
    if element.input_type == "email" or "email" in auto:
        return "メールアドレス欄"
    if element.input_type == "tel" or auto.startswith("tel"):
        return "電話番号欄"
    if element.input_type == "url" or auto == "url":
        return "URL 欄"
    if element.input_type == "number":
        return "数値欄"
    if auto in {"name", "given-name", "family-name", "nickname"}:
        return "名前欄"
    if element.role in {"textarea", "textbox"} and not element.input_type:
        return "文章の入力欄"
    return "1 行の入力欄"


def jev_excerpt(view: PageView) -> str:
    """Jev に渡す本文。長すぎると判断が遅くなるため、先頭と末尾を残して切る。"""
    text = view.excerpt
    if len(text) <= EXCERPT_FOR_JEV:
        return text
    tail = EXCERPT_FOR_JEV - EXCERPT_HEAD_FOR_JEV - len(" … ")
    return f"{text[:EXCERPT_HEAD_FOR_JEV]} … {text[-tail:]}"


def field_purpose(element: Element) -> str:
    """入力欄がユーザー名か、パスワードか、それ以外か。"""
    auto = element.autocomplete.casefold()
    if element.input_type == "password" or "password" in auto:
        return "password"
    hay = f"{element.name} {element.role}".casefold()
    if any(hint.casefold() in hay for hint in _PASSWORD_HINTS):
        return "password"
    if auto in {"username", "email"} or any(hint.casefold() in hay for hint in _USERNAME_HINTS):
        return "username"
    return "text"


def request_line(task: str) -> str:
    """ログイン情報と URL を除いた、最後の依頼文。"""
    lines: list[str] = []
    for raw in task.splitlines():
        line = raw.strip().lstrip("-*").strip()
        if not line or line.startswith("#") or line.startswith("http"):
            continue
        if _USER_LINE.search(raw) or _PASS_LINE.search(raw):
            continue
        lines.append(line)
    return lines[-1][:200] if lines else ""


def redact_secrets(task: str) -> str:
    """モデルへ渡す文から、指示に書かれたパスワードの実値を消す。"""
    secrets = {match.group(1) for match in _PASS_LINE.finditer(task)}
    if not secrets:
        return task
    # パスワード行の値は長さに関わらず消す（値は正規表現の末尾にある）
    redacted = _PASS_LINE.sub(
        lambda m: m.group(0)[: m.start(1) - m.start(0)] + _SECRET_MASK, task
    )
    # 同じ値が別の行に書かれていても消す。短い値は URL などを壊すため対象外にする。
    for secret in sorted(secrets, key=len, reverse=True):
        if len(secret) >= _MIN_SECRET_LENGTH:
            redacted = redacted.replace(secret, _SECRET_MASK)
    return redacted
