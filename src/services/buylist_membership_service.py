"""Keep legacy Buylist membership aligned with canonical TradeCards.

The legacy Buylist remains a compatibility view while ``trade_cards`` owns
the Kanban lifecycle.  Synchronization is intentionally asymmetric: a
passive Buylist item may create a missing card or promote WATCHLIST to
BUYLIST, but it may never pull a card back from a later lifecycle state.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Optional

from sqlalchemy.engine import Engine

from src.core.trade_card_state import BoardStatus, TradeCardState
from src.services import trade_card_repository


@dataclass(frozen=True)
class BuylistMembershipSyncResult:
    """Outcome of reconciling one legacy Buylist item."""

    action: str = "skipped"
    card: Optional[TradeCardState] = None

    @property
    def changed(self) -> bool:
        return self.action in {"created", "promoted"}

    @property
    def card_key(self) -> str:
        return self.card.card_key if self.card is not None else ""


def _normalized_account(value: object) -> str:
    return str(value or "").strip()


def _normalized_symbol(value: object) -> str:
    return str(value or "").strip().upper()


def reconcile_buylist_item(
    engine: Optional[Engine],
    item,
    *,
    default_account_no: str = "",
    watchlist=None,
    existing_card: Optional[TradeCardState] = None,
    existing_card_loaded: bool = False,
) -> BuylistMembershipSyncResult:
    """Ensure one Buylist item has a safe canonical representation.

    Missing rows retain the established legacy migration mapping.  For an
    existing row, only ``WATCHLIST -> BUYLIST`` is accepted, and only when
    the legacy row maps to passive ``BUYLIST`` state.  Every other existing
    lifecycle is canonical and remains untouched.

    ``existing_card_loaded`` lets a bootstrap pass reuse its bulk read.  CAS
    conflicts are re-read before one retry so a concurrent promotion is
    accepted while a concurrent move to a later lifecycle always wins.
    """

    if engine is None:
        return BuylistMembershipSyncResult()
    environment = str(getattr(item, "environment", "PROD") or "PROD").upper()
    symbol = _normalized_symbol(getattr(item, "symbol", ""))
    account_no = _normalized_account(getattr(item, "kis_account_no", ""))
    if not account_no:
        account_no = _normalized_account(default_account_no)
    if environment != "PROD" or not symbol or not account_no:
        return BuylistMembershipSyncResult()

    normalized_item = copy.copy(item)
    normalized_item.environment = "PROD"
    normalized_item.symbol = symbol
    normalized_item.kis_account_no = account_no

    current = (
        existing_card
        if existing_card_loaded
        else trade_card_repository.get_trade_card(
            engine, "PROD", account_no, symbol
        )
    )

    for _attempt in range(2):
        report = trade_card_repository.build_trade_card_migration(
            buylist_manager=SimpleNamespace(items=[normalized_item]),
            watchlist=watchlist,
            existing_cards=[current] if current is not None else [],
        )
        row = report.rows[0]

        if current is None:
            try:
                stored = trade_card_repository.create_trade_card(engine, row.card)
                return BuylistMembershipSyncResult(action="created", card=stored)
            except trade_card_repository.TradeCardVersionConflictError:
                current = trade_card_repository.get_trade_card(
                    engine, "PROD", account_no, symbol
                )
                continue

        if not (
            current.board_status == BoardStatus.WATCHLIST
            and row.card.board_status == BoardStatus.BUYLIST
        ):
            return BuylistMembershipSyncResult(action="unchanged", card=current)

        promoted = copy.deepcopy(current)
        promoted.previous_board_status = current.board_status
        promoted.board_status = BoardStatus.BUYLIST
        promoted.board_status_updated_at = datetime.now(timezone.utc)
        promoted.buylist_member = True
        promoted.watchlist_member = bool(
            current.watchlist_member or row.card.watchlist_member
        )
        try:
            stored = trade_card_repository.update_trade_card(
                engine,
                promoted,
                expected_version=current.version,
            )
            return BuylistMembershipSyncResult(action="promoted", card=stored)
        except (
            trade_card_repository.TradeCardVersionConflictError,
            trade_card_repository.TradeCardNotFoundError,
        ):
            current = trade_card_repository.get_trade_card(
                engine, "PROD", account_no, symbol
            )

    return BuylistMembershipSyncResult(action="conflicted", card=current)


def add_to_buylist(
    buylist_manager,
    item,
    *,
    engine: Optional[Engine] = None,
    default_account_no: str = "",
    watchlist=None,
) -> BuylistMembershipSyncResult:
    """Write a Buylist addition through to canonical Kanban state."""

    buylist_manager.add(item)
    return reconcile_buylist_item(
        engine,
        item,
        default_account_no=default_account_no,
        watchlist=watchlist,
    )
