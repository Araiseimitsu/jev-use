"""開始前の見積り：指示を実行してよいか・何ステップ必要かを Jev に 1 回で問う。"""

from dataclasses import asdict, dataclass, field
from typing import Any

from app.core.config import settings
from app.services.jev.asker import JevAsker, ask_or_fallback, model_of

#: 必要ステップ数の見積りレベル（Score の criteria。0 起点）
STEP_SCALE: tuple[str, ...] = (
    "1〜3 ステップ",
    "4〜8 ステップ",
    "9〜15 ステップ",
    "16 ステップ以上",
)

#: 各レベルに対する最大ステップ数の提案値
STEP_SCALE_TO_MAX_STEPS: dict[int, int] = {0: 3, 1: 8, 2: 15, 3: 25}
# 長い作業の可能性を平均値で消さないため、この割合を含む段階まで確保する。
STEP_COVERAGE = 0.8

#: API の max_steps 上限（routes のバリデーションと揃える）
MAX_STEPS_LIMIT = 50

#: 指示が具体的・実行可能と言える確率の下限。下回れば実行前に確認する
CLARITY_THRESHOLD = 0.3
FEASIBILITY_THRESHOLD = 0.3


@dataclass(frozen=True)
class TaskAssessment:
    """開始前に Jev が下した見積り。"""

    risk_probability: float = 0.0
    clarity_probability: float = 1.0
    feasibility_probability: float = 1.0
    suggested_max_steps: int = 0
    requires_confirmation: bool = False
    #: 確認が必要な理由（UI / CLI にそのまま表示する）
    reasons: list[str] = field(default_factory=list)
    model: str = ""
    latency_ms: int = 0
    used_fallback: bool = False
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_task_questions() -> dict[str, Any]:
    """開始前に問う型付き質問（noul × 3 と score）を組み立てる。"""
    from typesafe_sdk import Noul, Score

    return {
        "risk": Noul(
            instructions=(
                "この指示を Web ブラウザで実行すると、元に戻せない変更や外部への影響"
                "（送信、購入、契約、削除、ログイン、公開範囲の変更）が発生する可能性が高いか。"
            ),
            criteria={
                "true": "取り消しが難しい操作、または外部に影響する操作を含む",
                "false": "アプリの起動・閲覧・下書きの入力など、安全に取り消せる操作のみ",
            },
        ),
        "clarity": Noul(
            instructions="この指示は、追加の質問をせずに実行に移せるほど具体的か。",
            criteria={
                "true": "対象と完了状態が読み取れる",
                "false": "対象や完了状態が曖昧で、人によって解釈が分かれる",
            },
        ),
        "feasibility": Noul(
            instructions=(
                "この指示は、Web ブラウザでページを開き、リンクやボタンや入力欄を操作するだけで完了できるか。"
            ),
            criteria={
                "true": "ブラウザの中の操作で完結する",
                "false": "デスクトップアプリ、物理的な作業、ブラウザの外の手続きが必要",
            },
        ),
        "steps": Score(
            instructions="この指示を完了するには何ステップ程度の操作が必要か。",
            criteria=list(STEP_SCALE),
        ),
    }


def clamp_max_steps(value: int) -> int:
    """ステップ数の提案値を API の許容範囲へ収める。"""
    return max(1, min(value, MAX_STEPS_LIMIT))


def suggested_steps(probabilities: dict[int, float]) -> int:
    """Score の分布で少なくとも STEP_COVERAGE を含む段階の上限を返す。"""
    weights = [float(probabilities.get(index, 0.0)) for index in range(len(STEP_SCALE))]
    if any(weight < 0 or not 0 <= weight <= 1 for weight in weights):
        raise ValueError("ステップ数の確率が不正です")
    total = sum(weights)
    if not 0.99 <= total <= 1.01:
        raise ValueError("ステップ数の確率の合計が不正です")
    cumulative = 0.0
    for index, weight in enumerate(weights):
        cumulative += weight / total
        if cumulative >= STEP_COVERAGE:
            return clamp_max_steps(STEP_SCALE_TO_MAX_STEPS[index])
    return clamp_max_steps(STEP_SCALE_TO_MAX_STEPS[len(STEP_SCALE) - 1])


def interpret_assessment(response: Any, latency_ms: int) -> TaskAssessment:
    """Jev の回答を TaskAssessment へ変換する。

    Raises:
        KeyError: 期待した質問の回答が欠けている場合（呼び出し側でフォールバックする）。
    """
    risk = float(response.nouls["risk"].noul)
    clarity = float(response.nouls["clarity"].noul)
    feasibility = float(response.nouls["feasibility"].noul)
    max_steps = suggested_steps(response.scores["steps"].probabilities)

    reasons = []
    if risk >= settings.typesafe_risk_threshold:
        reasons.append(f"取り消しにくい操作を含む可能性が {risk:.0%} と判断されました。")
    if clarity < CLARITY_THRESHOLD:
        reasons.append(
            f"指示が具体的である確率が {clarity:.0%} と低く、意図と違う操作になるおそれがあります。"
        )
    if feasibility < FEASIBILITY_THRESHOLD:
        reasons.append(
            f"ブラウザ操作だけで完了できる確率が {feasibility:.0%} と判断されました。"
        )

    return TaskAssessment(
        risk_probability=risk,
        clarity_probability=clarity,
        feasibility_probability=feasibility,
        suggested_max_steps=max_steps,
        requires_confirmation=bool(reasons),
        reasons=reasons,
        model=model_of(response),
        latency_ms=latency_ms,
    )


class TaskAssessor:
    """開始前の見積りを行う判断器。"""

    def __init__(self, asker: JevAsker | None = None) -> None:
        self.asker = asker or JevAsker()

    @property
    def enabled(self) -> bool:
        return self.asker.enabled

    async def assess(self, task: str) -> TaskAssessment:
        """指示を評価して見積りを返す。失敗しても例外は投げず、確認なしで既定値を返す。"""
        return await ask_or_fallback(
            self.asker,
            state=task,
            questions=build_task_questions(),
            interpret=interpret_assessment,
            fallback=self._fallback,
            subject="事前判断",
        )

    @staticmethod
    def _fallback(message: str) -> TaskAssessment:
        return TaskAssessment(
            suggested_max_steps=settings.default_max_steps,
            used_fallback=True,
            message=message,
        )
