"""Configuration for the existing opening-range breakout strategy."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time


ORB_WINDOWS = {
    "1m": 1,
    "5m": 5,
    "30m": 30,
    "1h": 60,
}
US_MARKET_TIMEZONE = "America/New_York"


@dataclass(frozen=True)
class ORBStrategyConfig:
    window: str = "5m"
    buffer_pct: float = 0.001
    market_open: time = time(9, 30)
    require_complete: bool = True
    confirmation_price: float | None = None
    allow_probe: bool = False
