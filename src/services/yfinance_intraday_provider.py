"""yfinance-backed intraday provider."""
from __future__ import annotations

import time

import pandas as pd

from src.services.intraday_provider import (
    IntradayInterval,
    IntradayProviderError,
    IntradayProviderName,
    IntradayRequest,
    IntradayResult,
    normalize_ohlcv_frame,
)
from src.utils.data_loader import _extract_symbol_history, download_price_history
from src.utils.intraday_helpers import extract_latest_opening_bar


def fetch_yfinance_intraday(request: IntradayRequest) -> IntradayResult:
    """Fetch normalized intraday bars from yfinance-compatible Yahoo data."""
    if request.interval == IntradayInterval.FIVE_MINUTE.value:
        history = _download_5m_with_retries(request.symbol, request.window_days)
        bars = _extract_symbol_history(history, request.symbol)
        bars = normalize_ohlcv_frame(bars)
    elif request.interval == IntradayInterval.ONE_MINUTE.value:
        bars = _download_opening_1m_bar(request.symbol)
        bars = normalize_ohlcv_frame(bars)
    else:
        raise IntradayProviderError(f"Unsupported yfinance intraday interval: {request.interval}")

    warnings = []
    if bars.empty:
        warnings.append(f"No {request.interval} yfinance intraday rows returned for {request.symbol}.")
    return IntradayResult(
        symbol=request.symbol,
        interval=request.interval,
        source=IntradayProviderName.YFINANCE,
        bars=bars,
        exchange=request.exchange,
        warnings=warnings,
    )


def _download_5m_with_retries(symbol: str, days: int, attempts: int = 3) -> pd.DataFrame:
    last_error = None
    for attempt in range(attempts):
        try:
            history = download_price_history([symbol], period=f"{days}d", interval="5m", max_symbols=1)
            if not history.empty:
                return history
            last_error = RuntimeError("empty yfinance response")
        except Exception as exc:
            last_error = exc
        time.sleep(0.6 * (attempt + 1))
    raise IntradayProviderError(f"yfinance 5-minute fetch failed after {attempts} attempts: {last_error}")


def _download_opening_1m_bar(symbol: str) -> pd.DataFrame:
    try:
        history = download_price_history([symbol], period="1d", interval="1m", max_symbols=1)
        return extract_latest_opening_bar(history, symbol)
    except Exception:
        return pd.DataFrame()
