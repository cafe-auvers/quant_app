"""``ExecutionCommandGateway`` -- the single application component permitted
to invoke destructive broker operations (Workstream 3, B1).

``docs/kanban_production_readiness.md``, PR2 (Workstreams 3 + 9), second
review pass. Two genuinely different call shapes for the two modes
(:mod:`src.core.execution_mode`), not one shape trying to serve both
(review finding 2 -- "Do not force the execution gateway to masquerade as
the old minimal Broker protocol"):

- ``LEGACY_COMPATIBILITY`` (``BUYBOARD_ENGINE_ENABLED=false``, every
  production request today and for the duration of this whole program):
  ``submit_order``/``cancel_order`` conform exactly to
  :class:`~src.brokers.execution_broker_protocol.ExecutionBrokerProtocol`
  (the existing :class:`~src.services.broker.Broker` protocol), so this
  gateway is a drop-in ``broker=`` for
  :func:`~src.services.order_execution_service.submit_guarded_overseas_order`
  and :func:`~src.services.order_reconciliation.cancel_and_reconcile_order`.
  A transparent pass-through to the real broker -- no command journal, no
  capital reservation, no ``ExecutionOrderRecord``.
- ``GUARDED_ENGINE`` (``BUYBOARD_ENGINE_ENABLED=true`` -- implemented and
  tested here, never selected in production by this PR): ``submit_guarded``/
  ``cancel_guarded``/``replace_guarded`` take explicit request models
  (:mod:`src.core.execution_request`) carrying a **caller-generated,
  stable command identity** -- never minted inside this gateway (that was
  finding 1's bug: a fresh UUID on every call made "restart-safe
  idempotency" impossible even in principle). The full A1-A11 submit
  sequence and B1-B4 cancel/replace sequence run only in this mode.

Calling ``submit_order``/``cancel_order`` while ``GUARDED_ENGINE`` is
active, or ``submit_guarded``/``cancel_guarded``/``replace_guarded`` while
``LEGACY_COMPATIBILITY`` is active, raises immediately -- the two APIs are
not interchangeable fallbacks for each other.

B2's gate sequence in ``GUARDED_ENGINE`` mode, in order: kill switch
(``trading_state``), persisted execution ownership (H1,
:mod:`src.core.execution_ownership` -- Workstream 9's actual signed scope,
not the lighter in-process mutual-exclusion claim alone), a verified
execution lease with a proven epoch (finding 4 -- ``lease=None`` or an
unverifiable epoch both reject, never fail open), mutation budget
(:mod:`src.services.mutation_budget_protocol` -- a real seam for
Workstream 10, required to be explicitly injected), quantity/price
validity, then the atomic command+reservation+``PREPARED`` record
transaction.
"""
from __future__ import annotations

import logging
import math
import threading
from contextlib import contextmanager
from typing import Any, Callable, Dict, List, Optional, Tuple

from sqlalchemy.engine import Engine

from src.brokers.execution_broker_protocol import Broker, BrokerSubmissionResult, KisBroker
from src.core.capital_reservation import CapitalReservation
from src.core.execution_mode import ExecutionLease, ExecutionMode, ExecutionSource, resolve_execution_mode
from src.core.execution_order_record import (
    AdoptedOrderPermission,
    BrokerIdentityStatus,
    ExecutionOrderRecord,
    ExecutionOrderStatus,
    OrderOrigin,
    allowed_status_transitions,
    apply_status_transition,
    is_cancellable,
    validate_consistency,
)
from src.core.execution_ownership import ExecutionOwner, owner_for_source
from src.core.execution_request import CancelExecutionRequest, ReplaceExecutionRequest, SubmitExecutionRequest
from src.core.order_recovery_state import OrderRecoveryState, validate_recovery_transition
from src.core.order_state import (
    REGULAR_LIMIT_EXECUTION,
    RESERVED_MOO_EXECUTION,
    BrokerOrderDiscoveryResult,
    BrokerOrderStatusSnapshot,
    OrderSide,
    OrderStatus,
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
    ExecutionLeaseProtocol,
    LeaseNotCurrentError,
)
from src.services.execution_order_repository import (
    ensure_execution_orders_table,
    fetch_execution_order,
    insert_execution_order,
    update_execution_order,
)
from src.services.execution_ownership_repository import ensure_execution_ownership_table, get_ownership
from src.services.mutation_budget_protocol import CommandType, MutationBudgetProtocol

logger = logging.getLogger(__name__)


# --- exceptions ---------------------------------------------------------


class GuardedExecutionError(RuntimeError):
    """Base for every ``GUARDED_ENGINE``-mode gateway rejection. Never
    raised in ``LEGACY_COMPATIBILITY`` mode -- that mode's exceptions are
    whatever the real broker itself raises, unchanged."""


class WrongGatewayModeError(GuardedExecutionError):
    """``submit_order``/``cancel_order`` (the Broker-protocol,
    ``LEGACY_COMPATIBILITY``-only methods) were called while
    ``GUARDED_ENGINE`` is active, or ``submit_guarded``/``cancel_guarded``/
    ``replace_guarded`` were called while ``LEGACY_COMPATIBILITY`` is
    active. The two APIs are never interchangeable (see the module
    docstring)."""


class ConcurrentExecutionOwnershipError(GuardedExecutionError):
    """Two destructive commands tried to be in flight for the same
    ``(environment, account_no, symbol)`` in this process at once
    (Workstream 9's in-process mutual exclusion). Raised regardless of
    :class:`~src.core.execution_mode.ExecutionMode`."""


class ExecutionOwnershipMismatchError(GuardedExecutionError):
    """H1/B2: the persisted ``execution_owner`` for this
    ``(environment, account_no, symbol)`` does not authorize the calling
    :class:`~src.core.execution_mode.ExecutionSource`. ``MANUAL``-owned
    symbols reject every application source; ``KANBAN``-owned symbols
    accept only ``KANBAN_BOARD``; ``LEGACY``-owned (the default) accepts
    ``LEGACY_BUY_DASHBOARD`` and unattributed ``SYSTEM`` callers, never
    ``KANBAN_BOARD``."""


class GuardedSubmissionRejectedError(GuardedExecutionError):
    """The broker explicitly rejected the submission (a clean, pre-
    acceptance rejection) -- never treated as a reason to retry."""


class GuardedSubmissionAmbiguousError(GuardedExecutionError):
    """Timeout, transport loss, or any other outcome the broker adapter
    cannot distinguish from "maybe accepted" -- INV-23: never retried
    automatically. The order is left ``UNKNOWN_SUBMISSION_STATE`` for
    reconciliation (PR3)."""


class AmbiguousPostBrokerPersistenceError(GuardedExecutionError):
    """The broker call itself completed (accepted a submission, or
    answered a cancel) but persisting that outcome afterward failed. The
    durable record was **not** confirmed updated -- it remains at
    whatever status it held before this write (``SUBMITTING`` for a
    submission, ``CANCEL_PENDING`` for a cancellation). A caller must
    never reinterpret this as ``REJECTED``/``FAILED`` and must never
    resubmit or re-cancel with a new command identity -- the already-
    recorded stable idempotency key means a later retry with the *same*
    identity is safely rejected as a duplicate once the database is
    reachable again; reconciliation (PR3) is what actually resolves this
    from broker truth, not a caller-level guess.
    """

    def __init__(self, message: str, *, broker_order_id: str = "", raw_response: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.broker_order_id = broker_order_id
        self.raw_response = raw_response or {}


class CancelNotPermittedError(GuardedExecutionError):
    """The cancel-eligibility check (identity/permission/recovery-state/
    status, or an account/environment mismatch against the requested
    order) failed -- no broker call was attempted."""


class GuardedCancellationRejectedError(GuardedExecutionError):
    """The broker explicitly refused the cancel request itself (the order
    had already progressed past the point a cancel could apply)."""


class GuardedCancellationAmbiguousError(GuardedExecutionError):
    """Timeout or transport loss on a cancel request -- per the frozen
    contract, "must not be blindly retried. It remains reconciliation work
    for PR3." Left ``CANCEL_PENDING`` with ``recovery_state=DISCOVERING``."""


class ReplaceNotSafeError(GuardedExecutionError):
    """``replace_guarded``'s cancel-then-resubmit could not establish a
    safe (``CANCELLED``) outcome for the order being replaced -- the new
    order is never submitted in this case."""


class OrderNotFoundForCancelError(GuardedExecutionError):
    pass


class GuardedEngineRequiresDatabaseError(GuardedExecutionError):
    """``GUARDED_ENGINE`` mode was selected but no database ``Engine`` was
    configured on this gateway instance."""


class GuardedEngineRequiresMutationBudgetError(GuardedExecutionError):
    """``GUARDED_ENGINE`` mode was selected but no
    :class:`~src.services.mutation_budget_protocol.MutationBudgetProtocol`
    was configured. Fails closed rather than silently allowing every
    mutation through an implicit "always available" default -- see that
    module's own docstring."""


class LeaseNotVerifiedError(GuardedExecutionError):
    """``GUARDED_ENGINE`` mode requires a lease that is both present and
    whose epoch the configured
    :class:`~src.services.execution_lease_protocol.ExecutionLeaseProtocol`
    can actually prove -- unlike
    :class:`~src.services.execution_lease_protocol.ExecutionLeaseProtocol`'s
    own "``lease=None`` means unfenced" convention (which exists for
    other, non-destructive-mutation callers), this gateway never treats a
    missing or unverifiable lease as "no fencing requested" in this mode.
    """


# --- mutual exclusion (Workstream 9's in-process component of B2) ----------


class _OwnershipRegistry:
    """In-process, per-``(environment, account_no, symbol)`` mutual
    exclusion: only **one** destructive command may be in flight for a
    given key at a time. This is *not* H1 -- H1 is the persisted,
    cross-restart ``execution_owner`` assignment enforced separately via
    :func:`~src.services.execution_ownership_repository.get_ownership`;
    this registry only prevents two calls from racing the same account+
    symbol through this *process* at the same instant, regardless of
    :class:`~src.core.execution_mode.ExecutionMode` or which
    :class:`~src.core.execution_mode.ExecutionSource` is calling.

    Strict exclusion, not source-based reentrancy: nothing in this gateway
    nests a public claim inside another (``replace_guarded`` calls its
    internal ``_do_cancel``/``_do_submit`` directly, never through the
    public claiming methods), so a same-source reentrancy allowance was
    both unnecessary and, in an earlier version of this class, a genuine
    thread-safety bug (it tracked only the source *value*, not which
    specific caller was holding it).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._holders: Dict[Tuple[str, str, str], ExecutionSource] = {}

    @contextmanager
    def claim(self, key: Tuple[str, str, str], source: ExecutionSource):
        with self._lock:
            holder = self._holders.get(key)
            if holder is not None:
                raise ConcurrentExecutionOwnershipError(
                    f"{key} already has an in-flight destructive command from "
                    f"{holder.value}; refusing a concurrent one from {source.value}"
                )
            self._holders[key] = source
        try:
            yield
        finally:
            with self._lock:
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


def _cancellable_for_replace(record: ExecutionOrderRecord) -> bool:
    """Mirrors :func:`~src.core.execution_order_record.is_cancellable`'s
    identity/recovery-state/status checks exactly, but a ``USER_ADOPTED``
    record is authorized by ``AdoptedOrderPermission.REPLACE`` here, not
    ``CANCEL`` (review finding 7): the internal cancel step of a composed
    replace is authorized by the replace decision itself, never by a
    separate, possibly-absent cancel grant. ``is_cancellable`` itself
    (part of PR1's frozen contract) is not modified -- this is a distinct,
    parallel predicate used only by ``replace_guarded``.
    """
    if record.origin not in (OrderOrigin.APPLICATION, OrderOrigin.USER_ADOPTED):
        return False
    if record.origin == OrderOrigin.USER_ADOPTED and AdoptedOrderPermission.REPLACE not in record.adoption_permissions:
        return False
    return (
        record.broker_identity_status == BrokerIdentityStatus.EXACT
        and bool(record.broker_order_id)
        and record.recovery_state in (OrderRecoveryState.NONE, OrderRecoveryState.CANCEL_REQUIRED)
        and ExecutionOrderStatus.CANCEL_PENDING in allowed_status_transitions(record.status)
    )


# --- the gateway ----------------------------------------------------------


class ExecutionCommandGateway:
    """``LEGACY_COMPATIBILITY``: conforms to
    :class:`~src.brokers.execution_broker_protocol.ExecutionBrokerProtocol`
    via ``submit_order``/``cancel_order``. ``GUARDED_ENGINE``: use
    ``submit_guarded``/``cancel_guarded``/``replace_guarded`` instead --
    see the module docstring."""

    def __init__(
        self,
        *,
        real_broker: Optional[Broker] = None,
        engine: Optional[Engine] = None,
        lease_protocol: Optional[ExecutionLeaseProtocol] = None,
        mutation_budget: Optional[MutationBudgetProtocol] = None,
        mode_override: Optional[bool] = None,
        ownership_registry: Optional[_OwnershipRegistry] = None,
    ) -> None:
        self._real_broker: Broker = real_broker if real_broker is not None else KisBroker()
        self._engine = engine
        self._lease_protocol: ExecutionLeaseProtocol = lease_protocol or DefaultExecutionLeaseProtocol(
            engine=engine
        )
        self._mutation_budget = mutation_budget
        self._mode_override = mode_override
        self._ownership = ownership_registry or _OwnershipRegistry()

    @property
    def mode(self) -> ExecutionMode:
        return resolve_execution_mode(self._mode_override)

    # --- ExecutionBrokerProtocol: LEGACY_COMPATIBILITY only ---------------------

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
    ) -> BrokerSubmissionResult:
        source = _as_source(source)
        key = _recovery_key(environment, account_no, symbol)
        with self._ownership.claim(key, source):
            if self.mode != ExecutionMode.LEGACY_COMPATIBILITY:
                raise WrongGatewayModeError(
                    "submit_order() is the LEGACY_COMPATIBILITY Broker-protocol method; "
                    "GUARDED_ENGINE mode requires submit_guarded(request=SubmitExecutionRequest(...)) "
                    "-- see the module docstring"
                )
            return self._real_broker.submit_order(
                environment=environment, account_no=account_no, symbol=symbol, side=side,
                quantity=quantity, limit_price=limit_price, exchange=exchange,
                execution_policy=execution_policy,
            )

    def is_ambiguous_submission_error(self, error: BaseException) -> bool:
        if isinstance(error, GuardedSubmissionAmbiguousError):
            return True
        if isinstance(error, GuardedExecutionError):
            return False
        try:
            return self._real_broker.is_ambiguous_submission_error(error)
        except Exception:
            return True

    def cancel_order(
        self,
        *,
        environment: str,
        account_no: str,
        is_reserved: bool = False,
        source: Any = ExecutionSource.SYSTEM,
        **kwargs: Any,
    ) -> BrokerOrderStatusSnapshot:
        source = _as_source(source)
        symbol = str(kwargs.get("symbol") or "").upper()
        key = _recovery_key(environment, account_no, symbol)
        with self._ownership.claim(key, source):
            if self.mode != ExecutionMode.LEGACY_COMPATIBILITY:
                raise WrongGatewayModeError(
                    "cancel_order() is the LEGACY_COMPATIBILITY Broker-protocol method; "
                    "GUARDED_ENGINE mode requires cancel_guarded(request=CancelExecutionRequest(...)) "
                    "-- see the module docstring"
                )
            return self._real_broker.cancel_order(
                environment=environment, account_no=account_no, is_reserved=is_reserved, **kwargs
            )

    # --- read-only passthroughs (never guarded -- not destructive) -------------

    def get_order(self, **kwargs: Any) -> List[BrokerOrderStatusSnapshot]:
        return self._real_broker.get_order(**kwargs)

    def discover_orders(self, **kwargs: Any) -> BrokerOrderDiscoveryResult:
        return self._real_broker.discover_orders(**kwargs)

    def get_positions(self, **kwargs: Any) -> Dict[str, Any]:
        return self._real_broker.get_positions(**kwargs)

    # --- GUARDED_ENGINE only ----------------------------------------------------

    def submit_guarded(self, request: SubmitExecutionRequest) -> ExecutionOrderRecord:
        self._require_guarded_mode()
        key = _recovery_key(request.environment, request.account_no, request.symbol)
        with self._ownership.claim(key, request.source):
            return self._do_submit(request)

    def cancel_guarded(self, request: CancelExecutionRequest) -> ExecutionOrderRecord:
        self._require_guarded_mode()
        record = self._fetch_record(request.client_order_id)
        if record is None:
            raise OrderNotFoundForCancelError(
                f"No ExecutionOrderRecord for client_order_id={request.client_order_id!r}"
            )
        key = _recovery_key(record.environment, record.account_no, record.symbol)
        with self._ownership.claim(key, request.source):
            return self._do_cancel(request, record=record, permission_check=is_cancellable)

    def replace_guarded(self, request: ReplaceExecutionRequest) -> ExecutionOrderRecord:
        """Composed, never a synthetic first-class broker status: cancel
        the existing exact order (authorized by ``REPLACE``, not
        ``CANCEL`` -- finding 7), confirm a safe (``CANCELLED``) outcome,
        then submit a brand-new order linked via
        ``replaces_execution_order_id``. The original record is never
        mutated into the replacement.
        """
        self._require_guarded_mode()
        original = self._fetch_record(request.client_order_id)
        if original is None:
            raise OrderNotFoundForCancelError(f"No ExecutionOrderRecord for client_order_id={request.client_order_id!r}")
        key = _recovery_key(original.environment, original.account_no, original.symbol)
        with self._ownership.claim(key, request.source):
            cancel_request = CancelExecutionRequest(
                client_order_id=request.client_order_id,
                cancel_command_id=f"{request.replace_command_id}:CANCEL",
                environment=request.environment, account_no=request.account_no,
                lease=request.lease, source=request.source,
            )
            self._do_cancel(cancel_request, record=original, permission_check=_cancellable_for_replace)
            cancelled = self._fetch_record(request.client_order_id)
            if cancelled is None or cancelled.status != ExecutionOrderStatus.CANCELLED:
                status = cancelled.status.value if cancelled is not None else "MISSING"
                raise ReplaceNotSafeError(
                    f"Cannot replace {request.client_order_id!r}: cancellation did not reach a safe "
                    f"CANCELLED outcome (status={status}) -- a fill or an ambiguous cancel outcome must "
                    "be resolved before a replace can proceed"
                )
            submit_request = SubmitExecutionRequest(
                client_order_id=request.new_client_order_id, environment=cancelled.environment,
                account_no=cancelled.account_no, symbol=cancelled.symbol, side=cancelled.side,
                intent=cancelled.intent, quantity=request.new_quantity, limit_price=request.new_limit_price,
                exchange=cancelled.exchange, execution_policy=cancelled.execution_policy,
                attempt_group_id=cancelled.attempt_group_id, attempt_number=cancelled.attempt_number + 1,
                lease=request.lease, source=request.source,
                replaces_execution_order_id=request.client_order_id,
            )
            return self._do_submit(submit_request)

    # --- internal guards ---------------------------------------------------

    def _require_guarded_mode(self) -> None:
        if self.mode != ExecutionMode.GUARDED_ENGINE:
            raise WrongGatewayModeError(
                "This method requires GUARDED_ENGINE mode; use submit_order()/cancel_order() "
                "(the Broker-protocol methods) in LEGACY_COMPATIBILITY mode instead"
            )

    def _require_engine(self) -> Engine:
        if self._engine is None:
            raise GuardedEngineRequiresDatabaseError(
                "GUARDED_ENGINE mode requires a database engine; none was configured on this gateway"
            )
        return self._engine

    def _require_mutation_budget(self) -> MutationBudgetProtocol:
        if self._mutation_budget is None:
            raise GuardedEngineRequiresMutationBudgetError(
                "GUARDED_ENGINE mode requires a MutationBudgetProtocol; none was configured on this "
                "gateway -- see src.services.mutation_budget_protocol"
            )
        return self._mutation_budget

    def _require_verified_lease(self, lease: Optional[ExecutionLease]) -> None:
        if lease is None:
            raise LeaseNotVerifiedError(
                "GUARDED_ENGINE mode requires an explicit execution lease; none was supplied"
            )
        try:
            self._lease_protocol.require_current(lease)
        except LeaseNotCurrentError as exc:
            raise LeaseNotVerifiedError(str(exc)) from exc
        if not getattr(self._lease_protocol, "epoch_verified", False):
            raise LeaseNotVerifiedError(
                "The configured lease protocol cannot verify the lease epoch against an "
                "authoritative source -- refusing to proceed in GUARDED_ENGINE mode "
                "(see DefaultExecutionLeaseProtocol.epoch_verified)"
            )

    def _require_ownership(self, environment: str, account_no: str, symbol: str, source: ExecutionSource) -> None:
        engine = self._require_engine()
        ensure_execution_ownership_table(engine)
        ownership = get_ownership(engine, environment=environment, account_no=account_no, symbol=symbol)
        if ownership.owner == ExecutionOwner.MANUAL:
            raise ExecutionOwnershipMismatchError(
                f"{environment}/{account_no}/{symbol} is MANUAL-owned; no application source may "
                "submit or cancel destructive commands for it"
            )
        if ownership.owner == ExecutionOwner.KANBAN:
            if source != ExecutionSource.KANBAN_BOARD:
                raise ExecutionOwnershipMismatchError(
                    f"{environment}/{account_no}/{symbol} is KANBAN-owned; {source.value} is not authorized"
                )
            return
        # LEGACY (the H2 default, or an explicit assignment)
        if source == ExecutionSource.KANBAN_BOARD:
            raise ExecutionOwnershipMismatchError(
                f"{environment}/{account_no}/{symbol} is LEGACY-owned; KANBAN_BOARD is not authorized"
            )

    def _fetch_record(self, client_order_id: str) -> Optional[ExecutionOrderRecord]:
        engine = self._require_engine()
        return fetch_execution_order(engine, client_order_id)

    @staticmethod
    def _persist_or_raise_ambiguous(
        fn: Callable[[], None], *, context: str, broker_order_id: str = "",
        raw_response: Optional[Dict[str, Any]] = None,
    ) -> None:
        try:
            fn()
        except AmbiguousPostBrokerPersistenceError:
            raise
        except Exception as exc:
            raise AmbiguousPostBrokerPersistenceError(
                f"{context}: the broker call already completed, but persisting that outcome failed: "
                f"{exc}. The durable record was not confirmed updated -- treat this as unresolved, "
                "never as rejected, and do not resubmit/re-cancel.",
                broker_order_id=broker_order_id, raw_response=raw_response,
            ) from exc

    # --- internal: GUARDED_ENGINE sequences -------------------------------------

    def _do_submit(self, request: SubmitExecutionRequest) -> ExecutionOrderRecord:
        engine = self._require_engine()
        mutation_budget = self._require_mutation_budget()
        environment = request.environment
        account_no = request.account_no
        symbol = request.symbol

        # 1. feature/environment/account/session inputs.
        trading_state.require_trading_enabled(environment, symbol)

        # H1/B2: persisted execution ownership.
        self._require_ownership(environment, account_no, symbol, request.source)

        # 2. execution lease and lease epoch -- required, never fails open.
        self._require_verified_lease(request.lease)

        # B2: mutation budget (Workstream 10 seam).
        mutation_budget.require_available(CommandType.SUBMIT)

        # 3. command intent, quantity, price.
        quantity = int(request.quantity)
        if quantity <= 0:
            raise ValueError(f"quantity must be positive, got {quantity}")
        if request.execution_policy == RESERVED_MOO_EXECUTION:
            limit_price = 0.0
        else:
            limit_price = float(request.limit_price)
            if not math.isfinite(limit_price) or limit_price <= 0:
                raise ValueError(f"limit_price must be positive and finite, got {limit_price}")

        # 4. caller-stable idempotency key -- never generated in here.
        client_order_id = request.client_order_id
        if not client_order_id:
            raise ValueError(
                "SubmitExecutionRequest.client_order_id must be a non-blank, caller-generated "
                "stable identity"
            )
        idempotency_key = f"SUBMIT:{client_order_id}"

        # finding 11: a SELL exit does not reserve buying-power notional --
        # only a BUY reduces capital available for new entries. Still
        # creates a (zero-notional) reservation row so the record's
        # capital_reservation_id/audit trail stays uniform across sides.
        requested_notional = quantity * limit_price if request.side == OrderSide.BUY else 0.0

        reservation = CapitalReservation.create(
            environment=environment, account_no=account_no, symbol=symbol,
            attempt_group_id=request.attempt_group_id or client_order_id,
            requested_notional=requested_notional,
        )
        command = ExecutionCommand(
            idempotency_key=idempotency_key, command_type="submit", environment=environment,
            account_no=account_no, symbol=symbol,
            lease_epoch=request.lease.lease_epoch if request.lease else 0,
            owner_device_id=request.lease.device_id if request.lease else "",
            lease_token=request.lease.lease_token if request.lease else "",
            source=request.source.value,
        )
        record = ExecutionOrderRecord(
            environment=environment, account_no=account_no, symbol=symbol, side=request.side,
            intent=request.intent, client_order_id=client_order_id,
            attempt_group_id=request.attempt_group_id, attempt_number=request.attempt_number,
            submitted_quantity=quantity, submitted_limit_price=limit_price, exchange=request.exchange,
            execution_policy=request.execution_policy,
            owner_device_id=request.lease.device_id if request.lease else "",
            lease_token=request.lease.lease_token if request.lease else "",
            lease_epoch=request.lease.lease_epoch if request.lease else 0,
            capital_reservation_id=reservation.reservation_id,
            replaces_execution_order_id=request.replaces_execution_order_id,
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
                environment=environment, account_no=account_no, symbol=symbol, side=request.side,
                quantity=quantity, limit_price=limit_price, exchange=request.exchange,
                execution_policy=request.execution_policy,
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

            def _persist_failure() -> None:
                with engine.begin() as conn:
                    update_execution_order(conn, record, expected_version=record.version)
                    update_command_response(
                        conn, idempotency_key, status="AMBIGUOUS" if ambiguous else "FAILED",
                        broker_response={"error": str(exc)},
                    )
                    if not ambiguous:
                        # 11: never automatically retry -- a clean rejection
                        # gives the reservation back; an ambiguous one does
                        # not (the order may yet turn out to exist).
                        reservation.release()
                        update_reservation(conn, reservation)

            self._persist_or_raise_ambiguous(
                _persist_failure, context=f"submit {client_order_id!r} failure persistence"
            )
            if ambiguous:
                raise GuardedSubmissionAmbiguousError(str(exc)) from exc
            raise GuardedSubmissionRejectedError(str(exc)) from exc

        # 10. success -- ACKNOWLEDGED with exact identity.
        record.remaining_quantity = record.submitted_quantity
        apply_status_transition(
            record, ExecutionOrderStatus.ACKNOWLEDGED, broker_order_id=submission.broker_order_id
        )

        def _persist_success() -> None:
            with engine.begin() as conn:
                update_execution_order(conn, record, expected_version=record.version)
                update_command_response(
                    conn, idempotency_key, status="ACKNOWLEDGED", broker_response=submission.raw_response
                )

        self._persist_or_raise_ambiguous(
            _persist_success, context=f"submit {client_order_id!r} success persistence",
            broker_order_id=submission.broker_order_id, raw_response=submission.raw_response,
        )
        return record

    def _do_cancel(
        self, request: CancelExecutionRequest, *, record: ExecutionOrderRecord,
        permission_check: Callable[[ExecutionOrderRecord], bool],
    ) -> ExecutionOrderRecord:
        engine = self._require_engine()
        mutation_budget = self._require_mutation_budget()

        if record.environment != request.environment or record.account_no != request.account_no:
            raise CancelNotPermittedError(
                f"client_order_id={request.client_order_id!r} belongs to "
                f"{record.environment}/{record.account_no}, not the requested "
                f"{request.environment}/{request.account_no}"
            )

        self._require_ownership(record.environment, record.account_no, record.symbol, request.source)
        self._require_verified_lease(request.lease)
        mutation_budget.require_available(CommandType.CANCEL)

        if not permission_check(record):
            raise CancelNotPermittedError(
                f"client_order_id={request.client_order_id!r} is not currently cancellable "
                f"(status={record.status.value}, recovery_state={record.recovery_state.value}, "
                f"origin={record.origin.value})"
            )

        cancel_idempotency_key = f"CANCEL:{request.cancel_command_id}"
        command = ExecutionCommand(
            idempotency_key=cancel_idempotency_key, command_type="cancel", environment=record.environment,
            account_no=record.account_no, symbol=record.symbol,
            lease_epoch=request.lease.lease_epoch if request.lease else record.lease_epoch,
            owner_device_id=request.lease.device_id if request.lease else record.owner_device_id,
            lease_token=request.lease.lease_token if request.lease else record.lease_token,
            target_broker_order_id=record.broker_order_id, source=request.source.value,
        )

        ensure_execution_commands_table(engine)
        # Idempotency: insert_command raises DuplicateCommandError on a
        # replayed cancel_command_id, propagated unchanged -- a genuinely
        # new cancel decision must use a new cancel_command_id (finding 8),
        # which is the caller's (ExecutionWorkflowService's) job, not this
        # method's.
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

            def _persist_cancel_failure() -> None:
                with engine.begin() as conn:
                    update_command_response(
                        conn, cancel_idempotency_key, status="AMBIGUOUS" if ambiguous else "FAILED",
                        broker_response={"error": str(exc)},
                    )
                    if ambiguous:
                        _transition_recovery_state(record, OrderRecoveryState.DISCOVERING)
                    else:
                        apply_status_transition(record, ExecutionOrderStatus.WORKING)
                    update_execution_order(conn, record, expected_version=record.version)

            self._persist_or_raise_ambiguous(
                _persist_cancel_failure, context=f"cancel {request.client_order_id!r} failure persistence"
            )
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
            _transition_recovery_state(record, OrderRecoveryState.DISCOVERING)

        def _persist_cancel_success() -> None:
            with engine.begin() as conn:
                update_execution_order(conn, record, expected_version=record.version)
                update_command_response(
                    conn, cancel_idempotency_key, status="ACKNOWLEDGED", broker_response=snapshot.raw_response
                )

        self._persist_or_raise_ambiguous(
            _persist_cancel_success, context=f"cancel {request.client_order_id!r} success persistence",
            broker_order_id=record.broker_order_id, raw_response=snapshot.raw_response,
        )
        return record


# --- process-wide default instance (LEGACY_COMPATIBILITY only) --------------

_default_gateway_lock = threading.Lock()
_default_gateway: Optional[ExecutionCommandGateway] = None


def get_default_execution_gateway() -> ExecutionCommandGateway:
    """The gateway every existing legacy call site is migrated to use as
    its default ``broker=`` (Workstream 9) -- wraps the real
    :class:`~src.services.broker.KisBroker` with no database engine,
    lease protocol, or mutation budget configured, since production never
    selects ``GUARDED_ENGINE`` mode (``BUYBOARD_ENGINE_ENABLED`` stays
    ``false``) and this singleton is never used for it. See
    :mod:`src.services.buyboard_runtime`'s guarded composition root
    (finding 10) for the ``GUARDED_ENGINE``-capable gateway construction.
    """
    global _default_gateway
    if _default_gateway is None:
        with _default_gateway_lock:
            if _default_gateway is None:
                _default_gateway = ExecutionCommandGateway(real_broker=KisBroker())
    return _default_gateway


def build_guarded_execution_gateway(
    *,
    engine: Engine,
    lease_protocol: ExecutionLeaseProtocol,
    mutation_budget: MutationBudgetProtocol,
    real_broker: Optional[Broker] = None,
) -> ExecutionCommandGateway:
    """The explicit ``GUARDED_ENGINE``-capable composition root (finding
    10): every dependency ``GUARDED_ENGINE`` mode actually needs is
    required as a keyword argument here, so a caller assembling this at
    startup gets a clear, immediate ``TypeError`` for a missing one rather
    than the gateway silently defaulting to something unsafe and failing
    only much later, at the first real submission.
    """
    if engine is None:
        raise GuardedEngineRequiresDatabaseError("build_guarded_execution_gateway requires engine")
    if lease_protocol is None:
        raise LeaseNotVerifiedError("build_guarded_execution_gateway requires lease_protocol")
    if mutation_budget is None:
        raise GuardedEngineRequiresMutationBudgetError("build_guarded_execution_gateway requires mutation_budget")
    return ExecutionCommandGateway(
        real_broker=real_broker if real_broker is not None else KisBroker(),
        engine=engine, lease_protocol=lease_protocol, mutation_budget=mutation_budget,
    )
