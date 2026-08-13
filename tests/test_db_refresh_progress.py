import numpy as np
import pandas as pd
from sqlalchemy import create_engine

import src.utils.data_loader as data_loader
import src.utils.db_loader as db_loader
from src.infrastructure.database.repositories import chart_indicators, scanner


def test_chart_indicator_refresh_logs_progress(monkeypatch):
    logs = []
    history = pd.DataFrame(
        {
            "Open": [10.0, 11.0],
            "High": [11.0, 12.0],
            "Low": [9.0, 10.0],
            "Close": [10.5, 11.5],
            "Volume": [1000.0, 1200.0],
        },
        index=[pd.Timestamp("2026-01-05"), pd.Timestamp("2026-01-06")],
    )
    monkeypatch.setattr(
        chart_indicators,
        "load_universe_history_from_db",
        lambda tickers, engine, start=None, end=None, interval="1d": {"SPY": history, "AAPL": history},
    )
    monkeypatch.setattr(
        chart_indicators,
        "get_chart_indicator_refresh_plan",
        lambda *args, **kwargs: {
            "AAPL": pd.Timestamp("2026-01-05").to_pydatetime(),
            "BAD": pd.Timestamp("2026-01-05").to_pydatetime(),
        },
    )
    monkeypatch.setattr(chart_indicators, "save_chart_indicators_batch_to_db", lambda records, engine: len(records))

    engine = create_engine("sqlite:///:memory:", future=True)
    updated = db_loader.refresh_chart_indicators_to_db(
        ["SPY", "AAPL", "BAD"],
        engine=engine,
        log_callback=logs.append,
    )

    assert updated == ["AAPL"]
    assert any("Calculating chart indicators" in message for message in logs)
    assert any("Chart indicators progress" in message for message in logs)
    assert any("BAD: unable to calculate chart indicators" in message for message in logs)


def test_scanner_metrics_refresh_logs_calculate_and_save_progress(monkeypatch):
    logs = []
    history = pd.DataFrame(
        {
            "Open": [10.0],
            "High": [11.0],
            "Low": [9.0],
            "Close": [10.5],
            "Volume": [1000.0],
        },
        index=[pd.Timestamp("2026-01-05")],
    )

    monkeypatch.setattr(
        scanner,
        "load_universe_history_from_db",
        lambda tickers, engine, start=None: {"SPY": history, "AAPL": history, "MSFT": history},
    )
    monkeypatch.setattr(
        data_loader,
        "compute_stock_metrics",
        lambda symbol, symbol_history, spy_history=None: {"symbol": symbol, "return_1m": 1.0, "return_3m": 1.0},
    )
    snapshot_date = pd.Timestamp("2026-01-05").to_pydatetime()
    monkeypatch.setattr(scanner, "scanner_metrics_snapshot_date", lambda: snapshot_date)
    monkeypatch.setattr(
        scanner,
        "get_price_history_watermarks",
        lambda *args, **kwargs: {
            "SPY": (snapshot_date, 1),
            "AAPL": (snapshot_date, 1),
            "MSFT": (snapshot_date, 1),
        },
    )
    cache_checks = iter([False, True])
    monkeypatch.setattr(
        scanner,
        "is_scanner_metrics_snapshot_current",
        lambda *args, **kwargs: next(cache_checks),
    )
    monkeypatch.setattr(
        scanner,
        "save_scanner_metrics_snapshot_to_db",
        lambda metrics_list, date, fingerprint, engine, **kwargs: [
            item["symbol"] for item in metrics_list
        ],
    )

    updated = db_loader.refresh_scanner_metrics_to_db(
        ["AAPL", "MSFT"],
        engine=object(),
        log_callback=logs.append,
    )

    assert updated == ["AAPL", "MSFT"]
    assert any("Calculating scanner metrics" in message for message in logs)
    assert any("Scanner metrics progress" in message for message in logs)
    assert any("Saving scanner metrics" in message for message in logs)
    assert any("Scanner metrics save progress" in message for message in logs)


def test_scanner_metrics_batch_save_accepts_numpy_scalars():
    engine = create_engine("sqlite:///:memory:", future=True)
    date = pd.Timestamp("2026-01-05").to_pydatetime()

    saved = db_loader.save_scanner_metrics_batch_to_db(
        [
            {
                "symbol": "AAPL",
                "price": np.float64(123.45),
                "volume": np.int64(1000),
                "above_sma_20": np.bool_(True),
            }
        ],
        date,
        engine,
    )
    loaded = db_loader.load_scanner_metrics_from_db(["AAPL"], engine, date)

    assert saved == ["AAPL"]
    assert loaded[0]["price"] == 123.45
    assert loaded[0]["volume"] == 1000
    assert loaded[0]["above_sma_20"] is True
