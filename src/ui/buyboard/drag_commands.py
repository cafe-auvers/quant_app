"""Kanban drag/action command dataclasses. ``buydashboard_to_kanban.md``
section 8.

The Buy Board UI never mutates :class:`~src.core.trade_card_state.TradeCardState`
or submits a broker order directly -- every drag, button click, or dialog
confirmation produces one of these commands and hands it to
:mod:`src.ui.buyboard.controller` for validation and application. Every
command carries ``expected_card_version`` so the backend can reject a stale
command (spec section 317) instead of silently applying a drag made against
out-of-date state -- the concrete guard for this is
:func:`src.services.trade_card_repository.update_trade_card`'s conditional
``WHERE version == expected_version``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Union
from uuid import uuid4


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def new_command_id() -> str:
    return uuid4().hex


@dataclass(frozen=True)
class BoardCommand:
    """Fields common to every Kanban command (spec section 308-316)."""

    environment: str
    account_no: str
    symbol: str
    expected_card_version: int
    command_id: str = field(default_factory=new_command_id)
    requested_at: datetime = field(default_factory=_utc_now)

    def __post_init__(self) -> None:
        object.__setattr__(self, "environment", str(self.environment or "").upper())
        object.__setattr__(self, "account_no", str(self.account_no or ""))
        object.__setattr__(self, "symbol", str(self.symbol or "").upper())
        object.__setattr__(
            self, "expected_card_version", int(self.expected_card_version or 0)
        )


@dataclass(frozen=True)
class MoveToWatchlist(BoardCommand):
    """Watchlist <-> Buylist membership move; never touches a broker."""


@dataclass(frozen=True)
class MoveToBuylist(BoardCommand):
    """Also used for Buy Today -> Buylist ("deactivate, no order exists")."""


@dataclass(frozen=True)
class ActivateForToday(BoardCommand):
    """Buylist -> Buy Today. Authorizes monitoring only (spec section 78-86);
    does not itself arm or submit anything."""


@dataclass(frozen=True)
class CancelEntry(BoardCommand):
    """Cancel a working entry order / abandon Buy Today monitoring."""


@dataclass(frozen=True)
class RequestPartialSell(BoardCommand):
    """Open Positions -> Partial Sell, with a user-entered quantity (spec
    section 563-579). The controller converts this to ``RequestSellAll``
    when ``quantity >= broker_orderable_quantity``.
    """

    quantity: int = 0

    def __post_init__(self) -> None:
        super().__post_init__()
        object.__setattr__(self, "quantity", int(self.quantity or 0))


@dataclass(frozen=True)
class RequestSellAll(BoardCommand):
    """Open Positions (or Partial Sell, on stop trigger) -> Sell All."""


@dataclass(frozen=True)
class CancelQueuedSellAll(BoardCommand):
    """Cancel a premarket queued Sell-All-at-open instruction (spec section
    720-724) before it fires."""


@dataclass(frozen=True)
class SetBreakevenStop(BoardCommand):
    """Manually pull the stop to the cost-adjusted breakeven price now."""


@dataclass(frozen=True)
class SetManualStop(BoardCommand):
    """Manual stop price; must satisfy ``price >= max(breakeven, active_stop)``
    (spec section 646-659) -- can only tighten risk, never widen it."""

    price: float = 0.0

    def __post_init__(self) -> None:
        super().__post_init__()
        object.__setattr__(self, "price", float(self.price or 0.0))


@dataclass(frozen=True)
class ReorderCard(BoardCommand):
    """Sets this card's ``kanban_priority`` within its column (spec section
    379: "higher Kanban priority wins" on a simultaneous-trigger tie).
    Review finding P1-8: the model carried ``kanban_priority`` from Phase 1
    on, but nothing let the user actually change it -- this is that
    command. ``target_priority`` is the new absolute value, not a delta;
    the board layer computes it from the card's current neighbors (see
    ``board.py``'s "Move Up"/"Move Down" actions) so this command itself
    stays a simple, order-independent set.
    """

    target_priority: int = 0

    def __post_init__(self) -> None:
        super().__post_init__()
        object.__setattr__(self, "target_priority", int(self.target_priority or 0))


AnyBoardCommand = Union[
    MoveToWatchlist,
    MoveToBuylist,
    ActivateForToday,
    CancelEntry,
    RequestPartialSell,
    RequestSellAll,
    CancelQueuedSellAll,
    SetBreakevenStop,
    SetManualStop,
    ReorderCard,
]
