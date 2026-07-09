"""Opening range breakout helpers."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import time
from typing import Iterable, List, Optional

import pandas as pd


ORB_WINDOWS = {
    "1m": 1,
    "5m": 5,
    "30m": 30,
    "1h": 60,
}


@dataclass(frozen=True)
class OrbRange:
    symbol: str
    window: str
    start: pd.Timestamp
    end: pd.Timestamp
    high: float
    low: float


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


@dataclass(frozen=True)
class OrbEntrySignal:
    """Entry classification combining ORB levels with a daily structural breakout price."""
    orb_high: float
    orb_low: float
    breakout_price: Optional[float]
    breakout_trigger: float           # breakout_price * (1 + buffer_pct)
    entry_trigger: float              # ORB high; valid only after it clears breakout_trigger
    current_price: float
    signal: str                       # see evaluate_orb_entry_signal for values
    allow_entry: bool
    allow_full_size: bool
    suggested_size_multiplier: float  # 1.0 = full, 0.5 = partial, 0.0 = no entry


def evaluate_orb_entry_signal(
    orb_high: float,
    orb_low: float,
    breakout_price: Optional[float],
    current_price: float,
    buffer_pct: float = 0.001,
    confirmation_price: Optional[float] = None,
    allow_probe: bool = False,
) -> OrbEntrySignal:
    """Classify an ORB trade entry given ORB levels and a daily structural breakout price.

    Signals:
      confirmed_orb_breakout               — price > entry_trigger → full entry allowed
      orb_only_inside_base                 — price > orb_high but below breakout_trigger → no entry
      structural_breakout_not_fully_confirmed — (probe mode) above breakout_trigger but below
                                               optional confirmation_price → partial entry only
      no_entry                             — price has not cleared orb_high → no entry
    """
    bp = breakout_price if (breakout_price is not None and breakout_price > 0) else 0.0
    breakout_trigger = bp * (1 + buffer_pct) if bp > 0 else 0.0
    entry_trigger = orb_high

    if bp <= 0:
        signal = "missing_breakout_price"
        allow_entry = False
        allow_full_size = False
        size_mult = 0.0
    elif orb_high <= breakout_trigger:
        signal = "orb_high_below_breakout_trigger"
        allow_entry = False
        allow_full_size = False
        size_mult = 0.0
    elif current_price > entry_trigger:
        signal = "confirmed_orb_breakout"
        allow_entry = True
        allow_full_size = True
        size_mult = 1.0
    elif (
        allow_probe and bp > 0
        and current_price > breakout_trigger
        and confirmation_price is not None
        and current_price <= confirmation_price
    ):
        signal = "structural_breakout_not_fully_confirmed"
        allow_entry = True
        allow_full_size = False
        size_mult = 0.5
    else:
        signal = "no_entry"
        allow_entry = False
        allow_full_size = False
        size_mult = 0.0

    return OrbEntrySignal(
        orb_high=orb_high,
        orb_low=orb_low,
        breakout_price=breakout_price,
        breakout_trigger=breakout_trigger,
        entry_trigger=entry_trigger,
        current_price=current_price,
        signal=signal,
        allow_entry=allow_entry,
        allow_full_size=allow_full_size,
        suggested_size_multiplier=size_mult,
    )


def calculate_orb_range(
    symbol: str,
    intraday: pd.DataFrame,
    window: str,
    market_open: time = time(9, 30),
    require_complete: bool = True,
) -> Optional[OrbRange]:
    """Calculate opening-range high/low from intraday bars.

    Returns None if the window has not yet fully elapsed (require_complete=True,
    the default) — a bar timestamped at or after `end` must exist, confirming
    the last period inside the window has closed.
    """
    if window not in ORB_WINDOWS or intraday.empty:
        return None
    if "High" not in intraday.columns or "Low" not in intraday.columns:
        return None

    bars = intraday.sort_index()
    if bars.index.tz is not None:
        local_index = bars.index
    else:
        local_index = bars.index.tz_localize(None)

    start_candidates = [idx for idx in local_index if idx.time() >= market_open]
    if not start_candidates:
        return None

    start = pd.Timestamp(start_candidates[0])
    end = start + pd.Timedelta(minutes=ORB_WINDOWS[window])

    # Window is complete only when a bar at or after `end` exists,
    # meaning the last bar inside [start, end) has fully closed.
    if require_complete and local_index[-1] < end:
        return None

    window_bars = bars[(local_index >= start) & (local_index < end)]
    if window_bars.empty:
        return None

    return OrbRange(
        symbol=symbol.upper(),
        window=window,
        start=start,
        end=end,
        high=float(window_bars["High"].max()),
        low=float(window_bars["Low"].min()),
    )


def evaluate_orb_signal(
    symbol: str,
    intraday: pd.DataFrame,
    window: str,
    breakout_price: Optional[float] = None,
    target_price: Optional[float] = None,
    buffer_pct: float = 0.001,
    market_open: time = time(9, 30),
) -> Optional[OrbSignal]:
    orb_range = calculate_orb_range(
        symbol=symbol,
        intraday=intraday,
        window=window,
        market_open=market_open,
    )
    if orb_range is None or "Close" not in intraday.columns or intraday.empty:
        return None

    latest_price = float(intraday.sort_index()["Close"].iloc[-1])
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
        symbol=symbol.upper(),
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
    available = {key: value for key, value in aggregations.items() if key in intraday.columns}
    return intraday.sort_index().resample(rule).agg(available).dropna(how="any")


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
