"""アプリの入口と API のテスト。"""

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health_returns_ok() -> None:
    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_config_returns_browser_settings() -> None:
    response = client.get("/api/config")

    assert response.status_code == 200
    assert set(response.json()) == {
        "default_max_steps",
        "typesafe_enabled",
        "text_enabled",
        "headless",
    }


@pytest.mark.parametrize(
    "path",
    ["/api/desktop/run", "/api/desktop/assess", "/api/computer/run", "/api/route"],
)
def test_removed_desktop_endpoints_are_gone(path: str) -> None:
    response = client.post(path, json={"task": "メモ帳を開く"})

    assert response.status_code in (404, 405)


def test_browser_run_requires_typesafe_key(monkeypatch) -> None:
    from app.core.config import settings

    monkeypatch.setattr(settings, "typesafe_api_key", "")

    response = client.post("/api/browser/run", json={"task": "天気を調べる"})

    assert response.status_code == 400
    assert "TYPESAFE_API_KEY" in response.json()["detail"]


def test_browser_run_rejects_too_many_steps() -> None:
    response = client.post("/api/browser/run", json={"task": "天気を調べる", "max_steps": 51})

    assert response.status_code == 422


def test_assess_requires_task() -> None:
    response = client.post("/api/browser/assess", json={"task": "  "})

    assert response.status_code == 400


def test_assess_returns_jev_assessment(monkeypatch) -> None:
    from app.api import routes
    from tests.fakes import fake_asker, jev_response

    asker, _ = fake_asker(
        jev_response(
            nouls={"risk": 0.9, "clarity": 0.9, "feasibility": 0.9}, scores={"steps": 0.0}
        )
    )
    real_assessor = routes.TaskAssessor
    monkeypatch.setattr(routes, "TaskAssessor", lambda: real_assessor(asker))

    response = client.post("/api/browser/assess", json={"task": "注文を確定して"})

    assert response.status_code == 200
    data = response.json()
    assert data["requires_confirmation"] is True
    assert data["suggested_max_steps"] == 3
    assert data["reasons"]


def test_root_serves_index_html() -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "ブラウザに調べものを任せる" in response.text
