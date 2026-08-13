"""Compatibility import for the position sizer's pre-P1 module path.

New code should import from :mod:`src.risk.position_sizer`. This shim keeps
older scripts and integrations working while risk logic moves to its dedicated
package.
"""
from src.risk.position_sizer import PositionSize, PositionSizer, SizingMethod

__all__ = ["PositionSize", "PositionSizer", "SizingMethod"]
