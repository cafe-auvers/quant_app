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

``LEGACY_COMPATIBILITY`` mode is unchanged at the broker boundary from the first
version of this PR: ``request_submit``/``request_cancel`` still call
:func:`~src.services.order_execution_service.submit_guarded_overseas_order`/
:func:`~src.services.order_reconciliation.cancel_and_reconcile_order`
exactly as before, with the gateway bound as their ``broker=``. Return
shape is normalized in both modes to ``ExecutionSubmissionResult`` so
entry/exit orchestration never needs to guess which persistence model it
received.
"""
from __future__ import annotations

import inspect
import math
from contextlib import nullcontext
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

from src.brokers.execution_broker_protocol import (
    Broker,
    BrokerOrderStatusSnapshot,
    BrokerSubmissionResult,
)
from src.core.execution_mode import ExecutionMode, ExecutionSource
from src.core.exit_execution_command import ExitExecutionCommand
from src.core.entry_monitoring_command import build_entry_monitoring_command
from src.core.stop_change_command import build_stop_change_command
from src.core.execution_order_record import (
    BrokerIdentityStatus,
    ExecutionOrderRecord,
    ExecutionOrderStatus,
    TERMINAL_EXECUTION_ORDER_STATUSES,
)
from src.core.execution_ownership import ExecutionOwner
from src.core.execution_request import (
    CancelExecutionRequest,
    CancelIntent,
    ReplaceExecutionRequest,
    SubmitExecutionRequest,
)
from src.core.execution_result import ExecutionSubmissionResult
from src.core.order_state import (
    REGULAR_LIMIT_EXECUTION,
    BrokerOrder,
    BrokerOrderDiscoveryResult,
    OrderIntent,
    OrderSide,
    generate_client_order_id,
)
from src.services.execution_command_gateway import ExecutionCommandGateway, get_default_execution_gateway
from src.services.execution_command_gateway import get_legacy_execution_gateway
from src.services.execution_lease_protocol import ExecutionLease
from src.services.order_ledger import ORDERS_FILE
from src.services.order_execution_service import submit_guarded_overseas_order
from src.services.order_reconciliation import cancel_and_reconcile_order


class BoardCommandRejectedError(RuntimeError):
    """A well-formed board request is unsafe or invalid for current truth."""


class BoardRuntimeFenceError(BoardCommandRejectedError):
    """Runtime/readiness changed after the board projection was rendered."""


class BoardOwnershipMismatchError(BoardCommandRejectedError):
    """The Kanban UI does not own the requested account/symbol lifecycle."""


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

    @staticmethod
    def _accepts_source(method: Any) -> bool:
        """Decide before a mutation; never retry after an unsupported kwarg."""
        try:
            parameters = inspect.signature(method).parameters.values()
        except (TypeError, ValueError):
            return False
        return any(
            parameter.name == "source"
            or parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in parameters
        )

    def submit_order(self, **kwargs: Any) -> BrokerSubmissionResult:
        if self._accepts_source(self._gateway.submit_order):
            kwargs["source"] = self._source
        return self._gateway.submit_order(**kwargs)

    def is_ambiguous_submission_error(self, error: BaseException) -> bool:
        return self._gateway.is_ambiguous_submission_error(error)

    def cancel_order(self, **kwargs: Any) -> BrokerOrderStatusSnapshot:
        if self._accepts_source(self._gateway.cancel_order):
            kwargs["source"] = self._source
        return self._gateway.cancel_order(**kwargs)

    def get_order(self, **kwargs: Any) -> List[BrokerOrderStatusSnapshot]:
        return self._gateway.get_order(**kwargs)

    def discover_orders(self, **kwargs: Any) -> BrokerOrderDiscoveryResult:
        return self._gateway.discover_orders(**kwargs)

    def get_positions(self, **kwargs: Any) -> Dict[str, Any]:
        return self._gateway.get_positions(**kwargs)


def _resolved_mode(gateway_or_broker: Any) -> ExecutionMode:
    """``gateway`` parameters throughout this module accept anything
    conforming to ``Broker`` (matching every existing legacy call site's
    own ``broker: Optional[Broker]`` flexibility -- e.g.
    ``build_buyboard_runtime``'s own ``broker=`` override, which tests use
    to inject a bare hand-rolled fake, not necessarily an
    ``ExecutionCommandGateway``). A plain ``Broker`` has no concept of
    ``GUARDED_ENGINE`` mode at all, so it is treated as
    ``LEGACY_COMPATIBILITY`` -- the only mode meaningful for something
    that doesn't expose a ``mode`` in the first place.
    """
    return getattr(gateway_or_broker, "mode", ExecutionMode.LEGACY_COMPATIBILITY)


def _legacy_broker_with_source(
    gateway_or_broker: Any, source: ExecutionSource
) -> Any:
    """Wrap every legacy broker while preserving its exact call signature."""
    return _SourceBoundGatewayBroker(gateway_or_broker, source)


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
    attempt_deadline_at: Optional[str] = None,
    strategy_instance_id: str = "",
    emergency: bool = False,
    **legacy_kwargs: Any,
) -> ExecutionSubmissionResult:
    """The single shared submission entry point (INV-21).

    ``client_order_id``, in ``GUARDED_ENGINE`` mode: pass the *same*
    identity you used on a prior attempt of this exact logical submission
    to replay it safely (a duplicate is rejected, never resubmitted to the
    broker); omit it for a genuinely new submission, and a fresh one is
    minted here, once.

    ``strategy_instance_id`` is required (non-blank) whenever ``source``
    is ``KANBAN_BOARD`` and the target symbol's persisted ownership is
    ``KANBAN`` (H1) -- the gateway rejects a blank or mismatched one.

    ``**legacy_kwargs`` are ``submit_guarded_overseas_order``'s own
    remaining parameters (``pre_trade_risk_decision``,
    ``execution_authority``, ``execution_lease``, etc.), used only in
    ``LEGACY_COMPATIBILITY`` mode and passed through unchanged.
    """
    ownership_engine = legacy_kwargs.get("lease_engine")
    resolved_gateway = gateway or (
        get_legacy_execution_gateway(ownership_engine)
        if ownership_engine is not None
        else get_default_execution_gateway()
    )
    if _resolved_mode(resolved_gateway) == ExecutionMode.GUARDED_ENGINE:
        stable_id = client_order_id or generate_client_order_id(environment, account_no, symbol, side, intent)
        pre_trade_risk_decision = legacy_kwargs.pop(
            "pre_trade_risk_decision", None
        )
        risk_strategy_id = str(legacy_kwargs.pop("strategy_id", "") or "")
        risk_plan_id = str(legacy_kwargs.pop("plan_id", "") or "")
        request = SubmitExecutionRequest(
            client_order_id=stable_id, environment=environment, account_no=account_no, symbol=symbol,
            side=side, intent=intent, quantity=quantity, limit_price=limit_price, exchange=exchange,
            execution_policy=execution_policy, attempt_group_id=attempt_group_id,
            attempt_number=attempt_number, attempt_deadline_at=attempt_deadline_at,
            lease=lease, source=source,
            strategy_instance_id=strategy_instance_id,
            emergency=emergency,
            pre_trade_risk_decision=pre_trade_risk_decision,
            risk_strategy_id=risk_strategy_id,
            risk_plan_id=risk_plan_id,
        )
        return ExecutionSubmissionResult.from_execution_order(
            resolved_gateway.submit_guarded(request)
        )
    order = submit_guarded_overseas_order(
        environment=environment, account_no=account_no, symbol=symbol, side=side, intent=intent,
        quantity=quantity, limit_price=limit_price, exchange=exchange, execution_policy=execution_policy,
        path=path, broker=_legacy_broker_with_source(resolved_gateway, source),
        attempt_deadline_at=attempt_deadline_at, **legacy_kwargs,
    )
    return ExecutionSubmissionResult.from_broker_order(order)


def request_exit_submit(
    *,
    source: ExecutionSource,
    command: ExitExecutionCommand,
    **kwargs: Any,
) -> ExecutionSubmissionResult:
    """Submit the exact frontend-neutral L3 exit command."""

    passthrough = dict(kwargs)
    for owned_field in (
        "environment",
        "account_no",
        "symbol",
        "side",
        "intent",
        "quantity",
        "limit_price",
        "exchange",
        "execution_policy",
        "emergency",
    ):
        passthrough.pop(owned_field, None)
    return request_submit(
        source=source,
        environment=command.environment,
        account_no=command.account_no,
        symbol=command.symbol,
        side=command.side,
        intent=command.intent,
        quantity=command.quantity,
        limit_price=command.limit_price,
        exchange=command.exchange,
        execution_policy=command.execution_policy,
        emergency=command.emergency,
        **passthrough,
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
    strategy_instance_id: str = "",
    emergency: bool = False,
    protective_entry_completion: bool = False,
    symbol: str = "",
    broker_order_id: str = "",
    quantity: int = 0,
    side: str = "",
    exchange: str = "NASD",
    ownership_engine=None,
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
    resolved_gateway = gateway or (
        get_legacy_execution_gateway(ownership_engine)
        if ownership_engine is not None
        else get_default_execution_gateway()
    )
    if _resolved_mode(resolved_gateway) == ExecutionMode.GUARDED_ENGINE:
        stable_cancel_id = cancel_command_id or f"{client_order_id}:{uuid4().hex[:12]}"
        request = CancelExecutionRequest(
            client_order_id=client_order_id, cancel_command_id=stable_cancel_id,
            environment=environment, account_no=account_no, lease=lease, source=source,
            strategy_instance_id=strategy_instance_id,
            emergency=emergency,
            protective_entry_completion=protective_entry_completion,
            symbol=symbol, broker_order_id=broker_order_id,
            quantity=quantity, side=side, exchange=exchange,
        )
        return resolved_gateway.cancel_guarded(request)
    return cancel_and_reconcile_order(
        client_order_id,
        path=path,
        broker=_legacy_broker_with_source(resolved_gateway, source),
    )


def request_cancel_intent(
    intent: CancelIntent,
    *,
    gateway: Optional[ExecutionCommandGateway] = None,
    path: Path = ORDERS_FILE,
) -> Any:
    """Route a complete cancellation intent through the shared workflow."""
    return request_cancel(
        source=intent.source,
        client_order_id=intent.client_order_id,
        gateway=gateway,
        path=path,
        cancel_command_id=intent.cancel_command_id,
        lease=intent.lease,
        environment=intent.environment,
        account_no=intent.account_no,
        strategy_instance_id=intent.strategy_instance_id,
        emergency=intent.emergency,
        protective_entry_completion=intent.protective_entry_completion,
        symbol=intent.symbol,
        broker_order_id=intent.broker_order_id,
        quantity=intent.quantity,
        side=intent.side,
        exchange=intent.exchange,
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
    strategy_instance_id: str = "",
    pre_trade_risk_decision: Any = None,
    risk_strategy_id: str = "",
    risk_plan_id: str = "",
) -> ExecutionOrderRecord:
    """``GUARDED_ENGINE`` only -- no legacy or Kanban call site performs a
    broker-level replace today (confirmed by codebase survey); raises
    ``NotImplementedError`` in ``LEGACY_COMPATIBILITY`` mode, same as the
    gateway's own ``replace_guarded`` would if reached directly.

    A production BUY/ENTRY replacement must carry a fresh portfolio decision
    built with ``PortfolioRiskManager.evaluate_entry(...,
    replaced_reservation_id=original.capital_reservation_id)``. The gateway
    validates and transfers that reservation before cancelling the original.
    """
    resolved_gateway = gateway or get_default_execution_gateway()
    if _resolved_mode(resolved_gateway) != ExecutionMode.GUARDED_ENGINE:
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
        strategy_instance_id=strategy_instance_id,
        pre_trade_risk_decision=pre_trade_risk_decision,
        risk_strategy_id=risk_strategy_id,
        risk_plan_id=risk_plan_id,
    )
    return resolved_gateway.replace_guarded(request)


# -- Kanban workflow/projection boundary (Workstream 13 / INV-21) ---------


def _load_board_types():
    # Kept local so broker-only users of this module do not pay for board
    # repository/table setup and to avoid widening the import surface of the
    # legacy execution path above.
    from src.core import board_workflow

    return board_workflow


def _move_board_card(card, target_status) -> None:
    from src.core.kanban_transitions import (
        InvalidBoardTransitionError,
        validate_board_transition,
    )

    try:
        validate_board_transition(card.board_status, target_status)
    except InvalidBoardTransitionError as exc:
        raise BoardCommandRejectedError(str(exc)) from exc
    card.previous_board_status = card.board_status
    card.board_status = target_status


def _board_action_name(command, card) -> str:
    types = _load_board_types()
    if isinstance(command, types.ActivateForToday):
        return "NEW_ENTRY"
    if isinstance(command, types.CancelEntry) and card.entry_client_order_id:
        return "KNOWN_CANCEL"
    if isinstance(
        command,
        (
            types.RequestPartialSell,
            types.RequestSellAll,
            types.CancelPartialSell,
            types.CancelQueuedSellAll,
            types.SetOrbStop,
            types.SetBreakevenStop,
            types.SetManualStop,
        ),
    ):
        return "PROTECTIVE_EXIT"
    return "PRESENTATION"


def _is_breakout_plan_command(command) -> bool:
    types = _load_board_types()
    return isinstance(command, (types.SetBreakoutPrice, types.ClearBreakoutPrice))


def _require_breakout_plan_mutation_policy(command, card, context) -> None:
    """Authorize canonical breakout edits independently of executor state.

    Setting a breakout price is planning metadata until a fill is confirmed,
    so BUYLIST, BUY_TODAY, and an unfilled ENTRY_PENDING card may be revised
    regardless of regular-session state. Clearing an active Buy Today plan is
    still a lifecycle change and retains its session fence. Every direct edit
    requires verified local Operator Control; the operator-command consumer
    supplies the same flag only after consuming a durably authorized request.
    """

    if not _is_breakout_plan_command(command):
        return
    types = _load_board_types()
    if isinstance(command, types.SetBreakoutPrice):
        try:
            price = float(command.price)
            buffer_pct = float(command.buffer_pct)
        except (TypeError, ValueError, OverflowError) as exc:
            raise BoardCommandRejectedError(
                "Breakout price and buffer must be finite numbers"
            ) from exc
        if not math.isfinite(price) or price <= 0:
            raise BoardCommandRejectedError(
                "Breakout price must be a finite positive number"
            )
        if not math.isfinite(buffer_pct) or not 0.0 <= buffer_pct <= 1.0:
            raise BoardCommandRejectedError(
                "ORB buffer must be between 0% and 100%"
            )
    if not context.local_operator_control:
        raise BoardCommandRejectedError(
            "Only the current Operator Control owner may change breakout planning"
        )
    if (
        isinstance(command, types.ClearBreakoutPrice)
        and context.regular_session_open is None
    ):
        raise BoardCommandRejectedError(
            "Market-session state is unavailable; breakout planning fails closed"
        )
    if card is None:
        return

    from src.core.trade_card_state import BoardStatus

    allowed = {
        BoardStatus.WATCHLIST,
        BoardStatus.BUYLIST,
        BoardStatus.BUY_TODAY,
    }
    if isinstance(command, types.SetBreakoutPrice):
        allowed.add(BoardStatus.ENTRY_PENDING)
    if card.board_status not in allowed:
        raise BoardCommandRejectedError(
            f"Cannot change breakout planning from {card.board_status.value}"
        )
    if card.board_status == BoardStatus.WATCHLIST and not card.watchlist_member:
        raise BoardCommandRejectedError(
            "This Watchlist candidate was removed; add it to Watchlist again "
            "before changing its breakout target"
        )
    if (
        isinstance(command, types.ClearBreakoutPrice)
        and card.board_status == BoardStatus.BUY_TODAY
        and context.regular_session_open is True
    ):
        raise BoardCommandRejectedError(
            "The published Buy Today plan is immutable during regular market hours"
        )


def _is_execution_affecting(command, card) -> bool:
    return _board_action_name(command, card) != "PRESENTATION"


def _require_current_board_runtime(command, card, context) -> None:
    if not context.enforce_runtime_fences:
        return
    if (
        command.expected_readiness_generation
        and command.expected_readiness_generation != context.readiness_generation
    ):
        raise BoardRuntimeFenceError(
            "Engine readiness changed since this card was rendered; refresh before retrying"
        )
    if not _is_execution_affecting(command, card):
        return
    # A BUY_TODAY cancellation only withdraws monitoring and cannot touch a
    # broker.  It remains available while the execution engine is disabled.
    types = _load_board_types()
    local_cancel = isinstance(command, types.CancelEntry) and not card.entry_client_order_id
    if not context.engine_enabled and not local_cancel:
        raise BoardRuntimeFenceError(
            "The Buy Board execution engine is disabled; no order action was recorded"
        )
    if context.reconciliation_in_progress:
        raise BoardRuntimeFenceError(
            "Broker reconciliation is in progress for this account; refresh after it completes"
        )
    if not context.device_active:
        raise BoardRuntimeFenceError(
            "This device is not the active execution owner; the request was not recorded"
        )
    if not context.action_ready:
        reason = "; ".join(context.restriction_reasons) or "runtime readiness is incomplete"
        raise BoardRuntimeFenceError(f"Action blocked: {reason}")


def _require_kanban_board_ownership(
    engine, command, card, context, *, ownership=None
):
    """Recheck durable symbol ownership for every execution-affecting UI action."""
    if not context.enforce_runtime_fences or not _is_execution_affecting(command, card):
        return None
    from src.services.execution_ownership_repository import get_ownership

    if ownership is None:
        try:
            ownership = get_ownership(
                engine,
                environment=card.environment,
                account_no=card.account_no,
                symbol=card.symbol,
            )
        except Exception as exc:
            raise BoardOwnershipMismatchError(
                "Execution ownership could not be verified; Kanban fails closed"
            ) from exc
    if ownership.owner != ExecutionOwner.KANBAN:
        raise BoardOwnershipMismatchError(
            f"{card.symbol} is {ownership.owner.value}-owned; Kanban may observe but cannot mutate it"
        )
    if not ownership.strategy_instance_id:
        raise BoardOwnershipMismatchError(
            f"{card.symbol} has invalid KANBAN ownership without a strategy identity"
        )
    if (
        command.expected_execution_owner
        and command.expected_execution_owner != ownership.owner.value
    ):
        raise BoardOwnershipMismatchError(
            "Execution ownership changed since this card was rendered"
        )
    if (
        command.expected_ownership_version
        and command.expected_ownership_version != ownership.version
    ):
        raise BoardOwnershipMismatchError(
            "Execution ownership revision changed since this card was rendered"
        )
    if (
        command.expected_strategy_instance_id
        and command.expected_strategy_instance_id != ownership.strategy_instance_id
    ):
        raise BoardOwnershipMismatchError(
            "Kanban strategy ownership changed since this card was rendered"
        )
    return ownership


def _active_owned_orders(engine, card) -> List[ExecutionOrderRecord]:
    from src.services.execution_order_repository import list_execution_orders_for_card

    return [
        order
        for order in list_execution_orders_for_card(
            engine,
            environment=card.environment,
            account_no=card.account_no,
            symbol=card.symbol,
        )
        if order.status not in TERMINAL_EXECUTION_ORDER_STATUSES
    ]


def _card_tracks_order(card, order: ExecutionOrderRecord) -> bool:
    """Return whether durable card correlation owns this active order."""
    if order.client_order_id and order.client_order_id in {
        card.entry_client_order_id,
        card.exit_client_order_id,
    }:
        return True
    return bool(
        order.attempt_group_id
        and order.attempt_group_id
        in {card.entry_attempt_group_id, card.exit_attempt_group_id}
    )


def _active_external_orders(engine, card):
    from src.core.discovered_external_order import ExternalOrderDisposition
    from src.services.discovered_external_order_repository import (
        list_discovered_external_orders_for_account,
    )

    terminal = {
        ExecutionOrderStatus.FILLED,
        ExecutionOrderStatus.CANCELLED,
        ExecutionOrderStatus.EXPIRED,
        ExecutionOrderStatus.REJECTED,
    }
    return [
        order
        for order in list_discovered_external_orders_for_account(
            engine,
            environment=card.environment,
            account_no=card.account_no,
        )
        if order.symbol == card.symbol
        and order.disposition == ExternalOrderDisposition.DISCOVERED_UNOWNED
        and order.broker_status not in terminal
    ]


def _require_board_action_not_conflicted(engine, command, card) -> List[ExecutionOrderRecord]:
    types = _load_board_types()
    from src.core.trade_card_state import (
        BoardStatus,
        PositionRuntimeStatus,
        has_durable_execution_evidence,
    )
    from src.services.execution_order_repository import list_execution_orders_for_card

    owned_orders = list_execution_orders_for_card(
        engine,
        environment=card.environment,
        account_no=card.account_no,
        symbol=card.symbol,
    )
    active_orders = [
        order
        for order in owned_orders
        if order.status not in TERMINAL_EXECUTION_ORDER_STATUSES
    ]

    def has_durable_entry_or_position_evidence() -> bool:
        return bool(active_orders or has_durable_execution_evidence(card))

    def has_confirmed_fill_or_position_evidence() -> bool:
        return bool(
            max(0, int(card.broker_quantity or 0)) > 0
            or max(0, int(card.orderable_quantity or 0)) > 0
            or float(card.average_entry_price or 0.0) > 0
            or card.position_runtime_status != PositionRuntimeStatus.NONE
            or any(
                max(0, int(order.filled_quantity or 0)) > 0
                or order.status == ExecutionOrderStatus.FILLED
                for order in owned_orders
            )
        )

    if isinstance(command, types.SetBreakoutPrice):
        if has_confirmed_fill_or_position_evidence():
            raise BoardCommandRejectedError(
                "Breakout price cannot change after a fill or position is confirmed"
            )
        if (
            card.board_status == BoardStatus.WATCHLIST
            and has_durable_entry_or_position_evidence()
        ):
            raise BoardCommandRejectedError(
                "Breakout planning cannot change on a hidden Watchlist card while execution evidence exists"
            )
        # Changing the reference price does not mutate or replace an
        # already-persisted unfilled order. Its identity, reservation, and
        # reconciliation lifecycle are preserved by _apply_board_mutation.
        return active_orders
    elif isinstance(command, types.ClearBreakoutPrice):
        if has_durable_entry_or_position_evidence():
            raise BoardCommandRejectedError(
                "Breakout planning cannot be removed after entry, order, reservation, or position evidence exists"
            )

    planning_stage_move = isinstance(command, types.MoveToWatchlist) or (
        isinstance(command, types.MoveToBuylist)
        and card.board_status == BoardStatus.WATCHLIST
    )
    if (
        isinstance(command, types.MoveToBuylist)
        and card.board_status == BoardStatus.WATCHLIST
        and not card.watchlist_member
    ):
        raise BoardCommandRejectedError(
            "This Watchlist candidate was removed; add it again before promotion"
        )
    if planning_stage_move and has_durable_entry_or_position_evidence():
        raise BoardCommandRejectedError(
            "Planning membership cannot change while order, reservation, or "
            "position evidence exists"
        )
    if isinstance(command, types.MoveToWatchlist) and _active_external_orders(
        engine, card
    ):
        # WATCHLIST is intentionally hidden from the execution board.  Moving
        # a card there while an unowned broker order is attached would also
        # hide that order's mandatory alert row from the operator.
        raise BoardCommandRejectedError(
            "Planning membership cannot hide an active unowned external broker order"
        )

    # BUY_TODAY -> BUYLIST is a harmless presentation change only while no
    # entry identity has been consumed.  The runtime persists that identity
    # before crossing the broker boundary, so allowing the drag afterward
    # could hide a BUY that becomes WORKING a moment later.  Once any such
    # evidence exists the caller must request CancelEntry and wait for
    # terminal broker reconciliation to perform the actual return.
    if isinstance(command, types.MoveToBuylist):
        entry_lifecycle_active = card.board_status.value == "ENTRY_PENDING" or (
            card.board_status.value == "BUY_TODAY"
            and (
                card.entry_client_order_id
                or card.entry_submission_unresolved
                or card.entry_cancel_in_flight
                or any(order.side == OrderSide.BUY for order in active_orders)
            )
        )
        if entry_lifecycle_active:
            raise BoardCommandRejectedError(
                "A BUY identity/order already exists; request entry cancellation and wait for broker-confirmed terminal reconciliation"
            )
        return active_orders

    if isinstance(command, (types.MoveToWatchlist, types.ReorderCard)):
        return active_orders
    if card.entry_submission_unresolved or card.exit_submission_unresolved:
        raise BoardCommandRejectedError(
            "An ambiguous order is awaiting reconciliation; no new board action is allowed"
        )
    if card.entry_cancel_in_flight or card.exit_cancel_in_flight:
        raise BoardCommandRejectedError(
            "A cancellation is already unresolved; wait for broker reconciliation"
        )
    if isinstance(
        command, (types.SetOrbStop, types.SetBreakevenStop, types.SetManualStop)
    ) and card.pending_stop_command_id:
        raise BoardCommandRejectedError(
            "A stop change is still synchronizing with live market data; wait for runtime acknowledgement"
        )
    if _active_external_orders(engine, card):
        raise BoardCommandRejectedError(
            "An unowned external broker order is active for this symbol; it must remain separate and be resolved or explicitly adopted"
        )

    ambiguous = [
        order
        for order in active_orders
        if order.status == ExecutionOrderStatus.UNKNOWN_SUBMISSION_STATE
        or order.broker_identity_status == BrokerIdentityStatus.AMBIGUOUS
    ]
    if ambiguous:
        raise BoardCommandRejectedError(
            "An owned order has ambiguous broker identity; reconcile it before another UI action"
        )
    unlinked = [order for order in active_orders if not _card_tracks_order(card, order)]
    if unlinked:
        raise BoardCommandRejectedError(
            "An active unlinked owned broker order fences execution; explicitly link or resolve it before another execution action"
        )
    if isinstance(command, types.CancelEntry) and card.entry_block_reason == "cancel_requested":
        raise BoardCommandRejectedError("Entry cancellation is already pending")
    if isinstance(command, types.RequestPartialSell):
        if any(order.side == OrderSide.SELL for order in active_orders):
            raise BoardCommandRejectedError(
                "A sell order is already pending for this symbol"
            )
        if card.board_status.value == "SELL_ALL" and (
            card.exit_client_order_id
            or card.exit_pending_attempt_number
            or card.reserved_sell_quantity
            or card.exit_cancel_command_id
        ):
            raise BoardCommandRejectedError(
                "Sell All has already reached the durable execution lifecycle; "
                "wait for broker reconciliation before reducing the objective"
            )
    if isinstance(command, types.RequestSellAll) and card.board_status.value == "SELL_ALL":
        raise BoardCommandRejectedError("Sell All is already pending")
    if isinstance(command, types.CancelQueuedSellAll) and (
        card.exit_client_order_id
        or any(order.side == OrderSide.SELL for order in active_orders)
    ):
        raise BoardCommandRejectedError(
            "The market-open SELL has already reached the execution lifecycle; it cannot be withdrawn as a local queue gesture"
        )
    return active_orders


def _apply_board_mutation(command, card, *, context=None, active_orders=()) -> None:
    from src.core.trade_card_state import (
        BoardStatus,
        EntryRuntimeStatus,
        PositionRuntimeStatus,
        StopType,
        has_durable_execution_evidence,
    )
    from src.services.position_manager import (
        compute_breakeven_stop_price,
        minimum_manual_stop_price,
    )

    types = _load_board_types()

    def clear_executable_entry_plan() -> None:
        card.selected_orb_window = None
        card.position_percent = 0.0
        card.planned_quantity = 0
        card.target_position_quantity = 0
        card.entry_orb_window = None
        card.entry_orb_high = None
        card.entry_orb_low = None
        card.entry_trigger = None
        card.stop_adr = None
        card.entry_block_reason = ""
        card.next_retry_at = None
        card.entry_attempt_group_id = ""
        card.entry_attempt_count = 0
        card.entry_client_order_id = ""
        card.entry_pending_attempt_number = 0
        card.entry_submission_unresolved = False
        card.entry_cancel_in_flight = False
        card.entry_cancel_reason = ""
        card.entry_cancel_command_id = ""
        card.entry_remaining_target_quantity = 0
        card.capital_reservation_id = ""
        card.market_data_last_trusted_price = None
        card.market_data_last_trusted_at = None
        card.market_data_outage_started_at = None
        card.market_data_outage_risk_tier = ""

    if isinstance(command, types.SetBreakoutPrice):
        try:
            previous_breakout = float(card.breakout_price or 0.0)
        except (TypeError, ValueError, OverflowError):
            previous_breakout = 0.0
        had_canonical_target = bool(
            math.isfinite(previous_breakout) and previous_breakout > 0
        )
        preserve_unfilled_execution = bool(
            active_orders or has_durable_execution_evidence(card)
        )
        if not preserve_unfilled_execution:
            clear_executable_entry_plan()
        # The Buy Board header is a default for a genuinely new plan. A target
        # revision on an existing plan (especially BUY_TODAY) keeps its frozen
        # buffer across devices and executor handoff.
        if (
            card.board_status in {BoardStatus.WATCHLIST, BoardStatus.BUYLIST}
            and not had_canonical_target
        ):
            card.buffer_pct = float(command.buffer_pct)
        card.breakout_price = float(command.price)
        if card.board_status == BoardStatus.WATCHLIST:
            card.watchlist_member = True
            card.buylist_member = False
        else:
            card.buylist_member = True
        if not preserve_unfilled_execution:
            card.buy_today_note = ""
            card.entry_runtime_status = (
                EntryRuntimeStatus.ORB_FORMING
                if card.board_status == BoardStatus.BUY_TODAY
                else None
            )
        return

    if isinstance(command, types.ClearBreakoutPrice):
        if card.board_status == BoardStatus.BUY_TODAY:
            _move_board_card(card, BoardStatus.BUYLIST)
        clear_executable_entry_plan()
        card.breakout_price = None
        if card.board_status == BoardStatus.WATCHLIST:
            card.watchlist_member = True
            card.buylist_member = False
        else:
            card.buylist_member = True
        card.session_date = None
        card.buy_today_note = ""
        card.entry_runtime_status = None
        return

    def request_stop_change(stop_type, price: float) -> None:
        requested_at = command.requested_at
        if requested_at.tzinfo is None:
            from datetime import timezone

            requested_at = requested_at.replace(tzinfo=timezone.utc)
        stop_command = build_stop_change_command(
            environment=card.environment,
            account_no=card.account_no,
            symbol=card.symbol,
            stop_type=stop_type,
            price=price,
            quantity=card.broker_quantity,
        )
        card.pending_stop_type = stop_command.stop_type
        card.pending_stop_price = stop_command.price
        card.pending_stop_quantity = stop_command.quantity
        card.pending_stop_command_id = command.command_id
        card.pending_stop_requested_at = requested_at
    if isinstance(command, types.CancelEntry):
        if card.board_status == BoardStatus.BUY_TODAY and not card.entry_client_order_id:
            _move_board_card(card, BoardStatus.BUYLIST)
            card.buy_today_note = ""
            card.entry_runtime_status = None
            card.entry_block_reason = ""
            card.entry_attempt_group_id = ""
            card.entry_attempt_count = 0
            card.entry_client_order_id = ""
            card.entry_pending_attempt_number = 0
            card.entry_submission_unresolved = False
        elif card.board_status in (BoardStatus.BUY_TODAY, BoardStatus.ENTRY_PENDING):
            card.entry_block_reason = "cancel_requested"
        else:
            raise BoardCommandRejectedError(
                f"Cannot cancel an entry from {card.board_status.value}"
            )
        return

    if isinstance(command, types.RequestPartialSell):
        source_status = card.board_status
        if source_status not in {BoardStatus.OPEN_POSITION, BoardStatus.SELL_ALL}:
            raise BoardCommandRejectedError(
                "Partial sell can only be requested from Open Positions or an unsubmitted Sell All"
            )
        if command.quantity <= 0:
            raise BoardCommandRejectedError("Partial-sell quantity must be positive")
        orderable = card.orderable_quantity or card.broker_quantity
        if orderable <= 0:
            raise BoardCommandRejectedError("No broker-confirmed orderable quantity to sell")
        if command.quantity >= orderable:
            if source_status != BoardStatus.SELL_ALL:
                _move_board_card(card, BoardStatus.SELL_ALL)
            card.exit_all_required = True
            card.pending_partial_sell_quantity = 0
        else:
            if source_status == BoardStatus.SELL_ALL:
                # Retire the old liquidation objective before creating the
                # independent partial-exit lifecycle.  The conflict checks
                # above prove no working/ambiguous SELL can be orphaned.
                card.sell_all_at_market_open = False
                card.exit_all_required = False
                card.reserved_sell_quantity = 0
                card.next_exit_retry_at = None
                card.exit_attempt_count = 0
                card.exit_attempt_group_id = ""
                card.exit_client_order_id = ""
                card.exit_pending_attempt_number = 0
                card.exit_submission_unresolved = False
                card.last_exit_error = ""
                card.exit_cancel_in_flight = False
                card.exit_cancel_requested_at = None
                card.exit_cancel_command_id = ""
            _move_board_card(card, BoardStatus.PARTIAL_SELL)
            card.pending_partial_sell_quantity = command.quantity
            card.position_runtime_status = PositionRuntimeStatus.PARTIAL_EXIT_PENDING
        return

    if isinstance(command, types.CancelPartialSell):
        if card.board_status != BoardStatus.PARTIAL_SELL:
            raise BoardCommandRejectedError("No Partial Sell objective to withdraw")
        active_sell = any(order.side == OrderSide.SELL for order in active_orders)
        durable_exit_started = bool(
            active_sell
            or card.exit_client_order_id
            or card.exit_pending_attempt_number
            or card.reserved_sell_quantity
        )
        # Zero is the durable withdrawal signal consumed by the runtime.  A
        # known working order must stay visibly pending until cancellation
        # and broker reconciliation establish the remaining position.
        card.pending_partial_sell_quantity = 0
        card.next_exit_retry_at = None
        card.last_exit_error = ""
        if durable_exit_started:
            return
        _move_board_card(card, BoardStatus.OPEN_POSITION)
        card.position_runtime_status = PositionRuntimeStatus.OPEN
        card.reserved_sell_quantity = 0
        card.exit_attempt_group_id = ""
        card.exit_attempt_count = 0
        card.exit_client_order_id = ""
        card.exit_pending_attempt_number = 0
        card.exit_submission_unresolved = False
        card.exit_cancel_in_flight = False
        card.exit_cancel_requested_at = None
        card.exit_cancel_command_id = ""
        return

    if isinstance(command, types.SetOrbStop):
        if card.position_runtime_status == PositionRuntimeStatus.NONE:
            raise BoardCommandRejectedError("No open position to set a stop on")
        orb_low = float(card.entry_orb_low or 0.0)
        if orb_low <= 0:
            raise BoardCommandRejectedError("The frozen entry ORB low is unavailable")
        if card.active_stop_price and orb_low < card.active_stop_price:
            raise BoardCommandRejectedError(
                "Changing back to the ORB low would widen current stop protection"
            )
        request_stop_change(StopType.ORB_LOW, orb_low)
        return

    if isinstance(command, types.SetBreakevenStop):
        if card.position_runtime_status == PositionRuntimeStatus.NONE:
            raise BoardCommandRejectedError("No open position to set a stop on")
        breakeven = compute_breakeven_stop_price(card.average_entry_price)
        if card.active_stop_price and breakeven < card.active_stop_price:
            raise BoardCommandRejectedError(
                "Changing to breakeven would widen current stop protection"
            )
        request_stop_change(StopType.BREAKEVEN, breakeven)
        return

    if isinstance(command, types.SetManualStop):
        if card.position_runtime_status == PositionRuntimeStatus.NONE:
            raise BoardCommandRejectedError("No open position to set a stop on")
        minimum = minimum_manual_stop_price(card)
        if command.price < minimum:
            raise BoardCommandRejectedError(
                f"Manual stop {command.price} cannot widen risk below the minimum {minimum}"
            )
        request_stop_change(StopType.MANUAL_PRICE, command.price)
        return

    if isinstance(command, types.CancelQueuedSellAll):
        if card.board_status != BoardStatus.SELL_ALL or not card.sell_all_at_market_open:
            raise BoardCommandRejectedError("No queued market-open Sell All to cancel")
        _move_board_card(card, BoardStatus.OPEN_POSITION)
        card.sell_all_at_market_open = False
        card.exit_all_required = False
        card.position_runtime_status = PositionRuntimeStatus.OPEN
        return

    if isinstance(command, types.ReorderCard):
        card.kanban_priority = command.target_priority
        return

    targets = {
        types.MoveToWatchlist: BoardStatus.WATCHLIST,
        types.MoveToBuylist: BoardStatus.BUYLIST,
        types.ActivateForToday: BoardStatus.BUY_TODAY,
        types.RequestSellAll: BoardStatus.SELL_ALL,
    }
    target = targets.get(type(command))
    if target is None:
        raise BoardCommandRejectedError(
            f"Unrecognized board command type: {type(command).__name__}"
        )
    _move_board_card(card, target)
    if isinstance(command, types.MoveToWatchlist):
        clear_executable_entry_plan()
        card.watchlist_member = True
        card.buylist_member = False
        card.session_date = None
        card.entry_runtime_status = None
        card.buy_today_note = ""
    elif isinstance(command, types.MoveToBuylist):
        clear_executable_entry_plan()
        card.watchlist_member = True
        card.buylist_member = True
        card.buy_today_note = ""
        card.session_date = None
        card.entry_runtime_status = None
        card.entry_block_reason = ""
        if not card.entry_client_order_id and card.broker_quantity <= 0:
            card.entry_attempt_group_id = ""
            card.entry_attempt_count = 0
    elif isinstance(command, types.ActivateForToday):
        card.buy_today_note = ""
        if context is not None and context.session_date is not None:
            card.session_date = context.session_date
        else:
            from src.utils.market_calendar import current_or_next_nyse_session_date

            card.session_date = current_or_next_nyse_session_date()
        monitoring_command = build_entry_monitoring_command(
            environment=card.environment,
            account_no=card.account_no,
            symbol=card.symbol,
        )
        card.buylist_member = monitoring_command.enabled
        # Planning columns do not own current-session ORB runtime state.
        # Every activation starts a fresh observation; a target/allocation by
        # itself must never bypass the 1m/5m/30m ORB requirement.
        card.entry_runtime_status = EntryRuntimeStatus.ORB_FORMING
        card.entry_block_reason = ""
    elif isinstance(command, types.RequestSellAll):
        # This is durable liquidation intent only.  The engine cancels a
        # conflicting BUY, refreshes quantity, submits, and reconciliation
        # alone may eventually project CLOSED.
        card.exit_all_required = True
        card.pending_partial_sell_quantity = 0
        card.position_runtime_status = PositionRuntimeStatus.LIQUIDATING
        if context is not None and context.regular_session_open is False:
            card.sell_all_at_market_open = True
            card.position_runtime_status = PositionRuntimeStatus.QUEUED_FOR_OPEN


def request_board_action(
    engine,
    command,
    *,
    context=None,
    claim_kanban_ownership: bool = False,
):
    """Validate and persist one revision-aware Kanban workflow request.

    No broker method is called here.  Broker-affecting commands persist an
    intent consumed by the same trading engine/workflow gateway used by the
    legacy dashboard; broker reconciliation remains the only source of fills,
    quantities, and terminal card placement.  ``claim_kanban_ownership`` is
    restricted to those durable-intent commands and commits the explicit H2
    ownership cutover atomically with the card revision.
    """
    from src.core.board_workflow import (
        AdoptExternalOrder,
        BoardActionContext,
        BoardWorkflowResult,
        ActivateForToday,
        CancelPartialSell,
        CancelQueuedSellAll,
        RequestPartialSell,
        RequestSellAll,
        ClearBreakoutPrice,
        SetBreakevenStop,
        SetBreakoutPrice,
        SetManualStop,
        SetOrbStop,
    )
    from src.services import trade_card_repository
    from src.services.discovered_external_order_repository import (
        adopt_external_order_in_db,
        fetch_discovered_external_order,
    )
    from src.services.trade_card_repository import (
        TradeCardNotFoundError,
        TradeCardVersionConflictError,
    )
    from src.core.trade_card_state import BoardStatus, TradeCardState

    resolved_context = context or BoardActionContext()
    intent_only_types = (
        ActivateForToday,
        CancelPartialSell,
        RequestPartialSell,
        RequestSellAll,
        CancelQueuedSellAll,
        SetOrbStop,
        SetBreakevenStop,
        SetManualStop,
    )
    if claim_kanban_ownership and not isinstance(command, intent_only_types):
        raise BoardCommandRejectedError(
            "Only durable Kanban execution intent may claim symbol ownership"
        )
    if isinstance(command, AdoptExternalOrder):
        external = fetch_discovered_external_order(engine, command.external_order_id)
        if external is None or (
            external.environment,
            external.account_no,
            external.symbol,
        ) != (command.environment, command.account_no, command.symbol):
            raise BoardCommandRejectedError(
                "The selected external order no longer belongs to this UI scope"
            )
        adoption_card = trade_card_repository.get_trade_card(
            engine, command.environment, command.account_no, command.symbol
        )
        if command.expected_card_version:
            if adoption_card is None:
                raise TradeCardNotFoundError(
                    f"No trade card for {command.environment}:{command.account_no}:{command.symbol}"
                )
            if adoption_card.version != command.expected_card_version:
                raise TradeCardVersionConflictError(
                    f"Command {command.command_id} expected version "
                    f"{command.expected_card_version}, stored version is {adoption_card.version}"
                )
        record = adopt_external_order_in_db(
            engine,
            command.external_order_id,
            adopted_by=command.adopted_by,
        )
        return BoardWorkflowResult(
            card=adoption_card,
            command_id=command.command_id,
            adopted_execution_client_order_id=record.client_order_id,
        )

    card = trade_card_repository.get_trade_card(
        engine, command.environment, command.account_no, command.symbol
    )
    _require_breakout_plan_mutation_policy(command, card, resolved_context)
    if card is None:
        if isinstance(command, SetBreakoutPrice):
            if command.expected_card_version != 0:
                raise TradeCardNotFoundError(
                    f"No trade card for {command.environment}:{command.account_no}:{command.symbol}"
                )
            trade_card_repository.ensure_trade_cards_table(engine)
            with engine.begin() as conn:
                current = trade_card_repository.get_trade_card_in_transaction(
                    conn,
                    command.environment,
                    command.account_no,
                    command.symbol,
                    for_update=True,
                )
                if current is not None:
                    raise TradeCardVersionConflictError(
                        f"Command {command.command_id} expected a missing card, "
                        f"stored version is {current.version}"
                    )
                _require_breakout_plan_mutation_policy(
                    command, None, resolved_context
                )
                created = TradeCardState(
                    environment=command.environment,
                    account_no=command.account_no,
                    symbol=command.symbol,
                    board_status=BoardStatus.WATCHLIST,
                    watchlist_member=True,
                    buylist_member=False,
                )
                _apply_board_mutation(
                    command,
                    created,
                    context=resolved_context,
                    active_orders=(),
                )
                updated = trade_card_repository.insert_trade_card(conn, created)
            trade_card_repository.sync_trade_card_local_snapshot(updated)
            return BoardWorkflowResult(card=updated, command_id=command.command_id)
        raise TradeCardNotFoundError(
            f"No trade card for {command.environment}:{command.account_no}:{command.symbol}"
        )
    if card.version != command.expected_card_version:
        raise TradeCardVersionConflictError(
            f"Command {command.command_id} expected version "
            f"{command.expected_card_version}, stored version is {card.version}"
        )

    if isinstance(command, ActivateForToday):
        for other in trade_card_repository.list_trade_cards(
            engine,
            environment=command.environment,
            raise_on_error=True,
        ):
            if (
                str(other.symbol or "").strip().upper()
                == str(command.symbol or "").strip().upper()
                and str(other.account_no or "").strip()
                != str(command.account_no or "").strip()
                and other.board_status == BoardStatus.BUY_TODAY
            ):
                raise BoardCommandRejectedError(
                    f"{command.symbol} is already active in Buy Today for "
                    "another account. The ORB queue is symbol-scoped, so "
                    "only one account can activate that symbol at a time."
                )

    _require_current_board_runtime(command, card, resolved_context)

    active_orders = _require_board_action_not_conflicted(engine, command, card)
    # The final ownership check and card CAS share one transaction.  On
    # MySQL the locked rows prevent a same-symbol ownership transfer from
    # slipping between authorization and intent persistence.
    from src.services.execution_ownership_repository import (
        assign_ownership_in_transaction,
        ensure_execution_ownership_table,
        get_ownership_in_transaction,
    )
    from src.core.execution_config import KANBAN_STRATEGY_INSTANCE_ID
    from src.core.execution_ownership import ExecutionOwnership

    trade_card_repository.ensure_trade_cards_table(engine)
    ensure_execution_ownership_table(engine)
    stop_change_coordinator = None
    stop_change_scope = nullcontext()
    if isinstance(command, (SetOrbStop, SetBreakevenStop, SetManualStop)):
        from src.services.stop_change_coordinator import stop_change_coordinator_for

        stop_change_coordinator = stop_change_coordinator_for(engine)
        stop_change_scope = stop_change_coordinator.lock_cards([card.card_key])

    # A stop request owns the same per-card coordinator lock from its final
    # revision/ownership read through canonical commit and process-local
    # publication.  The runtime's feed drain takes this lock too, so it can
    # never evaluate an event after the commit while still being unaware of
    # the newly durable request.
    with stop_change_scope:
        with engine.begin() as conn:
            current = trade_card_repository.get_trade_card_in_transaction(
                conn,
                command.environment,
                command.account_no,
                command.symbol,
                for_update=True,
            )
            if current is None:
                raise TradeCardNotFoundError(
                    f"No trade card for {command.environment}:{command.account_no}:{command.symbol}"
                )
            if current.version != command.expected_card_version:
                raise TradeCardVersionConflictError(
                    f"Command {command.command_id} expected version "
                    f"{command.expected_card_version}, stored version is {current.version}"
                )
            _require_breakout_plan_mutation_policy(
                command, current, resolved_context
            )
            _require_current_board_runtime(command, current, resolved_context)
            ownership = get_ownership_in_transaction(
                conn,
                environment=current.environment,
                account_no=current.account_no,
                symbol=current.symbol,
                for_update=True,
            )
            if claim_kanban_ownership:
                target_strategy = str(KANBAN_STRATEGY_INSTANCE_ID or "").strip()
                if not target_strategy:
                    raise BoardCommandRejectedError(
                        "KANBAN_STRATEGY_INSTANCE_ID is blank; Kanban cannot claim execution ownership"
                    )
                if command.expected_execution_owner and (
                    command.expected_execution_owner != ownership.owner.value
                ):
                    raise BoardOwnershipMismatchError(
                        "Execution ownership changed since this card was rendered"
                    )
                if (
                    command.expected_execution_owner
                    and command.expected_ownership_version != ownership.version
                ):
                    raise BoardOwnershipMismatchError(
                        "Execution ownership revision changed since this card was rendered"
                    )
                if ownership.owner == ExecutionOwner.MANUAL:
                    raise BoardOwnershipMismatchError(
                        f"{command.symbol} is MANUAL-owned; explicit administrative transfer is required"
                    )
                if (
                    ownership.owner == ExecutionOwner.KANBAN
                    and ownership.strategy_instance_id != target_strategy
                ):
                    raise BoardOwnershipMismatchError(
                        f"{command.symbol} belongs to another Kanban strategy instance"
                    )
                if ownership.owner == ExecutionOwner.LEGACY:
                    ownership = assign_ownership_in_transaction(
                        conn,
                        ExecutionOwnership(
                            environment=current.environment,
                            account_no=current.account_no,
                            symbol=current.symbol,
                            owner=ExecutionOwner.KANBAN,
                            strategy_instance_id=target_strategy,
                            assigned_by=f"kanban_intent:{command.command_id[:16]}",
                        ),
                        expected_version=ownership.version,
                    )
            else:
                _require_kanban_board_ownership(
                    engine,
                    command,
                    current,
                    resolved_context,
                    ownership=ownership,
                )
            _apply_board_mutation(
                command,
                current,
                context=resolved_context,
                active_orders=active_orders,
            )
            updated = trade_card_repository.update_trade_card_in_transaction(
                conn, current, expected_version=command.expected_card_version
            )
        if stop_change_coordinator is not None:
            stop_change_coordinator.record_durable(updated)
    trade_card_repository.sync_trade_card_local_snapshot(updated)
    return BoardWorkflowResult(card=updated, command_id=command.command_id)


def project_board_card(
    engine,
    card,
    *,
    context=None,
    ownership=None,
    all_orders=None,
    external_orders=None,
):
    """Build one read-only projection from card, order, external, and owner truth."""
    import copy

    from src.core.board_workflow import BoardCardProjection, BoardProjectionContext
    from src.core.trade_card_state import BoardStatus
    from src.services.execution_ownership_repository import get_ownership

    projection_context = context or BoardProjectionContext()
    if ownership is None:
        ownership = get_ownership(
            engine,
            environment=card.environment,
            account_no=card.account_no,
            symbol=card.symbol,
        )
    if all_orders is None:
        all_orders = _active_owned_orders(engine, card)
    orders = [order for order in all_orders if _card_tracks_order(card, order)]
    unlinked_orders = [order for order in all_orders if order not in orders]
    external = (
        _active_external_orders(engine, card)
        if external_orders is None
        else list(external_orders)
    )
    ambiguous_count = sum(
        order.status == ExecutionOrderStatus.UNKNOWN_SUBMISSION_STATE
        or order.broker_identity_status == BrokerIdentityStatus.AMBIGUOUS
        for order in orders
    )
    restrictions = [
        *projection_context.global_restrictions,
        *projection_context.restrictions_for(card.account_no),
    ]
    planning_only = card.board_status in {
        BoardStatus.WATCHLIST,
        BoardStatus.BUYLIST,
    }
    if not planning_only and ownership.owner != ExecutionOwner.KANBAN:
        restrictions.append(f"Observation only: execution owner is {ownership.owner.value}")
    if external:
        restrictions.append("Active unowned broker order fences execution")
    if unlinked_orders:
        restrictions.append("Unlinked owned broker order requires separate review")
    if card.entry_submission_unresolved or card.exit_submission_unresolved or ambiguous_count:
        restrictions.append("Ambiguous order requires reconciliation")
    if card.entry_cancel_in_flight or card.exit_cancel_in_flight:
        restrictions.append("Cancellation confirmation pending")
    reconciliation_blocked = projection_context.reconciliation_blocked_for(
        card.account_no
    )
    if reconciliation_blocked:
        restrictions.append("Account reconciliation incomplete or stale")
    return BoardCardProjection(
        # Never hand the UI the mutable aggregate instance held by a caller
        # (notably the runtime worker cache). A projection rendered at N must
        # stay at N even if reconciliation mutates its source object to N+1.
        card=copy.deepcopy(card),
        ownership_owner=ownership.owner.value,
        ownership_version=ownership.version,
        strategy_instance_id=ownership.strategy_instance_id,
        readiness_generation=projection_context.readiness_generation,
        reconciliation_blocked=reconciliation_blocked,
        engine_restrictions=tuple(dict.fromkeys(restrictions)),
        owned_order_statuses=tuple(order.status for order in orders),
        working_order_count=len(orders),
        ambiguous_order_count=int(ambiguous_count),
        unlinked_owned_orders=tuple(copy.deepcopy(unlinked_orders)),
        external_orders=tuple(copy.deepcopy(external)),
    )


def list_board_projections(
    engine,
    *,
    environment="PROD",
    context=None,
    board_statuses=None,
):
    import copy

    from src.core.board_workflow import (
        BoardExecutionOrderProjection,
        BoardExternalOrderProjection,
        BoardProjectionContext,
    )
    from src.core.discovered_external_order import ExternalOrderDisposition
    from src.core.execution_ownership import ExecutionOwnership
    from src.core.trade_card_state import BoardStatus
    from src.services import trade_card_repository
    from src.services.discovered_external_order_repository import (
        list_discovered_external_orders,
    )
    from src.services.execution_order_repository import list_execution_orders
    from src.services.execution_ownership_repository import (
        list_execution_ownership,
    )

    if engine is None:
        return []
    projection_context = context or BoardProjectionContext()
    cards = trade_card_repository.list_trade_cards(
        engine, environment=environment, raise_on_error=True
    )
    ownership_rows = list_execution_ownership(engine, environment=environment)
    all_execution_orders = list_execution_orders(engine, environment=environment)
    all_external_orders = list_discovered_external_orders(
        engine, environment=environment
    )
    if board_statuses is not None:
        visible_statuses = set(board_statuses)
        cards = [card for card in cards if card.board_status in visible_statuses]
    ownership_by_scope = {
        (row.environment, row.account_no, row.symbol): row
        for row in ownership_rows
    }
    owned_by_scope = {}
    for order in all_execution_orders:
        if order.status in TERMINAL_EXECUTION_ORDER_STATUSES:
            continue
        owned_by_scope.setdefault(
            (order.environment, order.account_no, order.symbol), []
        ).append(order)
    terminal = {
        ExecutionOrderStatus.FILLED,
        ExecutionOrderStatus.CANCELLED,
        ExecutionOrderStatus.EXPIRED,
        ExecutionOrderStatus.REJECTED,
    }
    external_by_scope = {}
    for order in all_external_orders:
        if (
            order.disposition != ExternalOrderDisposition.DISCOVERED_UNOWNED
            or order.broker_status in terminal
        ):
            continue
        external_by_scope.setdefault(
            (order.environment, order.account_no, order.symbol), []
        ).append(order)
    card_projections = []
    for card in cards:
        scope = (card.environment, card.account_no, card.symbol)
        ownership = ownership_by_scope.get(scope) or ExecutionOwnership(
            environment=card.environment,
            account_no=card.account_no,
            symbol=card.symbol,
        )
        card_projections.append(
            project_board_card(
                engine,
                card,
                context=context,
                ownership=ownership,
                all_orders=owned_by_scope.get(scope, ()),
                external_orders=external_by_scope.get(scope, ()),
            )
        )
    # Hidden lifecycle cards are still returned for lightweight mirror sync,
    # but they must never claim attached orders for visibility purposes.  A
    # pre-existing inconsistent WATCHLIST/CLOSED card with a live order must
    # leave that order visible as a standalone warning row.
    hidden_board_statuses = {BoardStatus.WATCHLIST, BoardStatus.CLOSED}
    visible_card_projections = [
        projection
        for projection in card_projections
        if projection.card.board_status not in hidden_board_statuses
    ]
    card_scopes = {
        (projection.card.environment, projection.card.account_no, projection.card.symbol)
        for projection in visible_card_projections
    }
    attached_external_ids = {
        external.external_order_id
        for projection in visible_card_projections
        for external in projection.external_orders
    }
    standalone_external = [
        BoardExternalOrderProjection(
            order=copy.deepcopy(order),
            readiness_generation=projection_context.readiness_generation,
            engine_restrictions=tuple(
                dict.fromkeys(
                    [
                        *projection_context.global_restrictions,
                        *projection_context.restrictions_for(order.account_no),
                        "Unowned broker order: observation only",
                    ]
                )
            ),
        )
        for order in all_external_orders
        if order.external_order_id not in attached_external_ids
        and order.disposition == ExternalOrderDisposition.DISCOVERED_UNOWNED
        and order.broker_status not in terminal
    ]
    standalone_owned = [
        BoardExecutionOrderProjection(
            order=copy.deepcopy(order),
            readiness_generation=projection_context.readiness_generation,
            engine_restrictions=tuple(
                dict.fromkeys(
                    [
                        *projection_context.global_restrictions,
                        *projection_context.restrictions_for(order.account_no),
                        "Unlinked owned broker order: observation only",
                    ]
                )
            ),
        )
        for order in all_execution_orders
        if order.status not in TERMINAL_EXECUTION_ORDER_STATUSES
        and (order.environment, order.account_no, order.symbol) not in card_scopes
    ]
    return [*card_projections, *standalone_external, *standalone_owned]


def get_board_projection_revision(engine, *, environment="PROD"):
    """Return one compact token covering every table used by the board.

    A timer can compare this single SQL result every three minutes and avoid both
    JSON transfer and UI rebuild when nothing changed.  The full projection
    remains normalized relational state; this token is only invalidation.
    """

    from sqlalchemy import func, literal, select, union_all
    from src.infrastructure.database.coordination_engine import (
        coordination_read_connection,
    )
    from src.services.discovered_external_order_repository import (
        ensure_discovered_external_orders_table,
    )
    from src.services.execution_order_repository import (
        ensure_execution_orders_table,
    )
    from src.services.execution_ownership_repository import (
        ensure_execution_ownership_table,
    )
    from src.services.trade_card_repository import ensure_trade_cards_table

    environment = str(environment or "PROD").upper()
    tables = (
        ("cards", ensure_trade_cards_table(engine)),
        ("owners", ensure_execution_ownership_table(engine)),
        ("orders", ensure_execution_orders_table(engine)),
        ("external", ensure_discovered_external_orders_table(engine)),
    )
    statements = []
    for name, table in tables:
        statements.append(
            select(
                literal(name).label("source"),
                func.count(table.c.id).label("row_count"),
                func.coalesce(func.sum(table.c.version), 0).label("version_sum"),
                func.max(table.c.updated_at).label("updated_at"),
            ).where(table.c.environment == environment)
        )
    with coordination_read_connection(engine) as conn:
        rows = conn.execute(union_all(*statements)).fetchall()
    return tuple(
        (str(row.source), int(row.row_count or 0), int(row.version_sum or 0), str(row.updated_at or ""))
        for row in rows
    )


def get_board_projection(
    engine, *, environment: str, account_no: str, symbol: str, context=None
):
    from src.services import trade_card_repository

    card = trade_card_repository.get_trade_card(
        engine, environment, account_no, symbol
    )
    if card is None:
        return None
    return project_board_card(engine, card, context=context)
