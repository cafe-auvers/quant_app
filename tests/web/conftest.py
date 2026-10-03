from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.web.api import build_services, create_api_app
from src.web.config import WebConfig


@pytest.fixture(autouse=True)
def disable_real_pc_change_notifications(monkeypatch):
    """Keep web unit tests from contacting a configured deployment PC."""

    from src.services import pc_remote_control

    monkeypatch.setattr(
        pc_remote_control,
        "notify_pc_coordination_change",
        lambda *_args, **_kwargs: False,
    )


@pytest.fixture
def web_config(tmp_path):
    return WebConfig(
        data_dir=tmp_path / "web",
        watchlist_history_path=str(tmp_path / "watchlist.json"),
    )


@pytest.fixture
def services(web_config):
    value = build_services(web_config)
    value.auth.bootstrap_user("owner", "correct horse battery staple")
    return value


@pytest.fixture
def client(web_config, services):
    with TestClient(create_api_app(web_config, services=services), base_url="http://localhost:8080") as value:
        yield value


def login(client: TestClient) -> dict:
    csrf = client.get("/api/v1/auth/csrf").json()["csrf_token"]
    response = client.post(
        "/api/v1/auth/login",
        headers={"Origin": "http://localhost:8080", "X-CSRF-Token": csrf},
        json={"username": "owner", "password": "correct horse battery staple"},
    )
    assert response.status_code == 200, response.text
    return response.json()


@pytest.fixture
def authenticated(client):
    session = login(client)
    client.headers.update(
        {"Origin": "http://localhost:8080", "X-CSRF-Token": session["csrf_token"]}
    )
    return client
