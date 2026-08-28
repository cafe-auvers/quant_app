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
- P0-6/P0-7: any working entry order is reconciled every heartbeat tick,
  fills are protected the instant they're observed, and a cancel request
  is never treated as a completed cancellation until the broker confirms
  it.
- Entry submissions and completion attempts use the ORB-high trigger as a
  resting limit; when the high is between legal U.S. price ticks, the order
  uses the first legal tick above it. A live quote never raises that price.
- P1-6: an existing position whose feed disconnects or loses its structural
  subscription health is flagged independently of the new-entry freshness
  gate.
- P1-14: the EOD cleanup service is actually invoked from the heartbeat
  (gated on an injectable "are we in the EOD window" hook) instead of
  existing only as a module nothing ever called.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional

from src.core import execution_config
from src.core.execution_config import is_buyboard_engine_enabled
from src.core.execution_result import UnifiedExecutionStatus
from src.core.order_state import BrokerOrder, OrderIntent
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
    EntryCancelReason,
    EntryTrigger,
)
from src.services.execution_command_gateway import (
    AmbiguousPostBrokerPersistenceError,
    GuardedCancellationRejectedError,
    GuardedSubmissionAmbiguousError,
)
from src.services.execution_command_repository import DuplicateCommandError
from src.services.eod_trading_service import EodTradingService
from src.services.intraday_data_service import ExecutionGradeDataUnavailableError
from src.services.position_manager import (
    BrokerHolding,
    PositionActionCallbacks,
    PositionManager,
)
from src.services.realtime_market_data import (
    QuoteSnapshot,
    RealtimeMarketDataService,
)

logger = logging.getLogger(__name__)

# board_status values whose card should react to a live price tick.
_TICK_REACTIVE_POSITION_STATUSES = {BoardStatus.OPEN_POSITION, BoardStatus.PARTIAL_SELL}

_OUTCOME_TO_ENTRY_RUNTIME_STATUS = {
    AttemptOutcome.WAITING_FOR_CAPITAL: EntryRuntimeStatus.WAITING_FOR_CAPITAL,
    AttemptOutcome.COOLDOWN: EntryRuntimeStatus.RETRY_COOLDOWN,
    AttemptOutcome.RATE_LIMITED: EntryRuntimeStatus.RETRY_COOLDOWN,
    AttemptOutcome.REJECTED: EntryRuntimeStatus.RETRY_COOLDOWN,
    AttemptOutcome.BROKER_ROUTING_REJECTED: EntryRuntimeStatus.RETRY_COOLDOWN,
    AttemptOutcome.DUPLICATE_ORDER: EntryRuntimeStatus.ORDER_PENDING,
    AttemptOutcome.UNRESOLVED: EntryRuntimeStatus.ORDER_PENDING,
}

_DATA_STALE_WARNING = "DATA_STALE"
_OUTAGE_HIGH_WARNING = "MARKET_DATA_OUTAGE_HIGH"
_OUTAGE_LOW_WARNING = "MARKET_DATA_OUTAGE_LOW"
_TRADING_HALT_EXIT_WARNING = "TRADING_HALT_EXIT_PENDING"
# Section 5's PARTIAL_EXIT_ATTEMPT_TTL_SECONDS / SELL_ALL_ATTEMPT_TTL_SECONDS /
# EXIT_CANCEL_CONFIRMATION_TIMEOUT_SECONDS: surfaced on the card the same way
# _DATA_STALE_WARNING already is, so a liquidation cancel that the broker
# will not confirm is visible on the board, not just in the log.
_EXIT_CANCEL_STALLED_WARNING = "EXIT_CANCEL_STALLED"
_SUPPORTED_ORB_WINDOWS = {"1m", "5m", "30m"}
_LIVE_ORB_SELECTION_STATUSES = {
    EntryRuntimeStatus.ORB_FORMING,
    EntryRuntimeStatus.WAITING_BREAKOUT,
    EntryRuntimeStatus.ARMED,
    EntryRuntimeStatus.EXECUTE_READY,
    EntryRuntimeStatus.DATA_UNAVAILABLE,
}


def _complete_orb_entry_plan(card: TradeCardState) -> bool:
    """Return whether a card contains executable, risk-sized ORB geometry.

    A daily breakout/allocation is planning intent, not an entry plan.  This
    boundary deliberately requires the fields copied from a verified ORB
    candidate before either quote recovery or a live crossing can arm a BUY.
    """

    window = str(card.selected_orb_window or card.entry_orb_window or "").strip()
    try:
        trigger = float(card.entry_trigger or 0.0)
        orb_high = float(card.entry_orb_high or 0.0)
        orb_low = float(card.entry_orb_low or 0.0)
        breakout = float(card.breakout_price or 0.0)
        buffer_pct = float(card.buffer_pct)
        quantity = int(card.planned_quantity or 0)
    except (TypeError, ValueError, OverflowError):
        return False
    if not all(
        math.isfinite(value)
        for value in (trigger, orb_high, orb_low, breakout, buffer_pct)
    ):
        return False
    buffered_breakout = breakout * (1.0 + buffer_pct)
    return bool(
        window in _SUPPORTED_ORB_WINDOWS
        and trigger > 0
        and orb_high > 0
        and orb_low > 0
        and breakout > 0
        and 0.0 <= buffer_pct <= 1.0
        and math.isclose(trigger, orb_high, rel_tol=1e-9, abs_tol=1e-6)
        and orb_high > buffered_breakout
        and orb_low < trigger
        and orb_low <= orb_high
        and quantity > 0
    )


def classify_market_data_outage_risk(
    card: TradeCardState,
    *,
    trusted_price: float,
    account_equity: float = 0.0,
    bid: Optional[float] = None,
    ask: Optional[float] = None,
    liquidity_tier: str = "",
) -> execution_config.MarketDataOutageRiskTier:
    """Classify once from the last trusted observation, then freeze it."""
    price = max(0.0, float(trusted_price or 0.0))
    quantity = max(0, int(card.broker_quantity or 0))
    stop = max(0.0, float(card.active_stop_price or 0.0))
    average = max(0.0, float(card.average_entry_price or 0.0))
    if price <= 0:
        return execution_config.MarketDataOutageRiskTier.HIGH
    if card.exit_all_required or card.board_status in {
        BoardStatus.PARTIAL_SELL,
        BoardStatus.SELL_ALL,
    }:
        return execution_config.MarketDataOutageRiskTier.HIGH
    if stop and price <= stop * (
        1.0 + execution_config.MARKET_DATA_OUTAGE_RISK_BUFFER_PCT
    ):
        return execution_config.MarketDataOutageRiskTier.HIGH
    if average and price < average:
        loss_pct = (average - price) / average
        if loss_pct >= execution_config.MARKET_DATA_OUTAGE_LOSS_THRESHOLD_PCT:
            return execution_config.MarketDataOutageRiskTier.HIGH
    if account_equity > 0 and quantity > 0:
        concentration = price * quantity / account_equity
        risk_to_stop = max(0.0, average - stop) * quantity / account_equity
        if (
            concentration >= execution_config.MARKET_DATA_OUTAGE_CONCENTRATION_PCT
            or risk_to_stop >= execution_config.MARKET_DATA_OUTAGE_ACCOUNT_RISK_PCT
        ):
            return execution_config.MarketDataOutageRiskTier.HIGH
    # ``card.stop_adr`` is a percentage of ADR, not a dollar ATR value.  The
    # frozen entry geometry lets us recover the original dollar ADR without
    # introducing mutable market history into an outage decision:
    #
    #   stop_adr = (entry - ORB low) / ADR dollars * 100
    #
    # If an older/manual card lacks that geometry, omit this optional signal;
    # the stop buffer, loss, concentration, account-risk, and liquidity checks
    # above remain active.
    entry = float(card.entry_trigger or 0.0)
    entry_stop = float(card.entry_orb_low or 0.0)
    stop_adr_percent = float(card.stop_adr or 0.0)
    adr_price = (
        (entry - entry_stop) * 100.0 / stop_adr_percent
        if (
            math.isfinite(entry)
            and math.isfinite(entry_stop)
            and math.isfinite(stop_adr_percent)
            and entry > entry_stop > 0.0
            and stop_adr_percent > 0.0
        )
        else 0.0
    )
    if stop and adr_price > 0.0:
        distance_in_atr = max(0.0, price - stop) / adr_price
        if distance_in_atr <= execution_config.MARKET_DATA_OUTAGE_STOP_DISTANCE_ATR:
            return execution_config.MarketDataOutageRiskTier.HIGH
    if str(liquidity_tier or "").upper() in {"ILLIQUID", "HIGH_SPREAD"}:
        return execution_config.MarketDataOutageRiskTier.HIGH
    if bid and ask and bid > 0 and (ask - bid) / bid >= 0.02:
        return execution_config.MarketDataOutageRiskTier.HIGH
    return execution_config.MarketDataOutageRiskTier.LOW


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
    # Review finding P0-5: the broker-truth source of a card's *cumulative*
    # position (across every entry attempt, not just the most recent
    # order), used by _protect_new_fill so a second/third attempt's fill is
    # added to the position instead of compared against or overwriting the
    # total from a previous attempt. Optional -- when not supplied (e.g. a
    # test double, or a caller that hasn't wired it), _protect_new_fill
    # falls back to an order-scoped delta accumulation.
    refresh_broker_position: Optional[
        Callable[[TradeCardState], Optional[BrokerHolding]]
    ] = None
    # Persists an order mutated in-process (currently only
    # ``order.applied_filled_quantity``, written by the P0-5 fallback fill
    # accounting below) back to the durable order ledger. Without this, a
    # mutation made here would be silently lost the moment the next tick
    # re-fetches the order fresh, causing the same delta to be re-applied
    # (double-counted) on every subsequent tick. Optional -- unused
    # whenever ``refresh_broker_position`` is wired (the preferred path,
    # which never touches ``applied_filled_quantity``).
    persist_order: Optional[Callable[[BrokerOrder], None]] = None


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
        market_is_open: Optional[Callable[[], bool]] = None,
        eod_window_reached: Optional[Callable[[], bool]] = None,
        prepare_entry_attempt: Optional[Callable[[TradeCardState], None]] = None,
        account_equity_provider: Optional[Callable[[str, str], float]] = None,
        broader_market_risk_signal: Optional[Callable[[TradeCardState], bool]] = None,
        liquidity_tier_lookup: Optional[
            Callable[[TradeCardState, Optional[QuoteSnapshot]], str]
        ] = None,
        unattended_session: Optional[Callable[[], bool]] = None,
        trading_halt_lookup: Optional[Callable[[str], bool]] = None,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._entry_attempt_manager = entry_attempt_manager
        self._position_manager = position_manager
        self._market_data = market_data
        self._position_callbacks = position_callbacks
        self._entry_deadline_lookup = entry_deadline_lookup
        self._eod_service = eod_service
        # Review finding P0-8: these were previously hardcoded
        # always-True/always-False methods with no way for a production
        # caller to override them (nothing subclasses TradingEngine), so
        # premarket Sell All queuing, EOD cleanup, and any other
        # session-aware gate silently never behaved correctly outside
        # tests. Defaults preserve the old test-friendly behavior for any
        # caller that doesn't supply one.
        self._market_is_open_fn = market_is_open or (lambda: True)
        self._eod_window_reached_fn = eod_window_reached or (lambda: False)
        self._prepare_entry_attempt = prepare_entry_attempt or (lambda card: None)
        self._account_equity_provider = account_equity_provider or (
            lambda environment, account_no: 0.0
        )
        self._broader_market_risk_signal = broader_market_risk_signal or (
            lambda card: False
        )
        self._liquidity_tier_lookup = liquidity_tier_lookup or (
            lambda card, quote: ""
        )
        self._unattended_session = unattended_session or (lambda: True)
        self._trading_halt_lookup = trading_halt_lookup or (lambda symbol: False)
        self._clock = clock

    @staticmethod
    def is_enabled() -> bool:
        return is_buyboard_engine_enabled()

    # --- Every market-data tick (section 766-770, 784-788) ------------

    def evaluate_quote(
        self, cards: List[TradeCardState], quote: QuoteSnapshot
    ) -> List[TradeCardState]:
        """Update price and evaluate the stop immediately for every card
        matching ``quote.symbol`` (review finding P1-6: the same symbol can
        be held in more than one account, and a single quote tick must
        evaluate the stop against all of them, not just the first match).
        Entry-trigger re-evaluation off a tick is intentionally *not* here
        -- ORB candidate recomputation (unchanged, per section 15) happens
        on the caller's own cadence and feeds
        ``card.entry_runtime_status``/``entry_trigger`` before
        ``run_heartbeat`` looks at BUY_TODAY cards.
        """
        if not self.is_enabled():
            return []
        if not quote.regular_session:
            return []
        symbol = quote.symbol.upper()
        matching = [
            card
            for card in cards
            if card.symbol == symbol and card.board_status in _TICK_REACTIVE_POSITION_STATUSES
        ]
        changed: List[TradeCardState] = []
        for card in matching:
            try:
                if self._evaluate_quote_for_card(card, quote):
                    changed.append(card)
            except Exception:
                # Review finding P1-5: one account's card failing to
                # process a tick (a broker error while cancelling a working
                # order, say) must not stop the same tick from reaching
                # every other card holding this symbol.
                logger.exception(
                    "evaluate_quote failed for %s (%s:%s)", card.symbol, card.environment, card.account_no
                )
        return changed

    def evaluate_entry_quote(
        self,
        cards: List[TradeCardState],
        quote: QuoteSnapshot,
        *,
        prepare_entry_plan: Optional[
            Callable[[TradeCardState, QuoteSnapshot], bool]
        ] = None,
    ) -> List[TradeCardState]:
        """Consume one representative trade event for BUY_TODAY entries.

        The market-data accumulator can coalesce a busy drain window, but it
        emits the actual min/max trade snapshots.  Entry decisions must use
        those event prices directly rather than consulting only the cache's
        final trade, otherwise a 100 -> 105 -> 101 breakout path disappears.
        Callers pass only mutation-ready cards; this method does not widen
        reconciliation or ownership authorization.
        """
        now = self._clock()
        if (
            not self.is_enabled()
            or not quote.regular_session
            or not quote.entry_trigger_eligible
            or not quote.is_execution_fresh(now=now)
        ):
            return []
        symbol = quote.symbol.upper()
        matching = [
            card
            for card in cards
            if card.symbol == symbol and card.board_status == BoardStatus.BUY_TODAY
        ]
        if not matching:
            return []
        prepared: List[TradeCardState] = []
        if prepare_entry_plan is not None:
            for card in matching:
                # A live event may choose among current ORB windows, but it
                # must never erase an attempt manager's cooldown/capital/
                # order state and thereby manufacture an early retry.
                if card.entry_runtime_status not in _LIVE_ORB_SELECTION_STATUSES:
                    continue
                try:
                    if prepare_entry_plan(card, quote):
                        prepared.append(card)
                except Exception:
                    logger.exception(
                        "Live ORB candidate selection failed for %s (%s:%s)",
                        card.symbol,
                        card.environment,
                        card.account_no,
                    )
        promoted: List[TradeCardState] = []
        for card in matching:
            if card.entry_runtime_status not in {
                EntryRuntimeStatus.WAITING_BREAKOUT,
                EntryRuntimeStatus.ARMED,
            }:
                continue
            if not _complete_orb_entry_plan(card):
                continue
            if quote.last_price < float(card.entry_trigger):
                continue
            card.entry_runtime_status = EntryRuntimeStatus.EXECUTE_READY
            card.entry_block_reason = ""
            promoted.append(card)

        evaluated = self._evaluate_buy_today(
            matching,
            quote_overrides={symbol: quote},
        )
        changed: List[TradeCardState] = []
        seen: set[int] = set()
        for card in [*prepared, *promoted, *evaluated]:
            if id(card) in seen:
                continue
            seen.add(id(card))
            changed.append(card)
        return changed

    def evaluate_pending_stop_handoff(
        self, cards: List[TradeCardState], quote: QuoteSnapshot
    ) -> List[TradeCardState]:
        """Evaluate the narrow request-to-feed-lock window safely.

        A trade received after the durable UI request but just before the
        feed lock installed the tighter rule is detached with the old stop.
        It is evaluated against that old generation first, then against the
        requested protection.  A trade received after installation already
        carries the new override, so sticky liquidation makes this check a
        no-op and the event can trigger at most once.
        """

        if not self.is_enabled() or not quote.regular_session:
            return []
        changed: List[TradeCardState] = []
        for card in cards:
            requested_at = card.pending_stop_requested_at
            pending_price = float(card.pending_stop_price or 0.0)
            if (
                card.symbol != quote.symbol.upper()
                or card.board_status not in _TICK_REACTIVE_POSITION_STATUSES
                or not card.pending_stop_command_id
                or requested_at is None
                or quote.received_at < requested_at
                or pending_price <= 0
                or quote.last_price > pending_price
                or card.exit_all_required
            ):
                continue
            old_active = card.active_stop_price
            without_card_override = replace(
                quote,
                stop_price_overrides=tuple(
                    item
                    for item in quote.stop_price_overrides
                    if item[0] != card.card_key
                ),
            )
            try:
                card.active_stop_price = pending_price
                if self._evaluate_quote_for_card(card, without_card_override):
                    changed.append(card)
            finally:
                card.active_stop_price = old_active
        return changed

    def _evaluate_quote_for_card(self, card: TradeCardState, quote: QuoteSnapshot) -> bool:
        was_exit_required = card.exit_all_required
        was_board_status = card.board_status
        was_warnings = list(card.warnings)

        self._clear_stale_warning(card)
        stop_override = dict(quote.stop_price_overrides).get(card.card_key)
        if stop_override is None:
            self._position_manager.evaluate_tick(card, quote.last_price)
        else:
            # A stop-version rotation detached this event under the shared
            # market-data lock. Evaluate it against that exact old stop,
            # even if the freshly loaded card already carries the new stop.
            current_stop = card.active_stop_price
            try:
                card.active_stop_price = stop_override
                self._position_manager.evaluate_tick(card, quote.last_price)
            finally:
                card.active_stop_price = current_stop
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
                self._initiate_sell_all(card)

        return (
            card.exit_all_required != was_exit_required
            or card.board_status != was_board_status
            or card.warnings != was_warnings
        )

    def _clear_stale_warning(self, card: TradeCardState) -> None:
        if _DATA_STALE_WARNING in card.warnings:
            card.warnings = [w for w in card.warnings if w != _DATA_STALE_WARNING]

    @staticmethod
    def _clear_market_data_outage_warnings(card: TradeCardState) -> None:
        outage_warnings = {_OUTAGE_HIGH_WARNING, _OUTAGE_LOW_WARNING}
        card.warnings = [
            warning for warning in card.warnings if warning not in outage_warnings
        ]

    def _entry_conflict_clear_for_liquidation(
        self, card: TradeCardState
    ) -> tuple[bool, bool]:
        """Cancel and track any completion BUY before a liquidation SELL.

        Returns ``(clear_to_sell, card_changed)``.  The in-flight marker is
        deliberately a hard fence even if a transient lookup returns no
        order; broker-confirmed terminal reconciliation owns clearing it.
        """
        changed = False
        if card.entry_remaining_target_quantity:
            card.entry_remaining_target_quantity = 0
            changed = True
        working_buy = self._entry_deadline_lookup.find_open_entry_order(card)
        if working_buy is not None:
            if not card.entry_cancel_in_flight:
                card.entry_cancel_in_flight = True
                card.entry_cancel_reason = EntryCancelReason.EXIT_ALL.value
                changed = True
                self._position_callbacks.request_cancel(
                    card, working_buy.client_order_id, scope="ENTRY"
                )
            return False, changed
        if card.entry_cancel_in_flight or card.entry_submission_unresolved:
            return False, changed
        return True, changed

    def _initiate_sell_all(
        self, card: TradeCardState, *, queue_for_market_open: bool = False
    ) -> bool:
        """One initiation path for stop, user, and feed-outage liquidation."""
        before = (
            card.board_status,
            card.position_runtime_status,
            card.exit_all_required,
            card.sell_all_at_market_open,
            card.entry_remaining_target_quantity,
            card.entry_cancel_in_flight,
            card.entry_cancel_reason,
        )
        self._position_manager.start_sell_all(
            card, callbacks=self._position_callbacks
        )
        if queue_for_market_open:
            self._position_manager.queue_sell_all_at_market_open(card)
        self._entry_conflict_clear_for_liquidation(card)
        after = (
            card.board_status,
            card.position_runtime_status,
            card.exit_all_required,
            card.sell_all_at_market_open,
            card.entry_remaining_target_quantity,
            card.entry_cancel_in_flight,
            card.entry_cancel_reason,
        )
        return before != after

    # --- Heartbeat (section 789-799) -----------------------------------

    def run_heartbeat(self, cards: List[TradeCardState]) -> List[TradeCardState]:
        if not self.is_enabled():
            return []
        changed: List[TradeCardState] = []
        # Review finding P1-5: every stage already isolates exceptions
        # per-card internally; this outer guard is defense in depth so an
        # unexpected failure *between* per-card iterations (e.g. inside a
        # batch call like process_triggers, or a bug in a stage itself)
        # only skips that one stage for this tick instead of aborting every
        # later stage -- EOD cleanup, stale-quote flagging, Sell All
        # retries -- for every other card too.
        for stage in (
            self._recover_retryable_cards,
            self._evaluate_buy_today,
            self._reconcile_entry_orders,
            self._process_entry_completion,
            self._process_partial_sell_requests,
            self._reconcile_partial_sell_fills,
            self._process_queued_market_open_sells,
            self._reconcile_sell_all_orders,
            self._retry_incomplete_sell_alls,
            self._detect_stale_position_quotes,
            self._run_eod_cleanup_if_due,
        ):
            try:
                changed.extend(stage(cards))
            except Exception:
                logger.exception("Buyboard heartbeat stage %s failed", getattr(stage, "__name__", stage))
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
            try:
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
                    if (
                        _complete_orb_entry_plan(card)
                        and self._market_data.entry_quote_ready(card.symbol, now=now)
                    ):
                        card.entry_runtime_status = EntryRuntimeStatus.EXECUTE_READY
                        card.entry_block_reason = ""
                        changed.append(card)
            except Exception:
                # Review finding P1-5: one problematic card must never stop
                # the rest of this stage (let alone the whole heartbeat)
                # from processing every other symbol.
                logger.exception("_recover_retryable_cards failed for %s", card.symbol)
        return changed

    # -- Approved ORB-high price ------------------------------------------

    @staticmethod
    def _orb_high_entry_limit(card: TradeCardState) -> Optional[float]:
        """Return the broker-legal BUY limit at or immediately above ORB high.

        U.S. orders at or above $1 use one-cent ticks; sub-dollar orders use
        four-decimal ticks in the KIS adapter. Rounding a BUY down would put
        the resting order below the strategy's ORB-high price, so the risk
        check and persisted command both receive the first legal tick at or
        above the approved trigger.
        """
        try:
            trigger = Decimal(str(card.entry_trigger or 0.0))
        except (InvalidOperation, TypeError, ValueError, OverflowError):
            return None
        if not trigger.is_finite() or trigger <= 0:
            return None
        tick = Decimal("0.0001") if trigger < Decimal("1") else Decimal("0.01")
        return float(trigger.quantize(tick, rounding=ROUND_CEILING))

    def _target_plan_quantity(
        self, card: TradeCardState, *, entry_price: float
    ) -> int:
        """Return the quantity already approved by the current ORB plan."""

        planned = max(0, int(card.planned_quantity or 0))
        return planned if entry_price > 0 else 0

    def _apply_definitive_entry_rejection(
        self,
        card: TradeCardState,
        *,
        detail: str,
    ) -> None:
        """Retire an entry after the broker definitively declined it.

        Ambiguous submissions keep their durable identity and remain fenced.
        A guarded broker rejection is the opposite: the gateway has confirmed
        that no working order exists, so leaving the card in Entry Pending (or
        retrying the same rejected security every cooldown) is incorrect.
        """

        card.entry_runtime_status = None
        card.entry_block_reason = ""
        card.next_retry_at = None
        card.entry_attempt_group_id = ""
        card.entry_attempt_count = 0
        card.entry_client_order_id = ""
        card.entry_pending_attempt_number = 0
        card.entry_submission_unresolved = False
        card.entry_cancel_in_flight = False
        card.entry_cancel_reason = ""
        card.entry_cancel_command_id = ""
        card.entry_remaining_target_quantity = 0
        card.capital_reservation_id = ""
        self._entry_attempt_manager.reset_symbol(
            card.environment,
            card.account_no,
            card.symbol,
        )

        if card.broker_quantity > 0:
            card.board_status = BoardStatus.OPEN_POSITION
            if not card.exit_all_required:
                card.position_runtime_status = PositionRuntimeStatus.OPEN
            return

        card.board_status = BoardStatus.BUYLIST
        card.session_date = None
        card.buylist_member = True
        card.buy_today_note = (
            f"Entry rejected by broker: {detail}" if detail else "Entry rejected by broker"
        )
        card.selected_orb_window = None
        card.position_percent = 0.0
        card.planned_quantity = 0
        card.target_position_quantity = 0
        card.entry_orb_window = None
        card.entry_orb_high = None
        card.entry_orb_low = None
        card.entry_trigger = None
        card.stop_adr = None

    # -- BUY_TODAY -> entry attempts -------------------------------------

    def _evaluate_buy_today(
        self,
        cards: List[TradeCardState],
        *,
        quote_overrides: Optional[Dict[str, QuoteSnapshot]] = None,
    ) -> List[TradeCardState]:
        triggers: List[EntryTrigger] = []
        trigger_cards: Dict[str, TradeCardState] = {}
        changed: List[TradeCardState] = []
        now = self._clock()

        # Review finding P0-8: entries had no explicit market-session gate
        # at all -- a WAITING_FOR_CAPITAL/cooldown card could otherwise fire
        # a brand-new attempt outside regular trading hours the instant it
        # became EXECUTE_READY again.
        if not self._market_is_open():
            return changed

        for card in cards:
            if card.board_status != BoardStatus.BUY_TODAY:
                continue
            if card.entry_runtime_status not in {
                EntryRuntimeStatus.WAITING_BREAKOUT,
                EntryRuntimeStatus.ARMED,
                EntryRuntimeStatus.EXECUTE_READY,
            }:
                continue
            try:
                if not _complete_orb_entry_plan(card):
                    card.entry_runtime_status = EntryRuntimeStatus.ORB_FORMING
                    card.entry_block_reason = (
                        "A complete current-session ORB entry plan is required"
                    )
                    if card not in changed:
                        changed.append(card)
                    continue
                # Section 827-832 step 2: "WebSocket disconnects... block new
                # entries" -- checked independently of quote staleness so a
                # disconnect blocks entries immediately, not only once the
                # last cached quote ages past QUOTE_STALE_AFTER_SECONDS.
                quote = (quote_overrides or {}).get(card.symbol)
                if quote is None:
                    quote = self._market_data.latest_quote(card.symbol)
                if not self._market_data.entry_quote_ready(card.symbol, now=now):
                    # Section 826, 839: a stale/missing execution-grade quote
                    # blocks the attempt outright -- never guess with the
                    # last known price.
                    reason = (
                        "Fresh KIS WebSocket trade and quote events are required "
                        "before an automatic entry"
                    )
                    if (
                        card.entry_runtime_status != EntryRuntimeStatus.DATA_UNAVAILABLE
                        or card.entry_block_reason != reason
                    ):
                        card.entry_runtime_status = EntryRuntimeStatus.DATA_UNAVAILABLE
                        card.entry_block_reason = reason
                        changed.append(card)
                    continue
                price = self._orb_high_entry_limit(card)
                if not price:
                    continue
                planned_quantity = self._target_plan_quantity(
                    card, entry_price=price
                )
                if planned_quantity <= 0:
                    continue
                if card not in changed:
                    changed.append(card)
                self._prepare_entry_attempt(card)
                key = f"{card.environment}:{card.account_no}:{card.symbol}"
                trigger_cards[key] = card
                triggers.append(
                    EntryTrigger(
                        environment=card.environment,
                        account_no=card.account_no,
                        symbol=card.symbol,
                        trigger_at=now,
                        kanban_priority=card.kanban_priority,
                        quantity=planned_quantity,
                        limit_price=price,
                        notional=planned_quantity * price,
                        attempt_group_id=card.entry_attempt_group_id,
                        attempt_number=card.entry_pending_attempt_number,
                        client_order_id=card.entry_client_order_id,
                    )
                )
            except Exception:
                # Review finding P1-5: an error building this symbol's
                # trigger must not block every other symbol's entry.
                logger.exception("_evaluate_buy_today failed to build a trigger for %s", card.symbol)

        if not triggers:
            return changed

        results = self._entry_attempt_manager.process_triggers(triggers)
        for result in results:
            key = f"{result.trigger.environment}:{result.trigger.account_no}:{result.trigger.symbol}"
            card = trigger_cards.get(key)
            if card is None:
                continue
            try:
                if result.outcome in (
                    AttemptOutcome.SUBMITTED,
                    AttemptOutcome.DUPLICATE_ORDER,
                    AttemptOutcome.UNRESOLVED,
                ):
                    card.board_status = BoardStatus.ENTRY_PENDING
                    card.entry_runtime_status = EntryRuntimeStatus.ORDER_PENDING
                    card.entry_attempt_group_id = result.attempt_group_id
                    card.entry_attempt_count = result.attempt_count
                    card.entry_submission_unresolved = (
                        result.outcome != AttemptOutcome.SUBMITTED
                    )
                    if result.outcome == AttemptOutcome.SUBMITTED:
                        card.entry_block_reason = ""
                    if result.submission is not None:
                        card.entry_client_order_id = result.submission.client_order_id
                        card.capital_reservation_id = result.submission.capital_reservation_id
                    card.next_retry_at = None
                elif result.outcome == AttemptOutcome.BROKER_REJECTED:
                    self._apply_definitive_entry_rejection(
                        card,
                        detail=result.detail,
                    )
                elif result.outcome == AttemptOutcome.SYMBOL_LOCKED:
                    continue  # transient, another in-process attempt is running
                else:
                    card.entry_runtime_status = _OUTCOME_TO_ENTRY_RUNTIME_STATUS.get(
                        result.outcome, card.entry_runtime_status
                    )
                    if result.outcome == AttemptOutcome.BROKER_ROUTING_REJECTED:
                        card.entry_block_reason = (
                            "KIS rejected the verified exchange route (APBK0656); "
                            "the plan remains in Buy Today for a corrected retry"
                        )
                        card.buy_today_note = card.entry_block_reason
                    card.entry_attempt_group_id = result.attempt_group_id
                    card.entry_attempt_count = result.attempt_count
                    card.next_retry_at = result.retry_at  # P0-4: persist so a restart doesn't lose it
                    if result.outcome in {
                        AttemptOutcome.REJECTED,
                        AttemptOutcome.BROKER_ROUTING_REJECTED,
                    }:
                        card.entry_client_order_id = ""
                        card.entry_pending_attempt_number = 0
                        card.entry_submission_unresolved = False
                changed.append(card)
            except Exception:
                logger.exception("_evaluate_buy_today failed to apply a result for %s", card.symbol)
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
            # A cancel we requested (e.g. an EXIT_ALL escalation cancelling
            # the working completion-buy from evaluate_quote) may leave
            # board_status/entry_remaining_target_quantity already moved on
            # to something else in the very same tick -- keep tracking the
            # order until the broker actually confirms the cancel so its
            # capital reservation is settled exactly once, instead of
            # silently falling out of scope with the reservation still held.
            or card.entry_cancel_in_flight
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
            try:
                order = self._entry_deadline_lookup.find_open_entry_order(card)
                if order is None:
                    continue
                refreshed = self._entry_deadline_lookup.reconcile_order(order)

                fill_protected = False
                if refreshed.filled_quantity > card.broker_quantity:
                    self._protect_new_fill(card, refreshed)
                    fill_protected = True

                cancel_requested = card.entry_block_reason in (
                    "cancel_requested",
                    "eod_cancel_requested",
                )
                deadline_passed = self._attempt_deadline_passed(refreshed, now)
                already_has_position = card.broker_quantity > 0

                # Review finding P0-6: record *why* a cancel is being
                # escalated the moment it is (not after the broker confirms
                # it) so the terminal branch below knows whether to preserve
                # entry_remaining_target_quantity once the cancel resolves.
                # entry_cancel_in_flight is the guard: only stamp a reason
                # the first tick a cancel is actually requested, never
                # overwrite it on a later tick while still awaiting
                # confirmation of that same cancel.
                if not card.entry_cancel_in_flight and (deadline_passed or cancel_requested):
                    if card.entry_block_reason == "cancel_requested":
                        card.entry_cancel_reason = EntryCancelReason.USER_CANCEL.value
                    elif card.entry_block_reason == "eod_cancel_requested":
                        card.entry_cancel_reason = EntryCancelReason.EOD.value
                    else:
                        card.entry_cancel_reason = EntryCancelReason.TTL_REPRICE.value

                action = self._entry_attempt_manager.resolve_entry_order(
                    refreshed,
                    at_deadline=deadline_passed,
                    cancel_requested=cancel_requested,
                    cancel_order=lambda o: self._position_callbacks.request_cancel(
                        card, o.client_order_id, scope="ENTRY"
                    ),
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
            except Exception:
                # Review finding P1-5: one symbol's reconciliation failure
                # (a broker error, a malformed order) must not stop every
                # other ENTRY_PENDING/entry-completing card from being
                # reconciled this tick.
                logger.exception("_reconcile_entry_orders failed for %s", card.symbol)
        return changed

    @staticmethod
    def _attempt_deadline_passed(order: BrokerOrder, now: datetime) -> bool:
        """Generic ``order.attempt_deadline_at`` check -- shared by entry
        orders (ENTRY_ATTEMPT_TTL_SECONDS) and exit orders
        (PARTIAL_EXIT_ATTEMPT_TTL_SECONDS/SELL_ALL_ATTEMPT_TTL_SECONDS); the
        field itself carries no notion of which TTL produced it, only when
        submission stamped the deadline.
        """
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
        cancel is in flight ("any confirmed fill must create and protect
        the position immediately").

        Review finding P0-5: ``order.filled_quantity`` is scoped to *this*
        order/attempt only. A completion attempt (the second, third, ...
        entry order for the same card, submitted by
        ``_process_entry_completion`` after a first partial fill) starts
        its own ``filled_quantity`` back at 0 -- comparing it directly
        against ``card.broker_quantity`` (the cumulative total) either
        ignores the new fill entirely (while it's still below the running
        total) or, once it does exceed it, *overwrites* the cumulative
        total with just this order's fill, silently discarding every
        earlier attempt's shares. Fixed by preferring a broker-truth
        position refresh (cumulative by construction) when one is wired,
        falling back to an order-scoped delta accumulation otherwise.
        """
        # Captured *before* _apply_cumulative_fill mutates broker_quantity --
        # this, not card.stop_type (which a rediscovered/fixture card can
        # already carry for unrelated reasons), is the reliable signal for
        # "is this the very first fill this card has ever received."
        was_first_fill = card.broker_quantity <= 0
        self._apply_cumulative_fill(card, order)
        remaining_target = max(0, card.target_position_quantity - card.broker_quantity)
        card.entry_remaining_target_quantity = remaining_target
        # A liquidation already in progress for this card (a stop hit while
        # completing the entry, or an explicit Sell All/Partial Sell) owns
        # board_status/position_runtime_status from here on -- a fill
        # protected while that is happening must not resurrect
        # OPEN_POSITION out from under it.
        is_liquidating = card.exit_all_required or card.board_status in (
            BoardStatus.SELL_ALL,
            BoardStatus.PARTIAL_SELL,
        )
        if not is_liquidating:
            card.board_status = BoardStatus.OPEN_POSITION
            card.position_runtime_status = (
                PositionRuntimeStatus.ENTRY_COMPLETING
                if remaining_target > 0
                else PositionRuntimeStatus.OPEN
            )
        if was_first_fill:
            # Set the initial ORB-low stop. A later completion fill must
            # not re-run this (section 620: the entry ORB is frozen at
            # first fill, never recalculated).
            self._position_manager.apply_first_fill_stop(
                card,
                entry_orb_low=card.entry_orb_low or 0.0,
                entry_orb_window=card.entry_orb_window or card.selected_orb_window or "",
            )
        else:
            card.stop_quantity = card.broker_quantity

    def _apply_cumulative_fill(self, card: TradeCardState, order: BrokerOrder) -> None:
        """Updates ``card.broker_quantity``/``average_entry_price`` from
        this fill, preferring broker truth (correct across any number of
        attempts) and falling back to an order-scoped delta accumulation
        with a weighted-average price when no broker-refresh callback is
        wired (review finding P0-5).
        """
        refresher = self._entry_deadline_lookup.refresh_broker_position
        if refresher is not None:
            holding = refresher(card)
            if holding is not None:
                card.broker_quantity = holding.quantity
                card.average_entry_price = holding.average_price
                card.orderable_quantity = holding.quantity
                order.applied_filled_quantity = order.filled_quantity
                return

        delta = max(0, order.filled_quantity - order.applied_filled_quantity)
        if delta > 0:
            prior_cost = card.broker_quantity * card.average_entry_price
            new_cost = delta * float(order.avg_fill_price or 0.0)
            new_quantity = card.broker_quantity + delta
            card.average_entry_price = (
                (prior_cost + new_cost) / new_quantity if new_quantity > 0 else 0.0
            )
            card.broker_quantity = new_quantity
            card.orderable_quantity = new_quantity
            order.applied_filled_quantity = order.filled_quantity
            if self._entry_deadline_lookup.persist_order is not None:
                self._entry_deadline_lookup.persist_order(order)

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
        # cancel_requested itself is not branched on below any more -- P0-6
        # replaced that single boolean with card.entry_cancel_reason (set by
        # the caller in _reconcile_entry_orders at the moment it escalates
        # to a cancel), which distinguishes USER_CANCEL/EOD/TTL_REPRICE
        # instead of collapsing all three into "was a cancel requested?".
        # Kept as a parameter for call-site clarity/back-compat.
        if action == AttemptDeadlineAction.STILL_WORKING:
            return False

        if action == AttemptDeadlineAction.AWAIT_CANCEL_CONFIRMATION:
            if card.entry_cancel_in_flight:
                return False
            card.entry_cancel_in_flight = True
            return True

        if action == AttemptDeadlineAction.MOVE_TO_OPEN_POSITION:
            # "FILLED" means *this order* executed completely -- it does
            # not by itself mean the card's overall target_position_quantity
            # has been reached (a smaller completion order can fill 100%
            # and still leave a gap). _protect_new_fill already computed
            # the correct entry_remaining_target_quantity/position_runtime_status
            # from the actual cumulative broker_quantity; do not stomp it
            # back to 0/OPEN unconditionally here, or a still-incomplete
            # target silently stops being pursued the moment one attempt
            # happens to fill in full.
            self._protect_new_fill(card, order)
            card.entry_runtime_status = None
            card.entry_block_reason = ""
            card.entry_cancel_in_flight = False
            card.entry_cancel_reason = ""
            card.entry_cancel_command_id = ""
            card.entry_client_order_id = ""
            card.entry_pending_attempt_number = 0
            card.entry_submission_unresolved = False
            card.next_retry_at = None
            if card.entry_remaining_target_quantity <= 0:
                card.entry_attempt_group_id = ""
            return True

        if action == AttemptDeadlineAction.CONFIRMED_CANCELLED_WITH_FILL:
            if card.broker_quantity <= 0:
                self._protect_new_fill(card, order)
            # Review finding P0-6: only an automatic TTL reprice-cancel
            # keeps trying to fill the remainder -- a USER_CANCEL/EOD/
            # EXIT_ALL cancel means something explicitly decided this entry
            # should stop, so the partial fill stands but no more buying
            # happens for it.
            retry_remainder = card.entry_cancel_reason == EntryCancelReason.TTL_REPRICE.value
            if not retry_remainder:
                card.entry_remaining_target_quantity = 0
            card.entry_runtime_status = None
            card.entry_block_reason = ""
            card.entry_cancel_in_flight = False
            card.entry_cancel_reason = ""
            card.entry_cancel_command_id = ""
            card.entry_client_order_id = ""
            card.entry_pending_attempt_number = 0
            card.entry_submission_unresolved = False
            card.next_retry_at = None
            if not retry_remainder:
                card.entry_attempt_group_id = ""
            if not card.exit_all_required:
                card.position_runtime_status = (
                    PositionRuntimeStatus.ENTRY_COMPLETING
                    if card.entry_remaining_target_quantity > 0
                    else PositionRuntimeStatus.OPEN
                )
            return True

        if action == AttemptDeadlineAction.CONFIRMED_CANCELLED_ZERO_FILL:
            card.entry_cancel_in_flight = False
            reason = card.entry_cancel_reason
            card.entry_cancel_reason = ""
            card.entry_cancel_command_id = ""
            card.entry_client_order_id = ""
            card.entry_pending_attempt_number = 0
            card.entry_submission_unresolved = False
            retry_remainder = reason == EntryCancelReason.TTL_REPRICE.value
            if already_has_position:
                # A completion attempt for the remainder was cancelled with
                # no additional fill -- the existing (already protected)
                # position stands; only an automatic TTL cancel keeps
                # trying to complete it.
                if not retry_remainder:
                    card.entry_remaining_target_quantity = 0
                    card.entry_attempt_group_id = ""
                card.entry_runtime_status = None
                card.entry_block_reason = ""
                if not card.exit_all_required:
                    card.position_runtime_status = (
                        PositionRuntimeStatus.ENTRY_COMPLETING
                        if card.entry_remaining_target_quantity > 0
                        else PositionRuntimeStatus.OPEN
                    )
                return True
            if reason in (EntryCancelReason.USER_CANCEL.value, EntryCancelReason.EOD.value):
                card.board_status = BoardStatus.BUYLIST
                card.session_date = None
                card.entry_runtime_status = None
                card.entry_block_reason = ""
                card.entry_attempt_group_id = ""
            else:
                card.board_status = BoardStatus.BUY_TODAY
                card.entry_runtime_status = EntryRuntimeStatus.RETRY_COOLDOWN
                card.next_retry_at = now + timedelta(seconds=execution_config.ENTRY_RETRY_COOLDOWN_SECONDS)
            return True

        if action == AttemptDeadlineAction.RELEASE_AND_RETRY_AFTER_COOLDOWN:
            card.entry_cancel_in_flight = False
            card.entry_cancel_reason = ""
            card.entry_cancel_command_id = ""
            card.entry_client_order_id = ""
            card.entry_pending_attempt_number = 0
            card.entry_submission_unresolved = False
            if already_has_position:
                card.entry_remaining_target_quantity = 0
                card.entry_attempt_group_id = ""
                if not card.exit_all_required:
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

        if not self._market_is_open():  # P0-8
            return []

        for card in cards:
            if card.board_status != BoardStatus.OPEN_POSITION:
                continue
            if card.entry_remaining_target_quantity <= 0:
                continue
            if card.exit_all_required:
                continue  # a stop/liquidation is in progress -- do not buy more
            if card.entry_submission_unresolved:
                continue
            try:
                if self._entry_deadline_lookup.find_open_entry_order(card) is not None:
                    continue  # already retrying, handled by _reconcile_entry_orders
                if not self._market_data.entry_quote_ready(card.symbol, now=now):
                    continue  # cannot safely re-attempt without fresh execution-grade data
                price = self._orb_high_entry_limit(card)
                if not price:
                    continue
                self._prepare_entry_attempt(card)
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
                        attempt_group_id=card.entry_attempt_group_id,
                        attempt_number=card.entry_pending_attempt_number,
                        client_order_id=card.entry_client_order_id,
                    )
                )
            except Exception:
                logger.exception("_process_entry_completion failed to build a trigger for %s", card.symbol)

        if not triggers:
            return []

        changed: List[TradeCardState] = []
        results = self._entry_attempt_manager.process_triggers(triggers)
        for result in results:
            key = f"{result.trigger.environment}:{result.trigger.account_no}:{result.trigger.symbol}"
            card = trigger_cards.get(key)
            if card is None or result.outcome == AttemptOutcome.SYMBOL_LOCKED:
                continue
            try:
                if result.outcome in (
                    AttemptOutcome.SUBMITTED,
                    AttemptOutcome.DUPLICATE_ORDER,
                    AttemptOutcome.UNRESOLVED,
                ):
                    card.entry_runtime_status = EntryRuntimeStatus.ORDER_PENDING
                    card.entry_submission_unresolved = (
                        result.outcome != AttemptOutcome.SUBMITTED
                    )
                    if result.outcome == AttemptOutcome.SUBMITTED:
                        card.entry_block_reason = ""
                    if result.submission is not None:
                        card.entry_client_order_id = result.submission.client_order_id
                        card.capital_reservation_id = result.submission.capital_reservation_id
                    card.next_retry_at = None
                elif result.outcome == AttemptOutcome.BROKER_REJECTED:
                    self._apply_definitive_entry_rejection(
                        card,
                        detail=result.detail,
                    )
                else:
                    card.entry_runtime_status = _OUTCOME_TO_ENTRY_RUNTIME_STATUS.get(
                        result.outcome, card.entry_runtime_status
                    )
                    if result.outcome == AttemptOutcome.BROKER_ROUTING_REJECTED:
                        card.entry_block_reason = (
                            "KIS rejected the verified exchange route (APBK0656); "
                            "the plan remains in Buy Today for a corrected retry"
                        )
                        card.buy_today_note = card.entry_block_reason
                    card.next_retry_at = result.retry_at
                    if result.outcome in {
                        AttemptOutcome.REJECTED,
                        AttemptOutcome.BROKER_ROUTING_REJECTED,
                    }:
                        card.entry_client_order_id = ""
                        card.entry_pending_attempt_number = 0
                        card.entry_submission_unresolved = False
                changed.append(card)
            except Exception:
                logger.exception("_process_entry_completion failed to apply a result for %s", card.symbol)
        return changed

    # -- Partial Sell processing (review finding P0-3) -------------------

    def _process_partial_sell_requests(self, cards: List[TradeCardState]) -> List[TradeCardState]:
        """A card that just moved to PARTIAL_SELL (``pending_partial_sell_quantity``
        set by the board command, spec section 563-579) actually gets a
        PARTIAL_EXIT order submitted here -- previously only the visual
        column changed.
        """
        changed: List[TradeCardState] = []
        now = self._clock()
        for card in cards:
            if card.board_status != BoardStatus.PARTIAL_SELL:
                continue
            if card.pending_partial_sell_quantity <= 0:
                continue
            if card.exit_submission_unresolved:
                continue
            if card.next_exit_retry_at is not None and now < card.next_exit_retry_at:
                continue  # P1-4: back off after a rejected/errored submission
            try:
                if self._position_callbacks.find_open_sell_order(card) is not None:
                    continue  # already submitted/working
                remaining = self._position_callbacks.refresh_orderable_quantity(
                    card.environment, card.account_no, card.symbol
                )
                quantity = min(card.pending_partial_sell_quantity, remaining)
                if quantity <= 0:
                    continue
                deadline = now + timedelta(
                    seconds=execution_config.PARTIAL_EXIT_ATTEMPT_TTL_SECONDS
                )
                order = self._position_callbacks.submit_sell_order(
                    environment=card.environment,
                    account_no=card.account_no,
                    symbol=card.symbol,
                    quantity=quantity,
                    reason="partial_sell",
                    attempt_deadline_at=deadline.isoformat(),
                    trade_card=card,
                )
                if order is not None and order.status == UnifiedExecutionStatus.REJECTED:
                    self._back_off_exit_retry(card, order.error_message or "Partial sell rejected")
                    changed.append(card)
                    continue
                self._consume_exit_attempt(card)
                # Review finding P1-3: broker_quantity/orderable_quantity
                # stay broker-authoritative -- do not guess orderable_quantity
                # here before the order is confirmed filled/rejected.
                # reserved_sell_quantity is purely informational bookkeeping
                # of what this working order is for.
                card.reserved_sell_quantity = quantity
                card.next_exit_retry_at = None
                card.last_exit_error = ""
                changed.append(card)
            except (
                GuardedSubmissionAmbiguousError,
                AmbiguousPostBrokerPersistenceError,
                DuplicateCommandError,
            ) as exc:
                card.exit_submission_unresolved = True
                card.next_exit_retry_at = None
                card.last_exit_error = f"UNRESOLVED: {exc}"[:500]
                changed.append(card)
            except Exception as exc:
                logger.exception("_process_partial_sell_requests failed for %s", card.symbol)
                self._back_off_exit_retry(card, str(exc))
                changed.append(card)
        return changed

    def _back_off_exit_retry(self, card: TradeCardState, error: str) -> None:
        """Review finding P1-4: a rejected/erroring Sell All or Partial Sell
        submission must not be resubmitted on literally every 1-second
        heartbeat tick."""
        self._consume_exit_attempt(card)
        card.exit_client_order_id = ""
        card.exit_pending_attempt_number = 0
        card.exit_submission_unresolved = False
        card.last_exit_error = str(error or "")[:500]
        card.next_exit_retry_at = self._clock() + timedelta(
            seconds=execution_config.EXIT_RETRY_COOLDOWN_SECONDS
        )

    @staticmethod
    def _consume_exit_attempt(card: TradeCardState) -> None:
        """Record the highest logical exit attempt whose identity was used."""
        attempt_number = card.exit_pending_attempt_number or (
            card.exit_attempt_count + 1
        )
        card.exit_attempt_count = max(card.exit_attempt_count, attempt_number)

    def _retire_terminal_exit_attempt(self, card: TradeCardState) -> bool:
        """Retire one terminal order identity while preserving its chain."""
        changed = self._clear_exit_cancel_tracking(card)
        if card.exit_client_order_id or card.exit_pending_attempt_number:
            self._consume_exit_attempt(card)
            card.exit_client_order_id = ""
            card.exit_pending_attempt_number = 0
            card.exit_submission_unresolved = False
            changed = True
        return changed

    def _reconcile_partial_sell_fills(self, cards: List[TradeCardState]) -> List[TradeCardState]:
        """Once the PARTIAL_EXIT order resolves, apply the fill and move
        the stop to breakeven (spec section 596-603), returning the card to
        Open Positions.

        Also owns the PARTIAL_EXIT_ATTEMPT_TTL_SECONDS cancel escalation
        (code review: a working exit order previously had no deadline at
        all) -- folded into this same reconcile pass, rather than a
        separate stage, so a status transition observed here is never
        re-reconciled (and its fill silently dropped) by a second call this
        same tick.
        """
        changed: List[TradeCardState] = []
        now = self._clock()
        for card in cards:
            if card.board_status != BoardStatus.PARTIAL_SELL:
                continue
            try:
                order = self._position_callbacks.find_open_sell_order(card)
                if order is None:
                    if card.pending_partial_sell_quantity <= 0:
                        # A user withdrawal whose known order is now absent
                        # has reached its terminal reconciliation boundary.
                        # Refresh holdings before returning to Open so a
                        # fill that raced the cancel is never hidden.
                        remaining = self._position_callbacks.refresh_orderable_quantity(
                            card.environment, card.account_no, card.symbol
                        )
                        self._complete_partial_exit(card, remaining)
                        changed.append(card)
                        continue
                    if self._clear_exit_cancel_tracking(card):
                        changed.append(card)
                    continue
                refreshed = self._position_callbacks.reconcile_sell_order(order)
                if refreshed.status.value not in ("FILLED", "CANCELLED", "REJECTED", "EXPIRED"):
                    if self._escalate_exit_cancel_if_due(
                        card,
                        refreshed,
                        now,
                        force=card.pending_partial_sell_quantity <= 0,
                    ):
                        changed.append(card)
                    continue
                remaining = self._position_callbacks.refresh_orderable_quantity(
                    card.environment, card.account_no, card.symbol
                )
                self._complete_partial_exit(card, remaining)
                changed.append(card)
            except Exception:
                logger.exception("_reconcile_partial_sell_fills failed for %s", card.symbol)
        return changed

    def _complete_partial_exit(
        self, card: TradeCardState, remaining: int
    ) -> None:
        """Project terminal partial-exit truth and retire its correlation."""
        self._clear_exit_cancel_tracking(card)
        # A rejected or zero-fill cancellation leaves the existing stop
        # untouched. Any broker-confirmed quantity decrease, including a
        # fill racing a user cancel, receives breakeven protection.
        if remaining < card.broker_quantity:
            self._position_manager.on_partial_exit_filled(
                card, refreshed_broker_quantity=remaining
            )
        else:
            card.broker_quantity = remaining
            card.orderable_quantity = remaining
            card.board_status = BoardStatus.OPEN_POSITION
            card.position_runtime_status = PositionRuntimeStatus.OPEN
        card.pending_partial_sell_quantity = 0
        card.reserved_sell_quantity = 0
        card.exit_client_order_id = ""
        card.exit_pending_attempt_number = 0
        card.exit_submission_unresolved = False
        card.exit_attempt_group_id = ""
        card.exit_attempt_count = 0
        card.next_exit_retry_at = None
        card.last_exit_error = ""

    # -- Queued market-open Sell All (section 720-732) -------------------

    def _process_queued_market_open_sells(self, cards: List[TradeCardState]) -> List[TradeCardState]:
        changed: List[TradeCardState] = []
        for card in cards:
            if card.board_status != BoardStatus.SELL_ALL or not card.sell_all_at_market_open:
                continue
            if not self._market_is_open():
                continue
            try:
                self._initiate_sell_all(card)
                card.sell_all_at_market_open = False
                changed.append(card)
            except Exception:
                logger.exception("_process_queued_market_open_sells failed for %s", card.symbol)
        return changed

    # -- Exit-order TTL/cancel-confirm helpers, shared by Partial Sell and --
    # -- Sell All (code review: neither previously had any deadline at all) --

    def _clear_exit_cancel_tracking(self, card: TradeCardState) -> bool:
        """Resets the exit-side cancel-in-flight bookkeeping. Returns True
        if anything actually changed, so callers only report the card as
        touched when this had an effect.
        """
        changed = card.exit_cancel_in_flight or card.exit_cancel_requested_at is not None
        card.exit_cancel_in_flight = False
        card.exit_cancel_requested_at = None
        card.exit_cancel_command_id = ""
        if _EXIT_CANCEL_STALLED_WARNING in card.warnings:
            card.warnings = [w for w in card.warnings if w != _EXIT_CANCEL_STALLED_WARNING]
            changed = True
        return changed

    def _escalate_exit_cancel_if_due(
        self,
        card: TradeCardState,
        order: BrokerOrder,
        now: datetime,
        *,
        force: bool = False,
    ) -> bool:
        """Called for a still-open (non-terminal) Partial Sell/Sell All
        order every heartbeat tick. Requests a cancel once its
        ``attempt_deadline_at`` (stamped at submission time from
        ``PARTIAL_EXIT_ATTEMPT_TTL_SECONDS``/``SELL_ALL_ATTEMPT_TTL_SECONDS``)
        passes, and -- mirroring the entry side's two-phase cancel -- never
        re-requests one that is already in flight; instead it watches
        ``EXIT_CANCEL_CONFIRMATION_TIMEOUT_SECONDS`` and flags the card if
        the broker still hasn't confirmed the cancel by then, so a stalled
        liquidation is visible rather than silently waiting forever.
        """
        if card.exit_cancel_in_flight:
            requested_at = card.exit_cancel_requested_at
            if requested_at is not None:
                stuck_seconds = (now - requested_at).total_seconds()
                if stuck_seconds > execution_config.EXIT_CANCEL_CONFIRMATION_TIMEOUT_SECONDS:
                    logger.error(
                        "Exit cancel for %s (%s:%s) unconfirmed %.0fs after cancel "
                        "request -- broker order %s may need manual attention",
                        card.symbol, card.environment, card.account_no,
                        stuck_seconds, order.client_order_id,
                    )
                    if _EXIT_CANCEL_STALLED_WARNING not in card.warnings:
                        card.warnings = [*card.warnings, _EXIT_CANCEL_STALLED_WARNING]
                        return True
            return False
        if not force and not self._attempt_deadline_passed(order, now):
            return False
        self._position_callbacks.request_cancel(card, order.client_order_id, scope="EXIT")
        card.exit_cancel_in_flight = True
        card.exit_cancel_requested_at = now
        return True

    def _reconcile_sell_all_orders(self, cards: List[TradeCardState]) -> List[TradeCardState]:
        """Sell All's working order previously had no reconciliation of its
        own in this engine at all -- ``_retry_incomplete_sell_alls`` only
        ever checked whether ``find_open_sell_order`` returned nothing,
        which stays true forever for a WORKING/PARTIALLY_FILLED order until
        something else reconciles it. This queries/reconciles it every
        heartbeat tick and applies the same SELL_ALL_ATTEMPT_TTL_SECONDS
        cancel escalation the review asked for, so a stalled liquidation is
        cancelled and repriced instead of sitting open indefinitely.
        """
        changed: List[TradeCardState] = []
        now = self._clock()
        for card in cards:
            if card.board_status != BoardStatus.SELL_ALL:
                continue
            try:
                order = self._position_callbacks.find_open_sell_order(card)
                if order is None:
                    if self._retire_terminal_exit_attempt(card):
                        changed.append(card)
                    continue
                refreshed = self._position_callbacks.reconcile_sell_order(order)
                if refreshed.is_open():
                    # A Partial Sell upgraded by the user to Sell All must be
                    # cancelled immediately, not left working until its TTL.
                    # The replacement full liquidation is still submitted
                    # only after terminal reconciliation proves it gone.
                    if self._escalate_exit_cancel_if_due(
                        card,
                        refreshed,
                        now,
                        force=refreshed.intent == OrderIntent.PARTIAL_EXIT,
                    ):
                        changed.append(card)
                    continue
                # Terminal (filled/cancelled/rejected/expired) -- refresh the
                # broker-truth remaining quantity immediately so the UI and
                # _retry_incomplete_sell_alls (which runs later this same
                # tick) both see current reality rather than waiting a full
                # extra heartbeat to notice the order is gone.
                remaining = self._position_callbacks.refresh_orderable_quantity(
                    card.environment, card.account_no, card.symbol
                )
                card.broker_quantity = remaining
                card.orderable_quantity = remaining
                self._retire_terminal_exit_attempt(card)
                changed.append(card)
            except Exception:
                logger.exception("_reconcile_sell_all_orders failed for %s", card.symbol)
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
        now = self._clock()
        for card in cards:
            if card.board_status != BoardStatus.SELL_ALL:
                continue
            if card.next_exit_retry_at is not None and now < card.next_exit_retry_at:
                continue  # P1-4: back off after a rejected/errored submission
            if card.exit_submission_unresolved:
                continue
            try:
                clear_to_sell, conflict_changed = (
                    self._entry_conflict_clear_for_liquidation(card)
                )
                if conflict_changed and card not in changed:
                    changed.append(card)
                if not clear_to_sell:
                    continue
                if self._position_callbacks.find_open_sell_order(card) is not None:
                    continue  # already working, or its cancel hasn't confirmed yet
                # A synchronous confirmed cancel can make the prior order
                # disappear between the reconciliation and retry stages in
                # this same heartbeat. Consume and retire that exact identity
                # before the replacement is derived.
                if self._retire_terminal_exit_attempt(card) and card not in changed:
                    changed.append(card)
                if self._trading_halt_lookup(card.symbol):
                    if _TRADING_HALT_EXIT_WARNING not in card.warnings:
                        card.warnings = [*card.warnings, _TRADING_HALT_EXIT_WARNING]
                        if card not in changed:
                            changed.append(card)
                    continue
                if _TRADING_HALT_EXIT_WARNING in card.warnings:
                    card.warnings = [
                        warning
                        for warning in card.warnings
                        if warning != _TRADING_HALT_EXIT_WARNING
                    ]
                    if card not in changed:
                        changed.append(card)
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
                # P0-8: a liquidation triggered outside the queued-at-open
                # path (e.g. a stop hit while the market happens to be
                # closed) must wait for the session, not fire a
                # regular-hours order outside it.
                market_open = self._market_is_open()
                if not market_open and not card.sell_all_at_market_open:
                    continue
                card.broker_quantity = remaining
                card.orderable_quantity = remaining
                deadline = now + timedelta(seconds=execution_config.SELL_ALL_ATTEMPT_TTL_SECONDS)
                order = self._position_callbacks.submit_sell_order(
                    environment=card.environment,
                    account_no=card.account_no,
                    symbol=card.symbol,
                    quantity=remaining,
                    reason=(
                        "sell_all"
                        if card.sell_all_at_market_open
                        else "sell_all_retry"
                    ),
                    attempt_deadline_at=deadline.isoformat(),
                    trade_card=card,
                )
                if order is not None and order.status == UnifiedExecutionStatus.REJECTED:
                    self._back_off_exit_retry(card, order.error_message or "Sell All rejected")
                else:
                    self._consume_exit_attempt(card)
                    if card.sell_all_at_market_open:
                        card.sell_all_at_market_open = False
                        card.position_runtime_status = PositionRuntimeStatus.LIQUIDATING
                    card.next_exit_retry_at = None
                    card.last_exit_error = ""
                changed.append(card)
            except (
                GuardedSubmissionAmbiguousError,
                AmbiguousPostBrokerPersistenceError,
                DuplicateCommandError,
            ) as exc:
                card.exit_submission_unresolved = True
                card.next_exit_retry_at = None
                card.last_exit_error = f"UNRESOLVED: {exc}"[:500]
                changed.append(card)
            except GuardedCancellationRejectedError as exc:
                # request_cancel_with_lifecycle already retired the failed
                # caller-owned ID and cleared the in-flight marker. This was
                # not a SELL submission failure, so do not consume an exit
                # attempt or impose a sell-retry backoff; the next heartbeat
                # may make a fresh cancellation decision with a fresh ID.
                card.next_exit_retry_at = None
                card.last_exit_error = f"Entry cancel rejected: {exc}"[:500]
                if card not in changed:
                    changed.append(card)
            except ExecutionGradeDataUnavailableError as exc:
                # Pricing failed before prepare_exit_identity() and therefore
                # consumed no logical attempt. Preserve the bounded reprice
                # counter at its authoritative cap and retry only after the
                # normal cooldown (or manual/feed recovery).
                card.exit_client_order_id = ""
                card.exit_pending_attempt_number = 0
                card.exit_submission_unresolved = False
                card.last_exit_error = str(exc)[:500]
                card.next_exit_retry_at = self._clock() + timedelta(
                    seconds=execution_config.EXIT_RETRY_COOLDOWN_SECONDS
                )
                if card not in changed:
                    changed.append(card)
            except Exception as exc:
                logger.exception("_retry_incomplete_sell_alls failed for %s", card.symbol)
                self._back_off_exit_retry(card, str(exc))
                changed.append(card)
        return changed

    def _market_is_open(self) -> bool:
        """Delegates to the injected ``market_is_open`` callable (review
        finding P0-8) -- production wiring (``src.services.buyboard_runtime``)
        supplies the app's real, holiday-aware NYSE session calculation
        (``src.utils.market_calendar.is_regular_session_open``); defaults to
        always-open so unit tests can drive this deterministically without a
        real calendar unless they explicitly opt in.
        """
        return self._market_is_open_fn()

    # -- EOD cleanup (review finding P1-14) -------------------------------

    def _run_eod_cleanup_if_due(self, cards: List[TradeCardState]) -> List[TradeCardState]:
        """The EOD service (section 13) previously existed only as a module
        nothing ever called. Runs on every heartbeat tick once
        ``_eod_window_reached`` says the final safety window has begun. The
        service receives the separate market-closed state so untouched Buy
        Today cards cannot reset before the bell. Safe to call repeatedly
        since each stage inside it is itself idempotent
        (a card that has already moved off BUY_TODAY/ENTRY_PENDING/an
        incomplete-target OPEN_POSITION simply no longer matches any of its
        conditions).
        """
        if self._eod_service is None:
            return []
        if not self._eod_window_reached():
            return []
        return self._eod_service.run_eod_cleanup(
            cards,
            market_closed=not self._market_is_open(),
        )

    def expire_buy_today_cards_if_due(
        self, cards: List[TradeCardState]
    ) -> List[TradeCardState]:
        """Retire one-session Buy Today intent for the full board scope.

        The worker deliberately passes only execution-ready cards to the main
        heartbeat.  Session expiry is not an entry or broker mutation, so it
        must receive every card instead of being suppressed by reconciliation
        or quote-readiness failures.
        """

        if self._eod_service is None:
            return []
        market_closed_in_eod_window = bool(
            self._eod_window_reached() and not self._market_is_open()
        )
        return self._eod_service.expire_buy_today_cards(
            cards,
            market_closed=market_closed_in_eod_window,
        )

    def _eod_window_reached(self) -> bool:
        """Delegates to the injected ``eod_window_reached`` callable (review
        finding P0-8) -- production wiring opens the processing window shortly
        before the holiday-aware regular-session close and leaves it open
        afterward. Defaults to False so tests never trigger EOD cleanup unless
        they explicitly opt in.
        """
        return self._eod_window_reached_fn()

    # -- Structural feed protection for existing positions ------------------

    def _detect_stale_position_quotes(self, cards: List[TradeCardState]) -> List[TradeCardState]:
        """Detect structural feed loss for an existing open position.

        New entries and exact broker mutations still require execution-fresh
        data.  This monitor intentionally uses connection/subscription/error
        state instead of a per-symbol event-age threshold: event-driven,
        illiquid symbols can be quiet without their feed being unavailable.
        """
        changed: List[TradeCardState] = []
        now = self._clock()
        for card in cards:
            if card.board_status not in _TICK_REACTIVE_POSITION_STATUSES:
                position_exposure_remains = bool(
                    card.broker_quantity > 0
                    or card.position_runtime_status
                    not in {PositionRuntimeStatus.NONE, PositionRuntimeStatus.CLOSED}
                )
                # Pending/liquidating lifecycle stages are owned by their
                # dedicated reconciliation logic.  Preserve any incident
                # already raised for a still-exposed card, but do not create a
                # new one or erase it from this unrelated heartbeat stage.
                if position_exposure_remains:
                    continue
                before = (
                    card.market_data_outage_started_at,
                    card.market_data_outage_risk_tier,
                    tuple(card.warnings),
                )
                self._clear_stale_warning(card)
                self._clear_market_data_outage_warnings(card)
                card.market_data_outage_started_at = None
                card.market_data_outage_risk_tier = ""
                after = (
                    card.market_data_outage_started_at,
                    card.market_data_outage_risk_tier,
                    tuple(card.warnings),
                )
                if before != after:
                    changed.append(card)
                continue
            try:
                quote = self._market_data.latest_quote(card.symbol)
                feed_available = getattr(
                    self._market_data, "is_symbol_feed_available", None
                )
                if callable(feed_available):
                    structurally_ready = bool(
                        feed_available(
                            card.symbol,
                            require_trade=True,
                            require_quote=False,
                        )
                    )
                else:
                    structurally_ready = bool(
                        self._market_data.is_symbol_execution_ready(
                            card.symbol,
                            require_trade=True,
                            require_quote=False,
                            now=now,
                        )
                    )
                feed_unavailable = not structurally_ready
                already_flagged = _DATA_STALE_WARNING in card.warnings
                if feed_unavailable and not already_flagged:
                    card.warnings = [*card.warnings, _DATA_STALE_WARNING]
                    changed.append(card)
                elif not feed_unavailable and already_flagged:
                    self._clear_stale_warning(card)
                    changed.append(card)

                if (
                    not feed_unavailable
                    and (
                        card.market_data_outage_started_at is not None
                        or _OUTAGE_HIGH_WARNING in card.warnings
                        or _OUTAGE_LOW_WARNING in card.warnings
                    )
                ):
                    before = (
                        card.market_data_last_trusted_price,
                        card.market_data_last_trusted_at,
                        card.market_data_outage_started_at,
                        card.market_data_outage_risk_tier,
                        tuple(card.warnings),
                    )
                    execution_fresh = bool(
                        quote is not None
                        and quote.last_price > 0
                        and self._market_data.is_symbol_execution_ready(
                            card.symbol,
                            require_trade=True,
                            require_quote=False,
                            now=now,
                        )
                    )
                    if execution_fresh:
                        card.market_data_last_trusted_price = float(quote.last_price)
                        card.market_data_last_trusted_at = quote.broker_event_at
                        try:
                            recovered_equity = float(
                                self._account_equity_provider(
                                    card.environment, card.account_no
                                )
                                or 0.0
                            )
                        except Exception:
                            recovered_equity = 0.0
                        recovered_tier = classify_market_data_outage_risk(
                            card,
                            trusted_price=float(quote.last_price),
                            account_equity=recovered_equity,
                            bid=quote.bid,
                            ask=quote.ask,
                            liquidity_tier=self._liquidity_tier_lookup(card, quote),
                        )
                        card.market_data_outage_risk_tier = recovered_tier.value
                    card.market_data_outage_started_at = None
                    self._clear_market_data_outage_warnings(card)
                    after = (
                        card.market_data_last_trusted_price,
                        card.market_data_last_trusted_at,
                        card.market_data_outage_started_at,
                        card.market_data_outage_risk_tier,
                        tuple(card.warnings),
                    )
                    if before != after and card not in changed:
                        changed.append(card)
                    continue

                if not feed_unavailable:
                    continue
                if card.market_data_outage_started_at is None:
                    card.market_data_outage_started_at = now
                    trusted_price = float(
                        card.market_data_last_trusted_price
                        or (quote.last_price if quote is not None else 0.0)
                    )
                    card.market_data_last_trusted_price = (
                        trusted_price if trusted_price > 0 else None
                    )
                    card.market_data_last_trusted_at = (
                        quote.broker_event_at if quote is not None else None
                    )
                    try:
                        equity = float(
                            self._account_equity_provider(
                                card.environment, card.account_no
                            )
                            or 0.0
                        )
                    except Exception:
                        equity = 0.0
                    tier = classify_market_data_outage_risk(
                        card,
                        trusted_price=trusted_price,
                        account_equity=equity,
                        bid=quote.bid if quote is not None else None,
                        ask=quote.ask if quote is not None else None,
                        liquidity_tier=self._liquidity_tier_lookup(card, quote),
                    )
                    card.market_data_outage_risk_tier = tier.value
                    warning = (
                        _OUTAGE_HIGH_WARNING
                        if tier == execution_config.MarketDataOutageRiskTier.HIGH
                        else _OUTAGE_LOW_WARNING
                    )
                    if warning not in card.warnings:
                        card.warnings = [*card.warnings, warning]
                    if card not in changed:
                        changed.append(card)

                elapsed = max(
                    0.0, (now - card.market_data_outage_started_at).total_seconds()
                )
                if (
                    card.market_data_outage_risk_tier
                    == execution_config.MarketDataOutageRiskTier.LOW.value
                    and self._broader_market_risk_signal(card)
                ):
                    card.market_data_outage_risk_tier = (
                        execution_config.MarketDataOutageRiskTier.HIGH.value
                    )
                    card.warnings = [
                        w for w in card.warnings if w != _OUTAGE_LOW_WARNING
                    ]
                    if _OUTAGE_HIGH_WARNING not in card.warnings:
                        card.warnings = [*card.warnings, _OUTAGE_HIGH_WARNING]
                    if card not in changed:
                        changed.append(card)

                tier_is_high = (
                    card.market_data_outage_risk_tier
                    == execution_config.MarketDataOutageRiskTier.HIGH.value
                )
                max_hold_reached = bool(
                    execution_config.MARKET_DATA_OUTAGE_MAX_HOLD_SECONDS > 0
                    and elapsed >= execution_config.MARKET_DATA_OUTAGE_MAX_HOLD_SECONDS
                )
                grace_reached = bool(
                    tier_is_high
                    and elapsed >= execution_config.MARKET_DATA_OUTAGE_GRACE_SECONDS
                )
                if (
                    (grace_reached or max_hold_reached)
                    and not (
                        execution_config.MARKET_DATA_OUTAGE_SUPERVISED_HOLD_ONLY
                        and not self._unattended_session()
                    )
                    and not card.exit_all_required
                ):
                    if self._market_is_open():
                        self._initiate_sell_all(card)
                    else:
                        self._initiate_sell_all(
                            card, queue_for_market_open=True
                        )
                    if card not in changed:
                        changed.append(card)
            except Exception:
                logger.exception("_detect_stale_position_quotes failed for %s", card.symbol)
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
