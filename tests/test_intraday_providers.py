import datetime as dt

import pandas as pd
import pytest
from sqlalchemy import create_engine

from src.api.kis_intraday import KisIntradayClient, KisIntradayNotConfiguredError, normalize_intraday_rows
from src.core.orb import calculate_orb_range
from src.services.intraday_data_service import fetch_intraday_with_fallback, load_best_intraday_history
import src.services.intraday_data_service as intraday_data_service
from src.services.intraday_provider import (
    IntradayInterval,
    IntradayProviderName,
    IntradayRequest,
    IntradayResult,
    resample_ohlcv_bars,
)
from src.services.kis_intraday_provider import KisIntradayProviderError, fetch_kis_intraday
import src.services.kis_intraday_provider as kis_provider
from src.services.yfinance_intraday_provider import fetch_yfinance_intraday
import src.services.yfinance_intraday_provider as yfinance_provider
from src.ui.workers import IntradayFetchWorker
from src.utils.db_loader import save_intraday_history_to_db


def _bars(start="2026-01-02 09:30", periods=6, freq="1min") -> pd.DataFrame:
    index = pd.date_range(start, periods=periods, freq=freq)
    return pd.DataFrame(
        {
            "Open": range(10, 10 + periods),
            "High": range(11, 11 + periods),
            "Low": range(9, 9 + periods),
            "Close": [10.5 + i for i in range(periods)],
            "Volume": [1000 + i for i in range(periods)],
        },
        index=index,
    )


def test_normalize_intraday_rows_skips_missing_raw_fields():
    result = normalize_intraday_rows(
        symbol="AAPL",
        exchange="NASD",
        rows=[
            {"time": "2026-01-02 09:30", "open": "10", "high": "11", "low": "9", "close": "10.5", "volume": "100"},
            {"time": "2026-01-02 09:31", "open": "bad", "high": "12", "low": "10", "close": "11.5"},
            {"time": "2026-01-02 09:32", "open": "12", "high": "13", "low": "11", "close": "12.5", "volume": "300"},
        ],
        time_field="time",
        open_field="open",
        high_field="high",
        low_field="low",
        close_field="close",
        volume_field="volume",
    )

    assert list(result.bars["Close"]) == [10.5, 12.5]
    assert list(result.bars.columns) == ["Open", "High", "Low", "Close", "Volume"]


def test_kis_client_raises_clear_error_when_intraday_disabled(monkeypatch):
    monkeypatch.setenv("KIS_INTRADAY_ENABLED", "false")

    with pytest.raises(KisIntradayNotConfiguredError, match="KIS intraday is disabled"):
        KisIntradayClient(client=object()).fetch_overseas_1m("AAPL")


def test_kis_provider_raises_clear_error_when_not_configured(monkeypatch):
    monkeypatch.setattr(kis_provider, "is_kis_intraday_enabled", lambda: False)
    request = IntradayRequest(symbol="AAPL", interval="1m", allow_fallback=False)

    with pytest.raises(KisIntradayProviderError, match="KIS intraday is disabled"):
        fetch_kis_intraday(request)


def test_yfinance_provider_can_be_mocked(monkeypatch):
    bars = _bars(freq="5min")
    monkeypatch.setattr(yfinance_provider, "_download_5m_with_retries", lambda symbol, days: pd.DataFrame())
    monkeypatch.setattr(yfinance_provider, "_extract_symbol_history", lambda history, symbol: bars)

    result = fetch_yfinance_intraday(IntradayRequest(symbol="AAPL", interval="5m"))

    assert result.source == IntradayProviderName.YFINANCE.value
    assert result.symbol == "AAPL"
    assert result.bars.equals(bars)


def test_fallback_service_uses_yfinance_when_kis_disabled(monkeypatch):
    monkeypatch.setattr(intraday_data_service, "is_kis_intraday_enabled", lambda: False)
    monkeypatch.setattr(
        intraday_data_service,
        "fetch_yfinance_intraday",
        lambda request: IntradayResult(request.symbol, request.interval, "yfinance", _bars(freq="5min")),
    )

    result = fetch_intraday_with_fallback(IntradayRequest(symbol="AAPL", interval="5m"))

    assert result.source == "yfinance"
    assert "KIS intraday disabled/unconfigured." in result.warnings


def test_fallback_service_uses_yfinance_after_kis_failure(monkeypatch):
    monkeypatch.setattr(intraday_data_service, "is_kis_intraday_enabled", lambda: True)
    monkeypatch.setattr(intraday_data_service, "fetch_kis_intraday", lambda request: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(
        intraday_data_service,
        "fetch_yfinance_intraday",
        lambda request: IntradayResult(request.symbol, request.interval, "yfinance", _bars(freq="5min")),
    )

    result = fetch_intraday_with_fallback(IntradayRequest(symbol="AAPL", interval="5m", allow_fallback=True))

    assert result.source == "yfinance"
    assert any("KIS intraday failed/unavailable" in warning for warning in result.warnings)


def test_fallback_service_does_not_use_yfinance_when_fallback_disabled(monkeypatch):
    called = {"yfinance": False}
    monkeypatch.setattr(intraday_data_service, "is_kis_intraday_enabled", lambda: True)
    monkeypatch.setattr(intraday_data_service, "fetch_kis_intraday", lambda request: (_ for _ in ()).throw(RuntimeError("boom")))

    def fake_yfinance(request):
        called["yfinance"] = True
        return IntradayResult(request.symbol, request.interval, "yfinance", _bars(freq="5min"))

    monkeypatch.setattr(intraday_data_service, "fetch_yfinance_intraday", fake_yfinance)

    result = fetch_intraday_with_fallback(IntradayRequest(symbol="AAPL", interval="5m", allow_fallback=False))

    assert result.source == "none"
    assert result.bars.empty
    assert called["yfinance"] is False


def test_cached_loader_prefers_kis_over_yfinance():
    engine = create_engine("sqlite:///:memory:")
    yfinance_bars = _bars(freq="5min")
    kis_bars = yfinance_bars.copy()
    kis_bars["Close"] = kis_bars["Close"] + 100

    assert save_intraday_history_to_db("AAPL", yfinance_bars, engine, interval="5m", source="yfinance")
    assert save_intraday_history_to_db("AAPL", kis_bars, engine, interval="5m", source="kis")

    loaded, source = load_best_intraday_history("AAPL", engine, interval="5m")

    assert source == "kis"
    assert loaded.iloc[-1]["Close"] == kis_bars.iloc[-1]["Close"]


def test_orb_result_matches_normalized_kis_and_yfinance_bars():
    raw_rows = [
        {
            "time": str(timestamp),
            "open": row.Open,
            "high": row.High,
            "low": row.Low,
            "close": row.Close,
            "volume": row.Volume,
        }
        for timestamp, row in _bars().iterrows()
    ]
    kis_result = normalize_intraday_rows(
        "AAPL",
        "NASD",
        raw_rows,
        time_field="time",
        open_field="open",
        high_field="high",
        low_field="low",
        close_field="close",
        volume_field="volume",
    )
    yfinance_bars = _bars()

    kis_orb = calculate_orb_range("AAPL", kis_result.bars, "5m")
    yf_orb = calculate_orb_range("AAPL", yfinance_bars, "5m")

    assert kis_orb is not None
    assert yf_orb is not None
    assert kis_orb.high == yf_orb.high
    assert kis_orb.low == yf_orb.low


def test_resample_5m_from_1m_bars_produces_valid_ohlcv():
    resampled = resample_ohlcv_bars(_bars(periods=10), IntradayInterval.FIVE_MINUTE)

    assert len(resampled) == 2
    assert resampled.iloc[0]["Open"] == 10
    assert resampled.iloc[0]["High"] == 15
    assert resampled.iloc[0]["Low"] == 9
    assert resampled.iloc[0]["Close"] == 14.5
    assert resampled.iloc[0]["Volume"] == sum(1000 + i for i in range(5))


def test_intraday_worker_still_emits_existing_finished_payload_shape(monkeypatch):
    monkeypatch.setattr(
        "src.ui.workers.fetch_intraday_with_fallback",
        lambda request: IntradayResult(request.symbol, request.interval, "yfinance", _bars(freq="5min")),
    )
    emitted = []
    worker = IntradayFetchWorker("AAPL", engine=None, window_days=3)
    worker.finished_fetch.connect(lambda *args: emitted.append(args))

    worker.run()

    assert len(emitted) == 1
    symbol, fetched, window_days, source = emitted[0]
    assert symbol == "AAPL"
    assert isinstance(fetched, pd.DataFrame)
    assert window_days == 3
    assert source == "yfinance"


def test_intraday_provider_modules_import():
    import src.api.kis_intraday
    import src.services.intraday_data_service
    import src.services.kis_intraday_provider
    import src.services.yfinance_intraday_provider

    assert src.api.kis_intraday is not None
    assert src.services.intraday_data_service is not None
    assert src.services.kis_intraday_provider is not None
    assert src.services.yfinance_intraday_provider is not None
