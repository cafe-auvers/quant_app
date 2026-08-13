"""Position sizing calculations.

Moved here from src/core/position_sizer.py as part of risk-check consolidation.
This is the same class, thresholds, and math unchanged, just relocated to the
module that owns pre-trade risk checks.
"""
from dataclasses import dataclass
from enum import Enum
import math
from typing import Optional


class SizingMethod(Enum):
    """Position sizing methods."""
    FIXED_PERCENT = "fixed_percent"      # Fixed % of account
    KELLY_CRITERION = "kelly"             # Kelly criterion
    VOLATILITY_BASED = "volatility"       # Based on ATR
    RISK_BASED = "risk_based"             # Based on stop loss


@dataclass
class PositionSize:
    """Result of position sizing calculation."""
    shares: int
    dollar_amount: float
    percent_of_account: float
    risk_amount: float


def _finite_float(value: object) -> Optional[float]:
    """Return a finite float, or ``None`` for invalid user/data input."""
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _zero_position() -> PositionSize:
    """Return a fail-closed sizing result that cannot produce an order."""
    return PositionSize(
        shares=0,
        dollar_amount=0.0,
        percent_of_account=0.0,
        risk_amount=0.0,
    )


class PositionSizer:
    """Position sizing calculator."""

    def __init__(self, account_size: float, max_risk_per_trade: float = 0.02):
        """
        Initialize position sizer.

        Args:
            account_size: Total account size in currency units
            max_risk_per_trade: Maximum risk per trade as % of account (e.g., 0.02 = 2%)
        """
        normalized_account_size = _finite_float(account_size)
        normalized_max_risk = _finite_float(max_risk_per_trade)
        self.account_size = (
            normalized_account_size
            if normalized_account_size is not None and normalized_account_size > 0
            else 0.0
        )
        # A risk fraction above 100% is never a valid position-sizing input.
        self.max_risk_per_trade = (
            normalized_max_risk
            if normalized_max_risk is not None and 0.0 <= normalized_max_risk <= 1.0
            else 0.0
        )

    def size_fixed_percent(self, entry_price: float,
                          percent: float = 0.01) -> PositionSize:
        """
        Calculate position size as fixed % of account.

        Args:
            entry_price: Entry price per share
            percent: Position size as % of account (e.g., 0.01 = 1%)

        Returns:
            PositionSize object
        """
        entry = _finite_float(entry_price)
        position_percent = _finite_float(percent)
        if (
            self.account_size <= 0
            or entry is None
            or entry <= 0
            or position_percent is None
            or position_percent <= 0
            or position_percent > 1.0
        ):
            return _zero_position()

        dollar_amount = self.account_size * position_percent
        if not math.isfinite(dollar_amount) or dollar_amount <= 0:
            return _zero_position()
        raw_shares = dollar_amount / entry
        if not math.isfinite(raw_shares) or raw_shares < 0:
            return _zero_position()
        shares = int(raw_shares)

        return PositionSize(
            shares=shares,
            dollar_amount=shares * entry,
            percent_of_account=position_percent,
            risk_amount=dollar_amount * self.max_risk_per_trade
        )

    def size_risk_based(self, entry_price: float, stop_loss_price: float,
                       risk_percent: Optional[float] = None) -> PositionSize:
        """
        Calculate position size based on risk.

        Args:
            entry_price: Entry price per share
            stop_loss_price: Stop loss price per share
            risk_percent: % of account to risk (defaults to max_risk_per_trade)

        Returns:
            PositionSize object
        """
        entry = _finite_float(entry_price)
        stop = _finite_float(stop_loss_price)
        risk_fraction = (
            self.max_risk_per_trade
            if risk_percent is None
            else _finite_float(risk_percent)
        )
        if (
            self.account_size <= 0
            or entry is None
            or entry <= 0
            or stop is None
            or stop <= 0
            or stop >= entry
            or risk_fraction is None
            or risk_fraction <= 0
            or risk_fraction > 1.0
        ):
            return _zero_position()

        # Amount willing to lose and points at risk per share.  Do not use an
        # absolute difference here: a long stop at or above entry is invalid
        # and must not silently turn into a tradable risk distance.
        risk_amount = self.account_size * risk_fraction
        risk_per_share = entry - stop
        if (
            not math.isfinite(risk_amount)
            or risk_amount <= 0
            or not math.isfinite(risk_per_share)
            or risk_per_share <= 0
        ):
            return _zero_position()

        # Shares = Risk / Risk per share.  Retain upward rounding so a valid
        # fractional-risk plan remains actionable, while invalid inputs above
        # fail closed to zero shares.
        raw_shares = risk_amount / risk_per_share
        if not math.isfinite(raw_shares) or raw_shares <= 0:
            return _zero_position()
        shares = int(math.ceil(raw_shares))
        dollar_amount = shares * entry
        position_percent = dollar_amount / self.account_size
        if (
            shares <= 0
            or not math.isfinite(dollar_amount)
            or not math.isfinite(position_percent)
        ):
            return _zero_position()

        return PositionSize(
            shares=shares,
            dollar_amount=dollar_amount,
            percent_of_account=position_percent,
            risk_amount=risk_amount
        )

    def size_volatility_based(self, entry_price: float, atr: float,
                             atr_multiplier: float = 2.0) -> PositionSize:
        """
        Calculate position size based on volatility (ATR).

        Args:
            entry_price: Entry price per share
            atr: Average True Range value
            atr_multiplier: Stop loss distance as multiple of ATR

        Returns:
            PositionSize object
        """
        entry = _finite_float(entry_price)
        atr_value = _finite_float(atr)
        multiplier = _finite_float(atr_multiplier)
        if (
            entry is None
            or entry <= 0
            or atr_value is None
            or atr_value <= 0
            or multiplier is None
            or multiplier <= 0
        ):
            return _zero_position()
        stop_loss_price = entry - (atr_value * multiplier)
        return self.size_risk_based(entry, stop_loss_price)

    def size_kelly(self, win_rate: float, avg_win: float,
                  avg_loss: float) -> PositionSize:
        """
        Calculate position size using Kelly criterion.

        Args:
            win_rate: Win rate as decimal (e.g., 0.55 = 55%)
            avg_win: Average win amount per trade
            avg_loss: Average loss amount per trade

        Returns:
            PositionSize object
        """
        win_rate_value = _finite_float(win_rate)
        avg_win_value = _finite_float(avg_win)
        avg_loss_value = _finite_float(avg_loss)
        if (
            self.account_size <= 0
            or win_rate_value is None
            or not 0.0 <= win_rate_value <= 1.0
            or avg_win_value is None
            or avg_win_value <= 0
            or avg_loss_value is None
            or avg_loss_value < 0
        ):
            return _zero_position()
        if avg_loss_value == 0:
            # Preserve the documented neutral 1:1 fallback for missing loss
            # history while malformed/negative/non-finite values fail closed.
            avg_loss_value = avg_win_value

        # Kelly % = (win_rate * avg_win - (1 - win_rate) * avg_loss) / avg_win
        kelly_percent = (
            win_rate_value * avg_win_value
            - (1 - win_rate_value) * avg_loss_value
        ) / avg_win_value
        if not math.isfinite(kelly_percent):
            return _zero_position()

        # Apply fractional Kelly for safety (e.g., 25% of Kelly)
        kelly_percent = kelly_percent * 0.25

        # Cap at max risk per trade
        kelly_percent = min(kelly_percent, self.max_risk_per_trade)
        kelly_percent = max(kelly_percent, 0)

        return self.size_fixed_percent(entry_price=1.0, percent=kelly_percent)
