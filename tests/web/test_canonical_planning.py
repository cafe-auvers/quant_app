from __future__ import annotations

import datetime as dt
import json
from dataclasses import replace

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from src.core.trade_card_state import BoardStatus, TradeCardState
from src.utils.market_calendar import current_or_next_nyse_session_date
from src.web.api import build_services, create_api_app
from src.web.canonical_planning import CanonicalPlanningSource

from .conftest import login


def canonical_source(tmp_path) -> CanonicalPlanningSource:
    engine = create_engine(f"sqlite:///{tmp_path / 'canonical.db'}", future=True)
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE trade_cards ("
                "environment TEXT NOT NULL, account_no TEXT NOT NULL, "
                "symbol TEXT NOT NULL, board_status TEXT NOT NULL, "
                "version INTEGER NOT NULL, payload TEXT NOT NULL, "
                "updated_at DATETIME NOT NULL)"
            )
        )
        current = current_or_next_nyse_session_date()
        stale = current - dt.timedelta(days=1)
        cards = (
            TradeCardState(
                environment="PROD",
                account_no="account-a",
                symbol="AAPL",
                name="Apple",
                version=4,
                board_status=BoardStatus.WATCHLIST,
                watchlist_member=True,
                watchlist_session_date=current,
                breakout_price=201.5,
            ),
            TradeCardState(
                environment="PROD",
                account_no="account-a",
                symbol="MSFT",
                name="Microsoft",
                version=7,
                board_status=BoardStatus.BUYLIST,
                watchlist_member=True,
                watchlist_session_date=current,
                buylist_member=True,
                breakout_price=520.0,
            ),
            TradeCardState(
                environment="PROD",
                account_no="account-a",
                symbol="NVDA",
                name="NVIDIA",
                version=9,
                board_status=BoardStatus.BUY_TODAY,
                watchlist_member=True,
                watchlist_session_date=current,
                buylist_member=True,
                breakout_price=190.0,
            ),
            TradeCardState(
                environment="PROD",
                account_no="account-a",
                symbol="OLD",
                version=2,
                board_status=BoardStatus.WATCHLIST,
                watchlist_member=True,
                watchlist_session_date=stale,
            ),
            TradeCardState(
                environment="PROD",
                account_no="account-a",
                symbol="HIST",
                version=3,
                board_status=BoardStatus.WATCHLIST,
                watchlist_member=False,
                breakout_price=88.5,
            ),
        )
        for card in cards:
            connection.execute(
                text(
                    "INSERT INTO trade_cards "
                    "(environment, account_no, symbol, board_status, version, payload, updated_at) "
                    "VALUES (:environment, :account_no, :symbol, :board_status, :version, :payload, :updated_at)"
                ),
                {
                    "environment": card.environment,
                    "account_no": card.account_no,
                    "symbol": card.symbol,
                    "board_status": card.board_status.value,
                    "version": card.version,
                    "payload": json.dumps(card.to_dict()),
                    "updated_at": card.updated_at.replace(tzinfo=None),
                },
            )
    return CanonicalPlanningSource(
        enabled=True,
        environment="PROD",
        account_no="account-a",
        engine=engine,
        cache_seconds=0,
    )


def test_canonical_projection_preserves_independent_memberships(tmp_path):
    source = canonical_source(tmp_path)
    result = source.list_plans()
    rows = {row["symbol"]: row for row in result["rows"]}

    assert set(rows) == {"AAPL", "MSFT", "NVDA"}
    assert rows["MSFT"]["watchlist_member"] is True
    assert rows["MSFT"]["buylist_member"] is True
    assert rows["NVDA"]["watchlist_member"] is True
    assert rows["NVDA"]["buylist_member"] is True
    assert rows["NVDA"]["buy_today_member"] is True
    assert source.list_buy_today()["rows"][0]["symbol"] == "NVDA"
    assert source.get_plan("OLD")["card"] is None
    historical = source.get_plan("HIST")["card"]
    assert historical["display_stage"] == "BREAKOUT"
    assert historical["watchlist_member"] is False
    assert historical["breakout_price"] == 88.5
    source.close()


def test_connected_api_returns_canonical_rows_and_never_local_sandbox_rows(
    web_config, tmp_path
):
    connected = replace(web_config, mode="CONNECTED")
    services = build_services(connected)
    services.canonical_planning = canonical_source(tmp_path)
    services.auth.bootstrap_user("owner", "correct horse battery staple")

    with TestClient(
        create_api_app(connected, services=services),
        base_url="http://localhost:8080",
    ) as client:
        session = login(client)
        client.headers.update(
            {
                "Origin": "http://localhost:8080",
                "X-CSRF-Token": session["csrf_token"],
            }
        )
        planning = client.get("/api/v1/planning").json()
        buy_today = client.get("/api/v1/buy-today-drafts").json()
        current = client.get("/api/v1/planning/MSFT").json()

    assert planning["sync_state"] == "PC CANONICAL READ-ONLY"
    assert {row["symbol"] for row in planning["rows"]} == {"AAPL", "MSFT", "NVDA"}
    assert [row["symbol"] for row in buy_today["rows"]] == ["NVDA"]
    assert current["card"]["breakout_price"] == 520.0
    assert current["card"]["source"] == "CANONICAL_TIDB"


def test_connected_buy_today_preview_is_shared_but_never_published(
    web_config, tmp_path
):
    connected = replace(web_config, mode="CONNECTED")
    services = build_services(connected)
    services.canonical_planning = canonical_source(tmp_path)
    services.auth.bootstrap_user("owner", "correct horse battery staple")
    app = create_api_app(connected, services=services)

    with TestClient(app, base_url="http://localhost:8080") as phone:
        session = login(phone)
        phone.headers.update(
            {
                "Origin": "http://localhost:8080",
                "X-CSRF-Token": session["csrf_token"],
            }
        )
        saved = phone.post(
            "/api/v1/planning/MSFT/buy-today-preview",
            json={"expected_revision": 7},
        )
        assert saved.status_code == 200
        assert saved.json()["published"] is False
        assert saved.json()["executable"] is False

    with TestClient(app, base_url="http://localhost:8080") as laptop:
        session = login(laptop)
        laptop.headers.update(
            {
                "Origin": "http://localhost:8080",
                "X-CSRF-Token": session["csrf_token"],
            }
        )
        visible = laptop.get("/api/v1/buy-today-drafts").json()
        rows = {row["symbol"]: row for row in visible["rows"]}
        assert set(rows) == {"MSFT", "NVDA"}
        assert rows["MSFT"]["stage"] == "BUY_TODAY_DRAFT"
        assert rows["MSFT"]["executable"] is False
        assert rows["NVDA"]["canonical_stage"] == "BUY_TODAY"

        cancelled = laptop.request(
            "DELETE",
            "/api/v1/planning/MSFT/buy-today-preview",
            json={"expected_revision": 7},
        )
        assert cancelled.status_code == 200
        assert cancelled.json()["cancelled"] is True
        assert [
            row["symbol"]
            for row in laptop.get("/api/v1/buy-today-drafts").json()["rows"]
        ] == ["NVDA"]

        forbidden = laptop.post("/api/v1/planning/MSFT/activate-buy-today")
        assert forbidden.status_code == 403
