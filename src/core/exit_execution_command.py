"""Frontend-neutral durable intent for one SELL submission.

The legacy Buy Dashboard and Kanban runtime intentionally have different UI
lifecycles, but INV-21/L3 requires them to cross the execution boundary with
the same domain command for the same account state and user intent.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from src.core.order_state import (
    REGULAR_LIMIT_EXECUTION,
    RESERVED_MOO_EXECUTION,
    OrderIntent,
    OrderSide,
)


_MANUAL_SESSION_AWARE_INTENTS = frozenset(
    {
        OrderIntent.PARTIAL_EXIT,
        OrderIntent.PARTIAL_TAKE_PROFIT,
        OrderIntent.MANUAL_EXIT,
    }
)


def exit_execution_policy(
    *, environment: str, intent: OrderIntent, regular_session_open: bool
) -> str:
    """Select the shared legacy/Kanban session policy."""

    if (
        str(environment or "").strip().upper() == "PROD"
        and intent in _MANUAL_SESSION_AWARE_INTENTS
        and not regular_session_open
    ):
        return RESERVED_MOO_EXECUTION
    return REGULAR_LIMIT_EXECUTION


@dataclass(frozen=True)
class ExitExecutionCommand:
    environment: str
    account_no: str
    symbol: str
    intent: OrderIntent
    quantity: int
    limit_price: float
    exchange: str = "NASD"
    execution_policy: str = REGULAR_LIMIT_EXECUTION
    side: OrderSide = OrderSide.SELL
    emergency: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "environment", str(self.environment or "").upper())
        object.__setattr__(self, "account_no", str(self.account_no or ""))
        object.__setattr__(self, "symbol", str(self.symbol or "").upper())
        object.__setattr__(self, "quantity", int(self.quantity or 0))
        object.__setattr__(self, "limit_price", float(self.limit_price or 0.0))
        object.__setattr__(self, "exchange", str(self.exchange or "NASD").upper())
        object.__setattr__(
            self, "execution_policy", str(self.execution_policy or "").upper()
        )
        if self.side != OrderSide.SELL:
            raise ValueError("ExitExecutionCommand side must be SELL")
        if self.quantity <= 0:
            raise ValueError("ExitExecutionCommand quantity must be positive")
        if self.execution_policy == RESERVED_MOO_EXECUTION:
            if self.limit_price != 0.0:
                raise ValueError("RESERVED_MOO exit command requires limit_price=0")
        elif (
            self.execution_policy != REGULAR_LIMIT_EXECUTION
            or not math.isfinite(self.limit_price)
            or self.limit_price <= 0
        ):
            raise ValueError("Regular exit command requires a positive finite limit price")


def build_exit_execution_command(
    *,
    environment: str,
    account_no: str,
    symbol: str,
    intent: OrderIntent,
    quantity: int,
    regular_session_open: bool,
    limit_price: float | None = None,
    exchange: str = "NASD",
) -> ExitExecutionCommand:
    policy = exit_execution_policy(
        environment=environment,
        intent=intent,
        regular_session_open=regular_session_open,
    )
    resolved_price = 0.0 if policy == RESERVED_MOO_EXECUTION else float(limit_price or 0.0)
    return ExitExecutionCommand(
        environment=environment,
        account_no=account_no,
        symbol=symbol,
        intent=intent,
        quantity=quantity,
        limit_price=resolved_price,
        exchange=exchange,
        execution_policy=policy,
        emergency=intent in {OrderIntent.MANUAL_EXIT, OrderIntent.STOP_LOSS},
    )
