"""Durable, broker-neutral guarded submission for overseas orders."""

from __future__ import annotations

import logging
import math
from pathlib import Path
from typing import Any, Callable, Optional

from src.core.order_state import (
    REGULAR_LIMIT_EXECUTION,
    RESERVED_MOO_EXECUTION,
    BrokerOrder,
    OrderIntent,
    OrderSide,
    OrderStatus,
)
from src.risk.pre_trade import (
    PreTradeRiskDecision,
    PreTradeRiskRejectedError,
    normalize_share_quantity,
    require_pre_trade_risk_approval,
)
from src.services import trading_state
from src.services.broker import Broker, BrokerSubmissionResult, KisBroker
from src.services.execution_authority import (
    ExecutionAuthority,
    LeaseExpiredError,
    LeaseHandle,
)
from src.services.order_ledger import (
    ORDERS_FILE,
    reserve_order_if_no_matching_open,
    upsert_order,
)
from src.services.trading_state import TradingDisabledError

logger = logging.getLogger(__name__)


class DuplicateOpenOrderError(RuntimeError):
    """Raised when a matching unresolved broker order already exists."""

    def __init__(self, order: BrokerOrder) -> None:
        self.order = order
        super().__init__(
            f"Open {order.side.value} {order.intent.value} order already exists for "
            f"{order.symbol} in {order.environment} account {order.account_no}. "
            "Reconcile or cancel it before submitting another order."
        )


def submit_guarded_overseas_order(
    *,
    environment: str,
    account_no: str,
    symbol: str,
    side: OrderSide,
    intent: OrderIntent,
    quantity: int,
    limit_price: float,
    exchange: str = "NASD",
    execution_policy: str = REGULAR_LIMIT_EXECUTION,
    allow_duplicate: bool = False,
    path: Path = ORDERS_FILE,
    broker: Optional[Broker] = None,
    pre_trade_risk_decision: Optional[PreTradeRiskDecision] = None,
    strategy_id: str = "",
    plan_id: str = "",
    event_recorder: Optional[Callable[..., Any]] = None,
    signal_payload: Optional[dict[str, Any]] = None,
    signal_event_type: str = "SIGNAL_CREATED",
    execution_authority: Optional[ExecutionAuthority] = None,
    execution_lease: Optional[LeaseHandle] = None,
    lease_engine: Optional[Any] = None,
) -> BrokerOrder:
    """Submit an overseas order with a durable local idempotency guard.

    This function records local intent before touching the KIS API. A returned
    ACCEPTED order means only that KIS received the order request; it never
    implies a broker fill or local position change.

    ``broker`` defaults to ``KisBroker()`` (the real KIS API) -- this state
    machine itself has no KIS-specific knowledge and doesn't change based on
    which ``Broker`` implementation runs underneath it. ENTRY orders must
    carry a fresh risk approval bound to the complete order fingerprint; exit
    orders deliberately remain available without entry-risk approval.
    """

    def emit_event(event_type: str, order: BrokerOrder, **extra: Any) -> None:
        if event_recorder is None:
            return
        try:
            event_recorder(
                event_type,
                strategy_id=strategy_id,
                symbol=order.symbol,
                signal_id=plan_id,
                order_id=order.client_order_id,
                broker_order_id=order.broker_order_id,
                environment=order.environment,
                account_no=order.account_no,
                price=order.limit_price,
                quantity=order.quantity_requested,
                payload={
                    "side": order.side.value,
                    "intent": order.intent.value,
                    "execution_policy": order.execution_policy,
                    "plan_id": plan_id,
                    **extra,
                },
            )
        except Exception:
            # Observability must never alter order state or induce a retry.
            logger.exception("Trading event recorder failed for %s", event_type)

    trading_state.require_trading_enabled(environment, symbol)
    environment = str(environment or "").upper()
    account_no = str(account_no or "")
    symbol = str(symbol or "").upper()
    side = side if isinstance(side, OrderSide) else OrderSide(str(side).upper())
    intent = (
        intent if isinstance(intent, OrderIntent) else OrderIntent(str(intent).upper())
    )
    quantity = normalize_share_quantity(quantity)
    exchange = str(exchange or "").strip().upper()
    execution_policy = str(execution_policy or REGULAR_LIMIT_EXECUTION).strip().upper()
    if execution_policy not in {
        REGULAR_LIMIT_EXECUTION,
        RESERVED_MOO_EXECUTION,
    }:
        raise ValueError(f"Unsupported execution_policy={execution_policy!r}")
    if side == OrderSide.BUY and intent != OrderIntent.ENTRY:
        raise ValueError("BUY orders require explicit ENTRY intent")
    if intent == OrderIntent.ENTRY and side != OrderSide.BUY:
        raise ValueError("ENTRY intent requires a BUY order")
    if execution_policy == RESERVED_MOO_EXECUTION:
        if environment != "PROD" or side != OrderSide.SELL:
            raise ValueError("Reserved MOO execution requires a PROD sell order")
        limit_price = 0.0
    else:
        try:
            limit_price = float(limit_price)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("limit_price must be positive and finite") from exc
        if not math.isfinite(limit_price) or limit_price <= 0:
            raise ValueError(f"limit_price must be positive, got {limit_price}")

    def require_current_entry_approval() -> None:
        if intent == OrderIntent.ENTRY:
            require_pre_trade_risk_approval(
                pre_trade_risk_decision,
                environment=environment,
                account_no=account_no,
                symbol=symbol,
                side=side,
                intent=intent,
                quantity=quantity,
                reference_price=limit_price,
                exchange=exchange,
                execution_policy=execution_policy,
                strategy_id=strategy_id,
                plan_id=plan_id,
            )

    def require_current_lease() -> None:
        if execution_authority is not None:
            execution_authority.require_current_lease(lease_engine, execution_lease)

    order = BrokerOrder.create(
        environment=environment,
        account_no=account_no,
        symbol=symbol,
        side=side,
        intent=intent,
        quantity_requested=quantity,
        limit_price=limit_price,
        exchange=exchange,
        status=OrderStatus.CREATED,
        execution_policy=execution_policy,
        buylist_symbol_key=f"{environment}:{account_no}:{symbol}",
    )
    if signal_payload:
        # A strategy signal and its position-risk decision are separate facts.
        # Risk rejection below must not rewrite an actionable strategy output
        # as SIGNAL_REJECTED.
        normalized_signal_event = str(signal_event_type or "").strip().upper()
        if normalized_signal_event not in {"SIGNAL_CREATED", "SIGNAL_REJECTED"}:
            normalized_signal_event = "SIGNAL_CREATED"
        emit_event(normalized_signal_event, order, **signal_payload)
    if intent == OrderIntent.ENTRY:
        try:
            require_current_entry_approval()
        except PreTradeRiskRejectedError as exc:
            emit_event("RISK_REJECTED", order, reason=str(exc))
            raise
        emit_event(
            "RISK_APPROVED",
            order,
            evaluated_at=getattr(pre_trade_risk_decision, "evaluated_at", None),
            expires_at=getattr(pre_trade_risk_decision, "expires_at", None),
        )
    emit_event("ORDER_INTENT_CREATED", order)

    broker = KisBroker() if broker is None else broker

    match = reserve_order_if_no_matching_open(
        order,
        allow_duplicate=allow_duplicate,
        path=path,
    )
    if match is not None:
        raise DuplicateOpenOrderError(match)
    emit_event("ORDER_RESERVED", order)

    # Re-check the kill switch, short-lived entry approval, and (when a lease
    # was supplied) main-device authority after the durable duplicate-order
    # reservation. Any of these can change/expire while waiting for the
    # ledger lock. No broker request was attempted, so close the reserved
    # row as REJECTED instead of leaving an open order behind.
    try:
        trading_state.require_trading_enabled(environment, symbol)
        require_current_entry_approval()
        require_current_lease()
    except (TradingDisabledError, PreTradeRiskRejectedError, LeaseExpiredError) as exc:
        order.status = OrderStatus.REJECTED
        order.error_message = str(exc)
        order.touch()
        upsert_order(order, path=path)
        if isinstance(exc, PreTradeRiskRejectedError):
            emit_event("RISK_REJECTED", order, reason=str(exc))
        emit_event("ORDER_REJECTED", order, reason=str(exc))
        raise

    # Once we are about to issue the non-idempotent broker request, a process
    # crash or lost response must be treated as potentially submitted.  This
    # blocks a retry until reconciliation checks the broker.
    order.status = OrderStatus.UNKNOWN_SUBMISSION_STATE
    order.touch()
    upsert_order(order, path=path)
    emit_event("ORDER_SUBMISSION_STARTED", order)

    # The synchronous journal write above may contend or fsync slowly. Recheck
    # every gate at the last possible service boundary so neither a newly
    # disabled kill switch, an expired entry approval, nor a lost main-device
    # lease can cross into the non-idempotent broker call.
    try:
        trading_state.require_trading_enabled(environment, symbol)
        require_current_entry_approval()
        require_current_lease()
    except (TradingDisabledError, PreTradeRiskRejectedError, LeaseExpiredError) as exc:
        order.status = OrderStatus.REJECTED
        order.error_message = str(exc)
        order.touch()
        upsert_order(order, path=path)
        if isinstance(exc, PreTradeRiskRejectedError):
            emit_event("RISK_REJECTED", order, reason=str(exc))
        emit_event("ORDER_REJECTED", order, reason=str(exc))
        raise

    try:
        submission = broker.submit_order(
            environment=environment,
            account_no=account_no,
            symbol=symbol,
            side=side,
            quantity=quantity,
            limit_price=limit_price,
            exchange=exchange,
            execution_policy=execution_policy,
        )
        if not isinstance(submission, BrokerSubmissionResult):
            raise TypeError("Broker.submit_order() must return BrokerSubmissionResult")
    except TradingDisabledError as exc:
        # The broker boundary guarantees that no HTTP submission was attempted
        # when this exception is raised, so this is a clear local rejection,
        # never an ambiguous submission state.
        order.status = OrderStatus.REJECTED
        order.error_message = str(exc)
        order.touch()
        upsert_order(order, path=path)
        emit_event("ORDER_REJECTED", order, reason=str(exc))
        return order
    except Exception as exc:
        try:
            ambiguous = broker.is_ambiguous_submission_error(exc)
        except Exception:
            # An invalid/custom broker that cannot classify an error must fail
            # conservatively: the non-idempotent request may have reached it.
            ambiguous = True
            logger.exception("Broker failed to classify submission error")
        if ambiguous:
            order.status = OrderStatus.UNKNOWN_SUBMISSION_STATE
            logger.warning(
                "Broker order submission result unknown for %s %s %s account %s: %s",
                environment,
                side.value,
                symbol,
                account_no or "<unknown>",
                exc,
            )
        else:
            order.status = OrderStatus.REJECTED
        order.error_message = str(exc)
        order.touch()
        upsert_order(order, path=path)
        emit_event(
            "ORDER_SUBMISSION_UNKNOWN" if ambiguous else "ORDER_REJECTED",
            order,
            reason=str(exc),
        )
        return order

    order.status = OrderStatus.ACCEPTED
    order.broker_order_id = submission.broker_order_id
    order.raw_submit_response = submission.raw_response
    order.filled_quantity = 0
    order.remaining_quantity = order.quantity_requested
    order.touch()
    upsert_order(order, path=path)
    emit_event("ORDER_ACCEPTED", order)
    return order
