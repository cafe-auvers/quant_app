"""Position and exit engine. ``buydashboard_to_kanban.md`` sections 14-18.

Pure decision functions (breakeven math, stop-trigger evaluation, minimum
manual-stop bound) live at module scope so they're directly unit testable
and reusable from the Kanban command layer
(:mod:`src.ui.buyboard.controller`). ``PositionManager``'s sequence methods
orchestrate the multi-step flows the spec describes (stop-during-partial-
sell, Sell All, manual-position discovery) using **injected** broker-action
callbacks -- this module never imports :mod:`src.api.kis_order` directly,
mirroring how :mod:`src.services.entry_attempt_manager` receives a
``cancel_order`` callback and :mod:`src.services.order_execution_service`
receives a ``Broker`` instance rather than talking to KIS itself.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional
from uuid import uuid4

from src.core import execution_config
from src.core.execution_mode import ExecutionSource
from src.core.execution_request import CancelIntent
from src.core.execution_result import ExecutionSubmissionResult
from src.core.order_state import BrokerOrder
from src.core.trade_card_state import (
    BoardStatus,
    PositionRuntimeStatus,
    StopType,
    TradeCardState,
)
from src.services.execution_command_gateway import (
    GuardedCancellationAmbiguousError,
    GuardedCancellationRejectedError,
)

logger = logging.getLogger(__name__)


def _default_cancel_intent_factory(
    card: TradeCardState, client_order_id: str, scope: str
) -> CancelIntent:
    return CancelIntent(
        client_order_id=client_order_id,
        cancel_command_id=f"{client_order_id}:{scope}:{uuid4().hex}",
        environment=card.environment,
        account_no=card.account_no,
        lease=None,
        strategy_instance_id="",
        source=ExecutionSource.KANBAN_BOARD,
    )


def request_cancel_with_lifecycle(
    *,
    card: TradeCardState,
    client_order_id: str,
    scope: str,
    cancel_order: Callable[[CancelIntent], None],
    cancel_intent_factory: Callable[[TradeCardState, str, str], CancelIntent],
    persist_cancel_state: Callable[[TradeCardState], None],
) -> None:
    """Apply caller-owned cancel identity rules around one gateway call."""
    normalized_scope = str(scope or "").upper()
    intent = cancel_intent_factory(card, client_order_id, normalized_scope)
    try:
        result = cancel_order(intent)
    except GuardedCancellationAmbiguousError:
        # The broker may have acted. Keep the same command identity and
        # mark the cancel in flight so no heartbeat issues another call.
        if normalized_scope == "ENTRY":
            card.entry_cancel_in_flight = True
        else:
            card.exit_cancel_in_flight = True
            if card.exit_cancel_requested_at is None:
                card.exit_cancel_requested_at = datetime.now(timezone.utc)
        persist_cancel_state(card)
    except GuardedCancellationRejectedError:
        # The broker explicitly did not act. This decision is complete and
        # its command ID must never be replayed; the next heartbeat may mint
        # a fresh ID for a genuinely new cancel decision.
        if normalized_scope == "ENTRY":
            card.entry_cancel_command_id = ""
            card.entry_cancel_in_flight = False
            card.entry_cancel_reason = ""
        else:
            card.exit_cancel_command_id = ""
            card.exit_cancel_in_flight = False
            card.exit_cancel_requested_at = None
        persist_cancel_state(card)
        raise
    else:
        status = getattr(getattr(result, "status", None), "value", "")
        if (
            normalized_scope == "ENTRY"
            and intent.emergency
            and intent.protective_entry_completion
            and status in {"FILLED", "CANCELLED", "REJECTED", "EXPIRED"}
        ):
            # This exact completion BUY is broker-terminal. During a DB
            # outage there is no later canonical projection tick to clear
            # the hard entry-cancel fence before the protective SELL, so
            # retire only this caller-owned correlation locally. The
            # emergency journal preserves it for recovery/audit.
            card.entry_cancel_command_id = ""
            card.entry_cancel_in_flight = False
            card.entry_cancel_reason = ""
            card.entry_client_order_id = ""
            card.entry_pending_attempt_number = 0
            card.entry_submission_unresolved = False
            card.entry_remaining_target_quantity = 0
            card.entry_attempt_group_id = ""
            card.entry_attempt_count = 0
            persist_cancel_state(card)


def round_up_to_valid_tick(price: float, tick_size: float = 0.01) -> float:
    """Section 631: ``breakeven_stop_price = round_up_to_valid_tick(raw_breakeven)``.

    KIS overseas US equities trade in one-cent ticks at/above $1 and
    four-decimal ticks below $1 (the same granularity
    :func:`src.api.kis_order.format_overseas_order_price` already applies to
    outbound order prices). Rounding *up* -- never down -- guarantees the
    computed stop is never more aggressive (lower) than the raw formula
    intended.
    """
    if price <= 0:
        return 0.0
    effective_tick = 0.0001 if price < 1.0 else tick_size
    ticks = math.ceil(round(price / effective_tick, 6) - 1e-9)
    return round(ticks * effective_tick, 4)


def compute_breakeven_stop_price(
    average_entry_price: float, *, breakeven_buffer_bps: Optional[float] = None
) -> float:
    """Section 622-631: ``raw_breakeven = avg_cost * (1 + buffer_bps / 10_000)``."""
    bps = (
        breakeven_buffer_bps
        if breakeven_buffer_bps is not None
        else execution_config.BREAKEVEN_BUFFER_BPS
    )
    raw_breakeven = float(average_entry_price or 0.0) * (1 + float(bps) / 10_000)
    return round_up_to_valid_tick(raw_breakeven)


def _prices_match(a: float, b: float, *, tolerance: float = 1e-6) -> bool:
    return abs(float(a or 0.0) - float(b or 0.0)) <= tolerance


def minimum_manual_stop_price(card: TradeCardState) -> float:
    """Section 646-659: a manual stop can tighten risk but never widen it
    below breakeven or below the current active stop."""
    breakeven = compute_breakeven_stop_price(card.average_entry_price)
    current = card.active_stop_price or 0.0
    return max(breakeven, current)


def evaluate_stop_trigger(card: TradeCardState, current_price: float) -> bool:
    """Section 661-669: ``if current_price <= active_stop_price: exit_all_required = True``.

    Sticky by construction from the caller's point of view: once
    ``card.exit_all_required`` is already True this returns True
    unconditionally, regardless of what ``current_price`` recovers to
    afterward -- "a subsequent price recovery does not cancel the
    liquidation instruction" (section 667).
    """
    if card.exit_all_required:
        return True
    if card.active_stop_price is None or card.active_stop_price <= 0:
        return False
    return current_price <= card.active_stop_price


@dataclass
class PositionActionCallbacks:
    """Injected broker-boundary actions for the sequence methods below."""

    cancel_order: Callable[[CancelIntent], Any]
    submit_sell_order: Callable[..., ExecutionSubmissionResult]
    refresh_orderable_quantity: Callable[[str, str, str], int]  # (env, account, symbol) -> qty
    cancel_intent_factory: Callable[[TradeCardState, str, str], CancelIntent] = (
        _default_cancel_intent_factory
    )
    persist_cancel_state: Callable[[TradeCardState], None] = lambda card: None
    # Finds this card's currently-working exit (SELL) order, if any -- used
    # by callers (src.services.trading_engine) that need to cancel a
    # specific in-flight partial-sell order before escalating to a full
    # liquidation (section 671-697's "stop triggered during partial sell"),
    # and to avoid ever submitting a replacement sell order while a cancel
    # is still in flight (code review finding P1-4). Optional/defaulted so
    # existing callers that never need it are unaffected.
    find_open_sell_order: Callable[[TradeCardState], Optional[BrokerOrder]] = (
        lambda card: None
    )
    # Refreshes one sell order's status from the broker without cancelling
    # it -- the exit-side counterpart of EntryDeadlineLookup.reconcile_order.
    reconcile_sell_order: Callable[[BrokerOrder], BrokerOrder] = lambda order: order

    def request_cancel(
        self, card: TradeCardState, client_order_id: str, *, scope: str
    ) -> None:
        request_cancel_with_lifecycle(
            card=card,
            client_order_id=client_order_id,
            scope=scope,
            cancel_order=self.cancel_order,
            cancel_intent_factory=self.cancel_intent_factory,
            persist_cancel_state=self.persist_cancel_state,
        )


@dataclass(frozen=True)
class BrokerHolding:
    """One broker-reported overseas holding, typed and normalized."""

    symbol: str
    quantity: int
    average_price: float


def extract_overseas_holdings(position_snapshot: Optional[Dict[str, Any]]) -> List[BrokerHolding]:
    """Parses the ``Broker.get_positions()`` shape (``{"overseas": {"holdings":
    [...]}, ...}``, the same structure :mod:`src.services.handoff_reconciliation`
    already consumes) into typed, non-zero holdings."""
    holdings = ((position_snapshot or {}).get("overseas") or {}).get("holdings") or []
    result: List[BrokerHolding] = []
    for raw in holdings:
        if not isinstance(raw, dict):
            continue
        try:
            quantity = int(float(raw.get("quantity", 0) or 0))
        except (TypeError, ValueError):
            continue
        if quantity <= 0:
            continue
        symbol = str(raw.get("symbol", "")).strip().upper()
        if not symbol:
            continue
        try:
            average_price = float(raw.get("average_price", 0.0) or 0.0)
        except (TypeError, ValueError):
            average_price = 0.0
        result.append(BrokerHolding(symbol=symbol, quantity=quantity, average_price=average_price))
    return result


class PositionManager:
    """Orchestrates the position/exit sequences. Stateless aside from the
    ``TradeCardState`` each method mutates and returns -- callers
    (:mod:`src.services.trading_engine`) own persistence via
    :mod:`src.services.trade_card_repository`.
    """

    # --- Stop management (section 16) --------------------------------

    def apply_first_fill_stop(
        self, card: TradeCardState, *, entry_orb_low: float, entry_orb_window: str
    ) -> TradeCardState:
        """Section 606-620: at first fill, the initial stop is the ORB low.
        The selected entry ORB values are persisted here and must not be
        recalculated later -- "the position manager should not repeatedly
        recalculate the historical entry ORB after the position has been
        opened" (section 620).
        """
        card.stop_type = StopType.ORB_LOW
        card.active_stop_price = entry_orb_low
        card.stop_quantity = card.broker_quantity
        card.entry_orb_window = entry_orb_window
        card.entry_orb_low = entry_orb_low
        return card

    def apply_breakeven_stop(self, card: TradeCardState) -> TradeCardState:
        card.stop_type = StopType.BREAKEVEN
        card.active_stop_price = compute_breakeven_stop_price(card.average_entry_price)
        card.stop_quantity = card.broker_quantity
        return card

    def apply_manual_stop(self, card: TradeCardState, requested_price: float) -> TradeCardState:
        minimum = minimum_manual_stop_price(card)
        if requested_price < minimum:
            raise ValueError(
                f"Manual stop {requested_price} cannot widen risk below the minimum "
                f"{minimum} (max of breakeven and the current active stop, section 646-659)"
            )
        card.stop_type = StopType.MANUAL_PRICE
        card.active_stop_price = requested_price
        return card

    def on_partial_exit_filled(
        self, card: TradeCardState, *, refreshed_broker_quantity: int
    ) -> TradeCardState:
        """Section 596-603: after a confirmed partial exit, refresh the
        broker-reported quantity and move the stop to cost-adjusted
        breakeven -- replacing the legacy ``max(existing_stop, avg_cost)``
        behavior entirely (section 644).
        """
        card.broker_quantity = refreshed_broker_quantity
        card.orderable_quantity = refreshed_broker_quantity
        self.apply_breakeven_stop(card)
        card.board_status = BoardStatus.OPEN_POSITION
        card.position_runtime_status = PositionRuntimeStatus.OPEN
        return card

    def evaluate_tick(self, card: TradeCardState, current_price: float) -> TradeCardState:
        """Called on every market-data tick (section 766-770)."""
        if evaluate_stop_trigger(card, current_price):
            card.exit_all_required = True
        return card

    # --- Stop triggered while a partial sell is working (section 17) -----

    def handle_stop_triggered_during_partial_sell(
        self,
        card: TradeCardState,
        *,
        callbacks: PositionActionCallbacks,
        working_partial_sell_client_order_id: Optional[str],
    ) -> TradeCardState:
        """Section 671-697's required sequence. Steps 1-2 are this card's
        own state (exit_all_required + the board_status move below, which
        makes :func:`src.ui.buyboard.controller._apply_partial_sell`'s
        OPEN_POSITION-only guard block any further partial-sell command).
        Step 3 requests the cancel here; steps 4-7 (refresh quantity, submit
        the actual liquidation) are deliberately left to the trading
        engine's retry stage rather than done inline here (code review
        finding P1-4: a replacement sell must never be submitted while the
        cancel of the previous one is still unconfirmed -- the retry stage
        only submits once ``find_open_sell_order`` reports nothing working,
        which stays true of the cancelled order until the broker actually
        confirms it). Step 8 ("continue until flat") is likewise the
        caller's heartbeat re-invoking the retry stage, not a loop here.
        """
        card.exit_all_required = True
        if working_partial_sell_client_order_id:
            callbacks.request_cancel(
                card, working_partial_sell_client_order_id, scope="EXIT"
            )  # step 3
        card.pending_partial_sell_quantity = 0
        card.board_status = BoardStatus.SELL_ALL
        card.position_runtime_status = PositionRuntimeStatus.LIQUIDATING
        return card

    # --- Sell All (section 18) -------------------------------------------

    def start_sell_all(
        self,
        card: TradeCardState,
        *,
        callbacks: PositionActionCallbacks,
        working_buy_client_order_id: Optional[str] = None,
    ) -> TradeCardState:
        """Section 699-709 steps 1-3: sets intent and cancels any
        conflicting working BUY order. Steps 4-6 (refresh orderable
        quantity, submit the liquidation), the reprice/retry loop (step 8),
        and the Closed transition (step 9, via :meth:`confirm_flat`) are
        all owned by the trading engine's retry stage -- see
        :meth:`handle_stop_triggered_during_partial_sell`'s docstring for
        why submission does not happen inline here (P1-4).
        """
        card.exit_all_required = True
        if working_buy_client_order_id:
            callbacks.request_cancel(card, working_buy_client_order_id, scope="ENTRY")  # step 3
        card.board_status = BoardStatus.SELL_ALL
        card.position_runtime_status = PositionRuntimeStatus.LIQUIDATING
        return card

    def queue_sell_all_at_market_open(self, card: TradeCardState) -> TradeCardState:
        """Section 720-724: before market open, queue rather than submit."""
        card.board_status = BoardStatus.SELL_ALL
        card.position_runtime_status = PositionRuntimeStatus.QUEUED_FOR_OPEN
        card.sell_all_at_market_open = True
        card.exit_all_required = True
        return card

    def confirm_flat(self, card: TradeCardState) -> TradeCardState:
        """Section 709: "Move to Closed only when broker confirms zero.\""""
        if card.broker_quantity > 0:
            raise ValueError(
                f"Cannot close {card.symbol}: broker_quantity is {card.broker_quantity}, not zero"
            )
        card.board_status = BoardStatus.CLOSED
        card.position_runtime_status = PositionRuntimeStatus.CLOSED
        card.stop_type = None
        card.active_stop_price = None
        card.stop_quantity = 0
        card.pending_stop_type = None
        card.pending_stop_price = None
        card.pending_stop_quantity = 0
        card.pending_stop_command_id = ""
        card.pending_stop_requested_at = None
        card.exit_all_required = False
        card.sell_all_at_market_open = False
        card.market_data_outage_started_at = None
        card.market_data_outage_risk_tier = ""
        # A flat confirmation definitively ends this trade cycle. Retire
        # both correlation scopes so historical execution rows cannot
        # project onto a later Buylist/new-cycle card for the same symbol.
        card.entry_attempt_group_id = ""
        card.entry_attempt_count = 0
        card.entry_client_order_id = ""
        card.entry_pending_attempt_number = 0
        card.entry_submission_unresolved = False
        card.entry_cancel_in_flight = False
        card.entry_cancel_reason = ""
        card.entry_cancel_command_id = ""
        card.entry_remaining_target_quantity = 0
        card.exit_attempt_group_id = ""
        card.exit_attempt_count = 0
        card.exit_client_order_id = ""
        card.exit_pending_attempt_number = 0
        card.exit_submission_unresolved = False
        card.exit_cancel_in_flight = False
        card.exit_cancel_requested_at = None
        card.exit_cancel_command_id = ""
        card.reserved_sell_quantity = 0
        card.next_exit_retry_at = None
        card.last_exit_error = ""
        return card

    # --- Manual purchases outside the application (section 14) -----------

    def discover_manual_position(
        self,
        card: Optional[TradeCardState],
        *,
        environment: str,
        account_no: str,
        symbol: str,
        name: str,
        broker_quantity: int,
        average_entry_price: float,
    ) -> TradeCardState:
        """Section 534-562: broker position reconciliation is authoritative.
        Finds (or creates) the symbol's single card and moves it to Open
        Positions, preserving Watchlist/Buylist membership metadata rather
        than discarding it.
        """
        if card is None:
            card = TradeCardState(
                environment=environment,
                account_no=account_no,
                symbol=symbol,
                name=name,
                board_status=BoardStatus.OPEN_POSITION,
                watchlist_member=True,
                buylist_member=True,
                return_to_buylist_after_close=True,
            )
        else:
            card.board_status = BoardStatus.OPEN_POSITION
        card.broker_quantity = broker_quantity
        card.orderable_quantity = broker_quantity
        card.average_entry_price = average_entry_price
        card.position_runtime_status = PositionRuntimeStatus.OPEN
        if card.stop_type is None:
            # No ORB/entry history exists for a manually-purchased position
            # -- section 620 forbids inventing one. Code review finding
            # P1-3: silently pinning a stop at average cost is unsafe,
            # because if the current price is already below cost the very
            # next tick would trigger an immediate Sell All the instant the
            # engine notices this position. Instead, leave active_stop_price
            # unset (evaluate_stop_trigger never fires with no stop price),
            # flag the card for the user, and block new automated entries
            # for this symbol until an explicit SetManualStop/
            # SetBreakevenStop command sets a real stop.
            card.entry_block_reason = "manual_position_requires_stop"
            if "STOP_REQUIRED" not in card.warnings:
                card.warnings = [*card.warnings, "STOP_REQUIRED"]
        return card

    def reconcile_manual_exit(self, card: TradeCardState, *, broker_quantity: int) -> TradeCardState:
        """Section 14 (manual sale discovered): broker truth wins even when
        no local Sell All command exists."""
        card.broker_quantity = broker_quantity
        card.orderable_quantity = broker_quantity
        if broker_quantity <= 0:
            return self.confirm_flat(card)
        card.stop_quantity = min(card.stop_quantity, broker_quantity)
        return card

    def reconcile_broker_positions(
        self,
        cards: List[TradeCardState],
        holdings: List[BrokerHolding],
        *,
        environment: str,
        account_no: str,
        symbol_name_lookup: Callable[[str], str] = lambda symbol: symbol,
    ) -> List[TradeCardState]:
        """Cross-checks every broker-reported holding for this account
        against the current card set. This is the general-purpose version
        of section 534-562/14's manual-purchase-discovery flow and section
        1028-1032's "broker position exists without local card" test: it
        runs any time (not only on PC/laptop handoff, which is
        :mod:`src.services.handoff_reconciliation`'s narrower job), driven
        by the caller on the ``FULL_RECONCILIATION_SECONDS`` cadence
        (section 804-806).

        Broker truth always wins for *quantity and average price*. It does
        not win for board_status/exit intent: code review finding P1-1
        flagged that the original version routed every quantity-matching
        SELL_ALL/PARTIAL_SELL/ENTRY_PENDING card through
        :meth:`discover_manual_position`, which forces ``board_status`` back
        to OPEN_POSITION -- silently cancelling an in-progress liquidation
        or partial sell out from under the user/engine. A card already
        known to the application only has its broker-derived facts
        refreshed here; :meth:`discover_manual_position` (which does move
        board_status) is reserved for genuinely new/unknown positions.

        Returns every card that was created or mutated so the caller can
        persist them.
        """
        changed: List[TradeCardState] = []
        holdings_by_symbol = {holding.symbol: holding for holding in holdings}
        cards_by_symbol = {
            card.symbol: card
            for card in cards
            if card.environment == environment and card.account_no == account_no
        }

        # Statuses where the application already has an authoritative,
        # in-progress intent that broker reconciliation must not overwrite.
        _INTENT_PRESERVING_STATUSES = {
            BoardStatus.SELL_ALL,
            BoardStatus.PARTIAL_SELL,
            BoardStatus.ENTRY_PENDING,
        }

        for symbol, holding in holdings_by_symbol.items():
            card = cards_by_symbol.get(symbol)
            if card is None:
                changed.append(
                    self.discover_manual_position(
                        None,
                        environment=environment,
                        account_no=account_no,
                        symbol=symbol,
                        name=symbol_name_lookup(symbol),
                        broker_quantity=holding.quantity,
                        average_entry_price=holding.average_price,
                    )
                )
                continue

            # P1-2: compare quantity *and* average price, not quantity alone
            # -- an unchanged share count with a changed average cost (e.g.
            # an averaging-down purchase that nets back to the same size)
            # must still update the card.
            facts_changed = card.broker_quantity != holding.quantity or not _prices_match(
                card.average_entry_price, holding.average_price
            )

            if card.board_status in _INTENT_PRESERVING_STATUSES:
                if facts_changed:
                    card.broker_quantity = holding.quantity
                    card.average_entry_price = holding.average_price
                    changed.append(card)
                continue

            if card.board_status != BoardStatus.OPEN_POSITION or facts_changed:
                changed.append(
                    self.discover_manual_position(
                        card,
                        environment=environment,
                        account_no=account_no,
                        symbol=symbol,
                        name=card.name,
                        broker_quantity=holding.quantity,
                        average_entry_price=holding.average_price,
                    )
                )

        # Section 14 "manual sale discovered": a card believes it holds
        # shares the broker no longer reports at all.
        for symbol, card in cards_by_symbol.items():
            if symbol in holdings_by_symbol:
                continue
            if card.broker_quantity <= 0:
                continue
            if card.board_status == BoardStatus.SELL_ALL:
                # Expected outcome of a liquidation in progress -- let the
                # normal Sell All retry stage (which already checks
                # orderable_quantity) drive the Closed transition so the
                # rest of that sequence's bookkeeping stays consistent,
                # rather than jumping to Closed from a reconciliation pass.
                card.broker_quantity = 0
                card.orderable_quantity = 0
                changed.append(card)
                continue
            self.reconcile_manual_exit(card, broker_quantity=0)
            changed.append(card)

        return changed
