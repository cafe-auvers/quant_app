"""Intraday helper functions shared by UI and workers."""
from __future__ import annotations

import datetime as dt

import pandas as pd

from src.utils.data_loader import _extract_symbol_history


def utcnow_naive() -> dt.datetime:
    """Return a naive UTC timestamp for existing DB/storage comparisons."""
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def extract_latest_opening_bar(history: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """Return the first available 1-minute bar for the latest cached session."""
    symbol_history = _extract_symbol_history(history, symbol)
    if symbol_history is None or symbol_history.empty:
        return pd.DataFrame()
    bars = symbol_history.sort_index()
    session_dates = pd.to_datetime(bars.index).date
    latest_date = session_dates[-1]
    session = bars[session_dates == latest_date]
    if session.empty:
        return pd.DataFrame()
    return session.head(1)


def intraday_cache_needs_backfill(cached: pd.DataFrame, since: dt.datetime) -> bool:
    if cached.empty:
        return True
    oldest = pd.Timestamp(cached.index.min()).tz_localize(None)
    return oldest > pd.Timestamp(since) + pd.Timedelta(hours=12)
