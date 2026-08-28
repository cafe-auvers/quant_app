"""``FakeExecutionBroker`` -- a deterministic, fully-scripted
:class:`~src.brokers.execution_broker_protocol.ExecutionBrokerProtocol`
implementation for PR2's tests.

``docs/kanban_production_readiness.md``, Workstream 3 (PR2): "PR2 tests
should use a deterministic fake broker capable of producing: Acceptance
with broker ID / Explicit rejection / Timeout before response / Transport
exception / Acceptance followed by persistence failure / Cancel accepted /
Cancel rejected / Fill racing cancellation."

Every scenario is queued explicitly before the call that consumes it --
there is no hidden randomness or clock dependency, so a failing test always
reproduces. Calling ``submit_order``/``cancel_order`` with nothing queued
raises :class:`AssertionError` immediately rather than silently returning a
default -- an un-scripted call in one of these tests is a test bug, not a
scenario to paper over.

"Acceptance followed by persistence failure" is not something a broker
double can itself simulate -- it is a fault in the *caller's* database
write after a real broker acceptance, exercised by tests that inject a
failing engine/connection around a normal ``queue_acceptance()``, not by
anything this class does differently.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from src.brokers.execution_broker_protocol import BrokerSubmissionResult
from src.core.order_state import (
    BrokerOrderDiscoveryResult,
    BrokerOrderStatusSnapshot,
    OrderSide,
    OrderStatus,
)


class BrokerTimeoutError(Exception):
    """No response was received before the caller gave up -- the broker may
    or may not have received/acted on the request. Always ambiguous."""


class BrokerTransportError(Exception):
    """A transport-level failure (connection reset, DNS, TLS) with no HTTP
    response at all. Always ambiguous, same reasoning as a timeout."""


class BrokerRejectionError(Exception):
    """The broker responded and explicitly said no. Never ambiguous."""

    def __init__(self, message: str = "rejected", *, raw_response: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.raw_response: Dict[str, Any] = raw_response or {}


@dataclass
class FakeExecutionBroker:
    """Queue-driven fake. Each ``queue_*`` call appends one scripted outcome;
    each ``submit_order``/``cancel_order`` call consumes exactly one, in
    order, and records the kwargs it was called with for assertion."""

    submit_calls: List[Dict[str, Any]] = field(default_factory=list)
    cancel_calls: List[Dict[str, Any]] = field(default_factory=list)
    get_order_calls: List[Dict[str, Any]] = field(default_factory=list)
    _submit_queue: List[Callable[[], BrokerSubmissionResult]] = field(default_factory=list)
    _cancel_queue: List[Callable[[], BrokerOrderStatusSnapshot]] = field(default_factory=list)
    _get_order_queue: List[List[BrokerOrderStatusSnapshot]] = field(default_factory=list)
    _last_cancel_snapshot: Optional[BrokerOrderStatusSnapshot] = None

    # --- scripting: submission -------------------------------------------------

    def queue_acceptance(
        self, *, broker_order_id: str = "B-1", raw_response: Optional[Dict[str, Any]] = None
    ) -> None:
        result = BrokerSubmissionResult(
            broker_order_id=broker_order_id, raw_response=raw_response or {"status": "accepted"}
        )
        self._submit_queue.append(lambda: result)

    def queue_rejection(
        self, *, message: str = "order rejected", raw_response: Optional[Dict[str, Any]] = None
    ) -> None:
        def _raise() -> BrokerSubmissionResult:
            raise BrokerRejectionError(message, raw_response=raw_response or {"status": "rejected"})

        self._submit_queue.append(_raise)

    def queue_timeout(self) -> None:
        def _raise() -> BrokerSubmissionResult:
            raise BrokerTimeoutError("timed out waiting for a submission response")

        self._submit_queue.append(_raise)

    def queue_transport_exception(self) -> None:
        def _raise() -> BrokerSubmissionResult:
            raise BrokerTransportError("connection reset before a response was received")

        self._submit_queue.append(_raise)

    # --- scripting: cancellation -------------------------------------------------

    def queue_cancel_confirmed(self, *, raw_response: Optional[Dict[str, Any]] = None) -> None:
        def _make() -> BrokerOrderStatusSnapshot:
            return BrokerOrderStatusSnapshot(
                environment="", account_no="", symbol="",
                status=OrderStatus.CANCELLED, raw_response=raw_response or {"status": "cancelled"},
            )

        self._cancel_queue.append(_make)

    def queue_cancel_acknowledged(self) -> None:
        def _make() -> BrokerOrderStatusSnapshot:
            return BrokerOrderStatusSnapshot(
                environment="",
                account_no="",
                symbol="",
                status=OrderStatus.CANCEL_REQUESTED,
                raw_response={"status": "cancel_requested"},
            )

        self._cancel_queue.append(_make)

    def queue_order_snapshot(self, snapshot: BrokerOrderStatusSnapshot) -> None:
        self._get_order_queue.append([snapshot])

    def queue_cancel_rejected(
        self, *, message: str = "cancel rejected -- order already progressed",
        raw_response: Optional[Dict[str, Any]] = None,
    ) -> None:
        def _raise() -> BrokerOrderStatusSnapshot:
            raise BrokerRejectionError(message, raw_response=raw_response or {"status": "cancel_rejected"})

        self._cancel_queue.append(_raise)

    def queue_cancel_timeout(self) -> None:
        def _raise() -> BrokerOrderStatusSnapshot:
            raise BrokerTimeoutError("timed out waiting for a cancel response")

        self._cancel_queue.append(_raise)

    def queue_cancel_fill_race(
        self, *, filled_quantity: int, quantity_requested: int, side: OrderSide = OrderSide.BUY
    ) -> None:
        """A fill raced the cancel: the broker's own answer to "cancel this"
        is "it already (partially) filled" -- ``ExecutionOrderStatus``'s own
        ``CANCEL_PENDING -> FILLED/PARTIALLY_FILLED`` row (revision 3), not
        an exception."""

        def _make() -> BrokerOrderStatusSnapshot:
            status = OrderStatus.FILLED if filled_quantity >= quantity_requested else OrderStatus.PARTIALLY_FILLED
            return BrokerOrderStatusSnapshot(
                environment="", account_no="", symbol="", side=side, status=status,
                quantity_requested=quantity_requested, filled_quantity=filled_quantity,
                remaining_quantity=max(0, quantity_requested - filled_quantity),
                raw_response={"status": "filled_before_cancel"},
            )

        self._cancel_queue.append(_make)

    # --- ExecutionBrokerProtocol -------------------------------------------------

    def submit_order(self, **kwargs: Any) -> BrokerSubmissionResult:
        self.submit_calls.append(dict(kwargs))
        if not self._submit_queue:
            raise AssertionError(
                "FakeExecutionBroker.submit_order called with no queued behavior -- "
                "call queue_acceptance()/queue_rejection()/queue_timeout()/"
                "queue_transport_exception() before exercising the caller under test"
            )
        return self._submit_queue.pop(0)()

    def is_ambiguous_submission_error(self, error: BaseException) -> bool:
        return isinstance(error, (BrokerTimeoutError, BrokerTransportError))

    def cancel_order(self, **kwargs: Any) -> BrokerOrderStatusSnapshot:
        self.cancel_calls.append(dict(kwargs))
        if not self._cancel_queue:
            raise AssertionError(
                "FakeExecutionBroker.cancel_order called with no queued behavior -- "
                "call queue_cancel_confirmed()/queue_cancel_rejected()/queue_cancel_timeout()/"
                "queue_cancel_fill_race() before exercising the caller under test"
            )
        result = self._cancel_queue.pop(0)()
        self._last_cancel_snapshot = result
        return result

    def is_ambiguous_cancellation_error(self, error: BaseException) -> bool:
        """Not part of the base ``Broker`` protocol (legacy cancellation has
        no ambiguity classification today -- see the gateway's own
        docstring) but used by :class:`~src.services.execution_command_gateway.ExecutionCommandGateway`
        in ``GUARDED_ENGINE`` mode, which does need one."""
        return isinstance(error, (BrokerTimeoutError, BrokerTransportError))

    def get_order(self, **kwargs: Any) -> List[BrokerOrderStatusSnapshot]:
        self.get_order_calls.append(dict(kwargs))
        if self._get_order_queue:
            return self._get_order_queue.pop(0)
        if self._last_cancel_snapshot is not None:
            snapshot = self._last_cancel_snapshot
            snapshot.environment = str(kwargs.get("environment") or "")
            snapshot.account_no = str(kwargs.get("account_no") or "")
            snapshot.symbol = str(kwargs.get("symbol") or "")
            snapshot.broker_order_id = str(kwargs.get("broker_order_id") or "")
            snapshot.client_order_id = str(kwargs.get("client_order_id") or "")
            if snapshot.quantity_requested <= 0:
                snapshot.quantity_requested = int(kwargs.get("quantity") or 0)
            return [snapshot]
        return []

    def discover_orders(self, **kwargs: Any) -> BrokerOrderDiscoveryResult:
        return BrokerOrderDiscoveryResult(
            open_orders_complete=True, history_complete=True, reserved_orders_complete=True
        )

    def get_positions(self, **kwargs: Any) -> Dict[str, Any]:
        return {}
