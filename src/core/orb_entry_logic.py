"""Precise rules for confirmed-breakout passive ORB entries.

The execution queue, Kanban bridge, and trading engine all use these helpers
so price-zone validation and timeframe upgrades cannot drift apart.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Optional


ORB_SCORE_VERSION = "ORB_POSITION_SCORE_V1"
ORB_TIMEFRAME_RANK = {"1m": 1, "5m": 5, "30m": 30}


def precise_decimal(value: Any) -> Optional[Decimal]:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() else None


def us_equity_tick(price: Any) -> Optional[Decimal]:
    value = precise_decimal(price)
    if value is None or value <= 0:
        return None
    return Decimal("0.0001") if value < Decimal("1") else Decimal("0.01")


def is_legal_execution_price(price: Any) -> bool:
    value = precise_decimal(price)
    tick = us_equity_tick(value)
    return bool(
        value is not None and tick is not None and value == value.quantize(tick)
    )


def passive_entry_prices(
    *,
    breakout_price: Any,
    orb_high: Any,
    orb_low: Any,
    execution_price: Any = None,
) -> tuple[Optional[Decimal], Optional[Decimal], Optional[Decimal], str]:
    """Return ``(floor, breakout_trigger, execution, reason)``.

    Automated candidates default to ORH. A non-null execution price is
    treated as already configured and is never silently changed.
    """

    breakout = precise_decimal(breakout_price)
    high = precise_decimal(orb_high)
    low = precise_decimal(orb_low)
    execution = precise_decimal(high if execution_price is None else execution_price)
    if any(value is None or value <= 0 for value in (breakout, high, low, execution)):
        return None, None, execution, "ORB prices must be positive and finite"
    assert breakout is not None and high is not None and low is not None
    assert execution is not None
    if low > high:
        return None, None, execution, "ORB low cannot exceed ORB high"
    floor = max(breakout, low)
    trigger = max(breakout, high)
    if floor >= high:
        return floor, trigger, execution, "No valid passive-pullback execution zone"
    if not is_legal_execution_price(execution):
        return (
            floor,
            trigger,
            execution,
            "Execution price is not on a valid U.S. equity tick",
        )
    if not floor < execution <= high:
        return (
            floor,
            trigger,
            execution,
            "Execution price must be strictly above the breakout/ORL floor and at or below ORH",
        )
    return floor, trigger, execution, ""


def score_strictly_higher(replacement: Any, active: Any) -> bool:
    """Scores are established at one decimal place; compare that exact value."""

    replacement_score = precise_decimal(replacement)
    active_score = precise_decimal(active)
    if replacement_score is None or active_score is None:
        return False
    score_tick = Decimal("0.1")
    return replacement_score.quantize(score_tick) > active_score.quantize(score_tick)


def is_later_timeframe(replacement: str, active: str) -> bool:
    return ORB_TIMEFRAME_RANK.get(str(replacement), 0) > ORB_TIMEFRAME_RANK.get(
        str(active), 0
    )
