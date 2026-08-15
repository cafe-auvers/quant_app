"""``ExecutionWorkflowService`` -- the one workflow service both the legacy
Buy Dashboard and the Kanban board call (INV-21, Workstream 9).

``docs/kanban_production_readiness.md``, PR2: "Legacy Buy Dashboard and
Kanban invoke the same underlying workflow service -- no UI module calls
the broker, reconciliation engine, or command repository directly."

This module is deliberately thin. The already-reviewed guard sequences in
:func:`~src.services.order_execution_service.submit_guarded_overseas_order`
(kill switch, pre-trade risk, duplicate-order reservation, lease re-checks)
and :func:`~src.services.order_reconciliation.cancel_and_reconcile_order`
are **not** reimplemented or bypassed here -- ``request_submit``/
``request_cancel`` call them exactly as before, with one change: the
``broker`` they receive is always bound to
:class:`~src.services.execution_command_gateway.ExecutionCommandGateway`
(Workstream 9's single mutation boundary) with the calling frontend's
:class:`~src.core.execution_mode.ExecutionSource` attached, instead of an
unattributed ``KisBroker``/``None`` default. Nothing about *when* or *how*
the broker is actually called changes -- see
:mod:`src.core.execution_mode`'s module docstring for why that's true even
though this now runs through a new module boundary.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from src.brokers.execution_broker_protocol import (
    Broker,
    BrokerOrderStatusSnapshot,
    BrokerSubmissionResult,
)
from src.core.execution_mode import ExecutionSource
from src.core.order_state import (
    REGULAR_LIMIT_EXECUTION,
    BrokerOrder,
    BrokerOrderDiscoveryResult,
    OrderIntent,
    OrderSide,
)
from src.services.execution_command_gateway import ExecutionCommandGateway, get_default_execution_gateway
from src.services.order_ledger import ORDERS_FILE
from src.services.order_execution_service import submit_guarded_overseas_order
from src.services.order_reconciliation import cancel_and_reconcile_order


class _SourceBoundGatewayBroker:
    """Adapts an :class:`ExecutionCommandGateway` to the exact ``Broker``
    protocol shape ``submit_guarded_overseas_order``/
    ``cancel_and_reconcile_order`` call -- neither passes a ``source=``
    keyword (the ``Broker`` protocol has no such parameter), so this binds
    it once here instead, letting every call this workflow service makes
    stay correctly source-attributed without changing either function's
    signature or call convention.

    ``GUARDED_ENGINE``-mode cancellation note: ``cancel_and_reconcile_order``
    does not pass its own ``client_order_id`` argument through to
    ``broker.cancel_order(...)`` (it identifies the broker-side order by
    ``broker_order_id`` instead) -- harmless in ``LEGACY_COMPATIBILITY``
    mode (the only mode this call path ever runs in production), but it
    means this specific path cannot drive the gateway's ``GUARDED_ENGINE``
    cancel sequence, which requires ``client_order_id`` to look up the
    ``ExecutionOrderRecord``. Widening ``cancel_and_reconcile_order``'s own
    call to the broker is out of PR2's scope (see the module docstring's
    "minimal behavioral alteration" constraint).
    """

    def __init__(self, gateway: ExecutionCommandGateway, source: ExecutionSource) -> None:
        self._gateway = gateway
        self._source = source

    def submit_order(self, **kwargs: Any) -> BrokerSubmissionResult:
        return self._gateway.submit_order(source=self._source, **kwargs)

    def is_ambiguous_submission_error(self, error: BaseException) -> bool:
        return self._gateway.is_ambiguous_submission_error(error)

    def cancel_order(self, **kwargs: Any) -> BrokerOrderStatusSnapshot:
        return self._gateway.cancel_order(source=self._source, **kwargs)

    def get_order(self, **kwargs: Any) -> List[BrokerOrderStatusSnapshot]:
        return self._gateway.get_order(**kwargs)

    def discover_orders(self, **kwargs: Any) -> BrokerOrderDiscoveryResult:
        return self._gateway.discover_orders(**kwargs)

    def get_positions(self, **kwargs: Any) -> Dict[str, Any]:
        return self._gateway.get_positions(**kwargs)


def request_submit(
    *,
    source: ExecutionSource,
    environment: str,
    account_no: str,
    symbol: str,
    side: OrderSide,
    intent: OrderIntent,
    quantity: int,
    limit_price: float,
    exchange: str = "NASD",
    execution_policy: str = REGULAR_LIMIT_EXECUTION,
    gateway: Optional[ExecutionCommandGateway] = None,
    path: Path = ORDERS_FILE,
    **kwargs: Any,
) -> BrokerOrder:
    """The single shared submission entry point (INV-21). ``**kwargs`` are
    ``submit_guarded_overseas_order``'s own remaining parameters
    (``pre_trade_risk_decision``, ``execution_authority``,
    ``execution_lease``, ``attempt_group_id``, etc.) passed through
    unchanged -- this function adds only ``source`` attribution, nothing
    else about the call.
    """
    resolved_gateway = gateway or get_default_execution_gateway()
    return submit_guarded_overseas_order(
        environment=environment, account_no=account_no, symbol=symbol, side=side, intent=intent,
        quantity=quantity, limit_price=limit_price, exchange=exchange, execution_policy=execution_policy,
        path=path, broker=_SourceBoundGatewayBroker(resolved_gateway, source), **kwargs,
    )


def request_cancel(
    *,
    source: ExecutionSource,
    client_order_id: str,
    gateway: Optional[ExecutionCommandGateway] = None,
    path: Path = ORDERS_FILE,
) -> BrokerOrder:
    """The single shared cancellation entry point (INV-21)."""
    resolved_gateway = gateway or get_default_execution_gateway()
    return cancel_and_reconcile_order(
        client_order_id, path=path, broker=_SourceBoundGatewayBroker(resolved_gateway, source)
    )
