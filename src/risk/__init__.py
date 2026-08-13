"""Risk/sizing checks the execution path calls through.

This package is the single authoritative home for pre-trade risk math (today:
position sizing). See docs/next_steps_plan.md P1 for the consolidation
rationale -- this move is behavior-preserving: PositionSizer's thresholds and
math are unchanged, only its module location moved out of src/core/.
"""
from src.risk.position_sizer import PositionSize, PositionSizer, SizingMethod

__all__ = ["PositionSize", "PositionSizer", "SizingMethod"]
