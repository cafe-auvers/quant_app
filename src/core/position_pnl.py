"""Read-only gross position and hypothetical exit estimates."""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class PositionPnl:
    dollars: float
    percent: float
    proceeds: float


def estimate_position_pnl(average_cost, price, quantity) -> PositionPnl | None:
    try:
        cost, mark, shares = float(average_cost), float(price), float(quantity)
    except (TypeError, ValueError, OverflowError):
        return None
    if not all(math.isfinite(value) and value > 0 for value in (cost, mark, shares)):
        return None
    if not shares.is_integer():
        return None
    dollars, proceeds = (mark - cost) * shares, mark * shares
    percent = (mark / cost - 1) * 100
    if not all(math.isfinite(value) for value in (dollars, proceeds, percent)):
        return None
    return PositionPnl(dollars=dollars, percent=percent, proceeds=proceeds)
