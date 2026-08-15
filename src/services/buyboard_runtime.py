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
--------------------------------------------------
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
- A real tick-level KIS quote for ``RealtimeMarketDataService`` (no such
  endpoint is used anywhere in this codebase yet -- see
  :mod:`src.services.realtime_market_data`'s module docstring). The
  fallback wired in here (``_kis_only_quote_fetcher``) polls the latest
  1-minute bar close, which is materially coarser than a real tick and is
  not sufficient for detecting an intraminute stop-loss touch that
  recovers before the bar closes.
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
from datetime import datetime, timezone
from typing import Callable, Optional

from sqlalchemy.engine import Engine

from src.core import execution_config
from src.core.order_state import (
    BrokerOrder,
    BrokerOrderDiscoveryResult,
    BrokerOrderStatusSnapshot,
    OrderIntent,
    OrderSide,
    OrderStatus,
    REGULAR_LIMIT_EXECUTION,
    is_open_status,
)
from src.core.trade_card_state import TradeCardState
from src.risk.orb_position import calculate_orb_position_values, is_orb_position_plan_valid
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


def _marketable_sell_limit_price(quote: Optional[QuoteSnapshot]) -> Optional[float]:
    """A marketable SELL limit: the live bid if one is cached (guaranteed
    at/above what a market order would clear at this instant), else a small
    discount off the last trade price -- mirrors the existing legacy Buy
    Dashboard's ``src.ui.buylist.constants.STOP_LOSS_SELL_LIMIT_DISCOUNT_PCT``
    approach (same number, re-declared as
    ``execution_config.SELL_MARKETABLE_DISCOUNT_PCT`` rather than importing
    across the services/ui boundary).
    """
    if quote is None:
        return None
    if quote.bid:
        return float(quote.bid)
    if quote.last_price:
        return float(quote.last_price) * (1.0 - execution_config.SELL_MARKETABLE_DISCOUNT_PCT)
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


def _exchange_from_snapshot(snapshot: BrokerOrderStatusSnapshot) -> str:
    """Best-effort KIS exchange-code recovery from a discovered snapshot's
    raw response row -- see :func:`_cancel_discovered_order`. Returns ""
    (never a guessed default) when it cannot be found, so the caller can
    fail closed instead of silently targeting the wrong exchange.
    """
    row = snapshot.raw_response or {}
    if not isinstance(row, dict):
        return ""
    for key in ("ovrs_excg_cd", "OVRS_EXCG_CD", "excg_cd", "EXCG_CD", "exchange"):
        for candidate in (key, key.lower(), key.upper()):
            value = row.get(candidate)
            if value:
                return str(value).strip().upper()
    return ""


def _cancel_discovered_order(
    card: TradeCardState, snapshot: BrokerOrderStatusSnapshot, *, broker: Broker
) -> None:
    """Best-effort direct cancel of a discovered order's unfilled remainder
    -- see :attr:`EodActionCallbacks.cancel_discovered_order`. Unlike
    :func:`_cancel_order`, there is no local :class:`BrokerOrder` record to
    look up or persist a reconciled result to (that is the entire point --
    this only runs when local tracking has already been lost), so this
    calls ``broker.cancel_order`` directly, keyed by the snapshot's own
    ``broker_order_id``, and never raises.

    Always uses the regular (non-reserved) cancel endpoint: discovery
    folds regular and reserved-MOO snapshots into one list with no flag
    marking which source an order came from, so the two genuinely
    different broker cancel calls (regular needs
    symbol/quantity/side/exchange; reserved needs only broker_order_id/
    reservation_date) can't be told apart from a snapshot alone in
    general. This is safe specifically here because reserved-MOO
    execution is exclusively a sell-side mechanism in this codebase
    (``KisBroker.submit_order`` only ever routes it to
    ``place_overseas_reserved_market_on_open_sell``) and every candidate
    reaching this function is a BUY entry order
    (``_find_matching_broker_entry_snapshot`` filters on
    ``side == OrderSide.BUY``), which is never submitted as reserved-MOO.

    Exchange is not a structured field on a discovered snapshot, but KIS's
    cancel endpoint requires one (``OVRS_EXCG_CD``) to route the request.
    Review finding: a hardcoded "NASD" default here would silently
    misroute (or simply fail) a cancel for a genuinely NYSE/AMEX order --
    the broader discovery code explicitly queries multiple exchanges, so
    this is not merely theoretical. Recovered from the snapshot's own raw
    KIS response row (``snapshot.raw_response``) via
    :func:`_exchange_from_snapshot` instead; if it cannot be determined,
    this refuses to guess and no-ops, matching this module's fail-closed
    pattern everywhere else automatic cancellation is involved.
    """
    broker_order_id = str(snapshot.broker_order_id or "").strip()
    if not broker_order_id:
        logger.warning(
            "Cannot cancel discovered order for %s: snapshot has no broker_order_id", card.symbol
        )
        return
    exchange = _exchange_from_snapshot(snapshot)
    if not exchange:
        logger.warning(
            "Cannot cancel discovered order for %s: exchange could not be determined from "
            "the discovered snapshot (broker_order_id=%s) -- refusing to guess",
            card.symbol, broker_order_id,
        )
        return
    remaining = snapshot.remaining_quantity or max(
        0, snapshot.quantity_requested - snapshot.filled_quantity
    )
    if remaining <= 0:
        remaining = snapshot.quantity_requested
    if remaining <= 0:
        logger.warning(
            "Cannot cancel discovered order for %s: no remaining quantity to cancel", card.symbol
        )
        return
    try:
        broker.cancel_order(
            environment=snapshot.environment,
            account_no=snapshot.account_no,
            is_reserved=False,
            symbol=snapshot.symbol,
            broker_order_id=broker_order_id,
            quantity=remaining,
            side=snapshot.side.value,
            exchange=exchange,
        )
    except Exception:
        logger.exception(
            "Direct cancel of discovered order failed for %s (broker_order_id=%s)",
            card.symbol, broker_order_id,
        )


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


def build_buyboard_runtime(
    *,
    buying_power_provider: Callable[[str, str], float],
    card_lookup: Callable[[str, str, str], Optional[TradeCardState]],
    account_equity_provider: Optional[Callable[[str, str], float]] = None,
    capital_reservation_engine: Optional[Engine] = None,
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
    resolved_broker = broker or KisBroker()
    resolved_equity_provider = account_equity_provider or buying_power_provider

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
        return submit_guarded_overseas_order(
            broker=resolved_broker,
            pre_trade_risk_decision=decision,
            strategy_id=RISK_STRATEGY_ID,
            plan_id=plan_id,
            execution_authority=execution_authority,
            execution_lease=execution_lease,
            lease_engine=lease_engine,
            **submit_kwargs,
        )

    entry_attempt_manager = EntryAttemptManager(
        buying_power_provider=buying_power_provider,
        submit_order=submit_order,
        capital_reservation_engine=capital_reservation_engine,
    )

    position_manager = PositionManager()

    entry_deadline_lookup = EntryDeadlineLookup(
        find_open_entry_order=_find_open_entry_order,
        reconcile_order=lambda order: _reconcile_order(order, broker=resolved_broker),
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
        reason = submit_kwargs.pop("reason", "sell_all")
        intent = _SELL_REASON_TO_INTENT.get(reason, OrderIntent.MANUAL_EXIT)
        symbol = submit_kwargs["symbol"]
        quote = resolved_market_data.latest_quote(symbol)
        limit_price = _marketable_sell_limit_price(quote)
        if limit_price is None:
            raise ExecutionGradeDataUnavailableError(
                f"No live quote available to price a marketable SELL for {symbol}"
            )
        exchange = submit_kwargs.pop("exchange", "NASD")
        return submit_guarded_overseas_order(
            broker=resolved_broker,
            side=OrderSide.SELL,
            intent=intent,
            limit_price=limit_price,
            exchange=exchange,
            plan_id=f"{submit_kwargs.get('environment', '')}:{symbol}:SELL:{reason}",
            execution_authority=execution_authority,
            execution_lease=execution_lease,
            lease_engine=lease_engine,
            **submit_kwargs,
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
        # Review finding P0: a historical filled order discovered from
        # KIS's ~14-day order-history window must not resurrect a position
        # the account no longer actually holds -- confirm against a fresh
        # current-holdings lookup before trusting one.
        get_current_holding=lambda card: _refresh_broker_position(card, broker=resolved_broker),
    )
    eod_service = EodTradingService(
        entry_attempt_manager=entry_attempt_manager,
        position_manager=position_manager,
        callbacks=eod_callbacks,
        reservations_path=capital_allocator.RESERVATIONS_FILE,
        capital_reservation_engine=capital_reservation_engine,
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
        # Review finding P0-8: real, holiday-aware NYSE session hooks
        # instead of TradingEngine's always-open/never-EOD test defaults --
        # without these, premarket Sell All queuing never reliably fired,
        # a stop-triggered liquidation could attempt a regular-hours order
        # outside the session, and EOD cleanup never ran in production.
        market_is_open=is_regular_session_open,
        eod_window_reached=_eod_window_reached,
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
