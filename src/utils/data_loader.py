"""Data loading utilities for stock scanning using yfinance."""
from __future__ import annotations

from pathlib import Path
from typing import List, Dict, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed
import datetime as dt
import logging
import random
import time
from io import StringIO

import numpy as np
import pandas as pd
import requests
import yfinance as yf

DEFAULT_UNIVERSE_CACHE = Path("data/us_kis_tickers.csv")
SP500_UNIVERSE_CACHE = Path("data/sp500_tickers.csv")
WIKI_SP500_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
DEFAULT_UNIVERSE = [
    "AAPL",
    "MSFT",
    "NVDA",
    "AMZN",
    "GOOGL",
    "META",
    "TSLA",
    "BRK-B",
    "JPM",
    "JNJ",
    "V",
    "PG",
    "UNH",
    "MA",
    "DIS",
    "PYPL",
    "CRM",
    "INTC",
    "CSCO",
    "NFLX",
]


def _limit_symbols(symbols: List[str], max_symbols: Optional[int]) -> List[str]:
    if max_symbols is None or int(max_symbols) <= 0:
        return symbols
    return symbols[: int(max_symbols)]


def _read_symbol_cache(path: Path) -> List[str]:
    if not path.exists():
        return []
    try:
        df = pd.read_csv(path, dtype=str).fillna("")
    except Exception:
        return []

    for column in ("Symbol", "YahooSymbol", "symbol", "ticker"):
        if column in df.columns:
            symbols = [str(symbol).strip().upper() for symbol in df[column].tolist() if str(symbol).strip()]
            return list(dict.fromkeys(symbols))
    return []


def _read_kis_symbol_cache(path: Path) -> List[str]:
    if not path.exists():
        return []
    try:
        df = pd.read_csv(path, dtype=str).fillna("")
    except Exception:
        return []

    try:
        from overseas_stock_code import is_common_stock_like_symbol

        if "Symbol" in df.columns:
            name_column = "Name" if "Name" in df.columns else "english_name"
            names = df[name_column] if name_column in df.columns else ""
            mask = [
                is_common_stock_like_symbol(symbol, name)
                for symbol, name in zip(df["Symbol"].astype(str), names if isinstance(names, pd.Series) else [""] * len(df))
            ]
            df = df[mask]
    except Exception:
        pass

    for column in ("Symbol", "YahooSymbol", "symbol", "ticker"):
        if column in df.columns:
            symbols = [str(symbol).strip().upper() for symbol in df[column].tolist() if str(symbol).strip()]
            return list(dict.fromkeys(symbols))
    return []


def get_sp500_tickers(max_symbols: Optional[int] = 200) -> List[str]:
    """Return a list of S&P 500 tickers, using a local cache if available."""
    cached_symbols = _read_symbol_cache(SP500_UNIVERSE_CACHE)
    if cached_symbols:
        return _limit_symbols(cached_symbols, max_symbols)

    try:
        response = requests.get(
            WIKI_SP500_URL,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                "Accept-Language": "en-US,en;q=0.9",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            },
            timeout=15,
        )
        response.raise_for_status()
        tables = pd.read_html(StringIO(response.text))
        for table in tables:
            if "Symbol" in table.columns:
                symbols = table["Symbol"].astype(str).tolist()
                SP500_UNIVERSE_CACHE.parent.mkdir(parents=True, exist_ok=True)
                pd.DataFrame({"Symbol": symbols}).to_csv(SP500_UNIVERSE_CACHE, index=False)
                return _limit_symbols(symbols, max_symbols)
    except Exception:
        pass

    return _limit_symbols(DEFAULT_UNIVERSE, max_symbols)


def get_us_kis_tickers(max_symbols: Optional[int] = None, refresh: bool = False) -> List[str]:
    """Return US stock tickers from the KIS overseas master cache."""
    if not refresh:
        cached_symbols = _read_kis_symbol_cache(DEFAULT_UNIVERSE_CACHE)
        if cached_symbols:
            return _limit_symbols(cached_symbols, max_symbols)

    try:
        from overseas_stock_code import load_us_kis_stock_universe

        universe = load_us_kis_stock_universe(cache_path=DEFAULT_UNIVERSE_CACHE, refresh=refresh)
        symbols = [str(symbol).strip().upper() for symbol in universe.get("Symbol", []) if str(symbol).strip()]
        symbols = list(dict.fromkeys(symbols))
        if symbols:
            return _limit_symbols(symbols, max_symbols)
    except Exception:
        pass

    cached_symbols = _read_kis_symbol_cache(DEFAULT_UNIVERSE_CACHE)
    return _limit_symbols(cached_symbols, max_symbols)


YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart"
YAHOO_REQUEST_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    'Accept': 'application/json, text/javascript, */*; q=0.01',
    'Accept-Language': 'en-US,en;q=0.9',
    'Referer': 'https://finance.yahoo.com/',
    'Connection': 'keep-alive',
}


def _parse_chart_history_response(response: requests.Response) -> pd.DataFrame:
    if response.status_code != 200:
        return pd.DataFrame()

    try:
        payload = response.json()
    except ValueError:
        return pd.DataFrame()

    chart = payload.get("chart", {})
    result = chart.get("result")
    if not result:
        return pd.DataFrame()

    result = result[0]
    timestamp = result.get("timestamp")
    quote = result.get("indicators", {}).get("quote", [{}])[0]
    if not timestamp or not quote:
        return pd.DataFrame()

    data = {
        "Open": quote.get("open", []),
        "High": quote.get("high", []),
        "Low": quote.get("low", []),
        "Close": quote.get("close", []),
        "Volume": quote.get("volume", []),
    }
    df = pd.DataFrame(data, index=pd.to_datetime(timestamp, unit="s", utc=True))
    df = df.dropna(subset=["Close", "High", "Low", "Volume"])

    meta = result.get("meta", {})
    tz = meta.get("exchangeTimezoneName")
    if tz and not df.empty:
        try:
            df = df.tz_convert(tz)
        except Exception:
            pass

    return df


YAHOO_CHART_HOSTS = [
    "https://query1.finance.yahoo.com/v8/finance/chart",
    "https://query2.finance.yahoo.com/v8/finance/chart",
]


def _download_symbol_history_chart(
    symbol: str,
    period: str = "3mo",
    interval: str = "1d",
    max_attempts: int = 3,
) -> pd.DataFrame:
    session = requests.Session()
    session.headers.update(YAHOO_REQUEST_HEADERS)
    params = {
        "range": period,
        "interval": interval,
        "includePrePost": "false",
        "events": "div,splits",
    }
    for attempt in range(1, max_attempts + 1):
        for host in YAHOO_CHART_HOSTS:
            try:
                url = f"{host}/{symbol}"
                response = session.get(url, params=params, timeout=30)
                if response.status_code == 200:
                    history = _parse_chart_history_response(response)
                    if not history.empty:
                        return history
                if response.status_code == 429:
                    time_to_wait = min(5 * attempt, 30) + random.uniform(0.3, 1.5)
                    time.sleep(time_to_wait)
                    continue
                if response.status_code >= 500:
                    time.sleep(min(2 * attempt, 10))
                    continue
            except Exception:
                time.sleep(min(2 * attempt, 10))
                continue

    return pd.DataFrame()


def _download_symbol_history(
    symbol: str,
    period: str = "3mo",
    interval: str = "1d",
    max_attempts: int = 3,
    threads: bool | int = False,
) -> pd.DataFrame:
    """Download historical data for a single symbol."""
    history = _download_symbol_history_chart(symbol, period=period, interval=interval, max_attempts=max_attempts)
    if not history.empty:
        return history

    for attempt in range(1, max_attempts + 1):
        try:
            yf_logger = logging.getLogger("yfinance")
            old_level = yf_logger.level
            yf_logger.setLevel(logging.CRITICAL)
            try:
                history = yf.download(
                    tickers=symbol,
                    period=period,
                    interval=interval,
                    auto_adjust=True,
                    threads=threads,
                    progress=False,
                )
            finally:
                yf_logger.setLevel(old_level)

            if not history.empty:
                return history
        except Exception:
            pass

        if attempt < max_attempts:
            time.sleep(min(2 * attempt, 8) + random.uniform(0.1, 0.7))

    return pd.DataFrame()


def _default_yfinance_chunk_size(interval: str) -> int:
    normalized = (interval or "1d").strip().lower()
    if normalized == "1d":
        return 200
    if normalized == "1h":
        return 100
    return 50


def _clean_download_tickers(tickers: List[str], max_symbols: Optional[int]) -> List[str]:
    cleaned = [ticker.strip().upper() for ticker in tickers if ticker and ticker.strip()]
    cleaned = list(dict.fromkeys(cleaned))
    return _limit_symbols(cleaned, max_symbols)


def _chunked_symbols(symbols: List[str], chunk_size: int) -> List[List[str]]:
    size = max(1, int(chunk_size or len(symbols) or 1))
    return [symbols[index:index + size] for index in range(0, len(symbols), size)]


def _download_yfinance_batch(
    symbols: List[str],
    period: str,
    interval: str,
    threads: bool | int,
) -> pd.DataFrame:
    yf_logger = logging.getLogger("yfinance")
    old_level = yf_logger.level
    yf_logger.setLevel(logging.CRITICAL)
    try:
        return yf.download(
            tickers=symbols,
            period=period,
            interval=interval,
            group_by="ticker",
            auto_adjust=True,
            threads=threads,
            progress=False,
        )
    finally:
        yf_logger.setLevel(old_level)


def _normalize_batch_download_columns(history: pd.DataFrame, symbols: List[str]) -> pd.DataFrame:
    if history.empty or isinstance(history.columns, pd.MultiIndex) or len(symbols) != 1:
        return history
    normalized = history.copy()
    normalized.columns = pd.MultiIndex.from_product([[symbols[0]], normalized.columns])
    return normalized


def _symbols_with_downloaded_history(history: pd.DataFrame, symbols: List[str]) -> List[str]:
    if history.empty:
        return []
    available = []
    for symbol in symbols:
        symbol_history = _extract_symbol_history(history, symbol)
        if symbol_history is not None and not symbol_history.empty:
            available.append(symbol)
    return available


def _worker_count(threads: bool | int, default: int = 8) -> int:
    if isinstance(threads, bool):
        return default if threads else 1
    try:
        return max(1, min(16, int(threads)))
    except (TypeError, ValueError):
        return default


def _download_chart_fallback_batch(
    symbols: List[str],
    period: str,
    interval: str,
    threads: bool | int,
) -> pd.DataFrame:
    if not symbols:
        return pd.DataFrame()

    frames = []
    max_workers = _worker_count(threads)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(
                _download_symbol_history_chart,
                symbol,
                period,
                interval,
                2,
            ): symbol
            for symbol in symbols
        }
        for future in as_completed(futures):
            symbol = futures[future]
            try:
                symbol_history = future.result()
            except Exception:
                continue
            if symbol_history.empty:
                continue
            if not isinstance(symbol_history.columns, pd.MultiIndex):
                symbol_history.columns = pd.MultiIndex.from_product([[symbol], symbol_history.columns])
            frames.append(symbol_history)

    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, axis=1)


def download_price_history(
    tickers: List[str],
    period: str = "3mo",
    interval: str = "1d",
    max_symbols: Optional[int] = 200,
    chunk_size: Optional[int] = None,
    threads: bool | int = 8,
    batch_sleep: float = 1.0,
    max_retries: int = 1,
    fallback_to_single: bool = True,
    chart_fallback: bool = True,
) -> pd.DataFrame:
    """Download price history for a universe of tickers.

    Uses yfinance batch downloads by default and keeps the existing MultiIndex
    return shape for callers that extract one symbol at a time. Partial batch
    failures are retried with smaller chunks, then optionally with the existing
    single-symbol fallback.
    """
    universe = _clean_download_tickers(tickers, max_symbols)
    if not universe:
        return pd.DataFrame()

    effective_chunk_size = chunk_size or _default_yfinance_chunk_size(interval)
    frames: List[pd.DataFrame] = []
    failed_symbols: List[str] = []
    batches = _chunked_symbols(universe, effective_chunk_size)

    for batch_index, batch in enumerate(batches):
        try:
            data = _download_yfinance_batch(batch, period=period, interval=interval, threads=threads)
        except Exception:
            data = pd.DataFrame()
        data = _normalize_batch_download_columns(data, batch)

        available = _symbols_with_downloaded_history(data, batch)
        if not data.empty and available:
            frames.append(data)
        failed_symbols.extend([symbol for symbol in batch if symbol not in available])

        if batch_sleep > 0 and batch_index < len(batches) - 1:
            time.sleep(batch_sleep + random.uniform(0.0, 0.5))

    retry_symbols = list(dict.fromkeys(failed_symbols))
    for retry_index in range(1, max(0, int(max_retries)) + 1):
        if not retry_symbols:
            break

        wait_seconds = min(30.0, max(1.0, batch_sleep) * (2 ** (retry_index - 1))) + random.uniform(0.2, 1.0)
        time.sleep(wait_seconds)
        retry_chunk_size = max(1, min(len(retry_symbols), max(1, effective_chunk_size // (2 ** retry_index))))
        next_retry: List[str] = []

        for batch in _chunked_symbols(retry_symbols, retry_chunk_size):
            try:
                data = _download_yfinance_batch(batch, period=period, interval=interval, threads=threads)
            except Exception:
                data = pd.DataFrame()
            data = _normalize_batch_download_columns(data, batch)

            available = _symbols_with_downloaded_history(data, batch)
            if not data.empty and available:
                frames.append(data)
            next_retry.extend([symbol for symbol in batch if symbol not in available])

        retry_symbols = list(dict.fromkeys(next_retry))

    if chart_fallback and retry_symbols:
        chart_data = _download_chart_fallback_batch(
            retry_symbols,
            period=period,
            interval=interval,
            threads=threads,
        )
        chart_data = _normalize_batch_download_columns(chart_data, retry_symbols)
        available = _symbols_with_downloaded_history(chart_data, retry_symbols)
        if not chart_data.empty and available:
            frames.append(chart_data)
        retry_symbols = [symbol for symbol in retry_symbols if symbol not in set(available)]

    if fallback_to_single and retry_symbols:
        for symbol in retry_symbols:
            try:
                symbol_history = _download_symbol_history(
                    symbol,
                    period=period,
                    interval=interval,
                    threads=threads,
                )
                if symbol_history.empty:
                    continue
                if not isinstance(symbol_history.columns, pd.MultiIndex):
                    symbol_history.columns = pd.MultiIndex.from_product([[symbol], symbol_history.columns])
                frames.append(symbol_history)
            except Exception:
                continue

    if not frames:
        return pd.DataFrame()

    return pd.concat(frames, axis=1)


def _extract_symbol_history(history: pd.DataFrame, symbol: str) -> Optional[pd.DataFrame]:
    if history.empty:
        return None

    if isinstance(history.columns, pd.MultiIndex):
        if symbol in history.columns.levels[0]:
            symbol_df = history[symbol].copy()
        else:
            return None
    else:
        symbol_df = history.copy()

    if symbol_df.empty:
        return None

    if "Close" not in symbol_df.columns or "High" not in symbol_df.columns or "Low" not in symbol_df.columns:
        return None

    return symbol_df.dropna(subset=["Close", "High", "Low", "Volume"])


def compute_stock_metrics(
    symbol: str,
    history: pd.DataFrame,
    min_history_days: int = 1,
    spy_history: Optional[pd.DataFrame] = None,
) -> Optional[Dict]:
    """Compute scanner metrics for a single stock symbol."""
    symbol_history = _extract_symbol_history(history, symbol)
    if symbol_history is None or len(symbol_history) < min_history_days + 1:
        return None

    close = symbol_history["Close"].astype(float)
    high = symbol_history["High"].astype(float)
    low = symbol_history["Low"].astype(float)
    volume = symbol_history["Volume"].astype(float)

    if close.empty or volume.empty:
        return None

    latest_price = float(close.iloc[-1])
    latest_volume = float(volume.iloc[-1])
    dollar_volume = latest_price * latest_volume

    # Basic tradability averages
    avg_volume_20d = float(volume.rolling(20, min_periods=1).mean().iloc[-1]) if len(volume) > 0 else 0.0
    avg_dollar_volume_20d = float((close * volume).rolling(20, min_periods=1).mean().iloc[-1]) if len(close) > 0 else 0.0

    prev_close = close.shift(1)
    
    # Volatility
    adr_series = ((high - low) / prev_close).replace([np.inf, -np.inf], np.nan)
    adr_20 = float(adr_series.rolling(20, min_periods=5).mean().iloc[-1] * 100) if len(adr_series.dropna()) >= 1 else 0.0

    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs()
    ], axis=1).max(axis=1)
    atr_14 = tr.rolling(14, min_periods=1).mean()
    atr_14_pct = float((atr_14 / close).iloc[-1] * 100) if not atr_14.empty and not np.isnan(atr_14.iloc[-1]) else 0.0

    if len(close) > 1 and prev_close.iloc[-1] > 0:
        range_today_pct = float(((high.iloc[-1] - low.iloc[-1]) / prev_close.iloc[-1]) * 100)
    else:
        range_today_pct = float(((high.iloc[-1] - low.iloc[-1]) / latest_price) * 100) if latest_price > 0 else 0.0

    # Momentum / returns
    return_1w = float((close.iloc[-1] / close.iloc[-6] - 1) * 100) if len(close) >= 6 and close.iloc[-6] > 0 else 0.0
    return_1m = float((close.iloc[-1] / close.iloc[-22] - 1) * 100) if len(close) >= 22 and close.iloc[-22] > 0 else 0.0
    return_3m = float((close.iloc[-1] / close.iloc[-64] - 1) * 100) if len(close) >= 64 and close.iloc[-64] > 0 else 0.0
    return_6m = float((close.iloc[-1] / close.iloc[-127] - 1) * 100) if len(close) >= 127 and close.iloc[-127] > 0 else 0.0

    # Moving averages
    sma_20 = float(close.rolling(20, min_periods=1).mean().iloc[-1]) if len(close) > 0 else latest_price
    ema_50 = float(close.ewm(span=50, adjust=False).mean().iloc[-1]) if len(close) > 0 else latest_price
    sma_200 = float(close.rolling(200, min_periods=1).mean().iloc[-1]) if len(close) > 0 else latest_price

    above_sma_20 = bool(latest_price > sma_20)
    above_ema_50 = bool(latest_price > ema_50)
    ma_alignment = bool(sma_20 > ema_50 > sma_200)

    distance_from_20ma_pct = float((latest_price / sma_20 - 1) * 100) if sma_20 > 0 else 0.0
    distance_from_50ema_pct = float((latest_price / ema_50 - 1) * 100) if ema_50 > 0 else 0.0

    # Trend intensity
    if ema_50 == 0 or np.isnan(ema_50):
        trend_intensity = 0.0
    else:
        ratio = latest_price / ema_50 - 1
        trend_intensity = float(min(100.0, max(0.0, 100.0 * np.tanh(ratio * 20))))

    trend_score = float(trend_intensity + (10.0 if above_sma_20 else -10.0) + (10.0 if above_ema_50 else -10.0) + (20.0 if ma_alignment else 0.0))

    # Volume quality
    relative_volume = float(latest_volume / avg_volume_20d) if avg_volume_20d > 0 else 1.0
    volume_expansion = float(latest_volume / volume.iloc[-2]) if len(volume) > 1 and volume.iloc[-2] > 0 else 1.0
    
    min_vol_10d = float(volume.iloc[-10:].min()) if len(volume) >= 1 else latest_volume
    volume_dryup_ratio = float(min_vol_10d / avg_volume_20d) if avg_volume_20d > 0 else 1.0

    # Breakout / consolidation
    high_20d = float(high.iloc[-20:].max()) if len(high) > 0 else latest_price
    high_50d = float(high.iloc[-50:].max()) if len(high) > 0 else latest_price
    high_252d = float(high.iloc[-252:].max()) if len(high) > 0 else latest_price

    close_to_52w_high_pct = float((latest_price / high_252d - 1) * 100) if high_252d > 0 else 0.0
    distance_to_20d_high_pct = float((high_20d / latest_price - 1) * 100) if latest_price > 0 else 0.0

    breakout_20d = bool(latest_price > high.iloc[-21:-1].max()) if len(high) > 1 else False
    breakout_50d = bool(latest_price > high.iloc[-51:-1].max()) if len(high) > 1 else False

    max_high_10d = float(high.iloc[-10:].max()) if len(high) > 0 else latest_price
    min_low_10d = float(low.iloc[-10:].min()) if len(low) > 0 else latest_price
    consolidation_range_10d_pct = float(((max_high_10d - min_low_10d) / latest_price) * 100) if latest_price > 0 else 0.0
    consolidation_tightness = float(100.0 / (consolidation_range_10d_pct + 1.0))
    pullback_depth_pct = float((latest_price / high_50d - 1) * 100) if high_50d > 0 else 0.0

    # Overextension
    sma_10 = float(close.rolling(10, min_periods=1).mean().iloc[-1]) if len(close) > 0 else latest_price
    extension_10ma_pct = float((latest_price / sma_10 - 1) * 100) if sma_10 > 0 else 0.0
    extension_20ma_pct = distance_from_20ma_pct
    extension_50ema_pct = distance_from_50ema_pct

    return_3d = float((close.iloc[-1] / close.iloc[-4] - 1) * 100) if len(close) >= 4 and close.iloc[-4] > 0 else 0.0
    return_5d = float((close.iloc[-1] / close.iloc[-6] - 1) * 100) if len(close) >= 6 and close.iloc[-6] > 0 else 0.0

    consecutive_up_days = 0
    for idx in range(1, len(close)):
        if close.iloc[-idx] > close.iloc[-idx - 1]:
            consecutive_up_days += 1
        else:
            break
    parabolic_flag = bool(extension_10ma_pct > 15.0 or return_5d > 25.0)

    # Relative strength (with SPY history alignment)
    rs_score_252 = 90.0
    rs_above_sma_50 = False
    rs_slope_20d = 0.0

    # Handle automatic extraction of SPY history from multi-symbol history if not explicitly provided
    if spy_history is None and isinstance(history, pd.DataFrame):
        if isinstance(history.columns, pd.MultiIndex) and "SPY" in history.columns.levels[0]:
            spy_history = history["SPY"].copy()

    if spy_history is not None and not spy_history.empty:
        stock_df = symbol_history.copy()
        stock_df.index = pd.to_datetime(stock_df.index).tz_localize(None)
        spy_df = spy_history.copy()
        spy_df.index = pd.to_datetime(spy_df.index).tz_localize(None)

        merged = pd.DataFrame(index=stock_df.index)
        merged["close"] = stock_df["Close"].astype(float)
        merged["spy_close"] = spy_df["Close"].astype(float)
        merged = merged.dropna().sort_index()

        if not merged.empty:
            relative_strength = merged["close"] / merged["spy_close"].replace(0, pd.NA).astype(float)
            rs_window = relative_strength.iloc[-252:]
            if not rs_window.empty:
                rs_score_252 = float(rs_window.rank(pct=True, method="max").iloc[-1] * 100)

            rs_sma_50 = relative_strength.rolling(50, min_periods=1).mean()
            if not rs_sma_50.empty:
                rs_above_sma_50 = bool(relative_strength.iloc[-1] > rs_sma_50.iloc[-1])

            if len(relative_strength) > 20 and relative_strength.iloc[-21] > 0:
                rs_slope_20d = float((relative_strength.iloc[-1] / relative_strength.iloc[-21] - 1) * 100)

    return {
        "symbol": symbol,
        "name": symbol,

        # Basic tradability
        "price": latest_price,
        "volume": latest_volume,
        "avg_volume_20d": avg_volume_20d,
        "dollar_volume": dollar_volume,
        "avg_dollar_volume_20d": avg_dollar_volume_20d,
        "price_history_days": len(close),

        # Volatility
        "adr": adr_20,
        "adr_20": adr_20,
        "atr_14_pct": atr_14_pct,
        "range_today_pct": range_today_pct,

        # Momentum / growth
        "return_1w": return_1w,
        "return_1m": return_1m,
        "return_3m": return_3m,
        "return_6m": return_6m,
        "growth_rank": 0.0,      # Will be updated by ranker
        "growth_rank_1m": 0.0,   # Will be updated by ranker
        "growth_rank_3m": 0.0,   # Will be updated by ranker

        # Trend
        "sma_20": sma_20,
        "ema_50": ema_50,
        "sma_200": sma_200,
        "above_sma_20": above_sma_20,
        "above_ema_50": above_ema_50,
        "ma_alignment": ma_alignment,
        "distance_from_20ma_pct": distance_from_20ma_pct,
        "distance_from_50ema_pct": distance_from_50ema_pct,
        "trend_intensity": trend_intensity,
        "trend_score": trend_score,

        # Volume quality
        "relative_volume": relative_volume,
        "volume_expansion": volume_expansion,
        "volume_dryup_ratio": volume_dryup_ratio,

        # Breakout / consolidation
        "high_20d": high_20d,
        "high_50d": high_50d,
        "high_252d": high_252d,
        "close_to_52w_high_pct": close_to_52w_high_pct,
        "distance_to_20d_high_pct": distance_to_20d_high_pct,
        "breakout_20d": breakout_20d,
        "breakout_50d": breakout_50d,
        "consolidation_range_10d_pct": consolidation_range_10d_pct,
        "consolidation_tightness": consolidation_tightness,
        "pullback_depth_pct": pullback_depth_pct,

        # Overextension
        "extension_10ma_pct": extension_10ma_pct,
        "extension_20ma_pct": extension_20ma_pct,
        "extension_50ema_pct": extension_50ema_pct,
        "return_3d": return_3d,
        "return_5d": return_5d,
        "consecutive_up_days": consecutive_up_days,
        "parabolic_flag": parabolic_flag,

        # Relative strength
        "rs_score_252": rs_score_252,
        "rs_above_sma_50": rs_above_sma_50,
        "rs_slope_20d": rs_slope_20d,

        # Final scoring
        "score": 0.0,
    }


def get_universe_stock_metrics(
    tickers: List[str],
    period: str = "1y",
    interval: str = "1d",
    max_symbols: int = 200,
) -> List[Dict]:
    """Download universe price history and return computed stock metrics."""
    download_tickers = list(dict.fromkeys(["SPY", *tickers]))
    history = download_price_history(download_tickers, period=period, interval=interval, max_symbols=max_symbols + 1)
    if history.empty:
        return []

    spy_history = None
    if isinstance(history.columns, pd.MultiIndex) and "SPY" in history.columns.levels[0]:
        spy_history = history["SPY"].copy()

    metrics = []
    for symbol in tickers[:max_symbols]:
        result = compute_stock_metrics(symbol, history, spy_history=spy_history)
        if result is not None:
            metrics.append(result)

    if not metrics:
        return []

    # Rank 1-month growth (populated in 'growth_rank' and 'growth_rank_1m')
    growth_values_1m = [item.get("return_1m", 0.0) for item in metrics]
    ranks_1m = pd.Series(growth_values_1m).rank(pct=True, method="max") * 100
    for idx, item in enumerate(metrics):
        item["growth_rank"] = float(ranks_1m.iloc[idx])
        item["growth_rank_1m"] = float(ranks_1m.iloc[idx])

    # Rank 3-month growth
    growth_values_3m = [item.get("return_3m", 0.0) for item in metrics]
    ranks_3m = pd.Series(growth_values_3m).rank(pct=True, method="max") * 100
    for idx, item in enumerate(metrics):
        item["growth_rank_3m"] = float(ranks_3m.iloc[idx])

    return metrics


def get_default_universe(max_symbols: Optional[int] = None, refresh: bool = False) -> List[str]:
    """Return the default KIS-registered US stock universe for scanning."""
    kis_symbols = get_us_kis_tickers(max_symbols=max_symbols, refresh=refresh)
    if kis_symbols:
        return kis_symbols
    return get_sp500_tickers(max_symbols=max_symbols)
