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
from typing import Any, Callable, Dict, List, Optional

from src.core import execution_config
from src.core.order_state import BrokerOrder
from src.core.trade_card_state import (
    BoardStatus,
    PositionRuntimeStatus,
    StopType,
    TradeCardState,
)

logger = logging.getLogger(__name__)


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

    cancel_order: Callable[[str], None]  # by client_order_id
    submit_sell_order: Callable[..., BrokerOrder]
    refresh_orderable_quantity: Callable[[str, str, str], int]  # (env, account, symbol) -> qty
    # Finds this card's currently-working exit (SELL) order, if any -- used
    # by callers (src.services.trading_engine) that need to cancel a
    # specific in-flight partial-sell order before escalating to a full
    # liquidation (section 671-697's "stop triggered during partial sell").
    # Optional/defaulted so existing callers that never need it are
    # unaffected.
    find_open_sell_order: Callable[[TradeCardState], Optional[BrokerOrder]] = (
        lambda card: None
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
        Steps 3-7 use the injected callbacks; step 8 ("continue until flat")
        is the caller's heartbeat re-invoking this engine, not a loop here.
        """
        card.exit_all_required = True
        if working_partial_sell_client_order_id:
            callbacks.cancel_order(working_partial_sell_client_order_id)  # step 3
        remaining = callbacks.refresh_orderable_quantity(  # steps 4 & 6
            card.environment, card.account_no, card.symbol
        )
        card.broker_quantity = remaining
        card.orderable_quantity = remaining
        card.pending_partial_sell_quantity = 0
        card.board_status = BoardStatus.SELL_ALL
        card.position_runtime_status = PositionRuntimeStatus.LIQUIDATING
        if remaining > 0:
            callbacks.submit_sell_order(  # step 7
                environment=card.environment,
                account_no=card.account_no,
                symbol=card.symbol,
                quantity=remaining,
                reason="stop_triggered_during_partial_sell",
            )
        return card

    # --- Sell All (section 18) -------------------------------------------

    def start_sell_all(
        self,
        card: TradeCardState,
        *,
        callbacks: PositionActionCallbacks,
        working_buy_client_order_id: Optional[str] = None,
    ) -> TradeCardState:
        """Section 699-709 steps 1-6. Reprice/retry-until-flat (step 8) and
        the Closed transition (step 9, via :meth:`confirm_flat`) are driven
        by the caller re-invoking this engine on subsequent heartbeats, not
        a loop in this single call.
        """
        card.exit_all_required = True
        if working_buy_client_order_id:
            callbacks.cancel_order(working_buy_client_order_id)  # step 3
        remaining = callbacks.refresh_orderable_quantity(  # steps 4-5
            card.environment, card.account_no, card.symbol
        )
        card.broker_quantity = remaining
        card.orderable_quantity = remaining
        card.board_status = BoardStatus.SELL_ALL
        card.position_runtime_status = PositionRuntimeStatus.LIQUIDATING
        if remaining > 0:
            callbacks.submit_sell_order(  # step 6
                environment=card.environment,
                account_no=card.account_no,
                symbol=card.symbol,
                quantity=remaining,
                reason="sell_all",
            )
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
        card.exit_all_required = False
        card.sell_all_at_market_open = False
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
            # -- section 620 forbids inventing one, so this starts at a
            # conservative manual stop pinned to cost until the user (or a
            # later SetBreakevenStop/SetManualStop command) adjusts it.
            card.stop_type = StopType.MANUAL_PRICE
            card.active_stop_price = average_entry_price
            card.stop_quantity = broker_quantity
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

        Broker truth always wins. Returns every card that was created or
        mutated so the caller can persist them.
        """
        changed: List[TradeCardState] = []
        holdings_by_symbol = {holding.symbol: holding for holding in holdings}
        cards_by_symbol = {
            card.symbol: card
            for card in cards
            if card.environment == environment and card.account_no == account_no
        }

        for symbol, holding in holdings_by_symbol.items():
            card = cards_by_symbol.get(symbol)
            already_correct = (
                card is not None
                and card.board_status == BoardStatus.OPEN_POSITION
                and card.broker_quantity == holding.quantity
            )
            if already_correct:
                continue
            updated = self.discover_manual_position(
                card,
                environment=environment,
                account_no=account_no,
                symbol=symbol,
                name=symbol_name_lookup(symbol) if card is None else card.name,
                broker_quantity=holding.quantity,
                average_entry_price=holding.average_price,
            )
            changed.append(updated)

        # Section 14 "manual sale discovered": a card believes it holds
        # shares the broker no longer reports at all.
        for symbol, card in cards_by_symbol.items():
            if symbol in holdings_by_symbol:
                continue
            if card.broker_quantity > 0:
                self.reconcile_manual_exit(card, broker_quantity=0)
                changed.append(card)

        return changed
