"""FastAPI アプリの入口。

API は `/api` 配下、画面ファイルは `StaticFiles` で `/` に配信する（単一サーバー）。
"""

import asyncio
import sys

if sys.platform == "win32":
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    except Exception:
        pass

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api.routes import router
from app.core.config import settings


def create_app() -> FastAPI:
    app = FastAPI(title="app", version="0.1.0")
    app.include_router(router, prefix="/api")
    # ルートのマウントは API ルートより後に定義する
    app.mount(
        "/",
        StaticFiles(directory=settings.frontend_dir, html=True),
        name="frontend",
    )
    return app


app = create_app()