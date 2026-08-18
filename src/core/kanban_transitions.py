"""Pure Kanban policy logic: legal board-status transitions, the AS-IS to
TO-BE migration mapping (``buydashboard_to_kanban.md`` section 25), and the
one-card-per-symbol invariant (section 43-47).

Deliberately free of I/O and Qt, mirroring how :mod:`src.core.exit_policy`
keeps policy decisions out of the UI layer -- everything here is a pure
function over :class:`~src.core.trade_card_state.TradeCardState` /
``BoardStatus`` values so it can be unit tested without a database, a broker,
or a running app.
"""
from __future__ import annotations

from typing import Dict, Iterable, List, Set

from src.core.trade_card_state import BoardStatus, TradeCardState


class InvalidBoardTransitionError(ValueError):
    """Raised when a requested board-status move is not on the allowed graph."""


class DuplicateCardError(ValueError):
    """Raised when more than one card exists for the same (env, account, symbol)."""


# The visible Kanban graph from section 17-31 of the spec. Keys are the
# *current* board_status; values are every board_status a card may move to
# directly. Backward moves that the spec explicitly describes (Buy Today ->
# Buylist with no order, Partial Sell -> Open Positions after reconciling
# fills, Sell All -> Closed only once broker-confirmed flat) are included;
# nothing else is legal.
#
# NOTE: this graph governs only user-facing drag *commands*
# (src.ui.buyboard.controller.apply_board_command). The EOD service
# (src.services.eod_trading_service) and the heartbeat engine
# (src.services.trading_engine) mutate board_status directly, bypassing
# this graph entirely, because they are the verified "system" actors the
# graph exists to protect against -- e.g. ENTRY_PENDING -> BUYLIST is a
# legitimate system transition once an order has been confirmed
# cancelled/zero-filled (section 517-521), but it is deliberately *not* in
# this graph so a user cannot drag a card with a still-unresolved order
# straight back to Buylist and orphan it (section 989-990's "Block Buy
# Today -> Buylist when an unresolved order exists" test). A user-initiated
# cancel of a pending entry goes through CancelEntry instead, which -- for
# an ENTRY_PENDING card -- only flags the request; the engine performs the
# actual broker cancel and moves the card once confirmed.
ALLOWED_BOARD_TRANSITIONS: Dict[BoardStatus, Set[BoardStatus]] = {
    BoardStatus.WATCHLIST: {BoardStatus.BUYLIST},
    BoardStatus.BUYLIST: {BoardStatus.WATCHLIST, BoardStatus.BUY_TODAY},
    BoardStatus.BUY_TODAY: {BoardStatus.BUYLIST, BoardStatus.ENTRY_PENDING},
    BoardStatus.ENTRY_PENDING: {
        BoardStatus.OPEN_POSITION,  # any confirmed fill
    },
    BoardStatus.OPEN_POSITION: {
        BoardStatus.PARTIAL_SELL,
        BoardStatus.SELL_ALL,
    },
    BoardStatus.PARTIAL_SELL: {
        # CancelPartialSell may take this edge immediately only before a
        # durable SELL lifecycle exists. Once submitted, broker-terminal
        # reconciliation owns the same edge (including raced partial fills).
        BoardStatus.OPEN_POSITION,
        BoardStatus.SELL_ALL,  # stop/user escalation while partial-selling
    },
    BoardStatus.SELL_ALL: {
        # A user may reduce an unsubmitted/fully reconciled liquidation
        # objective to a partial exit.  The workflow layer additionally
        # rejects this edge while any SELL identity, reservation, working
        # order, ambiguous submission, or cancellation is still live.
        BoardStatus.PARTIAL_SELL,
        BoardStatus.CLOSED,  # only once broker confirms zero
        # CancelQueuedSellAll (section 302-304) cancels a premarket queued
        # sell-at-open instruction before it fires -- the card returns to
        # Open Positions. This edge must never be taken once the liquidation
        # is actually working during market hours (the command handler
        # additionally requires sell_all_at_market_open=True).
        BoardStatus.OPEN_POSITION,
    },
    # Broker-confirmed flat. A card may only leave CLOSED through the
    # return-to-buylist-after-close membership restore (see
    # ``restore_closed_card_membership`` below), never a direct drag command.
    BoardStatus.CLOSED: set(),
}


def validate_board_transition(current: BoardStatus, target: BoardStatus) -> None:
    """Raise ``InvalidBoardTransitionError`` unless ``target`` is directly
    reachable from ``current`` on the Kanban graph. No-op moves (current ==
    target) are always allowed -- a command that re-confirms the current
    column is not a transition.
    """
    if current == target:
        return
    allowed = ALLOWED_BOARD_TRANSITIONS.get(current, set())
    if target not in allowed:
        raise InvalidBoardTransitionError(
            f"Cannot move a card from {current.value} to {target.value}"
        )


def restore_closed_card_membership(card: TradeCardState) -> BoardStatus:
    """Section 552-554: a CLOSED card whose ``return_to_buylist_after_close``
    flag is set moves back to BUYLIST on the next daily reset, independent of
    any drag command. Anything else stays CLOSED.
    """
    if card.board_status == BoardStatus.CLOSED and card.return_to_buylist_after_close:
        return BoardStatus.BUYLIST
    return card.board_status


def validate_single_card_per_symbol(
    cards: Iterable[TradeCardState],
) -> List[str]:
    """Section 43: "One account + one symbol = one Kanban card." Returns a
    list of human-readable violation messages (empty if the invariant holds).
    Pure/read-only -- callers decide how to resolve a violation.
    """
    seen: Dict[str, TradeCardState] = {}
    violations: List[str] = []
    for card in cards:
        key = card.card_key
        if key in seen:
            violations.append(
                f"Duplicate card for {key}: statuses "
                f"{seen[key].board_status.value!r} and {card.board_status.value!r}"
            )
            continue
        seen[key] = card
    return violations


def require_single_card_per_symbol(cards: Iterable[TradeCardState]) -> None:
    """Raise ``DuplicateCardError`` if the one-card invariant is violated."""
    violations = validate_single_card_per_symbol(cards)
    if violations:
        raise DuplicateCardError("; ".join(violations))


# --- Section 25: AS-IS -> TO-BE migration mapping ---------------------------
#
# monitoring_status values that map directly to a BoardStatus, independent of
# any other field on the legacy BuylistItem/ExecutionQueueItem.
_DIRECT_STATUS_MAP: Dict[str, BoardStatus] = {
    "WATCHING": BoardStatus.BUYLIST,
    "ORDER_PENDING": BoardStatus.ENTRY_PENDING,
    "ORDER_SUBMITTED": BoardStatus.ENTRY_PENDING,
    "BUY_SUBMITTED": BoardStatus.ENTRY_PENDING,
    "UNKNOWN_SUBMISSION_STATE": BoardStatus.ENTRY_PENDING,
    "BOUGHT": BoardStatus.OPEN_POSITION,
    "PARTIAL_EXIT_SUBMITTED": BoardStatus.PARTIAL_SELL,
    "PARTIAL_EXIT_RESERVED": BoardStatus.PARTIAL_SELL,
    "SELL_SUBMITTED": BoardStatus.SELL_ALL,
    "SELL_RESERVED": BoardStatus.SELL_ALL,
    "SOLD": BoardStatus.CLOSED,
}


def migrate_legacy_status_to_board_status(
    monitoring_status: str,
    *,
    orb_monitor_enabled: bool = False,
    shares_held: int = 0,
) -> BoardStatus:
    """Map one legacy ``BuylistItem.monitoring_status`` value to the new
    ``BoardStatus`` per the spec section 25 table.

    Two legacy statuses need extra fields to disambiguate, exactly as the
    spec's table footnotes describe:
      - ``ACTIVE`` (or any status with ``orb_monitor_enabled`` set) means the
        user authorized monitoring today -> BUY_TODAY, *unless* a position is
        already held, in which case broker-confirmed OPEN_POSITION wins.
      - ``BUY_PARTIAL`` only means OPEN_POSITION when ``shares_held > 0``; a
        partial-fill row with zero shares (fully cancelled/reconciled away)
        has nothing to show and falls back to BUYLIST so it isn't stranded on
        the board.

    Anything unrecognized defaults to BUYLIST -- the safe, non-armed column --
    rather than guessing into BUY_TODAY or a broker-order-owning column.
    Broker reconciliation (spec section 981-983: "Migration must then query
    KIS before enabling execution") overrides every migrated assumption
    afterwards regardless of what this function returns.
    """
    status = str(monitoring_status or "").strip().upper()

    if status == "BUY_PARTIAL":
        return BoardStatus.OPEN_POSITION if shares_held > 0 else BoardStatus.BUYLIST

    if shares_held > 0 and status not in _DIRECT_STATUS_MAP:
        # A row with a confirmed broker position but an unrecognized/legacy
        # status string must not be dropped back to Buylist -- broker truth
        # (shares_held > 0) outranks an unmapped status string.
        return BoardStatus.OPEN_POSITION

    # orb_monitor_enabled is the real "user authorized monitoring today"
    # signal (section 80-86) and must win over the neutral WATCHING status,
    # which only means "no order/position lifecycle event has happened yet"
    # -- it says nothing about whether the user activated the card. It must
    # never override a more specific order/position status like BOUGHT or
    # ORDER_PENDING, so this check is scoped to WATCHING/ACTIVE only.
    if status in ("WATCHING", "ACTIVE") and (orb_monitor_enabled or status == "ACTIVE"):
        return BoardStatus.BUY_TODAY

    if status in _DIRECT_STATUS_MAP:
        return _DIRECT_STATUS_MAP[status]

    return BoardStatus.BUYLIST
