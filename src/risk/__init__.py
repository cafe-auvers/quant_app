"""Risk/sizing checks the execution path calls through.

This package is the single authoritative home for pre-trade risk math (today:
position sizing and ORB position-plan validation). PositionSizer's thresholds
and math are unchanged; shared ORB checks are kept here so UI, worker, and
execution paths cannot drift independently.
"""
from src.risk.position_sizer import PositionSize, PositionSizer, SizingMethod
from src.risk.orb_position import (
    MAX_CAPITAL_PERCENT,
    MAX_STOP_ADR,
    MIN_CAPITAL_PERCENT,
    MIN_STOP_ADR,
    calculate_orb_position_values,
    is_orb_position_plan_valid,
    score_orb_position_recommendation,
    validate_orb_position_values,
)

__all__ = [
    "MAX_CAPITAL_PERCENT",
    "MAX_STOP_ADR",
    "MIN_CAPITAL_PERCENT",
    "MIN_STOP_ADR",
    "PositionSize",
    "PositionSizer",
    "SizingMethod",
    "calculate_orb_position_values",
    "is_orb_position_plan_valid",
    "score_orb_position_recommendation",
    "validate_orb_position_values",
]
