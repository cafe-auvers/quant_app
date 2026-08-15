"""Durable local identity for every broker order the application submits.

``docs/kanban_production_readiness.md``, Workstream 2 (A1-A3, A6), signed
off in that document's revision 3.1. This is the fix for INV-1 ("every
broker order submitted by the application has a durable local identity")
-- prior recovery code repeatedly had to *infer* whether a discovered
broker order belonged to this application from account+symbol+side+
quantity+price, because no durable "we submitted this exact order" record
survived a crash between broker acceptance and local persistence. An
:class:`ExecutionOrderRecord` is that record, written atomically (with its
capital reservation and command) *before* the broker is ever called --
see :func:`~src.services.execution_command_gateway` (Workstream 3) for the
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
  ``broker_order_id``.

A broker order discovered with *no* matching ``ExecutionOrderRecord`` at
all is a fundamentally different object -- see
:class:`~src.core.discovered_external_order.DiscoveredExternalOrder`, which
is never created here and never becomes one of these except through an
explicit, audited adoption.
"""
from __future__ import annotations

import datetime as dt
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


# Reaching either of these means a broker call was actually made and its
# outcome isn't known yet -- per the "expected combinations" table,
# broker_identity_status should read AMBIGUOUS by the time an
# origin=APPLICATION record gets here, with no new evidence required to
# know that much.
_STATUSES_IMPLYING_AMBIGUOUS_IDENTITY: FrozenSet[ExecutionOrderStatus] = frozenset(
    {ExecutionOrderStatus.SUBMITTING, ExecutionOrderStatus.UNKNOWN_SUBMISSION_STATE}
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

    When ``target`` is ``SUBMITTING``/``UNKNOWN_SUBMISSION_STATE`` and
    identity is still ``NOT_ASSIGNED``, ``broker_identity_status`` is
    promoted to ``AMBIGUOUS`` automatically -- the status alone already
    implies that much, no new evidence needed. Reaching ``ACKNOWLEDGED``
    (or any later status) with exact identity confirmed requires passing
    ``broker_order_id`` here (routed through
    :func:`mark_broker_identity_exact`) -- ``EXACT`` is never inferred
    from the status transition alone (A3's own rule).
    """
    validate_status_transition(record.status, target)
    if (
        target in _STATUSES_IMPLYING_AMBIGUOUS_IDENTITY
        and record.broker_identity_status == BrokerIdentityStatus.NOT_ASSIGNED
    ):
        record.broker_identity_status = BrokerIdentityStatus.AMBIGUOUS
    if broker_order_id is not None:
        mark_broker_identity_exact(record, broker_order_id)
    record.status = target


class OrderOrigin(str, Enum):
    APPLICATION = "APPLICATION"  # created via the normal submission flow
    USER_ADOPTED = "USER_ADOPTED"  # created by adopting a DiscoveredExternalOrder


class BrokerIdentityStatus(str, Enum):
    NOT_ASSIGNED = "NOT_ASSIGNED"  # no broker call made yet (PREPARED)
    AMBIGUOUS = "AMBIGUOUS"  # broker call made, outcome unknown
    EXACT = "EXACT"  # broker_order_id confirmed


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
    submitted_at: str = field(default_factory=utc_now_iso)
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
        self.side = _coerce_enum(self.side, OrderSide, OrderSide.BUY)
        self.intent = _coerce_enum(self.intent, OrderIntent, OrderIntent.UNKNOWN)
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
        self.status = _coerce_enum(self.status, ExecutionOrderStatus, ExecutionOrderStatus.PREPARED)
        self.filled_quantity = int(self.filled_quantity or 0)
        self.remaining_quantity = int(self.remaining_quantity or 0)
        self.average_fill_price = float(self.average_fill_price or 0.0)
        self.origin = _coerce_enum(self.origin, OrderOrigin, OrderOrigin.APPLICATION)
        self.broker_identity_status = _coerce_enum(
            self.broker_identity_status, BrokerIdentityStatus, BrokerIdentityStatus.NOT_ASSIGNED
        )
        self.recovery_state = _coerce_enum(
            self.recovery_state, OrderRecoveryState, OrderRecoveryState.NONE
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
    """
    broker_order_id = str(broker_order_id or "").strip()
    if not broker_order_id:
        raise ValueError("broker_identity_status cannot become EXACT without a confirmed broker_order_id")
    record.broker_order_id = broker_order_id
    record.broker_identity_status = BrokerIdentityStatus.EXACT


# Statuses with an outbound edge to CANCEL_PENDING in the transition table
# above -- the only statuses a cancel command can actually target.
_CANCELLABLE_STATUSES: FrozenSet[ExecutionOrderStatus] = frozenset(
    status
    for status, targets in _ALLOWED_STATUS_TRANSITIONS.items()
    if ExecutionOrderStatus.CANCEL_PENDING in targets
)


def is_cancellable(record: ExecutionOrderRecord) -> bool:
    """B2/B3's cancel-gate identity check, as a reusable predicate: exact
    broker identity, application/user-adopted origin, non-ambiguous
    recovery, and a status that can actually reach ``CANCEL_PENDING`` --
    never ``origin`` alone (revision 3.1's central correction)."""
    return (
        record.origin in (OrderOrigin.APPLICATION, OrderOrigin.USER_ADOPTED)
        and record.broker_identity_status == BrokerIdentityStatus.EXACT
        and bool(record.broker_order_id)
        and record.recovery_state != OrderRecoveryState.OWNERSHIP_UNCERTAIN
        and record.status in _CANCELLABLE_STATUSES
    )


def _coerce_enum(value: Any, enum_cls: type, default: Enum) -> Enum:
    if isinstance(value, enum_cls):
        return value
    try:
        return enum_cls(str(value).upper())
    except (TypeError, ValueError):
        return default
