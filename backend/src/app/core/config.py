"""アプリ設定。

環境変数（`backend/.env`）で上書きできる。実値は Git 管理しない。
"""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/src/app/core/config.py から backend/ とプロジェクトルートを求める
BACKEND_DIR = Path(__file__).resolve().parents[3]
PROJECT_ROOT = BACKEND_DIR.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=BACKEND_DIR / ".env", extra="ignore")

    app_env: str = "development"
    frontend_dir: Path = PROJECT_ROOT / "frontend"

    gemini_api_key: str = ""
    # 次の操作と入力文の選択、最終回答の文章に使う（画像は送らない）
    gemini_model: str = "gemini-3.5-flash-lite"
    default_max_steps: int = 15
    # ブラウザを表示せずに操作するか
    browser_headless: bool = False
    # サイトの「人間ですか？」を人が済ませたあとのクッキーを残す場所
    browser_profile_dir: Path = BACKEND_DIR / ".browser-profile"
    # その確認を待つ上限（秒）。超えたら止める
    human_check_timeout_seconds: float = 180.0

    # TypeSafe AI（System One モデル Jev）による操作の危険度の判定と実行前の見積もり
    typesafe_api_key: str = ""
    typesafe_model: str = "jev-latest"
    typesafe_timeout_seconds: float = 10.0
    # この確率以上の操作は、人間の確認がなければ実行しない
    typesafe_risk_threshold: float = 0.7
    # 人間の承認を待つ上限（秒）。超えたら実行しない
    approval_timeout_seconds: float = 120.0

    # タスク終了時に Windows の標準通知を出すか
    enable_notifications: bool = True


settings = Settings()