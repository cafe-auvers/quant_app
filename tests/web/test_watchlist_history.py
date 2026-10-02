from __future__ import annotations

import json
import uuid
from dataclasses import replace

from src.web.api import build_services, create_api_app
from src.web.store import WebStore
from src.web.watchlist_history import WatchlistHistorySource, merge_watchlist_history

from .conftest import login


def _command(
    store: WebStore, operation: str, symbol: str, revision: int
) -> None:
    store.apply_planning_command(
        command_id=str(uuid.uuid4()),
        operation=operation,
        symbol=symbol,
        expected_revision=revision,
        actor="history-test",
    )


def _date_operations(store: WebStore, dates: dict[tuple[str, str], str]) -> None:
    with store.connection() as connection:
        rows = connection.execute(
            "SELECT id, symbol, detail_json FROM web_audit ORDER BY id"
        ).fetchall()
        for row in rows:
            operation = json.loads(str(row["detail_json"]))["operation"]
            created_at = dates[(str(row["symbol"]), operation)]
            connection.execute(
                "UPDATE web_audit SET created_at=? WHERE id=?",
                (created_at, int(row["id"])),
            )


def test_watchlist_history_reconstructs_overlaps_and_deduplicates(tmp_path):
    store = WebStore(tmp_path / "state.db")
    _command(store, "add_watchlist", "AAPL", 0)
    store.apply_planning_command(
        command_id=str(uuid.uuid4()), operation="set_breakout", symbol="AAPL",
        expected_revision=1, actor="history-test", breakout_price=200,
    )
    _command(store, "promote_buylist", "AAPL", 2)
    _command(store, "move_watchlist", "AAPL", 3)
    _command(store, "remove_watchlist", "AAPL", 4)
    _command(store, "add_watchlist", "AMD", 0)
    _command(store, "remove_watchlist", "AMD", 1)
    _command(store, "add_watchlist", "MSFT", 0)
    _date_operations(
        store,
        {
            ("AAPL", "add_watchlist"): "2026-09-20T09:00:00+00:00",
            ("AAPL", "set_breakout"): "2026-09-21T09:00:00+00:00",
            ("AAPL", "promote_buylist"): "2026-09-23T09:00:00+00:00",
            ("AAPL", "move_watchlist"): "2026-09-28T09:00:00+00:00",
            ("AAPL", "remove_watchlist"): "2026-09-29T09:00:00+00:00",
            ("AMD", "add_watchlist"): "2026-09-25T09:00:00+00:00",
            ("AMD", "remove_watchlist"): "2026-10-01T09:00:00+00:00",
            ("MSFT", "add_watchlist"): "2026-10-02T09:00:00+00:00",
        },
    )

    rows = store.list_watchlist_history("2026-09-22", "2026-09-30")

    assert [row["symbol"] for row in rows] == ["AMD", "AAPL"]
    assert rows[0] == {
        "symbol": "AMD",
        "first_seen_date": "2026-09-25",
        "last_seen_date": "2026-09-30",
        "watchlist_periods": 1,
    }
    assert rows[1]["first_seen_date"] == "2026-09-22"
    assert rows[1]["last_seen_date"] == "2026-09-29"
    assert rows[1]["watchlist_periods"] == 1


def test_market_close_rollover_archives_watchlist_and_preserves_buylist(tmp_path):
    store = WebStore(tmp_path / "state.db")
    assert store.rollover_watchlist_session("2026-10-02")["initialized"] is True

    _command(store, "add_watchlist", "AAPL", 0)
    store.apply_planning_command(
        command_id=str(uuid.uuid4()),
        operation="set_breakout",
        symbol="AAPL",
        expected_revision=1,
        actor="history-test",
        breakout_price=215.5,
    )
    _command(store, "add_watchlist", "MSFT", 0)
    store.apply_planning_command(
        command_id=str(uuid.uuid4()),
        operation="set_breakout",
        symbol="MSFT",
        expected_revision=1,
        actor="history-test",
        breakout_price=500.25,
    )
    _command(store, "promote_buylist", "MSFT", 2)
    with store.connection() as connection:
        connection.execute(
            "UPDATE web_audit SET created_at='2026-10-02T14:00:00+00:00'"
        )

    rolled = store.rollover_watchlist_session("2026-10-05")

    assert rolled == {
        "rolled_over": True,
        "initialized": False,
        "previous_session_date": "2026-10-02",
        "session_date": "2026-10-05",
        "archived_symbols": 2,
    }
    assert store.get_plan("AAPL")["watchlist_member"] is False
    assert store.get_plan("AAPL")["breakout_price"] == 215.5
    assert store.get_plan("MSFT")["stage"] == "BUYLIST"
    assert store.get_plan("MSFT")["watchlist_member"] is False
    assert store.get_plan("MSFT")["breakout_price"] == 500.25
    history = store.list_watchlist_history("2026-10-02", "2026-10-02")
    assert {row["symbol"] for row in history} == {"AAPL", "MSFT"}

    repeated = store.rollover_watchlist_session("2026-10-05")
    assert repeated["rolled_over"] is False
    assert repeated["archived_symbols"] == 0
    assert store.get_plan("MSFT")["breakout_price"] == 500.25


def test_first_rollover_check_adopts_session_without_clearing_existing_cards(tmp_path):
    store = WebStore(tmp_path / "state.db")
    _command(store, "add_watchlist", "AAPL", 0)

    result = store.rollover_watchlist_session("2026-10-05")

    assert result["initialized"] is True
    assert result["rolled_over"] is False
    assert store.get_plan("AAPL")["stage"] == "WATCHLIST"


def test_watchlist_history_endpoint_requires_valid_inclusive_dates(authenticated):
    response = authenticated.get(
        "/api/v1/planning-history",
        params={"start_date": "2026-09-19", "end_date": "2026-10-02"},
    )
    assert response.status_code == 200
    assert response.json()["start_date"] == "2026-09-19"
    assert response.json()["end_date"] == "2026-10-02"

    invalid = authenticated.get(
        "/api/v1/planning-history",
        params={"start_date": "2026-10-02", "end_date": "2026-09-19"},
    )
    assert invalid.status_code == 422


def test_dated_watchlist_history_returns_unique_symbols_in_range(tmp_path):
    archive = tmp_path / "watchlist.json"
    archive.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "sessions": {
                    "2026-09-18": {
                        "date": "2026-09-18",
                        "items": [{"symbol": "OUT", "name": "Outside"}],
                    },
                    "2026-09-22": {
                        "date": "2026-09-22",
                        "items": [
                            {"symbol": "AAPL", "name": "Apple"},
                            {"symbol": "MSFT", "name": "Microsoft"},
                        ],
                    },
                    "2026-09-29": {
                        "date": "2026-09-29",
                        "items": [{"symbol": "AAPL", "name": "Apple Inc."}],
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    rows = WatchlistHistorySource(archive).list_range("2026-09-19", "2026-10-02")

    assert [row["symbol"] for row in rows] == ["AAPL", "MSFT"]
    assert rows[0] == {
        "symbol": "AAPL",
        "name": "Apple Inc.",
        "first_seen_date": "2026-09-22",
        "last_seen_date": "2026-09-29",
        "watchlist_sessions": 2,
    }
    assert rows[1]["watchlist_sessions"] == 1


def test_history_merge_deduplicates_canonical_and_local_rows():
    rows = merge_watchlist_history(
        [
            {
                "symbol": "AAPL",
                "name": "Apple",
                "first_seen_date": "2026-09-22",
                "last_seen_date": "2026-09-29",
                "watchlist_sessions": 2,
            }
        ],
        [
            {
                "symbol": "AAPL",
                "first_seen_date": "2026-09-25",
                "last_seen_date": "2026-10-01",
                "watchlist_periods": 1,
            }
        ],
    )

    assert rows == [
        {
            "symbol": "AAPL",
            "name": "Apple",
            "first_seen_date": "2026-09-22",
            "last_seen_date": "2026-10-01",
            "watchlist_sessions": 2,
            "watchlist_periods": 1,
        }
    ]


def test_history_endpoint_reads_configured_dated_watchlist(web_config, tmp_path):
    archive = tmp_path / "canonical-watchlist.json"
    archive.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "sessions": {
                    "2026-09-25": {
                        "date": "2026-09-25",
                        "items": [{"symbol": "NVDA", "name": "NVIDIA"}],
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    configured = replace(web_config, watchlist_history_path=str(archive))
    services = build_services(configured)
    services.auth.bootstrap_user("owner", "correct horse battery staple")

    from fastapi.testclient import TestClient

    with TestClient(
        create_api_app(configured, services=services),
        base_url="http://localhost:8080",
    ) as client:
        session = login(client)
        client.headers.update(
            {
                "Origin": "http://localhost:8080",
                "X-CSRF-Token": session["csrf_token"],
            }
        )
        response = client.get(
            "/api/v1/planning-history",
            params={"start_date": "2026-09-19", "end_date": "2026-10-02"},
        )

    assert response.status_code == 200
    assert response.json()["source"] == "DATED WATCHLIST + LOCAL WEB AUDIT"
    assert response.json()["rows"][0]["symbol"] == "NVDA"
