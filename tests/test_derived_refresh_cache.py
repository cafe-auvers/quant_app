import datetime as dt

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import create_engine, delete

import src.utils.db_loader as db_loader


@pytest.fixture
def engine():
    value = create_engine("sqlite:///:memory:", future=True)
    db_loader._ensured_engines.discard(id(value))
    try:
        yield value
    finally:
        db_loader._ensured_engines.discard(id(value))
        value.dispose()


def _history(dates, start_price=100.0):
    size = len(dates)
    close = np.arange(start_price, start_price + size, dtype=float)
    return pd.DataFrame(
        {
            "Open": close - 0.5,
            "High": close + 1.0,
            "Low": close - 1.0,
            "Close": close,
            "Adj Close": close,
            "Volume": np.arange(1_000_000, 1_000_000 + size, dtype=float),
        },
        index=pd.DatetimeIndex(dates),
    )


def _seed_daily_history(engine, dates):
    assert db_loader.save_symbol_history_to_db("SPY", _history(dates, 200), engine)
    assert db_loader.save_symbol_history_to_db("AAPL", _history(dates, 100), engine)


def test_chart_plan_finds_middle_gap_and_refresh_repairs_it(engine, monkeypatch):
    dates = pd.bdate_range("2026-01-05", periods=8)
    _seed_daily_history(engine, dates)
    full = db_loader.calculate_chart_indicators(
        "AAPL", _history(dates, 100), _history(dates, 200)
    )
    missing_date = pd.Timestamp(dates[3]).to_pydatetime()
    partial = full.loc[pd.to_datetime(full["date"]) != pd.Timestamp(missing_date)]
    assert db_loader.save_chart_indicators_to_db("AAPL", partial, engine)

    plan = db_loader.get_chart_indicator_refresh_plan(engine, ["SPY", "AAPL"])

    assert plan == {"AAPL": missing_date}
    assert db_loader.refresh_chart_indicators_to_db(["SPY", "AAPL"], engine) == ["AAPL"]
    assert db_loader.get_chart_indicator_refresh_plan(engine, ["SPY", "AAPL"]) == {}

    monkeypatch.setattr(
        db_loader,
        "load_universe_history_from_db",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("current indicators must not load or calculate history")
        ),
    )
    assert db_loader.refresh_chart_indicators_to_db(["SPY", "AAPL"], engine) == []


def test_chart_plan_does_not_accept_a_latest_only_row_as_complete(engine):
    dates = pd.bdate_range("2026-01-05", periods=5)
    _seed_daily_history(engine, dates)
    indicators = db_loader.calculate_chart_indicators(
        "AAPL", _history(dates, 100), _history(dates, 200)
    )
    assert db_loader.save_chart_indicators_to_db("AAPL", indicators.tail(1), engine)

    plan = db_loader.get_chart_indicator_refresh_plan(engine, ["SPY", "AAPL"])

    assert plan == {"AAPL": pd.Timestamp(dates[0]).to_pydatetime()}


def test_incremental_chart_calculation_matches_full_calculation():
    dates = pd.bdate_range("2024-01-02", periods=360)
    symbol_history = _history(dates, 80)
    spy_dates = dates.delete(np.arange(0, len(dates), 17))
    spy_history = _history(spy_dates, 180)
    full = db_loader.calculate_chart_indicators(
        "AAPL", symbol_history, spy_history
    )
    start_date = pd.Timestamp(full["date"].iloc[-45]).to_pydatetime()
    expected = full.loc[pd.to_datetime(full["date"]) >= pd.Timestamp(start_date)].copy()
    actual = db_loader.calculate_chart_indicators_since(
        "AAPL", symbol_history, spy_history, start_date=start_date
    )
    columns = [column for column in expected.columns if column != "updated_at"]

    pd.testing.assert_frame_equal(
        expected[columns].reset_index(drop=True),
        actual[columns].reset_index(drop=True),
        check_dtype=False,
        check_exact=False,
        rtol=1e-12,
        atol=1e-12,
    )


def test_scanner_snapshot_is_authoritative_and_detects_partial_rows(engine, monkeypatch):
    dates = pd.bdate_range("2026-01-05", periods=5)
    _seed_daily_history(engine, dates)
    assert db_loader.save_symbol_history_to_db("MSFT", _history(dates, 140), engine)
    symbols = ["SPY", "AAPL", "MSFT"]
    watermarks = db_loader.get_price_history_watermarks(engine, symbols)
    snapshot_date = db_loader.scanner_metrics_snapshot_date(dt.date(2026, 1, 9))
    fingerprint = db_loader.scanner_metrics_input_fingerprint(symbols, watermarks)
    saved = db_loader.save_scanner_metrics_snapshot_to_db(
        [
            {"symbol": "AAPL", "return_1m": 1.0},
            {"symbol": "MSFT", "return_1m": 2.0},
        ],
        snapshot_date,
        fingerprint,
        engine,
    )

    assert saved == ["AAPL", "MSFT"]
    assert db_loader.is_scanner_metrics_snapshot_current(
        engine,
        ["AAPL", "MSFT"],
        history_watermarks=watermarks,
        snapshot_date=snapshot_date,
    )

    table = db_loader._ensure_scanner_metrics_table(engine)
    with engine.begin() as conn:
        conn.execute(
            delete(table).where(
                table.c.symbol == "MSFT", table.c.date == snapshot_date
            )
        )

    assert not db_loader.is_scanner_metrics_snapshot_current(
        engine,
        ["AAPL", "MSFT"],
        history_watermarks=watermarks,
        snapshot_date=snapshot_date,
    )


def test_scanner_cache_hit_skips_history_loading_and_calculation(engine, monkeypatch):
    dates = pd.bdate_range("2026-01-05", periods=5)
    _seed_daily_history(engine, dates)
    symbols = ["SPY", "AAPL"]
    watermarks = db_loader.get_price_history_watermarks(engine, symbols)
    snapshot_date = db_loader.scanner_metrics_snapshot_date()
    fingerprint = db_loader.scanner_metrics_input_fingerprint(symbols, watermarks)
    assert db_loader.save_scanner_metrics_snapshot_to_db(
        [{"symbol": "AAPL", "return_1m": 1.0}],
        snapshot_date,
        fingerprint,
        engine,
    ) == ["AAPL"]

    monkeypatch.setattr(
        db_loader,
        "load_universe_history_from_db",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("current scanner metrics must not load history")
        ),
    )

    assert db_loader.refresh_scanner_metrics_to_db(["SPY", "AAPL"], engine) == []
    cached = db_loader.get_universe_stock_metrics_from_db(["AAPL"], engine)
    assert [item["symbol"] for item in cached] == ["AAPL"]


def test_scanner_reader_rejects_old_80_percent_style_partial_cache(engine, monkeypatch):
    dates = pd.bdate_range("2026-01-05", periods=5)
    _seed_daily_history(engine, dates)
    assert db_loader.save_symbol_history_to_db("MSFT", _history(dates, 140), engine)
    snapshot_date = db_loader.scanner_metrics_snapshot_date()
    old_inputs = ["SPY", "AAPL"]
    old_watermarks = db_loader.get_price_history_watermarks(engine, old_inputs)
    old_fingerprint = db_loader.scanner_metrics_input_fingerprint(
        old_inputs, old_watermarks
    )
    assert db_loader.save_scanner_metrics_snapshot_to_db(
        [{"symbol": "AAPL", "return_1m": 1.0}],
        snapshot_date,
        old_fingerprint,
        engine,
    ) == ["AAPL"]
    history_loads = []
    monkeypatch.setattr(
        db_loader,
        "load_universe_history_from_db",
        lambda symbols, *args, **kwargs: history_loads.append(list(symbols)) or {},
    )

    assert db_loader.get_universe_stock_metrics_from_db(["AAPL", "MSFT"], engine) == []
    assert history_loads == [["SPY", "AAPL", "MSFT"]]


def test_scanner_fingerprint_tracks_spy_and_cache_version(engine, monkeypatch):
    dates = pd.bdate_range("2026-01-05", periods=3)
    _seed_daily_history(engine, dates)
    symbols = ["SPY", "AAPL"]
    original = db_loader.get_price_history_watermarks(engine, symbols)
    first = db_loader.scanner_metrics_input_fingerprint(symbols, original)

    later_spy = _history([pd.Timestamp("2026-01-08")], 250)
    assert db_loader.save_symbol_history_to_db("SPY", later_spy, engine)
    changed = db_loader.get_price_history_watermarks(engine, symbols)
    second = db_loader.scanner_metrics_input_fingerprint(symbols, changed)
    monkeypatch.setattr(
        db_loader,
        "SCANNER_METRICS_CACHE_VERSION",
        db_loader.SCANNER_METRICS_CACHE_VERSION + 1,
    )
    third = db_loader.scanner_metrics_input_fingerprint(symbols, changed)

    assert first != second
    assert second != third
