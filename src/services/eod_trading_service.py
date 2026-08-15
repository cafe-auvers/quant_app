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

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

from sqlalchemy.engine import Engine

from src.core.execution_request import CancelIntent
from src.core.order_state import (
    BrokerOrder,
    OrderStatus,
)
from src.core.trade_card_state import (
    BoardStatus,
    EntryRuntimeStatus,
    PositionRuntimeStatus,
    TradeCardState,
)
from src.services import capital_allocator
from src.services.entry_attempt_manager import (
    AttemptDeadlineAction,
    EntryAttemptManager,
    EntryCancelReason,
)
from src.services.position_manager import (
    PositionManager,
    _default_cancel_intent_factory,
    request_cancel_with_lifecycle,
)

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
    cancel_order: Callable[[CancelIntent], None]
    cancel_intent_factory: Callable[[TradeCardState, str, str], CancelIntent] = (
        _default_cancel_intent_factory
    )
    persist_cancel_state: Callable[[TradeCardState], None] = lambda card: None

    def request_cancel(
        self, card: TradeCardState, client_order_id: str, *, scope: str = "ENTRY"
    ) -> None:
        request_cancel_with_lifecycle(
            card=card,
            client_order_id=client_order_id,
            scope=scope,
            cancel_order=self.cancel_order,
            cancel_intent_factory=self.cancel_intent_factory,
            persist_cancel_state=self.persist_cancel_state,
        )


class EodTradingService:
    def __init__(
        self,
        *,
        entry_attempt_manager: EntryAttemptManager,
        position_manager: PositionManager,
        callbacks: EodActionCallbacks,
        reservations_path: Optional[Path] = None,
        capital_reservation_engine: Optional[Engine] = None,
    ) -> None:
        self._entry_attempt_manager = entry_attempt_manager
        self._position_manager = position_manager
        self._callbacks = callbacks
        # Resolved once here rather than relying on capital_allocator's own
        # function-default parameter, which is bound at import time and
        # will not pick up a later monkeypatch/override -- same reasoning
        # as EntryAttemptManager.__init__.
        self._reservations_path = reservations_path or capital_allocator.RESERVATIONS_FILE
        # Review finding P1-1: mirror capital-reservation releases to the
        # shared database too, when one is available, for the same
        # cross-device-visibility reason EntryAttemptManager does.
        self._capital_reservation_engine = capital_reservation_engine

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
            # A durable local order is sufficient to restore the card's
            # tracking scope. Account-wide broker discovery and all
            # ownership decisions belong exclusively to
            # account_reconciliation; EOD must not heuristically claim an
            # otherwise-unowned broker order.
            card.board_status = BoardStatus.ENTRY_PENDING
            card.entry_runtime_status = EntryRuntimeStatus.ORDER_PENDING
            if order.attempt_group_id:
                card.entry_attempt_group_id = order.attempt_group_id
            if order.attempt_number:
                card.entry_attempt_count = order.attempt_number
            return True
        card.board_status = BoardStatus.BUYLIST
        card.entry_runtime_status = None
        card.entry_block_reason = ""
        card.entry_orb_high = None
        card.entry_orb_low = None
        card.entry_orb_window = None
        card.entry_trigger = None
        self._entry_attempt_manager.reset_symbol(card.environment, card.account_no, card.symbol)
        if card.capital_reservation_id:
            capital_allocator.release_reservation(
                card.capital_reservation_id,
                path=self._reservations_path,
                engine=self._capital_reservation_engine,
            )
            card.capital_reservation_id = ""
        return True

    # -- Entry Pending at EOD (section 517-529, review finding P0-7) -----

    def _resolve_entry_pending_at_eod(self, card: TradeCardState) -> bool:
        """Drives the *same* two-phase request-then-confirm cancellation
        state machine (:meth:`EntryAttemptManager.resolve_entry_order`) the
        normal intraday heartbeat uses, instead of calling ``cancel_order``
        and immediately assuming the cancellation succeeded. Review finding
        P0-7: the old version released the capital reservation and moved
        the card to Buylist/Open Positions in the same call that requested
        the cancel -- if the broker actually filled part or all of the
        order *after* that instant, the fill would be silently orphaned
        (no card, no reservation, no stop). Now:

        ENTRY_PENDING -> (cancel requested, EOD reason stamped) ->
        AWAIT_CANCEL_CONFIRMATION -> next call confirms ->
        zero fill -> Buylist / any fill -> Open Positions / still
        unresolved -> stays Entry Pending (Reconciling).
        """
        order = self._callbacks.find_open_entry_order(card)
        if order is None:
            # The account reducer is the sole owner of broker discovery.
            # If no durable local order reached this EOD path, fail closed
            # and wait for the next account pass; never infer ownership or
            # terminal state from a per-card heuristic query here.
            if card.entry_runtime_status != EntryRuntimeStatus.DATA_UNAVAILABLE:
                card.entry_runtime_status = EntryRuntimeStatus.DATA_UNAVAILABLE
                return True
            return False

        refreshed = self._callbacks.reconcile_order(order)

        if refreshed.status in _UNRESOLVED_STATUSES:
            # Section 526-529: "Remain Entry Pending — Reconciling. Do not
            # assume cancellation. Do not move to Buylist."
            card.entry_runtime_status = EntryRuntimeStatus.DATA_UNAVAILABLE
            return True

        if not card.entry_cancel_in_flight:
            card.entry_cancel_reason = EntryCancelReason.EOD.value
        action = self._entry_attempt_manager.resolve_entry_order(
            refreshed,
            at_deadline=True,
            cancel_requested=True,
            cancel_order=lambda o: self._callbacks.request_cancel(
                card, o.client_order_id, scope="ENTRY"
            ),
        )

        if action in (
            AttemptDeadlineAction.STILL_WORKING,
            AttemptDeadlineAction.BLOCK_SYMBOL_PENDING_RECONCILIATION,
        ):
            # Unresolved -- stays Entry Pending/Reconciling; capital stays
            # reserved until a later EOD pass (or startup reconciliation)
            # resolves it.
            if card.entry_runtime_status != EntryRuntimeStatus.DATA_UNAVAILABLE:
                card.entry_runtime_status = EntryRuntimeStatus.DATA_UNAVAILABLE
                return True
            return False

        if action == AttemptDeadlineAction.AWAIT_CANCEL_CONFIRMATION:
            if card.entry_cancel_in_flight:
                return False
            card.entry_cancel_in_flight = True
            return True

        # Every remaining action is terminal -- the broker has confirmed a
        # final status for the cancel EOD requested; resolve_entry_order
        # has already settled/released the capital reservation.
        card.entry_cancel_in_flight = False
        card.entry_cancel_reason = ""
        card.entry_cancel_command_id = ""
        card.capital_reservation_id = ""

        if refreshed.filled_quantity > 0:
            # Section 522-525: confirmed cancelled with a fill (or, rarely,
            # confirmed FILLED outright) -- reconcile the actual filled
            # position and move to Open Positions.
            card.board_status = BoardStatus.OPEN_POSITION
            card.broker_quantity = refreshed.filled_quantity
            card.orderable_quantity = refreshed.filled_quantity
            card.average_entry_price = refreshed.avg_fill_price
            card.entry_remaining_target_quantity = 0  # stop attempting completion at EOD
            card.position_runtime_status = PositionRuntimeStatus.OPEN
            self._position_manager.apply_first_fill_stop(
                card,
                entry_orb_low=card.entry_orb_low or 0.0,
                entry_orb_window=card.entry_orb_window or card.selected_orb_window or "",
            )
            card.entry_runtime_status = None
            return True

        # Section 517-521: confirmed cancelled with zero fill.
        card.board_status = BoardStatus.BUYLIST
        card.entry_runtime_status = None
        return True

    # -- Open position with an incomplete target (section 530-533) ------

    def _stop_incomplete_target_completion(self, card: TradeCardState) -> bool:
        if card.entry_remaining_target_quantity <= 0:
            return False
        order = self._callbacks.find_open_entry_order(card)
        # Section 530-533: stop attempting to complete the target at EOD
        # regardless of what the cancel eventually resolves to -- unlike
        # the ENTRY_PENDING case above, an already-open position stays open
        # either way, so there is no Buylist/Open-Positions branch to wait
        # for. Still request+track the cancel through two-phase
        # confirmation (rather than assuming it immediately succeeded) so a
        # late fill from *this* order is protected and its capital
        # reservation is actually settled instead of silently orphaned --
        # entry_cancel_in_flight keeps _reconcile_entry_orders tracking it
        # on the next heartbeat regardless of board_status/remaining target.
        if order is not None:
            self._callbacks.request_cancel(card, order.client_order_id, scope="ENTRY")
            if not card.entry_cancel_in_flight:
                card.entry_cancel_in_flight = True
                card.entry_cancel_reason = EntryCancelReason.EOD.value
        card.entry_remaining_target_quantity = 0
        card.position_runtime_status = PositionRuntimeStatus.OPEN
        return True
