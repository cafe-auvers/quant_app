"""``ExecutionWorkflowService`` -- the one workflow service both the legacy
Buy Dashboard and the Kanban board call (INV-21, Workstream 9).

``docs/kanban_production_readiness.md``, PR2, second review pass (findings
1/2/8): this module is where a **stable, caller-generated command
identity** is created -- once, here, before the first gateway call -- for
``GUARDED_ENGINE`` mode. It is never generated inside
:class:`~src.services.execution_command_gateway.ExecutionCommandGateway`
itself, which is what made restart-safe idempotency impossible in the
first version of this PR. A caller that actually needs replay-safety
across a real process restart must generate and durably remember its own
``client_order_id``/``cancel_command_id``/``replace_command_id`` and pass
the *same* one on every retry of the *same* logical decision -- this
module only threads whatever identity it's given (or mints a fresh one
when none is given, which is correct for a **new** decision, never a
replay of an old one).

``LEGACY_COMPATIBILITY`` mode (today, always) is unchanged from the first
version of this PR: ``request_submit``/``request_cancel`` still call
:func:`~src.services.order_execution_service.submit_guarded_overseas_order`/
:func:`~src.services.order_reconciliation.cancel_and_reconcile_order`
exactly as before, with the gateway bound as their ``broker=``. Return
shape genuinely differs by mode -- a ``BrokerOrder`` in
``LEGACY_COMPATIBILITY``, an ``ExecutionOrderRecord`` in
``GUARDED_ENGINE`` -- since those are the two modes' actual persisted
representations; unifying that is Workstream 13 (UI projection) territory,
not this PR's.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

from src.brokers.execution_broker_protocol import (
    Broker,
    BrokerOrderStatusSnapshot,
    BrokerSubmissionResult,
)
from src.core.execution_mode import ExecutionMode, ExecutionSource
from src.core.execution_order_record import ExecutionOrderRecord
from src.core.execution_request import CancelExecutionRequest, ReplaceExecutionRequest, SubmitExecutionRequest
from src.core.order_state import (
    REGULAR_LIMIT_EXECUTION,
    BrokerOrder,
    BrokerOrderDiscoveryResult,
    OrderIntent,
    OrderSide,
    generate_client_order_id,
)
from src.services.execution_command_gateway import ExecutionCommandGateway, get_default_execution_gateway
from src.services.execution_lease_protocol import ExecutionLease
from src.services.order_ledger import ORDERS_FILE
from src.services.order_execution_service import submit_guarded_overseas_order
from src.services.order_reconciliation import cancel_and_reconcile_order


class _SourceBoundGatewayBroker:
    """Adapts an :class:`ExecutionCommandGateway` to the exact
    ``ExecutionBrokerProtocol`` shape ``submit_guarded_overseas_order``/
    ``cancel_and_reconcile_order`` call -- LEGACY_COMPATIBILITY mode only
    (the gateway itself raises ``WrongGatewayModeError`` if
    ``submit_order``/``cancel_order`` are ever reached while
    ``GUARDED_ENGINE`` is active, so this adapter cannot silently drive
    the wrong mode)."""

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
    client_order_id: Optional[str] = None,
    lease: Optional[ExecutionLease] = None,
    attempt_group_id: str = "",
    attempt_number: int = 1,
    **legacy_kwargs: Any,
) -> Any:
    """The single shared submission entry point (INV-21).

    ``client_order_id``, in ``GUARDED_ENGINE`` mode: pass the *same*
    identity you used on a prior attempt of this exact logical submission
    to replay it safely (a duplicate is rejected, never resubmitted to the
    broker); omit it for a genuinely new submission, and a fresh one is
    minted here, once.

    ``**legacy_kwargs`` are ``submit_guarded_overseas_order``'s own
    remaining parameters (``pre_trade_risk_decision``,
    ``execution_authority``, ``execution_lease``, etc.), used only in
    ``LEGACY_COMPATIBILITY`` mode and passed through unchanged.
    """
    resolved_gateway = gateway or get_default_execution_gateway()
    if resolved_gateway.mode == ExecutionMode.GUARDED_ENGINE:
        stable_id = client_order_id or generate_client_order_id(environment, account_no, symbol, side, intent)
        request = SubmitExecutionRequest(
            client_order_id=stable_id, environment=environment, account_no=account_no, symbol=symbol,
            side=side, intent=intent, quantity=quantity, limit_price=limit_price, exchange=exchange,
            execution_policy=execution_policy, attempt_group_id=attempt_group_id,
            attempt_number=attempt_number, lease=lease, source=source,
        )
        return resolved_gateway.submit_guarded(request)
    return submit_guarded_overseas_order(
        environment=environment, account_no=account_no, symbol=symbol, side=side, intent=intent,
        quantity=quantity, limit_price=limit_price, exchange=exchange, execution_policy=execution_policy,
        path=path, broker=_SourceBoundGatewayBroker(resolved_gateway, source), **legacy_kwargs,
    )


def request_cancel(
    *,
    source: ExecutionSource,
    client_order_id: str,
    gateway: Optional[ExecutionCommandGateway] = None,
    path: Path = ORDERS_FILE,
    cancel_command_id: Optional[str] = None,
    lease: Optional[ExecutionLease] = None,
    environment: str = "",
    account_no: str = "",
) -> Any:
    """The single shared cancellation entry point (INV-21).

    ``cancel_command_id``, in ``GUARDED_ENGINE`` mode: pass the *same*
    identity to replay an earlier, still-unresolved cancel decision (e.g.
    after a timeout); a genuinely new, later cancel decision for the same
    order (e.g. after an earlier cancel was explicitly rejected and the
    order resumed working) must use a new one -- reusing an old one would
    be indistinguishable from a replay and permanently rejected as a
    duplicate (finding 8). Omit it to mint a fresh one for a new decision.

    ``environment``/``account_no`` are required in ``GUARDED_ENGINE`` mode
    (the gateway verifies them against the order's own persisted record --
    finding 9's account/environment-match gate); ``LEGACY_COMPATIBILITY``
    mode does not need them (the local order ledger already knows).
    """
    resolved_gateway = gateway or get_default_execution_gateway()
    if resolved_gateway.mode == ExecutionMode.GUARDED_ENGINE:
        stable_cancel_id = cancel_command_id or f"{client_order_id}:{uuid4().hex[:12]}"
        request = CancelExecutionRequest(
            client_order_id=client_order_id, cancel_command_id=stable_cancel_id,
            environment=environment, account_no=account_no, lease=lease, source=source,
        )
        return resolved_gateway.cancel_guarded(request)
    return cancel_and_reconcile_order(
        client_order_id, path=path, broker=_SourceBoundGatewayBroker(resolved_gateway, source)
    )


def request_replace(
    *,
    source: ExecutionSource,
    client_order_id: str,
    new_quantity: int,
    new_limit_price: float,
    gateway: Optional[ExecutionCommandGateway] = None,
    replace_command_id: Optional[str] = None,
    new_client_order_id: Optional[str] = None,
    lease: Optional[ExecutionLease] = None,
    environment: str = "",
    account_no: str = "",
) -> ExecutionOrderRecord:
    """``GUARDED_ENGINE`` only -- no legacy or Kanban call site performs a
    broker-level replace today (confirmed by codebase survey); raises
    ``NotImplementedError`` in ``LEGACY_COMPATIBILITY`` mode, same as the
    gateway's own ``replace_guarded`` would if reached directly.
    """
    resolved_gateway = gateway or get_default_execution_gateway()
    if resolved_gateway.mode != ExecutionMode.GUARDED_ENGINE:
        raise NotImplementedError(
            "request_replace is only available in GUARDED_ENGINE mode -- no legacy call site "
            "performs a broker-level replace today"
        )
    stable_replace_id = replace_command_id or uuid4().hex
    stable_new_id = new_client_order_id or f"{client_order_id}:REPLACE:{stable_replace_id}"
    request = ReplaceExecutionRequest(
        client_order_id=client_order_id, replace_command_id=stable_replace_id,
        new_client_order_id=stable_new_id, new_quantity=new_quantity, new_limit_price=new_limit_price,
        environment=environment, account_no=account_no, lease=lease, source=source,
    )
    return resolved_gateway.replace_guarded(request)
