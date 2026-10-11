from __future__ import annotations

import datetime as dt
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import create_engine

from src.core.journal_charts import snapshot_bars
from src.core.execution_order_record import BrokerIdentityStatus, ExecutionOrderRecord, ExecutionOrderStatus
from src.core.order_state import OrderIntent, OrderSide
from src.core.trade_card_state import BoardStatus, TradeCardState
from src.core.trade_journal import estimated_gross_results, fill_facts, select_entry_plan
from src.services.daily_trading_summary import record_trade_card_snapshot
from src.services.execution_order_repository import record_execution_order, save_execution_order
from src.services.trade_journal import (
    TradeJournalStore, export_journal_html,
    load_journal_market_data, sync_trade_journal,
)

SESSION = dt.date(2026, 10, 8)
NOW = dt.datetime(2026, 10, 8, 14, 0, tzinfo=dt.timezone.utc)


def order(identity="entry", *, quantity=10, filled=10, price=100, side=OrderSide.BUY):
    return ExecutionOrderRecord(
        environment="PROD", account_no="12345678-01", symbol="TEST",
        side=side, intent=OrderIntent.ENTRY if side == OrderSide.BUY else OrderIntent.MANUAL_EXIT,
        client_order_id=identity, broker_order_id=f"broker-{identity}",
        broker_identity_status=BrokerIdentityStatus.EXACT,
        status=ExecutionOrderStatus.FILLED if filled == quantity else ExecutionOrderStatus.PARTIALLY_FILLED,
        submitted_quantity=quantity, filled_quantity=filled, remaining_quantity=quantity-filled,
        average_fill_price=price, prepared_at="2026-10-08T13:36:00+00:00",
        submission_started_at="2026-10-08T13:36:00+00:00",
        last_broker_seen_at="2026-10-08T13:59:00+00:00", market_session_date=SESSION.isoformat(),
    )


def market_data(symbol, session, captured_at):
    daily = pd.DataFrame({
        "Open": [98, 99, 100], "High": [102, 103, 104], "Low": [96, 97, 98],
        "Close": [99, 100, 102], "Volume": [1000, 1200, 1400],
    }, index=pd.to_datetime(["2026-10-07", "2026-10-08", "2026-10-09"]))
    times = pd.date_range("2026-10-08T13:25:00Z", periods=81, freq="5min")
    intraday = pd.DataFrame({
        "Open": 99., "High": 102., "Low": 98., "Close": 100., "Volume": 1000.,
    }, index=times)
    return {"1d": (daily, "fixture daily"), "5m": (intraday, "fixture intraday")}


@pytest.fixture
def source(tmp_path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'source.sqlite3').as_posix()}")
    yield engine
    engine.dispose()


@pytest.fixture
def store(tmp_path):
    return TradeJournalStore(tmp_path / "journal")


def sync(source, store, **kwargs):
    return sync_trade_journal(source, store=store, market_loader=market_data, now=NOW, **kwargs)


def test_capture_uses_only_confirmed_fills_and_never_submission_price(source, store):
    record_execution_order(source, order(filled=3))
    unfilled = order("unfilled", filled=0, price=0)
    unfilled.status = ExecutionOrderStatus.WORKING
    record_execution_order(source, unfilled)
    result = sync(source, store)
    assert result["changed"] == 1
    trade = result["trades"][0]
    assert trade["quantity"] == 3
    assert trade["average_fill_price"] == 100
    assert trade["broker_execution_at"] is None
    assert "exact broker execution" in trade["time_basis"]
    assert len(list(store.root.rglob("*.svg"))) == 2
    exported = (store.trade_directory(trade) / "trade.json").read_text(encoding="utf-8")
    assert "12345678-01" not in exported
    assert "broker-entry" not in exported
    assert "lease_token" not in exported


def test_repeated_poll_and_restart_do_not_duplicate_fill_observations(source, store):
    record_execution_order(source, order())
    assert sync(source, store)["changed"] == 1
    reopened = TradeJournalStore(store.root)
    assert sync(source, reopened)["changed"] == 0
    with reopened.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 1
    assert len(list(store.root.rglob("observations/*.json"))) == 1


def test_partial_fill_updates_preserve_first_plan_charts_and_notes(source, store):
    entry = record_execution_order(source, order(filled=3))
    card = TradeCardState(
        environment="PROD", account_no=entry.account_no, symbol="TEST",
        board_status=BoardStatus.ENTRY_PENDING, session_date=SESSION,
        selected_orb_window="5m", entry_orb_window="5m", entry_orb_high=101,
        entry_orb_low=98, entry_trigger=101, entry_client_order_id="entry",
        entry_order_generation=2, planned_quantity=10,
    )
    record_trade_card_snapshot(source, card, occurred_at=NOW-dt.timedelta(minutes=25))
    first = sync(source, store)["trades"][0]
    snapshot = store.snapshots(first["journal_id"])
    store.save_notes(first["journal_id"], {"thesis": "Tight base; risk defined first"})
    entry.filled_quantity = 10
    entry.remaining_quantity = 0
    entry.average_fill_price = 100.5
    entry.status = ExecutionOrderStatus.FILLED
    save_execution_order(source, entry, expected_version=entry.version)
    card.entry_orb_window = "30m"
    card.entry_orb_high = 150
    record_trade_card_snapshot(source, card, occurred_at=NOW)
    result = sync(source, store)
    trade = result["trades"][0]
    assert result["changed"] == 1
    assert trade["quantity"] == 10
    assert trade["entry_plan"]["fields"]["entry_orb_window"] == "5m"
    assert trade["entry_plan"]["fields"]["entry_orb_high"] == 101
    assert trade["notes"]["thesis"] == "Tight base; risk defined first"
    assert store.snapshots(trade["journal_id"]) == snapshot
    with store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 2


def test_plan_recovery_uses_earliest_link_after_submission_and_ignores_other_accounts():
    entry = order()
    def event(time, window, account=entry.account_no):
        return {"occurred_at": time, "payload": {"environment": "PROD", "account_no": account,
                "symbol": "TEST", "entry_client_order_id": "entry", "entry_orb_window": window}}
    result = select_entry_plan(entry, [
        event("2026-10-08T13:37:00+00:00", "5m"),
        event("2026-10-08T14:00:00+00:00", "30m"),
        event("2026-10-08T13:36:30+00:00", "1m", "other-account"),
    ])
    assert result["fields"]["entry_orb_window"] == "5m"
    assert result["provenance"] == "order-linked snapshot"


def test_plan_failure_still_archives_fills(source, store, monkeypatch):
    import src.services.trade_journal as module
    record_execution_order(source, order())
    monkeypatch.setattr(module, "load_trade_journal_events", lambda *args: (_ for _ in ()).throw(OSError("plan offline")))
    result = sync(source, store)
    assert len(result["trades"]) == 1
    assert result["errors"] == ["plan offline"]
    assert "missing" in result["trades"][0]["entry_plan"]["provenance"]


def test_chart_failure_preserves_fill_and_retries_after_backoff(source, store):
    record_execution_order(source, order())
    calls = []
    def unavailable(*args):
        calls.append(args)
        raise OSError("chart offline")
    result = sync_trade_journal(source, store=store, market_loader=unavailable, now=NOW)
    assert len(result["trades"]) == 1
    assert result["errors"] == ["chart offline"]
    assert (store.trade_directory(result["trades"][0]) / "trade.json").is_file()
    sync_trade_journal(source, store=store, market_loader=unavailable, now=NOW+dt.timedelta(minutes=1))
    assert len(calls) == 1
    result = sync_trade_journal(source, store=store, market_loader=market_data, now=NOW+dt.timedelta(minutes=16))
    snapshots = store.snapshots(result["trades"][0]["journal_id"])
    assert len(snapshots) == 2
    assert all("recovered view" in row["capture_basis"] for row in snapshots)


def test_completed_five_minute_bars_and_daily_session_dates_have_no_future_rows():
    frames = market_data("TEST", SESSION, NOW)
    five = snapshot_bars(frames["5m"][0], SESSION, "5m", NOW)
    daily = snapshot_bars(frames["1d"][0], SESSION, "1d", NOW)
    assert five[0]["time"].startswith("2026-10-08T13:30")
    assert five[-1]["time"].startswith("2026-10-08T13:55")
    assert len(five) == 6
    assert [row["time"] for row in daily] == ["2026-10-07", "2026-10-08"]
    utc_daily = frames["1d"][0].copy()
    utc_daily.index = utc_daily.index.tz_localize("UTC")
    assert snapshot_bars(utc_daily, SESSION, "1d", NOW) == daily


def test_eod_snapshots_are_distinct_and_preserve_first_observed_view(source, store):
    record_execution_order(source, order())
    trade = sync(source, store)["trades"][0]
    first = store.snapshots(trade["journal_id"])
    eod = dt.datetime(2026, 10, 8, 20, 20, tzinfo=dt.timezone.utc)
    sync_trade_journal(source, store=store, market_loader=market_data, now=eod)
    snapshots = store.snapshots(trade["journal_id"])
    assert len(snapshots) == 4
    assert [row for row in snapshots if row["stage"] == "first_observed"] == first
    full_session = next(row for row in snapshots if row["stage"] == "end_of_day" and row["interval"] == "5m")
    assert len(full_session["bars"]) == 78


def test_early_close_and_dst_use_new_york_session_boundaries():
    session = dt.date(2026, 11, 27)
    index = pd.date_range("2026-11-27T14:30Z", periods=78, freq="5min")
    bars = pd.DataFrame({"Open": 100., "High": 102., "Low": 98., "Close": 101., "Volume": 1.}, index=index)
    result = snapshot_bars(bars, session, "5m", dt.datetime(2026, 11, 27, 21, 0, tzinfo=dt.timezone.utc))
    assert len(result) == 42
    assert result[-1]["time"].startswith("2026-11-27T17:55")


def test_cancelled_partial_fill_is_included_and_invalid_price_is_excluded():
    entry = order(filled=2)
    entry.status = ExecutionOrderStatus.CANCELLED
    assert fill_facts(entry, NOW)["quantity"] == 2
    entry.average_fill_price = float("nan")
    assert fill_facts(entry, NOW) is None


def test_missing_sell_cost_basis_stays_unknown_and_accounts_do_not_mix():
    buy = fill_facts(order(), NOW)
    sell = fill_facts(order("sell", side=OrderSide.SELL, quantity=4, filled=4, price=110), NOW)
    sell["submitted_at"] = "2026-10-08T15:00:00+00:00"
    assert estimated_gross_results([buy, sell])[sell["journal_id"]] == 40
    sell["account_key"] = "other-account"
    assert estimated_gross_results([buy, sell])[sell["journal_id"]] is None


def test_private_archive_export_escapes_notes(source, store, tmp_path):
    record_execution_order(source, order())
    trade = sync(source, store)["trades"][0]
    store.save_notes(trade["journal_id"], {"thesis": "<script>alert('private')</script> Tight base", "lesson": "Follow the stop"})
    trade = store.list_trades()[0]
    destination = export_journal_html(store, tmp_path / "journal.html")
    exported = destination.read_text(encoding="utf-8")
    assert "&lt;script&gt;" in exported
    assert "<script>alert" not in exported
    assert "<svg" in exported
    assert "Exact broker execution time unavailable" in exported
    assert "12345678-01" not in exported


def test_decreased_quantity_is_reported_without_overwriting_prior_evidence(source, store):
    entry = record_execution_order(source, order())
    original = sync(source, store)["trades"][0]
    entry.filled_quantity = 3
    entry.remaining_quantity = 7
    entry.status = ExecutionOrderStatus.PARTIALLY_FILLED
    save_execution_order(source, entry, expected_version=entry.version)
    result = sync(source, store)
    assert "decreased" in result["errors"][0]
    assert result["trades"][0]["quantity"] == original["quantity"] == 10


def test_cache_only_loader_does_not_download(monkeypatch):
    import src.utils.data_loader as data_loader
    monkeypatch.setattr(data_loader, "download_price_history", lambda *args, **kwargs: pytest.fail("Unexpected network call"))
    frames = load_journal_market_data("TEST", SESSION, NOW, allow_network=False)
    assert all(frame.empty for frame, _ in frames.values())


def test_stale_opening_cache_fetches_full_day_for_eod(monkeypatch):
    import src.utils.data_loader as data_loader
    import src.infrastructure.database.repositories.market_bars as repository
    frames = market_data("TEST", SESSION, NOW)
    calls = []
    def downloaded(tickers, **kwargs):
        calls.append(kwargs["interval"])
        return frames[kwargs["interval"]][0]
    monkeypatch.setattr(data_loader, "download_price_history", downloaded)
    monkeypatch.setattr(repository, "load_symbol_history_from_db", lambda *args, **kwargs: frames["1d"][0])
    monkeypatch.setattr(repository, "load_intraday_history_from_db", lambda *args, **kwargs:
                        pd.DataFrame() if kwargs["interval"] == "1m" else frames["5m"][0].iloc[:5])
    eod = dt.datetime(2026, 10, 8, 20, 20, tzinfo=dt.timezone.utc)
    result = load_journal_market_data("TEST", SESSION, eod, market_engine=object(), allow_network=True)
    assert calls == ["1d", "5m"]
    assert len(snapshot_bars(result["5m"][0], SESSION, "5m", eod)) == 78


def test_unattended_helper_and_offline_export_use_explicit_archive(source, store, tmp_path):
    record_execution_order(source, order())
    script = Path(__file__).resolve().parents[1] / "scripts" / "sync_trade_journal.py"
    export = tmp_path / "helper_export.html"
    args = [sys.executable, str(script), "--archive", str(store.root),
            "--source-db", source.url.database, "--no-network", "--export", str(export)]
    result = subprocess.run(args, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert "1 orders" in result.stdout
    assert len(store.list_trades()) == 1
    assert export.is_file()
    result = subprocess.run([
        sys.executable, str(script), "--archive", str(store.root), "--export-only",
        "--session", SESSION.isoformat(), "--export", str(export),
        "--source-db", str(tmp_path / "does_not_exist.sqlite3"),
    ], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
