"""Position sizing calculations."""
from dataclasses import dataclass
from enum import Enum
from typing import Dict, Optional


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


class PositionSizer:
    """Position sizing calculator."""
    
    def __init__(self, account_size: float, max_risk_per_trade: float = 0.02):
        """
        Initialize position sizer.
        
        Args:
            account_size: Total account size in currency units
            max_risk_per_trade: Maximum risk per trade as % of account (e.g., 0.02 = 2%)
        """
        self.account_size = account_size
        self.max_risk_per_trade = max_risk_per_trade
    
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
        dollar_amount = self.account_size * percent
        shares = int(dollar_amount / entry_price)
        
        return PositionSize(
            shares=shares,
            dollar_amount=shares * entry_price,
            percent_of_account=percent,
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
        if risk_percent is None:
            risk_percent = self.max_risk_per_trade
        
        # Amount willing to lose
        risk_amount = self.account_size * risk_percent
        
        # Points at risk per share
        risk_per_share = abs(entry_price - stop_loss_price)
        
        if risk_per_share == 0:
            risk_per_share = entry_price * 0.02  # Default 2% if equal
        
        # Shares = Risk / Risk per share
        import math
        shares = int(math.ceil(risk_amount / risk_per_share))
        dollar_amount = shares * entry_price
        position_percent = dollar_amount / self.account_size
        
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
        stop_loss_price = entry_price - (atr * atr_multiplier)
        return self.size_risk_based(entry_price, stop_loss_price)
    
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
        if avg_loss == 0:
            avg_loss = avg_win  # Assume 1:1 risk/reward if no loss data
        
        # Kelly % = (win_rate * avg_win - (1 - win_rate) * avg_loss) / avg_win
        kelly_percent = (win_rate * avg_win - (1 - win_rate) * avg_loss) / avg_win
        
        # Apply fractional Kelly for safety (e.g., 25% of Kelly)
        kelly_percent = kelly_percent * 0.25
        
        # Cap at max risk per trade
        kelly_percent = min(kelly_percent, self.max_risk_per_trade)
        kelly_percent = max(kelly_percent, 0)
        
        return self.size_fixed_percent(entry_price=1.0, percent=kelly_percent)
