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
from .entry_policy import breakout_trade_confirms, build_passive_pullback_plan
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
    execution_price: Optional[float] = None,
    buffer_pct: float = 0.001,
    confirmation_price: Optional[float] = None,
    allow_probe: bool = False,
) -> OrbEntrySignal:
    """Classify an ORB entry using the approved passive-pullback rules.

    ``buffer_pct``, ``confirmation_price``, and ``allow_probe`` remain in the
    compatibility signature, but do not alter the finalized contract.  A
    breakout is confirmed only by a trade strictly above
    ``max(breakout_price, ORH)``; order submission is evaluated separately
    against a fresh quote and the exact ``execution_price``.
    """
    del buffer_pct, confirmation_price, allow_probe
    high = finite_float(orb_high)
    low = finite_float(orb_low)
    price = finite_float(current_price)
    bp = finite_float(breakout_price)
    execution = finite_float(execution_price)

    def _result(
        *,
        signal: str,
        floor: float = 0.0,
        trigger: float = 0.0,
        confirmed: bool = False,
        effective_execution: Optional[float] = None,
        safe_high: Optional[float] = None,
        safe_low: Optional[float] = None,
    ) -> OrbEntrySignal:
        return OrbEntrySignal(
            orb_high=(
                safe_high
                if safe_high is not None
                else (high if high is not None and high > 0 else 0.0)
            ),
            orb_low=(
                safe_low
                if safe_low is not None
                else (low if low is not None and low > 0 else 0.0)
            ),
            breakout_price=breakout_price,
            entry_floor=floor,
            breakout_trigger=trigger,
            entry_trigger=trigger,
            execution_price=(
                effective_execution
                if effective_execution is not None and effective_execution > 0
                else (execution if execution is not None and execution > 0 else None)
            ),
            current_price=price if price is not None and price > 0 else 0.0,
            breakout_confirmed=confirmed,
            signal=signal,
            allow_entry=confirmed,
            allow_full_size=confirmed,
            suggested_size_multiplier=1.0 if confirmed else 0.0,
        )

    if high is None or low is None or high <= 0 or low <= 0 or low > high:
        return _result(signal="invalid_orb_range")
    if bp is None or bp <= 0:
        return _result(signal="missing_breakout_price")
    plan = build_passive_pullback_plan(
        orb_high=high,
        orb_low=low,
        breakout_price=bp,
        execution_price=execution,
    )
    if plan is None:
        return _result(
            signal="invalid_pullback_geometry",
            floor=max(bp, low),
            trigger=max(bp, high),
        )
    if price is None or price <= 0:
        return _result(
            signal="invalid_current_price",
            floor=plan.floor_price,
            trigger=plan.breakout_trigger,
            effective_execution=plan.execution_price,
        )

    confirmed = breakout_trade_confirms(
        trade_price=price,
        breakout_trigger=plan.breakout_trigger,
    )
    return _result(
        signal="confirmed_orb_breakout" if confirmed else "no_entry",
        floor=plan.floor_price,
        trigger=plan.breakout_trigger,
        confirmed=confirmed,
        effective_execution=plan.execution_price,
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
            execution_price=market.metadata.get("execution_price"),
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
                reference_price=float(entry.execution_price or 0.0),
                trigger_price=entry.breakout_trigger,
                stop_price=orb_range.low,
                reason=entry.signal,
                generated_at=market.as_of,
                metadata={
                    "window": self.config.window,
                    "orb_high": entry.orb_high,
                    "orb_low": entry.orb_low,
                    "breakout_price": entry.breakout_price,
                    "breakout_trigger": entry.breakout_trigger,
                    "entry_floor": entry.entry_floor,
                    "execution_price": entry.execution_price,
                    "breakout_confirmed": entry.breakout_confirmed,
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
