"""One-second engine orchestrator. ``buydashboard_to_kanban.md`` section 20.

Wires :mod:`src.services.entry_attempt_manager`,
:mod:`src.services.realtime_market_data`, and
:mod:`src.services.position_manager` together. Deliberately takes the
current set of :class:`~src.core.trade_card_state.TradeCardState` rows as an
explicit argument and *returns* the ones it mutated, rather than reaching
into :mod:`src.services.trade_card_repository` itself -- callers own
fetch/persist (with the real optimistic-version check), this class owns
only the per-tick and per-heartbeat *decisions*. That keeps it testable the
same way :mod:`src.services.entry_attempt_manager` and
:mod:`src.services.position_manager` are: synchronous, explicit in, explicit
out, broker/DB access only via injected callables.

In production, ``run_heartbeat`` is invoked by a ``QTimer`` on
``ENGINE_HEARTBEAT_SECONDS`` (default 1s) and ``evaluate_quote`` is wired to
:meth:`~src.services.realtime_market_data.RealtimeMarketDataService.on_quote`.
Both are no-ops while :func:`src.core.execution_config.is_buyboard_engine_enabled`
is False, so the legacy Buy Dashboard 60-second monitor remains the sole
live trading path until the new engine is explicitly turned on.

This module was substantially reworked after an external code review of the
first pass. Fixes folded in here (see the docstrings on the individual
methods for the exact finding each one addresses):

- P0-2/P0-3/P1-4: dragging to Sell All / Partial Sell now actually results
  in an order once the engine runs -- submission is centralized in the
  retry stages below rather than (not) happening inline in
  ``PositionManager``, and a cancel is never overlapped with a replacement
  submission.
- P0-4: cards stuck in RETRY_COOLDOWN/WAITING_FOR_CAPITAL/DATA_UNAVAILABLE
  are actively recovered back to EXECUTE_READY instead of sitting there
  for the rest of the session.
- P0-5: a partially-filled entry's remaining target quantity is actually
  retried, not just recorded.
- P0-6/P0-7: any working entry order is reconciled every heartbeat tick
  (not only at its 15s deadline), fills are protected the instant they're
  observed, and a cancel request is never treated as a completed
  cancellation until the broker confirms it.
- P0-9: a retried/completion entry price is checked against the live quote
  so it stays marketable instead of only ever using the original ORB
  trigger price.
- P1-6: an existing open position whose quote has gone stale/disconnected
  is flagged immediately, independent of the new-entry staleness gate.
- P1-14: the EOD cleanup service is actually invoked from the heartbeat
  (gated on an injectable "are we in the EOD window" hook) instead of
  existing only as a module nothing ever called.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional

from src.core import execution_config
from src.core.execution_config import is_buyboard_engine_enabled
from src.core.order_state import BrokerOrder
from src.core.trade_card_state import (
    BoardStatus,
    EntryRuntimeStatus,
    PositionRuntimeStatus,
    TradeCardState,
)
from src.services.entry_attempt_manager import (
    AttemptDeadlineAction,
    AttemptOutcome,
    EntryAttemptManager,
    EntryTrigger,
)
from src.services.eod_trading_service import EodTradingService
from src.services.position_manager import PositionActionCallbacks, PositionManager
from src.services.realtime_market_data import (
    QuoteSnapshot,
    RealtimeMarketDataService,
    is_quote_stale,
)

logger = logging.getLogger(__name__)

# board_status values whose card should react to a live price tick.
_TICK_REACTIVE_POSITION_STATUSES = {BoardStatus.OPEN_POSITION, BoardStatus.PARTIAL_SELL}

_OUTCOME_TO_ENTRY_RUNTIME_STATUS = {
    AttemptOutcome.WAITING_FOR_CAPITAL: EntryRuntimeStatus.WAITING_FOR_CAPITAL,
    AttemptOutcome.COOLDOWN: EntryRuntimeStatus.RETRY_COOLDOWN,
    AttemptOutcome.RATE_LIMITED: EntryRuntimeStatus.RETRY_COOLDOWN,
    AttemptOutcome.REJECTED: EntryRuntimeStatus.RETRY_COOLDOWN,
    AttemptOutcome.DUPLICATE_ORDER: EntryRuntimeStatus.ORDER_PENDING,
}

_DATA_STALE_WARNING = "DATA_STALE"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class EntryDeadlineLookup:
    """How the engine finds and refreshes the working entry order for an
    ENTRY_PENDING *or* entry-completing OPEN_POSITION card. Both are
    injected -- this module never queries the order ledger or KIS directly.
    """

    find_open_entry_order: Callable[[TradeCardState], Optional[BrokerOrder]]
    reconcile_order: Callable[[BrokerOrder], BrokerOrder]


class TradingEngine:
    def __init__(
        self,
        *,
        entry_attempt_manager: EntryAttemptManager,
        position_manager: PositionManager,
        market_data: RealtimeMarketDataService,
        position_callbacks: PositionActionCallbacks,
        entry_deadline_lookup: EntryDeadlineLookup,
        eod_service: Optional[EodTradingService] = None,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._entry_attempt_manager = entry_attempt_manager
        self._position_manager = position_manager
        self._market_data = market_data
        self._position_callbacks = position_callbacks
        self._entry_deadline_lookup = entry_deadline_lookup
        self._eod_service = eod_service
        self._clock = clock

    @staticmethod
    def is_enabled() -> bool:
        return is_buyboard_engine_enabled()

    # --- Every market-data tick (section 766-770, 784-788) ------------

    def evaluate_quote(
        self, cards: List[TradeCardState], quote: QuoteSnapshot
    ) -> List[TradeCardState]:
        """Update price and evaluate the stop immediately for the one card
        matching ``quote.symbol``, if any. Entry-trigger re-evaluation off a
        tick is intentionally *not* here -- ORB candidate recomputation
        (unchanged, per section 15) happens on the caller's own cadence and
        feeds ``card.entry_runtime_status``/``entry_trigger`` before
        ``run_heartbeat`` looks at BUY_TODAY cards.
        """
        if not self.is_enabled():
            return []
        card = self._find_card(cards, environment=None, account_no=None, symbol=quote.symbol)
        if card is None or card.board_status not in _TICK_REACTIVE_POSITION_STATUSES:
            return []
        was_exit_required = card.exit_all_required
        was_board_status = card.board_status
        was_warnings = list(card.warnings)

        self._clear_stale_warning(card)
        self._position_manager.evaluate_tick(card, quote.last_price)
        if card.exit_all_required and card.board_status != BoardStatus.SELL_ALL:
            if card.board_status == BoardStatus.PARTIAL_SELL:
                # Section 671-697: cancel the working partial-sell order
                # (if any) before escalating to a full liquidation.
                working_sell = self._position_callbacks.find_open_sell_order(card)
                self._position_manager.handle_stop_triggered_during_partial_sell(
                    card,
                    callbacks=self._position_callbacks,
                    working_partial_sell_client_order_id=(
                        working_sell.client_order_id if working_sell is not None else None
                    ),
                )
            else:
                # Section 500-504: "stop hit while completing entry" --
                # an ENTRY_COMPLETING card still has board_status
                # OPEN_POSITION, so it reaches this branch too. Cancel any
                # remaining BUY order for the target completion before
                # selling the actual (already-filled) remainder.
                working_buy = self._entry_deadline_lookup.find_open_entry_order(card)
                self._position_manager.start_sell_all(
                    card,
                    callbacks=self._position_callbacks,
                    working_buy_client_order_id=(
                        working_buy.client_order_id if working_buy is not None else None
                    ),
                )
                card.entry_remaining_target_quantity = 0

        if (
            card.exit_all_required != was_exit_required
            or card.board_status != was_board_status
            or card.warnings != was_warnings
        ):
            return [card]
        return []

    def _clear_stale_warning(self, card: TradeCardState) -> None:
        if _DATA_STALE_WARNING in card.warnings:
            card.warnings = [w for w in card.warnings if w != _DATA_STALE_WARNING]

    # --- Heartbeat (section 789-799) -----------------------------------

    def run_heartbeat(self, cards: List[TradeCardState]) -> List[TradeCardState]:
        if not self.is_enabled():
            return []
        changed: List[TradeCardState] = []
        changed.extend(self._recover_retryable_cards(cards))
        changed.extend(self._evaluate_buy_today(cards))
        changed.extend(self._reconcile_entry_orders(cards))
        changed.extend(self._process_entry_completion(cards))
        changed.extend(self._process_partial_sell_requests(cards))
        changed.extend(self._reconcile_partial_sell_fills(cards))
        changed.extend(self._process_queued_market_open_sells(cards))
        changed.extend(self._retry_incomplete_sell_alls(cards))
        changed.extend(self._detect_stale_position_quotes(cards))
        changed.extend(self._run_eod_cleanup_if_due(cards))
        # De-duplicate while preserving order, in case a card was touched by
        # more than one stage in the same pass.
        seen = set()
        unique: List[TradeCardState] = []
        for card in changed:
            if id(card) in seen:
                continue
            seen.add(id(card))
            unique.append(card)
        return unique

    # -- Retry-state recovery (review finding P0-4) -----------------------

    def _recover_retryable_cards(self, cards: List[TradeCardState]) -> List[TradeCardState]:
        """A card stuck in RETRY_COOLDOWN/WAITING_FOR_CAPITAL/DATA_UNAVAILABLE
        must eventually get another shot at EXECUTE_READY, or the "keep
        trying throughout the market day" behavior never happens. This does
        NOT recompute the ORB candidate itself (unchanged, section 15) --
        it only decides *when* to let ``_evaluate_buy_today`` try the
        existing candidate (``card.entry_trigger``/``planned_quantity``)
        again; that method re-validates price/capital/data freshness on its
        own and will put the card right back into whichever blocked state
        still applies.
        """
        changed: List[TradeCardState] = []
        now = self._clock()
        for card in cards:
            if card.board_status != BoardStatus.BUY_TODAY:
                continue
            if card.entry_runtime_status == EntryRuntimeStatus.RETRY_COOLDOWN:
                if card.next_retry_at is not None and now >= card.next_retry_at:
                    card.entry_runtime_status = EntryRuntimeStatus.EXECUTE_READY
                    card.next_retry_at = None
                    changed.append(card)
            elif card.entry_runtime_status == EntryRuntimeStatus.WAITING_FOR_CAPITAL:
                # Capital may have freed up since the last tick -- give it
                # another shot; _evaluate_buy_today puts it right back to
                # WAITING_FOR_CAPITAL if it's still unavailable.
                card.entry_runtime_status = EntryRuntimeStatus.EXECUTE_READY
                changed.append(card)
            elif card.entry_runtime_status == EntryRuntimeStatus.DATA_UNAVAILABLE:
                quote = self._market_data.latest_quote(card.symbol)
                if self._market_data.is_connected() and not is_quote_stale(quote):
                    card.entry_runtime_status = EntryRuntimeStatus.EXECUTE_READY
                    changed.append(card)
        return changed

    # -- Marketable price (review finding P0-9) ---------------------------

    def _marketable_entry_price(
        self, card: TradeCardState, quote: Optional[QuoteSnapshot]
    ) -> Optional[float]:
        """Section 819-822: an entry submission needs a live KIS quote, and
        that quote should actually inform the submitted price -- resending
        only the original ORB trigger price can leave a limit order
        sitting unmarketable below a market that has since moved up.
        Reviewed finding P0-9's fix: take the more aggressive (higher, for
        a BUY) of the ORB trigger, the current ask, and the current last
        price.
        """
        entry_trigger = card.entry_trigger or card.breakout_price
        if not entry_trigger:
            return None
        candidates = [entry_trigger]
        if quote is not None:
            if quote.ask:
                candidates.append(quote.ask)
            if quote.last_price:
                candidates.append(quote.last_price)
        return max(candidates)

    # -- BUY_TODAY -> entry attempts -------------------------------------

    def _evaluate_buy_today(self, cards: List[TradeCardState]) -> List[TradeCardState]:
        triggers: List[EntryTrigger] = []
        trigger_cards: Dict[str, TradeCardState] = {}
        changed: List[TradeCardState] = []
        now = self._clock()

        for card in cards:
            if card.board_status != BoardStatus.BUY_TODAY:
                continue
            if card.entry_runtime_status != EntryRuntimeStatus.EXECUTE_READY:
                continue
            # Section 827-832 step 2: "WebSocket disconnects... block new
            # entries" -- checked independently of quote staleness so a
            # disconnect blocks entries immediately, not only once the last
            # cached quote ages past QUOTE_STALE_AFTER_SECONDS.
            quote = self._market_data.latest_quote(card.symbol)
            if not self._market_data.is_connected() or is_quote_stale(quote):
                # Section 826, 839: a stale/missing execution-grade quote
                # blocks the attempt outright -- never guess with the last
                # known price.
                if card.entry_runtime_status != EntryRuntimeStatus.DATA_UNAVAILABLE:
                    card.entry_runtime_status = EntryRuntimeStatus.DATA_UNAVAILABLE
                    changed.append(card)
                continue
            price = self._marketable_entry_price(card, quote)
            if not price or card.planned_quantity <= 0:
                continue
            key = f"{card.environment}:{card.account_no}:{card.symbol}"
            trigger_cards[key] = card
            triggers.append(
                EntryTrigger(
                    environment=card.environment,
                    account_no=card.account_no,
                    symbol=card.symbol,
                    trigger_at=now,
                    kanban_priority=card.kanban_priority,
                    quantity=card.planned_quantity,
                    limit_price=price,
                    notional=card.planned_quantity * price,
                )
            )

        if not triggers:
            return changed

        results = self._entry_attempt_manager.process_triggers(triggers)
        for result in results:
            key = f"{result.trigger.environment}:{result.trigger.account_no}:{result.trigger.symbol}"
            card = trigger_cards.get(key)
            if card is None:
                continue
            if result.outcome == AttemptOutcome.SUBMITTED:
                card.board_status = BoardStatus.ENTRY_PENDING
                card.entry_runtime_status = EntryRuntimeStatus.ORDER_PENDING
                card.entry_attempt_group_id = result.attempt_group_id
                card.entry_attempt_count = result.attempt_count
                card.next_retry_at = None
            elif result.outcome == AttemptOutcome.SYMBOL_LOCKED:
                continue  # transient, another in-process attempt is running
            else:
                card.entry_runtime_status = _OUTCOME_TO_ENTRY_RUNTIME_STATUS.get(
                    result.outcome, card.entry_runtime_status
                )
                card.entry_attempt_group_id = result.attempt_group_id
                card.entry_attempt_count = result.attempt_count
                card.next_retry_at = result.retry_at  # P0-4: persist so a restart doesn't lose it
            changed.append(card)
        return changed

    # -- Continuous entry-order reconciliation (review findings P0-6/P0-7) --

    def _entry_tracking_scope(self, cards: List[TradeCardState]) -> List[TradeCardState]:
        return [
            card
            for card in cards
            if card.board_status == BoardStatus.ENTRY_PENDING
            or (
                card.board_status == BoardStatus.OPEN_POSITION
                and card.entry_remaining_target_quantity > 0
            )
        ]

    def _reconcile_entry_orders(self, cards: List[TradeCardState]) -> List[TradeCardState]:
        """Runs every heartbeat tick -- not only at the 15s deadline
        (P0-6) -- for any card with a tracked working entry order,
        including a partially-filled OPEN_POSITION card still trying to
        complete its target. Any newly observed fill is protected
        immediately; a cancel is escalated only at the deadline (or a user
        cancel request) and is never treated as complete until the broker
        actually confirms it (P0-7).
        """
        changed: List[TradeCardState] = []
        now = self._clock()
        for card in self._entry_tracking_scope(cards):
            order = self._entry_deadline_lookup.find_open_entry_order(card)
            if order is None:
                continue
            refreshed = self._entry_deadline_lookup.reconcile_order(order)

            fill_protected = False
            if refreshed.filled_quantity > card.broker_quantity:
                self._protect_new_fill(card, refreshed)
                fill_protected = True

            cancel_requested = card.entry_block_reason == "cancel_requested"
            deadline_passed = self._entry_deadline_passed(refreshed, now)
            already_has_position = card.broker_quantity > 0

            action = self._entry_attempt_manager.resolve_entry_order(
                refreshed,
                at_deadline=deadline_passed,
                cancel_requested=cancel_requested,
                cancel_order=lambda o: self._position_callbacks.cancel_order(o.client_order_id),
            )
            resolved = self._apply_entry_resolution(
                card,
                refreshed,
                action,
                cancel_requested=cancel_requested,
                already_has_position=already_has_position,
                now=now,
            )
            if resolved or fill_protected:
                changed.append(card)
        return changed

    @staticmethod
    def _entry_deadline_passed(order: BrokerOrder, now: datetime) -> bool:
        if not order.attempt_deadline_at:
            return False
        try:
            deadline = datetime.fromisoformat(order.attempt_deadline_at)
        except ValueError:
            return False
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        return now >= deadline

    def _protect_new_fill(self, card: TradeCardState, order: BrokerOrder) -> None:
        """A confirmed fill is protected the instant it is observed,
        regardless of whether the attempt has reached its deadline or a
        cancel is in flight (P0-6: "any confirmed fill must create and
        protect the position immediately").
        """
        card.board_status = BoardStatus.OPEN_POSITION
        card.broker_quantity = order.filled_quantity
        card.orderable_quantity = order.filled_quantity
        card.average_entry_price = order.avg_fill_price
        remaining_target = max(0, card.target_position_quantity - order.filled_quantity)
        card.entry_remaining_target_quantity = remaining_target
        card.position_runtime_status = (
            PositionRuntimeStatus.ENTRY_COMPLETING if remaining_target > 0 else PositionRuntimeStatus.OPEN
        )
        self._position_manager.apply_first_fill_stop(
            card,
            entry_orb_low=card.entry_orb_low or 0.0,
            entry_orb_window=card.entry_orb_window or card.selected_orb_window or "",
        )

    def _apply_entry_resolution(
        self,
        card: TradeCardState,
        order: BrokerOrder,
        action: AttemptDeadlineAction,
        *,
        cancel_requested: bool,
        already_has_position: bool,
        now: datetime,
    ) -> bool:
        if action == AttemptDeadlineAction.STILL_WORKING:
            return False

        if action == AttemptDeadlineAction.AWAIT_CANCEL_CONFIRMATION:
            if card.entry_cancel_in_flight:
                return False
            card.entry_cancel_in_flight = True
            return True

        if action == AttemptDeadlineAction.MOVE_TO_OPEN_POSITION:
            self._protect_new_fill(card, order)
            card.entry_remaining_target_quantity = 0
            card.position_runtime_status = PositionRuntimeStatus.OPEN
            card.entry_runtime_status = None
            card.entry_block_reason = ""
            card.entry_cancel_in_flight = False
            card.next_retry_at = None
            return True

        if action == AttemptDeadlineAction.CONFIRMED_CANCELLED_WITH_FILL:
            if card.broker_quantity <= 0:
                self._protect_new_fill(card, order)
            # The user cancelled or the deadline passed with a partial
            # fill locked in -- keep the real shares, stop retrying the
            # remainder.
            card.entry_remaining_target_quantity = 0
            card.position_runtime_status = PositionRuntimeStatus.OPEN
            card.entry_runtime_status = None
            card.entry_block_reason = ""
            card.entry_cancel_in_flight = False
            card.next_retry_at = None
            return True

        if action == AttemptDeadlineAction.CONFIRMED_CANCELLED_ZERO_FILL:
            card.entry_cancel_in_flight = False
            if already_has_position:
                # A completion-attempt for the remainder was cancelled with
                # no additional fill -- the existing (already protected)
                # position stands; just stop trying to complete it.
                card.entry_remaining_target_quantity = 0
                card.position_runtime_status = PositionRuntimeStatus.OPEN
                card.entry_runtime_status = None
                card.entry_block_reason = ""
                return True
            if cancel_requested:
                card.board_status = BoardStatus.BUYLIST
                card.entry_runtime_status = None
                card.entry_block_reason = ""
            else:
                card.board_status = BoardStatus.BUY_TODAY
                card.entry_runtime_status = EntryRuntimeStatus.RETRY_COOLDOWN
                card.next_retry_at = now + timedelta(seconds=execution_config.ENTRY_RETRY_COOLDOWN_SECONDS)
            return True

        if action == AttemptDeadlineAction.RELEASE_AND_RETRY_AFTER_COOLDOWN:
            card.entry_cancel_in_flight = False
            if already_has_position:
                card.entry_remaining_target_quantity = 0
                card.position_runtime_status = PositionRuntimeStatus.OPEN
                card.entry_runtime_status = None
                return True
            card.board_status = BoardStatus.BUY_TODAY
            card.entry_runtime_status = EntryRuntimeStatus.RETRY_COOLDOWN
            card.next_retry_at = now + timedelta(seconds=execution_config.ENTRY_RETRY_COOLDOWN_SECONDS)
            return True

        if action == AttemptDeadlineAction.BLOCK_SYMBOL_PENDING_RECONCILIATION:
            # Section 526-529: "do not assume cancellation" -- capital
            # stays reserved, board_status is untouched, and
            # entry_block_reason ("cancel_requested", if set) is left as-is
            # so the next tick retries resolving it. ORDER_PENDING (not
            # DATA_UNAVAILABLE, which is reserved for stale/missing market
            # *quotes* in _evaluate_buy_today) is the correct badge here --
            # an order genuinely exists, its outcome is just unresolved.
            if card.entry_runtime_status != EntryRuntimeStatus.ORDER_PENDING:
                card.entry_runtime_status = EntryRuntimeStatus.ORDER_PENDING
                return True
            return False

        return False

    # -- Entry completion: retry the remaining target (review finding P0-5) --

    def _process_entry_completion(self, cards: List[TradeCardState]) -> List[TradeCardState]:
        """A partially-filled entry's remaining target quantity is actually
        resubmitted here, not just recorded and forgotten -- reuses the
        same per-symbol lock/capital-reservation machinery as the initial
        entry attempt, keyed off the card's still-populated
        ``entry_trigger``/ORB fields (unchanged strategy data, section 15).
        """
        triggers: List[EntryTrigger] = []
        trigger_cards: Dict[str, TradeCardState] = {}
        now = self._clock()

        for card in cards:
            if card.board_status != BoardStatus.OPEN_POSITION:
                continue
            if card.entry_remaining_target_quantity <= 0:
                continue
            if card.exit_all_required:
                continue  # a stop/liquidation is in progress -- do not buy more
            if self._entry_deadline_lookup.find_open_entry_order(card) is not None:
                continue  # already retrying, handled by _reconcile_entry_orders
            quote = self._market_data.latest_quote(card.symbol)
            if not self._market_data.is_connected() or is_quote_stale(quote):
                continue  # cannot safely re-attempt without fresh execution-grade data
            price = self._marketable_entry_price(card, quote)
            if not price:
                continue
            key = f"{card.environment}:{card.account_no}:{card.symbol}"
            trigger_cards[key] = card
            triggers.append(
                EntryTrigger(
                    environment=card.environment,
                    account_no=card.account_no,
                    symbol=card.symbol,
                    trigger_at=now,
                    kanban_priority=card.kanban_priority,
                    quantity=card.entry_remaining_target_quantity,
                    limit_price=price,
                    notional=card.entry_remaining_target_quantity * price,
                )
            )

        if not triggers:
            return []

        changed: List[TradeCardState] = []
        results = self._entry_attempt_manager.process_triggers(triggers)
        for result in results:
            key = f"{result.trigger.environment}:{result.trigger.account_no}:{result.trigger.symbol}"
            card = trigger_cards.get(key)
            if card is None or result.outcome == AttemptOutcome.SYMBOL_LOCKED:
                continue
            if result.outcome == AttemptOutcome.SUBMITTED:
                card.entry_runtime_status = EntryRuntimeStatus.ORDER_PENDING
                card.next_retry_at = None
            else:
                card.entry_runtime_status = _OUTCOME_TO_ENTRY_RUNTIME_STATUS.get(
                    result.outcome, card.entry_runtime_status
                )
                card.next_retry_at = result.retry_at
            changed.append(card)
        return changed

    # -- Partial Sell processing (review finding P0-3) -------------------

    def _process_partial_sell_requests(self, cards: List[TradeCardState]) -> List[TradeCardState]:
        """A card that just moved to PARTIAL_SELL (``pending_partial_sell_quantity``
        set by the board command, spec section 563-579) actually gets a
        PARTIAL_EXIT order submitted here -- previously only the visual
        column changed.
        """
        changed: List[TradeCardState] = []
        for card in cards:
            if card.board_status != BoardStatus.PARTIAL_SELL:
                continue
            if card.pending_partial_sell_quantity <= 0:
                continue
            if self._position_callbacks.find_open_sell_order(card) is not None:
                continue  # already submitted/working
            remaining = self._position_callbacks.refresh_orderable_quantity(
                card.environment, card.account_no, card.symbol
            )
            quantity = min(card.pending_partial_sell_quantity, remaining)
            if quantity <= 0:
                continue
            self._position_callbacks.submit_sell_order(
                environment=card.environment,
                account_no=card.account_no,
                symbol=card.symbol,
                quantity=quantity,
                reason="partial_sell",
            )
            card.orderable_quantity = remaining - quantity
            changed.append(card)
        return changed

    def _reconcile_partial_sell_fills(self, cards: List[TradeCardState]) -> List[TradeCardState]:
        """Once the PARTIAL_EXIT order resolves, apply the fill and move
        the stop to breakeven (spec section 596-603), returning the card to
        Open Positions.
        """
        changed: List[TradeCardState] = []
        for card in cards:
            if card.board_status != BoardStatus.PARTIAL_SELL:
                continue
            order = self._position_callbacks.find_open_sell_order(card)
            if order is None:
                continue
            refreshed = self._position_callbacks.reconcile_sell_order(order)
            if refreshed.status.value in ("FILLED", "CANCELLED", "REJECTED", "EXPIRED"):
                remaining = self._position_callbacks.refresh_orderable_quantity(
                    card.environment, card.account_no, card.symbol
                )
                self._position_manager.on_partial_exit_filled(card, refreshed_broker_quantity=remaining)
                card.pending_partial_sell_quantity = 0
                changed.append(card)
        return changed

    # -- Queued market-open Sell All (section 720-732) -------------------

    def _process_queued_market_open_sells(self, cards: List[TradeCardState]) -> List[TradeCardState]:
        changed: List[TradeCardState] = []
        for card in cards:
            if card.board_status != BoardStatus.SELL_ALL or not card.sell_all_at_market_open:
                continue
            if not self._market_is_open():
                continue
            self._position_manager.start_sell_all(card, callbacks=self._position_callbacks)
            card.sell_all_at_market_open = False
            changed.append(card)
        return changed

    # -- Sell All reprice/retry until flat (section 707-709, review P0-2/P1-4) --

    def _retry_incomplete_sell_alls(self, cards: List[TradeCardState]) -> List[TradeCardState]:
        """Section 707-709: "Reprice/retry until flat" / "Move to Closed
        only when broker confirms zero." This is now the *sole* place a
        Sell All liquidation order is submitted -- ``PositionManager.start_sell_all``
        and ``handle_stop_triggered_during_partial_sell`` only set intent
        and cancel a conflicting order (review finding P1-4); submitting
        here, gated on ``find_open_sell_order`` returning nothing, means a
        cancel is always broker-confirmed gone before its replacement goes
        out, so a late fill from the cancelled order can never overlap with
        the new one.
        """
        changed: List[TradeCardState] = []
        for card in cards:
            if card.board_status != BoardStatus.SELL_ALL or card.sell_all_at_market_open:
                continue
            if self._position_callbacks.find_open_sell_order(card) is not None:
                continue  # already working, or its cancel hasn't confirmed yet
            remaining = self._position_callbacks.refresh_orderable_quantity(
                card.environment, card.account_no, card.symbol
            )
            if remaining <= 0:
                if card.broker_quantity != 0:
                    card.broker_quantity = 0
                    card.orderable_quantity = 0
                self._position_manager.confirm_flat(card)
                changed.append(card)
                continue
            card.broker_quantity = remaining
            card.orderable_quantity = remaining
            self._position_callbacks.submit_sell_order(
                environment=card.environment,
                account_no=card.account_no,
                symbol=card.symbol,
                quantity=remaining,
                reason="sell_all_retry",
            )
            changed.append(card)
        return changed

    def _market_is_open(self) -> bool:
        """Overridable hook -- production wiring supplies the app's existing
        US-market-session calculation (already implemented for the market
        status widget per project memory); defaults to always-open so unit
        tests can drive this deterministically without a real calendar.
        """
        return True

    # -- EOD cleanup (review finding P1-14) -------------------------------

    def _run_eod_cleanup_if_due(self, cards: List[TradeCardState]) -> List[TradeCardState]:
        """The EOD service (section 13) previously existed only as a module
        nothing ever called. Runs on every heartbeat tick once
        ``_eod_window_reached`` says we're within
        ``EOD_ENTRY_CLEANUP_SECONDS_BEFORE_CLOSE`` of the close -- safe to
        call repeatedly since each stage inside it is itself idempotent
        (a card that has already moved off BUY_TODAY/ENTRY_PENDING/an
        incomplete-target OPEN_POSITION simply no longer matches any of its
        conditions).
        """
        if self._eod_service is None:
            return []
        if not self._eod_window_reached():
            return []
        return self._eod_service.run_eod_cleanup(cards)

    def _eod_window_reached(self) -> bool:
        """Overridable hook -- production wiring supplies real
        seconds-to-close using the app's existing market-session
        calculation, comparing against
        ``EOD_ENTRY_CLEANUP_SECONDS_BEFORE_CLOSE``. Defaults to False so
        tests never trigger EOD cleanup unless they explicitly opt in.
        """
        return False

    # -- Stale-quote protection for existing positions (review finding P1-6) --

    def _detect_stale_position_quotes(self, cards: List[TradeCardState]) -> List[TradeCardState]:
        """New entries already refuse to fire on stale/missing data
        (``_evaluate_buy_today``). An *existing* open position needs the
        same visibility even though it doesn't block anything by itself --
        the position stays protected by whatever the last good quote/REST
        poll produced, but the card is flagged immediately so the failure
        is visible rather than silent (section 826-833).
        """
        changed: List[TradeCardState] = []
        for card in cards:
            if card.board_status not in _TICK_REACTIVE_POSITION_STATUSES:
                continue
            quote = self._market_data.latest_quote(card.symbol)
            stale = not self._market_data.is_connected() or is_quote_stale(quote)
            already_flagged = _DATA_STALE_WARNING in card.warnings
            if stale and not already_flagged:
                card.warnings = [*card.warnings, _DATA_STALE_WARNING]
                changed.append(card)
            elif not stale and already_flagged:
                self._clear_stale_warning(card)
                changed.append(card)
        return changed

    @staticmethod
    def _find_card(
        cards: List[TradeCardState],
        *,
        environment: Optional[str],
        account_no: Optional[str],
        symbol: str,
    ) -> Optional[TradeCardState]:
        symbol = symbol.upper()
        for card in cards:
            if card.symbol != symbol:
                continue
            if environment is not None and card.environment != environment:
                continue
            if account_no is not None and card.account_no != account_no:
                continue
            return card
        return None
