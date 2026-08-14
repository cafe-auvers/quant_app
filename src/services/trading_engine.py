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
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, List, Optional

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


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class EntryDeadlineLookup:
    """Section 793-799: how the engine finds and refreshes the working
    entry order for an ENTRY_PENDING card. Both are injected -- this module
    never queries the order ledger or KIS directly.
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
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._entry_attempt_manager = entry_attempt_manager
        self._position_manager = position_manager
        self._market_data = market_data
        self._position_callbacks = position_callbacks
        self._entry_deadline_lookup = entry_deadline_lookup
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

        if card.exit_all_required != was_exit_required or card.board_status != was_board_status:
            return [card]
        return []

    # --- Heartbeat (section 789-799) -----------------------------------

    def run_heartbeat(self, cards: List[TradeCardState]) -> List[TradeCardState]:
        if not self.is_enabled():
            return []
        changed: List[TradeCardState] = []
        changed.extend(self._evaluate_buy_today(cards))
        changed.extend(self._check_entry_attempt_deadlines(cards))
        changed.extend(self._process_queued_market_open_sells(cards))
        changed.extend(self._retry_incomplete_sell_alls(cards))
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

    # -- BUY_TODAY -> entry attempts -------------------------------------

    def _evaluate_buy_today(self, cards: List[TradeCardState]) -> List[TradeCardState]:
        triggers: List[EntryTrigger] = []
        trigger_cards: dict[str, TradeCardState] = {}
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
            price = card.entry_trigger or card.breakout_price
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
            elif result.outcome == AttemptOutcome.SYMBOL_LOCKED:
                continue  # transient, another in-process attempt is running
            else:
                card.entry_runtime_status = _OUTCOME_TO_ENTRY_RUNTIME_STATUS.get(
                    result.outcome, card.entry_runtime_status
                )
            changed.append(card)
        return changed

    # -- ENTRY_PENDING -> deadline resolution ----------------------------

    def _check_entry_attempt_deadlines(self, cards: List[TradeCardState]) -> List[TradeCardState]:
        changed: List[TradeCardState] = []
        now = self._clock()
        for card in cards:
            if card.board_status != BoardStatus.ENTRY_PENDING:
                continue
            order = self._entry_deadline_lookup.find_open_entry_order(card)
            if order is None:
                continue

            # A user-initiated CancelEntry (section 298-304) resolves the
            # order right away rather than waiting for the 15s attempt
            # deadline -- the flag is set by
            # src.ui.buyboard.controller._apply_cancel_entry, which
            # deliberately leaves board_status at ENTRY_PENDING until this
            # engine actually cancels/reconciles the order below.
            cancel_requested = card.entry_block_reason == "cancel_requested"

            deadline_passed = False
            if order.attempt_deadline_at:
                try:
                    deadline = datetime.fromisoformat(order.attempt_deadline_at)
                except ValueError:
                    deadline = None
                if deadline is not None:
                    if deadline.tzinfo is None:
                        deadline = deadline.replace(tzinfo=timezone.utc)
                    deadline_passed = now >= deadline

            if not deadline_passed and not cancel_requested:
                continue

            refreshed = self._entry_deadline_lookup.reconcile_order(order)
            action = self._entry_attempt_manager.handle_attempt_deadline(
                refreshed, cancel_order=lambda o: self._position_callbacks.cancel_order(o.client_order_id)
            )
            self._apply_deadline_action(card, refreshed, action, cancel_requested=cancel_requested)
            changed.append(card)
        return changed

    def _apply_deadline_action(
        self,
        card: TradeCardState,
        order: BrokerOrder,
        action: AttemptDeadlineAction,
        *,
        cancel_requested: bool = False,
    ) -> None:
        if action == AttemptDeadlineAction.MOVE_TO_OPEN_POSITION:
            card.board_status = BoardStatus.OPEN_POSITION
            card.broker_quantity = order.filled_quantity
            card.orderable_quantity = order.filled_quantity
            card.average_entry_price = order.avg_fill_price
            card.entry_remaining_target_quantity = 0
            card.position_runtime_status = PositionRuntimeStatus.OPEN
            self._position_manager.apply_first_fill_stop(
                card,
                entry_orb_low=card.entry_orb_low or 0.0,
                entry_orb_window=card.entry_orb_window or card.selected_orb_window or "",
            )
            card.entry_runtime_status = None
            card.entry_block_reason = ""
        elif action == AttemptDeadlineAction.CANCEL_REMAINDER_AND_RETRY:
            card.board_status = BoardStatus.OPEN_POSITION
            card.broker_quantity = order.filled_quantity
            card.orderable_quantity = order.filled_quantity
            card.average_entry_price = order.avg_fill_price
            self._position_manager.apply_first_fill_stop(
                card,
                entry_orb_low=card.entry_orb_low or 0.0,
                entry_orb_window=card.entry_orb_window or card.selected_orb_window or "",
            )
            if cancel_requested:
                # The user cancelled -- keep the filled shares (they're
                # real, already protected by the stop above) but stop
                # attempting to complete the target, same as the EOD
                # "incomplete target" handling.
                card.entry_remaining_target_quantity = 0
                card.position_runtime_status = PositionRuntimeStatus.OPEN
                card.entry_block_reason = ""
            else:
                card.entry_remaining_target_quantity = max(
                    0, card.target_position_quantity - order.filled_quantity
                )
                card.position_runtime_status = PositionRuntimeStatus.ENTRY_COMPLETING
            card.entry_runtime_status = None
        elif action in (
            AttemptDeadlineAction.CANCEL_AND_RETRY_AFTER_COOLDOWN,
            AttemptDeadlineAction.RELEASE_AND_RETRY_AFTER_COOLDOWN,
        ):
            if cancel_requested:
                # A user-initiated cancel resolves to Buylist directly --
                # there is nothing to "retry", the user asked to stop.
                card.board_status = BoardStatus.BUYLIST
                card.entry_runtime_status = None
                card.entry_block_reason = ""
            else:
                card.board_status = BoardStatus.BUY_TODAY
                card.entry_runtime_status = EntryRuntimeStatus.RETRY_COOLDOWN
        elif action == AttemptDeadlineAction.BLOCK_SYMBOL_PENDING_RECONCILIATION:
            card.entry_runtime_status = EntryRuntimeStatus.ORDER_PENDING
            # entry_block_reason ("cancel_requested", if set) is left as-is
            # so the next heartbeat retries resolving/cancelling this order
            # -- section 526-529's "do not assume cancellation" applies
            # equally to a user-requested cancel.

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

    # -- Sell All reprice/retry until flat (section 707-709) -------------

    def _retry_incomplete_sell_alls(self, cards: List[TradeCardState]) -> List[TradeCardState]:
        """Section 707-709: "Reprice/retry until flat" / "Move to Closed
        only when broker confirms zero." A SELL_ALL card with no order
        currently working (the previous attempt finished -- filled,
        partially filled, cancelled, or rejected) either still holds shares
        (resubmit for the remainder) or is now flat (close it). A card with
        a live working sell order is left alone this tick.
        """
        changed: List[TradeCardState] = []
        for card in cards:
            if card.board_status != BoardStatus.SELL_ALL or card.sell_all_at_market_open:
                continue
            if self._position_callbacks.find_open_sell_order(card) is not None:
                continue  # already retrying/working
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
            if remaining == card.broker_quantity:
                continue  # nothing new to report this tick
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
