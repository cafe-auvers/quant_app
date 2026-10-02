from __future__ import annotations

from dataclasses import replace
import uuid

from fastapi.testclient import TestClient
import pytest

from src.web.api import build_services, create_api_app
from src.web.config import WebConfigError, load_web_config

from .conftest import login


def test_reads_require_authentication(client):
    response = client.get("/api/v1/scanner")
    assert response.status_code == 401


def test_host_and_origin_are_validated(client):
    assert client.get("/api/v1/auth/csrf", headers={"Host": "evil.example"}).status_code == 400
    response = client.post(
        "/api/v1/auth/login",
        headers={"Origin": "https://evil.example"},
        json={"username": "owner", "password": "correct horse battery staple"},
    )
    assert response.status_code == 400


def test_bracketed_ipv6_loopback_host_is_supported(web_config):
    ipv6_config = replace(
        web_config, trusted_hosts=("localhost", "127.0.0.1", "::1")
    )
    services = build_services(ipv6_config)
    with TestClient(
        create_api_app(ipv6_config, services=services),
        base_url="http://localhost:8080",
    ) as ipv6_client:
        assert (
            ipv6_client.get(
                "/api/v1/auth/csrf", headers={"Host": "[::1]:8080"}
            ).status_code
            == 200
        )


def test_login_uses_csrf_and_server_side_session(client):
    missing = client.post(
        "/api/v1/auth/login",
        headers={"Origin": "http://localhost:8080"},
        json={"username": "owner", "password": "correct horse battery staple"},
    )
    assert missing.status_code == 403
    result = login(client)
    assert result["csrf_token"]
    session = client.get("/api/v1/session")
    assert session.status_code == 200
    assert session.json()["mode"] == "SANDBOX"
    assert session.json()["sync_state"] == "NOT SYNCED TO EXECUTOR"
    assert session.json()["market_status"]["state"] in {"OPEN", "CLOSED"}
    assert "quant_web_session" in client.cookies


def test_status_includes_current_market_phase(authenticated):
    response = authenticated.get("/api/v1/status")

    assert response.status_code == 200
    market = response.json()["market_status"]
    assert market["label"].startswith("Market Status: ")
    assert market["compact_label"].startswith("Market ")
    assert market["timezone"] == "America/New_York"
    assert market["watchlist_session_date"]
    assert response.json()["watchlist_rollover"]["session_date"] == market["watchlist_session_date"]


def test_status_heartbeat_rolls_watchlist_and_reports_the_new_session(
    authenticated, monkeypatch
):
    import src.web.api as web_api

    session_date = {"value": "2026-10-02"}

    def fake_status():
        return {
            "state": "CLOSED",
            "phase": "AFTER_HOURS",
            "label": "Market Status: Closed (After Hours)",
            "compact_label": "Market Closed · AH",
            "watchlist_session_date": session_date["value"],
            "as_of": "2026-10-02T16:01:00-04:00",
            "timezone": "America/New_York",
        }

    monkeypatch.setattr(web_api, "nyse_market_status", fake_status)
    assert authenticated.get("/api/v1/status").json()["watchlist_rollover"]["initialized"]

    response = authenticated.post(
        "/api/v1/planning/AAPL/commands",
        json={
            "command_id": str(uuid.uuid4()),
            "operation": "add_watchlist",
            "expected_revision": 0,
        },
    )
    assert response.status_code == 200

    session_date["value"] = "2026-10-05"
    heartbeat = authenticated.get("/api/v1/status")

    assert heartbeat.status_code == 200
    assert heartbeat.json()["watchlist_rollover"]["rolled_over"] is True
    assert heartbeat.json()["watchlist_rollover"]["archived_symbols"] == 1
    assert authenticated.get("/api/v1/planning/AAPL").json()["card"] is None


def test_mutation_requires_session_csrf(client):
    login(client)
    response = client.post(
        "/api/v1/planning/AAPL/commands",
        headers={"Origin": "http://localhost:8080"},
        json={
            "command_id": str(uuid.uuid4()),
            "operation": "add_watchlist",
            "expected_revision": 0,
        },
    )
    assert response.status_code == 403


def test_security_headers_are_present(client):
    response = client.get("/api/v1/auth/csrf")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert response.headers["x-frame-options"] == "DENY"


def test_logout_revokes_session(authenticated):
    assert authenticated.post("/api/v1/auth/logout").status_code == 200
    assert authenticated.get("/api/v1/session").status_code == 401


def test_execution_and_power_operations_are_denied(authenticated):
    paths = (
        "/api/v1/execution/arm",
        "/api/v1/execution/full-live",
        "/api/v1/orders/buy",
        "/api/v1/orders/sell",
        "/api/v1/orders/cancel",
        "/api/v1/orders/replace",
        "/api/v1/ownership/transfer",
        "/api/v1/risk/global-limit",
        "/api/v1/power/shutdown",
    )
    for path in paths:
        response = authenticated.post(path)
        assert response.status_code == 403, (path, response.text)


def test_api_docs_are_not_exposed(client):
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404


def test_chart_endpoint_serves_the_precompressed_cache(authenticated):
    first = authenticated.get("/api/v1/charts/AAPL/1D")
    assert first.status_code == 200
    assert first.headers["content-encoding"] == "gzip"
    assert first.headers["x-chart-cache"] == "MISS"
    assert first.json()["symbol"] == "AAPL"
    second = authenticated.get("/api/v1/charts/AAPL/1D")
    assert second.status_code == 200
    assert second.headers["x-chart-cache"] == "HIT"
    unchanged = authenticated.get(
        "/api/v1/charts/AAPL/1D",
        headers={"If-None-Match": second.headers["etag"]},
    )
    assert unchanged.status_code == 304


def test_connected_mode_never_exposes_sandbox_plans(web_config):
    connected = replace(web_config, mode="CONNECTED")
    services = build_services(connected)
    services.auth.bootstrap_user("owner", "correct horse battery staple")
    services.store.apply_planning_command(
        command_id=str(uuid.uuid4()),
        operation="add_watchlist",
        symbol="AAPL",
        expected_revision=0,
        actor="test",
    )
    with TestClient(
        create_api_app(connected, services=services),
        base_url="http://localhost:8080",
    ) as connected_client:
        session = login(connected_client)
        connected_client.headers.update(
            {
                "Origin": "http://localhost:8080",
                "X-CSRF-Token": session["csrf_token"],
            }
        )
        listing = connected_client.get("/api/v1/planning").json()
        assert listing["rows"] == []
        assert listing["sync_state"].startswith("CANONICAL PLANNING UNAVAILABLE")
        mutation = connected_client.post(
            "/api/v1/planning/AAPL/commands",
            json={
                "command_id": str(uuid.uuid4()),
                "operation": "set_breakout",
                "expected_revision": 1,
                "breakout_price": 100,
            },
        )
        assert mutation.status_code == 403
        preview = connected_client.post(
            "/api/v1/planning/AAPL/buy-today-preview",
            json={"expected_revision": 1},
        )
        assert preview.status_code == 503


def test_connected_canonical_write_switch_requires_explicit_allowlist():
    with pytest.raises(WebConfigError, match="allowlist"):
        load_web_config(
            overrides={
                "mode": "CONNECTED",
                "canonical_planning_reads": True,
                "pc_repository_path": ".",
                "canonical_planning_writes": True,
                "connected_passive_operations": [],
            }
        )
