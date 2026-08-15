"""``OrderRecoveryState`` -- whether the application currently *trusts* its
own view of an :class:`~src.core.execution_order_record.ExecutionOrderRecord`'s
status, independent of what that status value actually is.

``docs/kanban_production_readiness.md``, Workstream 2 (A3/A4a), signed off
in that document's revision 3.1. This is a deliberately separate dimension
from :class:`~src.core.execution_order_record.ExecutionOrderStatus` (the
order's own broker-facing lifecycle) -- an order can be, say, ``WORKING``
(a perfectly normal broker status) while its ``recovery_state`` is
``BROKER_IDENTITY_UNCERTAIN`` (the application isn't sure it's tracking the right
order at all). Conflating the two was never done here, unlike the review
history's earlier ``UNRECONCILED_BROKER_ORDER`` warning, which *was* used as
both a user-facing label and (via string membership checks) the sweep
selector driving reconciliation -- exactly the anti-pattern INV-7 exists to
rule out. ``is_unreconciled_broker_order`` below is the one sanctioned way
to derive that presentation warning; nothing may set it directly.

This module intentionally has no dependency on
:mod:`src.core.execution_order_record` or any other project module -- it is
a leaf, reusable by both ``ExecutionOrderRecord`` (which carries a
``recovery_state`` field) and Workstream 4's account-reconciliation engine
without risking an import cycle.
"""
from __future__ import annotations

from enum import Enum
from typing import FrozenSet


class OrderRecoveryState(str, Enum):
    NONE = "NONE"
    DISCOVERING = "DISCOVERING"
    BROKER_IDENTITY_UNCERTAIN = "BROKER_IDENTITY_UNCERTAIN"
    CANCEL_REQUIRED = "CANCEL_REQUIRED"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    AWAITING_CANCEL_CONFIRMATION = "AWAITING_CANCEL_CONFIRMATION"
    TERMINAL_RECONCILED = "TERMINAL_RECONCILED"
    MANUAL_INTERVENTION_REQUIRED = "MANUAL_INTERVENTION_REQUIRED"


# No further automatic transitions out of either of these (the
# OrderRecoveryState transition table's final row).
TERMINAL_RECOVERY_STATES: FrozenSet[OrderRecoveryState] = frozenset(
    {OrderRecoveryState.TERMINAL_RECONCILED, OrderRecoveryState.MANUAL_INTERVENTION_REQUIRED}
)

# The transition table's specific rows, excluding the "any -> MANUAL_
# INTERVENTION_REQUIRED" row, which applies uniformly to every non-terminal
# state and is added in allowed_recovery_transitions() below rather than
# repeated on every entry here.
_SPECIFIC_TRANSITIONS = {
    OrderRecoveryState.NONE: frozenset({OrderRecoveryState.DISCOVERING}),
    OrderRecoveryState.DISCOVERING: frozenset(
        {OrderRecoveryState.NONE, OrderRecoveryState.BROKER_IDENTITY_UNCERTAIN}
    ),
    OrderRecoveryState.BROKER_IDENTITY_UNCERTAIN: frozenset({OrderRecoveryState.CANCEL_REQUIRED}),
    OrderRecoveryState.CANCEL_REQUIRED: frozenset({OrderRecoveryState.CANCEL_REQUESTED}),
    OrderRecoveryState.CANCEL_REQUESTED: frozenset(
        {OrderRecoveryState.AWAITING_CANCEL_CONFIRMATION}
    ),
    OrderRecoveryState.AWAITING_CANCEL_CONFIRMATION: frozenset(
        {OrderRecoveryState.TERMINAL_RECONCILED}
    ),
}


class InvalidOrderRecoveryTransitionError(RuntimeError):
    """Raised by :func:`validate_recovery_transition` -- per A3, an invalid
    transition raises; it must never silently overwrite the stored state."""


def allowed_recovery_transitions(current: OrderRecoveryState) -> FrozenSet[OrderRecoveryState]:
    """Every state `current` may legally move to next, per the
    ``OrderRecoveryState`` transition table -- including the "any stage"
    escalation to ``MANUAL_INTERVENTION_REQUIRED`` for every non-terminal
    state."""
    allowed = set(_SPECIFIC_TRANSITIONS.get(current, frozenset()))
    if current not in TERMINAL_RECOVERY_STATES:
        allowed.add(OrderRecoveryState.MANUAL_INTERVENTION_REQUIRED)
    return frozenset(allowed)


def validate_recovery_transition(current: OrderRecoveryState, target: OrderRecoveryState) -> None:
    if target not in allowed_recovery_transitions(current):
        raise InvalidOrderRecoveryTransitionError(
            f"OrderRecoveryState cannot transition {current.value} -> {target.value}"
        )


def is_unreconciled_broker_order(recovery_state: OrderRecoveryState) -> bool:
    """The ``UNRECONCILED_BROKER_ORDER`` presentation warning (INV-7):
    *derived* from ``recovery_state``, never itself authoritative and never
    itself the thing a reconciliation sweep should select on -- callers
    that need "does this need more reconciliation work" should test
    ``recovery_state`` directly, not this warning string.
    """
    return recovery_state not in (OrderRecoveryState.NONE, OrderRecoveryState.TERMINAL_RECONCILED)
