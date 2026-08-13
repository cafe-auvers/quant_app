"""Authoritative ORB position-sizing thresholds and calculations.

These functions preserve the formulas that were previously duplicated in the
watchlist UI, background worker, and execution queue. Keeping the pure checks
here prevents those paths from drifting while leaving their UI wiring intact.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional


MIN_CAPITAL_PERCENT = 10.0
MAX_CAPITAL_PERCENT = 30.0
MIN_STOP_ADR = 15.0
MAX_STOP_ADR = 66.0


def _finite_float(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _zero_position_values() -> Dict[str, Any]:
    return {
        "total_risk": 0.0,
        "risk_per_share": 0.0,
        "shares": 0.0,
        "investment": 0.0,
        "capital_percent": 0.0,
        "stop_loss_percent": 0.0,
        "sl_adr": None,
    }


def calculate_orb_position_values(
    account_size: float,
    risk_percent: float,
    entry_price: float,
    stop_price: float,
    adr_percent: Optional[float] = None,
) -> Dict[str, Any]:
    """Return the existing risk-budgeted ORB position metrics."""
    account = _finite_float(account_size)
    risk_fraction = _finite_float(risk_percent)
    entry = _finite_float(entry_price)
    stop = _finite_float(stop_price)
    if (
        account is None
        or account <= 0
        or risk_fraction is None
        or risk_fraction <= 0
        or risk_fraction > 1.0
        or entry is None
        or entry <= 0
        or stop is None
        or stop <= 0
        or stop >= entry
    ):
        return _zero_position_values()

    total_risk = account * risk_fraction
    risk_per_share = entry - stop
    raw_shares = total_risk / risk_per_share
    shares = float(math.ceil(raw_shares)) if raw_shares > 0 else 0.0
    investment = shares * entry
    capital_percent = investment / account * 100.0
    stop_loss_percent = risk_per_share / entry * 100.0
    normalized_adr = _finite_float(adr_percent) if adr_percent is not None else None
    sl_adr = (
        (stop_loss_percent / normalized_adr * 100.0)
        if normalized_adr is not None and normalized_adr > 0
        else None
    )
    return {
        "total_risk": total_risk,
        "risk_per_share": risk_per_share,
        "shares": shares,
        "investment": investment,
        "capital_percent": capital_percent,
        "stop_loss_percent": stop_loss_percent,
        "sl_adr": sl_adr,
    }


def is_orb_position_plan_valid(
    sizing: Dict[str, Any], adr_percent: Optional[float]
) -> bool:
    """Apply the established ORB hard limits without changing thresholds."""
    if sizing.get("shares", 0.0) < 1.0:
        return False
    capital_percent = sizing.get("capital_percent", 0.0)
    if capital_percent < MIN_CAPITAL_PERCENT or capital_percent >= MAX_CAPITAL_PERCENT:
        return False
    stop_loss_percent = sizing.get("stop_loss_percent", 0.0)
    if (
        adr_percent is not None
        and adr_percent > 0
        and stop_loss_percent >= adr_percent
    ):
        return False
    sl_adr = sizing.get("sl_adr")
    if sl_adr is not None and (sl_adr < MIN_STOP_ADR or sl_adr > MAX_STOP_ADR):
        return False
    return True


def validate_orb_position_values(
    sizing: Dict[str, Any], adr_percent: Optional[float]
) -> List[str]:
    """Return the execution queue's existing human-readable risk warnings."""
    warnings: List[str] = []
    shares = int(sizing.get("shares", 0) or 0)
    capital_percent = float(sizing.get("capital_percent", 0.0) or 0.0)
    stop_loss_percent = float(sizing.get("stop_loss_percent", 0.0) or 0.0)
    stop_adr = sizing.get("sl_adr")

    if shares < 1:
        warnings.append("Position size calculation resulted in 0 shares")
    if capital_percent < MIN_CAPITAL_PERCENT:
        warnings.append(
            f"Capital allocation ({capital_percent:.2f}%) is below "
            f"{MIN_CAPITAL_PERCENT:.0f}%"
        )
    if capital_percent >= MAX_CAPITAL_PERCENT:
        warnings.append(
            f"Capital allocation ({capital_percent:.2f}%) exceeds "
            f"{MAX_CAPITAL_PERCENT:.0f}%"
        )
    if adr_percent is not None and adr_percent > 0 and stop_loss_percent >= adr_percent:
        warnings.append(
            f"Stop loss % ({stop_loss_percent:.2f}%) is wider than ADR "
            f"({adr_percent:.2f}%)"
        )
    if stop_adr is not None:
        stop_adr_value = float(stop_adr)
        if stop_adr_value < MIN_STOP_ADR:
            warnings.append(
                f"Stop/ADR ({stop_adr_value:.2f}%) is below {MIN_STOP_ADR:.0f}%"
            )
        elif stop_adr_value > MAX_STOP_ADR:
            warnings.append(
                f"Stop/ADR ({stop_adr_value:.2f}%) exceeds {MAX_STOP_ADR:.0f}%"
            )
    return warnings


def score_orb_position_recommendation(
    sizing: Dict[str, Any], risk_percent: float
) -> float:
    """Score an ORB plan using the existing weighting and target values."""
    sl_adr = sizing.get("sl_adr")
    capital_percent = sizing.get("capital_percent", 0.0)
    if sl_adr is None:
        return 0.0
    sl_adr_score = max(0.0, 100.0 - abs(float(sl_adr) - 65.0) * 3.0)
    capital_score = max(0.0, 100.0 - abs(float(capital_percent) - 17.5) * 4.0)
    risk_score = max(0.0, 100.0 - float(risk_percent) * 100.0 * 25.0)
    return round(
        (sl_adr_score * 0.45) + (capital_score * 0.40) + (risk_score * 0.15),
        1,
    )
