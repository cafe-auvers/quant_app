"""Tests for src.services.trade_card_repository."""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.pool import NullPool

from src.core.trade_card_state import BoardStatus, TradeCardState
from src.core.watchlist import BuylistItem, BuylistManager, Watchlist, WatchlistItem
from src.services import trade_card_repository as repo


def _make_engine(tmp_path):
    return create_engine(
        f"sqlite:///{tmp_path / 'trade_cards.db'}",
        future=True,
        poolclass=NullPool,
    )


def _card(symbol="AAPL", account_no="1", **kw) -> TradeCardState:
    return TradeCardState(environment="PROD", account_no=account_no, symbol=symbol, **kw)


def _buylist_item(symbol="AAPL", **overrides) -> BuylistItem:
    fields = dict(
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
        kis_account_no="1",
    )
    fields.update(overrides)
    return BuylistItem(**fields)


# --- CRUD / optimistic concurrency ------------------------------------------


def test_create_then_get_round_trips(tmp_path):
    engine = _make_engine(tmp_path)
    created = repo.create_trade_card(engine, _card(), local_snapshot_path=tmp_path / "trade_cards.json")
    assert created.version == 1
    fetched = repo.get_trade_card(engine, "PROD", "1", "AAPL")
    assert fetched is not None
    assert fetched.symbol == "AAPL"
    assert fetched.version == 1


def test_strict_reads_do_not_turn_database_outage_into_missing_cards(monkeypatch):
    outage = OperationalError(
        "SELECT trade_cards", {}, RuntimeError("canonical database offline")
    )
    monkeypatch.setattr(
        repo,
        "_ensure_trade_cards_table",
        lambda _engine: (_ for _ in ()).throw(outage),
    )
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)

    assert repo.list_trade_cards(engine) == []
    assert repo.get_trade_card(engine, "PROD", "1", "AAPL") is None
    with pytest.raises(OperationalError):
        repo.list_trade_cards(engine, raise_on_error=True)
    with pytest.raises(OperationalError):
        repo.get_trade_card(
            engine, "PROD", "1", "AAPL", raise_on_error=True
        )


def test_create_duplicate_raises_version_conflict(tmp_path):
    engine = _make_engine(tmp_path)
    repo.create_trade_card(engine, _card(), local_snapshot_path=tmp_path / "trade_cards.json")
    with pytest.raises(repo.TradeCardVersionConflictError):
        repo.create_trade_card(engine, _card(), local_snapshot_path=tmp_path / "trade_cards.json")


def test_update_with_correct_expected_version_succeeds(tmp_path):
    engine = _make_engine(tmp_path)
    repo.create_trade_card(engine, _card(), local_snapshot_path=tmp_path / "trade_cards.json")
    fetched = repo.get_trade_card(engine, "PROD", "1", "AAPL")
    fetched.board_status = BoardStatus.BUYLIST
    updated = repo.update_trade_card(engine, fetched, expected_version=fetched.version, local_snapshot_path=tmp_path / "trade_cards.json")
    assert updated.version == 2
    refetched = repo.get_trade_card(engine, "PROD", "1", "AAPL")
    assert refetched.board_status == BoardStatus.BUYLIST
    assert refetched.version == 2


def test_update_with_stale_expected_version_rejected(tmp_path):
    """This is the PC/laptop contradictory-drag guard (spec section 317-319)."""
    engine = _make_engine(tmp_path)
    repo.create_trade_card(engine, _card(), local_snapshot_path=tmp_path / "trade_cards.json")

    # Device A reads version 1 and successfully writes -> version becomes 2.
    device_a_view = repo.get_trade_card(engine, "PROD", "1", "AAPL")
    device_a_view.board_status = BoardStatus.BUYLIST
    repo.update_trade_card(engine, device_a_view, expected_version=1, local_snapshot_path=tmp_path / "trade_cards.json")

    # Device B independently read version 1 earlier and now tries to write
    # against that stale version -- must be rejected, not silently applied.
    device_b_view = _card(board_status=BoardStatus.BUY_TODAY)
    with pytest.raises(repo.TradeCardVersionConflictError):
        repo.update_trade_card(engine, device_b_view, expected_version=1, local_snapshot_path=tmp_path / "trade_cards.json")

    # The winning write from device A must still be intact.
    final = repo.get_trade_card(engine, "PROD", "1", "AAPL")
    assert final.board_status == BoardStatus.BUYLIST
    assert final.version == 2


def test_update_missing_card_raises_not_found(tmp_path):
    engine = _make_engine(tmp_path)
    with pytest.raises(repo.TradeCardNotFoundError):
        repo.update_trade_card(engine, _card(), expected_version=1, local_snapshot_path=tmp_path / "trade_cards.json")


def test_list_trade_cards_filters_by_account(tmp_path):
    engine = _make_engine(tmp_path)
    repo.create_trade_card(engine, _card(symbol="AAPL", account_no="1"), local_snapshot_path=tmp_path / "trade_cards.json")
    repo.create_trade_card(engine, _card(symbol="MSFT", account_no="2"), local_snapshot_path=tmp_path / "trade_cards.json")
    assert {c.symbol for c in repo.list_trade_cards(engine, account_no="1")} == {"AAPL"}
    assert {c.symbol for c in repo.list_trade_cards(engine)} == {"AAPL", "MSFT"}


# --- Local snapshot ----------------------------------------------------------


def test_local_snapshot_round_trip(tmp_path):
    snapshot_path = tmp_path / "trade_cards.json"
    cards = [_card(symbol="AAPL"), _card(symbol="MSFT", account_no="2")]
    repo.save_local_trade_cards_snapshot(cards, path=snapshot_path)
    restored = repo.load_local_trade_cards_snapshot(path=snapshot_path)
    assert {c.symbol for c in restored} == {"AAPL", "MSFT"}


def test_sync_local_snapshot_from_database(tmp_path):
    engine = _make_engine(tmp_path)
    snapshot_path = tmp_path / "trade_cards.json"
    repo.create_trade_card(engine, _card(), local_snapshot_path=tmp_path / "trade_cards.json")
    cards = repo.list_trade_cards(engine)
    repo.save_local_trade_cards_snapshot(cards, path=snapshot_path)
    restored = repo.load_local_trade_cards_snapshot(path=snapshot_path)
    assert len(restored) == 1
    assert restored[0].symbol == "AAPL"


def test_create_trade_card_writes_local_snapshot_immediately(tmp_path):
    """Code review finding P1-13: the snapshot must update on every
    successful write, not only via an explicit migration sync call."""
    engine = _make_engine(tmp_path)
    snapshot_path = tmp_path / "trade_cards.json"
    repo.create_trade_card(engine, _card(), local_snapshot_path=snapshot_path)

    restored = repo.load_local_trade_cards_snapshot(path=snapshot_path)
    assert len(restored) == 1
    assert restored[0].symbol == "AAPL"


def test_update_trade_card_refreshes_local_snapshot_immediately(tmp_path):
    engine = _make_engine(tmp_path)
    snapshot_path = tmp_path / "trade_cards.json"
    repo.create_trade_card(engine, _card(), local_snapshot_path=snapshot_path)
    fetched = repo.get_trade_card(engine, "PROD", "1", "AAPL")
    fetched.board_status = BoardStatus.BUYLIST

    repo.update_trade_card(
        engine, fetched, expected_version=fetched.version, local_snapshot_path=snapshot_path
    )

    restored = repo.load_local_trade_cards_snapshot(path=snapshot_path)
    assert len(restored) == 1
    assert restored[0].board_status == BoardStatus.BUYLIST
    assert restored[0].version == 2


def test_snapshot_write_is_a_per_card_upsert_not_a_full_replace(tmp_path):
    """A write for one symbol must not clobber another symbol's entry
    already in the snapshot."""
    engine = _make_engine(tmp_path)
    snapshot_path = tmp_path / "trade_cards.json"
    repo.create_trade_card(
        engine, _card(symbol="AAPL"), local_snapshot_path=snapshot_path
    )
    repo.create_trade_card(
        engine, _card(symbol="MSFT"), local_snapshot_path=snapshot_path
    )

    restored = repo.load_local_trade_cards_snapshot(path=snapshot_path)
    assert {c.symbol for c in restored} == {"AAPL", "MSFT"}


def test_local_snapshot_write_falls_back_to_module_default_path(tmp_path, monkeypatch):
    """No explicit local_snapshot_path -- must resolve LOCAL_TRADE_CARDS_FILE
    dynamically at call time (so a monkeypatch of the module attribute is
    honored), not from a value frozen as a function-default at import time."""
    engine = _make_engine(tmp_path)
    isolated_path = tmp_path / "trade_cards.json"
    monkeypatch.setattr(repo, "LOCAL_TRADE_CARDS_FILE", isolated_path)

    repo.create_trade_card(engine, _card())  # no local_snapshot_path passed

    assert isolated_path.exists()
    restored = repo.load_local_trade_cards_snapshot(path=isolated_path)
    assert len(restored) == 1


# --- Migration from legacy BuylistItem (spec section 25) -------------------


def test_migration_dry_run_does_not_touch_database(tmp_path):
    engine = _make_engine(tmp_path)
    manager = BuylistManager()
    manager.add(_buylist_item(monitoring_status="WATCHING"))

    report = repo.migrate_buylist_to_trade_cards(engine, buylist_manager=manager, apply=False)

    assert len(report.creates) == 1
    assert report.creates[0].card.board_status == BoardStatus.BUYLIST
    assert report.creates[0].card.risk_percent == pytest.approx(0.01)
    assert repo.list_trade_cards(engine) == []


def test_migration_converts_buylist_percentage_points_to_card_fraction(tmp_path):
    engine = _make_engine(tmp_path)
    manager = BuylistManager()
    manager.add(_buylist_item(risk_percent=0.5))

    report = repo.migrate_buylist_to_trade_cards(
        engine, buylist_manager=manager, apply=False
    )

    assert report.creates[0].card.risk_percent == pytest.approx(0.005)


def test_migration_apply_persists_and_is_idempotent(tmp_path):
    engine = _make_engine(tmp_path)
    manager = BuylistManager()
    manager.add(_buylist_item(symbol="AAPL", monitoring_status="BOUGHT", shares_held=100, avg_cost=150.0))
    manager.add(_buylist_item(symbol="MSFT", monitoring_status="WATCHING"))

    first = repo.migrate_buylist_to_trade_cards(engine, buylist_manager=manager, apply=True, local_snapshot_path=tmp_path / "trade_cards.json")
    assert len(first.creates) == 2
    stored = {c.symbol: c for c in repo.list_trade_cards(engine)}
    assert stored["AAPL"].board_status == BoardStatus.OPEN_POSITION
    assert stored["AAPL"].broker_quantity == 100
    assert stored["MSFT"].board_status == BoardStatus.BUYLIST

    # Running the same migration again must not create duplicate rows nor
    # bump versions for unchanged cards.
    second = repo.migrate_buylist_to_trade_cards(engine, buylist_manager=manager, apply=True, local_snapshot_path=tmp_path / "trade_cards.json")
    assert len(second.creates) == 0
    assert len(second.updates) == 0
    stored_again = repo.list_trade_cards(engine)
    assert len(stored_again) == 2
    assert stored_again[0].version == 1 or stored_again[1].version == 1


def test_migration_reflects_status_change_as_update(tmp_path):
    engine = _make_engine(tmp_path)
    manager = BuylistManager()
    manager.add(_buylist_item(symbol="AAPL", monitoring_status="WATCHING"))
    repo.migrate_buylist_to_trade_cards(engine, buylist_manager=manager, apply=True, local_snapshot_path=tmp_path / "trade_cards.json")

    manager.get("AAPL", "PROD").monitoring_status = "BOUGHT"
    manager.get("AAPL", "PROD").shares_held = 50
    report = repo.migrate_buylist_to_trade_cards(engine, buylist_manager=manager, apply=True, local_snapshot_path=tmp_path / "trade_cards.json")

    assert len(report.updates) == 1
    updated = repo.get_trade_card(engine, "PROD", "1", "AAPL")
    assert updated.board_status == BoardStatus.OPEN_POSITION
    assert updated.version == 2


def test_migration_respects_watchlist_membership(tmp_path):
    engine = _make_engine(tmp_path)
    watchlist = Watchlist()
    watchlist.add("AAPL", "Apple Inc.")
    manager = BuylistManager()
    manager.add(_buylist_item(symbol="AAPL"))

    report = repo.migrate_buylist_to_trade_cards(
        engine, buylist_manager=manager, watchlist=watchlist, apply=False
    )
    assert report.creates[0].card.watchlist_member is True


def test_collection_revision_changes_without_returning_payloads(tmp_path):
    engine = _make_engine(tmp_path)
    empty = repo.get_trade_card_collection_revision(engine, environment="PROD")
    assert empty.row_count == 0

    repo.create_trade_card(
        engine,
        _card(),
        local_snapshot_path=tmp_path / "trade_cards.json",
    )
    created = repo.get_trade_card_collection_revision(engine, environment="PROD")
    assert created.row_count == 1
    assert created != empty

    card = repo.get_trade_card(engine, "PROD", "1", "AAPL")
    card.name = "Updated"
    repo.update_trade_card(
        engine,
        card,
        expected_version=card.version,
        local_snapshot_path=tmp_path / "trade_cards.json",
    )
    updated = repo.get_trade_card_collection_revision(engine, environment="PROD")
    assert updated.version_sum == created.version_sum + 1
    assert updated != created
