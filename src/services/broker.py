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

``KisBroker`` is the sole real implementation today. It contains the small
amount of KIS-specific response/error normalization required to keep the
execution and reconciliation services broker-neutral; no retry or lifecycle
state logic is introduced here.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Protocol

from src.api import kis_account_snapshot_dual, kis_order
from src.core.order_state import (
    REGULAR_LIMIT_EXECUTION,
    RESERVED_MOO_EXECUTION,
    BrokerOrderDiscoveryResult,
    BrokerOrderStatusSnapshot,
    OrderSide,
)
from src.core.runtime_safety_audit import (
    BROKER_MUTATION_AUDIT_SOURCE,
    record_broker_mutation_attempt,
    register_runtime_safety_audit_source,
)
from src.services import trading_state
from src.utils.config import get_env_value


register_runtime_safety_audit_source(BROKER_MUTATION_AUDIT_SOURCE)


@dataclass(frozen=True)
class BrokerSubmissionResult:
    """Broker-neutral acknowledgement of an order submission request.

    This represents acceptance only, never a fill. ``raw_response`` is kept
    for durable diagnostics while ``broker_order_id`` gives the execution
    service one normalized field independent of the broker's response shape.
    """

    broker_order_id: str
    raw_response: Dict[str, Any]


def _extract_kis_broker_order_id(response: Dict[str, Any]) -> str:
    """Normalize KIS regular/reserved order identifiers at the adapter edge."""
    candidates = (
        "OVRS_RSVN_ODNO",
        "ovrs_rsvn_odno",
        "ODNO",
        "odno",
        "order_no",
        "ORD_NO",
    )

    def walk(value: Any) -> str:
        if isinstance(value, dict):
            for key in candidates:
                if value.get(key):
                    return str(value[key])
            for item in value.values():
                found = walk(item)
                if found:
                    return found
        elif isinstance(value, list):
            for item in value:
                found = walk(item)
                if found:
                    return found
        return ""

    return walk(response)


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
    ) -> BrokerSubmissionResult:
        """Submit an order and return a normalized acceptance result.

        Acceptance only -- callers must never treat a returned response as a
        fill; see PROJECT_ARCHITECTURE.md's KIS Order Lifecycle section.
        """
        ...

    def is_ambiguous_submission_error(self, error: BaseException) -> bool:
        """Whether an exception may have occurred after broker submission."""
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

    def discover_orders(
        self,
        *,
        environment: str,
        account_no: str,
    ) -> BrokerOrderDiscoveryResult:
        """Discover all regular and reserved orders with source completeness."""
        ...

    def get_positions(
        self,
        *,
        environment: str,
        account_no: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Fetch the current account position snapshot used for reconciliation."""
        ...


class ReadOnlyBroker:
    """Read-side broker facade for a pull-only standby runtime.

    Even if a future caller accidentally invokes an execution callback while
    the worker is in standby, this object has no mutation path to delegate.
    """

    def __init__(self, delegate: Broker) -> None:
        self._delegate = delegate

    def submit_order(self, **kwargs):
        raise RuntimeError("standby runtime is read-only; broker submission is disabled")

    def cancel_order(self, **kwargs):
        raise RuntimeError("standby runtime is read-only; broker cancellation is disabled")

    def is_ambiguous_submission_error(self, error: BaseException) -> bool:
        return False

    def get_order(self, **kwargs):
        return self._delegate.get_order(**kwargs)

    def discover_orders(self, **kwargs):
        return self._delegate.discover_orders(**kwargs)

    def get_positions(self, **kwargs):
        return self._delegate.get_positions(**kwargs)


class KisBroker:
    """``Broker`` implementation backed by the real KIS overseas order API."""

    # The gateway supplies priority context around a high-level operation,
    # but this adapter acquires no scheduler turn itself.  Each underlying
    # KIS HTTP request does that independently in ``src.api``.
    schedules_at_request_boundary = True

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
    ) -> BrokerSubmissionResult:
        record_broker_mutation_attempt()
        # Defense in depth: service callers are expected to check before
        # reserving an order, but the real broker boundary must not rely on
        # every present and future caller remembering to do so.
        trading_state.require_trading_enabled(environment, symbol)
        if execution_policy == RESERVED_MOO_EXECUTION:
            response = kis_order.place_overseas_reserved_market_on_open_sell(
                environment=environment,
                account_no=account_no,
                symbol=symbol,
                quantity=quantity,
                exchange=exchange,
            )
        else:
            side_value = side.value if isinstance(side, OrderSide) else str(side)
            response = kis_order.place_overseas_order(
                environment=environment,
                account_no=account_no,
                symbol=symbol,
                quantity=quantity,
                price=limit_price,
                side=side_value.lower(),
                exchange=exchange,
                order_type="limit",
            )
        return BrokerSubmissionResult(
            broker_order_id=_extract_kis_broker_order_id(response),
            raw_response=response,
        )

    def is_ambiguous_submission_error(self, error: BaseException) -> bool:
        return kis_order.is_ambiguous_order_submission_error(error)

    @staticmethod
    def is_confirmed_pre_acceptance_rejection(error: BaseException) -> bool:
        """Classify only KIS's typed, explicit rate-limit refusal as retryable.

        Network errors and generic API failures may have crossed the mutation
        boundary and therefore remain ambiguous/non-retryable.
        """

        from src.api.kis_account_snapshot_dual import KisRateLimitError

        return isinstance(error, KisRateLimitError)

    def cancel_order(
        self,
        *,
        environment: str,
        account_no: str,
        is_reserved: bool = False,
        **kwargs: Any,
    ) -> BrokerOrderStatusSnapshot:
        record_broker_mutation_attempt()
        # Gateway-only ownership correlation; never part of the KIS payload.
        kwargs.pop("ownership_symbol", None)
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

    @staticmethod
    def _authoritative_snapshots(
        snapshots: List[BrokerOrderStatusSnapshot],
    ) -> List[BrokerOrderStatusSnapshot]:
        """Drop only the compatibility sentinel for an authoritative empty query."""
        return [
            snapshot
            for snapshot in snapshots
            if not (
                snapshot.status.value == "UNKNOWN"
                and bool(snapshot.raw_response.get("not_found"))
            )
        ]

    def discover_orders(
        self,
        *,
        environment: str,
        account_no: str,
    ) -> BrokerOrderDiscoveryResult:
        """Query every broker order ledger needed for safe device handoff."""
        result = BrokerOrderDiscoveryResult()
        prefix = f"KIS_{str(environment or '').strip().upper()}"
        exchange_values = get_env_value(
            f"{prefix}_OVERSEAS_EXCHANGES", "NASD,NYSE,AMEX"
        ) or "NASD,NYSE,AMEX"
        exchanges = list(
            dict.fromkeys(
                value.strip().upper()
                for value in exchange_values.split(",")
                if value.strip()
            )
        ) or ["NASD", "NYSE", "AMEX"]

        regular_complete = True
        reserved_complete = True
        for exchange in exchanges:
            try:
                regular = kis_order.query_overseas_order(
                    environment=environment,
                    account_no=account_no,
                    symbol="",
                    exchange=exchange,
                )
                result.snapshots.extend(self._authoritative_snapshots(regular))
            except Exception as exc:
                regular_complete = False
                result.errors.append(
                    f"Regular order discovery failed for {exchange}: {exc}"
                )

            try:
                reserved = kis_order.query_overseas_reserved_order(
                    environment=environment,
                    account_no=account_no,
                    symbol="",
                    exchange=exchange,
                )
                result.snapshots.extend(self._authoritative_snapshots(reserved))
            except Exception as exc:
                reserved_complete = False
                result.errors.append(
                    f"Reserved order discovery failed for {exchange}: {exc}"
                )

        by_key = {}
        for snapshot in result.snapshots:
            key = (
                snapshot.account_no,
                snapshot.symbol,
                snapshot.broker_order_id,
                snapshot.status.value,
                snapshot.filled_quantity,
                snapshot.remaining_quantity,
            )
            by_key[key] = snapshot
        result.snapshots = list(by_key.values())
        result.open_orders_complete = regular_complete
        result.history_complete = regular_complete
        result.reserved_orders_complete = reserved_complete
        return result

    def get_positions(
        self,
        *,
        environment: str,
        account_no: Optional[str] = None,
    ) -> Dict[str, Any]:
        return kis_account_snapshot_dual.fetch_account_snapshot(
            environment,
            include_domestic=True,
            include_overseas=True,
            account_no=account_no,
        )
