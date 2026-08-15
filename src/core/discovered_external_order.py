"""``DiscoveredExternalOrder`` -- a broker order found at the broker with
**no** matching :class:`~src.core.execution_order_record.ExecutionOrderRecord`
at all (A4b). This is an *ownership* question, not a status question -- the
application has no record of ever submitting it. It may be a manual order,
a legacy-engine order, another application, or a prior database generation.

``docs/kanban_production_readiness.md``, Workstream 2 (A4b), signed off in
that document's revision 3.1. INV-22: never attached to a card, cancelled,
replaced, or capital-reserved-against automatically; alert-and-display-only
until a human explicitly adopts it, and that adoption is recorded as
adoption, not fabricated as original application ownership.

This is deliberately **not** an :class:`ExecutionOrderRecord` subtype or
variant -- keeping it a wholly separate record type keeps INV-1 ("every
order *submitted by the application*") literally true, and it does not
share :class:`~src.core.order_recovery_state.OrderRecoveryState` either,
since that state machine describes trust in an *own* order's status, which
has no meaning for an order the application never submitted. It has its own,
much simpler lifecycle: :class:`ExternalOrderDisposition`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, FrozenSet, Optional
from uuid import uuid4

from src.core.execution_order_record import (
    BrokerIdentityStatus,
    ExecutionOrderRecord,
    ExecutionOrderStatus,
    OrderOrigin,
)
from src.core.order_recovery_state import OrderRecoveryState
from src.core.order_state import OrderIntent, OrderSide, generate_client_order_id, utc_now_iso


class ExternalOrderDisposition(str, Enum):
    DISCOVERED_UNOWNED = "DISCOVERED_UNOWNED"  # default, on creation
    USER_ADOPTED = "USER_ADOPTED"  # explicit user action taken
    DISMISSED_TERMINAL = "DISMISSED_TERMINAL"  # broker confirms it's gone, nothing was ever adopted


_ALLOWED_DISPOSITION_TRANSITIONS: Dict[
    ExternalOrderDisposition, FrozenSet[ExternalOrderDisposition]
] = {
    ExternalOrderDisposition.DISCOVERED_UNOWNED: frozenset(
        {ExternalOrderDisposition.USER_ADOPTED, ExternalOrderDisposition.DISMISSED_TERMINAL}
    ),
}


class InvalidExternalOrderDispositionTransitionError(RuntimeError):
    """Raised by :func:`validate_disposition_transition` -- an invalid
    transition raises; it must never silently overwrite the stored
    disposition (same "derived, never authoritative" rule INV-7 already
    applies to ``OrderRecoveryState``)."""


def allowed_disposition_transitions(
    current: ExternalOrderDisposition,
) -> FrozenSet[ExternalOrderDisposition]:
    return _ALLOWED_DISPOSITION_TRANSITIONS.get(current, frozenset())


def validate_disposition_transition(
    current: ExternalOrderDisposition, target: ExternalOrderDisposition
) -> None:
    if target not in allowed_disposition_transitions(current):
        raise InvalidExternalOrderDispositionTransitionError(
            f"ExternalOrderDisposition cannot transition {current.value} -> {target.value}"
        )


def is_unreconciled_external_order(disposition: ExternalOrderDisposition) -> bool:
    """The ``UNRECONCILED_BROKER_ORDER``-equivalent alert for a discovered
    external order: derived from ``disposition``, never itself
    authoritative -- the same rule INV-7 applies to ``OrderRecoveryState``,
    extended here (revision 3.1) to this record type too.
    """
    return disposition == ExternalOrderDisposition.DISCOVERED_UNOWNED


@dataclass
class DiscoveredExternalOrder:
    """A snapshot of a broker order nothing local claims (A4b). Immutable
    aside from its own ``disposition``/``adopted_at``/``adopted_by`` fields
    -- it is the permanent audit-trail record of what was actually
    discovered and when, even after adoption (see :func:`adopt_external_order`,
    which creates a *separate*, new :class:`ExecutionOrderRecord` rather
    than mutating this one into something else).
    """

    environment: str
    account_no: str
    symbol: str
    side: OrderSide
    broker_order_id: str

    quantity_requested: int = 0
    filled_quantity: int = 0
    limit_price: float = 0.0
    broker_status: ExecutionOrderStatus = ExecutionOrderStatus.WORKING

    external_order_id: str = field(default_factory=lambda: uuid4().hex)
    discovered_at: str = field(default_factory=utc_now_iso)
    disposition: ExternalOrderDisposition = ExternalOrderDisposition.DISCOVERED_UNOWNED
    adopted_at: Optional[str] = None
    adopted_by: str = ""

    raw_response: Dict[str, Any] = field(default_factory=dict)
    version: int = 1

    def __post_init__(self) -> None:
        self.environment = str(self.environment or "").upper()
        self.account_no = str(self.account_no or "")
        self.symbol = str(self.symbol or "").upper()
        self.side = _coerce_enum(self.side, OrderSide, OrderSide.BUY)
        self.broker_order_id = str(self.broker_order_id or "").strip()
        if not self.broker_order_id:
            # A4b's entire premise is a *real* broker order -- one with no
            # exact broker identity at all is not a DiscoveredExternalOrder,
            # it is (per the reconciliation classification precedence) not
            # even resolvable to this record type.
            raise ValueError("DiscoveredExternalOrder requires a broker_order_id")
        self.quantity_requested = int(self.quantity_requested or 0)
        self.filled_quantity = int(self.filled_quantity or 0)
        self.limit_price = float(self.limit_price or 0.0)
        self.broker_status = _coerce_enum(
            self.broker_status, ExecutionOrderStatus, ExecutionOrderStatus.WORKING
        )
        self.disposition = _coerce_enum(
            self.disposition, ExternalOrderDisposition, ExternalOrderDisposition.DISCOVERED_UNOWNED
        )
        self.version = int(self.version or 1)
        if not isinstance(self.raw_response, dict):
            self.raw_response = {"raw": self.raw_response}


def adopt_external_order(
    external_order: DiscoveredExternalOrder,
    *,
    adopted_by: str,
    owner_device_id: str = "",
) -> ExecutionOrderRecord:
    """The *only* sanctioned way to turn a :class:`DiscoveredExternalOrder`
    into something the application can manage (L4's "Adopt" action;
    INV-22). Creates a **new, separate** :class:`ExecutionOrderRecord` --
    ``external_order`` itself is never mutated into one; its own
    ``disposition`` becomes ``USER_ADOPTED`` and it stays as the audit
    trail's permanent record of what was actually discovered, never
    rewritten to look application-originated.

    Raises if ``external_order`` has already been adopted or dismissed
    (adoption is a single, explicit, one-time action) or if
    ``adopted_by`` is blank (adoption must always be attributable).
    """
    adopted_by = str(adopted_by or "").strip()
    if not adopted_by:
        raise ValueError("adopt_external_order requires a non-blank adopted_by (audit attribution)")

    validate_disposition_transition(external_order.disposition, ExternalOrderDisposition.USER_ADOPTED)

    record = ExecutionOrderRecord(
        environment=external_order.environment,
        account_no=external_order.account_no,
        symbol=external_order.symbol,
        side=external_order.side,
        intent=OrderIntent.UNKNOWN,
        client_order_id=generate_client_order_id(
            external_order.environment,
            external_order.account_no,
            external_order.symbol,
            external_order.side,
            OrderIntent.UNKNOWN,
        ),
        broker_order_id=external_order.broker_order_id,
        submitted_quantity=external_order.quantity_requested,
        submitted_limit_price=external_order.limit_price,
        status=external_order.broker_status,
        filled_quantity=external_order.filled_quantity,
        origin=OrderOrigin.USER_ADOPTED,
        broker_identity_status=BrokerIdentityStatus.EXACT,
        recovery_state=OrderRecoveryState.NONE,
        owner_device_id=owner_device_id,
        adopted_from_external_order_id=external_order.external_order_id,
    )

    # Only after the new record is built successfully -- an adoption that
    # fails to construct the new record must not leave the external order
    # half-adopted.
    external_order.disposition = ExternalOrderDisposition.USER_ADOPTED
    external_order.adopted_at = utc_now_iso()
    external_order.adopted_by = adopted_by
    return record


def _coerce_enum(value: Any, enum_cls: type, default: Enum) -> Enum:
    if isinstance(value, enum_cls):
        return value
    try:
        return enum_cls(str(value).upper())
    except (TypeError, ValueError):
        return default
