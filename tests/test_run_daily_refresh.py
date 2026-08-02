"""Regression coverage for the unattended daily refresh gate."""
import datetime as dt
import importlib.util
from pathlib import Path


def _load_refresh_module():
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "run_daily_refresh.py"
    spec = importlib.util.spec_from_file_location("run_daily_refresh_for_tests", script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_refresh_gate_retries_hourly_when_daily_data_is_current(monkeypatch):
    refresh = _load_refresh_module()
    expected_date = dt.date(2026, 6, 23)
    tickers = ["SPY", "AAPL"]

    monkeypatch.setattr(refresh, "init_mysql_engine", lambda: object())
    monkeypatch.setattr(refresh, "_refresh_tickers", lambda: tickers)
    monkeypatch.setattr(refresh, "expected_latest_market_data_date", lambda: expected_date)
    monkeypatch.setattr(
        refresh,
        "get_latest_price_history_dates",
        lambda *args, **kwargs: {symbol: dt.datetime(2026, 6, 23) for symbol in tickers},
    )
    monkeypatch.setattr(
        refresh,
        "get_latest_hourly_price_history_timestamps",
        lambda *args, **kwargs: {"SPY": dt.datetime(2026, 6, 23, 20, 30)},
    )

    modes, reason = refresh._refresh_modes_needed()

    assert modes == ["1h"]
    assert "AAPL" in reason


def test_refresh_gate_retries_daily_when_only_some_symbols_are_stale(monkeypatch):
    refresh = _load_refresh_module()
    expected_date = dt.date(2026, 6, 23)
    tickers = ["SPY", "AAPL"]

    monkeypatch.setattr(refresh, "init_mysql_engine", lambda: object())
    monkeypatch.setattr(refresh, "_refresh_tickers", lambda: tickers)
    monkeypatch.setattr(refresh, "expected_latest_market_data_date", lambda: expected_date)
    monkeypatch.setattr(
        refresh,
        "get_latest_price_history_dates",
        lambda *args, **kwargs: {"SPY": dt.datetime(2026, 6, 23)},
    )
    monkeypatch.setattr(
        refresh,
        "get_latest_hourly_price_history_timestamps",
        lambda *args, **kwargs: {symbol: dt.datetime(2026, 6, 23, 20, 30) for symbol in tickers},
    )

    modes, reason = refresh._refresh_modes_needed()

    assert modes == ["1d"]
    assert "AAPL" in reason


def test_refresh_gate_skips_only_when_every_symbol_and_mode_is_current(monkeypatch):
    refresh = _load_refresh_module()
    expected_date = dt.date(2026, 6, 23)
    tickers = ["SPY", "AAPL"]

    monkeypatch.setattr(refresh, "init_mysql_engine", lambda: object())
    monkeypatch.setattr(refresh, "_refresh_tickers", lambda: tickers)
    monkeypatch.setattr(refresh, "expected_latest_market_data_date", lambda: expected_date)
    monkeypatch.setattr(
        refresh,
        "get_latest_price_history_dates",
        lambda *args, **kwargs: {symbol: dt.datetime(2026, 6, 23) for symbol in tickers},
    )
    monkeypatch.setattr(
        refresh,
        "get_latest_hourly_price_history_timestamps",
        lambda *args, **kwargs: {symbol: dt.datetime(2026, 6, 23, 20, 30) for symbol in tickers},
    )

    modes, reason = refresh._refresh_modes_needed()

    assert modes == []
    assert "All 2 refresh symbols are current" in reason
