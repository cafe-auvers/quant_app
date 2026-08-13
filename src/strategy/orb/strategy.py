"""Opening-range breakout strategy implementation.

The calculations in this module are the behavior-preserving implementation
formerly owned by ``src.core.orb``. Live and future backtest callers therefore
share the same range and entry-trigger semantics.
"""

from __future__ import annotations

import math
from datetime import time
from typing import Optional

import pandas as pd

from src.strategy.base import (
    MarketSnapshot,
    PortfolioSnapshot,
    Signal,
    SignalDirection,
    SignalKind,
)

from .config import ORB_WINDOWS, US_MARKET_TIMEZONE, ORBStrategyConfig
from .signals import OrbEntrySignal, OrbRange, OrbStrategyEvaluation


def finite_float(value: object) -> Optional[float]:
    """Return a finite float, or ``None`` for invalid price input."""
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def market_local_index(index: pd.Index) -> Optional[pd.DatetimeIndex]:
    """Normalize source bars to U.S. market time without changing legacy rules."""
    try:
        timestamps = pd.DatetimeIndex(index)
    except (TypeError, ValueError):
        return None
    if timestamps.empty or timestamps.hasnans:
        return None
    if timestamps.tz is not None:
        return timestamps.tz_convert(US_MARKET_TIMEZONE)

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
        return timestamps.tz_localize("UTC").tz_convert(US_MARKET_TIMEZONE)
    return timestamps


def calculate_orb_range(
    symbol: str,
    intraday: pd.DataFrame,
    window: str,
    market_open: time = time(9, 30),
    require_complete: bool = True,
) -> Optional[OrbRange]:
    """Calculate the completed opening range using the established behavior."""
    if (
        window not in ORB_WINDOWS
        or not isinstance(intraday, pd.DataFrame)
        or intraday.empty
    ):
        return None
    if "High" not in intraday.columns or "Low" not in intraday.columns:
        return None

    bars = intraday.sort_index()
    local_index = market_local_index(bars.index)
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

    session_mask = local_index.normalize() == start.normalize()
    session_index = local_index[session_mask]
    if session_index.empty or not (session_index == start).any():
        return None
    if require_complete and session_index[-1] < end:
        return None

    window_mask = session_mask & (local_index >= start) & (local_index < end)
    window_bars = bars[window_mask]
    if window_bars.empty:
        return None

    high = finite_float(window_bars["High"].max())
    low = finite_float(window_bars["Low"].min())
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


def evaluate_orb_entry_signal(
    orb_high: float,
    orb_low: float,
    breakout_price: Optional[float],
    current_price: float,
    buffer_pct: float = 0.001,
    confirmation_price: Optional[float] = None,
    allow_probe: bool = False,
) -> OrbEntrySignal:
    """Classify an ORB entry with the existing fail-closed trigger rules."""
    high = finite_float(orb_high)
    low = finite_float(orb_low)
    price = finite_float(current_price)
    bp = finite_float(breakout_price)
    buffer = finite_float(buffer_pct)

    if high is None or low is None or high <= 0 or low <= 0 or low > high:
        high = high if high is not None and high > 0 else 0.0
        low = low if low is not None and low > 0 else 0.0
        safe_price = price if price is not None and price > 0 else 0.0
        return OrbEntrySignal(
            orb_high=high,
            orb_low=low,
            breakout_price=breakout_price,
            breakout_trigger=0.0,
            entry_trigger=high,
            current_price=safe_price,
            signal="invalid_orb_range",
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
        signal_key = "orb_high_below_breakout_trigger"
        allow_entry = False
        allow_full_size = False
        size_multiplier = 0.0
    elif price > entry_trigger:
        signal_key = "confirmed_orb_breakout"
        allow_entry = True
        allow_full_size = True
        size_multiplier = 1.0
    else:
        confirmation = finite_float(confirmation_price)
        if (
            allow_probe
            and price > breakout_trigger
            and confirmation is not None
            and price <= confirmation
        ):
            signal_key = "structural_breakout_not_fully_confirmed"
            allow_entry = True
            allow_full_size = False
            size_multiplier = 0.5
        else:
            signal_key = "no_entry"
            allow_entry = False
            allow_full_size = False
            size_multiplier = 0.0

    return OrbEntrySignal(
        orb_high=high,
        orb_low=low,
        breakout_price=breakout_price,
        breakout_trigger=breakout_trigger,
        entry_trigger=entry_trigger,
        current_price=price,
        signal=signal_key,
        allow_entry=allow_entry,
        allow_full_size=allow_full_size,
        suggested_size_multiplier=size_multiplier,
    )


class ORBStrategy:
    """The existing long-only ORB entry rule behind the common Strategy shape."""

    strategy_id = "ORB"

    def __init__(self, config: Optional[ORBStrategyConfig] = None) -> None:
        self.config = config or ORBStrategyConfig()

    def evaluate(
        self,
        market: MarketSnapshot,
        portfolio: PortfolioSnapshot,
    ) -> OrbStrategyEvaluation:
        del portfolio  # Reserved for portfolio-aware strategies and P4 parity.
        bars = market.bars
        orb_range = calculate_orb_range(
            symbol=market.symbol,
            intraday=bars,
            window=self.config.window,
            market_open=self.config.market_open,
            require_complete=self.config.require_complete,
        )
        if orb_range is None:
            return OrbStrategyEvaluation(
                orb_range=None,
                entry=None,
                signal=None,
                reason="ORB window has not completed or market data is invalid",
            )

        entry = evaluate_orb_entry_signal(
            orb_high=orb_range.high,
            orb_low=orb_range.low,
            breakout_price=market.metadata.get("breakout_price"),
            current_price=market.current_price or 0.0,
            buffer_pct=self.config.buffer_pct,
            confirmation_price=self.config.confirmation_price,
            allow_probe=self.config.allow_probe,
        )
        signal = None
        if entry.allow_entry:
            signal = Signal(
                strategy_id=self.strategy_id,
                symbol=market.symbol,
                direction=SignalDirection.LONG,
                kind=SignalKind.ENTRY,
                reference_price=entry.entry_trigger,
                trigger_price=entry.entry_trigger,
                stop_price=orb_range.low,
                reason=entry.signal,
                generated_at=market.as_of,
                metadata={
                    "window": self.config.window,
                    "orb_high": entry.orb_high,
                    "orb_low": entry.orb_low,
                    "breakout_price": entry.breakout_price,
                    "breakout_trigger": entry.breakout_trigger,
                    "market_price": entry.current_price,
                    "allow_full_size": entry.allow_full_size,
                    "size_multiplier": entry.suggested_size_multiplier,
                },
            )
        return OrbStrategyEvaluation(
            orb_range=orb_range,
            entry=entry,
            signal=signal,
            reason=entry.signal,
        )

    def generate_signal(
        self,
        market: MarketSnapshot,
        portfolio: PortfolioSnapshot,
    ) -> Optional[Signal]:
        return self.evaluate(market, portfolio).signal
