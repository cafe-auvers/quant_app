"""Authoritative ORB position-sizing settings and calculations.

These functions preserve the formulas that were previously duplicated in the
watchlist UI, background worker, and execution queue. Keeping the pure checks
here prevents those paths from drifting while leaving their UI wiring intact.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Mapping, Optional


@dataclass(frozen=True)
class OrbSettings:
    """User-adjustable ORB validity bounds and scoring ideals."""

    capital_min_percent: float = 10.0
    capital_ideal_percent: float = 17.5
    capital_max_percent: float = 30.0
    stop_adr_min_percent: float = 15.0
    stop_adr_ideal_percent: float = 65.0
    stop_adr_max_percent: float = 66.0

    def __post_init__(self) -> None:
        values = tuple(asdict(self).values())
        if not all(math.isfinite(value) for value in values):
            raise ValueError("ORB settings must be finite numbers")
        if self.capital_min_percent < 0 or self.capital_max_percent > 100:
            raise ValueError("Capital allocation bounds must be between 0% and 100%")
        if self.stop_adr_min_percent < 0:
            raise ValueError("Stop/ADR bounds cannot be negative")
        if not (
            self.capital_min_percent
            <= self.capital_ideal_percent
            <= self.capital_max_percent
        ):
            raise ValueError("Capital ideal must be between its lower and upper bounds")
        if not (
            self.stop_adr_min_percent
            <= self.stop_adr_ideal_percent
            <= self.stop_adr_max_percent
        ):
            raise ValueError("Stop/ADR ideal must be between its lower and upper bounds")
        if self.capital_min_percent >= self.capital_max_percent:
            raise ValueError("Capital lower bound must be below its upper bound")
        if self.stop_adr_min_percent >= self.stop_adr_max_percent:
            raise ValueError("Stop/ADR lower bound must be below its upper bound")

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any] | None) -> "OrbSettings":
        """Load persisted values, falling back safely when they are malformed."""
        if isinstance(values, cls):
            return values
        defaults = cls()
        if not isinstance(values, Mapping):
            return defaults
        try:
            return cls(
                capital_min_percent=float(
                    values.get("capital_min_percent", defaults.capital_min_percent)
                ),
                capital_ideal_percent=float(
                    values.get("capital_ideal_percent", defaults.capital_ideal_percent)
                ),
                capital_max_percent=float(
                    values.get("capital_max_percent", defaults.capital_max_percent)
                ),
                stop_adr_min_percent=float(
                    values.get("stop_adr_min_percent", defaults.stop_adr_min_percent)
                ),
                stop_adr_ideal_percent=float(
                    values.get("stop_adr_ideal_percent", defaults.stop_adr_ideal_percent)
                ),
                stop_adr_max_percent=float(
                    values.get("stop_adr_max_percent", defaults.stop_adr_max_percent)
                ),
            )
        except (TypeError, ValueError, OverflowError):
            return defaults

    def to_dict(self) -> Dict[str, float]:
        return asdict(self)


DEFAULT_ORB_SETTINGS = OrbSettings()
_current_orb_settings = DEFAULT_ORB_SETTINGS

# Compatibility exports for callers that used the old fixed defaults. Runtime
# validation and scoring use ``get_orb_settings()`` instead of these aliases.
MIN_CAPITAL_PERCENT = DEFAULT_ORB_SETTINGS.capital_min_percent
MAX_CAPITAL_PERCENT = DEFAULT_ORB_SETTINGS.capital_max_percent
MIN_STOP_ADR = DEFAULT_ORB_SETTINGS.stop_adr_min_percent
MAX_STOP_ADR = DEFAULT_ORB_SETTINGS.stop_adr_max_percent


def configure_orb_settings(
    values: Mapping[str, Any] | OrbSettings | None,
) -> OrbSettings:
    """Set the process-wide ORB settings used by every execution path."""
    global _current_orb_settings
    _current_orb_settings = OrbSettings.from_mapping(values)
    return _current_orb_settings


def get_orb_settings() -> OrbSettings:
    """Return the immutable settings snapshot currently in force."""
    return _current_orb_settings


def _resolve_orb_settings(
    settings: Mapping[str, Any] | OrbSettings | None,
) -> OrbSettings:
    return (
        get_orb_settings()
        if settings is None
        else OrbSettings.from_mapping(settings)
    )


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
    sizing: Dict[str, Any],
    adr_percent: Optional[float],
    settings: Mapping[str, Any] | OrbSettings | None = None,
) -> bool:
    """Apply the configured ORB validity bounds."""
    orb_settings = _resolve_orb_settings(settings)
    if sizing.get("shares", 0.0) < 1.0:
        return False
    capital_percent = sizing.get("capital_percent", 0.0)
    if (
        capital_percent < orb_settings.capital_min_percent
        or capital_percent >= orb_settings.capital_max_percent
    ):
        return False
    stop_loss_percent = sizing.get("stop_loss_percent", 0.0)
    if (
        adr_percent is not None
        and adr_percent > 0
        and stop_loss_percent >= adr_percent
    ):
        return False
    sl_adr = sizing.get("sl_adr")
    if sl_adr is not None and (
        sl_adr < orb_settings.stop_adr_min_percent
        or sl_adr > orb_settings.stop_adr_max_percent
    ):
        return False
    return True


def validate_orb_position_values(
    sizing: Dict[str, Any],
    adr_percent: Optional[float],
    settings: Mapping[str, Any] | OrbSettings | None = None,
) -> List[str]:
    """Return human-readable warnings for the configured ORB bounds."""
    orb_settings = _resolve_orb_settings(settings)
    warnings: List[str] = []
    shares = int(sizing.get("shares", 0) or 0)
    capital_percent = float(sizing.get("capital_percent", 0.0) or 0.0)
    stop_loss_percent = float(sizing.get("stop_loss_percent", 0.0) or 0.0)
    stop_adr = sizing.get("sl_adr")

    if shares < 1:
        warnings.append("Position size calculation resulted in 0 shares")
    if capital_percent < orb_settings.capital_min_percent:
        warnings.append(
            f"Capital allocation ({capital_percent:.2f}%) is below "
            f"{orb_settings.capital_min_percent:g}%"
        )
    if capital_percent >= orb_settings.capital_max_percent:
        warnings.append(
            f"Capital allocation ({capital_percent:.2f}%) exceeds "
            f"{orb_settings.capital_max_percent:g}%"
        )
    if adr_percent is not None and adr_percent > 0 and stop_loss_percent >= adr_percent:
        warnings.append(
            f"Stop loss % ({stop_loss_percent:.2f}%) is wider than ADR "
            f"({adr_percent:.2f}%)"
        )
    if stop_adr is not None:
        stop_adr_value = float(stop_adr)
        if stop_adr_value < orb_settings.stop_adr_min_percent:
            warnings.append(
                f"Stop/ADR ({stop_adr_value:.2f}%) is below "
                f"{orb_settings.stop_adr_min_percent:g}%"
            )
        elif stop_adr_value > orb_settings.stop_adr_max_percent:
            warnings.append(
                f"Stop/ADR ({stop_adr_value:.2f}%) exceeds "
                f"{orb_settings.stop_adr_max_percent:g}%"
            )
    return warnings


def score_orb_position_recommendation(
    sizing: Dict[str, Any],
    risk_percent: float,
    settings: Mapping[str, Any] | OrbSettings | None = None,
) -> float:
    """Score an ORB plan using the configured ideal values."""
    orb_settings = _resolve_orb_settings(settings)
    sl_adr = sizing.get("sl_adr")
    capital_percent = sizing.get("capital_percent", 0.0)
    if sl_adr is None:
        return 0.0
    sl_adr_score = max(
        0.0,
        100.0
        - abs(float(sl_adr) - orb_settings.stop_adr_ideal_percent) * 3.0,
    )
    capital_score = max(
        0.0,
        100.0
        - abs(float(capital_percent) - orb_settings.capital_ideal_percent) * 4.0,
    )
    risk_score = max(0.0, 100.0 - float(risk_percent) * 100.0 * 25.0)
    return round(
        (sl_adr_score * 0.45) + (capital_score * 0.40) + (risk_score * 0.15),
        1,
    )
