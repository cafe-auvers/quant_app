"""Compatibility helpers for the built-in ORB strategy.

New strategy execution lives under :mod:`src.strategy.orb`. Existing imports
remain stable while UI callers are migrated incrementally.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time
from typing import Iterable, List, Optional

import pandas as pd

from src.strategy.orb import (
    ORB_WINDOWS,
    OrbEntrySignal,
    OrbRange,
    calculate_orb_range,
    evaluate_orb_entry_signal,
    finite_float as _finite_float,
    market_local_index as _market_local_index,
)


@dataclass(frozen=True)
class OrbSignal:
    symbol: str
    window: str
    latest_price: float
    range_high: float
    range_low: float
    breakout: Optional[str]
    target_met: bool  # Deprecated compatibility alias for breakout_confirmed.
    target_price: Optional[float] = None  # Deprecated compatibility alias.
    breakout_confirmed: bool = False
    breakout_price: Optional[float] = None


def evaluate_orb_signal(
    symbol: str,
    intraday: pd.DataFrame,
    window: str,
    breakout_price: Optional[float] = None,
    target_price: Optional[float] = None,
    buffer_pct: float = 0.001,
    market_open: time = time(9, 30),
) -> Optional[OrbSignal]:
    """Return the legacy display signal using the strategy-owned calculations."""
    orb_range = calculate_orb_range(
        symbol=symbol,
        intraday=intraday,
        window=window,
        market_open=market_open,
    )
    if (
        orb_range is None
        or not isinstance(intraday, pd.DataFrame)
        or "Close" not in intraday.columns
        or intraday.empty
    ):
        return None

    latest_price = _finite_float(intraday.sort_index()["Close"].iloc[-1])
    if latest_price is None or latest_price <= 0:
        return None
    breakout = None
    if latest_price > orb_range.high:
        breakout = "up"
    elif latest_price < orb_range.low:
        breakout = "down"

    if breakout_price is None and target_price is not None:
        breakout_price = target_price
    entry_signal = evaluate_orb_entry_signal(
        orb_high=orb_range.high,
        orb_low=orb_range.low,
        breakout_price=breakout_price,
        current_price=latest_price,
        buffer_pct=buffer_pct,
    )
    breakout_confirmed = entry_signal.allow_entry

    return OrbSignal(
        symbol=str(symbol or "").upper(),
        window=window,
        latest_price=latest_price,
        range_high=orb_range.high,
        range_low=orb_range.low,
        breakout=breakout,
        target_met=breakout_confirmed,
        target_price=breakout_price,
        breakout_confirmed=breakout_confirmed,
        breakout_price=breakout_price,
    )


def resample_intraday_bars(intraday: pd.DataFrame, interval: str) -> pd.DataFrame:
    """Resample intraday bars into a larger OHLCV interval."""
    if not isinstance(intraday, pd.DataFrame):
        raise TypeError("intraday must be a pandas DataFrame")
    if interval in {"1m", "5m"} or intraday.empty:
        return intraday.copy()

    rule_map = {"30m": "30min", "1h": "60min"}
    rule = rule_map.get(interval)
    if rule is None:
        raise ValueError(f"Unsupported intraday interval: {interval}")

    aggregations = {
        "Open": "first",
        "High": "max",
        "Low": "min",
        "Close": "last",
        "Volume": "sum",
    }
    available = {
        key: value for key, value in aggregations.items() if key in intraday.columns
    }
    bars = intraday.sort_index().copy()
    local_index = _market_local_index(bars.index)
    if local_index is None:
        return pd.DataFrame(columns=list(available))
    bars.index = local_index
    return (
        bars.resample(
            rule,
            origin="start_day",
            offset=pd.Timedelta(hours=9, minutes=30),
        )
        .agg(available)
        .dropna(how="any")
    )


def evaluate_watchlist_orb_signals(
    watchlist_items: Iterable[object],
    intraday_by_symbol: dict[str, pd.DataFrame],
    window: str,
) -> List[OrbSignal]:
    signals: List[OrbSignal] = []
    for item in watchlist_items:
        symbol = str(getattr(item, "symbol", "")).upper()
        if not symbol:
            continue
        signal = evaluate_orb_signal(
            symbol=symbol,
            intraday=intraday_by_symbol.get(symbol, pd.DataFrame()),
            window=window,
            breakout_price=getattr(item, "breakout_price", None),
        )
        if signal is not None:
            signals.append(signal)
    return signals


__all__ = [
    "ORB_WINDOWS",
    "OrbEntrySignal",
    "OrbRange",
    "OrbSignal",
    "calculate_orb_range",
    "evaluate_orb_entry_signal",
    "evaluate_orb_signal",
    "evaluate_watchlist_orb_signals",
    "resample_intraday_bars",
]
