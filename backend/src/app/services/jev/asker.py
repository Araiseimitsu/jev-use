"""TypeSafe Jev（System One）への問い合わせを担う共通部品。

判断ロジックは持たず、「型付き質問を 1 リクエストで投げ、回答とレイテンシを返す」ことだけを担う。
各判断器（task_assessor / browser_decider）が共有する。
"""

import time
from collections.abc import Callable
from typing import Any, Protocol

from app.core.config import settings

# 操作の選択に Jev が必須なため、キーが無いと実行を始めない（API・CLI・ランナーで共通）
MISSING_KEY_MESSAGE = "TypeSafe API キーが未設定です。backend/.env に TYPESAFE_API_KEY を設定してください。"


class _AsyncClient(Protocol):
    """テストで差し替えられるようにするための最小インターフェース。"""

    async def __aenter__(self) -> Any: ...

    async def __aexit__(self, *exc_info: object) -> None: ...

    async def system_one(self, **kwargs: Any) -> Any: ...


def _create_client(api_key: str, model: str, timeout: float) -> _AsyncClient:
    """TypeSafe の非同期クライアントを生成する。"""
    from typesafe_sdk import AsyncTypeSafeClient

    return AsyncTypeSafeClient(api_key=api_key, model=model, timeout=timeout)


class JevAsker:
    """TypeSafe Jev への問い合わせだけを担う薄いラッパー。"""

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        timeout: float | None = None,
        client_factory: Any = None,
    ) -> None:
        self.api_key = (api_key if api_key is not None else settings.typesafe_api_key).strip()
        self.model = model or settings.typesafe_model
        self.timeout = timeout or settings.typesafe_timeout_seconds
        self._client_factory = client_factory or _create_client

    @property
    def enabled(self) -> bool:
        """API キーが設定されているか。未設定なら各判断はフォールバックする。"""
        return bool(self.api_key)

    async def ask(self, state: Any, questions: dict[str, Any]) -> tuple[Any, int]:
        """型付き質問を 1 リクエストで投げ、(回答, レイテンシ ms) を返す。"""
        started = time.perf_counter()
        async with self._client_factory(self.api_key, self.model, self.timeout) as client:
            response = await client.system_one(state=state, questions=questions)
        return response, round((time.perf_counter() - started) * 1000)


async def ask_or_fallback[T](
    asker: JevAsker,
    state: Any,
    questions: dict[str, Any],
    interpret: Callable[[Any, int], T],
    fallback: Callable[[str], T],
    subject: str,
) -> T:
    """Jev に問い合わせて解釈する。未設定・通信失敗・解釈失敗はすべて fallback へ倒す。

    Jev はループを止めないための補助判断なので、失敗を理由に作業を止めない。
    理由は fallback の message として残し、UI / CLI に表示する。
    """
    if not asker.enabled:
        return fallback(f"TypeSafe API キーが未設定のため{subject}をスキップしました。")
    try:
        response, latency_ms = await asker.ask(state, questions)
    except Exception as e:
        return fallback(f"TypeSafe の{subject}に失敗しました（{type(e).__name__}）。")
    try:
        return interpret(response, latency_ms)
    except (KeyError, TypeError, ValueError, AttributeError) as e:
        return fallback(f"TypeSafe の{subject}結果を解釈できませんでした（{e}）。")


def probabilities_of(choice: Any) -> dict[str, float]:
    """Choice 回答の確率を float の辞書へ変換する。"""
    return {k: float(v) for k, v in dict(choice.probabilities).items()}


def model_of(response: Any) -> str:
    """回答したモデル名（バージョン）を取り出す。"""
    return str(getattr(response, "model", ""))
