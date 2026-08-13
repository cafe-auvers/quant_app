"""Opening-range breakout strategy plugin."""

from .config import ORB_WINDOWS, ORBStrategyConfig
from .signals import OrbEntrySignal, OrbRange, OrbStrategyEvaluation
from .strategy import (
    ORBStrategy,
    calculate_orb_range,
    evaluate_orb_entry_signal,
    finite_float,
    market_local_index,
)

__all__ = [
    "ORB_WINDOWS",
    "ORBStrategy",
    "ORBStrategyConfig",
    "OrbEntrySignal",
    "OrbRange",
    "OrbStrategyEvaluation",
    "calculate_orb_range",
    "evaluate_orb_entry_signal",
    "finite_float",
    "market_local_index",
]
