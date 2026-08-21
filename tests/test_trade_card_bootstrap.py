"""Tests for automatic Buy Board bootstrap from current application state."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

from src.core.trade_card_state import BoardStatus, TradeCardState
from src.core.watchlist import BuylistItem, BuylistManager, Watchlist
from src.services import trade_card_repository
from src.services.execution_workflow_service import list_board_projections
from src.services.trade_card_bootstrap import (
    bootstrap_trade_cards_from_current_state,
)
from src.ui.buyboard.columns import BOARD_COLUMN_ORDER


def _engine(tmp_path):
    return create_engine(
        f"sqlite:///{tmp_path / 'bootstrap.db'}",
        future=True,
        poolclass=NullPool,
    )


def _buylist_item(symbol: str, *, account_no: str = "") -> BuylistItem:
    return BuylistItem(
        symbol=symbol,
        name=f"{symbol} Inc.",
        entry_price=100.0,
        target_price=0.0,
        stop_loss=95.0,
        total_score=80.0,
        status="candidate",
        technical_score=80.0,
        setup_score=80.0,
        risk_score=80.0,
        news_score=80.0,
        timing_score=80.0,
        rr=2.0,
        stop_adr=30.0,
        position_percent=20.0,
        ai_summary="",
        warnings=[],
        kis_account_no=account_no,
        environment="PROD",
        monitoring_status="WATCHING",
    )


def test_bootstrap_creates_watchlist_only_card(tmp_path, monkeypatch):
    monkeypatch.setattr(
        trade_card_repository,
        "LOCAL_TRADE_CARDS_FILE",
        tmp_path / "trade_cards.json",
    )
    engine = _engine(tmp_path)
    watchlist = Watchlist()
    watchlist.add("AAPL", "Apple")
    manager = BuylistManager()

    result = bootstrap_trade_cards_from_current_state(
        engine,
        buylist_manager=manager,
        watchlist=watchlist,
        default_account_no="12345678-01",
    )

    card = trade_card_repository.get_trade_card(
        engine, "PROD", "12345678-01", "AAPL"
    )
    assert result.created_keys == ("PROD:12345678-01:AAPL",)
    assert card is not None
    assert card.board_status == BoardStatus.WATCHLIST
    assert card.watchlist_member is True
    assert card.buylist_member is False


def test_bootstrap_uses_default_account_for_legacy_buylist(tmp_path, monkeypatch):
    monkeypatch.setattr(
        trade_card_repository,
        "LOCAL_TRADE_CARDS_FILE",
        tmp_path / "trade_cards.json",
    )
    engine = _engine(tmp_path)
    manager = BuylistManager()
    manager.add(_buylist_item("MSFT", account_no=""))

    bootstrap_trade_cards_from_current_state(
        engine,
        buylist_manager=manager,
        watchlist=Watchlist(),
        default_account_no="12345678-01",
    )

    card = trade_card_repository.get_trade_card(
        engine, "PROD", "12345678-01", "MSFT"
    )
    assert card is not None
    assert card.board_status == BoardStatus.BUYLIST
    assert card.buylist_member is True


def test_buylist_buffer_round_trips_but_watchlist_bootstrap_discards_retired_orb_plan(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        trade_card_repository,
        "LOCAL_TRADE_CARDS_FILE",
        tmp_path / "trade_cards.json",
    )
    engine = _engine(tmp_path)
    manager = BuylistManager()
    buylist_item = _buylist_item("ZERO", account_no="12345678-01")
    buylist_item.buffer_pct = 0.0
    manager.add(buylist_item)
    watchlist = Watchlist()
    watch_item = watchlist.add("NONE", "No Buffer")
    watch_item.breakout_price = 123.45
    watch_item.selected_orb_plan = {
        "window": "5m",
        "buffer_pct": 0.0,
        "capital_percent": 25.0,
        "shares": 40,
        "entry_trigger": 124.0,
        "stop_adr": 30.0,
    }

    bootstrap_trade_cards_from_current_state(
        engine,
        buylist_manager=manager,
        watchlist=watchlist,
        default_account_no="12345678-01",
    )

    buylist_card = trade_card_repository.get_trade_card(
        engine, "PROD", "12345678-01", "ZERO"
    )
    watchlist_card = trade_card_repository.get_trade_card(
        engine, "PROD", "12345678-01", "NONE"
    )
    assert buylist_card.buffer_pct == 0.0
    assert watchlist_card.breakout_price == 123.45
    assert watchlist_card.buffer_pct == pytest.approx(0.001)
    assert watchlist_card.selected_orb_window is None
    assert watchlist_card.position_percent == 0.0
    assert watchlist_card.planned_quantity == 0
    assert watchlist_card.target_position_quantity == 0
    assert watchlist_card.entry_trigger is None
    assert watchlist_card.stop_adr is None


def test_bootstrap_never_overwrites_existing_kanban_lifecycle(tmp_path, monkeypatch):
    monkeypatch.setattr(
        trade_card_repository,
        "LOCAL_TRADE_CARDS_FILE",
        tmp_path / "trade_cards.json",
    )
    engine = _engine(tmp_path)
    existing = trade_card_repository.create_trade_card(
        engine,
        TradeCardState(
            environment="PROD",
            account_no="12345678-01",
            symbol="AAPL",
            board_status=BoardStatus.BUY_TODAY,
            buylist_member=True,
        ),
    )
    manager = BuylistManager()
    manager.add(_buylist_item("AAPL", account_no="12345678-01"))

    result = bootstrap_trade_cards_from_current_state(
        engine,
        buylist_manager=manager,
        watchlist=Watchlist(),
        default_account_no="12345678-01",
    )

    stored = trade_card_repository.get_trade_card(
        engine, "PROD", "12345678-01", "AAPL"
    )
    assert result.changed is False
    assert stored is not None
    assert stored.board_status == BoardStatus.BUY_TODAY
    assert stored.version == existing.version


def test_bootstrap_promotes_passive_watchlist_card_to_buylist(tmp_path, monkeypatch):
    monkeypatch.setattr(
        trade_card_repository,
        "LOCAL_TRADE_CARDS_FILE",
        tmp_path / "trade_cards.json",
    )
    engine = _engine(tmp_path)
    existing = trade_card_repository.create_trade_card(
        engine,
        TradeCardState(
            environment="PROD",
            account_no="12345678-01",
            symbol="AAPL",
            name="Apple",
            board_status=BoardStatus.WATCHLIST,
            watchlist_member=True,
            buylist_member=False,
        ),
    )
    manager = BuylistManager()
    manager.add(_buylist_item("AAPL", account_no="12345678-01"))

    first = bootstrap_trade_cards_from_current_state(
        engine,
        buylist_manager=manager,
        watchlist=Watchlist(),
        default_account_no="12345678-01",
    )
    promoted = trade_card_repository.get_trade_card(
        engine, "PROD", "12345678-01", "AAPL"
    )

    assert first.buylist_promoted_keys == ("PROD:12345678-01:AAPL",)
    assert promoted is not None
    assert promoted.board_status == BoardStatus.BUYLIST
    assert promoted.previous_board_status == BoardStatus.WATCHLIST
    assert promoted.watchlist_member is True
    assert promoted.buylist_member is True
    assert promoted.version == existing.version + 1

    # A restart or second device sees the canonical promotion and performs no
    # further write/version bump.
    second = bootstrap_trade_cards_from_current_state(
        engine,
        buylist_manager=manager,
        watchlist=Watchlist(),
        default_account_no="12345678-01",
    )
    refreshed = trade_card_repository.get_trade_card(
        engine, "PROD", "12345678-01", "AAPL"
    )
    assert second.changed is False
    assert refreshed is not None
    assert refreshed.version == promoted.version


@pytest.mark.parametrize(
    "status",
    [
        BoardStatus.BUY_TODAY,
        BoardStatus.ENTRY_PENDING,
        BoardStatus.OPEN_POSITION,
        BoardStatus.PARTIAL_SELL,
        BoardStatus.SELL_ALL,
    ],
)
def test_bootstrap_never_moves_advanced_lifecycle_back_to_buylist(
    tmp_path, monkeypatch, status
):
    monkeypatch.setattr(
        trade_card_repository,
        "LOCAL_TRADE_CARDS_FILE",
        tmp_path / "trade_cards.json",
    )
    engine = _engine(tmp_path)
    existing = trade_card_repository.create_trade_card(
        engine,
        TradeCardState(
            environment="PROD",
            account_no="12345678-01",
            symbol="AAPL",
            board_status=status,
            buylist_member=True,
        ),
    )
    manager = BuylistManager()
    manager.add(_buylist_item("AAPL", account_no="12345678-01"))

    result = bootstrap_trade_cards_from_current_state(
        engine,
        buylist_manager=manager,
        watchlist=Watchlist(),
        default_account_no="12345678-01",
    )

    stored = trade_card_repository.get_trade_card(
        engine, "PROD", "12345678-01", "AAPL"
    )
    assert result.changed is False
    assert stored is not None
    assert stored.board_status == status
    assert stored.version == existing.version


def test_bootstrap_cas_conflict_preserves_concurrent_lifecycle_advance(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        trade_card_repository,
        "LOCAL_TRADE_CARDS_FILE",
        tmp_path / "trade_cards.json",
    )
    engine = _engine(tmp_path)
    trade_card_repository.create_trade_card(
        engine,
        TradeCardState(
            environment="PROD",
            account_no="12345678-01",
            symbol="AAPL",
            board_status=BoardStatus.WATCHLIST,
            watchlist_member=True,
        ),
    )
    manager = BuylistManager()
    manager.add(_buylist_item("AAPL", account_no="12345678-01"))
    real_update = trade_card_repository.update_trade_card
    raced = False

    def update_after_concurrent_advance(
        target_engine, card, *, expected_version, local_snapshot_path=None
    ):
        nonlocal raced
        if not raced:
            raced = True
            concurrent = trade_card_repository.get_trade_card(
                target_engine, "PROD", "12345678-01", "AAPL"
            )
            assert concurrent is not None
            concurrent.board_status = BoardStatus.BUY_TODAY
            real_update(
                target_engine,
                concurrent,
                expected_version=concurrent.version,
                local_snapshot_path=local_snapshot_path,
            )
        return real_update(
            target_engine,
            card,
            expected_version=expected_version,
            local_snapshot_path=local_snapshot_path,
        )

    monkeypatch.setattr(
        trade_card_repository,
        "update_trade_card",
        update_after_concurrent_advance,
    )

    result = bootstrap_trade_cards_from_current_state(
        engine,
        buylist_manager=manager,
        watchlist=Watchlist(),
        default_account_no="12345678-01",
    )

    stored = trade_card_repository.get_trade_card(
        engine, "PROD", "12345678-01", "AAPL"
    )
    assert result.changed is False
    assert stored is not None
    assert stored.board_status == BoardStatus.BUY_TODAY
    assert stored.version == 2


def test_promoted_buylist_card_survives_visible_board_filter(tmp_path, monkeypatch):
    monkeypatch.setattr(
        trade_card_repository,
        "LOCAL_TRADE_CARDS_FILE",
        tmp_path / "trade_cards.json",
    )
    engine = _engine(tmp_path)
    trade_card_repository.create_trade_card(
        engine,
        TradeCardState(
            environment="PROD",
            account_no="12345678-01",
            symbol="AAPL",
            board_status=BoardStatus.WATCHLIST,
            watchlist_member=True,
        ),
    )
    manager = BuylistManager()
    manager.add(_buylist_item("AAPL", account_no="12345678-01"))

    bootstrap_trade_cards_from_current_state(
        engine,
        buylist_manager=manager,
        watchlist=Watchlist(),
        default_account_no="12345678-01",
    )
    projections = list_board_projections(
        engine,
        environment="PROD",
        board_statuses=BOARD_COLUMN_ORDER,
    )

    assert [(row.card.symbol, row.card.board_status) for row in projections] == [
        ("AAPL", BoardStatus.BUYLIST)
    ]


def test_fresh_cached_holding_is_visible_as_open_position(tmp_path, monkeypatch):
    monkeypatch.setattr(
        trade_card_repository,
        "LOCAL_TRADE_CARDS_FILE",
        tmp_path / "trade_cards.json",
    )
    engine = _engine(tmp_path)
    now = datetime(2026, 8, 17, 7, 30, tzinfo=timezone.utc)
    snapshots = {
        ("PROD", "12345678-01"): {
            "overseas": {
                "holdings": [
                    {
                        "symbol": "NVDA",
                        "quantity": 7,
                        "average_price": 181.25,
                    }
                ]
            }
        }
    }
    fetched = {("PROD", "12345678-01"): now - timedelta(seconds=5)}

    result = bootstrap_trade_cards_from_current_state(
        engine,
        buylist_manager=BuylistManager(),
        watchlist=Watchlist(),
        default_account_no="12345678-01",
        account_snapshots=snapshots,
        account_snapshot_fetched_at=fetched,
        now=now,
    )

    card = trade_card_repository.get_trade_card(
        engine, "PROD", "12345678-01", "NVDA"
    )
    assert result.changed
    assert card is not None
    assert card.board_status == BoardStatus.OPEN_POSITION
    assert card.broker_quantity == 7
    assert card.average_entry_price == 181.25
    assert "STOP_REQUIRED" in card.warnings


def test_stale_cached_holding_is_not_bootstrapped(tmp_path, monkeypatch):
    monkeypatch.setattr(
        trade_card_repository,
        "LOCAL_TRADE_CARDS_FILE",
        tmp_path / "trade_cards.json",
    )
    engine = _engine(tmp_path)
    now = datetime(2026, 8, 17, 7, 30, tzinfo=timezone.utc)
    snapshots = {
        ("PROD", "12345678-01"): {
            "overseas": {
                "holdings": [
                    {"symbol": "NVDA", "quantity": 7, "average_price": 181.25}
                ]
            }
        }
    }
    fetched = {("PROD", "12345678-01"): now - timedelta(minutes=10)}

    result = bootstrap_trade_cards_from_current_state(
        engine,
        buylist_manager=BuylistManager(),
        watchlist=Watchlist(),
        default_account_no="12345678-01",
        account_snapshots=snapshots,
        account_snapshot_fetched_at=fetched,
        max_snapshot_age_seconds=120,
        now=now,
    )

    assert result.changed is False
    assert (
        trade_card_repository.get_trade_card(
            engine, "PROD", "12345678-01", "NVDA"
        )
        is None
    )
