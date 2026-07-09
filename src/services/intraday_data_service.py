"""Intraday provider orchestration and cache-source selection."""
from __future__ import annotations

import datetime as dt
from typing import Optional, Tuple

import pandas as pd
from sqlalchemy.engine import Engine

from src.api.kis_intraday import is_kis_intraday_enabled
from src.services.intraday_provider import (
    IntradayProviderName,
    IntradayRequest,
    IntradayResult,
    empty_intraday_result,
)
from src.services.kis_intraday_provider import fetch_kis_intraday
from src.services.yfinance_intraday_provider import fetch_yfinance_intraday
from src.utils.db_loader import load_intraday_history_from_db


def fetch_intraday_with_fallback(request: IntradayRequest) -> IntradayResult:
    warnings = []
    if is_kis_intraday_enabled():
        try:
            kis_result = fetch_kis_intraday(request)
            if not kis_result.bars.empty:
                return kis_result
            warnings.extend(kis_result.warnings or ["KIS intraday returned no bars."])
        except Exception as exc:
            warnings.append(f"KIS intraday failed/unavailable; used yfinance fallback. Details: {exc}")

        if not request.allow_fallback:
            return empty_intraday_result(
                request,
                source=IntradayProviderName.NONE,
                warning="KIS intraday failed/unavailable and fallback is disabled.",
            )
    else:
        warnings.append("KIS intraday disabled/unconfigured.")
        if not request.allow_fallback:
            return empty_intraday_result(
                request,
                source=IntradayProviderName.NONE,
                warning="KIS intraday disabled/unconfigured and fallback is disabled.",
            )

    if request.allow_fallback:
        yfinance_result = fetch_yfinance_intraday(request)
        yfinance_result.warnings = warnings + yfinance_result.warnings
        return yfinance_result

    return empty_intraday_result(
        request,
        source=IntradayProviderName.NONE,
        warning="No intraday provider available and fallback is disabled.",
    )


def load_best_intraday_history(
    symbol: str,
    engine: Engine,
    interval: str,
    since: Optional[dt.datetime] = None,
) -> Tuple[pd.DataFrame, str]:
    """Load cached intraday bars, preferring KIS over yfinance.

    This leaves load_intraday_history_from_db unchanged and only adds provider
    preference for callers that need the best available cache.
    """
    for source in (
        IntradayProviderName.KIS.value,
        IntradayProviderName.YFINANCE.value,
    ):
        bars = load_intraday_history_from_db(symbol, engine, interval=interval, source=source, since=since)
        if not bars.empty:
            return bars, source

    legacy = load_intraday_history_from_db(symbol, engine, interval=interval, source=None, since=since)
    if not legacy.empty:
        return legacy, ""
    return pd.DataFrame(), IntradayProviderName.NONE.value


def format_intraday_source_label(source: str, warnings: Optional[list[str]] = None) -> str:
    warnings = warnings or []
    source = str(source or "").lower()
    if source == IntradayProviderName.KIS.value:
        return "Live data source: KIS intraday."
    if source == IntradayProviderName.YFINANCE.value:
        if any("KIS intraday failed/unavailable" in warning for warning in warnings) or is_kis_intraday_enabled():
            return "Live data source: yfinance fallback after KIS failure."
        return "Live data source: yfinance fallback; KIS intraday disabled/unconfigured."
    return "Live data source: none; KIS intraday disabled/unconfigured and fallback unavailable."
