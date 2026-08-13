"""ORB-specific assessments produced before the generic signal boundary."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pandas as pd

from src.strategy.base import Signal


@dataclass(frozen=True)
class OrbRange:
    symbol: str
    window: str
    start: pd.Timestamp
    end: pd.Timestamp
    high: float
    low: float


@dataclass(frozen=True)
class OrbEntrySignal:
    """Entry classification retained for compatibility with existing ORB callers."""

    orb_high: float
    orb_low: float
    breakout_price: Optional[float]
    breakout_trigger: float
    entry_trigger: float
    current_price: float
    signal: str
    allow_entry: bool
    allow_full_size: bool
    suggested_size_multiplier: float


@dataclass(frozen=True)
class OrbStrategyEvaluation:
    """Complete ORB evaluation, including non-actionable classifications."""

    orb_range: Optional[OrbRange]
    entry: Optional[OrbEntrySignal]
    signal: Optional[Signal]
    reason: str = ""
