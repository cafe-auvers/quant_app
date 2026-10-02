from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from src.web.application import create_web_app
from src.web.auth import SESSION_COOKIE
from src.web.config import WebConfig

from .conftest import login


def test_static_application_redirects_and_serves_dashboard(tmp_path):
    config = WebConfig(data_dir=tmp_path / "web")
    app, services = create_web_app(config)
    services.auth.bootstrap_user("owner", "correct horse battery staple")

    with TestClient(
        app, base_url="http://localhost:8080", follow_redirects=False
    ) as client:
        root = client.get("/")
        assert root.status_code == 303
        assert root.headers["location"] == "/login"

        sign_in = client.get("/login")
        assert sign_in.status_code == 200
        assert 'id="login-form"' in sign_in.text
        assert "_nicegui" not in sign_in.text.lower()

        login(client)
        dashboard = client.get("/")
        assert dashboard.status_code == 200
        assert 'id="quant-app"' in dashboard.text
        assert "_nicegui" not in dashboard.text.lower()

        script = client.get("/web-static/app.js")
        assert script.status_code == 200
        assert "bundleInflight" in script.text

        with client.websocket_connect(
            "/live-updates",
            headers={
                "Host": "localhost:8080",
                "Origin": "http://localhost:8080",
                "Cookie": f"{SESSION_COOKIE}={client.cookies.get(SESSION_COOKIE)}",
            },
        ) as socket:
            assert socket.receive_json() == {"kind": "ready"}
            services.live_updates.publish(
                {"kind": "planning", "symbol": "CURV", "revision": 10}
            )
            assert socket.receive_json() == {
                "kind": "planning",
                "symbol": "CURV",
                "revision": 10,
            }


def test_two_browser_sessions_invalidate_converge_and_reject_stale_write(tmp_path):
    config = WebConfig(data_dir=tmp_path / "web")
    app, services = create_web_app(config)
    services.auth.bootstrap_user("owner", "correct horse battery staple")

    with TestClient(app, base_url="http://localhost:8080") as browser_a:
        with TestClient(app, base_url="http://localhost:8080") as browser_b:
            session_a = login(browser_a)
            session_b = login(browser_b)
            browser_a.headers.update(
                {
                    "Origin": "http://localhost:8080",
                    "X-CSRF-Token": session_a["csrf_token"],
                }
            )
            browser_b.headers.update(
                {
                    "Origin": "http://localhost:8080",
                    "X-CSRF-Token": session_b["csrf_token"],
                }
            )
            with browser_b.websocket_connect(
                "/live-updates",
                headers={
                    "Host": "localhost:8080",
                    "Origin": "http://localhost:8080",
                    "Cookie": (
                        f"{SESSION_COOKIE}="
                        f"{browser_b.cookies.get(SESSION_COOKIE)}"
                    ),
                },
            ) as socket_b:
                assert socket_b.receive_json() == {"kind": "ready"}
                added = browser_a.post(
                    "/api/v1/planning/AAPL/commands",
                    json={
                        "command_id": str(uuid.uuid4()),
                        "operation": "add_watchlist",
                        "expected_revision": 0,
                        "breakout_price": None,
                    },
                )
                assert added.status_code == 200
                event = socket_b.receive_json()
                assert event["kind"] == "planning"
                assert event["symbol"] == "AAPL"
                observed = browser_b.get("/api/v1/planning/AAPL").json()["card"]
                assert observed["watchlist_member"] is True
                assert observed["version"] == 1

                committed = browser_b.post(
                    "/api/v1/planning/AAPL/commands",
                    json={
                        "command_id": str(uuid.uuid4()),
                        "operation": "set_breakout",
                        "expected_revision": 1,
                        "breakout_price": 201.25,
                    },
                )
                assert committed.status_code == 200
                assert socket_b.receive_json()["revision"] == 2

                stale = browser_a.post(
                    "/api/v1/planning/AAPL/commands",
                    json={
                        "command_id": str(uuid.uuid4()),
                        "operation": "set_breakout",
                        "expected_revision": 1,
                        "breakout_price": 202.0,
                    },
                )
                assert stale.status_code == 409
                assert stale.json()["current"]["version"] == 2
                assert stale.json()["current"]["breakout_price"] == 201.25


def test_retired_nicegui_tab_is_redirected_to_static_shell(tmp_path):
    config = WebConfig(data_dir=tmp_path / "web")
    app, _services = create_web_app(config)

    with TestClient(app, base_url="http://localhost:8080") as client:
        with client.websocket_connect(
            "/_nicegui_ws/socket.io/?EIO=4&transport=websocket",
            headers={
                "Host": "localhost:8080",
                "Origin": "http://localhost:8080",
            },
        ) as socket:
            assert socket.receive_text().startswith("0{")
            socket.send_text("40")
            assert socket.receive_text() == '40{"sid":"retired-ui-migration"}'
            assert socket.receive_text() == (
                '42["open",{"path":"/reset-ui","new_tab":false}]'
            )


def test_reset_ui_clears_legacy_cache_without_clearing_login_cookie(tmp_path):
    app, _services = create_web_app(WebConfig(data_dir=tmp_path / "web"))

    with TestClient(app, base_url="http://localhost:8080") as client:
        response = client.get("/reset-ui")
        assert response.status_code == 200
        assert "getRegistrations" in response.text
        assert "caches.keys()" in response.text
        assert "window.location.replace('/?lite=70')" in response.text
        assert "cookies" not in response.text.lower()
        assert "no-store" in response.headers["cache-control"]
