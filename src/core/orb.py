"""Opening range breakout helpers."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import time
import math
from typing import Iterable, List, Optional

import pandas as pd


ORB_WINDOWS = {
    "1m": 1,
    "5m": 5,
    "30m": 30,
    "1h": 60,
}

_US_MARKET_TIMEZONE = "America/New_York"


def _finite_float(value: object) -> Optional[float]:
    """Return a finite float, or ``None`` for invalid price input."""
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _market_local_index(index: pd.Index) -> Optional[pd.DatetimeIndex]:
    """Normalize source bars to US market time.

    KIS-normalized bars are naive market-local timestamps.  The local cache
    historically stores aware Yahoo timestamps as naive UTC, so detect its
    unambiguous 13:30/14:30 session-opening signature before treating a naive
    index as market-local.
    """
    try:
        timestamps = pd.DatetimeIndex(index)
    except (TypeError, ValueError):
        return None
    if timestamps.empty or timestamps.hasnans:
        return None
    if timestamps.tz is not None:
        return timestamps.tz_convert(_US_MARKET_TIMEZONE)

    local_open = time(9, 30)
    if any(timestamp.time() == local_open for timestamp in timestamps):
        return timestamps

    session_dates = timestamps.normalize()
    utc_open_times = {time(13, 30), time(14, 30)}
    first_times = []
    for session_date in session_dates.unique():
        session_bars = timestamps[session_dates == session_date]
        if session_bars.empty:
            continue
        first_times.append(session_bars[0].time())
    if first_times and all(first_time in utc_open_times for first_time in first_times):
        return timestamps.tz_localize("UTC").tz_convert(_US_MARKET_TIMEZONE)
    return timestamps


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
    high = _finite_float(orb_high)
    low = _finite_float(orb_low)
    price = _finite_float(current_price)
    bp = _finite_float(breakout_price)
    buffer = _finite_float(buffer_pct)

    # Any malformed market or user value must fail closed.  In particular, a
    # negative buffer used to lower the structural trigger and permit an entry
    # below the breakout level.
    if high is None or low is None or high <= 0 or low <= 0 or low > high:
        signal = "invalid_orb_range"
        high = high if high is not None and high > 0 else 0.0
        low = low if low is not None and low > 0 else 0.0
        breakout_trigger = 0.0
        entry_trigger = high
        safe_price = price if price is not None and price > 0 else 0.0
        return OrbEntrySignal(
            orb_high=high,
            orb_low=low,
            breakout_price=breakout_price,
            breakout_trigger=breakout_trigger,
            entry_trigger=entry_trigger,
            current_price=safe_price,
            signal=signal,
            allow_entry=False,
            allow_full_size=False,
            suggested_size_multiplier=0.0,
        )
    if buffer is None or buffer < 0:
        return OrbEntrySignal(
            orb_high=high,
            orb_low=low,
            breakout_price=breakout_price,
            breakout_trigger=0.0,
            entry_trigger=high,
            current_price=price if price is not None and price > 0 else 0.0,
            signal="invalid_buffer_pct",
            allow_entry=False,
            allow_full_size=False,
            suggested_size_multiplier=0.0,
        )
    if bp is None or bp <= 0:
        return OrbEntrySignal(
            orb_high=high,
            orb_low=low,
            breakout_price=breakout_price,
            breakout_trigger=0.0,
            entry_trigger=high,
            current_price=price if price is not None and price > 0 else 0.0,
            signal="missing_breakout_price",
            allow_entry=False,
            allow_full_size=False,
            suggested_size_multiplier=0.0,
        )

    breakout_trigger = bp * (1.0 + buffer)
    if not math.isfinite(breakout_trigger) or breakout_trigger <= 0:
        return OrbEntrySignal(
            orb_high=high,
            orb_low=low,
            breakout_price=breakout_price,
            breakout_trigger=0.0,
            entry_trigger=high,
            current_price=price if price is not None and price > 0 else 0.0,
            signal="invalid_buffer_pct",
            allow_entry=False,
            allow_full_size=False,
            suggested_size_multiplier=0.0,
        )
    if price is None or price <= 0:
        return OrbEntrySignal(
            orb_high=high,
            orb_low=low,
            breakout_price=breakout_price,
            breakout_trigger=breakout_trigger,
            entry_trigger=high,
            current_price=0.0,
            signal="invalid_current_price",
            allow_entry=False,
            allow_full_size=False,
            suggested_size_multiplier=0.0,
        )

    entry_trigger = high
    if high <= breakout_trigger:
        signal = "orb_high_below_breakout_trigger"
        allow_entry = False
        allow_full_size = False
        size_mult = 0.0
    elif price > entry_trigger:
        signal = "confirmed_orb_breakout"
        allow_entry = True
        allow_full_size = True
        size_mult = 1.0
    else:
        confirmation = _finite_float(confirmation_price)
        if (
            allow_probe
            and price > breakout_trigger
            and confirmation is not None
            and price <= confirmation
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
        orb_high=high,
        orb_low=low,
        breakout_price=breakout_price,
        breakout_trigger=breakout_trigger,
        entry_trigger=entry_trigger,
        current_price=price,
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
    if window not in ORB_WINDOWS or not isinstance(intraday, pd.DataFrame) or intraday.empty:
        return None
    if "High" not in intraday.columns or "Low" not in intraday.columns:
        return None

    bars = intraday.sort_index()
    local_index = _market_local_index(bars.index)
    if local_index is None:
        return None
    try:
        latest_session_date = local_index[-1].date()
        start = pd.Timestamp.combine(latest_session_date, market_open)
    except (AttributeError, TypeError, ValueError):
        return None
    if local_index.tz is not None:
        start = start.tz_localize(local_index.tz)
    end = start + pd.Timedelta(minutes=ORB_WINDOWS[window])

    # Intraday data is often cached for several sessions.  ORB levels must use
    # the latest session only, never the first 09:30 bar in a multi-day frame.
    session_mask = local_index.normalize() == start.normalize()
    session_index = local_index[session_mask]
    if session_index.empty or not (session_index == start).any():
        # Do not move the opening range forward to an arbitrary later bar.  A
        # missing opening bar is insufficient evidence for a tradable ORB.
        return None

    # Window is complete only when a bar at or after `end` exists,
    # meaning the last bar inside [start, end) has fully closed.
    if require_complete and session_index[-1] < end:
        return None

    window_mask = session_mask & (local_index >= start) & (local_index < end)
    window_bars = bars[window_mask]
    if window_bars.empty:
        return None

    high = _finite_float(window_bars["High"].max())
    low = _finite_float(window_bars["Low"].min())
    if high is None or low is None or high <= 0 or low <= 0 or low > high:
        return None

    return OrbRange(
        symbol=str(symbol or "").upper(),
        window=window,
        start=start,
        end=end,
        high=high,
        low=low,
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
    # A default hourly resample starts bins at :00, creating a 09:00 bar that
    # mixes pre-open data and makes an opening-hour ORB begin at 10:00.  Anchor
    # every larger interval to the 09:30 US market open instead.
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
