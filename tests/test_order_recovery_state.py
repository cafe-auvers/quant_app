"""Tests for src.core.order_recovery_state.

docs/kanban_production_readiness.md, Workstream 2, A3, revision 3.1.
"""
from __future__ import annotations

import pytest

from src.core.order_recovery_state import (
    InvalidOrderRecoveryTransitionError,
    OrderRecoveryState,
    TERMINAL_RECOVERY_STATES,
    allowed_recovery_transitions,
    is_unreconciled_broker_order,
    validate_recovery_transition,
)

_VALID_TRANSITION_PAIRS = [
    (frm, to) for frm in OrderRecoveryState for to in allowed_recovery_transitions(frm)
]


@pytest.mark.parametrize("frm,to", _VALID_TRANSITION_PAIRS)
def test_every_encoded_valid_transition_is_accepted(frm, to):
    validate_recovery_transition(frm, to)  # must not raise


@pytest.mark.parametrize(
    "frm,to",
    [
        (OrderRecoveryState.NONE, OrderRecoveryState.DISCOVERING),
        (OrderRecoveryState.DISCOVERING, OrderRecoveryState.NONE),
        (OrderRecoveryState.DISCOVERING, OrderRecoveryState.OWNERSHIP_UNCERTAIN),
        (OrderRecoveryState.OWNERSHIP_UNCERTAIN, OrderRecoveryState.CANCEL_REQUIRED),
        (OrderRecoveryState.CANCEL_REQUIRED, OrderRecoveryState.CANCEL_REQUESTED),
        (OrderRecoveryState.CANCEL_REQUESTED, OrderRecoveryState.AWAITING_CANCEL_CONFIRMATION),
        (OrderRecoveryState.AWAITING_CANCEL_CONFIRMATION, OrderRecoveryState.TERMINAL_RECONCILED),
    ],
)
def test_every_documented_transition_table_row_matches_the_written_contract(frm, to):
    validate_recovery_transition(frm, to)


@pytest.mark.parametrize(
    "frm",
    [
        OrderRecoveryState.NONE,
        OrderRecoveryState.DISCOVERING,
        OrderRecoveryState.OWNERSHIP_UNCERTAIN,
        OrderRecoveryState.CANCEL_REQUIRED,
        OrderRecoveryState.CANCEL_REQUESTED,
        OrderRecoveryState.AWAITING_CANCEL_CONFIRMATION,
    ],
)
def test_any_non_terminal_state_can_escalate_to_manual_intervention_required(frm):
    """The transition table's 'any -> MANUAL_INTERVENTION_REQUIRED' row --
    an unrecoverable contradiction at any stage."""
    validate_recovery_transition(frm, OrderRecoveryState.MANUAL_INTERVENTION_REQUIRED)


@pytest.mark.parametrize(
    "frm,to",
    [
        (OrderRecoveryState.NONE, OrderRecoveryState.OWNERSHIP_UNCERTAIN),
        (OrderRecoveryState.NONE, OrderRecoveryState.TERMINAL_RECONCILED),
        (OrderRecoveryState.OWNERSHIP_UNCERTAIN, OrderRecoveryState.NONE),
        (OrderRecoveryState.OWNERSHIP_UNCERTAIN, OrderRecoveryState.TERMINAL_RECONCILED),
        (OrderRecoveryState.CANCEL_REQUIRED, OrderRecoveryState.AWAITING_CANCEL_CONFIRMATION),
    ],
)
def test_undocumented_transitions_are_rejected(frm, to):
    with pytest.raises(InvalidOrderRecoveryTransitionError):
        validate_recovery_transition(frm, to)


@pytest.mark.parametrize("terminal_state", sorted(TERMINAL_RECOVERY_STATES, key=lambda s: s.value))
def test_terminal_states_have_no_outbound_transitions(terminal_state):
    for candidate in OrderRecoveryState:
        if candidate == terminal_state:
            continue
        with pytest.raises(InvalidOrderRecoveryTransitionError):
            validate_recovery_transition(terminal_state, candidate)


# --- UNRECONCILED_BROKER_ORDER derivation (INV-7) ----------------------------


@pytest.mark.parametrize(
    "state",
    [
        OrderRecoveryState.DISCOVERING,
        OrderRecoveryState.OWNERSHIP_UNCERTAIN,
        OrderRecoveryState.CANCEL_REQUIRED,
        OrderRecoveryState.CANCEL_REQUESTED,
        OrderRecoveryState.AWAITING_CANCEL_CONFIRMATION,
        OrderRecoveryState.MANUAL_INTERVENTION_REQUIRED,
    ],
)
def test_unreconciled_broker_order_warning_is_derived_not_authoritative(state):
    """Every non-NONE, non-TERMINAL_RECONCILED state means the warning is
    present -- it is *derived* from recovery_state, never a separately
    settable flag (INV-7)."""
    assert is_unreconciled_broker_order(state) is True


@pytest.mark.parametrize("state", [OrderRecoveryState.NONE, OrderRecoveryState.TERMINAL_RECONCILED])
def test_unreconciled_broker_order_warning_absent_for_clean_or_resolved_states(state):
    assert is_unreconciled_broker_order(state) is False
