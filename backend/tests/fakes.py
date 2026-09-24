"""テスト用のフェイク（実 API・実画面を使わずに判断ロジックを検証する）。"""

from types import SimpleNamespace
from typing import Any

from app.services.jev.asker import JevAsker


def choice(name: str, confidence: float = 0.9, options: tuple[str, ...] = ()) -> Any:
    """Jev の Choice 回答を模す。"""
    probabilities = {option: 0.0 for option in options}
    probabilities[name] = confidence
    return SimpleNamespace(choice=name, confidence=confidence, probabilities=probabilities)


def noul(value: float) -> Any:
    return SimpleNamespace(noul=value)


def jev_response(
    choices: dict[str, Any] | None = None,
    nouls: dict[str, float] | None = None,
    scores: dict[str, float] | None = None,
    score_probabilities: dict[str, dict[int, float]] | None = None,
    model: str = "jev-1.13.0",
) -> Any:
    """SystemOneResponse の形を模した最小オブジェクト。"""
    return SimpleNamespace(
        model=model,
        choices=choices or {},
        nouls={k: noul(v) for k, v in (nouls or {}).items()},
        scores={
            k: SimpleNamespace(
                score=v,
                confidence=0.9,
                probabilities=(score_probabilities or {}).get(k, {max(0, min(round(v), 3)): 1.0}),
            )
            for k, v in (scores or {}).items()
        },
    )


class FakeClient:
    """AsyncTypeSafeClient の代わりに固定レスポンス（または例外）を返す。

    responses に複数渡すと呼び出し順に返す（最後の 1 件はその後も返し続ける）。
    """

    def __init__(self, responses: list[Any], error: Exception | None, calls: list[dict[str, Any]]) -> None:
        self.responses = responses
        self.error = error
        self.calls = calls

    async def __aenter__(self) -> "FakeClient":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    async def system_one(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        index = min(len(self.calls) - 1, len(self.responses) - 1)
        return self.responses[index]


def fake_asker(
    *responses: Any, error: Exception | None = None
) -> tuple[JevAsker, list[dict[str, Any]]]:
    """フェイククライアントを使う JevAsker と、送信内容の記録を返す。"""
    calls: list[dict[str, Any]] = []
    asker = JevAsker(
        api_key="test-key",
        client_factory=lambda *args: FakeClient(list(responses), error, calls),
    )
    return asker, calls


def disabled_asker() -> JevAsker:
    """キー未設定（判断をスキップする）JevAsker。"""
    return JevAsker(api_key="")
