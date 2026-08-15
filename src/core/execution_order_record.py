"""Durable local identity for every broker order the application submits.

``docs/kanban_production_readiness.md``, Workstream 2 (A1-A3, A6), signed
off in that document's revision 3.1, amended by revision 3.2 (a narrow
errata pass discovered during PR1 implementation -- see that document's
change log for the exact list). This is the fix for INV-1 ("every broker
order submitted by the application has a durable local identity") -- prior
recovery code repeatedly had to *infer* whether a discovered broker order
belonged to this application from account+symbol+side+quantity+price,
because no durable "we submitted this exact order" record survived a crash
between broker acceptance and local persistence. An :class:`ExecutionOrderRecord`
is that record, written atomically (with its capital reservation and
command) *before* the broker is ever called -- see
:mod:`src.services.execution_order_repository` for its durable persistence
and :func:`~src.services.execution_command_gateway` (Workstream 3) for the
atomic pre-submission transaction this record's ``PREPARED``/``SUBMITTING``
states exist to support.

Two independent status-like dimensions, deliberately not merged (revision
3.1's central correction -- see the module docstring history in
``docs/kanban_production_readiness.md`` for why a single ``OrderOwnership``
field was wrong):

- :class:`ExecutionOrderStatus` -- the order's own broker-facing lifecycle.
- :class:`OrderOrigin` / :class:`BrokerIdentityStatus` -- did the
  application create this record at all, and is the *exact* broker order
  actually known yet. A ``PREPARED`` record has ``origin=APPLICATION`` (we
  are about to submit it) but ``broker_identity_status=NOT_ASSIGNED`` (no
  broker call has happened yet) -- it is not "verified" just because the
  application created it. Only :func:`mark_broker_identity_exact` may ever
  set ``broker_identity_status=EXACT``, and only alongside a real
  ``broker_order_id`` -- enforced (not merely encouraged) by
  :func:`apply_status_transition` at the point identity actually becomes
  load-bearing (``ACKNOWLEDGED``), and immutable once ``EXACT`` except for
  idempotent reconfirmation of the same ID (revision 3.2).

A broker order discovered with *no* matching ``ExecutionOrderRecord`` at
all is a fundamentally different object -- see
:class:`~src.core.discovered_external_order.DiscoveredExternalOrder`, which
is never created here and never becomes one of these except through an
explicit, audited adoption (see :class:`AdoptedOrderPermission`, revision
3.2, for the resulting record's scoped authority).

Every enum coercion in this module fails closed (raises on an unrecognized
value) rather than silently substituting a default -- revision 3.2: unlike
a broker response snapshot (where an unrecognized value defaulting to
``UNKNOWN`` is acceptable), a corrupted or invalid value for this record's
*own* authoritative fields must never be silently reinterpreted as
something else.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, FrozenSet, Optional

from src.core.order_recovery_state import OrderRecoveryState
from src.core.order_state import OrderIntent, OrderSide, REGULAR_LIMIT_EXECUTION, utc_now_iso


class ExecutionOrderStatus(str, Enum):
    PREPARED = "PREPARED"  # persisted, not yet sent to the broker
    SUBMITTING = "SUBMITTING"  # broker call in flight / durably committed pre-call
    ACKNOWLEDGED = "ACKNOWLEDGED"  # broker_order_id known
    UNKNOWN_SUBMISSION_STATE = "UNKNOWN_SUBMISSION_STATE"  # ambiguous window
    NOT_ACCEPTED_CONFIRMED = "NOT_ACCEPTED_CONFIRMED"  # inferred non-acceptance
    REJECTED = "REJECTED"  # explicit broker rejection
    WORKING = "WORKING"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCEL_PENDING = "CANCEL_PENDING"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    CANCELLED_LOCALLY = "CANCELLED_LOCALLY"  # gateway gates rejected it pre-call; nothing was ever sent


# Terminal (no outbound transitions) -- see the ExecutionOrderStatus
# transition table in docs/kanban_production_readiness.md.
TERMINAL_EXECUTION_ORDER_STATUSES: FrozenSet[ExecutionOrderStatus] = frozenset(
    {
        ExecutionOrderStatus.FILLED,
        ExecutionOrderStatus.CANCELLED,
        ExecutionOrderStatus.REJECTED,
        ExecutionOrderStatus.EXPIRED,
        ExecutionOrderStatus.CANCELLED_LOCALLY,
        ExecutionOrderStatus.NOT_ACCEPTED_CONFIRMED,
    }
)

# The transition table, encoded directly -- any FROM status missing here
# (i.e. every terminal one) has no allowed outbound transitions at all.
_ALLOWED_STATUS_TRANSITIONS: Dict[ExecutionOrderStatus, FrozenSet[ExecutionOrderStatus]] = {
    ExecutionOrderStatus.PREPARED: frozenset(
        {ExecutionOrderStatus.SUBMITTING, ExecutionOrderStatus.CANCELLED_LOCALLY}
    ),
    ExecutionOrderStatus.SUBMITTING: frozenset(
        {
            ExecutionOrderStatus.ACKNOWLEDGED,
            ExecutionOrderStatus.REJECTED,
            ExecutionOrderStatus.UNKNOWN_SUBMISSION_STATE,
        }
    ),
    ExecutionOrderStatus.UNKNOWN_SUBMISSION_STATE: frozenset(
        {
            ExecutionOrderStatus.ACKNOWLEDGED,
            ExecutionOrderStatus.REJECTED,
            ExecutionOrderStatus.NOT_ACCEPTED_CONFIRMED,
        }
    ),
    ExecutionOrderStatus.ACKNOWLEDGED: frozenset(
        {
            ExecutionOrderStatus.WORKING,
            ExecutionOrderStatus.PARTIALLY_FILLED,
            ExecutionOrderStatus.FILLED,
            ExecutionOrderStatus.REJECTED,
            # revision 3.2: an urgent cancel must not be forced to wait for
            # a separate WORKING observation that may not have arrived yet
            # -- ACKNOWLEDGED already means the order is live at the
            # broker (broker_order_id known).
            ExecutionOrderStatus.CANCEL_PENDING,
        }
    ),
    ExecutionOrderStatus.WORKING: frozenset(
        {
            ExecutionOrderStatus.PARTIALLY_FILLED,
            ExecutionOrderStatus.FILLED,
            ExecutionOrderStatus.REJECTED,
            ExecutionOrderStatus.EXPIRED,
            ExecutionOrderStatus.CANCEL_PENDING,
        }
    ),
    ExecutionOrderStatus.PARTIALLY_FILLED: frozenset(
        {
            ExecutionOrderStatus.FILLED,
            ExecutionOrderStatus.EXPIRED,
            ExecutionOrderStatus.CANCEL_PENDING,
        }
    ),
    ExecutionOrderStatus.CANCEL_PENDING: frozenset(
        {
            ExecutionOrderStatus.CANCELLED,
            ExecutionOrderStatus.FILLED,
            ExecutionOrderStatus.PARTIALLY_FILLED,
            # revision 3.2: the broker can explicitly reject the cancel
            # request itself (the order simply keeps working), or a
            # time-in-force expiry can race the cancel -- neither
            # previously had a valid outcome in this table.
            ExecutionOrderStatus.WORKING,
            ExecutionOrderStatus.EXPIRED,
        }
    ),
}


class InvalidExecutionOrderTransitionError(RuntimeError):
    """Raised by :func:`validate_status_transition` -- per A3, an invalid
    transition raises; it must never silently overwrite the stored status."""


def allowed_status_transitions(current: ExecutionOrderStatus) -> FrozenSet[ExecutionOrderStatus]:
    return _ALLOWED_STATUS_TRANSITIONS.get(current, frozenset())


def validate_status_transition(current: ExecutionOrderStatus, target: ExecutionOrderStatus) -> None:
    if target not in allowed_status_transitions(current):
        raise InvalidExecutionOrderTransitionError(
            f"ExecutionOrderStatus cannot transition {current.value} -> {target.value}"
        )


class OrderOrigin(str, Enum):
    APPLICATION = "APPLICATION"  # created via the normal submission flow
    USER_ADOPTED = "USER_ADOPTED"  # created by adopting a DiscoveredExternalOrder


class BrokerIdentityStatus(str, Enum):
    NOT_ASSIGNED = "NOT_ASSIGNED"  # no broker call made yet (PREPARED)
    AMBIGUOUS = "AMBIGUOUS"  # broker call made, outcome unknown
    EXACT = "EXACT"  # broker_order_id confirmed
    # revision 3.2: the confirmed-negative counterpart to EXACT. AMBIGUOUS
    # means "outcome unknown"; once REJECTED/NOT_ACCEPTED_CONFIRMED is
    # reached with no broker_order_id ever assigned, there is no longer
    # anything unknown -- staying AMBIGUOUS would be stale and wrong.
    NO_BROKER_ORDER_CONFIRMED = "NO_BROKER_ORDER_CONFIRMED"


class AdoptedOrderPermission(str, Enum):
    """(revision 3.2) The specific actions an adoption UI actually granted
    for a ``USER_ADOPTED`` :class:`ExecutionOrderRecord` -- ``origin ==
    USER_ADOPTED`` alone must never imply blanket authority over an order
    the application never submitted. Empty/unused for ``origin ==
    APPLICATION`` records, which already have full authority over what
    they themselves created."""

    LINK_TO_CARD = "LINK_TO_CARD"
    CANCEL = "CANCEL"
    REPLACE = "REPLACE"


# Reaching either of these means a broker call was actually made and its
# outcome isn't known yet -- per the "expected combinations" table,
# broker_identity_status should read AMBIGUOUS by the time an
# origin=APPLICATION record gets here, with no new evidence required to
# know that much.
_STATUSES_IMPLYING_AMBIGUOUS_IDENTITY: FrozenSet[ExecutionOrderStatus] = frozenset(
    {ExecutionOrderStatus.SUBMITTING, ExecutionOrderStatus.UNKNOWN_SUBMISSION_STATE}
)

# ACKNOWLEDGED is the sole gateway into WORKING/PARTIALLY_FILLED/FILLED/
# CANCEL_PENDING/CANCELLED (see the transition table above) -- enforcing
# exact identity at this one transition point transitively covers that
# whole subgraph (revision 3.2: this is now a hard requirement, not merely
# a documented expectation the caller could forget to satisfy).
_STATUSES_REQUIRING_EXACT_IDENTITY: FrozenSet[ExecutionOrderStatus] = frozenset(
    {ExecutionOrderStatus.ACKNOWLEDGED}
)

# A confirmed-negative outcome reached while identity was still merely
# AMBIGUOUS (never EXACT) means no broker order ever existed -- distinct
# from a *late* REJECTED reached from ACKNOWLEDGED/WORKING, where a real
# broker_order_id was already confirmed and identity correctly stays EXACT
# (the order existed and is now terminal, which is different from having
# never existed at all) (revision 3.2).
_STATUSES_IMPLYING_NO_BROKER_ORDER_CONFIRMED: FrozenSet[ExecutionOrderStatus] = frozenset(
    {ExecutionOrderStatus.REJECTED, ExecutionOrderStatus.NOT_ACCEPTED_CONFIRMED}
)


def apply_status_transition(
    record: "ExecutionOrderRecord",
    target: ExecutionOrderStatus,
    *,
    broker_order_id: Optional[str] = None,
) -> None:
    """Validates and applies a status transition in one step -- the normal
    way callers should mutate ``record.status``, so an invalid transition
    is caught at the point of the attempted mutation rather than
    discovered later by an inconsistent record.

    - ``target`` in (``SUBMITTING``, ``UNKNOWN_SUBMISSION_STATE``) with
      identity still ``NOT_ASSIGNED`` promotes ``broker_identity_status``
      to ``AMBIGUOUS`` automatically -- the status alone already implies
      that much.
    - ``target == ACKNOWLEDGED`` **requires** exact identity: either
      ``broker_order_id`` is supplied here (routed through
      :func:`mark_broker_identity_exact`), or ``broker_identity_status``
      must already be ``EXACT`` -- otherwise this raises (revision 3.2).
      ``EXACT`` is never inferred from the status transition alone.
    - ``target`` in (``REJECTED``, ``NOT_ACCEPTED_CONFIRMED``) reached
      while identity was still ``AMBIGUOUS`` demotes it to the confirmed-
      negative ``NO_BROKER_ORDER_CONFIRMED`` (revision 3.2) -- a *late*
      rejection reached with identity already ``EXACT`` is left ``EXACT``,
      since a real broker order did exist.
    """
    validate_status_transition(record.status, target)

    if target in _STATUSES_REQUIRING_EXACT_IDENTITY:
        if broker_order_id is not None:
            mark_broker_identity_exact(record, broker_order_id)
        if record.broker_identity_status != BrokerIdentityStatus.EXACT:
            raise ValueError(
                f"Cannot transition to {target.value} without a confirmed broker_order_id "
                "-- pass broker_order_id= or ensure broker_identity_status is already EXACT"
            )
    elif broker_order_id is not None:
        mark_broker_identity_exact(record, broker_order_id)

    if (
        target in _STATUSES_IMPLYING_AMBIGUOUS_IDENTITY
        and record.broker_identity_status == BrokerIdentityStatus.NOT_ASSIGNED
    ):
        record.broker_identity_status = BrokerIdentityStatus.AMBIGUOUS

    if (
        target in _STATUSES_IMPLYING_NO_BROKER_ORDER_CONFIRMED
        and record.broker_identity_status == BrokerIdentityStatus.AMBIGUOUS
    ):
        record.broker_identity_status = BrokerIdentityStatus.NO_BROKER_ORDER_CONFIRMED

    now = utc_now_iso()
    if target == ExecutionOrderStatus.SUBMITTING:
        record.submission_started_at = now
    if target == ExecutionOrderStatus.ACKNOWLEDGED:
        record.acknowledged_at = now

    record.status = target


@dataclass
class ExecutionOrderRecord:
    """The durable local record of one broker order the application itself
    submitted (or explicitly adopted). See the module docstring for the
    ``origin``/``broker_identity_status`` split and
    :mod:`src.core.order_recovery_state` for ``recovery_state``.
    """

    environment: str
    account_no: str
    symbol: str
    side: OrderSide
    intent: OrderIntent
    client_order_id: str

    broker_order_id: str = ""
    attempt_group_id: str = ""
    attempt_number: int = 1

    submitted_quantity: int = 0
    submitted_limit_price: float = 0.0
    exchange: str = ""
    execution_policy: str = REGULAR_LIMIT_EXECUTION
    # revision 3.2: three separate timestamps, not one. prepared_at is when
    # this record was first constructed (PREPARED); it is NOT proof a
    # broker call ever actually started -- that is submission_started_at,
    # set atomically with the SUBMITTING transition (apply_status_transition
    # above). A4a's candidate-fingerprint matching must use
    # submission_started_at, not prepared_at, for its submission time
    # window.
    prepared_at: str = field(default_factory=utc_now_iso)
    submission_started_at: Optional[str] = None
    acknowledged_at: Optional[str] = None
    market_session_date: Optional[str] = None

    owner_device_id: str = ""
    lease_token: str = ""
    lease_epoch: int = 0

    status: ExecutionOrderStatus = ExecutionOrderStatus.PREPARED
    filled_quantity: int = 0
    remaining_quantity: int = 0
    average_fill_price: float = 0.0

    cancel_requested_at: Optional[str] = None
    last_broker_seen_at: Optional[str] = None
    last_reconciled_at: Optional[str] = None

    origin: OrderOrigin = OrderOrigin.APPLICATION
    broker_identity_status: BrokerIdentityStatus = BrokerIdentityStatus.NOT_ASSIGNED
    recovery_state: OrderRecoveryState = OrderRecoveryState.NONE

    # (revision 3.2) The exact actions a USER_ADOPTED record's adoption
    # granted -- see AdoptedOrderPermission. Always empty for
    # origin=APPLICATION records (unused; the application has full
    # authority over its own submissions already).
    adoption_permissions: FrozenSet[AdoptedOrderPermission] = field(default_factory=frozenset)

    # Set only by replace_order's composed cancel-then-new-record operation
    # (see docs/kanban_production_readiness.md, "replace_order is not a
    # first-class status") and by adopt_external_order respectively.
    replaces_execution_order_id: str = ""
    adopted_from_external_order_id: str = ""

    capital_reservation_id: str = ""
    raw_submission_hash: str = ""
    version: int = 1

    def __post_init__(self) -> None:
        self.environment = str(self.environment or "").upper()
        self.account_no = str(self.account_no or "")
        self.symbol = str(self.symbol or "").upper()
        self.side = _strict_enum(self.side, OrderSide)
        self.intent = _strict_enum(self.intent, OrderIntent)
        self.client_order_id = str(self.client_order_id or "").strip()
        if not self.client_order_id:
            raise ValueError("ExecutionOrderRecord requires a client_order_id")
        self.broker_order_id = str(self.broker_order_id or "")
        self.attempt_number = int(self.attempt_number or 1)
        self.submitted_quantity = int(self.submitted_quantity or 0)
        self.submitted_limit_price = float(self.submitted_limit_price or 0.0)
        self.exchange = str(self.exchange or "").upper()
        self.execution_policy = str(self.execution_policy or REGULAR_LIMIT_EXECUTION).strip().upper()
        self.owner_device_id = str(self.owner_device_id or "")
        self.lease_token = str(self.lease_token or "")
        self.lease_epoch = int(self.lease_epoch or 0)
        self.status = _strict_enum(self.status, ExecutionOrderStatus)
        self.filled_quantity = int(self.filled_quantity or 0)
        self.remaining_quantity = int(self.remaining_quantity or 0)
        self.average_fill_price = float(self.average_fill_price or 0.0)
        self.origin = _strict_enum(self.origin, OrderOrigin)
        self.broker_identity_status = _strict_enum(self.broker_identity_status, BrokerIdentityStatus)
        self.recovery_state = _strict_enum(self.recovery_state, OrderRecoveryState)
        self.adoption_permissions = frozenset(
            _strict_enum(perm, AdoptedOrderPermission) for perm in (self.adoption_permissions or ())
        )
        self.version = int(self.version or 1)
        # A3's own rule, enforced at construction too (not just via
        # mark_broker_identity_exact): broker_identity_status must never be
        # EXACT without a confirmed broker_order_id.
        if self.broker_identity_status == BrokerIdentityStatus.EXACT and not self.broker_order_id:
            raise ValueError(
                "broker_identity_status cannot be EXACT without a confirmed broker_order_id"
            )


def mark_broker_identity_exact(record: ExecutionOrderRecord, broker_order_id: str) -> None:
    """The *only* sanctioned way to set ``broker_identity_status=EXACT``
    (A3: "``broker_identity_status`` only ever reaches ``EXACT`` alongside
    a confirmed ``broker_order_id`` -- never inferred from ``origin``
    alone"). Refuses without a real, non-empty ``broker_order_id``.

    Idempotent for the *same* ID (a later reconfirmation is harmless); a
    different ID once already ``EXACT`` is a contradiction and raises,
    never silently overwrites (revision 3.2).
    """
    broker_order_id = str(broker_order_id or "").strip()
    if not broker_order_id:
        raise ValueError("broker_identity_status cannot become EXACT without a confirmed broker_order_id")
    if (
        record.broker_identity_status == BrokerIdentityStatus.EXACT
        and record.broker_order_id
        and record.broker_order_id != broker_order_id
    ):
        raise ValueError(
            f"Contradiction: broker identity already EXACT as {record.broker_order_id!r}, "
            f"cannot reassign to {broker_order_id!r}"
        )
    record.broker_order_id = broker_order_id
    record.broker_identity_status = BrokerIdentityStatus.EXACT


# Statuses with an outbound edge to CANCEL_PENDING in the transition table
# above -- the only statuses a cancel command can actually target.
_CANCELLABLE_STATUSES: FrozenSet[ExecutionOrderStatus] = frozenset(
    status
    for status, targets in _ALLOWED_STATUS_TRANSITIONS.items()
    if ExecutionOrderStatus.CANCEL_PENDING in targets
)

# (revision 3.2) An explicit allow-list, not a `!= BROKER_IDENTITY_UNCERTAIN`
# deny-list -- the deny-list wrongly permitted DISCOVERING, MANUAL_
# INTERVENTION_REQUIRED, and an already-in-flight cancel
# (CANCEL_REQUESTED/AWAITING_CANCEL_CONFIRMATION) to accept a second,
# duplicate cancel command. CANCEL_REQUIRED is included because that state
# specifically represents "a cancel decision has just been made, about to
# be requested" -- the same cancel flow this predicate is gating, not a
# competing one.
_CANCEL_ELIGIBLE_RECOVERY_STATES: FrozenSet[OrderRecoveryState] = frozenset(
    {OrderRecoveryState.NONE, OrderRecoveryState.CANCEL_REQUIRED}
)


def is_cancellable(record: ExecutionOrderRecord) -> bool:
    """B2/B3's cancel-gate identity check, as a reusable predicate: exact
    broker identity, application/user-adopted origin (with, for
    ``USER_ADOPTED``, the adoption having actually granted ``CANCEL`` --
    revision 3.2), a recovery state on the explicit cancel-eligible
    allow-list, and a status that can actually reach ``CANCEL_PENDING`` --
    never ``origin`` alone (revision 3.1's central correction)."""
    if record.origin not in (OrderOrigin.APPLICATION, OrderOrigin.USER_ADOPTED):
        return False
    if (
        record.origin == OrderOrigin.USER_ADOPTED
        and AdoptedOrderPermission.CANCEL not in record.adoption_permissions
    ):
        return False
    return (
        record.broker_identity_status == BrokerIdentityStatus.EXACT
        and bool(record.broker_order_id)
        and record.recovery_state in _CANCEL_ELIGIBLE_RECOVERY_STATES
        and record.status in _CANCELLABLE_STATUSES
    )


def _strict_enum(value: Any, enum_cls: type) -> Enum:
    """Fail-closed enum coercion for this record's own authoritative
    fields (revision 3.2) -- unlike a broker response snapshot (where an
    unrecognized value defaulting to e.g. ``UNKNOWN`` is acceptable), a
    corrupted or invalid value read back for one of *this* record's own
    fields must never be silently reinterpreted as something else; it must
    raise so the corruption is caught, not masked.
    """
    if isinstance(value, enum_cls):
        return value
    try:
        return enum_cls(str(value).upper())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid {enum_cls.__name__} value: {value!r}") from exc
