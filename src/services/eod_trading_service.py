"""End-of-day cleanup and startup reconciliation.
``buydashboard_to_kanban.md`` section 13 and Phase 6 (section 1070-1075).

Like :mod:`src.services.entry_attempt_manager`/:mod:`src.services.position_manager`,
this is synchronous and takes explicit input/output: the caller (in
production, :mod:`src.services.trading_engine`'s heartbeat, gated on
``EOD_ENTRY_CLEANUP_SECONDS_BEFORE_CLOSE``) supplies the current cards and
gets back the ones that changed, to persist via
:mod:`src.services.trade_card_repository`.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional

from src.core.order_state import BrokerOrder, BrokerOrderDiscoveryResult, OrderStatus
from src.core.trade_card_state import (
    BoardStatus,
    EntryRuntimeStatus,
    PositionRuntimeStatus,
    TradeCardState,
)
from src.services import capital_allocator
from src.services.entry_attempt_manager import EntryAttemptManager
from src.services.position_manager import (
    BrokerHolding,
    PositionManager,
    extract_overseas_holdings,
)

logger = logging.getLogger(__name__)

# Statuses that mean "the broker has already finished with this order" --
# no cancel call is needed or safe to attempt.
_ALREADY_TERMINAL_STATUSES = {
    OrderStatus.FILLED,
    OrderStatus.CANCELLED,
    OrderStatus.REJECTED,
    OrderStatus.EXPIRED,
}
_UNRESOLVED_STATUSES = {OrderStatus.UNKNOWN_SUBMISSION_STATE, OrderStatus.UNKNOWN}


@dataclass
class EodActionCallbacks:
    """Injected broker-boundary actions -- this module never imports
    :mod:`src.api.kis_order` directly, mirroring
    :mod:`src.services.entry_attempt_manager`/:mod:`src.services.position_manager`.
    """

    find_open_entry_order: Callable[[TradeCardState], Optional[BrokerOrder]]
    reconcile_order: Callable[[BrokerOrder], BrokerOrder]
    cancel_order: Callable[[str], None]  # by client_order_id
    # Full account-wide discovery (open orders + history + reserved orders)
    # for this card's symbol. Code review finding P1-15: "no local order
    # found" must never by itself be treated as "no order exists" -- only a
    # *complete* discovery query (BrokerOrderDiscoveryResult.complete,
    # i.e. every source succeeded with no errors) can confirm that.
    # Optional/defaulted to an always-complete-empty result so existing
    # callers/tests that never exercise the "no order" path are unaffected.
    discover_all_orders: Callable[[TradeCardState], BrokerOrderDiscoveryResult] = (
        lambda card: BrokerOrderDiscoveryResult(
            open_orders_complete=True, history_complete=True, reserved_orders_complete=True
        )
    )


class EodTradingService:
    def __init__(
        self,
        *,
        entry_attempt_manager: EntryAttemptManager,
        position_manager: PositionManager,
        callbacks: EodActionCallbacks,
        reservations_path: Optional[Path] = None,
    ) -> None:
        self._entry_attempt_manager = entry_attempt_manager
        self._position_manager = position_manager
        self._callbacks = callbacks
        # Resolved once here rather than relying on capital_allocator's own
        # function-default parameter, which is bound at import time and
        # will not pick up a later monkeypatch/override -- same reasoning
        # as EntryAttemptManager.__init__.
        self._reservations_path = reservations_path or capital_allocator.RESERVATIONS_FILE

    def run_eod_cleanup(self, cards: List[TradeCardState]) -> List[TradeCardState]:
        """Section 13's table, applied to every card. The caller owns the
        timing gate (``EOD_ENTRY_CLEANUP_SECONDS_BEFORE_CLOSE`` before
        close) -- this method always processes whatever it's given.
        """
        changed: List[TradeCardState] = []
        for card in cards:
            if card.board_status == BoardStatus.BUY_TODAY:
                if self._reset_buy_today_with_no_order(card):
                    changed.append(card)
            elif card.board_status == BoardStatus.ENTRY_PENDING:
                if self._resolve_entry_pending_at_eod(card):
                    changed.append(card)
            elif card.board_status == BoardStatus.OPEN_POSITION:
                if self._stop_incomplete_target_completion(card):
                    changed.append(card)
        return changed

    # -- "Buy Today with no submitted order" (section 512-516) ----------

    def _reset_buy_today_with_no_order(self, card: TradeCardState) -> bool:
        order = self._callbacks.find_open_entry_order(card)
        if order is not None:
            # An order does exist -- this card's board_status is stale
            # (should already be ENTRY_PENDING); leave it for that branch
            # rather than silently discarding a live order's trail here.
            return False
        # P1-15: a single "not found" lookup is not proof nothing was
        # submitted -- confirm with a full account-wide discovery query
        # before treating this as safe. An incomplete/failed discovery
        # leaves the card exactly where it is for a later heartbeat to
        # retry, rather than silently resetting on a possibly-stale view.
        discovery = self._callbacks.discover_all_orders(card)
        if not discovery.complete:
            return False
        card.board_status = BoardStatus.BUYLIST
        card.entry_runtime_status = None
        card.entry_block_reason = ""
        card.entry_orb_high = None
        card.entry_orb_low = None
        card.entry_orb_window = None
        card.entry_trigger = None
        self._entry_attempt_manager.reset_symbol(card.environment, card.account_no, card.symbol)
        if card.capital_reservation_id:
            capital_allocator.release_reservation(card.capital_reservation_id, path=self._reservations_path)
            card.capital_reservation_id = ""
        return True

    # -- Entry Pending at EOD (section 517-529) --------------------------

    def _resolve_entry_pending_at_eod(self, card: TradeCardState) -> bool:
        order = self._callbacks.find_open_entry_order(card)
        if order is None:
            # P1-15: the local lookup alone cannot confirm "no order
            # exists" -- require a complete account-wide discovery first.
            # An incomplete query must leave the card in
            # Entry Pending/Reconciling (section 526-529's "do not assume
            # cancellation" applies just as much to "assume no order").
            discovery = self._callbacks.discover_all_orders(card)
            if not discovery.complete:
                if card.entry_runtime_status != EntryRuntimeStatus.DATA_UNAVAILABLE:
                    card.entry_runtime_status = EntryRuntimeStatus.DATA_UNAVAILABLE
                    return True
                return False
            card.board_status = BoardStatus.BUYLIST
            card.entry_runtime_status = None
            return True

        refreshed = self._callbacks.reconcile_order(order)

        if refreshed.status in _UNRESOLVED_STATUSES:
            # Section 526-529: "Remain Entry Pending — Reconciling. Do not
            # assume cancellation. Do not move to Buylist."
            card.entry_runtime_status = EntryRuntimeStatus.DATA_UNAVAILABLE
            return True

        if refreshed.filled_quantity <= 0:
            # Section 517-521: zero fills.
            if refreshed.status not in _ALREADY_TERMINAL_STATUSES:
                self._callbacks.cancel_order(refreshed.client_order_id)
            if refreshed.capital_reservation_id:
                capital_allocator.release_reservation(refreshed.capital_reservation_id, path=self._reservations_path)
            card.board_status = BoardStatus.BUYLIST
            card.entry_runtime_status = None
            card.capital_reservation_id = ""
            return True

        # Section 522-525: any fill -- cancel the remaining entry quantity,
        # reconcile the actual filled position, move to Open Positions.
        if refreshed.status not in _ALREADY_TERMINAL_STATUSES:
            self._callbacks.cancel_order(refreshed.client_order_id)
        if refreshed.capital_reservation_id:
            capital_allocator.release_reservation(refreshed.capital_reservation_id, path=self._reservations_path)
        card.board_status = BoardStatus.OPEN_POSITION
        card.broker_quantity = refreshed.filled_quantity
        card.orderable_quantity = refreshed.filled_quantity
        card.average_entry_price = refreshed.avg_fill_price
        card.entry_remaining_target_quantity = 0  # stop attempting completion at EOD
        card.position_runtime_status = PositionRuntimeStatus.OPEN
        card.capital_reservation_id = ""
        self._position_manager.apply_first_fill_stop(
            card,
            entry_orb_low=card.entry_orb_low or 0.0,
            entry_orb_window=card.entry_orb_window or card.selected_orb_window or "",
        )
        card.entry_runtime_status = None
        return True

    # -- Open position with an incomplete target (section 530-533) ------

    def _stop_incomplete_target_completion(self, card: TradeCardState) -> bool:
        if card.entry_remaining_target_quantity <= 0:
            return False
        order = self._callbacks.find_open_entry_order(card)
        if order is not None:
            self._callbacks.cancel_order(order.client_order_id)
        card.entry_remaining_target_quantity = 0
        card.position_runtime_status = PositionRuntimeStatus.OPEN
        return True


def reconcile_unresolved_orders_at_startup(
    cards: List[TradeCardState],
    *,
    position_manager: PositionManager,
    callbacks: EodActionCallbacks,
) -> List[TradeCardState]:
    """Section 1029 ("Application restart with unknown order") and Phase 6's
    "Run full startup reconciliation" (section 1070-1075): review finding
    P1-16 -- a restart must resolve every ENTRY_PENDING card's *order*
    state, not only broker *positions* (which
    :func:`run_startup_reconciliation`/``reconcile_broker_positions``
    already covers).

    Unlike EOD cleanup, this never forces a cancellation -- a still-working
    order is left alone for the normal heartbeat to keep managing; this
    only applies what reconciliation already reveals (a fill, or a
    broker-confirmed terminal status), and still refuses to assume "no
    order" from anything less than a complete discovery query (P1-15).
    """
    changed: List[TradeCardState] = []
    for card in cards:
        if card.board_status != BoardStatus.ENTRY_PENDING:
            continue
        order = callbacks.find_open_entry_order(card)
        if order is None:
            discovery = callbacks.discover_all_orders(card)
            if not discovery.complete:
                if card.entry_runtime_status != EntryRuntimeStatus.DATA_UNAVAILABLE:
                    card.entry_runtime_status = EntryRuntimeStatus.DATA_UNAVAILABLE
                    changed.append(card)
                continue
            card.board_status = BoardStatus.BUYLIST
            card.entry_runtime_status = None
            changed.append(card)
            continue

        refreshed = callbacks.reconcile_order(order)
        if refreshed.status in _UNRESOLVED_STATUSES:
            if card.entry_runtime_status != EntryRuntimeStatus.DATA_UNAVAILABLE:
                card.entry_runtime_status = EntryRuntimeStatus.DATA_UNAVAILABLE
                changed.append(card)
            continue

        if refreshed.filled_quantity > 0:
            card.board_status = BoardStatus.OPEN_POSITION
            card.broker_quantity = refreshed.filled_quantity
            card.orderable_quantity = refreshed.filled_quantity
            card.average_entry_price = refreshed.avg_fill_price
            card.entry_remaining_target_quantity = 0
            card.position_runtime_status = PositionRuntimeStatus.OPEN
            position_manager.apply_first_fill_stop(
                card,
                entry_orb_low=card.entry_orb_low or 0.0,
                entry_orb_window=card.entry_orb_window or card.selected_orb_window or "",
            )
            card.entry_runtime_status = None
            changed.append(card)
        elif refreshed.status in _ALREADY_TERMINAL_STATUSES:
            # Terminal with zero fill (CANCELLED/REJECTED/EXPIRED) --
            # confirmed gone, safe to return to Buylist.
            card.board_status = BoardStatus.BUYLIST
            card.entry_runtime_status = None
            changed.append(card)
        # else: genuinely still working (ACCEPTED/WORKING/CANCEL_REQUESTED)
        # -- leave it for the normal heartbeat; a restart must not force a
        # cancellation just because the process came back up.
    return changed


def run_startup_reconciliation(
    cards: List[TradeCardState],
    *,
    environment: str,
    account_no: str,
    position_snapshot: Optional[Dict],
    position_manager: PositionManager,
    symbol_name_lookup: Callable[[str], str] = lambda symbol: symbol,
    order_callbacks: Optional[EodActionCallbacks] = None,
) -> List[TradeCardState]:
    """Section 1070-1075 ("Run full startup reconciliation") and the
    "Application restart with open positions" / "Broker position exists
    without local card" test scenarios (section 1028-1032).

    Unlike :mod:`src.services.handoff_reconciliation` (which runs
    specifically when a device claims main-device status),
    this runs at every process startup regardless of main/secondary role --
    read-only devices still need their local card view corrected against
    broker truth before showing anything to the user. Composes
    :func:`src.services.position_manager.extract_overseas_holdings` with
    :meth:`~src.services.position_manager.PositionManager.reconcile_broker_positions`.

    "Resume queued exits after restart" needs no separate mechanism here:
    board_status/``sell_all_at_market_open``/``exit_all_required`` are
    already persisted per-card in :mod:`src.services.trade_card_repository`,
    so the next heartbeat pass (:mod:`src.services.trading_engine`) resumes
    exactly where it left off once this function has corrected quantities
    against broker truth.

    ``order_callbacks``, when supplied, additionally reconciles every
    ENTRY_PENDING card's *order* state via
    :func:`reconcile_unresolved_orders_at_startup` (review finding P1-16) --
    positions alone are not enough to safely resume: an order that filled
    or was cancelled entirely while the process was down needs the same
    broker-truth correction a live position quantity gets.
    """
    holdings: List[BrokerHolding] = extract_overseas_holdings(position_snapshot)
    changed = position_manager.reconcile_broker_positions(
        cards,
        holdings,
        environment=environment,
        account_no=account_no,
        symbol_name_lookup=symbol_name_lookup,
    )
    if order_callbacks is not None:
        order_changed = reconcile_unresolved_orders_at_startup(
            cards, position_manager=position_manager, callbacks=order_callbacks
        )
        seen = {id(card) for card in changed}
        for card in order_changed:
            if id(card) not in seen:
                seen.add(id(card))
                changed.append(card)
    return changed
