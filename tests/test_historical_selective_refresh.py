"""Regression tests for selective/restart-safe historical refreshes."""
import io

import pytest

import historical


class _State:
    def __init__(self):
        self.phases = []
        self.completed = []
        self.logs = []
        self.updated_count = None

    def set_phase(self, phase):
        self.phases.append(phase)

    def complete_phase(self, phase):
        self.completed.append(phase)

    def update_progress(self, *args):
        return None

    def log(self, message):
        self.logs.append(message)


@pytest.fixture(autouse=True)
def _disable_supplemental_provider_network(monkeypatch):
    monkeypatch.setattr(
        historical,
        "refresh_nasdaq_universe_stock_profiles",
        lambda *_args, **_kwargs: {"complete": 0, "changed": 0},
    )
    monkeypatch.setattr(
        historical,
        "refresh_universe_upcoming_earnings",
        lambda *_args, **_kwargs: {
            "events": 0,
            "symbols": 0,
            "changed": 0,
            "failed_dates": 0,
        },
    )
    monkeypatch.setattr(
        historical,
        "refresh_universe_earnings_history",
        lambda *_args, **_kwargs: {"attempted": 0},
    )


def test_read_refresh_symbols_from_stdin_normalizes_and_deduplicates(monkeypatch):
    monkeypatch.setattr(
        historical.sys,
        "stdin",
        io.StringIO(" aapl \nMSFT\nAAPL\n\n"),
    )

    assert historical.read_refresh_symbols_from_stdin() == ["AAPL", "MSFT"]


def test_build_refresh_tickers_can_use_cached_universe(monkeypatch):
    calls = []
    monkeypatch.setattr(
        historical,
        "get_default_universe",
        lambda max_symbols=None, refresh=False: calls.append((max_symbols, refresh))
        or [" aapl ", "spy", "AAPL"],
    )

    result = historical.build_refresh_tickers(10, refresh=False)

    assert result == ["SPY", "AAPL"]
    assert calls == [(10, False)]


def test_selective_child_rechecks_and_drops_symbols_that_are_now_current(monkeypatch):
    monkeypatch.setattr(
        historical, "expected_latest_market_data_date", lambda: historical.dt.date(2026, 6, 23)
    )
    monkeypatch.setattr(
        historical,
        "get_latest_price_history_dates",
        lambda *args, **kwargs: {
            "AAPL": historical.dt.datetime(2026, 6, 23),
            "MSFT": historical.dt.datetime(2026, 6, 22),
        },
    )

    assert historical.select_stale_history_symbols(
        object(), ["AAPL", "MSFT", "MISSING"], historical.MODE_1D
    ) == ["MSFT", "MISSING"]


def test_run_1d_downloads_only_stale_payload_but_checks_full_derived_universe(monkeypatch):
    state = _State()
    calls = {}
    watermarks = {
        "SPY": (historical.dt.datetime(2026, 6, 23), 250),
        "AAPL": (historical.dt.datetime(2026, 6, 23), 250),
        "MSFT": (historical.dt.datetime(2026, 6, 23), 250),
    }

    def refresh_history(tickers, engine, **kwargs):
        calls["history"] = list(tickers)
        return list(tickers)

    def refresh_chart(tickers, engine, **kwargs):
        calls["chart"] = (
            list(tickers),
            kwargs["force"],
            kwargs["history_watermarks"],
        )
        return []

    def refresh_scanner(tickers, engine, **kwargs):
        calls["scanner"] = (
            list(tickers),
            kwargs["force"],
            kwargs["history_watermarks"],
        )
        return []

    def chart_plan(*args, **kwargs):
        calls["chart_plan_watermarks"] = kwargs["history_watermarks"]
        return {}

    def scanner_current(*args, **kwargs):
        calls["scanner_current_watermarks"] = kwargs["history_watermarks"]
        return True

    def load_watermarks(*args, **kwargs):
        calls["watermark_kwargs"] = kwargs
        return watermarks

    def refresh_profiles(engine, tickers, **kwargs):
        calls["profiles"] = (list(tickers), kwargs["max_symbols"])
        return {"attempted": 3, "refreshed": 3, "unavailable": 0}

    monkeypatch.setattr(historical, "refresh_universe_history_to_db", refresh_history)
    monkeypatch.setattr(
        historical,
        "get_price_history_watermarks",
        load_watermarks,
    )
    monkeypatch.setattr(historical, "refresh_chart_indicators_to_db", refresh_chart)
    monkeypatch.setattr(historical, "refresh_scanner_metrics_to_db", refresh_scanner)
    monkeypatch.setattr(historical, "get_chart_indicator_refresh_plan", chart_plan)
    monkeypatch.setattr(historical, "is_scanner_metrics_snapshot_current", scanner_current)
    monkeypatch.setattr(
        historical, "refresh_universe_stock_profiles", refresh_profiles
    )

    historical.run_1d(
        object(),
        ["AAPL"],
        state,
        universe_tickers=["SPY", "AAPL", "MSFT"],
    )

    assert calls["history"] == ["AAPL"]
    assert calls["watermark_kwargs"] == {"interval": "1d", "strict": True}
    assert calls["chart"] == (["SPY", "AAPL", "MSFT"], False, watermarks)
    assert calls["chart_plan_watermarks"] is watermarks
    assert calls["scanner"] == (["SPY", "AAPL", "MSFT"], False, watermarks)
    assert calls["scanner_current_watermarks"] is watermarks
    assert calls["profiles"] == (
        ["SPY", "AAPL", "MSFT"],
        historical.PROFILE_REFRESH_BATCH_SIZE,
    )
    assert state.updated_count == 1
    assert state.completed == [
        "daily_history",
        "chart_indicators",
        "scanner_metrics",
        "stock_profiles",
        "earnings_events",
    ]


def test_derived_only_run_never_calls_price_downloader(monkeypatch):
    state = _State()
    derived_calls = []

    def unexpected_download(*args, **kwargs):
        raise AssertionError("price history must not be fetched")

    monkeypatch.setattr(historical, "refresh_universe_history_to_db", unexpected_download)
    monkeypatch.setattr(
        historical, "get_price_history_watermarks", lambda *args, **kwargs: {}
    )
    monkeypatch.setattr(
        historical,
        "refresh_chart_indicators_to_db",
        lambda tickers, *args, **kwargs: derived_calls.append(("chart", list(tickers))) or [],
    )
    monkeypatch.setattr(
        historical,
        "refresh_scanner_metrics_to_db",
        lambda tickers, *args, **kwargs: derived_calls.append(("scanner", list(tickers))) or [],
    )
    monkeypatch.setattr(
        historical, "get_chart_indicator_refresh_plan", lambda *args, **kwargs: {}
    )
    monkeypatch.setattr(
        historical, "is_scanner_metrics_snapshot_current", lambda *args, **kwargs: True
    )

    historical.run_1d(
        object(), [], state, universe_tickers=["SPY", "AAPL", "MSFT"]
    )

    assert derived_calls == [
        ("chart", ["SPY", "AAPL", "MSFT"]),
        ("scanner", ["SPY", "AAPL", "MSFT"]),
    ]
    assert any("skipping downloads" in message for message in state.logs)


def test_run_1d_does_not_mark_failed_derived_cache_complete(monkeypatch):
    state = _State()
    monkeypatch.setattr(
        historical, "get_price_history_watermarks", lambda *args, **kwargs: {}
    )
    monkeypatch.setattr(
        historical, "refresh_chart_indicators_to_db", lambda *args, **kwargs: []
    )
    monkeypatch.setattr(
        historical,
        "get_chart_indicator_refresh_plan",
        lambda *args, **kwargs: {"AAPL": historical.dt.datetime(2026, 6, 23)},
    )

    with pytest.raises(RuntimeError, match="Chart indicators remain incomplete"):
        historical.run_1d(
            object(), [], state, universe_tickers=["SPY", "AAPL"]
        )

    assert "daily_history" in state.completed
    assert "chart_indicators" not in state.completed
    assert "scanner_metrics" not in state.completed


def test_run_1d_does_not_mark_failed_scanner_snapshot_complete(monkeypatch):
    state = _State()
    monkeypatch.setattr(
        historical, "get_price_history_watermarks", lambda *args, **kwargs: {}
    )
    monkeypatch.setattr(
        historical, "refresh_chart_indicators_to_db", lambda *args, **kwargs: []
    )
    monkeypatch.setattr(
        historical, "get_chart_indicator_refresh_plan", lambda *args, **kwargs: {}
    )
    monkeypatch.setattr(
        historical, "refresh_scanner_metrics_to_db", lambda *args, **kwargs: []
    )
    monkeypatch.setattr(
        historical, "is_scanner_metrics_snapshot_current", lambda *args, **kwargs: False
    )

    with pytest.raises(RuntimeError, match="Scanner metrics snapshot remains incomplete"):
        historical.run_1d(
            object(), [], state, universe_tickers=["SPY", "AAPL"]
        )

    assert state.completed == ["daily_history", "chart_indicators"]
