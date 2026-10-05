from __future__ import annotations

import datetime as dt
import threading
from types import SimpleNamespace

import pandas as pd
import pytest

from src.risk.orb_position import OrbSettings
from src.utils.market_calendar import US_MARKET_ZONE
from src.web.intraday_monitor import (
    IntradayMonitor, MonitorContext, YahooMonitorProvider, daily_adr,
    evaluate_monitor_row, merge_monitor_rows, latest_daily_close,
)
from src.services.monitor_equity import publish_monitor_equity, read_monitor_equity


def moment(time="10:05", day="2026-10-02"):
    return dt.datetime.fromisoformat(f"{day}T{time}:00").replace(tzinfo=US_MARKET_ZONE)


def bars(*, high=102, low=100, close=103, until="10:05", day="2026-10-02"):
    index = pd.date_range(moment("09:30", day), moment(until, day), freq="min")
    frame = pd.DataFrame({"Open": 101.0, "High": float(high), "Low": float(low), "Close": 101.0, "Volume": 1000}, index=index)
    frame.loc[frame.index >= moment("10:01", day), ["High", "Close"]] = close
    return frame


def evaluate(frame=None, **kwargs):
    context = kwargs.pop("context", MonitorContext(settings=OrbSettings(), equity=100_000))
    card = kwargs.pop("card", {"symbol": "AAA", "breakout_price": 101, "watchlist_member": True})
    return evaluate_monitor_row(card, bars() if frame is None else frame,
                                now=kwargs.pop("now", moment()), context=context, adr=kwargs.pop("adr", 3.0), **kwargs)


def test_monitor_price_and_position_checks_pass_independently():
    row = evaluate()
    assert row["current_price"] == 103
    assert row["breakout_status"] == "ABOVE"
    assert row["broke_out_today"] is True
    assert len(row["orb"]) == 3
    assert all(window["price_status"] == window["position_status"] == "PASS" for window in row["orb"])
    assert row["orb"][0]["risk_percent"] in (0.25, 0.5, 0.75, 1, 1.25, 1.5, 1.75, 2)


def test_price_pass_does_not_hide_stop_adr_failure():
    row = evaluate(adr=1)
    for window in row["orb"]:
        assert window["price_status"] == "PASS"
        assert window["position_status"] == "FAIL"
        assert "ADR" in window["position_reason"]


def test_waiting_price_can_have_a_valid_position_plan():
    row = evaluate(bars(close=101.5))
    assert row["breakout_status"] == "ABOVE"
    assert all(window["price_status"] == "WAITING" for window in row["orb"])
    assert all(window["position_status"] == "PASS" for window in row["orb"])


def test_orb_confirmation_requires_strict_post_range_price():
    frame = bars(close=102)
    frame.loc[moment("09:30"), "High"] = 103
    row = evaluate(frame)
    assert row["broke_out_today"] is True
    assert all(window["price_status"] == "WAITING" for window in row["orb"])


def test_pulled_back_breakout_and_orb_confirmation_are_retained():
    frame = bars()
    frame.loc[frame.index[-1], "Close"] = 100.5
    row = evaluate(frame)
    assert row["breakout_status"] == "PULLED_BACK"
    assert row["broke_out_today"] is True
    assert all(window["price_status"] == "PASS" for window in row["orb"])


@pytest.mark.parametrize("inputs", [{"fetch_failed": True}, {"now": moment("10:10")}])
def test_stale_quote_preserves_observed_session_pass_without_claiming_current_quote(inputs):
    row = evaluate(**inputs)
    assert row["quote_status"] == "STALE"
    assert row["broke_out_today"] is None
    assert all(window["price_status"] == "PASS" for window in row["orb"])
    assert row["orb_session_date"] == "2026-10-02"


def test_previous_day_data_never_reports_current_passes():
    row = evaluate(frame=bars(day="2026-10-01"))
    assert row["quote_status"] == "STALE"
    assert row["broke_out_today"] is None
    assert all(window["price_status"] == "UNKNOWN" for window in row["orb"])


def test_missing_opening_minutes_and_unfinished_ranges_fail_closed():
    frame = bars().drop(moment("09:32"))
    row = evaluate(frame)
    assert row["orb"][0]["price_status"] == "PASS"
    assert row["orb"][1]["price_status"] == "UNKNOWN"
    assert "missing" in row["orb"][1]["price_reason"]
    row = evaluate(bars(until="09:33"), now=moment("09:33"))
    assert row["orb"][1]["price_status"] == "FORMING"


def test_missing_equity_or_adr_reports_unknown_position_separately():
    row = evaluate(context=MonitorContext(settings=OrbSettings(), position_error="Account equity unavailable"))
    assert row["orb"][0]["price_status"] == "PASS"
    assert row["orb"][0]["position_status"] == "UNKNOWN"
    assert row["orb"][0]["position_reason"] == "Account equity unavailable"
    row = evaluate(adr=None)
    assert row["orb"][0]["position_status"] == "UNKNOWN"


def test_shared_settings_and_small_account_rounding_are_used():
    row = evaluate(context=MonitorContext(settings=OrbSettings(capital_min_percent=96, capital_ideal_percent=98, capital_max_percent=100), equity=100_000))
    assert row["orb"][0]["position_status"] == "FAIL"
    assert "Capital" in row["orb"][0]["position_reason"]
    row = evaluate(context=MonitorContext(settings=OrbSettings(), equity=50))
    assert row["orb"][0]["position_status"] == "FAIL"


def test_breakout_equal_to_orh_fails_passive_position_geometry_only():
    row = evaluate(card={"symbol": "AAA", "breakout_price": 102})
    assert row["orb"][0]["price_status"] == "PASS"
    assert row["orb"][0]["position_status"] == "FAIL"
    assert "execution zone" in row["orb"][0]["position_reason"]


def test_premarket_does_not_confirm_regular_session_breakouts():
    frame = bars(until="09:30")
    frame.index = pd.date_range(moment("08:00"), periods=len(frame), freq="min")
    row = evaluate(frame, now=moment("08:00"))
    assert row["breakout_status"] == "PRE_MARKET"
    assert all(window["price_status"] == "UNKNOWN" for window in row["orb"])


def test_union_includes_all_memberships_and_only_current_drafts():
    plans = [
        {"symbol": "AAA", "watchlist_member": True, "buylist_member": True, "version": 2},
        {"symbol": "BBB", "buylist_member": True, "version": 3},
        {"symbol": "CCC", "buy_today_member": True},
        {"symbol": "DDD", "breakout_price": 10},
    ]
    rows = merge_monitor_rows(plans, [{"symbol": "AAA", "card_version": 2}, {"symbol": "BBB", "card_version": 2}, {"symbol": "ORPHAN"}])
    assert [row["symbol"] for row in rows] == ["AAA", "BBB", "CCC"]
    assert rows[0]["buy_today_member"] is True
    assert not rows[1].get("buy_today_member")


class Provider:
    def __init__(self):
        self.calls = []
        self.failed = False

    def fetch(self, symbols, *, interval, period=None):
        self.calls.append((list(symbols), interval))
        return {} if self.failed or interval == "1d" else {s: bars() for s in symbols}


def test_worker_caches_daily_inputs_and_adds_new_stocks_next_cycle():
    context = MonitorContext(rows=[{"symbol": "AAA"}])
    provider = Provider()
    monitor = IntradayMonitor(lambda: context, enabled=False, provider=provider, now=moment)
    monitor.refresh_once()
    monitor.refresh_once()
    assert provider.calls == [(["AAA"], "1m"), (["AAA"], "1d"), (["AAA"], "1m")]
    context.rows.append({"symbol": "BBB"})
    monitor.refresh_once()
    assert provider.calls[-2:] == [(["AAA", "BBB"], "1m"), (["BBB"], "1d")]
    provider.failed = True
    monitor.refresh_once()
    assert monitor.snapshot()["rows"][0]["quote_status"] == "STALE"


def test_snapshot_reads_never_download_or_start_multiple_workers():
    started, release = threading.Event(), threading.Event()
    calls = []
    def load():
        calls.append(True)
        started.set()
        assert release.wait(5)
        return MonitorContext()
    monitor = IntradayMonitor(load, enabled=True, provider=Provider(), now=moment)
    try:
        assert monitor.snapshot()["rows"] == []
        assert started.wait(2)
        worker = monitor._thread
        for _ in range(20):
            monitor.snapshot()
            monitor.invalidate()
        assert monitor._thread is worker
        assert len(calls) == 1
        assert monitor._next_refresh >= monitor.monotonic()
    finally:
        release.set()
        monitor.close()


def test_overlapping_refresh_is_skipped():
    monitor = IntradayMonitor(lambda: pytest.fail("must not overlap"), enabled=False)
    monitor._refresh_lock.acquire()
    try:
        monitor.refresh_once()
    finally:
        monitor._refresh_lock.release()


def test_daily_adr_uses_completed_bars_only():
    index = pd.bdate_range(end="2026-10-02", periods=23)
    frame = pd.DataFrame({"Close": 100, "High": 103, "Low": 100}, index=index)
    frame.iloc[-1, frame.columns.get_loc("High")] = 200
    assert daily_adr(frame, moment().date()) == pytest.approx(3)


def test_closed_market_downloads_once_and_retries_failures_with_backoff():
    provider = Provider()
    monitor = IntradayMonitor(lambda: MonitorContext(rows=[{"symbol": "AAA"}]), enabled=False,
                              provider=provider, now=lambda: moment("10:05", "2026-10-04"))
    monitor.refresh_once()
    monitor.refresh_once()
    assert len(provider.calls) == 2
    provider.failed = True
    monitor._failed_symbols.add("AAA")
    monitor.refresh_once()
    assert monitor._next_refresh > monitor.monotonic() + 100
    assert monitor.snapshot()["rows"][0]["quote_status"] == "STALE"
    provider.failed = False
    monitor.refresh_once()
    assert monitor.snapshot()["rows"][0]["quote_status"] == "CLOSED"


def test_weekend_keeps_breakout_and_completed_daily_close_when_minutes_are_missing():
    calls = []
    daily = pd.DataFrame({"Open": 100, "High": 104, "Low": 99, "Close": 103},
                         index=pd.bdate_range(end="2026-10-02", periods=25))
    class DailyProvider:
        def fetch(self, symbols, *, interval, period=None):
            calls.append((interval, period))
            return {"AAA": daily} if interval == "1d" else {}
    monitor = IntradayMonitor(lambda: MonitorContext(rows=[{"symbol": "AAA", "breakout_price": 101}]),
                              enabled=False, provider=DailyProvider(), now=lambda: moment("10:05", "2026-10-04"))
    monitor.refresh_once()
    row = monitor.snapshot()["rows"][0]
    assert row["current_price"] == 103
    assert row["breakout_price"] == 101
    assert row["quote_source"] == "DAILY_CLOSE"
    assert row["quote_as_of"] == moment("16:00").isoformat()
    assert row["quote_status"] == "CLOSED"
    assert row["broke_out_today"] is None
    assert row["orb"] == []
    assert calls == [("1m", "5d"), ("1d", None)]
    assert not monitor.snapshot()["error"]
    assert monitor._next_refresh > monitor.monotonic() + 100


def test_daily_fallback_does_not_create_current_intraday_signals():
    daily = pd.DataFrame({"Open": 100, "High": 104, "Low": 99, "Close": 103},
                         index=pd.bdate_range(end="2026-10-01", periods=25))
    class DailyProvider:
        def fetch(self, symbols, *, interval, period=None):
            return {"AAA": daily} if interval == "1d" else {}
    monitor = IntradayMonitor(lambda: MonitorContext(rows=[{"symbol": "AAA", "breakout_price": 101}],
                                                   settings=OrbSettings(), equity=100_000),
                              enabled=False, provider=DailyProvider(), now=moment)
    monitor.refresh_once()
    row = monitor.snapshot()["rows"][0]
    assert row["current_price"] == 103
    assert row["quote_status"] == "STALE"
    assert row["broke_out_today"] is None
    assert row["orb"] == []


def test_completed_daily_quote_uses_early_close_and_excludes_unfinished_bar():
    daily = pd.DataFrame({"Close": [101, 999, 103]},
                         index=pd.to_datetime(["2026-11-25", "2026-11-26", "2026-11-27"]))
    quote = latest_daily_close(daily, moment("12:00", "2026-11-27"))
    assert quote["price"] == 101
    quote = latest_daily_close(daily, moment("13:01", "2026-11-27"))
    assert quote == {"price": 103, "as_of": moment("13:00", "2026-11-27").isoformat()}


def test_daily_close_cannot_replace_newer_after_hours_price():
    from src.web.intraday_monitor import apply_daily_close_fallback
    row = evaluate(bars(until="19:00"), now=moment("19:05"))
    row["current_price"] = 105
    apply_daily_close_fallback(row, {"price": 103, "as_of": moment("16:00").isoformat()}, moment("19:05"))
    assert row["current_price"] == 105
    assert row["quote_source"] == "MINUTE"


def test_adr_outage_recovers_without_minute_by_minute_daily_downloads():
    tick = [0.0]
    provider = Provider()
    monitor = IntradayMonitor(lambda: MonitorContext(rows=[{"symbol": "AAA"}]), enabled=False,
                              provider=provider, now=moment, monotonic=lambda: tick[0])
    monitor.refresh_once()
    tick[0] = 60
    monitor.refresh_once()
    assert len([c for c in provider.calls if c[1] == "1d"]) == 1
    tick[0] = 901
    monitor.refresh_once()
    assert len([c for c in provider.calls if c[1] == "1d"]) == 2


@pytest.mark.parametrize("field_first", [False, True])
def test_yahoo_provider_batches_normalizes_class_symbols_and_bounds_threads(monkeypatch, field_first):
    import yfinance
    calls = []
    def download(symbols, **options):
        calls.append((symbols, options))
        data = pd.concat({s: bars() for s in symbols}, axis=1)
        return data.swaplevel(axis=1) if field_first else data
    monkeypatch.setattr(yfinance, "download", download)
    frames = YahooMonitorProvider().fetch(["BRK.B", "AAA"], interval="1m")
    assert set(frames) == {"BRK.B", "AAA"}
    assert calls[0][0] == ["BRK-B", "AAA"]
    assert calls[0][1]["threads"] == 4
    assert calls[0][1]["auto_adjust"] is False
    assert calls[0][1]["prepost"] is True


def test_yahoo_float_noise_does_not_reject_legal_orb_execution_prices(monkeypatch):
    import yfinance
    frame = bars()
    frame.loc[frame.index < moment("10:01"), "High"] = 102.000007629
    frame.loc[frame.index < moment("10:01"), "Low"] = 99.999992371
    monkeypatch.setattr(yfinance, "download", lambda *_args, **_kwargs: frame)
    normalized = YahooMonitorProvider().fetch(["AAA"], interval="1m")["AAA"]
    assert normalized.High.iloc[0] == 102
    assert normalized.Low.iloc[0] == 100
    assert evaluate(normalized)["orb"][0]["position_status"] == "PASS"


def test_yahoo_batches_cover_every_stock_beyond_one_chunk(monkeypatch):
    import yfinance
    calls = []
    def download(symbols, **options):
        calls.append(symbols)
        return pd.concat({s: bars() for s in symbols}, axis=1)
    monkeypatch.setattr(yfinance, "download", download)
    symbols = [f"T{i}" for i in range(85)]
    assert set(YahooMonitorProvider().fetch(symbols, interval="1m")) == set(symbols)
    assert [len(c) for c in calls] == [40, 40, 5]


def test_equity_projection_preserves_fetch_time_matches_account_and_expires(tmp_path):
    path = tmp_path / "equity.json"
    snapshot = SimpleNamespace(environment="PROD", account_no="test-account", total_equity_usd=100_000, received_at=moment())
    publish_monitor_equity(path, snapshot)
    modified = path.stat().st_mtime_ns
    publish_monitor_equity(path, snapshot)
    assert path.stat().st_mtime_ns == modified
    assert "test-account" not in path.read_text()
    assert read_monitor_equity(path, "PROD", "test-account", moment("10:10")) == (100_000, "")
    assert read_monitor_equity(path, "PROD", "other", moment())[0] is None
    assert read_monitor_equity(path, "PROD", "test-account", moment("10:21"))[0] is None


def test_monitor_endpoint_requires_login_and_sandbox_does_not_contact_yahoo(client, authenticated, services, monkeypatch):
    monkeypatch.setattr(services.monitor.provider, "fetch", lambda *_args, **_kwargs: pytest.fail("Sandbox must not download quotes"))
    result = authenticated.get("/api/v1/intraday-monitor")
    assert result.status_code == 200
    assert result.json()["enabled"] is False
    authenticated.cookies.clear()
    assert client.get("/api/v1/intraday-monitor").status_code == 401


def test_connected_context_reads_shared_bounds_and_account_without_write_authority(web_config, services, tmp_path):
    import json
    from dataclasses import replace
    from sqlalchemy import event, text
    from src.web.monitor_source import load_monitor_context
    from .test_canonical_planning import canonical_source

    source = canonical_source(tmp_path)
    with source.engine.begin() as connection:
        connection.execute(text("CREATE TABLE app_state_sync (state_key TEXT PRIMARY KEY, payload TEXT)"))
        connection.execute(text("INSERT INTO app_state_sync VALUES (:key, :payload)"),
                           {"key": "settings", "payload": json.dumps({"orb_settings": {"capital_min_percent": 12, "capital_ideal_percent": 18, "capital_max_percent": 24}})})
    statements = []
    event.listen(source.engine, "before_cursor_execute", lambda conn, cursor, statement, *args: statements.append(statement))
    config = replace(web_config, mode="CONNECTED", pc_repository_path=str(tmp_path),
                     canonical_planning_reads=True, canonical_planning_writes=False)
    snapshot = SimpleNamespace(environment="PROD", account_no="account-a", total_equity_usd=100_000,
                               received_at=dt.datetime.now(dt.timezone.utc))
    publish_monitor_equity(tmp_path / "data" / "monitor_equity.json", snapshot)
    try:
        context = load_monitor_context(config, services.store, source)
        assert {row["symbol"] for row in context.rows} == {"AAPL", "MSFT", "NVDA"}
        assert context.settings.capital_max_percent == 24
        assert context.equity == 100_000
        assert not context.position_error
        assert statements and all(statement.lstrip().upper().startswith("SELECT") for statement in statements)
    finally:
        source.close()


@pytest.mark.parametrize("bounds", [{"capital_min_percent": 40}, 0, "invalid"])
def test_shared_bounds_failure_preserves_price_monitoring_membership(web_config, services, tmp_path, bounds):
    from dataclasses import replace
    from src.web.monitor_source import load_monitor_context

    source = SimpleNamespace(list_plans=lambda: {"rows": [{"symbol": "AAA", "watchlist_member": True}]},
                             orb_monitor_inputs=lambda: {"account_no": "a", "document": {"orb_settings": bounds}})
    context = load_monitor_context(replace(web_config, mode="CONNECTED", pc_repository_path=str(tmp_path)), services.store, source)
    assert len(context.rows) == 1
    assert context.settings is None
    assert context.position_error == "Shared ORB settings unavailable"


def test_cached_reads_keep_session_history_but_clear_it_next_day(monkeypatch):
    clock = [moment()]
    monitor = IntradayMonitor(lambda: MonitorContext(rows=[{"symbol": "AAA", "breakout_price": 101, "watchlist_member": True}],
                                                   settings=OrbSettings(), equity=100_000),
                              enabled=False, provider=Provider(), now=lambda: clock[0])
    monitor.refresh_once()
    monkeypatch.setattr("src.web.intraday_monitor.evaluate_monitor_row", lambda *_args, **_kwargs: pytest.fail("Cached reads must not calculate ORB"))
    assert monitor.snapshot()["rows"][0]["orb"][0]["price_status"] == "PASS"
    clock[0] = moment("10:10")
    row = monitor.snapshot()["rows"][0]
    assert row["quote_status"] == "STALE"
    assert row["orb"][0]["price_status"] == "PASS"
    clock[0] = moment("10:05", "2026-10-04")
    row = monitor.snapshot()["rows"][0]
    assert row["quote_status"] == "CLOSED"
    assert row["broke_out_today"] is None
    assert row["orb"][0]["price_status"] == "UNKNOWN"


def test_authenticated_monitor_serves_shared_cached_worker_result(authenticated, services):
    completed = threading.Event()
    monitor = services.monitor
    monitor.enabled = True
    monitor.now = moment
    monitor.provider = Provider()
    monitor.load_context = lambda: MonitorContext(rows=[{"symbol": "AAA", "breakout_price": 101, "watchlist_member": True}],
                                                   settings=OrbSettings(), equity=100_000)
    monitor.on_update = completed.set
    assert authenticated.get("/api/v1/intraday-monitor").status_code == 200
    assert completed.wait(3)
    result = authenticated.get("/api/v1/intraday-monitor").json()
    assert result["rows"][0]["current_price"] == 103
    assert result["rows"][0]["orb"][0]["price_status"] == "PASS"
    assert len(monitor.provider.calls) == 2
    assert result["advisory"] is True
