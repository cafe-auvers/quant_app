"""Thin broker abstraction over the KIS overseas order API.

``OrderExecutionService``'s guarded-submission state machine (CREATED ->
UNKNOWN_SUBMISSION_STATE -> ACCEPTED/REJECTED, the local idempotency guard,
reconciliation against account snapshots) is unchanged by this module. This is
a wrapper around the existing, already-safe ``src.api.kis_order`` calls, not a
rewrite: it exists so callers ask ``broker.submit_order(...)`` instead of
importing ``src.api.kis_order`` directly, which is what lets a future
``SimulatedBroker`` (for the P4 backtester) or ``KisPaperBroker`` implement the
same ``Broker`` protocol later without ``OrderExecutionService`` -- or ORB, or
anything above it -- changing at all.

``KisBroker`` is the sole real implementation today. Every method here is a
direct, uninterpreted passthrough to the corresponding ``kis_order``/
``kis_account_snapshot_dual`` call -- no new retry, validation, or state logic
is introduced.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Protocol

from src.api import kis_account_snapshot_dual, kis_order
from src.core.order_state import (
    REGULAR_LIMIT_EXECUTION,
    RESERVED_MOO_EXECUTION,
    BrokerOrderStatusSnapshot,
    OrderSide,
)


class Broker(Protocol):
    """Everything ``OrderExecutionService`` needs from a live/simulated broker."""

    def submit_order(
        self,
        *,
        environment: str,
        account_no: str,
        symbol: str,
        side: OrderSide,
        quantity: int,
        limit_price: float,
        exchange: str = "NASD",
        execution_policy: str = REGULAR_LIMIT_EXECUTION,
    ) -> Dict[str, Any]:
        """Submit an order. Returns the raw broker response.

        Acceptance only -- callers must never treat a returned response as a
        fill; see docs/next_steps_plan.md and PROJECT_ARCHITECTURE.md's KIS
        Order Lifecycle section.
        """
        ...

    def cancel_order(
        self,
        *,
        environment: str,
        account_no: str,
        is_reserved: bool = False,
        **kwargs: Any,
    ) -> BrokerOrderStatusSnapshot:
        """Cancel a working order. ``kwargs`` match the underlying KIS call:
        regular cancels take symbol/broker_order_id/quantity/side/exchange;
        reserved-MOO cancels take broker_order_id/reservation_date instead
        (the two broker-side cancel endpoints have genuinely different
        required fields -- this wrapper does not paper over that)."""
        ...

    def get_order(
        self,
        *,
        environment: str,
        account_no: str,
        is_reserved: bool = False,
        **kwargs: Any,
    ) -> List[BrokerOrderStatusSnapshot]:
        """Query broker-side order/reservation status snapshots."""
        ...

    def get_positions(
        self,
        *,
        environment: str,
        account_no: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Fetch the current overseas account snapshot (holdings, cash)."""
        ...


class KisBroker:
    """``Broker`` implementation backed by the real KIS overseas order API."""

    def submit_order(
        self,
        *,
        environment: str,
        account_no: str,
        symbol: str,
        side: OrderSide,
        quantity: int,
        limit_price: float,
        exchange: str = "NASD",
        execution_policy: str = REGULAR_LIMIT_EXECUTION,
    ) -> Dict[str, Any]:
        if execution_policy == RESERVED_MOO_EXECUTION:
            return kis_order.place_overseas_reserved_market_on_open_sell(
                environment=environment,
                account_no=account_no,
                symbol=symbol,
                quantity=quantity,
                exchange=exchange,
            )
        side_value = side.value if isinstance(side, OrderSide) else str(side)
        return kis_order.place_overseas_order(
            environment=environment,
            account_no=account_no,
            symbol=symbol,
            quantity=quantity,
            price=limit_price,
            side=side_value.lower(),
            exchange=exchange,
            order_type="limit",
        )

    def cancel_order(
        self,
        *,
        environment: str,
        account_no: str,
        is_reserved: bool = False,
        **kwargs: Any,
    ) -> BrokerOrderStatusSnapshot:
        if is_reserved:
            return kis_order.cancel_overseas_reserved_order(
                environment=environment, account_no=account_no, **kwargs
            )
        return kis_order.cancel_overseas_order(
            environment=environment, account_no=account_no, **kwargs
        )

    def get_order(
        self,
        *,
        environment: str,
        account_no: str,
        is_reserved: bool = False,
        **kwargs: Any,
    ) -> List[BrokerOrderStatusSnapshot]:
        query_fn = (
            kis_order.query_overseas_reserved_order
            if is_reserved
            else kis_order.query_overseas_order
        )
        return query_fn(environment=environment, account_no=account_no, **kwargs)

    def get_positions(
        self,
        *,
        environment: str,
        account_no: Optional[str] = None,
    ) -> Dict[str, Any]:
        return kis_account_snapshot_dual.fetch_account_snapshot(
            environment,
            include_domestic=False,
            include_overseas=True,
            account_no=account_no,
        )
