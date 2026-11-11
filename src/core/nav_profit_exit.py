"""Durable, once-per-position NAV profit-taking policy; no broker I/O."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Mapping
from uuid import uuid4


@dataclass
class NavProfitExitState:
    cycle_id: str = ""
    nav_usd: float = 0.0
    target_profit_usd: float = 0.0
    status: str = "WAITING"
    target_quantity: int = 0
    fills: dict[str, int] = field(default_factory=dict)
    retired_orders: list[str] = field(default_factory=list)

    @property
    def filled_quantity(self) -> int:
        return sum(self.fills.values())

    @property
    def remaining_quantity(self) -> int:
        return max(0, self.target_quantity - self.filled_quantity)

    def to_dict(self) -> dict[str, Any]:
        return dict(cycle_id=self.cycle_id, nav_usd=self.nav_usd,
                    target_profit_usd=self.target_profit_usd, status=self.status,
                    target_quantity=self.target_quantity, fills=dict(self.fills),
                    retired_orders=list(self.retired_orders))

    @classmethod
    def from_dict(cls, value: Any) -> "NavProfitExitState":
        if isinstance(value, cls):
            return value
        if value is None:
            return cls()
        try:
            if not isinstance(value, Mapping):
                raise ValueError("Invalid NAV exit state")
            state = cls(
                cycle_id=str(value.get("cycle_id") or ""),
                nav_usd=float(value.get("nav_usd") or 0),
                target_profit_usd=float(value.get("target_profit_usd") or 0),
                status=str(value.get("status") or "WAITING"),
                target_quantity=int(value.get("target_quantity") or 0),
                fills={str(key): int(qty) for key, qty in (value.get("fills") or {}).items()},
                retired_orders=[str(key) for key in value.get("retired_orders", ())],
            )
            if (
                state.status not in {"WAITING", "ARMED", "SELLING", "DONE", "OVERRIDDEN", "BLOCKED"}
                or not all(math.isfinite(v) and v >= 0 for v in (state.nav_usd, state.target_profit_usd))
                or state.target_quantity < 0 or any(qty < 0 for qty in state.fills.values())
                or (state.status in {"ARMED", "SELLING", "DONE"} and (
                    not state.cycle_id or state.nav_usd <= 0 or state.target_profit_usd <= 0
                ))
                or (state.status in {"SELLING", "DONE"} and state.target_quantity <= 0)
                or (state.status in {"WAITING", "ARMED"} and (
                    state.target_quantity or state.fills or state.retired_orders
                ))
                or (state.status == "DONE" and state.filled_quantity < state.target_quantity)
            ):
                raise ValueError("Inconsistent NAV exit state")
            return state
        except (ValueError, TypeError, OverflowError, AttributeError):
            # Corrupt/unknown saved state must never silently re-arm a sale.
            return cls(status="BLOCKED")


NAV_PROFIT_NAV_REQUIRED = "AUTO_NAV_PROFIT_NAV_REQUIRED"


def initialize_nav_profit_exit(card, account_nav: float | None) -> bool:
    from src.core.trade_card_state import BoardStatus

    state = card.nav_profit_exit
    if state.status != "WAITING" or card.broker_quantity <= 0 or card.board_status not in {
        BoardStatus.OPEN_POSITION, BoardStatus.PARTIAL_SELL, BoardStatus.ENTRY_PENDING,
    }:
        return False
    if account_nav is None or not math.isfinite(account_nav) or account_nav <= 0:
        return False
    card.nav_profit_exit = NavProfitExitState(
        cycle_id=uuid4().hex, nav_usd=account_nav, target_profit_usd=account_nav * 0.02,
        status="OVERRIDDEN" if card.board_status == BoardStatus.PARTIAL_SELL else "ARMED",
    )
    card.warnings = [w for w in card.warnings if w != NAV_PROFIT_NAV_REQUIRED]
    return True


def nav_profit_sell_quantity(card, price: float) -> int:
    from src.core.trade_card_state import BoardStatus

    state = card.nav_profit_exit
    if (
        state.status != "ARMED" or not state.cycle_id or state.target_profit_usd <= 0
        or card.board_status != BoardStatus.OPEN_POSITION or card.broker_quantity < 2
        or card.exit_all_required or card.stop_loss_triggered
        or card.entry_submission_unresolved or card.entry_cancel_in_flight
        or card.entry_remaining_target_quantity > 0
        or card.exit_client_order_id or card.exit_submission_unresolved
        or card.exit_cancel_in_flight or card.reserved_sell_quantity
        or card.pending_partial_sell_quantity or card.pending_stop_command_id
        or not math.isfinite(price) or price <= 0
        or not math.isfinite(card.average_entry_price) or card.average_entry_price <= 0
        or (card.active_stop_price is not None and price <= card.active_stop_price)
    ):
        return 0
    profit = (price - card.average_entry_price) * card.broker_quantity
    quantity = card.broker_quantity // 2
    if not math.isfinite(profit) or profit + 1e-8 < state.target_profit_usd:
        return 0
    return quantity if card.orderable_quantity >= quantity else 0


def record_nav_profit_fill(card, client_order_id: str, filled_quantity: int) -> None:
    """Cumulative broker fills are idempotent across polling and restarts."""
    state = card.nav_profit_exit
    if state.status == "SELLING" and client_order_id:
        state.fills[client_order_id] = max(state.fills.get(client_order_id, 0), int(filled_quantity))


def retire_nav_profit_order(card, client_order_id: str) -> None:
    if client_order_id and client_order_id not in card.nav_profit_exit.retired_orders:
        card.nav_profit_exit.retired_orders.append(client_order_id)


def continue_nav_profit_exit(card, remaining: int) -> bool:
    """Keep an incompletely filled automatic objective through terminal retries."""
    from src.core.trade_card_state import BoardStatus, PositionRuntimeStatus

    state = card.nav_profit_exit
    if state.status != "SELLING":
        return False
    if state.remaining_quantity <= 0:
        state.status = "DONE"
        return False
    card.broker_quantity = remaining
    card.orderable_quantity = remaining
    card.stop_quantity = remaining
    card.pending_partial_sell_quantity = state.remaining_quantity
    card.pending_partial_sell_price = None
    card.reserved_sell_quantity = 0
    card.exit_client_order_id = ""
    card.exit_pending_attempt_number = 0
    card.exit_submission_unresolved = False
    card.exit_cancel_in_flight = False
    card.exit_cancel_requested_at = None
    card.exit_cancel_command_id = ""
    card.board_status = BoardStatus.PARTIAL_SELL
    card.position_runtime_status = PositionRuntimeStatus.PARTIAL_EXIT_PENDING
    return True
