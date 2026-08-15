"""``ExecutionCommandGateway`` -- the single application component permitted
to invoke destructive broker operations (Workstream 3, B1).

``docs/kanban_production_readiness.md``, PR2 (Workstreams 3 + 9).
``submit_order``/``cancel_order`` conform to
:class:`~src.brokers.execution_broker_protocol.ExecutionBrokerProtocol`
(the existing :class:`~src.services.broker.Broker` protocol) exactly, which
is what lets this gateway be handed to
:func:`src.services.order_execution_service.submit_guarded_overseas_order`
and :func:`src.services.order_reconciliation.cancel_and_reconcile_order` as
their existing ``broker: Optional[Broker]`` dependency-injection parameter
-- no signature change to either function, no new call convention for
either caller to learn.

Two-mode dispatch (:mod:`src.core.execution_mode`):

- ``LEGACY_COMPATIBILITY`` (``BUYBOARD_ENGINE_ENABLED=false``, which is
  every production request today and for the duration of this whole
  program): a transparent pass-through to the real broker. No command
  journal, no capital reservation, no ``ExecutionOrderRecord`` -- the
  already-reviewed legacy guard sequence upstream of this gateway
  (kill-switch, pre-trade risk, lease, duplicate-order reservation) is
  completely unchanged, and this mode changes nothing about what actually
  reaches the broker or when.
- ``GUARDED_ENGINE`` (``BUYBOARD_ENGINE_ENABLED=true`` -- implemented and
  tested here, never selected in production by this PR): the full
  A1-A11/B1-B4 sequence -- atomic command+reservation+``PREPARED`` record,
  durably committed ``SUBMITTING`` before any broker call, exact-identity
  requirements for cancel, composed cancel-then-resubmit for replace.

Both modes share one thing regardless of which is active: a per
``(environment, account_no, symbol)`` mutual-exclusion claim (Workstream 9 --
"legacy background workers cannot continue issuing orders in parallel with
the gateway"), and every command this gateway journals in ``GUARDED_ENGINE``
mode records which frontend (:class:`~src.core.execution_mode.ExecutionSource`)
issued it.
"""
from __future__ import annotations

import logging
import math
import threading
from contextlib import contextmanager
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.engine import Engine

from src.brokers.execution_broker_protocol import Broker, BrokerSubmissionResult, KisBroker
from src.core.capital_reservation import CapitalReservation
from src.core.execution_mode import ExecutionMode, ExecutionSource, resolve_execution_mode
from src.core.execution_order_record import (
    ExecutionOrderRecord,
    ExecutionOrderStatus,
    apply_status_transition,
    is_cancellable,
    validate_consistency,
)
from src.core.order_recovery_state import OrderRecoveryState, validate_recovery_transition
from src.core.order_state import (
    REGULAR_LIMIT_EXECUTION,
    RESERVED_MOO_EXECUTION,
    BrokerOrderDiscoveryResult,
    BrokerOrderStatusSnapshot,
    OrderIntent,
    OrderSide,
    OrderStatus,
    generate_client_order_id,
)
from src.services import trading_state
from src.services.capital_reservation_repository import (
    ensure_capital_reservations_table,
    insert_reservation,
    update_reservation,
)
from src.services.execution_command_repository import (
    ExecutionCommand,
    ensure_execution_commands_table,
    insert_command,
    update_command_response,
)
from src.services.execution_lease_protocol import (
    DefaultExecutionLeaseProtocol,
    ExecutionLease,
    ExecutionLeaseProtocol,
)
from src.services.execution_order_repository import (
    ensure_execution_orders_table,
    fetch_execution_order,
    insert_execution_order,
    update_execution_order,
)

logger = logging.getLogger(__name__)


# --- exceptions ---------------------------------------------------------


class GuardedExecutionError(RuntimeError):
    """Base for every ``GUARDED_ENGINE``-mode gateway rejection. Never
    raised in ``LEGACY_COMPATIBILITY`` mode -- that mode's exceptions are
    whatever the real broker itself raises, unchanged."""


class ConcurrentExecutionOwnershipError(GuardedExecutionError):
    """Two different :class:`~src.core.execution_mode.ExecutionSource`
    values tried to hold a destructive command in flight for the same
    ``(environment, account_no, symbol)`` at once (Workstream 9). Raised
    regardless of :class:`~src.core.execution_mode.ExecutionMode` -- this
    is the one gate PR2 enforces even while ``LEGACY_COMPATIBILITY`` is
    active."""


class GuardedSubmissionRejectedError(GuardedExecutionError):
    """The broker explicitly rejected the submission (a clean, pre-
    acceptance rejection) -- never treated as a reason to retry."""


class GuardedSubmissionAmbiguousError(GuardedExecutionError):
    """Timeout, transport loss, or any other outcome the broker adapter
    cannot distinguish from "maybe accepted" -- INV-23: never retried
    automatically. The order is left ``UNKNOWN_SUBMISSION_STATE`` for
    reconciliation (PR3)."""


class CancelNotPermittedError(GuardedExecutionError):
    """:func:`~src.core.execution_order_record.is_cancellable` returned
    ``False`` -- no broker call was attempted."""


class GuardedCancellationRejectedError(GuardedExecutionError):
    """The broker explicitly refused the cancel request itself (the order
    had already progressed past the point a cancel could apply)."""


class GuardedCancellationAmbiguousError(GuardedExecutionError):
    """Timeout or transport loss on a cancel request -- per the frozen
    contract, "must not be blindly retried. It remains reconciliation work
    for PR3." Left ``CANCEL_PENDING`` with ``recovery_state=DISCOVERING``."""


class ReplaceNotSafeError(GuardedExecutionError):
    """``replace_order``'s cancel-then-resubmit could not establish a safe
    (``CANCELLED``) outcome for the order being replaced -- the new order
    is never submitted in this case; a fill or an ambiguous cancel outcome
    must be resolved (by reconciliation, or a fresh caller decision) before
    a replace can safely proceed."""


class OrderNotFoundForCancelError(GuardedExecutionError):
    pass


class GuardedEngineRequiresDatabaseError(GuardedExecutionError):
    """``GUARDED_ENGINE`` mode was selected but no database ``Engine`` was
    configured on this gateway instance. Fails closed -- there is no
    "guarded" behavior without the durable command journal/order record
    this mode's entire safety case depends on."""


# --- mutual exclusion (Workstream 9) -------------------------------------


class _OwnershipRegistry:
    """In-process, per-``(environment, account_no, symbol)`` mutual
    exclusion: only one :class:`~src.core.execution_mode.ExecutionSource`
    may have a destructive command in flight for a given key at a time,
    regardless of :class:`~src.core.execution_mode.ExecutionMode`. This is
    intentionally lighter than a full, persisted, multi-strategy
    ``execution_owner`` table (H1/H2's complete form, future work) -- it
    only prevents two *different* callers from racing the same account+
    symbol through this gateway at the same moment; it does not yet track
    a durable, long-lived ownership assignment per symbol.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._holders: Dict[Tuple[str, str, str], ExecutionSource] = {}

    @contextmanager
    def claim(self, key: Tuple[str, str, str], source: ExecutionSource):
        with self._lock:
            holder = self._holders.get(key)
            if holder is not None and holder != source:
                raise ConcurrentExecutionOwnershipError(
                    f"{key} already has an in-flight destructive command from "
                    f"{holder.value}; refusing a concurrent one from {source.value}"
                )
            self._holders[key] = source
        try:
            yield
        finally:
            with self._lock:
                if self._holders.get(key) == source:
                    self._holders.pop(key, None)


def _recovery_key(environment: str, account_no: str, symbol: str) -> Tuple[str, str, str]:
    return (str(environment or "").upper(), str(account_no or ""), str(symbol or "").upper())


def _as_source(source: Any) -> ExecutionSource:
    if isinstance(source, ExecutionSource):
        return source
    try:
        return ExecutionSource(str(source or "").upper())
    except ValueError:
        return ExecutionSource.SYSTEM


def _transition_recovery_state(record: ExecutionOrderRecord, target: OrderRecoveryState) -> None:
    validate_recovery_transition(record.recovery_state, target)
    record.recovery_state = target
    validate_consistency(record)


# --- the gateway ----------------------------------------------------------


class ExecutionCommandGateway:
    """Conforms to :class:`~src.brokers.execution_broker_protocol.ExecutionBrokerProtocol`
    (``Broker``) -- a drop-in replacement anywhere a ``Broker`` is already
    accepted."""

    def __init__(
        self,
        *,
        real_broker: Optional[Broker] = None,
        engine: Optional[Engine] = None,
        lease_protocol: Optional[ExecutionLeaseProtocol] = None,
        mode_override: Optional[bool] = None,
        ownership_registry: Optional[_OwnershipRegistry] = None,
    ) -> None:
        self._real_broker: Broker = real_broker if real_broker is not None else KisBroker()
        self._engine = engine
        self._lease_protocol: ExecutionLeaseProtocol = lease_protocol or DefaultExecutionLeaseProtocol(
            engine=engine
        )
        self._mode_override = mode_override
        self._ownership = ownership_registry or _OwnershipRegistry()

    def _mode(self) -> ExecutionMode:
        return resolve_execution_mode(self._mode_override)

    # --- ExecutionBrokerProtocol: submission ---------------------------------

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
        source: Any = ExecutionSource.SYSTEM,
        intent: OrderIntent = OrderIntent.UNKNOWN,
        lease: Optional[ExecutionLease] = None,
        attempt_group_id: str = "",
        attempt_number: int = 1,
    ) -> BrokerSubmissionResult:
        source = _as_source(source)
        key = _recovery_key(environment, account_no, symbol)
        with self._ownership.claim(key, source):
            if self._mode() == ExecutionMode.LEGACY_COMPATIBILITY:
                return self._real_broker.submit_order(
                    environment=environment, account_no=account_no, symbol=symbol, side=side,
                    quantity=quantity, limit_price=limit_price, exchange=exchange,
                    execution_policy=execution_policy,
                )
            return self._submit_guarded(
                environment=environment, account_no=account_no, symbol=symbol, side=side,
                intent=intent, quantity=quantity, limit_price=limit_price, exchange=exchange,
                execution_policy=execution_policy, source=source, lease=lease,
                attempt_group_id=attempt_group_id, attempt_number=attempt_number,
            )

    def is_ambiguous_submission_error(self, error: BaseException) -> bool:
        if isinstance(error, GuardedSubmissionAmbiguousError):
            return True
        if isinstance(error, GuardedExecutionError):
            return False
        try:
            return self._real_broker.is_ambiguous_submission_error(error)
        except Exception:
            # An invalid/custom real broker that cannot classify an error
            # must fail conservatively -- same posture as
            # order_execution_service.submit_guarded_overseas_order.
            return True

    # --- ExecutionBrokerProtocol: cancellation ---------------------------------

    def cancel_order(
        self,
        *,
        environment: str,
        account_no: str,
        is_reserved: bool = False,
        source: Any = ExecutionSource.SYSTEM,
        client_order_id: str = "",
        lease: Optional[ExecutionLease] = None,
        **kwargs: Any,
    ) -> BrokerOrderStatusSnapshot:
        source = _as_source(source)
        symbol = str(kwargs.get("symbol") or "").upper()
        key = _recovery_key(environment, account_no, symbol)
        with self._ownership.claim(key, source):
            if self._mode() == ExecutionMode.LEGACY_COMPATIBILITY:
                return self._real_broker.cancel_order(
                    environment=environment, account_no=account_no, is_reserved=is_reserved, **kwargs
                )
            if not client_order_id:
                raise ValueError(
                    "GUARDED_ENGINE cancel_order requires client_order_id to look up the "
                    "ExecutionOrderRecord -- a broker_order_id/symbol pair alone is not enough "
                    "to enforce exact-identity/permission/lease gating"
                )
            return self._cancel_guarded(client_order_id=client_order_id, lease=lease, source=source)

    # --- replace (not part of the base Broker protocol) ------------------------

    def replace_order(
        self,
        *,
        client_order_id: str,
        new_quantity: int,
        new_limit_price: float,
        source: Any = ExecutionSource.SYSTEM,
        lease: Optional[ExecutionLease] = None,
    ) -> BrokerSubmissionResult:
        """Composed, never a synthetic first-class broker status (per the
        frozen contract): cancel the existing exact order, confirm a safe
        (``CANCELLED``) outcome, then submit a brand-new order linked via
        ``replaces_execution_order_id``. The original record is never
        mutated into the replacement.

        Only available in ``GUARDED_ENGINE`` mode -- no legacy call site
        performs a broker-level replace today (confirmed by codebase
        survey: neither ``order_execution_service.py`` nor
        ``order_reconciliation.py`` has a replace path), so
        ``LEGACY_COMPATIBILITY`` mode has nothing to transparently pass
        through to.
        """
        if self._mode() != ExecutionMode.GUARDED_ENGINE:
            raise NotImplementedError(
                "replace_order is only available in GUARDED_ENGINE mode -- no legacy "
                "call site performs a broker-level replace today"
            )
        source = _as_source(source)
        original = self._fetch_record(client_order_id)
        if original is None:
            raise OrderNotFoundForCancelError(f"No ExecutionOrderRecord for client_order_id={client_order_id!r}")
        key = _recovery_key(original.environment, original.account_no, original.symbol)
        with self._ownership.claim(key, source):
            self._cancel_guarded(client_order_id=client_order_id, lease=lease, source=source)
            cancelled = self._fetch_record(client_order_id)
            if cancelled is None or cancelled.status != ExecutionOrderStatus.CANCELLED:
                status = cancelled.status.value if cancelled is not None else "MISSING"
                raise ReplaceNotSafeError(
                    f"Cannot replace {client_order_id!r}: cancellation did not reach a safe "
                    f"CANCELLED outcome (status={status}) -- a fill or an ambiguous cancel "
                    "outcome must be resolved before a replace can proceed"
                )
            return self._submit_guarded(
                environment=cancelled.environment, account_no=cancelled.account_no,
                symbol=cancelled.symbol, side=cancelled.side, intent=cancelled.intent,
                quantity=new_quantity, limit_price=new_limit_price, exchange=cancelled.exchange,
                execution_policy=cancelled.execution_policy, source=source, lease=lease,
                attempt_group_id=cancelled.attempt_group_id, attempt_number=cancelled.attempt_number + 1,
                replaces_execution_order_id=client_order_id,
            )

    # --- read-only passthroughs (never guarded -- not destructive) -------------

    def get_order(self, **kwargs: Any) -> List[BrokerOrderStatusSnapshot]:
        return self._real_broker.get_order(**kwargs)

    def discover_orders(self, **kwargs: Any) -> BrokerOrderDiscoveryResult:
        return self._real_broker.discover_orders(**kwargs)

    def get_positions(self, **kwargs: Any) -> Dict[str, Any]:
        return self._real_broker.get_positions(**kwargs)

    # --- internal: GUARDED_ENGINE sequences -------------------------------------

    def _require_engine(self) -> Engine:
        if self._engine is None:
            raise GuardedEngineRequiresDatabaseError(
                "GUARDED_ENGINE mode requires a database engine; none was configured on this gateway"
            )
        return self._engine

    def _fetch_record(self, client_order_id: str) -> Optional[ExecutionOrderRecord]:
        engine = self._require_engine()
        return fetch_execution_order(engine, client_order_id)

    def _submit_guarded(
        self,
        *,
        environment: str,
        account_no: str,
        symbol: str,
        side: OrderSide,
        intent: OrderIntent,
        quantity: int,
        limit_price: float,
        exchange: str,
        execution_policy: str,
        source: ExecutionSource,
        lease: Optional[ExecutionLease],
        attempt_group_id: str,
        attempt_number: int,
        replaces_execution_order_id: str = "",
    ) -> BrokerSubmissionResult:
        engine = self._require_engine()
        environment = str(environment or "").upper()
        account_no = str(account_no or "")
        symbol = str(symbol or "").upper()

        # 1. feature/environment/account/session inputs.
        trading_state.require_trading_enabled(environment, symbol)

        # 2. execution lease and lease epoch.
        self._lease_protocol.require_current(lease)

        # 3. command intent, quantity, price. (Capital/card-version gating
        # is the caller's own pre-trade-risk layer's responsibility -- see
        # the module docstring; re-implementing it here would risk
        # diverging from the one, already-reviewed risk system.)
        quantity = int(quantity)
        if quantity <= 0:
            raise ValueError(f"quantity must be positive, got {quantity}")
        if execution_policy == RESERVED_MOO_EXECUTION:
            limit_price = 0.0
        else:
            limit_price = float(limit_price)
            if not math.isfinite(limit_price) or limit_price <= 0:
                raise ValueError(f"limit_price must be positive and finite, got {limit_price}")

        # 4. deterministic idempotency key.
        client_order_id = generate_client_order_id(environment, account_no, symbol, side, intent)
        idempotency_key = client_order_id

        reservation = CapitalReservation.create(
            environment=environment, account_no=account_no, symbol=symbol,
            attempt_group_id=attempt_group_id or client_order_id,
            requested_notional=quantity * (limit_price or 0.0),
        )
        command = ExecutionCommand(
            idempotency_key=idempotency_key, command_type="submit", environment=environment,
            account_no=account_no, symbol=symbol, lease_epoch=lease.lease_epoch if lease else 0,
            owner_device_id=lease.device_id if lease else "", lease_token=lease.lease_token if lease else "",
            source=source.value,
        )
        record = ExecutionOrderRecord(
            environment=environment, account_no=account_no, symbol=symbol, side=side, intent=intent,
            client_order_id=client_order_id, attempt_group_id=attempt_group_id,
            attempt_number=attempt_number, submitted_quantity=quantity, submitted_limit_price=limit_price,
            exchange=exchange, execution_policy=execution_policy,
            owner_device_id=lease.device_id if lease else "", lease_token=lease.lease_token if lease else "",
            lease_epoch=lease.lease_epoch if lease else 0, capital_reservation_id=reservation.reservation_id,
            replaces_execution_order_id=replaces_execution_order_id,
        )

        # 5 + 6. one transaction: command + reservation + PREPARED record.
        ensure_execution_commands_table(engine)
        ensure_execution_orders_table(engine)
        ensure_capital_reservations_table(engine)
        with engine.begin() as conn:
            insert_command(conn, command)
            insert_reservation(conn, reservation)
            insert_execution_order(conn, record)

        # 7 + 8. separate durable transaction: PREPARED -> SUBMITTING.
        apply_status_transition(record, ExecutionOrderStatus.SUBMITTING)
        with engine.begin() as conn:
            update_execution_order(conn, record, expected_version=record.version)

        # 9. only now call the broker.
        try:
            submission = self._real_broker.submit_order(
                environment=environment, account_no=account_no, symbol=symbol, side=side,
                quantity=quantity, limit_price=limit_price, exchange=exchange,
                execution_policy=execution_policy,
            )
        except Exception as exc:
            try:
                ambiguous = self._real_broker.is_ambiguous_submission_error(exc)
            except Exception:
                ambiguous = True
            target_status = (
                ExecutionOrderStatus.UNKNOWN_SUBMISSION_STATE if ambiguous else ExecutionOrderStatus.REJECTED
            )
            apply_status_transition(record, target_status)
            with engine.begin() as conn:
                update_execution_order(conn, record, expected_version=record.version)
                update_command_response(
                    conn, idempotency_key, status="AMBIGUOUS" if ambiguous else "FAILED",
                    broker_response={"error": str(exc)},
                )
                if not ambiguous:
                    # 11: never automatically retry -- a clean rejection
                    # gives the reservation back; an ambiguous one does not
                    # (the order may yet turn out to exist).
                    reservation.release()
                    update_reservation(conn, reservation)
            if ambiguous:
                raise GuardedSubmissionAmbiguousError(str(exc)) from exc
            raise GuardedSubmissionRejectedError(str(exc)) from exc

        # 10. success -- ACKNOWLEDGED with exact identity.
        record.remaining_quantity = record.submitted_quantity
        apply_status_transition(
            record, ExecutionOrderStatus.ACKNOWLEDGED, broker_order_id=submission.broker_order_id
        )
        with engine.begin() as conn:
            update_execution_order(conn, record, expected_version=record.version)
            update_command_response(
                conn, idempotency_key, status="ACKNOWLEDGED", broker_response=submission.raw_response
            )
        return submission

    def _cancel_guarded(
        self, *, client_order_id: str, lease: Optional[ExecutionLease], source: ExecutionSource
    ) -> BrokerOrderStatusSnapshot:
        engine = self._require_engine()
        record = self._fetch_record(client_order_id)
        if record is None:
            raise OrderNotFoundForCancelError(f"No ExecutionOrderRecord for client_order_id={client_order_id!r}")

        self._lease_protocol.require_current(lease)
        if not is_cancellable(record):
            raise CancelNotPermittedError(
                f"client_order_id={client_order_id!r} is not currently cancellable "
                f"(status={record.status.value}, recovery_state={record.recovery_state.value}, "
                f"origin={record.origin.value})"
            )

        cancel_idempotency_key = f"{client_order_id}:CANCEL:{record.attempt_number}"
        command = ExecutionCommand(
            idempotency_key=cancel_idempotency_key, command_type="cancel", environment=record.environment,
            account_no=record.account_no, symbol=record.symbol,
            lease_epoch=lease.lease_epoch if lease else record.lease_epoch,
            owner_device_id=lease.device_id if lease else record.owner_device_id,
            lease_token=lease.lease_token if lease else record.lease_token,
            target_broker_order_id=record.broker_order_id, source=source.value,
        )

        ensure_execution_commands_table(engine)
        # Idempotency-key-is-new check: a second cancel attempt for the same
        # (client_order_id, attempt_number) raises DuplicateCommandError
        # from insert_command itself, propagated to the caller unchanged --
        # no separate check needed here.
        with engine.begin() as conn:
            insert_command(conn, command)
            apply_status_transition(record, ExecutionOrderStatus.CANCEL_PENDING)
            update_execution_order(conn, record, expected_version=record.version)

        quantity = record.remaining_quantity or record.submitted_quantity
        try:
            snapshot = self._real_broker.cancel_order(
                environment=record.environment, account_no=record.account_no,
                is_reserved=(record.execution_policy == RESERVED_MOO_EXECUTION),
                symbol=record.symbol, broker_order_id=record.broker_order_id, quantity=quantity,
                side=record.side.value, exchange=record.exchange or "NASD",
            )
        except Exception as exc:
            classify = getattr(self._real_broker, "is_ambiguous_cancellation_error", None)
            try:
                ambiguous = classify(exc) if callable(classify) else True
            except Exception:
                ambiguous = True
            with engine.begin() as conn:
                update_command_response(
                    conn, cancel_idempotency_key, status="AMBIGUOUS" if ambiguous else "FAILED",
                    broker_response={"error": str(exc)},
                )
                if ambiguous:
                    # Never blindly retried -- left CANCEL_PENDING with
                    # recovery_state=DISCOVERING for reconciliation (PR3).
                    _transition_recovery_state(record, OrderRecoveryState.DISCOVERING)
                else:
                    # An explicit cancel rejection: the order simply keeps
                    # working (revision 3.2's CANCEL_PENDING -> WORKING row).
                    apply_status_transition(record, ExecutionOrderStatus.WORKING)
                update_execution_order(conn, record, expected_version=record.version)
            if ambiguous:
                raise GuardedCancellationAmbiguousError(str(exc)) from exc
            raise GuardedCancellationRejectedError(str(exc)) from exc

        if snapshot.status == OrderStatus.CANCELLED:
            apply_status_transition(record, ExecutionOrderStatus.CANCELLED)
        elif snapshot.status == OrderStatus.FILLED:
            record.filled_quantity = snapshot.filled_quantity or record.submitted_quantity
            record.remaining_quantity = 0
            apply_status_transition(record, ExecutionOrderStatus.FILLED)
        elif snapshot.status == OrderStatus.PARTIALLY_FILLED:
            record.filled_quantity = snapshot.filled_quantity
            record.remaining_quantity = max(0, record.submitted_quantity - snapshot.filled_quantity)
            apply_status_transition(record, ExecutionOrderStatus.PARTIALLY_FILLED)
        else:
            # A broker answer that isn't one of the three expected outcomes
            # is itself ambiguous -- needs reconciliation, not a guess.
            _transition_recovery_state(record, OrderRecoveryState.DISCOVERING)
        with engine.begin() as conn:
            update_execution_order(conn, record, expected_version=record.version)
            update_command_response(
                conn, cancel_idempotency_key, status="ACKNOWLEDGED", broker_response=snapshot.raw_response
            )
        return snapshot


# --- process-wide default instance ----------------------------------------

_default_gateway_lock = threading.Lock()
_default_gateway: Optional[ExecutionCommandGateway] = None


def get_default_execution_gateway() -> ExecutionCommandGateway:
    """The gateway every existing legacy call site is migrated to use as
    its default ``broker=`` (Workstream 9) -- wraps the real
    :class:`~src.services.broker.KisBroker` with no database engine
    configured, since production never selects ``GUARDED_ENGINE`` mode
    (``BUYBOARD_ENGINE_ENABLED`` stays ``false``) and therefore never
    needs one. A caller that *does* want ``GUARDED_ENGINE`` mode (tests)
    must construct its own :class:`ExecutionCommandGateway` with an engine
    supplied -- this singleton deliberately cannot do that, so a
    misconfigured production path fails closed
    (:class:`GuardedEngineRequiresDatabaseError`) rather than silently
    running the new engine against no database.
    """
    global _default_gateway
    if _default_gateway is None:
        with _default_gateway_lock:
            if _default_gateway is None:
                _default_gateway = ExecutionCommandGateway(real_broker=KisBroker())
    return _default_gateway
