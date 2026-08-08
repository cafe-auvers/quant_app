"""Regression coverage for the unattended daily refresh gate."""
import datetime as dt
import importlib.util
from pathlib import Path
from types import SimpleNamespace


def _load_refresh_module():
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "run_daily_refresh.py"
    spec = importlib.util.spec_from_file_location("run_daily_refresh_for_tests", script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _set_derived_current(monkeypatch, refresh):
    """Keep legacy gate tests focused on daily/hourly price freshness."""
    monkeypatch.setattr(
        refresh, "get_chart_indicator_refresh_plan", lambda *args, **kwargs: {}
    )
    monkeypatch.setattr(
        refresh, "is_scanner_metrics_snapshot_current", lambda *args, **kwargs: True
    )


def test_refresh_gate_retries_hourly_when_daily_data_is_current(monkeypatch):
    refresh = _load_refresh_module()
    expected_date = dt.date(2026, 6, 23)
    tickers = ["SPY", "AAPL"]

    monkeypatch.setattr(refresh, "init_mysql_engine", lambda: object())
    monkeypatch.setattr(refresh, "_refresh_tickers", lambda: tickers)
    monkeypatch.setattr(refresh, "expected_latest_market_data_date", lambda: expected_date)
    monkeypatch.setattr(refresh, "get_chronically_failing_symbols", lambda *args, **kwargs: set())
    _set_derived_current(monkeypatch, refresh)
    monkeypatch.setattr(
        refresh,
        "get_price_history_watermarks",
        lambda *args, **kwargs: {
            symbol: (dt.datetime(2026, 6, 23), 250) for symbol in tickers
        },
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
    monkeypatch.setattr(refresh, "get_chronically_failing_symbols", lambda *args, **kwargs: set())
    _set_derived_current(monkeypatch, refresh)
    monkeypatch.setattr(
        refresh,
        "get_price_history_watermarks",
        lambda *args, **kwargs: {"SPY": (dt.datetime(2026, 6, 23), 250)},
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
    monkeypatch.setattr(refresh, "get_chronically_failing_symbols", lambda *args, **kwargs: set())
    _set_derived_current(monkeypatch, refresh)
    monkeypatch.setattr(
        refresh,
        "get_price_history_watermarks",
        lambda *args, **kwargs: {
            symbol: (dt.datetime(2026, 6, 23), 250) for symbol in tickers
        },
    )
    monkeypatch.setattr(
        refresh,
        "get_latest_hourly_price_history_timestamps",
        lambda *args, **kwargs: {symbol: dt.datetime(2026, 6, 23, 20, 30) for symbol in tickers},
    )

    modes, reason = refresh._refresh_modes_needed()

    assert modes == []
    assert "All 2 refresh symbols and derived caches are current" in reason


def test_refresh_gate_skips_when_only_chronically_failing_symbols_are_stale(monkeypatch):
    """A permanently-unfetchable ticker (delisted, unsupported preferred-share
    class, etc.) must not force a full-universe refresh on every single PC
    restart forever once it's crossed the chronic-failure threshold."""
    refresh = _load_refresh_module()
    expected_date = dt.date(2026, 6, 23)
    tickers = ["SPY", "AAPL", "GDV-H"]

    monkeypatch.setattr(refresh, "init_mysql_engine", lambda: object())
    monkeypatch.setattr(refresh, "_refresh_tickers", lambda: tickers)
    monkeypatch.setattr(refresh, "expected_latest_market_data_date", lambda: expected_date)
    monkeypatch.setattr(
        refresh, "get_chronically_failing_symbols", lambda *args, **kwargs: {"GDV-H"}
    )
    _set_derived_current(monkeypatch, refresh)
    monkeypatch.setattr(
        refresh,
        "get_price_history_watermarks",
        lambda *args, **kwargs: {
            "SPY": (dt.datetime(2026, 6, 23), 250),
            "AAPL": (dt.datetime(2026, 6, 23), 250),
        },
    )
    monkeypatch.setattr(
        refresh,
        "get_latest_hourly_price_history_timestamps",
        lambda *args, **kwargs: {symbol: dt.datetime(2026, 6, 23, 20, 30) for symbol in tickers},
    )

    modes, reason = refresh._refresh_modes_needed()

    assert modes == []
    assert "1 1D / 0 1H chronic symbol" in reason


def test_refresh_gate_still_refreshes_non_chronic_stale_symbols(monkeypatch):
    """A chronically-failing symbol shouldn't mask a genuinely stale one."""
    refresh = _load_refresh_module()
    expected_date = dt.date(2026, 6, 23)
    tickers = ["SPY", "AAPL", "GDV-H"]

    monkeypatch.setattr(refresh, "init_mysql_engine", lambda: object())
    monkeypatch.setattr(refresh, "_refresh_tickers", lambda: tickers)
    monkeypatch.setattr(refresh, "expected_latest_market_data_date", lambda: expected_date)
    monkeypatch.setattr(
        refresh, "get_chronically_failing_symbols", lambda *args, **kwargs: {"GDV-H"}
    )
    _set_derived_current(monkeypatch, refresh)
    monkeypatch.setattr(
        refresh,
        "get_price_history_watermarks",
        lambda *args, **kwargs: {"SPY": (dt.datetime(2026, 6, 23), 250)},
    )
    monkeypatch.setattr(
        refresh,
        "get_latest_hourly_price_history_timestamps",
        lambda *args, **kwargs: {symbol: dt.datetime(2026, 6, 23, 20, 30) for symbol in tickers},
    )

    modes, reason = refresh._refresh_modes_needed()

    assert modes == ["1d"]
    assert "AAPL" in reason
    assert "GDV-H" not in reason.split("(1 1D")[0]


def test_refresh_gate_keeps_reference_symbol_actionable_during_broad_outage(monkeypatch):
    """A provider-wide outage must not quarantine every possible refresh trigger."""
    refresh = _load_refresh_module()
    expected_date = dt.date(2026, 6, 23)
    tickers = ["SPY", "AAPL"]

    monkeypatch.setattr(refresh, "init_mysql_engine", lambda: object())
    monkeypatch.setattr(refresh, "_refresh_tickers", lambda: tickers)
    monkeypatch.setattr(refresh, "expected_latest_market_data_date", lambda: expected_date)
    monkeypatch.setattr(
        refresh, "get_chronically_failing_symbols", lambda *args, **kwargs: set(tickers)
    )
    _set_derived_current(monkeypatch, refresh)
    monkeypatch.setattr(refresh, "get_price_history_watermarks", lambda *args, **kwargs: {})
    monkeypatch.setattr(
        refresh, "get_latest_hourly_price_history_timestamps", lambda *args, **kwargs: {}
    )

    modes, reason = refresh._refresh_modes_needed()

    assert modes == ["1d", "1h"]
    assert "1D data is stale for 1" in reason
    assert "1H data is stale for 1" in reason
    assert "SPY" in reason.split("(1 1D")[0]
    assert "1 1D / 1 1H chronic symbol" in reason


def test_refresh_gate_passes_only_stale_symbols_to_each_mode(monkeypatch):
    refresh = _load_refresh_module()
    expected_date = dt.date(2026, 6, 23)
    tickers = ["SPY", "AAPL", "MSFT", "GDV-H"]

    monkeypatch.setattr(refresh, "init_mysql_engine", lambda: object())
    monkeypatch.setattr(refresh, "_refresh_tickers", lambda: tickers)
    monkeypatch.setattr(refresh, "expected_latest_market_data_date", lambda: expected_date)
    monkeypatch.setattr(
        refresh, "get_chronically_failing_symbols", lambda *args, **kwargs: {"GDV-H"}
    )
    _set_derived_current(monkeypatch, refresh)
    monkeypatch.setattr(
        refresh,
        "get_price_history_watermarks",
        lambda *args, **kwargs: {
            "SPY": (dt.datetime(2026, 6, 23), 250),
            "MSFT": (dt.datetime(2026, 6, 23), 250),
        },
    )
    monkeypatch.setattr(
        refresh,
        "get_latest_hourly_price_history_timestamps",
        lambda *args, **kwargs: {
            "SPY": dt.datetime(2026, 6, 23, 20, 30),
            "AAPL": dt.datetime(2026, 6, 23, 20, 30),
            "GDV-H": dt.datetime(2026, 6, 23, 20, 30),
        },
    )

    targets, _ = refresh._refresh_targets_needed()

    # MSFT and the hourly-current names never enter a yfinance batch. The
    # chronic symbol is retried only because AAPL made the daily run useful.
    assert targets == {"1d": ["AAPL", "GDV-H"], "1h": ["MSFT"]}


def test_refresh_gate_can_resume_derived_work_without_price_downloads(monkeypatch):
    refresh = _load_refresh_module()
    expected_date = dt.date(2026, 6, 23)
    tickers = ["SPY", "AAPL", "MSFT"]
    latest = {symbol: dt.datetime(2026, 6, 23) for symbol in tickers}

    monkeypatch.setattr(refresh, "init_mysql_engine", lambda: object())
    monkeypatch.setattr(refresh, "_refresh_tickers", lambda: tickers)
    monkeypatch.setattr(refresh, "expected_latest_market_data_date", lambda: expected_date)
    monkeypatch.setattr(refresh, "get_chronically_failing_symbols", lambda *args, **kwargs: set())
    monkeypatch.setattr(
        refresh,
        "get_price_history_watermarks",
        lambda *args, **kwargs: {
            symbol: (value, 250) for symbol, value in latest.items()
        },
    )
    monkeypatch.setattr(
        refresh,
        "get_latest_hourly_price_history_timestamps",
        lambda *args, **kwargs: latest,
    )
    monkeypatch.setattr(
        refresh,
        "get_chart_indicator_refresh_plan",
        lambda *args, **kwargs: {"MSFT": dt.datetime(2026, 6, 20)},
    )
    monkeypatch.setattr(
        refresh, "is_scanner_metrics_snapshot_current", lambda *args, **kwargs: True
    )

    targets, reason = refresh._refresh_targets_needed()

    assert targets == {"1d": []}
    assert "Chart indicators need refresh for 1 symbol" in reason


def test_run_mode_sends_symbols_over_stdin_without_expanding_argv(monkeypatch):
    refresh = _load_refresh_module()
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(refresh.subprocess, "run", fake_run)

    assert refresh._run_mode("1d", ["AAPL", "MSFT"]) == 0
    assert captured["command"][-3:] == ["--mode", "1d", "--symbols-stdin"]
    assert captured["input"] == "AAPL\nMSFT\n"
    assert captured["text"] is True
    assert "AAPL" not in captured["command"]
    assert "MSFT" not in captured["command"]


def test_run_mode_uses_explicit_derived_only_protocol_for_empty_daily_plan(monkeypatch):
    refresh = _load_refresh_module()
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(refresh.subprocess, "run", fake_run)

    assert refresh._run_mode("1d", []) == 0
    assert captured["command"][-3:] == ["--mode", "1d", "--derived-only"]
    assert captured["input"] is None
