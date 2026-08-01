"""Regression coverage for empty KIS daily-data CLI runs."""
from __future__ import annotations

import pandas as pd

import src.api.kis_fetch_all_daily as daily
import src.utils.logging_config as logging_config


def test_watchlist_fetch_writes_headered_empty_csv(tmp_path, monkeypatch):
    output_path = tmp_path / "watchlist.csv"
    monkeypatch.setattr(daily, "load_watchlist_symbols", lambda: [])
    monkeypatch.setattr(daily, "fetch_watchlist_overseas_daily_bars", lambda **_kwargs: [])

    result = daily.run_watchlist_overseas_fetch("20260105", output_path=str(output_path))

    assert result.empty
    assert result.columns.tolist() == list(daily.OVERSEAS_DAILY_COLUMNS)
    assert pd.read_csv(output_path).columns.tolist() == list(daily.OVERSEAS_DAILY_COLUMNS)


def test_domestic_cli_handles_no_records_without_sort_values_crash(tmp_path, monkeypatch):
    class _Args:
        watchlist_overseas = False
        date = None
        output = None

    class _Client:
        session = object()

        def __init__(self, **_kwargs):
            pass

        def authenticate(self):
            return None

    symbols = pd.DataFrame(
        [{"symbol": "005930", "name": "Samsung", "market": "KOSPI"}]
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(logging_config, "configure_logging", lambda: None)
    monkeypatch.setattr(daily, "parse_args", lambda: _Args())
    monkeypatch.setattr(daily, "KISClient", _Client)
    monkeypatch.setattr(daily, "load_all_domestic_symbols", lambda _session: symbols)
    monkeypatch.setattr(daily, "get_last_market_open_date", lambda _client: pd.Timestamp("2026-01-05").date())
    monkeypatch.setattr(daily, "fetch_one_symbol_daily_bar", lambda **_kwargs: None)
    monkeypatch.setattr(daily.time, "sleep", lambda _seconds: None)

    daily.main()

    output_path = tmp_path / "kis_daily_all_20260105.csv"
    assert output_path.exists()
    assert pd.read_csv(output_path).columns.tolist() == list(daily.DOMESTIC_DAILY_COLUMNS)


def test_missing_watchlist_file_is_treated_as_an_empty_watchlist(tmp_path):
    assert daily.load_watchlist_symbols(tmp_path / "missing.json") == []
