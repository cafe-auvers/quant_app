"""Opening-range breakout strategy plugin."""

from .config import ORB_WINDOWS, ORBStrategyConfig
from .entry_policy import (
    ORB_ENTRY_POLICY_VERSION,
    PassivePullbackPlan,
    breakout_trade_confirms,
    build_passive_pullback_plan,
    is_strict_higher_timeframe_upgrade,
    passive_limit_submission_ready,
)
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
    "ORB_ENTRY_POLICY_VERSION",
    "PassivePullbackPlan",
    "OrbEntrySignal",
    "OrbRange",
    "OrbStrategyEvaluation",
    "calculate_orb_range",
    "breakout_trade_confirms",
    "build_passive_pullback_plan",
    "evaluate_orb_entry_signal",
    "finite_float",
    "is_strict_higher_timeframe_upgrade",
    "market_local_index",
    "passive_limit_submission_ready",
]
