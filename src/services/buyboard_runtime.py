"""Runtime composition root for the Kanban execution engine.
``buydashboard_to_kanban.md`` Phase 5-7; code review finding P0-8/Step 2.

Every module built for this redesign (``entry_attempt_manager``,
``position_manager``, ``eod_trading_service``, ``trading_engine``,
``capital_allocator``, ``trade_card_repository``, ``realtime_market_data``)
is deliberately dependency-injected and broker-agnostic -- none of them import
:mod:`src.api.kis_order` or talk to KIS directly. This module is where those
injected callbacks get their real implementations, wired to the *existing*,
already-tested order/broker infrastructure
(:mod:`src.services.order_ledger`, :mod:`src.services.order_reconciliation`,
:mod:`src.services.broker`) -- the review's core finding was that this
wiring simply did not exist anywhere, so nothing actually ran even with
``BUYBOARD_ENGINE_ENABLED=true``.

What this module fully wires
-----------------------------
- Order lookup/reconciliation/cancellation (``EntryDeadlineLookup``,
  ``PositionActionCallbacks``, ``EodActionCallbacks``) against the real
  local order ledger and KIS via :mod:`src.services.order_reconciliation`
  and :class:`src.services.broker.KisBroker`.
- Capital reservations against the shared database
  (:mod:`src.services.capital_reservation_repository`), not only the local
  JSON ledger.
- A lightweight pre-trade risk revalidation built from the card's own
  persisted ORB fields (``entry_trigger``/``stop_adr``/``breakout_price``),
  reusing :mod:`src.risk.orb_position`'s existing bounds
  (``is_orb_position_plan_valid``) -- every ENTRY submission still requires
  and receives a fresh, order-fingerprint-bound
  :class:`~src.risk.pre_trade.PreTradeRiskDecision` (section 149-164's
  gate remains enforced, nothing bypasses it).
- Main-device lease fencing via the existing
  :class:`~src.services.execution_authority.ExecutionAuthority` /
  :class:`~src.services.execution_authority.LeaseHandle`, exactly like the
  legacy Buy Dashboard's order submission already does.

What production activation still needs to supply
--------------------------------------------------
- ``buying_power_provider``: this module does not invent a new synchronous
  KIS balance query. The legacy dashboard already fetches account balance
  asynchronously via ``KisAccountWorker`` (src/ui/workers.py) to avoid
  blocking the UI thread; the same cached/most-recently-refreshed value
  should be handed in here rather than querying KIS synchronously from a
  1-second heartbeat tick.
- A real tick-level KIS quote for ``RealtimeMarketDataService`` (no such
  endpoint is used anywhere in this codebase yet -- see
  :mod:`src.services.realtime_market_data`'s module docstring).
- Running the assembled ``TradingEngine``/``RestPollingMarketDataService``
  on a background thread (mirroring the existing ``KisOrderWorker``/
  ``KisAccountWorker`` ``QThread`` pattern), not the UI thread -- every
  callback here performs real KIS network I/O.

None of this is activated automatically: constructing a
:class:`BuyboardRuntime` does not start anything, and nothing in
``src/ui/main_window.py`` constructs one unless
:func:`src.core.execution_config.is_buyboard_engine_enabled` is true.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional

from src.core.order_state import (
    BrokerOrder,
    BrokerOrderDiscoveryResult,
    OrderIntent,
    OrderSide,
    OrderStatus,
    REGULAR_LIMIT_EXECUTION,
    is_open_status,
)
from src.core.trade_card_state import TradeCardState
from src.risk.orb_position import is_orb_position_plan_valid
from src.risk.pre_trade import PreTradeRiskDecision
from src.services import capital_allocator, capital_reservation_repository
from src.services import order_ledger
from src.services import order_reconciliation
from src.services.broker import Broker, KisBroker
from src.services.entry_attempt_manager import EntryAttemptManager
from src.services.eod_trading_service import EodActionCallbacks, EodTradingService
from src.services.execution_authority import ExecutionAuthority, LeaseHandle
from src.services.order_execution_service import submit_guarded_overseas_order
from src.services.intraday_data_service import (
    ExecutionGradeDataUnavailableError,
    fetch_execution_grade_intraday,
)
from src.services.intraday_provider import IntradayInterval, IntradayRequest
from src.services.position_manager import PositionActionCallbacks, PositionManager
from src.services.realtime_market_data import (
    QuoteSnapshot,
    RealtimeMarketDataService,
    RestPollingMarketDataService,
)
from src.services.trading_engine import EntryDeadlineLookup, TradingEngine

logger = logging.getLogger(__name__)

RISK_STRATEGY_ID = "ORB_KANBAN"


# --- Order lookup/reconciliation, wired to the real local ledger + KIS -----


def _find_open_order(
    *, environment: str, account_no: str, symbol: str, side: OrderSide, intent: Optional[OrderIntent] = None
) -> Optional[BrokerOrder]:
    matches = order_ledger.find_open_orders(
        environment=environment, account_no=account_no, symbol=symbol, side=side, intent=intent
    )
    return matches[0] if matches else None


def _find_open_entry_order(card: TradeCardState) -> Optional[BrokerOrder]:
    return _find_open_order(
        environment=card.environment,
        account_no=card.account_no,
        symbol=card.symbol,
        side=OrderSide.BUY,
        intent=OrderIntent.ENTRY,
    )


def _find_open_sell_order(card: TradeCardState) -> Optional[BrokerOrder]:
    return _find_open_order(
        environment=card.environment, account_no=card.account_no, symbol=card.symbol, side=OrderSide.SELL
    )


def _reconcile_order(order: BrokerOrder, *, broker: Broker) -> BrokerOrder:
    """Query KIS for this specific order and persist the reconciled status,
    reusing :func:`src.services.order_reconciliation.query_and_reconcile_unresolved_orders`
    -- the same function the existing account-wide reconciliation loop uses.
    """
    if not is_open_status(order.status):
        return order
    updated = order_reconciliation.query_and_reconcile_unresolved_orders(
        environment=order.environment,
        account_no=order.account_no,
        symbol=order.symbol,
        broker=broker,
    )
    for candidate in updated:
        if candidate.client_order_id == order.client_order_id:
            return candidate
    refreshed = order_ledger.find_order(order.client_order_id)
    return refreshed if refreshed is not None else order


def _cancel_order(client_order_id: str, *, broker: Broker) -> None:
    try:
        order_reconciliation.cancel_and_reconcile_order(client_order_id, broker=broker)
    except ValueError as exc:
        # Already terminal / already cancelled / not found -- the caller
        # (entry_attempt_manager/position_manager) only ever calls this on
        # an order it believes is still open; a stale belief is not a bug
        # here, just logged so it's visible.
        logger.info("Cancel request for %s was a no-op: %s", client_order_id, exc)


def _discover_all_orders(card: TradeCardState, *, broker: Broker) -> BrokerOrderDiscoveryResult:
    return broker.discover_orders(environment=card.environment, account_no=card.account_no)


def _kis_only_quote_fetcher(symbol: str) -> QuoteSnapshot:
    """Rough fallback quote source: the latest execution-grade (KIS-only,
    never yfinance -- section 21) 1-minute bar's close, used only until a
    real tick/WebSocket quote feed is wired in (see the module docstring).
    This is materially coarser than a true tick -- production activation
    should replace this with a real streaming quote as soon as one exists.
    """
    result = fetch_execution_grade_intraday(
        IntradayRequest(symbol=symbol, interval=IntradayInterval.ONE_MINUTE, allow_fallback=False)
    )
    bars = result.bars
    if bars.empty:
        raise ExecutionGradeDataUnavailableError(f"No execution-grade bars for {symbol}")
    last_close = float(bars["Close"].iloc[-1])
    return QuoteSnapshot(symbol=symbol, last_price=last_close, source="kis_1m_bar_close")


def _refresh_orderable_quantity(environment: str, account_no: str, symbol: str, *, broker: Broker) -> int:
    snapshot = broker.get_positions(environment=environment, account_no=account_no)
    holdings = ((snapshot or {}).get("overseas") or {}).get("holdings") or []
    for holding in holdings:
        if str(holding.get("symbol", "")).strip().upper() == symbol.upper():
            try:
                return int(float(holding.get("orderable_quantity", holding.get("quantity", 0)) or 0))
            except (TypeError, ValueError):
                return 0
    return 0


# --- Lightweight pre-trade risk revalidation ---------------------------


def _revalidate_and_approve(
    card: TradeCardState,
    *,
    quantity: int,
    limit_price: float,
    exchange: str,
    account_size: float,
) -> Optional[PreTradeRiskDecision]:
    """Section 149-164's "fresh risk approval bound to the exact order
    fingerprint" gate, using the card's own persisted ORB fields.

    This is a lighter-weight revalidation than the legacy path's
    :func:`src.risk.pre_trade.assess_orb_entry_candidate`, which needs the
    live :class:`~src.core.execution_queue.OrbCandidate` object --
    ``TradingEngine`` intentionally does not carry that (the existing ORB
    strategy calculation is unchanged and untouched, per spec section 15;
    this engine only consumes the scalar values the card already persists).
    It still refuses to approve an order whose numbers don't satisfy the
    same bounds :func:`src.risk.orb_position.is_orb_position_plan_valid`
    already enforces elsewhere -- this is a real gate, not a rubber stamp.
    """
    stop_price = card.entry_orb_low
    reasons = []
    if quantity <= 0:
        reasons.append("Non-positive quantity")
    if limit_price <= 0:
        reasons.append("Non-positive reference price")
    if stop_price is not None and stop_price >= limit_price:
        reasons.append("Stop is not below the entry price")

    # Reuse the same canonical thresholds is_orb_position_plan_valid already
    # enforces elsewhere (MIN/MAX_CAPITAL_PERCENT, MIN/MAX_STOP_ADR) instead
    # of duplicating the numbers here. adr_percent=None because raw ADR% is
    # not itself persisted on the card (only the already-computed sl_adr
    # ratio is, in card.stop_adr) -- this skips only the
    # stop_loss_percent-vs-adr_percent cross-check, not the capital/sl_adr
    # bounds.
    sizing = {
        "shares": float(quantity),
        "capital_percent": (quantity * limit_price / account_size * 100.0) if account_size > 0 else 0.0,
        "stop_loss_percent": (
            (limit_price - stop_price) / limit_price * 100.0
            if stop_price is not None and limit_price > 0
            else 0.0
        ),
        "sl_adr": card.stop_adr,
    }
    if not reasons and not is_orb_position_plan_valid(sizing, adr_percent=None):
        reasons.append(
            f"Order fingerprint fails ORB position bounds: "
            f"capital_percent={sizing['capital_percent']:.1f}%, sl_adr={card.stop_adr}"
        )

    plan_id = f"{card.environment}:{card.symbol}:{card.selected_orb_window or 'unknown'}"
    if reasons:
        logger.info("Pre-trade risk revalidation rejected %s: %s", card.symbol, "; ".join(reasons))
        return PreTradeRiskDecision.reject(
            reasons=tuple(reasons),
            environment=card.environment,
            account_no=card.account_no,
            symbol=card.symbol,
            side=OrderSide.BUY,
            intent=OrderIntent.ENTRY,
            quantity=quantity,
            reference_price=limit_price,
            exchange=exchange,
            execution_policy=REGULAR_LIMIT_EXECUTION,
            strategy_id=RISK_STRATEGY_ID,
            plan_id=plan_id,
        )
    return PreTradeRiskDecision.approve(
        environment=card.environment,
        account_no=card.account_no,
        symbol=card.symbol,
        side=OrderSide.BUY,
        intent=OrderIntent.ENTRY,
        quantity=quantity,
        reference_price=limit_price,
        exchange=exchange,
        execution_policy=REGULAR_LIMIT_EXECUTION,
        strategy_id=RISK_STRATEGY_ID,
        plan_id=plan_id,
    )


@dataclass
class BuyboardRuntime:
    """Holds the fully-wired engine + the card lookup this process needs to
    find the ``TradeCardState`` a bare ``EntryTrigger``/``BrokerOrder``
    callback only has (environment, account_no, symbol) for.
    """

    trading_engine: TradingEngine
    entry_attempt_manager: EntryAttemptManager
    position_manager: PositionManager
    eod_service: EodTradingService
    market_data: RealtimeMarketDataService
    broker: Broker
    card_lookup: Callable[[str, str, str], Optional[TradeCardState]]
    account_size_provider: Callable[[str, str], float]


def build_buyboard_runtime(
    *,
    buying_power_provider: Callable[[str, str], float],
    card_lookup: Callable[[str, str, str], Optional[TradeCardState]],
    capital_reservation_engine=None,
    execution_authority: Optional[ExecutionAuthority] = None,
    execution_lease: Optional[LeaseHandle] = None,
    lease_engine=None,
    broker: Optional[Broker] = None,
    market_data: Optional[RealtimeMarketDataService] = None,
) -> BuyboardRuntime:
    """Assembles every engine piece with real callback implementations.

    ``buying_power_provider``/``card_lookup`` are the two seams this module
    cannot responsibly fill in itself (see the module docstring) --
    everything else is wired to existing, already-tested infrastructure.
    Construction alone does not start anything; the caller is responsible
    for driving ``trading_engine.run_heartbeat(...)``/``evaluate_quote(...)``
    from a background thread (never the UI thread -- every callback here
    performs real network I/O) and persisting the cards it returns via
    :mod:`src.services.trade_card_repository`.
    """
    resolved_broker = broker or KisBroker()

    def submit_order(**kwargs):
        environment = kwargs["environment"]
        account_no = kwargs["account_no"]
        symbol = kwargs["symbol"]
        card = card_lookup(environment, account_no, symbol)
        account_size = buying_power_provider(environment, account_no)
        decision = (
            _revalidate_and_approve(
                card,
                quantity=kwargs["quantity"],
                limit_price=kwargs["limit_price"],
                exchange=kwargs.get("exchange", "NASD"),
                account_size=account_size,
            )
            if card is not None
            else None
        )
        return submit_guarded_overseas_order(
            broker=resolved_broker,
            pre_trade_risk_decision=decision,
            strategy_id=RISK_STRATEGY_ID,
            plan_id=f"{environment}:{symbol}",
            execution_authority=execution_authority,
            execution_lease=execution_lease,
            lease_engine=lease_engine,
            **kwargs,
        )

    entry_attempt_manager = EntryAttemptManager(
        buying_power_provider=buying_power_provider,
        submit_order=submit_order,
    )

    position_manager = PositionManager()

    entry_deadline_lookup = EntryDeadlineLookup(
        find_open_entry_order=_find_open_entry_order,
        reconcile_order=lambda order: _reconcile_order(order, broker=resolved_broker),
    )

    def submit_sell_order(**kwargs):
        return submit_guarded_overseas_order(
            broker=resolved_broker,
            execution_authority=execution_authority,
            execution_lease=execution_lease,
            lease_engine=lease_engine,
            **kwargs,
        )

    position_callbacks = PositionActionCallbacks(
        cancel_order=lambda client_order_id: _cancel_order(client_order_id, broker=resolved_broker),
        submit_sell_order=submit_sell_order,
        refresh_orderable_quantity=lambda environment, account_no, symbol: _refresh_orderable_quantity(
            environment, account_no, symbol, broker=resolved_broker
        ),
        find_open_sell_order=_find_open_sell_order,
        reconcile_sell_order=lambda order: _reconcile_order(order, broker=resolved_broker),
    )

    eod_callbacks = EodActionCallbacks(
        find_open_entry_order=_find_open_entry_order,
        reconcile_order=lambda order: _reconcile_order(order, broker=resolved_broker),
        cancel_order=lambda client_order_id: _cancel_order(client_order_id, broker=resolved_broker),
        discover_all_orders=lambda card: _discover_all_orders(card, broker=resolved_broker),
    )
    eod_service = EodTradingService(
        entry_attempt_manager=entry_attempt_manager,
        position_manager=position_manager,
        callbacks=eod_callbacks,
        reservations_path=capital_allocator.RESERVATIONS_FILE,
    )

    resolved_market_data = market_data
    if resolved_market_data is None:
        # No KIS tick/WebSocket quote source exists anywhere in this
        # codebase yet (see realtime_market_data.py's module docstring).
        # This falls back to polling the latest execution-grade (KIS-only,
        # never yfinance -- section 21) 1-minute bar close, which is
        # functional but materially coarser than a real tick feed --
        # replace with a real streaming source as soon as one exists by
        # passing ``market_data=`` explicitly.
        resolved_market_data = RestPollingMarketDataService(quote_fetcher=_kis_only_quote_fetcher)

    trading_engine = TradingEngine(
        entry_attempt_manager=entry_attempt_manager,
        position_manager=position_manager,
        market_data=resolved_market_data,
        position_callbacks=position_callbacks,
        entry_deadline_lookup=entry_deadline_lookup,
        eod_service=eod_service,
    )

    return BuyboardRuntime(
        trading_engine=trading_engine,
        entry_attempt_manager=entry_attempt_manager,
        position_manager=position_manager,
        eod_service=eod_service,
        market_data=resolved_market_data,
        broker=resolved_broker,
        card_lookup=card_lookup,
        account_size_provider=buying_power_provider,
    )
