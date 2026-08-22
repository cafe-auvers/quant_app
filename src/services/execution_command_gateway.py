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
  No command journal, capital reservation, or ``ExecutionOrderRecord`` is
  introduced; when a shared DB is supplied, the gateway still enforces H1
  ownership immediately before delegating to the real broker.
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
import weakref
from contextlib import contextmanager
from typing import Any, Callable, Dict, List, Optional, Tuple

from sqlalchemy.engine import Engine

from src.brokers.execution_broker_protocol import Broker, BrokerSubmissionResult, KisBroker
from src.core import execution_config
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
from src.core.execution_ownership import ExecutionOwner, ExecutionOwnershipProof
from src.core.execution_request import CancelExecutionRequest, ReplaceExecutionRequest, SubmitExecutionRequest
from src.core.order_recovery_state import OrderRecoveryState, validate_recovery_transition
from src.core.order_state import (
    REGULAR_LIMIT_EXECUTION,
    RESERVED_MOO_EXECUTION,
    BrokerOrderDiscoveryResult,
    BrokerOrderStatusSnapshot,
    OrderIntent,
    OrderSide,
    OrderStatus,
)
from src.infrastructure.database.coordination_engine import coordination_read_connection
from src.services import trading_state
from src.services.capital_reservation_repository import (
    ensure_capital_reservations_table,
    fetch_reservation,
    insert_reservation_if_available,
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
from src.services.discovered_external_order_repository import (
    ActiveExternalOrderFenceError,
    ensure_discovered_external_orders_table,
    require_no_active_unowned_external_order,
)
from src.services.mutation_budget_protocol import CommandType, MutationBudgetProtocol
from src.risk.pre_trade import (
    PreTradeRiskRejectedError,
    require_pre_trade_risk_approval,
)
from src.services.kis_request_boundary import (
    install_process_kis_request_scheduler,
    kis_request_scope,
)
from src.services.kis_request_scheduler import RequestKind, RequestPriority
from src.services.emergency_journal import (
    EmergencyJournal,
    EmergencyJournalError,
    EmergencyLeaseAllowance,
    EmergencyLeaseAllowanceError,
)

logger = logging.getLogger(__name__)


# --- exceptions ---------------------------------------------------------


class GuardedExecutionError(RuntimeError):
    """Base for every ``GUARDED_ENGINE``-mode gateway rejection. Never
    raised in ``LEGACY_COMPATIBILITY`` mode -- that mode's exceptions are
    whatever the real broker itself raises, unchanged."""


class CanonicalDatabaseUnavailableError(GuardedExecutionError):
    """Canonical persistence is unavailable and the request must fail closed."""


class EmergencyActionNotPermittedError(GuardedExecutionError):
    """A database-outage request is not one of the narrowly allowed exits."""


class EmergencyJournalUnavailableError(GuardedExecutionError):
    """Mandatory local persistence failed before an emergency broker call."""


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


class GuardedSubmissionPreBrokerAbortedError(GuardedSubmissionRejectedError):
    """A journaled submission was definitively aborted by a final mutable
    gate before the broker was called. The caller may start a fresh attempt
    after the gate is resolved; the retired command identity is not reused."""


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


class GuardedCancellationPreBrokerAbortedError(GuardedCancellationRejectedError):
    """A journaled cancellation was definitively aborted by a final
    mutable gate before the broker was called. Subclassing explicit
    rejection applies the same caller-owned cancel-ID retirement rule."""


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


class GuardedEngineRequiresBuyingPowerProviderError(GuardedExecutionError):
    """GUARDED_ENGINE cannot validate entry capital without live buying power."""


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
        request_scheduler: Optional[MutationBudgetProtocol] = None,
        buying_power_provider: Optional[Callable[[str, str], float]] = None,
        mode_override: Optional[bool] = None,
        ownership_registry: Optional[_OwnershipRegistry] = None,
        emergency_journal: Optional[EmergencyJournal] = None,
        emergency_lease_allowance: Optional[EmergencyLeaseAllowance] = None,
        database_writable_provider: Optional[Callable[[], bool]] = None,
        handoff_pending_provider: Optional[Callable[[], bool]] = None,
        critical_alert_sink: Optional[Callable[[str, str, str], None]] = None,
        schema_migration_manager: Optional[Any] = None,
    ) -> None:
        self._real_broker: Broker = real_broker if real_broker is not None else KisBroker()
        self._engine = engine
        self._lease_protocol: ExecutionLeaseProtocol = lease_protocol or DefaultExecutionLeaseProtocol(
            engine=engine
        )
        if mutation_budget is not None and request_scheduler is not None:
            raise ValueError("Supply request_scheduler or mutation_budget, not both")
        # ``mutation_budget`` remains a compatibility keyword for existing
        # tests/callers. Production composition supplies the real Workstream
        # 10 scheduler through ``request_scheduler``.
        self._mutation_budget = request_scheduler or mutation_budget
        if request_scheduler is not None:
            install_process_kis_request_scheduler(request_scheduler)
        self._buying_power_provider = buying_power_provider
        self._mode_override = mode_override
        self._ownership = ownership_registry or _OwnershipRegistry()
        self._emergency_journal = emergency_journal or EmergencyJournal()
        self._emergency_lease_allowance = (
            emergency_lease_allowance
            or EmergencyLeaseAllowance(
                max_seconds=execution_config.EMERGENCY_LEASE_ALLOWANCE_SECONDS
            )
        )
        self._database_writable_provider = database_writable_provider
        self._handoff_pending_provider = handoff_pending_provider or (lambda: False)
        self._critical_alert_sink = critical_alert_sink
        self._schema_migration_manager = schema_migration_manager
        self._last_verified_lease: Optional[ExecutionLease] = None
        self._last_database_writable_state: Optional[bool] = None
        self._emergency_records: Dict[str, ExecutionOrderRecord] = {}
        # Process-local continuity across the instant the canonical database
        # becomes unavailable.  The same mutable record object is cached as
        # soon as a canonical command identity is prepared, so later status
        # transitions (ACKNOWLEDGED/WORKING/ambiguous cancel or submit) are
        # visible to offline orchestration before the guarded call returns or
        # raises.  This is only a conservative read-through cache; canonical
        # persistence remains authoritative after recovery.
        self._recent_execution_records: Dict[str, ExecutionOrderRecord] = {}
        self._cached_ownership_proofs: Dict[
            tuple[str, str, str], ExecutionOwnershipProof
        ] = {}

    @property
    def mode(self) -> ExecutionMode:
        return resolve_execution_mode(self._mode_override)

    @property
    def database_engine(self) -> Optional[Engine]:
        return self._engine

    @property
    def canonical_database_writable(self) -> bool:
        return self._canonical_database_is_writable()

    def require_guarded_runtime_ready(self) -> None:
        """Fail during composition if any guarded production gate is absent."""
        self._require_guarded_mode()
        self._require_engine()
        self._require_mutation_budget()
        self._require_buying_power_provider()
        if not getattr(self._lease_protocol, "epoch_verified", False):
            raise LeaseNotVerifiedError(
                "The configured guarded gateway cannot verify lease epochs"
            )

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
            if self._engine is not None:
                self._require_ownership(
                    environment, account_no, symbol, source
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
        ownership_symbol = str(
            kwargs.pop("ownership_symbol", "") or kwargs.get("symbol") or ""
        ).upper()
        symbol = ownership_symbol
        key = _recovery_key(environment, account_no, symbol)
        with self._ownership.claim(key, source):
            if self.mode != ExecutionMode.LEGACY_COMPATIBILITY:
                raise WrongGatewayModeError(
                    "cancel_order() is the LEGACY_COMPATIBILITY Broker-protocol method; "
                    "GUARDED_ENGINE mode requires cancel_guarded(request=CancelExecutionRequest(...)) "
                    "-- see the module docstring"
                )
            if self._engine is not None:
                self._require_ownership(
                    environment, account_no, ownership_symbol, source
                )
            return self._real_broker.cancel_order(
                environment=environment, account_no=account_no, is_reserved=is_reserved, **kwargs
            )

    # --- read-only passthroughs (never guarded -- not destructive) -------------

    def get_order(self, **kwargs: Any) -> List[BrokerOrderStatusSnapshot]:
        return self._execute_scheduled_read(
            lambda: self._real_broker.get_order(**kwargs),
            account_no=str(kwargs.get("account_no") or ""),
            endpoint="get_order",
            priority=RequestPriority.ACCOUNT_RECONCILIATION,
        )

    def discover_orders(self, **kwargs: Any) -> BrokerOrderDiscoveryResult:
        return self._execute_scheduled_read(
            lambda: self._real_broker.discover_orders(**kwargs),
            account_no=str(kwargs.get("account_no") or ""),
            endpoint="discover_orders",
            priority=RequestPriority.ACCOUNT_RECONCILIATION,
        )

    def get_positions(self, **kwargs: Any) -> Dict[str, Any]:
        return self._execute_scheduled_read(
            lambda: self._real_broker.get_positions(**kwargs),
            account_no=str(kwargs.get("account_no") or ""),
            endpoint="get_positions",
            priority=RequestPriority.ACCOUNT_RECONCILIATION,
        )

    # --- GUARDED_ENGINE only ----------------------------------------------------

    def _canonical_database_is_writable(self) -> bool:
        provider = self._database_writable_provider
        if provider is None:
            return True
        try:
            writable = bool(provider())
        except Exception:
            writable = False
        if writable:
            self._last_database_writable_state = True
        elif self._last_database_writable_state is not False:
            self.note_canonical_database_unavailable()
        return writable

    def note_canonical_database_unavailable(self) -> None:
        """Activate, but never extend, the last authoritative lease allowance."""

        if self._last_database_writable_state is False:
            return
        self._last_database_writable_state = False
        self._emergency_lease_allowance.begin_outage(
            self._last_verified_lease,
            verified_current=self._last_verified_lease is not None,
            handoff_pending=bool(self._handoff_pending_provider()),
        )

    def _emit_critical_alert(self, alert_class: str, dedupe_key: str, message: str) -> None:
        sink = self._critical_alert_sink
        if sink is None:
            logger.critical("%s [%s]: %s", alert_class, dedupe_key, message)
            return
        try:
            sink(alert_class, dedupe_key, message)
        except Exception:
            logger.exception("Critical alert sink failed for %s", alert_class)

    def note_canonical_lease_verified(self, lease: Optional[ExecutionLease]) -> None:
        """Cache an exact lease only after an authoritative healthy-DB check.

        The runtime may call this after its periodic lease check. A database
        outage can never create or extend this proof; a restart loses it.
        """

        if not self._canonical_database_is_writable():
            raise CanonicalDatabaseUnavailableError(
                "Cannot cache an execution lease while the canonical database is unavailable"
            )
        self._require_verified_lease(lease)

    def reconcile_emergency_journal(self) -> int:
        if not self._canonical_database_is_writable():
            raise CanonicalDatabaseUnavailableError(
                "Canonical database is unavailable during emergency-journal reconciliation"
            )
        count = self._emergency_journal.reconcile_into_canonical(self._require_engine())
        self._emergency_lease_allowance.clear()
        self._emergency_records.clear()
        if self._schema_migration_manager is not None:
            self._schema_migration_manager.reconcile_local_mutation_marker()
        return count

    def cached_emergency_record(
        self, client_order_id: str
    ) -> Optional[ExecutionOrderRecord]:
        return self._emergency_records.get(str(client_order_id or ""))

    def cached_execution_record(
        self, client_order_id: str
    ) -> Optional[ExecutionOrderRecord]:
        """Return the newest process-local canonical/emergency order view.

        This deliberately remains available while the canonical database is
        down.  It prevents a just-acknowledged destructive command from
        disappearing merely because the outage began before the runtime's
        next normal repository lookup.
        """

        key = str(client_order_id or "")
        return self._emergency_records.get(key) or self._recent_execution_records.get(
            key
        )

    def remember_canonical_execution_record(
        self, record: ExecutionOrderRecord
    ) -> None:
        """Refresh the process cache from a newer canonical read."""

        current = self._recent_execution_records.get(record.client_order_id)
        if current is None or int(record.version) >= int(current.version):
            self._recent_execution_records[record.client_order_id] = record

    def _mark_post_migration_broker_mutation(self) -> None:
        if self._schema_migration_manager is not None:
            self._schema_migration_manager.mark_post_migration_broker_mutation()

    def _cross_broker_boundary(self, operation: Callable[[], Any]) -> Any:
        self._mark_post_migration_broker_mutation()
        return operation()

    def submit_guarded(self, request: SubmitExecutionRequest) -> ExecutionOrderRecord:
        self._require_guarded_mode()
        key = _recovery_key(request.environment, request.account_no, request.symbol)
        with self._ownership.claim(key, request.source):
            if not self._canonical_database_is_writable():
                return self._do_emergency_submit(request)
            return self._do_submit(request)

    def cancel_guarded(self, request: CancelExecutionRequest) -> ExecutionOrderRecord:
        self._require_guarded_mode()
        if not self._canonical_database_is_writable():
            key = _recovery_key(request.environment, request.account_no, request.symbol)
            with self._ownership.claim(key, request.source):
                return self._do_emergency_cancel(request)
        record = self._fetch_record(request.client_order_id)
        if record is None:
            raise OrderNotFoundForCancelError(
                f"No ExecutionOrderRecord for client_order_id={request.client_order_id!r}"
            )
        key = _recovery_key(record.environment, record.account_no, record.symbol)
        with self._ownership.claim(key, request.source):
            return self._do_cancel(request, record=record, permission_check=is_cancellable)

    def replace_guarded(self, request: ReplaceExecutionRequest) -> ExecutionOrderRecord:
        """Composed, never a synthetic first-class broker status: validate
        the *entire* replacement request first (review finding 6, third
        pass -- an invalid or duplicate replacement request must make
        zero cancel calls, never cancel a perfectly good order only to
        discover afterward that the replacement itself was invalid), then
        persist a durable parent ``replace`` command *before* either
        broker call, then cancel the existing exact order (authorized by
        ``REPLACE``, not ``CANCEL`` -- finding 7 of the second pass),
        confirm a safe (``CANCELLED``) outcome, then submit a brand-new
        order linked via ``replaces_execution_order_id``. The original
        record is never mutated into the replacement.

        The parent ``replace`` command's own row
        (``idempotency_key=f"REPLACE:{replace_command_id}"``) is what lets
        restart recovery distinguish "a replace was requested" from either
        sub-step: its own status only ever finalizes to ``COMPLETED`` on
        full success; a replace that stopped partway (cancel confirmed,
        replacement not yet submitted) is reconstructed from the *linked*
        cancel/submit sub-commands' own already-durable statuses, not from
        additional intermediate writes to the parent row -- the command
        ledger's compare-and-set persistence (B4b) is deliberately a
        single ``REQUESTED -> one terminal state`` transition, not a
        multi-step state machine, and this reuses that as-is rather than
        fighting it.
        """
        self._require_guarded_mode()
        engine = self._require_engine()
        mutation_budget = self._require_mutation_budget()

        original = self._fetch_record(request.client_order_id)
        if original is None:
            raise OrderNotFoundForCancelError(f"No ExecutionOrderRecord for client_order_id={request.client_order_id!r}")
        key = _recovery_key(original.environment, original.account_no, original.symbol)
        with self._ownership.claim(key, request.source):
            # 1. validate the replacement request completely -- before
            # anything is mutated or cancelled.
            if original.environment != request.environment or original.account_no != request.account_no:
                raise CancelNotPermittedError(
                    f"client_order_id={request.client_order_id!r} belongs to "
                    f"{original.environment}/{original.account_no}, not the requested "
                    f"{request.environment}/{request.account_no}"
                )
            new_quantity = int(request.new_quantity)
            if new_quantity <= 0:
                raise ValueError(f"new_quantity must be positive, got {new_quantity}")
            if original.execution_policy == RESERVED_MOO_EXECUTION:
                new_limit_price = 0.0
            else:
                new_limit_price = float(request.new_limit_price)
                if not math.isfinite(new_limit_price) or new_limit_price <= 0:
                    raise ValueError(f"new_limit_price must be positive and finite, got {new_limit_price}")
            if not request.new_client_order_id:
                raise ValueError(
                    "ReplaceExecutionRequest.new_client_order_id must be a non-blank, caller-generated "
                    "stable identity"
                )
            if self._fetch_record(request.new_client_order_id) is not None:
                raise ValueError(
                    f"new_client_order_id={request.new_client_order_id!r} already exists -- a replacement "
                    "must use a fresh identity, never reuse an existing order's"
                )
            if original.side == OrderSide.BUY and original.intent == OrderIntent.ENTRY:
                require_pre_trade_risk_approval(
                    request.pre_trade_risk_decision,
                    environment=original.environment,
                    account_no=original.account_no,
                    symbol=original.symbol,
                    side=original.side,
                    intent=original.intent,
                    quantity=new_quantity,
                    reference_price=new_limit_price,
                    exchange=original.exchange,
                    execution_policy=original.execution_policy,
                    strategy_id=request.risk_strategy_id,
                    plan_id=request.risk_plan_id,
                )

            # 2. verify replace ownership/permission.
            self._require_ownership(
                original.environment, original.account_no, original.symbol, request.source,
                request.strategy_instance_id,
            )
            if not _cancellable_for_replace(original):
                raise CancelNotPermittedError(
                    f"client_order_id={request.client_order_id!r} is not currently replaceable "
                    f"(status={original.status.value}, recovery_state={original.recovery_state.value}, "
                    f"origin={original.origin.value})"
                )

            # 3. verify lease.
            self._require_verified_lease(request.lease)

            # 4. preflight both the cancel and submit mutation budgets
            # before committing to either broker call.
            replace_priority = self._priority_for_record(original)
            self._require_budget_available(
                mutation_budget,
                CommandType.REPLACE,
                account_no=original.account_no,
                endpoint="replace_order",
                priority=replace_priority,
                is_new_entry=(
                    original.side == OrderSide.BUY
                    and original.intent == OrderIntent.ENTRY
                ),
                consume=False,
            )
            self._require_budget_available(
                mutation_budget,
                CommandType.CANCEL,
                account_no=original.account_no,
                endpoint="cancel_order",
                priority=replace_priority,
                is_new_entry=False,
                consume=False,
            )
            self._require_budget_available(
                mutation_budget,
                CommandType.SUBMIT,
                account_no=original.account_no,
                endpoint="submit_order",
                priority=replace_priority,
                is_new_entry=(
                    original.side == OrderSide.BUY
                    and original.intent == OrderIntent.ENTRY
                ),
                consume=False,
            )

            # 5. persist the durable parent replace command/intention --
            # before either broker call, so a crash between the cancel and
            # the resubmit leaves durable evidence a replace was in
            # progress, not just two independent-looking sub-commands.
            replace_idempotency_key = f"REPLACE:{request.replace_command_id}"
            replace_command = ExecutionCommand(
                idempotency_key=replace_idempotency_key, command_type="replace",
                environment=original.environment, account_no=original.account_no, symbol=original.symbol,
                lease_epoch=request.lease.lease_epoch if request.lease else 0,
                owner_device_id=request.lease.device_id if request.lease else "",
                lease_token=request.lease.lease_token if request.lease else "",
                target_broker_order_id=original.broker_order_id, source=request.source.value,
            )
            ensure_execution_commands_table(engine)
            ensure_discovered_external_orders_table(engine)
            with engine.begin() as conn:
                require_no_active_unowned_external_order(
                    conn,
                    environment=original.environment,
                    account_no=original.account_no,
                    symbol=original.symbol,
                    allowed_adopted_client_order_id=original.client_order_id,
                )
                insert_command(conn, replace_command)

            # 6. cancel the original.
            cancel_request = CancelExecutionRequest(
                client_order_id=request.client_order_id,
                cancel_command_id=f"{request.replace_command_id}:CANCEL",
                environment=request.environment, account_no=request.account_no,
                lease=request.lease, source=request.source,
                strategy_instance_id=request.strategy_instance_id,
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

            # 7. submit the linked replacement.
            submit_request = SubmitExecutionRequest(
                client_order_id=request.new_client_order_id, environment=cancelled.environment,
                account_no=cancelled.account_no, symbol=cancelled.symbol, side=cancelled.side,
                intent=cancelled.intent, quantity=new_quantity, limit_price=new_limit_price,
                exchange=cancelled.exchange, execution_policy=cancelled.execution_policy,
                attempt_group_id=cancelled.attempt_group_id, attempt_number=cancelled.attempt_number + 1,
                lease=request.lease, source=request.source, strategy_instance_id=request.strategy_instance_id,
                replaces_execution_order_id=request.client_order_id,
                pre_trade_risk_decision=request.pre_trade_risk_decision,
                risk_strategy_id=request.risk_strategy_id,
                risk_plan_id=request.risk_plan_id,
            )
            result = self._do_submit(submit_request)

            # The parent replace command finalizes only on full success --
            # a replace that fails partway (e.g. the submit leg raises)
            # deliberately leaves this row at REQUESTED; restart recovery
            # reconstructs "how far did this get" from the cancel/submit
            # sub-commands' own already-durable statuses, per this
            # method's own docstring.
            with engine.begin() as conn:
                update_command_response(
                    conn, replace_idempotency_key, status="COMPLETED", broker_response={}
                )
            return result

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

    @staticmethod
    def _require_budget_available(
        budget: MutationBudgetProtocol,
        command_type: CommandType,
        *,
        account_no: str,
        endpoint: str,
        priority: RequestPriority,
        is_new_entry: bool,
        consume: bool,
    ) -> None:
        if getattr(budget, "context_aware", False):
            budget.require_available(
                command_type,
                account_no=account_no,
                endpoint=endpoint,
                priority=priority,
                is_new_entry=is_new_entry,
                consume=consume,
            )
            return
        # Compatibility for narrow PR2 test doubles implementing the old
        # one-argument seam. They are never used by production composition.
        budget.require_available(command_type)

    def _execute_scheduled_read(
        self,
        operation: Callable[[], Any],
        *,
        account_no: str,
        endpoint: str,
        priority: RequestPriority,
    ) -> Any:
        scheduler = self._mutation_budget
        if getattr(self._real_broker, "schedules_at_request_boundary", False):
            with kis_request_scope(
                scheduler=scheduler,
                account_no=account_no,
                kind=RequestKind.READ,
                priority=priority,
            ):
                return operation()
        execute = getattr(scheduler, "execute_read", None)
        if not callable(execute):
            return operation()
        return execute(
            operation,
            account_no=account_no,
            endpoint=endpoint,
            priority=priority,
        )

    def _execute_scheduled_mutation(
        self,
        operation: Callable[[], Any],
        *,
        command_type: CommandType,
        account_no: str,
        endpoint: str,
        priority: RequestPriority,
        is_new_entry: bool,
    ) -> Any:
        scheduler = self._require_mutation_budget()
        classifier = getattr(
            self._real_broker,
            "is_confirmed_pre_acceptance_rejection",
            None,
        )
        if getattr(self._real_broker, "schedules_at_request_boundary", False):
            with kis_request_scope(
                scheduler=scheduler,
                account_no=account_no,
                kind=RequestKind.MUTATION,
                priority=priority,
                command_type=command_type,
                endpoint=endpoint,
                is_new_entry=is_new_entry,
                mutation_classifier=(
                    classifier if callable(classifier) else None
                ),
            ):
                return operation()
        execute = getattr(scheduler, "execute_mutation", None)
        if not callable(execute):
            return operation()

        return execute(
            operation,
            command_type=command_type,
            account_no=account_no,
            endpoint=endpoint,
            priority=priority,
            is_new_entry=is_new_entry,
            is_confirmed_pre_acceptance_rejection=(
                classifier if callable(classifier) else None
            ),
        )

    @staticmethod
    def _priority_for_submit(request: SubmitExecutionRequest) -> RequestPriority:
        if request.side == OrderSide.BUY and request.intent == OrderIntent.ENTRY:
            return RequestPriority.NEW_ENTRY
        if request.intent in (OrderIntent.STOP_LOSS, OrderIntent.MANUAL_EXIT):
            return RequestPriority.EMERGENCY_EXIT
        return RequestPriority.EXIT_CANCEL_OR_RECONCILIATION

    @staticmethod
    def _priority_for_record(record: ExecutionOrderRecord) -> RequestPriority:
        if record.side == OrderSide.BUY:
            return RequestPriority.ENTRY_CANCEL
        if record.intent in (OrderIntent.STOP_LOSS, OrderIntent.MANUAL_EXIT):
            return RequestPriority.EMERGENCY_EXIT
        return RequestPriority.EXIT_CANCEL_OR_RECONCILIATION

    def _require_buying_power_provider(self) -> Callable[[str, str], float]:
        if self._buying_power_provider is None:
            raise GuardedEngineRequiresBuyingPowerProviderError(
                "GUARDED_ENGINE mode requires a buying_power_provider so A1 can "
                "validate actual available capital inside the reservation transaction"
            )
        return self._buying_power_provider

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
        if self._canonical_database_is_writable():
            self._last_verified_lease = lease
            self._emergency_lease_allowance.record_verified(
                lease,
                verified_current=True,
                handoff_pending=bool(self._handoff_pending_provider()),
            )

    def _require_ownership(
        self, environment: str, account_no: str, symbol: str, source: ExecutionSource,
        strategy_instance_id: str = "",
    ):
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
            # H1: "KANBAN plus strategy_instance_id" -- ownership is not
            # satisfied by the source alone; the calling strategy instance
            # must be the exact one this symbol is assigned to, so one
            # Kanban strategy can never act on a symbol assigned to a
            # different one (review finding 3, third pass).
            if not strategy_instance_id or strategy_instance_id != ownership.strategy_instance_id:
                raise ExecutionOwnershipMismatchError(
                    f"{environment}/{account_no}/{symbol} is KANBAN-owned by strategy_instance_id="
                    f"{ownership.strategy_instance_id!r}; {strategy_instance_id!r} is not authorized"
                )
            if self._canonical_database_is_writable():
                proof = ExecutionOwnershipProof.from_ownership(ownership)
                self._cached_ownership_proofs[
                    (ownership.environment, ownership.account_no, ownership.symbol)
                ] = proof
            return ownership
        # LEGACY (the H2 default, or an explicit assignment)
        if source == ExecutionSource.KANBAN_BOARD:
            raise ExecutionOwnershipMismatchError(
                f"{environment}/{account_no}/{symbol} is LEGACY-owned; KANBAN_BOARD is not authorized"
            )
        return ownership

    def note_canonical_ownership_verified(
        self,
        *,
        environment: str,
        account_no: str,
        symbol: str,
        source: ExecutionSource,
        strategy_instance_id: str,
    ) -> ExecutionOwnershipProof:
        if not self._canonical_database_is_writable():
            raise CanonicalDatabaseUnavailableError(
                "Cannot cache execution ownership while the canonical database is unavailable"
            )
        key = (
            str(environment or "").upper(),
            str(account_no or ""),
            str(symbol or "").upper(),
        )
        self._cached_ownership_proofs.pop(key, None)
        ownership = self._require_ownership(
            environment, account_no, symbol, source, strategy_instance_id
        )
        proof = ExecutionOwnershipProof.from_ownership(ownership)
        if proof.owner != ExecutionOwner.KANBAN:
            raise ExecutionOwnershipMismatchError(
                "Emergency Kanban execution requires explicit KANBAN ownership"
            )
        return proof

    def note_canonical_ownership_snapshot_verified(
        self,
        ownership: ExecutionOwnership,
        *,
        source: ExecutionSource,
        strategy_instance_id: str,
    ) -> ExecutionOwnershipProof:
        """Cache one row from a single authoritative bulk ownership read.

        This is equivalent to ``note_canonical_ownership_verified`` except the
        caller already fetched all relevant rows in one canonical query.
        Online broker mutations still perform their own exact boundary read.
        """

        if not self._canonical_database_is_writable():
            raise CanonicalDatabaseUnavailableError(
                "Cannot cache execution ownership while the canonical database is unavailable"
            )
        key = (
            str(ownership.environment or "").upper(),
            str(ownership.account_no or ""),
            str(ownership.symbol or "").upper(),
        )
        self._cached_ownership_proofs.pop(key, None)
        if (
            ownership.owner != ExecutionOwner.KANBAN
            or source != ExecutionSource.KANBAN_BOARD
            or not strategy_instance_id
            or strategy_instance_id != ownership.strategy_instance_id
            or int(ownership.version or 0) <= 0
        ):
            raise ExecutionOwnershipMismatchError(
                "Emergency Kanban execution requires exact KANBAN strategy ownership"
            )
        proof = ExecutionOwnershipProof.from_ownership(ownership)
        self._cached_ownership_proofs[key] = proof
        return proof

    def _require_cached_emergency_ownership(
        self,
        *,
        environment: str,
        account_no: str,
        symbol: str,
        source: ExecutionSource,
        strategy_instance_id: str,
    ) -> ExecutionOwnershipProof:
        key = (
            str(environment or "").upper(),
            str(account_no or ""),
            str(symbol or "").upper(),
        )
        proof = self._cached_ownership_proofs.get(key)
        if (
            proof is None
            or proof.owner != ExecutionOwner.KANBAN
            or source != ExecutionSource.KANBAN_BOARD
            or not strategy_instance_id
            or strategy_instance_id != proof.strategy_instance_id
            or proof.version <= 0
        ):
            raise EmergencyActionNotPermittedError(
                "Emergency mutation lacks exact cached KANBAN ownership/strategy proof"
            )
        return proof

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

    def _require_emergency_allowance(
        self, lease: Optional[ExecutionLease]
    ) -> ExecutionLease:
        if bool(self._handoff_pending_provider()):
            self._emergency_lease_allowance.clear()
            raise EmergencyActionNotPermittedError(
                "Emergency execution is forbidden while device handoff is pending"
            )
        if self._emergency_lease_allowance.snapshot is None:
            error = EmergencyActionNotPermittedError(
                "No exact lease was verified when the canonical database outage began"
            )
            self._emit_critical_alert(
                "DATABASE_UNAVAILABLE",
                f"emergency-lease:{getattr(lease, 'device_id', 'unknown')}",
                str(error),
            )
            raise error
        try:
            self._emergency_lease_allowance.require_valid(lease)
        except EmergencyLeaseAllowanceError as exc:
            self._emit_critical_alert(
                "DATABASE_UNAVAILABLE",
                f"emergency-lease:{getattr(lease, 'device_id', 'unknown')}",
                str(exc),
            )
            raise EmergencyActionNotPermittedError(str(exc)) from exc
        assert lease is not None
        return lease

    def _append_emergency_outcome(
        self,
        *,
        request_entry: Dict[str, Any],
        idempotency_key: str,
        status: str,
        broker_response: Optional[Dict[str, Any]],
    ) -> None:
        try:
            self._emergency_journal.append_outcome(
                requested_sequence=int(request_entry["sequence"]),
                idempotency_key=idempotency_key,
                status=status,
                broker_response=broker_response,
            )
        except Exception as exc:
            self._emit_critical_alert(
                "DATABASE_UNAVAILABLE",
                idempotency_key,
                "A broker mutation completed but its emergency outcome could not be fsynced: "
                f"{exc}",
            )
            raise AmbiguousPostBrokerPersistenceError(
                "Emergency broker mutation completed but the local outcome journal failed; "
                "never retry automatically"
            ) from exc

    def _do_emergency_submit(
        self, request: SubmitExecutionRequest
    ) -> ExecutionOrderRecord:
        if (
            not request.emergency
            or request.side != OrderSide.SELL
            or request.intent not in (OrderIntent.STOP_LOSS, OrderIntent.MANUAL_EXIT)
        ):
            raise CanonicalDatabaseUnavailableError(
                "Canonical database unavailable; ordinary and entry commands fail closed"
            )
        lease = self._require_emergency_allowance(request.lease)
        ownership_proof = self._require_cached_emergency_ownership(
            environment=request.environment,
            account_no=request.account_no,
            symbol=request.symbol,
            source=request.source,
            strategy_instance_id=request.strategy_instance_id,
        )
        with trading_state.allow_cached_emergency_authorization():
            trading_state.require_trading_enabled(
                request.environment,
                request.symbol,
            )
        quantity = int(request.quantity)
        limit_price = float(request.limit_price)
        if quantity <= 0 or not math.isfinite(limit_price) or limit_price <= 0:
            raise ValueError("Emergency SELL requires positive quantity and finite limit price")
        idempotency_key = f"SUBMIT:{request.client_order_id}"
        try:
            requested = self._emergency_journal.append_requested(
                idempotency_key=idempotency_key,
                command_type="submit",
                environment=request.environment,
                account_no=request.account_no,
                symbol=request.symbol,
                lease=lease,
                source=request.source.value,
                ownership_proof=ownership_proof.to_dict(),
                order_payload={
                    "client_order_id": request.client_order_id,
                    "side": request.side.value,
                    "intent": request.intent.value,
                    "quantity": quantity,
                    "limit_price": limit_price,
                    "exchange": request.exchange,
                    "execution_policy": request.execution_policy,
                    "attempt_group_id": request.attempt_group_id,
                    "attempt_number": int(request.attempt_number),
                },
            )
        except Exception as exc:
            self._emit_critical_alert(
                "DATABASE_UNAVAILABLE",
                idempotency_key,
                f"Emergency journal pre-write failed; broker was not called: {exc}",
            )
            raise EmergencyJournalUnavailableError(str(exc)) from exc

        try:
            with trading_state.allow_cached_emergency_authorization():
                submission = self._execute_scheduled_mutation(
                    lambda: self._cross_broker_boundary(
                        lambda: self._real_broker.submit_order(
                            environment=request.environment,
                            account_no=request.account_no,
                            symbol=request.symbol,
                            side=request.side,
                            quantity=quantity,
                            limit_price=limit_price,
                            exchange=request.exchange,
                            execution_policy=request.execution_policy,
                        )
                    ),
                    command_type=CommandType.SUBMIT,
                    account_no=request.account_no,
                    endpoint="submit_order",
                    priority=RequestPriority.EMERGENCY_EXIT,
                    is_new_entry=False,
                )
        except Exception as exc:
            try:
                ambiguous = self._real_broker.is_ambiguous_submission_error(exc)
            except Exception:
                ambiguous = True
            self._append_emergency_outcome(
                request_entry=requested,
                idempotency_key=idempotency_key,
                status="AMBIGUOUS" if ambiguous else "FAILED",
                broker_response={"error": str(exc)},
            )
            if ambiguous:
                raise GuardedSubmissionAmbiguousError(str(exc)) from exc
            raise GuardedSubmissionRejectedError(str(exc)) from exc

        self._append_emergency_outcome(
            request_entry=requested,
            idempotency_key=idempotency_key,
            status="ACKNOWLEDGED",
            broker_response={
                **submission.raw_response,
                "broker_order_id": submission.broker_order_id,
            },
        )
        record = ExecutionOrderRecord(
            environment=request.environment,
            account_no=request.account_no,
            symbol=request.symbol,
            side=request.side,
            intent=request.intent,
            client_order_id=request.client_order_id,
            attempt_group_id=request.attempt_group_id,
            attempt_number=request.attempt_number,
            attempt_deadline_at=request.attempt_deadline_at,
            submitted_quantity=quantity,
            submitted_limit_price=limit_price,
            remaining_quantity=quantity,
            exchange=request.exchange,
            execution_policy=request.execution_policy,
            owner_device_id=lease.device_id,
            lease_token=lease.lease_token,
            lease_epoch=lease.lease_epoch,
        )
        apply_status_transition(record, ExecutionOrderStatus.SUBMITTING)
        apply_status_transition(
            record,
            ExecutionOrderStatus.ACKNOWLEDGED,
            broker_order_id=submission.broker_order_id,
        )
        self._emit_critical_alert(
            "EMERGENCY_LIQUIDATION_ATTEMPTED",
            idempotency_key,
            f"Emergency protective SELL was submitted for {request.symbol}",
        )
        self._emergency_records[record.client_order_id] = record
        return record

    def _do_emergency_cancel(
        self, request: CancelExecutionRequest
    ) -> ExecutionOrderRecord:
        if (
            not request.emergency
            or (
                request.side != OrderSide.SELL.value
                and not (
                    request.side == OrderSide.BUY.value
                    and request.protective_entry_completion
                )
            )
            or not request.symbol
            or not request.broker_order_id
            or request.quantity <= 0
        ):
            raise CanonicalDatabaseUnavailableError(
                "Canonical database unavailable; only exact protective SELL or "
                "entry-completion BUY cancellations may use the local journal"
            )
        lease = self._require_emergency_allowance(request.lease)
        ownership_proof = self._require_cached_emergency_ownership(
            environment=request.environment,
            account_no=request.account_no,
            symbol=request.symbol,
            source=request.source,
            strategy_instance_id=request.strategy_instance_id,
        )
        with trading_state.allow_cached_emergency_authorization():
            trading_state.require_trading_enabled(
                request.environment,
                request.symbol,
            )
        idempotency_key = f"CANCEL:{request.cancel_command_id}"
        try:
            requested = self._emergency_journal.append_requested(
                idempotency_key=idempotency_key,
                command_type="cancel",
                environment=request.environment,
                account_no=request.account_no,
                symbol=request.symbol,
                lease=lease,
                source=request.source.value,
                ownership_proof=ownership_proof.to_dict(),
                target_broker_order_id=request.broker_order_id,
                order_payload={
                    "client_order_id": request.client_order_id,
                    "side": request.side,
                    "protective_entry_completion": request.protective_entry_completion,
                    "quantity": request.quantity,
                    "exchange": request.exchange,
                },
            )
        except Exception as exc:
            self._emit_critical_alert(
                "DATABASE_UNAVAILABLE",
                idempotency_key,
                f"Emergency journal pre-write failed; broker was not called: {exc}",
            )
            raise EmergencyJournalUnavailableError(str(exc)) from exc
        try:
            with trading_state.allow_cached_emergency_authorization():
                snapshot = self._execute_scheduled_mutation(
                    lambda: self._cross_broker_boundary(
                        lambda: self._real_broker.cancel_order(
                            environment=request.environment,
                            account_no=request.account_no,
                            is_reserved=False,
                            symbol=request.symbol,
                            broker_order_id=request.broker_order_id,
                            quantity=request.quantity,
                            side=request.side,
                            exchange=request.exchange,
                        )
                    ),
                    command_type=CommandType.CANCEL,
                    account_no=request.account_no,
                    endpoint="cancel_order",
                    priority=RequestPriority.EMERGENCY_EXIT,
                    is_new_entry=False,
                )
        except Exception as exc:
            classify = getattr(self._real_broker, "is_ambiguous_cancellation_error", None)
            try:
                ambiguous = classify(exc) if callable(classify) else True
            except Exception:
                ambiguous = True
            self._append_emergency_outcome(
                request_entry=requested,
                idempotency_key=idempotency_key,
                status="AMBIGUOUS" if ambiguous else "FAILED",
                broker_response={"error": str(exc)},
            )
            if ambiguous:
                raise GuardedCancellationAmbiguousError(str(exc)) from exc
            raise GuardedCancellationRejectedError(str(exc)) from exc
        self._append_emergency_outcome(
            request_entry=requested,
            idempotency_key=idempotency_key,
            status="ACKNOWLEDGED",
            broker_response={
                **snapshot.raw_response,
                "normalized_status": snapshot.status.value,
                "filled_quantity": snapshot.filled_quantity,
                "remaining_quantity": snapshot.remaining_quantity,
                "average_fill_price": snapshot.avg_fill_price,
            },
        )
        record = ExecutionOrderRecord(
            environment=request.environment,
            account_no=request.account_no,
            symbol=request.symbol,
            side=OrderSide(request.side),
            intent=(
                OrderIntent.ENTRY
                if request.protective_entry_completion
                else OrderIntent.MANUAL_EXIT
            ),
            client_order_id=request.client_order_id,
            broker_order_id=request.broker_order_id,
            submitted_quantity=request.quantity,
            remaining_quantity=request.quantity,
            exchange=request.exchange,
            owner_device_id=lease.device_id,
            lease_token=lease.lease_token,
            lease_epoch=lease.lease_epoch,
            broker_identity_status=BrokerIdentityStatus.EXACT,
            status=ExecutionOrderStatus.ACKNOWLEDGED,
        )
        apply_status_transition(record, ExecutionOrderStatus.CANCEL_PENDING)
        record.filled_quantity = max(0, snapshot.filled_quantity)
        record.remaining_quantity = max(0, snapshot.remaining_quantity)
        if snapshot.status == OrderStatus.CANCELLED:
            apply_status_transition(record, ExecutionOrderStatus.CANCELLED)
        elif snapshot.status == OrderStatus.FILLED:
            record.filled_quantity = snapshot.filled_quantity or request.quantity
            record.remaining_quantity = 0
            apply_status_transition(record, ExecutionOrderStatus.FILLED)
        elif snapshot.status == OrderStatus.PARTIALLY_FILLED:
            apply_status_transition(record, ExecutionOrderStatus.PARTIALLY_FILLED)
        else:
            _transition_recovery_state(record, OrderRecoveryState.DISCOVERING)
        self._emergency_records[record.client_order_id] = record
        return record

    def _do_submit(self, request: SubmitExecutionRequest) -> ExecutionOrderRecord:
        engine = self._require_engine()
        mutation_budget = self._require_mutation_budget()
        environment = request.environment
        account_no = request.account_no
        symbol = request.symbol

        # 1. feature/environment/account/session inputs.
        trading_state.require_trading_enabled(environment, symbol)

        # H1/B2: persisted execution ownership.
        self._require_ownership(environment, account_no, symbol, request.source, request.strategy_instance_id)

        # 2. execution lease and lease epoch -- required, never fails open.
        self._require_verified_lease(request.lease)

        # B2: mutation budget (Workstream 10 seam).
        submit_priority = self._priority_for_submit(request)
        is_new_entry = (
            request.side == OrderSide.BUY and request.intent == OrderIntent.ENTRY
        )
        if is_new_entry and self._schema_migration_manager is not None:
            self._schema_migration_manager.require_entries_ready()
        self._require_budget_available(
            mutation_budget,
            CommandType.SUBMIT,
            account_no=account_no,
            endpoint="submit_order",
            priority=submit_priority,
            is_new_entry=is_new_entry,
            consume=False,
        )

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

        def require_current_entry_risk_approval() -> None:
            if request.side != OrderSide.BUY or request.intent != OrderIntent.ENTRY:
                return
            require_pre_trade_risk_approval(
                request.pre_trade_risk_decision,
                environment=environment,
                account_no=account_no,
                symbol=symbol,
                side=request.side,
                intent=request.intent,
                quantity=quantity,
                reference_price=limit_price,
                exchange=request.exchange,
                execution_policy=request.execution_policy,
                strategy_id=request.risk_strategy_id,
                plan_id=request.risk_plan_id,
            )

        # Approval must be valid before any command or capital row is written.
        require_current_entry_risk_approval()

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
        buying_power = (
            float(self._require_buying_power_provider()(environment, account_no) or 0.0)
            if requested_notional > 0
            else 0.0
        )

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
            attempt_deadline_at=request.attempt_deadline_at,
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
        ensure_discovered_external_orders_table(engine)
        with engine.begin() as conn:
            require_no_active_unowned_external_order(
                conn,
                environment=environment,
                account_no=account_no,
                symbol=symbol,
            )
            insert_command(conn, command)
            insert_reservation_if_available(
                conn, reservation, buying_power=buying_power
            )
            insert_execution_order(conn, record)

        # 7 + 8. separate durable transaction: PREPARED -> SUBMITTING.
        apply_status_transition(record, ExecutionOrderStatus.SUBMITTING)
        with engine.begin() as conn:
            update_execution_order(conn, record, expected_version=record.version)
        # Only a durably journaled command can fence the outage path.  Cache
        # after SUBMITTING commits (and before the broker boundary), never for
        # a prepare transaction that itself failed.
        self._recent_execution_records[client_order_id] = record

        # Finding 7 (third pass): re-verify ownership and lease immediately
        # before the actual broker call, not only once, earlier, before the
        # journal/SUBMITTING commit -- another device could transfer
        # ownership or advance the lease epoch during that interval. This
        # is a re-check against the current authoritative state at the
        # last possible moment, not yet a persisted fencing-token/version
        # proof threaded through the command row itself (a further
        # enhancement -- see the module's own follow-up notes); it still
        # closes the concrete race: a stale authorization can no longer
        # reach the broker.
        try:
            self._require_ownership(
                environment, account_no, symbol, request.source,
                request.strategy_instance_id,
            )
            self._require_verified_lease(request.lease)
            # Recheck at the last possible moment because lease/database work
            # above may consume most of the approval's short TTL.
            require_current_entry_risk_approval()
            with coordination_read_connection(engine) as conn:
                require_no_active_unowned_external_order(
                    conn,
                    environment=environment,
                    account_no=account_no,
                    symbol=symbol,
                )
        except (
            ExecutionOwnershipMismatchError,
            LeaseNotVerifiedError,
            ActiveExternalOrderFenceError,
            PreTradeRiskRejectedError,
        ) as gate_error:
            # The broker boundary has definitely not been entered. Retire
            # every A1 artifact atomically so a final-gate race cannot look
            # like an ambiguous submission or keep capital reserved.
            apply_status_transition(record, ExecutionOrderStatus.CANCELLED_LOCALLY)
            reservation.release()
            with engine.begin() as conn:
                update_execution_order(conn, record, expected_version=record.version)
                update_command_response(
                    conn,
                    idempotency_key,
                    status="PRE_BROKER_ABORTED",
                    broker_response={"error": str(gate_error)},
                )
                update_reservation(
                    conn,
                    reservation,
                    expected_version=reservation.version,
                )
            raise GuardedSubmissionPreBrokerAbortedError(str(gate_error)) from gate_error

        # 9. only now call the broker.
        try:
            submission = self._execute_scheduled_mutation(
                lambda: self._cross_broker_boundary(
                    lambda: self._real_broker.submit_order(
                        environment=environment,
                        account_no=account_no,
                        symbol=symbol,
                        side=request.side,
                        quantity=quantity,
                        limit_price=limit_price,
                        exchange=request.exchange,
                        execution_policy=request.execution_policy,
                    )
                ),
                command_type=CommandType.SUBMIT,
                account_no=account_no,
                endpoint="submit_order",
                priority=submit_priority,
                is_new_entry=is_new_entry,
            )
        except Exception as exc:
            error_message = str(exc)
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
                        broker_response={"error": error_message},
                    )
                    if not ambiguous:
                        # 11: never automatically retry -- a clean rejection
                        # gives the reservation back; an ambiguous one does
                        # not (the order may yet turn out to exist).
                        reservation.release()
                        update_reservation(
                            conn,
                            reservation,
                            expected_version=reservation.version,
                        )

            self._persist_or_raise_ambiguous(
                _persist_failure, context=f"submit {client_order_id!r} failure persistence"
            )
            if ambiguous:
                raise GuardedSubmissionAmbiguousError(error_message) from exc
            raise GuardedSubmissionRejectedError(error_message) from exc

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
        # Cache before the first transition.  Every later mutation is applied
        # to this same object, including ambiguous outcomes that raise instead
        # of returning a record to the caller.
        self._recent_execution_records[record.client_order_id] = record
        engine = self._require_engine()
        mutation_budget = self._require_mutation_budget()

        if record.environment != request.environment or record.account_no != request.account_no:
            raise CancelNotPermittedError(
                f"client_order_id={request.client_order_id!r} belongs to "
                f"{record.environment}/{record.account_no}, not the requested "
                f"{request.environment}/{request.account_no}"
            )

        self._require_ownership(
            record.environment, record.account_no, record.symbol, request.source, request.strategy_instance_id
        )
        self._require_verified_lease(request.lease)
        cancel_priority = self._priority_for_record(record)
        self._require_budget_available(
            mutation_budget,
            CommandType.CANCEL,
            account_no=record.account_no,
            endpoint="cancel_order",
            priority=cancel_priority,
            is_new_entry=False,
            consume=False,
        )

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
        ensure_discovered_external_orders_table(engine)
        # Idempotency: insert_command raises DuplicateCommandError on a
        # replayed cancel_command_id, propagated unchanged -- a genuinely
        # new cancel decision must use a new cancel_command_id (finding 8),
        # which is the caller's (ExecutionWorkflowService's) job, not this
        # method's.
        pre_cancel_status = record.status
        with engine.begin() as conn:
            require_no_active_unowned_external_order(
                conn,
                environment=record.environment,
                account_no=record.account_no,
                symbol=record.symbol,
                allowed_adopted_client_order_id=record.client_order_id,
            )
            insert_command(conn, command)
            apply_status_transition(record, ExecutionOrderStatus.CANCEL_PENDING)
            update_execution_order(conn, record, expected_version=record.version)

        # Finding 7 (third pass): re-verify ownership and lease immediately
        # before the actual broker call -- see _do_submit's identical
        # re-check for the full reasoning.
        try:
            self._require_ownership(
                record.environment, record.account_no, record.symbol,
                request.source, request.strategy_instance_id,
            )
            self._require_verified_lease(request.lease)
            with coordination_read_connection(engine) as conn:
                require_no_active_unowned_external_order(
                    conn,
                    environment=record.environment,
                    account_no=record.account_no,
                    symbol=record.symbol,
                    allowed_adopted_client_order_id=record.client_order_id,
                )
        except (
            ExecutionOwnershipMismatchError,
            LeaseNotVerifiedError,
            ActiveExternalOrderFenceError,
        ) as gate_error:
            # No cancel reached the broker. Restore the exact live status
            # captured before CANCEL_PENDING and retire this caller-owned
            # command ID as a definitive local rejection.
            apply_status_transition(record, pre_cancel_status)
            with engine.begin() as conn:
                update_execution_order(conn, record, expected_version=record.version)
                update_command_response(
                    conn,
                    cancel_idempotency_key,
                    status="PRE_BROKER_ABORTED",
                    broker_response={"error": str(gate_error)},
                )
            raise GuardedCancellationPreBrokerAbortedError(str(gate_error)) from gate_error

        quantity = record.remaining_quantity or record.submitted_quantity
        try:
            snapshot = self._execute_scheduled_mutation(
                lambda: self._cross_broker_boundary(
                    lambda: self._real_broker.cancel_order(
                        environment=record.environment,
                        account_no=record.account_no,
                        is_reserved=(
                            record.execution_policy == RESERVED_MOO_EXECUTION
                        ),
                        symbol=record.symbol,
                        broker_order_id=record.broker_order_id,
                        quantity=quantity,
                        side=record.side.value,
                        exchange=record.exchange or "NASD",
                    )
                ),
                command_type=CommandType.CANCEL,
                account_no=record.account_no,
                endpoint="cancel_order",
                priority=cancel_priority,
                is_new_entry=False,
            )
        except Exception as exc:
            error_message = str(exc)
            classify = getattr(self._real_broker, "is_ambiguous_cancellation_error", None)
            try:
                ambiguous = classify(exc) if callable(classify) else True
            except Exception:
                ambiguous = True

            def _persist_cancel_failure() -> None:
                with engine.begin() as conn:
                    update_command_response(
                        conn, cancel_idempotency_key, status="AMBIGUOUS" if ambiguous else "FAILED",
                        broker_response={"error": error_message},
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
                raise GuardedCancellationAmbiguousError(error_message) from exc
            raise GuardedCancellationRejectedError(error_message) from exc

        record.filled_quantity = max(record.filled_quantity, snapshot.filled_quantity)
        if snapshot.avg_fill_price:
            record.average_fill_price = snapshot.avg_fill_price
        record.remaining_quantity = max(
            0,
            snapshot.remaining_quantity
            if snapshot.remaining_quantity
            else record.submitted_quantity - record.filled_quantity,
        )
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
                if record.status in (
                    ExecutionOrderStatus.CANCELLED,
                    ExecutionOrderStatus.FILLED,
                ) and record.capital_reservation_id:
                    reservation = fetch_reservation(
                        engine, record.capital_reservation_id
                    )
                    if reservation is not None and reservation.is_open():
                        filled_notional = record.filled_quantity * (
                            record.average_fill_price or record.submitted_limit_price
                        )
                        if filled_notional > 0:
                            reservation.consume(filled_notional)
                        if reservation.is_open():
                            reservation.release()
                        update_reservation(
                            conn,
                            reservation,
                            expected_version=reservation.version,
                        )

        self._persist_or_raise_ambiguous(
            _persist_cancel_success, context=f"cancel {request.client_order_id!r} success persistence",
            broker_order_id=record.broker_order_id, raw_response=snapshot.raw_response,
        )
        return record


# --- process-wide default instance (LEGACY_COMPATIBILITY only) --------------

_default_gateway_lock = threading.Lock()
_default_gateway: Optional[ExecutionCommandGateway] = None
_legacy_gateways_by_engine: "weakref.WeakKeyDictionary[Engine, ExecutionCommandGateway]" = (
    weakref.WeakKeyDictionary()
)


def get_default_execution_gateway() -> ExecutionCommandGateway:
    """The gateway every existing legacy call site is migrated to use as
    its default ``broker=`` (Workstream 9) -- wraps the real
    :class:`~src.services.broker.KisBroker` with no database engine,
    lease protocol, mutation budget, or buying-power provider configured.
    This singleton is therefore compatibility-only; guarded runtimes use
    an explicitly constructed gateway instead. See
    :mod:`src.services.buyboard_runtime`'s guarded composition root
    (finding 10) for the ``GUARDED_ENGINE``-capable gateway construction.
    """
    global _default_gateway
    if _default_gateway is None:
        with _default_gateway_lock:
            if _default_gateway is None:
                _default_gateway = ExecutionCommandGateway(
                    real_broker=KisBroker(),
                    mode_override=False,
                )
    return _default_gateway


def get_legacy_execution_gateway(engine: Engine) -> ExecutionCommandGateway:
    """Compatibility gateway with durable H1 ownership enforcement.

    UI/runtime callers that have the shared database must use this variant;
    the engine-less singleton remains only for standalone/backward-compatible
    callers that have no ownership database in scope.
    """

    if engine is None:
        return get_default_execution_gateway()
    with _default_gateway_lock:
        gateway = _legacy_gateways_by_engine.get(engine)
        if gateway is None:
            gateway = ExecutionCommandGateway(
                real_broker=KisBroker(),
                engine=engine,
                mode_override=False,
            )
            _legacy_gateways_by_engine[engine] = gateway
        return gateway


def build_guarded_execution_gateway(
    *,
    engine: Engine,
    lease_protocol: ExecutionLeaseProtocol,
    request_scheduler: Optional[MutationBudgetProtocol] = None,
    mutation_budget: Optional[MutationBudgetProtocol] = None,
    buying_power_provider: Callable[[str, str], float],
    real_broker: Optional[Broker] = None,
    emergency_journal: Optional[EmergencyJournal] = None,
    emergency_lease_allowance: Optional[EmergencyLeaseAllowance] = None,
    database_writable_provider: Optional[Callable[[], bool]] = None,
    handoff_pending_provider: Optional[Callable[[], bool]] = None,
    critical_alert_sink: Optional[Callable[[str, str, str], None]] = None,
    schema_migration_manager: Optional[Any] = None,
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
    if request_scheduler is not None and mutation_budget is not None:
        raise GuardedEngineRequiresMutationBudgetError(
            "build_guarded_execution_gateway accepts request_scheduler or mutation_budget, not both"
        )
    scheduler = request_scheduler or mutation_budget
    if scheduler is None:
        raise GuardedEngineRequiresMutationBudgetError(
            "build_guarded_execution_gateway requires request_scheduler"
        )
    if buying_power_provider is None:
        raise GuardedEngineRequiresBuyingPowerProviderError(
            "build_guarded_execution_gateway requires buying_power_provider"
        )
    return ExecutionCommandGateway(
        real_broker=real_broker if real_broker is not None else KisBroker(),
        engine=engine,
        lease_protocol=lease_protocol,
        request_scheduler=scheduler,
        buying_power_provider=buying_power_provider,
        emergency_journal=emergency_journal,
        emergency_lease_allowance=emergency_lease_allowance,
        database_writable_provider=database_writable_provider,
        handoff_pending_provider=handoff_pending_provider,
        critical_alert_sink=critical_alert_sink,
        schema_migration_manager=schema_migration_manager,
    )
