import datetime as dt
import hashlib
import json
from types import SimpleNamespace

import pandas as pd
import pytest

from src.core.journal_metrics import account_day_metrics, market_day_move
from src.core.trade_journal import NY_ZONE
from src.services.daily_journal import (
    approved_daily_publication, confirm_daily_journal, daily_journal_review_state,
    prepare_daily_journal_packet, save_daily_journal_draft,
)
from src.services.journal_context import (
    confirm_journal_cash_flow, fetch_journal_prices, fetch_journal_yields,
    journal_context_html, load_journal_context, record_journal_nav, sync_journal_market_day,
)
from src.services.trade_journal import TradeJournalStore

DAY = "2026-10-08"
PREVIOUS = "2026-10-07"


def snapshot(day=DAY, time="16:05", equity=100_000, account="private-account", environment="PROD"):
    return SimpleNamespace(account_no=account, environment=environment, total_equity_usd=equity,
                           received_at=dt.datetime.fromisoformat(f"{day}T{time}").replace(tzinfo=NY_ZONE),
                           source="synthetic broker read")


def seed_nav(store, *, previous=100_000, current=101_000):
    record_journal_nav(snapshot(PREVIOUS, equity=previous), store)
    record_journal_nav(snapshot(equity=current), store)
    return load_journal_context(store, DAY)["accounts"][0]["account_key"]


def market_loader(day, symbols):
    return {symbol: {"values": {PREVIOUS: 100, DAY: 101}, "source": "synthetic closes"} for symbol in symbols}


def after_close(day=DAY):
    return dt.datetime.fromisoformat(f"{day}T16:30").replace(tzinfo=NY_ZONE)


def test_post_close_nav_is_frozen_with_true_fetch_time_and_opaque_account(tmp_path):
    store = TradeJournalStore(tmp_path)
    record_journal_nav(snapshot(time="15:45", equity=90_000), store)
    record_journal_nav(snapshot(equity=100_000), store)
    record_journal_nav(snapshot(time="16:10", equity=102_000), store)
    account = load_journal_context(store, DAY)["accounts"][0]
    assert account["nav_usd"] == 100_000
    assert account["observation"]["observed_at"].endswith("16:05:00-04:00")
    assert "private-account" not in json.dumps(account)
    assert account["account_key"] == hashlib.sha256(b"PROD:private-account").hexdigest()[:16]


def test_intraday_nav_never_becomes_final_pnl(tmp_path):
    store = TradeJournalStore(tmp_path)
    record_journal_nav(snapshot(PREVIOUS), store)
    record_journal_nav(snapshot(time="10:00", equity=99_000), store)
    record_journal_nav(snapshot(time="09:45", equity=98_000), store)
    result = load_journal_context(store, DAY)["accounts"][0]
    assert result["nav_usd"] == 99_000
    assert result["daily_pnl_usd"] is None
    assert "Intraday" in result["status"]


@pytest.mark.parametrize("day,time", [(DAY, "09:00"), (DAY, "17:01"), ("2026-10-10", "16:05")])
def test_premarket_late_and_non_session_reads_are_not_falsely_eod(tmp_path, day, time):
    store = TradeJournalStore(tmp_path)
    record_journal_nav(snapshot(day, time), store)
    assert load_journal_context(store, day)["accounts"] == []


def test_early_close_and_korean_timestamp_map_to_ny_session(tmp_path):
    store = TradeJournalStore(tmp_path)
    value = snapshot("2026-11-27", "13:05")
    value.received_at = value.received_at.astimezone(dt.timezone(dt.timedelta(hours=9)))
    record_journal_nav(value, store)
    assert value.received_at.date().isoformat() == "2026-11-28"
    assert load_journal_context(store, "2026-11-27")["accounts"][0]["observation"]["stage"] == "post_close"


def test_accounts_and_environments_are_not_combined(tmp_path):
    store = TradeJournalStore(tmp_path)
    seed_nav(store)
    record_journal_nav(snapshot(account="second", equity=5000), store)
    record_journal_nav(snapshot(environment="VTS", equity=8000), store)
    accounts = load_journal_context(store, DAY)["accounts"]
    assert len(accounts) == 3
    assert sorted(row["nav_usd"] for row in accounts) == [5000, 8000, 101000]
    assert {row["environment"] for row in accounts} == {"PROD", "VTS"}


def test_unknown_cash_flows_do_not_become_profit_and_deposit_is_removed(tmp_path):
    store = TradeJournalStore(tmp_path)
    key = seed_nav(store, current=111_000)
    account = load_journal_context(store, DAY)["accounts"][0]
    assert account["nav_change_usd"] == 11_000
    assert account["daily_pnl_usd"] is None
    confirm_journal_cash_flow(store, DAY, account_key=key, net_external_flow_usd=10_000,
                             confirmation="I deposited $10,000 between these observations.")
    account = load_journal_context(store, DAY)["accounts"][0]
    assert account["daily_pnl_usd"] == 1000
    assert account["daily_return_pct"] == 1


def test_withdrawal_and_explicit_zero_are_handled(tmp_path):
    store = TradeJournalStore(tmp_path)
    key = seed_nav(store, current=94_000)
    confirm_journal_cash_flow(store, DAY, account_key=key, net_external_flow_usd=-5000,
                             confirmation="I withdrew $5,000.")
    account = load_journal_context(store, DAY)["accounts"][0]
    assert account["daily_pnl_usd"] == -1000
    assert account["daily_return_pct"] == -1
    confirm_journal_cash_flow(store, DAY, account_key=key, net_external_flow_usd=0,
                             confirmation="Correction: no external flows in this period.")
    assert load_journal_context(store, DAY)["accounts"][0]["daily_pnl_usd"] == -6000


def test_missing_previous_session_is_not_replaced_with_older_nav(tmp_path):
    store = TradeJournalStore(tmp_path)
    record_journal_nav(snapshot("2026-10-06"), store)
    record_journal_nav(snapshot(equity=101_000), store)
    row = load_journal_context(store, DAY)["accounts"][0]
    assert row["previous_nav_usd"] is None
    assert row["daily_pnl_usd"] is None
    with pytest.raises(ValueError, match="Both sessions"):
        confirm_journal_cash_flow(store, DAY, account_key=row["account_key"], net_external_flow_usd=0, confirmation="No flows")


def test_flow_confirmation_does_not_apply_to_changed_nav():
    previous = {"nav_usd": 100, "stage": "post_close", "observed_at": "old"}
    current = {"nav_usd": 110, "stage": "post_close", "observed_at": "new"}
    flow = {"net_external_flow_usd": 0, "confirmation": "none", "previous_observed_at": "old", "current_observed_at": "other"}
    assert account_day_metrics(current, previous, flow)["daily_pnl_usd"] is None


def test_yield_movement_is_basis_points_not_percent_return():
    move = market_day_move(4.32, 4.2, unit="yield_percent")
    assert move["change_bps"] == pytest.approx(12)
    assert move["change_pct"] is None
    assert market_day_move(101, 100)["change_pct"] == pytest.approx(1)
    assert market_day_move(float("nan"), 100)["change_pct"] is None
    assert market_day_move(.1, 0, unit="yield_percent")["change_bps"] == pytest.approx(10)
    assert market_day_move(-1, 100)["close"] is None


def test_market_capture_requires_both_exact_session_closes(tmp_path):
    store = TradeJournalStore(tmp_path)
    def missing_previous(day, symbols):
        return {"SPY": {"values": {"2026-10-06": 100, DAY: 102, "2026-10-09": 103}}}
    sync_journal_market_day(store, DAY, now=after_close(), allow_network=True, loader=missing_previous)
    spy = load_journal_context(store, DAY)["market"][0]
    assert spy["close"] == 102
    assert spy["change_pct"] is None
    assert "unavailable" in journal_context_html(load_journal_context(store, DAY))


def test_partial_market_values_remain_stable_and_retry_missing_baseline(tmp_path):
    store = TradeJournalStore(tmp_path)
    def only_current(day, symbols):
        return {"DGS10": {"values": {DAY: 4.32}, "source": "synthetic rates"}}
    sync_journal_market_day(store, DAY, now=after_close(), allow_network=True, loader=only_current)
    before = load_journal_context(store, DAY)
    sync_journal_market_day(store, DAY, now=after_close() + dt.timedelta(hours=1), allow_network=True, loader=only_current)
    assert load_journal_context(store, DAY) == before
    def complete(day, symbols):
        return {"DGS10": {"values": {PREVIOUS: 4.2, DAY: 4.4}, "source": "synthetic rates"}}
    sync_journal_market_day(store, DAY, now=after_close(), force=True, allow_network=True, loader=complete)
    rate = next(row for row in load_journal_context(store, DAY)["market"] if row["symbol"] == "DGS10")
    assert rate["close"] == 4.32  # The earlier observed level is preserved.
    assert rate["change_bps"] == pytest.approx(12)


def test_retry_gate_fills_missing_rows_and_freezes_captured_values(tmp_path):
    store = TradeJournalStore(tmp_path)
    calls = []
    def partial(day, symbols):
        calls.append(symbols)
        return {"SPY": market_loader(day, ["SPY"])["SPY"]}
    sync_journal_market_day(store, DAY, now=after_close(), allow_network=True, loader=partial)
    sync_journal_market_day(store, DAY, now=after_close() + dt.timedelta(minutes=1), allow_network=True, loader=partial)
    assert len(calls) == 1
    sync_journal_market_day(store, DAY, now=after_close(), force=True, allow_network=True, loader=market_loader)
    context = load_journal_context(store, DAY)
    assert all(row["close"] is not None for row in context["market"])
    assert "Russell 1000" in next(row for row in context["market"] if row["symbol"] == "IWB")["name"]
    assert (store.root / "days" / DAY / "market.json").is_file()
    before = json.dumps(context)
    sync_journal_market_day(store, DAY, now=after_close(), force=True, allow_network=True, loader=lambda *args: pytest.fail("Already complete"))
    assert json.dumps(load_journal_context(store, DAY)) == before


def test_no_network_before_close_or_when_disabled(tmp_path):
    store = TradeJournalStore(tmp_path)
    def fail(*args):
        pytest.fail("Network must stay disabled")
    sync_journal_market_day(store, DAY, now=snapshot(time="15:59").received_at, allow_network=True, loader=fail)
    sync_journal_market_day(store, DAY, now=after_close(), allow_network=False, loader=fail)


def test_yahoo_download_uses_unadjusted_close_and_bounded_exact_dates(monkeypatch):
    import yfinance
    seen = {}
    def download(symbols, **kwargs):
        seen.update(kwargs)
        return pd.DataFrame({("Close", "SPY"): [100, 101], ("Adj Close", "SPY"): [90, 92],
                             ("Close", "QQQ"): [200, 204]}, index=pd.to_datetime([PREVIOUS, DAY], utc=True))
    monkeypatch.setattr(yfinance, "download", download)
    rows = fetch_journal_prices(["SPY", "QQQ"], dt.date.fromisoformat(DAY))
    assert rows["SPY"]["values"][DAY] == 101
    assert seen["auto_adjust"] is False
    assert seen["start"] == PREVIOUS
    assert seen["end"] == "2026-10-09"
    assert seen["timeout"] == 8


def test_fred_csv_uses_direct_yield_percent_units(monkeypatch):
    import requests
    def get(url, *, params, timeout):
        assert url.startswith("https://fred.stlouisfed.org/")
        assert params["id"] == "DGS2,DGS10"
        assert timeout == 10
        return SimpleNamespace(raise_for_status=lambda: None, text=f"observation_date,DGS2,DGS10\n{PREVIOUS},4.10,4.20\n{DAY},.,4.32\n")
    monkeypatch.setattr(requests, "get", get)
    result = fetch_journal_yields(dt.date.fromisoformat(DAY))
    assert result["DGS10"]["values"][DAY] == 4.32
    assert DAY not in result["DGS2"]["values"]


def test_price_feed_failure_does_not_drop_available_yields(monkeypatch, tmp_path):
    from src.services import journal_context
    monkeypatch.setattr(journal_context, "fetch_journal_prices", lambda *args: (_ for _ in ()).throw(OSError("offline")))
    monkeypatch.setattr(journal_context, "fetch_journal_yields", lambda day: {"DGS10": {"values": {PREVIOUS: 4.2, DAY: 4.32}}})
    store = TradeJournalStore(tmp_path)
    sync_journal_market_day(store, DAY, now=after_close(), allow_network=True)
    rows = {row["symbol"]: row for row in load_journal_context(store, DAY)["market"]}
    assert rows["DGS10"]["change_bps"] == pytest.approx(12)
    assert rows["SPY"]["close"] is None


def test_packet_saved_html_and_daily_benchmark_comparison_include_context(tmp_path):
    store = TradeJournalStore(tmp_path)
    key = seed_nav(store, current=102000)
    confirm_journal_cash_flow(store, DAY, account_key=key, net_external_flow_usd=0, confirmation="No deposits or withdrawals")
    sync_journal_market_day(store, DAY, now=after_close(), allow_network=True, loader=market_loader)
    path = prepare_daily_journal_packet(store, DAY)
    context = json.loads((path / "trading_day.json").read_text())["daily_context"]
    assert context["accounts"][0]["excess_return_pp"]["SPY"] == pytest.approx(1)
    entry = save_daily_journal_draft(store, DAY, comments="Quiet day", draft="I reviewed the market today.")
    text = (store.root / "days" / DAY / "journal.html").read_text(encoding="utf-8")
    assert "Russell 1000" in text
    assert "$102,000.00" in text
    assert "bps" in text
    assert entry["state"] == "AWAITING_CONFIRMATION"
    assert "liabilities" in journal_context_html(context)


def test_new_market_evidence_invalidates_approved_draft(tmp_path):
    store = TradeJournalStore(tmp_path)
    entry = save_daily_journal_draft(store, DAY, comments="Quiet", draft="I stayed out today.")
    confirm_daily_journal(store, DAY, revision_id=entry["revision_id"], confirmation="Synthetic test approval", destinations=["test"])
    sync_journal_market_day(store, DAY, now=after_close(), allow_network=True, loader=market_loader)
    assert daily_journal_review_state(store, DAY) == "DRAFT_NEEDS_UPDATE"
    with pytest.raises(ValueError, match="facts or charts changed"):
        approved_daily_publication(store, DAY)


def test_nav_archive_failure_preserves_live_monitor_read(monkeypatch, tmp_path):
    from src.services import journal_context, monitor_equity
    monkeypatch.setenv("TRADE_JOURNAL_ENABLED", "true")
    monkeypatch.setattr(journal_context, "record_journal_nav", lambda *args: (_ for _ in ()).throw(OSError("disk unavailable")))
    path = tmp_path / "monitor.json"
    snap = snapshot()
    monitor_equity.publish_monitor_equity(path, snap)
    assert monitor_equity.read_monitor_equity(path, "PROD", "private-account", snap.received_at)[0] == 100000
