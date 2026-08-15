"""Tests for src.core.execution_order_record.

docs/kanban_production_readiness.md, Workstream 2, A1-A3, A6, revisions
3.1 and 3.2.
"""
from __future__ import annotations

import pytest

from src.core.execution_order_record import (
    AdoptedOrderPermission,
    BrokerIdentityStatus,
    ExecutionOrderRecord,
    ExecutionOrderStatus,
    InvalidExecutionOrderTransitionError,
    OrderOrigin,
    TERMINAL_EXECUTION_ORDER_STATUSES,
    allowed_status_transitions,
    apply_status_transition,
    is_cancellable,
    mark_broker_identity_exact,
    validate_status_transition,
)
from src.core.order_recovery_state import OrderRecoveryState
from src.core.order_state import OrderIntent, OrderSide


def _record(**overrides) -> ExecutionOrderRecord:
    fields = dict(
        environment="PROD", account_no="1", symbol="AAPL", side=OrderSide.BUY,
        intent=OrderIntent.ENTRY, client_order_id="CID-1",
    )
    fields.update(overrides)
    return ExecutionOrderRecord(**fields)


def _acknowledged(rec: ExecutionOrderRecord, broker_order_id: str = "B-1") -> ExecutionOrderRecord:
    apply_status_transition(rec, ExecutionOrderStatus.SUBMITTING)
    apply_status_transition(rec, ExecutionOrderStatus.ACKNOWLEDGED, broker_order_id=broker_order_id)
    return rec


# --- Construction / normalization -------------------------------------------


def test_record_requires_a_client_order_id():
    with pytest.raises(ValueError):
        _record(client_order_id="")


def test_record_defaults_to_prepared_application_not_assigned():
    rec = _record()
    assert rec.status == ExecutionOrderStatus.PREPARED
    assert rec.origin == OrderOrigin.APPLICATION
    assert rec.broker_identity_status == BrokerIdentityStatus.NOT_ASSIGNED
    assert rec.recovery_state == OrderRecoveryState.NONE
    assert rec.adoption_permissions == frozenset()


def test_record_normalizes_environment_and_symbol_case():
    rec = _record(environment="prod", symbol="aapl")
    assert rec.environment == "PROD"
    assert rec.symbol == "AAPL"


def test_record_rejects_broker_identity_exact_without_a_broker_order_id_at_construction():
    """A3's own rule enforced even outside mark_broker_identity_exact --
    the invariant holds regardless of how the record was built."""
    with pytest.raises(ValueError):
        _record(broker_identity_status=BrokerIdentityStatus.EXACT, broker_order_id="")


# --- revision 3.2: fail-closed enum coercion --------------------------------


def test_invalid_side_raises_rather_than_silently_defaulting():
    with pytest.raises(ValueError):
        _record(side="NOT_A_REAL_SIDE")


def test_invalid_status_raises_rather_than_silently_defaulting():
    with pytest.raises(ValueError):
        _record(status="NOT_A_REAL_STATUS")


def test_invalid_origin_raises_rather_than_silently_defaulting():
    with pytest.raises(ValueError):
        _record(origin="NOT_A_REAL_ORIGIN")


def test_invalid_recovery_state_raises_rather_than_silently_defaulting():
    with pytest.raises(ValueError):
        _record(recovery_state="NOT_A_REAL_RECOVERY_STATE")


# --- Timestamps (revision 3.2: three separate meanings) ---------------------


def test_prepared_at_is_set_at_construction():
    rec = _record()
    assert rec.prepared_at
    assert rec.submission_started_at is None
    assert rec.acknowledged_at is None


def test_submission_started_at_is_set_only_on_the_submitting_transition():
    rec = _record()
    apply_status_transition(rec, ExecutionOrderStatus.SUBMITTING)
    assert rec.submission_started_at is not None
    assert rec.acknowledged_at is None


def test_acknowledged_at_is_set_only_on_the_acknowledged_transition():
    rec = _record()
    _acknowledged(rec)
    assert rec.acknowledged_at is not None


# --- ExecutionOrderStatus transition table -----------------------------------
# Every FROM/TO pair the transition table lists is a valid transition; this
# is data-driven directly off the encoded table so the test and the
# production adjacency data can never silently drift apart -- see the
# explicit per-pair assertions below for the actual documented rows.


_VALID_TRANSITION_PAIRS = [
    (frm, to) for frm in ExecutionOrderStatus for to in allowed_status_transitions(frm)
]


@pytest.mark.parametrize("frm,to", _VALID_TRANSITION_PAIRS)
def test_every_encoded_valid_transition_is_accepted(frm, to):
    validate_status_transition(frm, to)  # must not raise


@pytest.mark.parametrize(
    "frm,to",
    [
        (ExecutionOrderStatus.PREPARED, ExecutionOrderStatus.SUBMITTING),
        (ExecutionOrderStatus.PREPARED, ExecutionOrderStatus.CANCELLED_LOCALLY),
        (ExecutionOrderStatus.SUBMITTING, ExecutionOrderStatus.ACKNOWLEDGED),
        (ExecutionOrderStatus.SUBMITTING, ExecutionOrderStatus.REJECTED),
        (ExecutionOrderStatus.SUBMITTING, ExecutionOrderStatus.UNKNOWN_SUBMISSION_STATE),
        (ExecutionOrderStatus.UNKNOWN_SUBMISSION_STATE, ExecutionOrderStatus.ACKNOWLEDGED),
        (ExecutionOrderStatus.UNKNOWN_SUBMISSION_STATE, ExecutionOrderStatus.REJECTED),
        (ExecutionOrderStatus.UNKNOWN_SUBMISSION_STATE, ExecutionOrderStatus.NOT_ACCEPTED_CONFIRMED),
        (ExecutionOrderStatus.ACKNOWLEDGED, ExecutionOrderStatus.WORKING),
        (ExecutionOrderStatus.ACKNOWLEDGED, ExecutionOrderStatus.PARTIALLY_FILLED),
        (ExecutionOrderStatus.ACKNOWLEDGED, ExecutionOrderStatus.FILLED),
        (ExecutionOrderStatus.ACKNOWLEDGED, ExecutionOrderStatus.REJECTED),
        (ExecutionOrderStatus.ACKNOWLEDGED, ExecutionOrderStatus.CANCEL_PENDING),  # revision 3.2
        (ExecutionOrderStatus.WORKING, ExecutionOrderStatus.PARTIALLY_FILLED),
        (ExecutionOrderStatus.WORKING, ExecutionOrderStatus.FILLED),
        (ExecutionOrderStatus.WORKING, ExecutionOrderStatus.REJECTED),
        (ExecutionOrderStatus.WORKING, ExecutionOrderStatus.EXPIRED),
        (ExecutionOrderStatus.WORKING, ExecutionOrderStatus.CANCEL_PENDING),
        (ExecutionOrderStatus.PARTIALLY_FILLED, ExecutionOrderStatus.FILLED),
        (ExecutionOrderStatus.PARTIALLY_FILLED, ExecutionOrderStatus.EXPIRED),
        (ExecutionOrderStatus.PARTIALLY_FILLED, ExecutionOrderStatus.CANCEL_PENDING),
        (ExecutionOrderStatus.CANCEL_PENDING, ExecutionOrderStatus.CANCELLED),
        (ExecutionOrderStatus.CANCEL_PENDING, ExecutionOrderStatus.FILLED),
        (ExecutionOrderStatus.CANCEL_PENDING, ExecutionOrderStatus.PARTIALLY_FILLED),
        (ExecutionOrderStatus.CANCEL_PENDING, ExecutionOrderStatus.WORKING),  # revision 3.2
        (ExecutionOrderStatus.CANCEL_PENDING, ExecutionOrderStatus.EXPIRED),  # revision 3.2
    ],
)
def test_every_documented_transition_table_row_matches_the_written_contract(frm, to):
    """Pins the transition table exactly as written in
    docs/kanban_production_readiness.md -- if this test needs to change,
    the document must change first (rule 1: no weakening an invariant to
    make a test pass)."""
    validate_status_transition(frm, to)


@pytest.mark.parametrize(
    "frm,to",
    [
        (ExecutionOrderStatus.PREPARED, ExecutionOrderStatus.ACKNOWLEDGED),
        (ExecutionOrderStatus.SUBMITTING, ExecutionOrderStatus.FILLED),
        (ExecutionOrderStatus.SUBMITTING, ExecutionOrderStatus.CANCELLED_LOCALLY),
        (ExecutionOrderStatus.WORKING, ExecutionOrderStatus.ACKNOWLEDGED),
        (ExecutionOrderStatus.CANCEL_PENDING, ExecutionOrderStatus.UNKNOWN_SUBMISSION_STATE),
        (ExecutionOrderStatus.FILLED, ExecutionOrderStatus.CANCELLED),
    ],
)
def test_undocumented_transitions_are_rejected(frm, to):
    with pytest.raises(InvalidExecutionOrderTransitionError):
        validate_status_transition(frm, to)


@pytest.mark.parametrize("terminal_status", sorted(TERMINAL_EXECUTION_ORDER_STATUSES, key=lambda s: s.value))
def test_terminal_statuses_have_no_outbound_transitions(terminal_status):
    for candidate in ExecutionOrderStatus:
        if candidate == terminal_status:
            continue
        with pytest.raises(InvalidExecutionOrderTransitionError):
            validate_status_transition(terminal_status, candidate)


def test_apply_status_transition_mutates_the_record_on_success():
    rec = _record()
    apply_status_transition(rec, ExecutionOrderStatus.SUBMITTING)
    assert rec.status == ExecutionOrderStatus.SUBMITTING


def test_apply_status_transition_raises_and_does_not_mutate_on_an_invalid_transition():
    rec = _record()
    with pytest.raises(InvalidExecutionOrderTransitionError):
        apply_status_transition(rec, ExecutionOrderStatus.FILLED)
    assert rec.status == ExecutionOrderStatus.PREPARED  # unchanged


# --- revision 3.2: ACKNOWLEDGED requires exact identity, not merely typical -


def test_transitioning_to_acknowledged_without_any_broker_order_id_raises():
    rec = _record()
    apply_status_transition(rec, ExecutionOrderStatus.SUBMITTING)
    with pytest.raises(ValueError):
        apply_status_transition(rec, ExecutionOrderStatus.ACKNOWLEDGED)
    assert rec.status == ExecutionOrderStatus.SUBMITTING  # unchanged -- never left inconsistent


def test_transitioning_to_acknowledged_succeeds_when_identity_already_exact():
    rec = _record()
    apply_status_transition(rec, ExecutionOrderStatus.SUBMITTING)
    mark_broker_identity_exact(rec, "B-1")
    apply_status_transition(rec, ExecutionOrderStatus.ACKNOWLEDGED)
    assert rec.status == ExecutionOrderStatus.ACKNOWLEDGED


# --- REJECTED vs. NOT_ACCEPTED_CONFIRMED (revision 3.1) ----------------------


def test_rejected_and_not_accepted_confirmed_are_both_reachable_but_distinct_from_unknown_submission_state():
    """The two different evidentiary bars from UNKNOWN_SUBMISSION_STATE
    are both real, separate terminal outcomes -- neither implies the
    other."""
    explicit_rejection = _record(client_order_id="CID-A")
    apply_status_transition(explicit_rejection, ExecutionOrderStatus.SUBMITTING)
    apply_status_transition(explicit_rejection, ExecutionOrderStatus.UNKNOWN_SUBMISSION_STATE)
    apply_status_transition(explicit_rejection, ExecutionOrderStatus.REJECTED)
    assert explicit_rejection.status == ExecutionOrderStatus.REJECTED

    inferred_absence = _record(client_order_id="CID-B")
    apply_status_transition(inferred_absence, ExecutionOrderStatus.SUBMITTING)
    apply_status_transition(inferred_absence, ExecutionOrderStatus.UNKNOWN_SUBMISSION_STATE)
    apply_status_transition(inferred_absence, ExecutionOrderStatus.NOT_ACCEPTED_CONFIRMED)
    assert inferred_absence.status == ExecutionOrderStatus.NOT_ACCEPTED_CONFIRMED


# --- BrokerIdentityStatus.NO_BROKER_ORDER_CONFIRMED (revision 3.2) ----------


def test_inferred_non_acceptance_demotes_ambiguous_identity_to_no_broker_order_confirmed():
    rec = _record()
    apply_status_transition(rec, ExecutionOrderStatus.SUBMITTING)
    apply_status_transition(rec, ExecutionOrderStatus.UNKNOWN_SUBMISSION_STATE)
    assert rec.broker_identity_status == BrokerIdentityStatus.AMBIGUOUS
    apply_status_transition(rec, ExecutionOrderStatus.NOT_ACCEPTED_CONFIRMED)
    assert rec.broker_identity_status == BrokerIdentityStatus.NO_BROKER_ORDER_CONFIRMED


def test_explicit_rejection_from_ambiguous_also_demotes_to_no_broker_order_confirmed():
    rec = _record()
    apply_status_transition(rec, ExecutionOrderStatus.SUBMITTING)
    apply_status_transition(rec, ExecutionOrderStatus.UNKNOWN_SUBMISSION_STATE)
    apply_status_transition(rec, ExecutionOrderStatus.REJECTED)
    assert rec.broker_identity_status == BrokerIdentityStatus.NO_BROKER_ORDER_CONFIRMED


def test_a_late_rejection_from_an_already_exact_identity_stays_exact():
    """A rejection reached from ACKNOWLEDGED/WORKING had a real,
    confirmed broker_order_id -- the order existed and is now terminal,
    which is different from having never existed at all."""
    rec = _record()
    _acknowledged(rec, broker_order_id="B-1")
    apply_status_transition(rec, ExecutionOrderStatus.REJECTED)
    assert rec.broker_identity_status == BrokerIdentityStatus.EXACT
    assert rec.broker_order_id == "B-1"


# --- broker_identity_status (revision 3.1: identity, not origin alone) ------


def test_broker_identity_status_never_reaches_exact_without_a_confirmed_broker_order_id():
    rec = _record()
    with pytest.raises(ValueError):
        mark_broker_identity_exact(rec, "")
    with pytest.raises(ValueError):
        mark_broker_identity_exact(rec, "   ")
    assert rec.broker_identity_status == BrokerIdentityStatus.NOT_ASSIGNED


def test_mark_broker_identity_exact_sets_both_fields_together():
    rec = _record()
    mark_broker_identity_exact(rec, "B-42")
    assert rec.broker_identity_status == BrokerIdentityStatus.EXACT
    assert rec.broker_order_id == "B-42"


def test_apply_status_transition_promotes_ambiguous_identity_on_submitting_with_no_new_evidence():
    rec = _record()
    apply_status_transition(rec, ExecutionOrderStatus.SUBMITTING)
    assert rec.broker_identity_status == BrokerIdentityStatus.AMBIGUOUS
    assert rec.broker_order_id == ""  # still no exact id -- just "a call was made"


def test_apply_status_transition_accepts_broker_order_id_to_reach_exact_identity_in_one_step():
    rec = _record()
    apply_status_transition(rec, ExecutionOrderStatus.SUBMITTING)
    apply_status_transition(rec, ExecutionOrderStatus.ACKNOWLEDGED, broker_order_id="B-7")
    assert rec.broker_identity_status == BrokerIdentityStatus.EXACT
    assert rec.broker_order_id == "B-7"


# --- mark_broker_identity_exact contradiction (revision 3.2) ---------------


def test_mark_broker_identity_exact_is_idempotent_for_the_same_id():
    rec = _record()
    mark_broker_identity_exact(rec, "B-1")
    mark_broker_identity_exact(rec, "B-1")  # must not raise
    assert rec.broker_order_id == "B-1"


def test_mark_broker_identity_exact_raises_on_a_contradictory_different_id():
    rec = _record()
    mark_broker_identity_exact(rec, "B-1")
    with pytest.raises(ValueError):
        mark_broker_identity_exact(rec, "B-2")
    assert rec.broker_order_id == "B-1"  # unchanged -- never silently overwritten


# --- Cancellation gate: exact identity required, origin alone is not enough -


def test_is_cancellable_requires_broker_identity_status_exact_not_just_application_origin():
    """revision 3.1's central correction: a PREPARED/SUBMITTING record has
    origin=APPLICATION but is never cancellable until broker identity is
    actually EXACT."""
    prepared = _record()
    assert prepared.origin == OrderOrigin.APPLICATION
    assert not is_cancellable(prepared)

    submitting = _record(client_order_id="CID-2")
    apply_status_transition(submitting, ExecutionOrderStatus.SUBMITTING)
    assert submitting.broker_identity_status == BrokerIdentityStatus.AMBIGUOUS
    assert not is_cancellable(submitting)


def test_is_cancellable_true_once_working_with_exact_identity():
    rec = _record()
    _acknowledged(rec)
    apply_status_transition(rec, ExecutionOrderStatus.WORKING)
    assert is_cancellable(rec)


def test_is_cancellable_false_for_a_terminal_status_even_with_exact_identity():
    rec = _record()
    _acknowledged(rec)
    apply_status_transition(rec, ExecutionOrderStatus.FILLED)
    assert not is_cancellable(rec)


# --- revision 3.2: recovery_state allow-list, not a deny-list --------------


@pytest.mark.parametrize(
    "state",
    [
        OrderRecoveryState.DISCOVERING,
        OrderRecoveryState.MANUAL_INTERVENTION_REQUIRED,
        OrderRecoveryState.CANCEL_REQUESTED,
        OrderRecoveryState.AWAITING_CANCEL_CONFIRMATION,
    ],
)
def test_is_cancellable_false_for_every_recovery_state_off_the_allow_list(state):
    """The old `!= BROKER_IDENTITY_UNCERTAIN` deny-list wrongly permitted
    all of these -- DISCOVERING (mid-resolution), MANUAL_INTERVENTION_REQUIRED
    (an unrecoverable contradiction), and an already-in-flight cancel
    (CANCEL_REQUESTED/AWAITING_CANCEL_CONFIRMATION, which would let a
    second, duplicate cancel command through)."""
    rec = _record()
    _acknowledged(rec)
    apply_status_transition(rec, ExecutionOrderStatus.WORKING)
    rec.recovery_state = state
    assert not is_cancellable(rec)


def test_is_cancellable_true_for_cancel_required_recovery_state():
    """CANCEL_REQUIRED represents "a cancel decision has just been made,
    about to be requested" -- the same cancel flow this predicate gates,
    not a competing one."""
    rec = _record()
    _acknowledged(rec)
    apply_status_transition(rec, ExecutionOrderStatus.WORKING)
    rec.recovery_state = OrderRecoveryState.CANCEL_REQUIRED
    assert is_cancellable(rec)


def test_is_cancellable_false_when_recovery_state_is_broker_identity_uncertain():
    rec = _record()
    _acknowledged(rec)
    apply_status_transition(rec, ExecutionOrderStatus.WORKING)
    rec.recovery_state = OrderRecoveryState.BROKER_IDENTITY_UNCERTAIN
    assert not is_cancellable(rec)


# --- AdoptedOrderPermission gating (revision 3.2) ---------------------------


def test_is_cancellable_false_for_a_user_adopted_record_without_cancel_permission():
    rec = _record()
    _acknowledged(rec)
    apply_status_transition(rec, ExecutionOrderStatus.WORKING)
    rec.origin = OrderOrigin.USER_ADOPTED
    assert rec.adoption_permissions == frozenset()
    assert not is_cancellable(rec)


def test_is_cancellable_true_for_a_user_adopted_record_with_cancel_permission_granted():
    rec = _record(adoption_permissions=frozenset({AdoptedOrderPermission.CANCEL}))
    rec.origin = OrderOrigin.USER_ADOPTED
    _acknowledged(rec)
    apply_status_transition(rec, ExecutionOrderStatus.WORKING)
    assert is_cancellable(rec)


def test_invalid_adoption_permission_raises_rather_than_silently_defaulting():
    with pytest.raises(ValueError):
        _record(adoption_permissions=frozenset({"NOT_A_REAL_PERMISSION"}))
