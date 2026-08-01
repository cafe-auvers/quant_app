import datetime as dt

import pandas as pd
from sqlalchemy import create_engine

import src.utils.data_loader as data_loader
from src.utils.db_loader import (
    _period_for_daily_refresh,
    load_symbol_history_from_db,
    save_universe_history_batch_to_db,
)


def _multi_symbol_history(symbols, dates=None):
    if dates is None:
        dates = pd.date_range("2026-01-01", periods=2, freq="D")
    columns = pd.MultiIndex.from_product([symbols, ["Open", "High", "Low", "Close", "Volume"]])
    values = []
    for row_index, _date in enumerate(dates):
        row = []
        for symbol_index, _symbol in enumerate(symbols):
            base = 10.0 + symbol_index + row_index
            row.extend([base, base + 1.0, base - 1.0, base + 0.5, 1000.0 + row_index])
        values.append(row)
    return pd.DataFrame(values, index=dates, columns=columns)


def test_download_price_history_uses_chunks_and_threads(monkeypatch):
    calls = []

    def fake_download(**kwargs):
        symbols = kwargs["tickers"]
        if isinstance(symbols, str):
            symbols = [symbols]
        calls.append({"symbols": list(symbols), "threads": kwargs["threads"]})
        return _multi_symbol_history(list(symbols))

    monkeypatch.setattr(data_loader.yf, "download", fake_download)

    history = data_loader.download_price_history(
        ["AAA", "BBB", "CCC"],
        period="1mo",
        interval="1d",
        max_symbols=3,
        chunk_size=2,
        threads=8,
        batch_sleep=0,
        max_retries=0,
        fallback_to_single=False,
    )

    assert [call["symbols"] for call in calls] == [["AAA", "BBB"], ["CCC"]]
    assert all(call["threads"] == 8 for call in calls)
    assert data_loader._extract_symbol_history(history, "CCC") is not None


def test_download_price_history_uses_chart_fallback_for_empty_batch(monkeypatch):
    def empty_yfinance_batch(**_kwargs):
        return pd.DataFrame()

    def fake_chart_history(symbol, period="3mo", interval="1d", max_attempts=3):
        return pd.DataFrame(
            {
                "Open": [10.0],
                "High": [11.0],
                "Low": [9.0],
                "Close": [10.5],
                "Volume": [1000.0],
            },
            index=[pd.Timestamp("2026-01-05")],
        )

    monkeypatch.setattr(data_loader.yf, "download", empty_yfinance_batch)
    monkeypatch.setattr(data_loader, "_download_symbol_history_chart", fake_chart_history)

    history = data_loader.download_price_history(
        ["SPY", "AAPL"],
        period="1mo",
        interval="1d",
        max_symbols=2,
        chunk_size=2,
        threads=2,
        batch_sleep=0,
        max_retries=0,
        fallback_to_single=False,
    )

    assert data_loader._extract_symbol_history(history, "SPY") is not None
    assert data_loader._extract_symbol_history(history, "AAPL") is not None


def test_daily_period_selection_uses_incremental_for_recent_cache():
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)

    assert _period_for_daily_refresh(None, full_period="1y", incremental_period="1mo") == "1y"
    assert _period_for_daily_refresh(now - dt.timedelta(days=3), full_period="1y", incremental_period="1mo") == "1mo"
    assert _period_for_daily_refresh(now - dt.timedelta(days=90), full_period="1y", incremental_period="1mo") == "1y"


def test_save_universe_history_batch_to_db_upserts_without_deleting_old_rows():
    engine = create_engine("sqlite:///:memory:", future=True)

    first_batch = _multi_symbol_history(["AAPL", "MSFT"], dates=pd.to_datetime(["2026-01-01"]))
    second_batch = _multi_symbol_history(["AAPL"], dates=pd.to_datetime(["2026-01-02"]))

    assert save_universe_history_batch_to_db(first_batch, ["AAPL", "MSFT"], engine, interval="1d") == 2
    assert save_universe_history_batch_to_db(second_batch, ["AAPL"], engine, interval="1d") == 1

    aapl = load_symbol_history_from_db("AAPL", engine, interval="1d")
    msft = load_symbol_history_from_db("MSFT", engine, interval="1d")

    assert len(aapl) == 2
    assert len(msft) == 1
    assert pd.Timestamp(aapl.index[-1]).date() == dt.date(2026, 1, 2)


def test_download_normalizes_field_first_single_symbol_columns(monkeypatch):
    columns = pd.MultiIndex.from_product(
        [["Open", "High", "Low", "Close", "Volume"], ["AAPL"]]
    )
    raw = pd.DataFrame(
        [[10.0, 11.0, 9.0, 10.5, 1000.0]],
        index=pd.to_datetime(["2026-01-05"]),
        columns=columns,
    )
    monkeypatch.setattr(data_loader, "_download_yfinance_batch", lambda *_args, **_kwargs: raw)

    history = data_loader.download_price_history(
        ["AAPL"], batch_sleep=0, max_retries=0, fallback_to_single=False
    )

    assert isinstance(history.columns, pd.MultiIndex)
    assert history.columns.get_level_values(0).unique().tolist() == ["AAPL"]
    symbol_history = data_loader._extract_symbol_history(history, "AAPL")
    assert symbol_history is not None
    assert symbol_history["Close"].iloc[-1] == 10.5


def test_partial_batch_fallback_does_not_leave_duplicate_ohlcv_columns(monkeypatch):
    initial = _multi_symbol_history(["AAA", "BBB"])
    initial.loc[:, pd.IndexSlice["BBB", :]] = float("nan")

    def fake_batch(symbols, **_kwargs):
        assert symbols == ["AAA", "BBB"]
        return initial

    def fake_chart(symbol, period="3mo", interval="1d", max_attempts=3):
        assert symbol == "BBB"
        return pd.DataFrame(
            {
                "Open": [20.0, 21.0],
                "High": [21.0, 22.0],
                "Low": [19.0, 20.0],
                "Close": [20.5, 21.5],
                "Volume": [2000.0, 2100.0],
            },
            index=pd.date_range("2026-01-01", periods=2, freq="D"),
        )

    monkeypatch.setattr(data_loader, "_download_yfinance_batch", fake_batch)
    monkeypatch.setattr(data_loader, "_download_symbol_history_chart", fake_chart)

    history = data_loader.download_price_history(
        ["AAA", "BBB"],
        batch_sleep=0,
        max_retries=0,
        fallback_to_single=False,
        threads=1,
    )

    bbb = data_loader._extract_symbol_history(history, "BBB")
    assert bbb is not None
    assert bbb.columns.is_unique
    assert bbb["Close"].tolist() == [20.5, 21.5]
    assert not history.columns.duplicated().any()


def test_chart_fallback_uses_adjusted_ohlcv_prices():
    class _Response:
        status_code = 200

        @staticmethod
        def json():
            return {
                "chart": {
                    "result": [
                        {
                            "timestamp": [1767571200],
                            "indicators": {
                                "quote": [
                                    {
                                        "open": [80.0],
                                        "high": [110.0],
                                        "low": [70.0],
                                        "close": [100.0],
                                        "volume": [1234.0],
                                    }
                                ],
                                "adjclose": [{"adjclose": [50.0]}],
                            },
                        }
                    ]
                }
            }

    history = data_loader._parse_chart_history_response(_Response())

    assert history["Open"].iloc[0] == 40.0
    assert history["High"].iloc[0] == 55.0
    assert history["Low"].iloc[0] == 35.0
    assert history["Close"].iloc[0] == 50.0
    assert history["Adj Close"].iloc[0] == 50.0
    assert history["Volume"].iloc[0] == 1234.0


def test_single_symbol_helper_returns_flat_ohlcv_frame(monkeypatch):
    batch = _multi_symbol_history(["AAPL"])
    monkeypatch.setattr(data_loader, "download_price_history", lambda *_args, **_kwargs: batch)

    history = data_loader.download_single_symbol_history("AAPL")

    assert not isinstance(history.columns, pd.MultiIndex)
    assert history.columns.tolist() == ["Open", "High", "Low", "Close", "Volume"]


def test_scanner_metrics_keeps_class_share_symbol_after_yahoo_normalization(monkeypatch):
    history = _multi_symbol_history(["SPY", "BRK-B"])
    requested = []

    def fake_download(tickers, **_kwargs):
        requested.extend(tickers)
        return history

    monkeypatch.setattr(data_loader, "download_price_history", fake_download)

    metrics = data_loader.get_universe_stock_metrics(["BRK.B"], max_symbols=1)

    assert requested == ["SPY", "BRK-B"]
    assert [item["symbol"] for item in metrics] == ["BRK-B"]
