"""Compatibility facade for the canonical passive ORB entry contract.

The production queue/runtime owns this policy in :mod:`src.core.orb_entry_logic`.
Legacy strategy and UI callers use the helpers here so they cannot retain a
second, drifting interpretation of the same price and timeframe rules.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from src.core.orb_entry_logic import (
    is_later_timeframe,
    passive_entry_prices,
    precise_decimal,
    score_strictly_higher,
)


SUPPORTED_ENTRY_WINDOWS = ("1m", "5m", "30m")
ORB_ENTRY_POLICY_VERSION = "PASSIVE_PULLBACK_V1"


@dataclass(frozen=True)
class PassivePullbackPlan:
    """Validated price geometry for one ORB window.

    ``breakout_trigger`` confirms momentum.  ``execution_price`` is the exact
    passive BUY limit used for the later pullback; it is never replaced with a
    marketable quote-derived price.
    """

    orb_high: float
    orb_low: float
    breakout_price: float
    floor_price: float
    breakout_trigger: float
    execution_price: float


def build_passive_pullback_plan(
    *,
    orb_high: object,
    orb_low: object,
    breakout_price: object,
    execution_price: object,
) -> Optional[PassivePullbackPlan]:
    """Return the approved pullback plan, or ``None`` when it is invalid.

    The price contract is::

        floor = max(breakout_price, ORL)
        trigger = max(breakout_price, ORH)
        floor < execution_price <= ORH
    """

    floor, trigger, execution, reason = passive_entry_prices(
        breakout_price=breakout_price,
        orb_high=orb_high,
        orb_low=orb_low,
        execution_price=execution_price,
    )
    high = precise_decimal(orb_high)
    low = precise_decimal(orb_low)
    breakout = precise_decimal(breakout_price)
    if reason or None in (floor, trigger, execution, high, low, breakout):
        return None
    assert floor is not None and trigger is not None and execution is not None
    assert high is not None and low is not None and breakout is not None
    return PassivePullbackPlan(
        orb_high=float(high),
        orb_low=float(low),
        breakout_price=float(breakout),
        floor_price=float(floor),
        breakout_trigger=float(trigger),
        execution_price=float(execution),
    )


def breakout_trade_confirms(*, trade_price: object, breakout_trigger: object) -> bool:
    """A post-range trade confirms only when it is strictly above the trigger."""

    trade = precise_decimal(trade_price)
    trigger = precise_decimal(breakout_trigger)
    return bool(
        trade is not None
        and trigger is not None
        and trade > 0
        and trigger > 0
        and trade > trigger
    )


def passive_limit_submission_ready(
    *, last_trade: object, best_ask: object, execution_price: object
) -> bool:
    """Return whether a fresh quote permits the exact passive BUY limit.

    Equality deliberately fails closed.  Both the last trade and best ask must
    be strictly above the intended execution price so the order rests for a
    pullback instead of crossing the market.
    """

    last = precise_decimal(last_trade)
    ask = precise_decimal(best_ask)
    execution = precise_decimal(execution_price)
    return bool(
        last is not None
        and ask is not None
        and execution is not None
        and last > 0
        and ask > 0
        and execution > 0
        and last > execution
        and ask > execution
    )


def is_strict_higher_timeframe_upgrade(
    *,
    current_window: object,
    current_score: object,
    candidate_window: object,
    candidate_score: object,
) -> bool:
    """Accept only a strictly higher timeframe with a strictly higher score."""

    return (
        str(current_window or "") in SUPPORTED_ENTRY_WINDOWS
        and str(candidate_window or "") in SUPPORTED_ENTRY_WINDOWS
        and is_later_timeframe(str(candidate_window), str(current_window))
        and score_strictly_higher(candidate_score, current_score)
    )


__all__ = [
    "PassivePullbackPlan",
    "ORB_ENTRY_POLICY_VERSION",
    "SUPPORTED_ENTRY_WINDOWS",
    "breakout_trade_confirms",
    "build_passive_pullback_plan",
    "is_strict_higher_timeframe_upgrade",
    "passive_limit_submission_ready",
]
