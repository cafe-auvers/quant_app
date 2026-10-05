from __future__ import annotations

import datetime as dt
import json
from dataclasses import replace

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, text

from src.core.trade_card_state import BoardStatus, TradeCardState
from src.core.exit_policy import market_session_date
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


def test_board_details_project_only_public_card_facts(tmp_path):
    source = canonical_source(tmp_path)
    now = dt.datetime.now(dt.timezone.utc)
    card = TradeCardState(
        environment="PROD", account_no="private-account", symbol="DETAIL",
        board_status=BoardStatus.PARTIAL_SELL, broker_quantity=100,
        orderable_quantity=75, average_entry_price=10, active_stop_price=9.5,
        stop_quantity=100, pending_stop_price=9.8, pending_stop_quantity=100,
        pending_partial_sell_quantity=25, reserved_sell_quantity=25,
        entry_execution_price=10.1, entry_breakout_trigger=10.05,
        entry_remaining_target_quantity=20, next_retry_at=now,
        next_exit_retry_at=now, market_data_last_trusted_price=10.5,
        market_data_last_trusted_at=now, entry_client_order_id="private-entry",
        exit_client_order_id="private-exit",
    )
    row = source._project_board_card(card)
    assert row["last_reported_price"] == 10.5 and row["price_as_of"] == now.isoformat()
    assert row["entry_execution_price"] == 10.1 and row["entry_breakout_trigger"] == 10.05
    assert row["stop_quantity"] == row["pending_stop_quantity"] == 100
    assert row["entry_remaining_target_quantity"] == 20
    assert row["reserved_sell_quantity"] == row["pending_partial_sell_quantity"] == 25
    assert row["next_retry_at"] == row["next_exit_retry_at"] == now.isoformat()
    assert not any(value.startswith("private-") for value in row.values() if isinstance(value, str))
    empty = source._project_board_card(TradeCardState(environment="PROD", account_no="a", symbol="EMPTY"))
    assert empty["last_reported_price"] is None and empty["price_as_of"] is None
    source.close()


def test_unchanged_board_checks_revision_without_redownloading_payloads(tmp_path):
    source = canonical_source(tmp_path)
    statements = []
    event.listen(source.engine, "before_cursor_execute", lambda _conn, _cursor, statement, *_args: statements.append(statement))
    original = source.list_plans()
    statements.clear()
    assert source.list_plans() == original
    assert len(statements) == 1
    assert "COUNT(*)" in statements[0]
    assert "payload" not in statements[0]
    with source.engine.begin() as connection:
        connection.execute(text("UPDATE trade_cards SET version=version+1 WHERE symbol='AAPL'"))
    statements.clear()
    changed = source.list_plans()
    assert changed["revision"] != original["revision"]
    assert any("SELECT payload" in statement for statement in statements)
    statements.clear()
    source.list_plans(force=True)
    assert any("SELECT payload" in statement for statement in statements)
    source.close()


def test_mobile_buylist_shows_current_session_rejection_without_stale_history(tmp_path):
    source = canonical_source(tmp_path)
    card = TradeCardState(
        environment="PROD", account_no="account-a", symbol="ODD",
        board_status=BoardStatus.BUYLIST,
        buy_today_note="Buy Today rejected - all ORB plans invalid.",
        last_buy_today_session_date=market_session_date(),
    )
    statements = []
    event.listen(source.engine, "before_cursor_execute", lambda _c, _cur, sql, *_a: statements.append(sql))
    assert source._project_board_card(card)["buy_today_note"] == card.buy_today_note
    card.last_buy_today_session_date -= dt.timedelta(days=1)
    assert source._project_board_card(card)["buy_today_note"] == ""
    card.last_buy_today_session_date = market_session_date()
    card.board_status = BoardStatus.BUY_TODAY
    assert source._project_board_card(card)["buy_today_note"] == ""
    assert statements == []
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
