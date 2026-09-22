"""API ルート（ブラウザ操作専用）。"""

import json
import uuid
from collections.abc import AsyncGenerator
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from app.core.config import settings
from app.services.browser_agent import BrowserAgentRunner
from app.services.jev.task_assessor import MAX_STEPS_LIMIT, TaskAssessor
from app.services.notifier import notify_browser_event
from app.services.page_state import redact_secrets

router = APIRouter()

# 実行中（停止・承認受付中）のランナー
ACTIVE_RUNS: dict[str, BrowserAgentRunner] = {}


class RunRequest(BaseModel):
    task: str = Field(..., description="実行する指示")
    max_steps: int | None = Field(default=None, ge=1, le=MAX_STEPS_LIMIT, description="最大ステップ数")
    headless: bool | None = Field(
        default=None, description="true で表示しない / false で表示する。未指定なら設定値"
    )


class AssessRequest(BaseModel):
    task: str = Field(..., description="見積もる指示")


class ApprovalRequest(BaseModel):
    granted: bool = Field(..., description="承認する場合 true / 拒否する場合 false")


class ConfigResponse(BaseModel):
    default_max_steps: int
    typesafe_enabled: bool
    text_enabled: bool
    headless: bool


def _require_task(task: str) -> str:
    cleaned = task.strip()
    if not cleaned:
        raise HTTPException(status_code=400, detail="タスクを入力してください。")
    return cleaned


def _require_typesafe() -> None:
    if not settings.typesafe_api_key.strip():
        raise HTTPException(
            status_code=400,
            detail="TypeSafe API キーが未設定です。backend/.env に TYPESAFE_API_KEY を設定してください。",
        )


def _to_sse(event: dict[str, Any]) -> dict[str, str]:
    return {"event": event["event"], "data": json.dumps(event["data"], ensure_ascii=False)}


def _notify_if_finished(event: dict[str, Any], task: str, mode: str) -> None:
    notify_browser_event(event, task, mode)


def _active_runner(run_id: str) -> BrowserAgentRunner:
    runner = ACTIVE_RUNS.get(run_id)
    if runner is None:
        raise HTTPException(status_code=404, detail="実行中のブラウザ操作が見つかりません。")
    return runner


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/config", response_model=ConfigResponse)
def get_config() -> ConfigResponse:
    return ConfigResponse(
        default_max_steps=settings.default_max_steps,
        typesafe_enabled=TaskAssessor().enabled,
        text_enabled=bool(settings.gemini_api_key.strip()),
        headless=settings.browser_headless,
    )


@router.post("/browser/assess")
async def assess_browser_task(request: AssessRequest) -> dict[str, object]:
    """実行前に危険度・具体性・実現性・ステップ数を見積もる。"""
    task = _require_task(request.task)
    assessment = await TaskAssessor().assess(redact_secrets(task))
    return assessment.to_dict()


@router.post("/browser/run")
async def run_browser_task(request: RunRequest) -> EventSourceResponse:
    """ページの要素を読んで Jev が操作を選び、Playwright が実行する。"""
    task = _require_task(request.task)
    _require_typesafe()

    run_id = uuid.uuid4().hex
    runner = BrowserAgentRunner(
        task=task,
        max_steps=request.max_steps,
        headless=request.headless,
    )
    ACTIVE_RUNS[run_id] = runner
    visible_task = redact_secrets(task)

    async def event_generator() -> AsyncGenerator[dict[str, str], None]:
        try:
            async for event in runner.run_stream():
                data = dict(event["data"])
                if event["event"] == "start":
                    data["run_id"] = run_id
                enriched = {"event": event["event"], "data": data}
                _notify_if_finished(enriched, visible_task, runner.mode)
                yield _to_sse(enriched)
        finally:
            ACTIVE_RUNS.pop(run_id, None)

    return EventSourceResponse(event_generator())


@router.post("/browser/stop/{run_id}")
def stop_browser_task(run_id: str) -> dict[str, str]:
    """実行中の操作に停止要求を出す。"""
    _active_runner(run_id).request_stop()
    return {"status": "stopping", "run_id": run_id}


@router.post("/browser/approve/{run_id}")
def approve_browser_action(run_id: str, request: ApprovalRequest) -> dict[str, object]:
    """危険と判断された操作を、承認または拒否する。"""
    _active_runner(run_id).resolve_approval(request.granted)
    return {"status": "granted" if request.granted else "denied", "run_id": run_id}


@router.post("/browser/human-ready/{run_id}")
def mark_browser_human_ready(run_id: str) -> dict[str, str]:
    """確認や手入力が終わったと伝え、待ちを終わらせる。"""
    _active_runner(run_id).mark_human_ready()
    return {"status": "ready", "run_id": run_id}
