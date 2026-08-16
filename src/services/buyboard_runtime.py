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
- The production SELL adapter (review finding P0-3): ``submit_sell_order``
  maps trading_engine.py's ``reason`` string to a real ``OrderIntent`` and
  prices the order from a live quote -- the original version forwarded
  ``reason`` straight into ``submit_guarded_overseas_order``, which does
  not accept it and has no default for the required
  ``side``/``intent``/``limit_price``, so every real Partial Sell/Sell All
  would have raised ``TypeError``.
- A single, consistent entry ``plan_id`` (review finding P0-4) shared by
  the risk decision and the actual submission -- previously
  ``_revalidate_and_approve`` and the ``submit_order`` wrapper computed two
  *different* values, so the pre-trade gate's exact-fingerprint check would
  have rejected every real entry.
- Cumulative, broker-truth fill accounting (review finding P0-5): a second
  or third entry attempt's fill is now read back from
  ``broker.get_positions()`` (``EntryDeadlineLookup.refresh_broker_position``)
  instead of being compared against/overwriting the running total from a
  previous attempt.
- Real, holiday-aware NYSE market-session hooks (review finding P0-8) via
  :mod:`src.utils.market_calendar`, instead of ``TradingEngine``'s
  always-open/never-EOD test defaults.
- Capital reservations against the shared database
  (:mod:`src.services.capital_reservation_repository`), threaded into both
  ``EntryAttemptManager`` and ``EodTradingService`` (review finding P1-1) --
  previously accepted as a parameter here but never actually passed
  anywhere.
- A lightweight pre-trade risk revalidation built from the card's own
  persisted ORB fields (``entry_trigger``/``stop_adr``/``breakout_price``),
  reusing :mod:`src.risk.orb_position`'s existing bounds
  (``is_orb_position_plan_valid``) -- every ENTRY submission still requires
  and receives a fresh, order-fingerprint-bound
  :class:`~src.risk.pre_trade.PreTradeRiskDecision` (section 149-164's
  gate remains enforced, nothing bypasses it) -- and re-sizes the
  submitted quantity down (never up) to what's actually safe at the live
  price really being submitted (review finding P1-9), not the stale share
  count computed against the original ORB trigger price.
- Main-device lease fencing via the existing
  :class:`~src.services.execution_authority.ExecutionAuthority` /
  :class:`~src.services.execution_authority.LeaseHandle`, exactly like the
  legacy Buy Dashboard's order submission already does.

What production activation still needs to supply
-------------------------------------------------
- ``buying_power_provider``/``account_equity_provider``: wired as of review
  finding P0-1's fix -- ``src.ui.main_window._sync_buyboard_runtime_worker``
  now passes :func:`src.services.buying_power_cache.make_buying_power_provider`/
  ``make_account_equity_provider`` rather than the old manual/hardcoded
  account-size figure. This module still does not invent a new synchronous
  KIS balance query itself: the legacy dashboard's ``KisAccountWorker``
  (src/ui/workers.py) fetches account balance asynchronously as it always
  has, and ``DashboardMixin.apply_cached_trade_account_size`` records each
  fresh snapshot into that cache the moment it arrives. The providers built
  from that cache are per-account (unlike the figure they replaced) and
  fail closed -- return 0.0 -- whenever no snapshot has been recorded for
  the exact ``(environment, account_no)`` being asked about, or the
  snapshot is older than ``DEFAULT_MAX_SNAPSHOT_AGE_SECONDS`` (15s). This
  means capital is never reserved off a stale or wrong-account balance, but
  it also means the cache must actually be warm before this engine can size
  or reserve anything for an account -- the account's KIS balance should be
  loaded (Watchlist tab's "Use KIS Balance", or an equivalent periodic
  refresh) before relying on automatic entries for that account.
- Credentialed Workstream 0 sign-off for the KIS WebSocket symbol keys,
  capacities, timestamp semantics, sequencing, and execution-notice shape.
  PR4 contains the transport and runtime integration, but its live factory
  refuses to start until that evidence is recorded and
  ``KIS_WS_PROTOCOL_VERIFIED=true``. The REST minute-bar fallback is now
  display/diagnostic only and cannot authorize automatic execution.
- Running the assembled ``TradingEngine``/``RestPollingMarketDataService``
  on a background thread (mirroring the existing ``KisOrderWorker``/
  ``KisAccountWorker`` ``QThread`` pattern), not the UI thread -- every
  callback here performs real KIS network I/O. ``src.ui.buyboard.runtime_worker``
  now provides that thread and is handed the real, cache-backed
  ``buying_power_provider`` described above.
- An ORB-evaluation caller: this module does not itself recompute ORB
  candidates for BUY_TODAY/WATCHLIST/BUYLIST cards --
  :mod:`src.services.trade_card_orb_bridge`'s ``TradeCardOrbEvaluator``
  (review finding P0-2) needs to be invoked, on some cadence, with the
  ``ExecutionQueueItem`` the legacy execution queue already recomputes, so
  a card can ever actually reach ``EXECUTE_READY``.

None of this is activated automatically: constructing a
:class:`BuyboardRuntime` does not start anything, and nothing in
``src/ui/main_window.py`` constructs one unless
:func:`src.core.execution_config.is_buyboard_engine_enabled` is true.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional
from uuid import uuid4

from sqlalchemy.engine import Engine

from src.core import execution_config
from src.core.execution_mode import ExecutionLease, ExecutionMode, ExecutionSource
from src.core.execution_request import (
    CancelIntent,
    derive_execution_client_order_id,
)
from src.core.execution_order_record import ExecutionOrderStatus
from src.core.execution_result import broker_order_from_execution_record
from src.core.order_state import (
    BrokerOrder,
    OrderIntent,
    OrderSide,
    REGULAR_LIMIT_EXECUTION,
    is_open_status,
)
from src.core.trade_card_state import TradeCardState
from src.risk.orb_position import calculate_orb_position_values, is_orb_position_plan_valid
from src.risk.pre_trade import PreTradeRiskDecision
from src.services import capital_allocator
from src.services import order_ledger
from src.services import order_reconciliation
from src.services.broker import Broker
from src.services.execution_command_gateway import (
    ExecutionCommandGateway,
    get_default_execution_gateway,
)
from src.services.execution_workflow_service import request_cancel_intent, request_submit
from src.services.execution_order_repository import (
    fetch_execution_order,
    list_execution_orders_for_card,
)
from src.services.entry_attempt_manager import EntryAttemptManager
from src.services.eod_trading_service import EodActionCallbacks, EodTradingService
from src.services.execution_authority import ExecutionAuthority, LeaseHandle
from src.services.intraday_data_service import (
    ExecutionGradeDataUnavailableError,
    fetch_execution_grade_intraday,
)
from src.services.intraday_provider import IntradayInterval, IntradayRequest
from src.services.kis_realtime_market_data import (
    build_kis_realtime_market_data_from_environment,
)
from src.services.position_manager import (
    BrokerHolding,
    PositionActionCallbacks,
    PositionManager,
    extract_overseas_holdings,
)
from src.services.realtime_market_data import (
    QuoteSnapshot,
    RealtimeMarketDataService,
    RestPollingMarketDataService,
)
from src.services.trading_engine import EntryDeadlineLookup, TradingEngine
from src.utils.market_calendar import (
    is_regular_session_open,
    seconds_until_regular_session_close,
)

logger = logging.getLogger(__name__)

RISK_STRATEGY_ID = "ORB_KANBAN"

# Review finding P0-3: trading_engine.py's submit_sell_order(...) calls pass
# a "reason" string (never a submit_guarded_overseas_order keyword) instead
# of side/intent/limit_price. This maps that reason to the correct
# OrderIntent for the adapter below.
_SELL_REASON_TO_INTENT = {
    "partial_sell": OrderIntent.PARTIAL_EXIT,
    "sell_all": OrderIntent.MANUAL_EXIT,
    "sell_all_retry": OrderIntent.MANUAL_EXIT,
    "stop_loss": OrderIntent.STOP_LOSS,
}


def _entry_plan_id(card: TradeCardState) -> str:
    """Built once, used for *both* the risk decision and the actual
    submission (review finding P0-4). The pre-trade gate requires an exact
    fingerprint match including ``plan_id`` -- previously
    ``_revalidate_and_approve`` built one value here while
    ``build_buyboard_runtime``'s ``submit_order`` wrapper independently
    built a *different* one (``f"{environment}:{symbol}"``, missing the
    ORB window) for the actual submission, so every entry would have been
    rejected with "Pre-trade risk approval does not match the requested
    order" the first time this ran against a real order.
    """
    return f"{card.environment}:{card.symbol}:{card.selected_orb_window or 'unknown'}"


def _marketable_sell_limit_price(
    quote: Optional[QuoteSnapshot],
    *,
    quote_is_execution_ready: bool = True,
    last_trusted_price: Optional[float] = None,
    emergency_reprice_attempt: int = 0,
) -> Optional[float]:
    """A bounded marketable SELL limit below a fresh bid, else a small
    discount off the last trusted trade price -- mirrors the existing legacy Buy
    Dashboard's ``src.ui.buylist.constants.STOP_LOSS_SELL_LIMIT_DISCOUNT_PCT``
    approach (same number, re-declared as
    ``execution_config.SELL_MARKETABLE_DISCOUNT_PCT`` rather than importing
    across the services/ui boundary).
    """
    if quote_is_execution_ready and quote is not None and quote.bid:
        # The bid is the reference, while the configured collar provides a
        # bounded chance of execution if the top of book moves before the
        # limit reaches the broker.  This remains a limit order, never an
        # unbounded market order.
        return float(quote.bid) * (
            1.0 - execution_config.SELL_MARKETABLE_DISCOUNT_PCT
        )
    reference = last_trusted_price
    if reference is None and quote_is_execution_ready and quote is not None:
        reference = quote.last_price
    if reference:
        # Bounded, controlled collar widening. The caller enforces the hard
        # attempt cap; no unbounded market order is assumed safe.
        discount = execution_config.SELL_MARKETABLE_DISCOUNT_PCT * max(
            1, emergency_reprice_attempt + 1
        )
        discount = min(discount, 0.05)
        return float(reference) * (1.0 - discount)
    return None


def _eod_window_reached() -> bool:
    seconds_left = seconds_until_regular_session_close()
    return 0 <= seconds_left <= execution_config.EOD_ENTRY_CLEANUP_SECONDS_BEFORE_CLOSE


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


def _cancel_order(intent: CancelIntent, *, broker: Broker) -> None:
    # Workstream 9 (PR2 third pass, finding 1): route through the shared
    # workflow service, not order_reconciliation directly -- request_cancel
    # is the one entry point both legacy and Kanban use (INV-21). In
    # LEGACY_COMPATIBILITY delegates to cancel_and_reconcile_order through
    # the gateway adapter; GUARDED_ENGINE consumes the full CancelIntent.
    try:
        request_cancel_intent(intent, gateway=broker)
    except ValueError as exc:
        # Already terminal / already cancelled / not found -- the caller
        # (entry_attempt_manager/position_manager) only ever calls this on
        # an order it believes is still open; a stale belief is not a bug
        # here, just logged so it's visible.
        logger.info("Cancel request for %s was a no-op: %s", intent.client_order_id, exc)




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


def _refresh_broker_position(card: TradeCardState, *, broker: Broker) -> Optional[BrokerHolding]:
    """The preferred (review finding P0-5) source for a card's *cumulative*
    position after any fill -- broker truth is correct across any number of
    entry attempts by construction, unlike comparing/accumulating a single
    order's ``filled_quantity``. Wired into
    ``EntryDeadlineLookup.refresh_broker_position`` below.
    """
    snapshot = broker.get_positions(environment=card.environment, account_no=card.account_no)
    for holding in extract_overseas_holdings(snapshot):
        if holding.symbol == card.symbol:
            return holding
    return None


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

    plan_id = _entry_plan_id(card)
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
    reconciliation_cancel_order: Callable[[TradeCardState, str], Any]
    reconciliation_emergency_sell: Callable[[TradeCardState, int], Any]


def build_buyboard_runtime(
    *,
    buying_power_provider: Callable[[str, str], float],
    card_lookup: Callable[[str, str, str], Optional[TradeCardState]],
    account_equity_provider: Optional[Callable[[str, str], float]] = None,
    capital_reservation_engine: Optional[Engine] = None,
    execution_authority: Optional[ExecutionAuthority] = None,
    execution_lease: Optional[Any] = None,
    lease_engine=None,
    broker: Optional[Broker] = None,
    market_data: Optional[RealtimeMarketDataService] = None,
    strategy_instance_id: str = "",
    persist_card_before_execution: Optional[Callable[[TradeCardState], None]] = None,
) -> BuyboardRuntime:
    """Assembles every engine piece with real callback implementations.

    ``strategy_instance_id`` identifies *this* engine instance for H1's
    persisted execution-ownership check (Workstream 9) -- every
    submission/cancellation/replace this runtime makes uses
    ``ExecutionSource.KANBAN_BOARD`` plus this ``strategy_instance_id``.
    Required (non-blank) for ``GUARDED_ENGINE`` composition and checked
    against every KANBAN-owned symbol. It is irrelevant in
    ``LEGACY_COMPATIBILITY`` mode.

    ``buying_power_provider``/``card_lookup`` are the two seams this module
    cannot responsibly fill in itself (see the module docstring) --
    everything else is wired to existing, already-tested infrastructure.
    Construction alone does not start anything; the caller is responsible
    for driving ``trading_engine.run_heartbeat(...)``/``evaluate_quote(...)``
    from a background thread (never the UI thread -- every callback here
    performs real network I/O) and persisting the cards it returns via
    :mod:`src.services.trade_card_repository`.

    ``account_equity_provider`` (review finding P1-9) is the *risk-sizing*
    base (total account equity) as opposed to ``buying_power_provider``'s
    *capital-availability* base (spendable cash) -- they can differ once
    existing positions are marked. Defaults to ``buying_power_provider``
    when not supplied separately, which is not a new limitation: the
    legacy Buy Dashboard's own ORB sizing
    (``src.ui.mixins.dashboard_mixin``'s ``manual_account_sizes``) already
    uses one manually-maintained figure for both today.

    ``capital_reservation_engine``, when supplied, makes capital
    reservations visible across devices (review finding P1-1) -- threaded
    into both :class:`~src.services.entry_attempt_manager.EntryAttemptManager`
    and :class:`~src.services.eod_trading_service.EodTradingService`, which
    previously accepted this parameter but never actually used it.
    """
    # Workstream 9 (PR2): the default broker is the shared execution
    # gateway, not a raw KisBroker -- this module's own broker calls
    # (submit via submit_guarded_overseas_order, cancel via
    # order_reconciliation.cancel_and_reconcile_order and the direct
    # discovered-order cancel below) now route through the same single
    # mutation boundary the legacy Buy Dashboard uses. This composition
    # remains inert until its caller drives the heartbeat;
    # the feature flag selects which gateway mode is permitted here.
    #
    # Fourth pass: enabled mode accepts only a fully configured guarded
    # gateway and rejects every plain-broker/legacy-mode downgrade at this
    # composition boundary. Missing lease, epoch verification, mutation
    # budget, buying-power validation, durable card persistence, or
    # strategy identity therefore fails at startup, before a callback can
    # reach the broker.
    engine_enabled = execution_config.is_buyboard_engine_enabled()
    if engine_enabled:
        if not isinstance(broker, ExecutionCommandGateway):
            raise RuntimeError(
                "BUYBOARD_ENGINE_ENABLED=true accepts only an ExecutionCommandGateway "
                "in GUARDED_ENGINE mode; plain broker overrides cannot downgrade execution"
            )
        if broker.mode != ExecutionMode.GUARDED_ENGINE:
            raise RuntimeError(
                "BUYBOARD_ENGINE_ENABLED=true requires gateway.mode=GUARDED_ENGINE"
            )
        broker.require_guarded_runtime_ready()
        if not isinstance(execution_lease, ExecutionLease):
            raise RuntimeError(
                "BUYBOARD_ENGINE_ENABLED=true requires an epoch-bearing ExecutionLease"
            )
        if not str(strategy_instance_id or "").strip():
            raise RuntimeError(
                "BUYBOARD_ENGINE_ENABLED=true requires strategy_instance_id"
            )
        if persist_card_before_execution is None:
            raise RuntimeError(
                "BUYBOARD_ENGINE_ENABLED=true requires durable card persistence before execution"
            )
        resolved_broker = broker
    else:
        if isinstance(broker, ExecutionCommandGateway) and broker.mode != ExecutionMode.LEGACY_COMPATIBILITY:
            raise RuntimeError(
                "BUYBOARD_ENGINE_ENABLED=false cannot compose a GUARDED_ENGINE gateway"
            )
        resolved_broker = broker if broker is not None else get_default_execution_gateway()
    guarded_mode = (
        isinstance(resolved_broker, ExecutionCommandGateway)
        and resolved_broker.mode == ExecutionMode.GUARDED_ENGINE
    )
    guarded_lease = execution_lease if isinstance(execution_lease, ExecutionLease) else None
    legacy_lease = execution_lease if isinstance(execution_lease, LeaseHandle) else None
    resolved_equity_provider = account_equity_provider or buying_power_provider

    def persist_execution_identity(card: TradeCardState) -> None:
        if guarded_mode:
            assert persist_card_before_execution is not None
            persist_card_before_execution(card)

    def prepare_entry_identity(
        card: TradeCardState, *, attempt_group_id: str, attempt_number: int
    ) -> tuple[str, str, int]:
        if card.entry_client_order_id:
            return (
                card.entry_client_order_id,
                card.entry_attempt_group_id or attempt_group_id,
                card.entry_pending_attempt_number or attempt_number,
            )
        group_id = card.entry_attempt_group_id or attempt_group_id or uuid4().hex
        number = max(1, int(attempt_number or card.entry_attempt_count + 1))
        card.entry_attempt_group_id = group_id
        card.entry_pending_attempt_number = number
        card.entry_client_order_id = derive_execution_client_order_id(
            attempt_group_id=group_id,
            attempt_number=number,
            environment=card.environment,
            account_no=card.account_no,
            symbol=card.symbol,
            intent=OrderIntent.ENTRY,
        )
        card.entry_submission_unresolved = False
        persist_execution_identity(card)
        return card.entry_client_order_id, group_id, number

    def prepare_entry_attempt(card: TradeCardState) -> None:
        prepare_entry_identity(
            card,
            attempt_group_id=card.entry_attempt_group_id,
            attempt_number=card.entry_pending_attempt_number or card.entry_attempt_count + 1,
        )

    def prepare_exit_identity(
        card: TradeCardState, *, intent: OrderIntent
    ) -> tuple[str, str, int]:
        if card.exit_client_order_id:
            return (
                card.exit_client_order_id,
                card.exit_attempt_group_id,
                card.exit_pending_attempt_number or max(1, card.exit_attempt_count + 1),
            )
        group_id = card.exit_attempt_group_id or uuid4().hex
        number = max(1, card.exit_attempt_count + 1)
        card.exit_attempt_group_id = group_id
        card.exit_pending_attempt_number = number
        card.exit_client_order_id = derive_execution_client_order_id(
            attempt_group_id=group_id,
            attempt_number=number,
            environment=card.environment,
            account_no=card.account_no,
            symbol=card.symbol,
            intent=intent,
        )
        card.exit_submission_unresolved = False
        persist_execution_identity(card)
        return card.exit_client_order_id, group_id, number

    def cancel_intent_factory(
        card: TradeCardState, client_order_id: str, scope: str
    ) -> CancelIntent:
        field_name = (
            "entry_cancel_command_id" if str(scope).upper() == "ENTRY"
            else "exit_cancel_command_id"
        )
        cancel_command_id = getattr(card, field_name)
        if not cancel_command_id:
            cancel_command_id = f"{client_order_id}:CANCEL:{uuid4().hex}"
            setattr(card, field_name, cancel_command_id)
            persist_execution_identity(card)
        return CancelIntent(
            client_order_id=client_order_id,
            cancel_command_id=cancel_command_id,
            environment=card.environment,
            account_no=card.account_no,
            lease=guarded_lease,
            strategy_instance_id=strategy_instance_id,
            source=ExecutionSource.KANBAN_BOARD,
        )

    def submit_order(**kwargs):
        environment = kwargs["environment"]
        account_no = kwargs["account_no"]
        symbol = kwargs["symbol"]
        card = card_lookup(environment, account_no, symbol)
        account_size = buying_power_provider(environment, account_no)

        quantity = kwargs["quantity"]
        limit_price = kwargs["limit_price"]
        # card.risk_percent is a *fraction* (e.g. 0.01 for 1%), matching
        # calculate_orb_position_values' own validation
        # (risk_fraction <= 1.0) and how the legacy dashboard already
        # stores it (risk_percent / 100.0 at input time) -- an unset/zero
        # value has no trustworthy risk budget to resize against, so the
        # resize is skipped entirely rather than guessing a fallback
        # percentage (a wrong guess here would either do nothing or size a
        # real order off a fabricated risk budget).
        if (
            card is not None
            and kwargs.get("intent") == OrderIntent.ENTRY
            and card.entry_orb_low
            and card.risk_percent
            and card.risk_percent > 0
        ):
            # Review finding P1-9: planned_quantity/target_position_quantity
            # were sized off the original ORB trigger price, but the entry
            # engine may submit at a higher, more-marketable live price
            # (P0-9) -- resize down (never up) to what's actually safe at
            # the price really being submitted, using the same
            # calculate_orb_position_values the legacy dashboard's sizing
            # already uses, rather than trusting a share count computed
            # against a since-moved price.
            equity = resolved_equity_provider(environment, account_no)
            sizing = calculate_orb_position_values(
                account_size=equity,
                risk_percent=card.risk_percent,
                entry_price=limit_price,
                stop_price=card.entry_orb_low,
                adr_percent=None,
            )
            safe_shares = int(sizing.get("shares", 0) or 0)
            if safe_shares > 0:
                quantity = min(quantity, safe_shares)

        decision = (
            _revalidate_and_approve(
                card,
                quantity=quantity,
                limit_price=limit_price,
                exchange=kwargs.get("exchange", "NASD"),
                account_size=account_size,
            )
            if card is not None
            else None
        )
        plan_id = _entry_plan_id(card) if card is not None else f"{environment}:{symbol}"
        submit_kwargs = dict(kwargs)
        submit_kwargs["quantity"] = quantity
        if guarded_mode and not submit_kwargs.get("client_order_id"):
            if card is None:
                raise RuntimeError(
                    f"No durable TradeCardState exists for guarded entry {environment}/{account_no}/{symbol}"
                )
            client_order_id, attempt_group_id, attempt_number = prepare_entry_identity(
                card,
                attempt_group_id=submit_kwargs.get("attempt_group_id", ""),
                attempt_number=submit_kwargs.get("attempt_number", 1),
            )
            submit_kwargs["client_order_id"] = client_order_id
            submit_kwargs["attempt_group_id"] = attempt_group_id
            submit_kwargs["attempt_number"] = attempt_number
        # Workstream 9 (PR2 third pass, finding 1): route through the
        # shared workflow service, not submit_guarded_overseas_order
        # directly -- request_submit is the one entry point both legacy
        # and Kanban use (INV-21). It normalizes the two persistence models
        # into one workflow result while preserving this function's risk
        # revalidation and gate sequence.
        return request_submit(
            source=ExecutionSource.KANBAN_BOARD,
            gateway=resolved_broker,
            strategy_instance_id=strategy_instance_id,
            lease=guarded_lease,
            pre_trade_risk_decision=decision,
            strategy_id=RISK_STRATEGY_ID,
            plan_id=plan_id,
            execution_authority=execution_authority,
            execution_lease=legacy_lease,
            lease_engine=lease_engine,
            **submit_kwargs,
        )

    authoritative_reservation_engine = (
        resolved_broker.database_engine
        if guarded_mode and isinstance(resolved_broker, ExecutionCommandGateway)
        else capital_reservation_engine
    )
    entry_attempt_manager = EntryAttemptManager(
        buying_power_provider=buying_power_provider,
        submit_order=submit_order,
        capital_reservation_engine=authoritative_reservation_engine,
        gateway_owns_capital_reservation=guarded_mode,
    )

    position_manager = PositionManager()

    guarded_open_statuses = {
        ExecutionOrderStatus.PREPARED,
        ExecutionOrderStatus.SUBMITTING,
        ExecutionOrderStatus.ACKNOWLEDGED,
        ExecutionOrderStatus.WORKING,
        ExecutionOrderStatus.PARTIALLY_FILLED,
        ExecutionOrderStatus.CANCEL_PENDING,
        ExecutionOrderStatus.UNKNOWN_SUBMISSION_STATE,
    }

    def find_runtime_order(
        card: TradeCardState,
        *,
        side: OrderSide,
        intent: Optional[OrderIntent] = None,
    ) -> Optional[BrokerOrder]:
        if not guarded_mode:
            return _find_open_order(
                environment=card.environment,
                account_no=card.account_no,
                symbol=card.symbol,
                side=side,
                intent=intent,
            )
        assert isinstance(resolved_broker, ExecutionCommandGateway)
        engine = resolved_broker.database_engine
        assert engine is not None
        for record in list_execution_orders_for_card(
            engine,
            environment=card.environment,
            account_no=card.account_no,
            symbol=card.symbol,
        ):
            if record.status not in guarded_open_statuses or record.side != side:
                continue
            if intent is not None and record.intent != intent:
                continue
            return broker_order_from_execution_record(record)
        return None

    def reconcile_runtime_order(order: BrokerOrder) -> BrokerOrder:
        if not guarded_mode:
            return _reconcile_order(order, broker=resolved_broker)
        assert isinstance(resolved_broker, ExecutionCommandGateway)
        engine = resolved_broker.database_engine
        assert engine is not None
        record = fetch_execution_order(engine, order.client_order_id)
        return broker_order_from_execution_record(record) if record is not None else order

    entry_deadline_lookup = EntryDeadlineLookup(
        find_open_entry_order=lambda card: find_runtime_order(
            card, side=OrderSide.BUY, intent=OrderIntent.ENTRY
        ),
        reconcile_order=reconcile_runtime_order,
        refresh_broker_position=lambda card: _refresh_broker_position(card, broker=resolved_broker),
        persist_order=order_ledger.upsert_order,
    )

    def submit_sell_order(**kwargs):
        """Adapter for trading_engine.py's submit_sell_order(...) calls
        (review finding P0-3): those calls pass
        environment/account_no/symbol/quantity/reason -- ``reason`` is not
        a ``submit_guarded_overseas_order`` keyword, and that function also
        requires ``side``/``intent``/``limit_price``, which were never
        supplied here before. This builds all three from what actually
        arrives: side is always SELL, intent comes from the reason
        (``_SELL_REASON_TO_INTENT``), and limit_price comes from the live
        quote (falling back to a small discount off the last trade when no
        bid is cached yet).
        """
        submit_kwargs = dict(kwargs)
        supplied_card = submit_kwargs.pop("trade_card", None)
        reason = submit_kwargs.pop("reason", "sell_all")
        intent = _SELL_REASON_TO_INTENT.get(reason, OrderIntent.MANUAL_EXIT)
        symbol = submit_kwargs["symbol"]
        card = supplied_card or card_lookup(
            submit_kwargs.get("environment", ""),
            submit_kwargs.get("account_no", ""),
            symbol,
        )
        quote = resolved_market_data.latest_quote(symbol)
        quote_ready = resolved_market_data.is_symbol_execution_ready(
            symbol, require_trade=False, require_quote=True
        )
        emergency_attempt = int(card.exit_attempt_count if card is not None else 0)
        if (
            not quote_ready
            and emergency_attempt
            >= execution_config.EMERGENCY_EXIT_MAX_REPRICE_ATTEMPTS
        ):
            raise ExecutionGradeDataUnavailableError(
                f"Emergency SELL collar retry limit reached for {symbol}; manual intervention required"
            )
        limit_price = _marketable_sell_limit_price(
            quote,
            quote_is_execution_ready=quote_ready,
            last_trusted_price=(
                card.market_data_last_trusted_price if card is not None else None
            ),
            emergency_reprice_attempt=emergency_attempt,
        )
        if limit_price is None:
            raise ExecutionGradeDataUnavailableError(
                f"No fresh bid or last trusted price is available to price a bounded SELL for {symbol}"
            )
        exchange = submit_kwargs.pop("exchange", "NASD")
        if guarded_mode:
            if card is None:
                raise RuntimeError(
                    f"No durable TradeCardState exists for guarded exit {symbol}"
                )
            client_order_id, attempt_group_id, attempt_number = prepare_exit_identity(
                card, intent=intent
            )
            submit_kwargs["client_order_id"] = client_order_id
            submit_kwargs["attempt_group_id"] = attempt_group_id
            submit_kwargs["attempt_number"] = attempt_number
        # Workstream 9 (PR2 third pass, finding 1): route through the
        # shared workflow service -- see submit_order's identical comment
        # above for the shared guarded/compatibility contract.
        return request_submit(
            source=ExecutionSource.KANBAN_BOARD,
            gateway=resolved_broker,
            strategy_instance_id=strategy_instance_id,
            lease=guarded_lease,
            side=OrderSide.SELL,
            intent=intent,
            limit_price=limit_price,
            exchange=exchange,
            plan_id=f"{submit_kwargs.get('environment', '')}:{symbol}:SELL:{reason}",
            execution_authority=execution_authority,
            execution_lease=legacy_lease,
            lease_engine=lease_engine,
            **submit_kwargs,
        )

    position_callbacks = PositionActionCallbacks(
        cancel_order=lambda intent: _cancel_order(intent, broker=resolved_broker),
        submit_sell_order=submit_sell_order,
        refresh_orderable_quantity=lambda environment, account_no, symbol: _refresh_orderable_quantity(
            environment, account_no, symbol, broker=resolved_broker
        ),
        cancel_intent_factory=cancel_intent_factory,
        persist_cancel_state=persist_execution_identity,
        find_open_sell_order=lambda card: find_runtime_order(
            card, side=OrderSide.SELL
        ),
        reconcile_sell_order=reconcile_runtime_order,
    )

    def reconciliation_cancel_order(
        card: TradeCardState, client_order_id: str
    ) -> Any:
        record = (
            fetch_execution_order(resolved_broker.database_engine, client_order_id)
            if guarded_mode and isinstance(resolved_broker, ExecutionCommandGateway)
            else None
        )
        scope = "ENTRY" if record is not None and record.side == OrderSide.BUY else "EXIT"
        return position_callbacks.request_cancel(
            card, client_order_id, scope=scope
        )

    def reconciliation_emergency_sell(
        card: TradeCardState, quantity: int
    ) -> Any:
        deadline = datetime.now(timezone.utc) + timedelta(
            seconds=execution_config.SELL_ALL_ATTEMPT_TTL_SECONDS
        )
        return submit_sell_order(
            environment=card.environment,
            account_no=card.account_no,
            symbol=card.symbol,
            quantity=quantity,
            reason="sell_all_retry",
            attempt_deadline_at=deadline.isoformat(),
            trade_card=card,
        )

    eod_callbacks = EodActionCallbacks(
        find_open_entry_order=lambda card: find_runtime_order(
            card, side=OrderSide.BUY, intent=OrderIntent.ENTRY
        ),
        reconcile_order=reconcile_runtime_order,
        cancel_order=lambda intent: _cancel_order(intent, broker=resolved_broker),
        cancel_intent_factory=cancel_intent_factory,
        persist_cancel_state=persist_execution_identity,
    )
    eod_service = EodTradingService(
        entry_attempt_manager=entry_attempt_manager,
        position_manager=position_manager,
        callbacks=eod_callbacks,
        reservations_path=capital_allocator.RESERVATIONS_FILE,
        capital_reservation_engine=authoritative_reservation_engine,
    )

    resolved_market_data = market_data
    if resolved_market_data is None:
        if (
            execution_config.KIS_MARKET_DATA_MODE == "WEBSOCKET"
            and execution_config.KIS_WS_ENABLED
        ):
            resolved_market_data = build_kis_realtime_market_data_from_environment(
                environment="PROD"
            )
        else:
            resolved_market_data = RestPollingMarketDataService(
                quote_fetcher=_kis_only_quote_fetcher,
                # Minute-bar REST polling is display/diagnostic fallback
                # only. It must never authorize an entry or impersonate
                # tick-level stop protection.
                execution_grade=False,
            )

    trading_engine = TradingEngine(
        entry_attempt_manager=entry_attempt_manager,
        position_manager=position_manager,
        market_data=resolved_market_data,
        position_callbacks=position_callbacks,
        entry_deadline_lookup=entry_deadline_lookup,
        eod_service=eod_service,
        # Review finding P0-8: real, holiday-aware NYSE session hooks
        # instead of TradingEngine's always-open/never-EOD test defaults --
        # without these, premarket Sell All queuing never reliably fired,
        # a stop-triggered liquidation could attempt a regular-hours order
        # outside the session, and EOD cleanup never ran in production.
        market_is_open=is_regular_session_open,
        eod_window_reached=_eod_window_reached,
        prepare_entry_attempt=prepare_entry_attempt if guarded_mode else None,
        account_equity_provider=account_equity_provider,
        trading_halt_lookup=resolved_market_data.is_symbol_trading_halted,
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
        reconciliation_cancel_order=reconciliation_cancel_order,
        reconciliation_emergency_sell=reconciliation_emergency_sell,
    )
