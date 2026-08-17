"""Frontend-neutral request to change protective stop configuration."""
from __future__ import annotations

import math
from dataclasses import dataclass

from src.core.trade_card_state import StopType


@dataclass(frozen=True)
class StopChangeCommand:
    environment: str
    account_no: str
    symbol: str
    stop_type: StopType
    price: float
    quantity: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "environment", str(self.environment or "").upper())
        object.__setattr__(self, "account_no", str(self.account_no or ""))
        object.__setattr__(self, "symbol", str(self.symbol or "").upper())
        object.__setattr__(self, "price", float(self.price or 0.0))
        object.__setattr__(self, "quantity", int(self.quantity or 0))
        if not math.isfinite(self.price) or self.price <= 0:
            raise ValueError("StopChangeCommand price must be positive and finite")
        if self.quantity <= 0:
            raise ValueError("StopChangeCommand quantity must be positive")


def build_stop_change_command(
    *,
    environment: str,
    account_no: str,
    symbol: str,
    stop_type: StopType,
    price: float,
    quantity: int,
) -> StopChangeCommand:
    return StopChangeCommand(
        environment=environment,
        account_no=account_no,
        symbol=symbol,
        stop_type=stop_type,
        price=price,
        quantity=quantity,
    )
