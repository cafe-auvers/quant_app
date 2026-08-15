"""Tests for src.core.discovered_external_order.

docs/kanban_production_readiness.md, Workstream 2, A4b, revisions 3.1 and
3.2.
"""
from __future__ import annotations

import pytest

from src.core.execution_order_record import (
    AdoptedOrderPermission,
    BrokerIdentityStatus,
    ExecutionOrderRecord,
    ExecutionOrderStatus,
    OrderOrigin,
)
from src.core.discovered_external_order import (
    DiscoveredExternalOrder,
    ExternalOrderDisposition,
    InvalidExternalOrderDispositionTransitionError,
    adopt_external_order,
    allowed_disposition_transitions,
    is_unreconciled_external_order,
    new_discovered_external_order,
    validate_disposition_transition,
)
from src.core.order_recovery_state import OrderRecoveryState
from src.core.order_state import OrderSide


def _external(**overrides) -> DiscoveredExternalOrder:
    fields = dict(
        environment="PROD", account_no="1", symbol="AAPL", side=OrderSide.BUY,
        broker_order_id="B-999", quantity_requested=10,
    )
    fields.update(overrides)
    return DiscoveredExternalOrder(**fields)


# --- Construction -------------------------------------------------------


def test_requires_a_broker_order_id():
    with pytest.raises(ValueError):
        _external(broker_order_id="")


def test_defaults_to_discovered_unowned():
    ext = _external()
    assert ext.disposition == ExternalOrderDisposition.DISCOVERED_UNOWNED
    assert ext.adopted_at is None
    assert ext.adopted_by == ""


def test_gets_a_generated_external_order_id():
    a = _external()
    b = _external()
    assert a.external_order_id
    assert a.external_order_id != b.external_order_id


def test_invalid_side_raises_rather_than_silently_defaulting():
    with pytest.raises(ValueError):
        _external(side="NOT_A_REAL_SIDE")


def test_invalid_broker_status_raises_rather_than_silently_defaulting():
    with pytest.raises(ValueError):
        _external(broker_status="NOT_A_REAL_STATUS")


# --- Redaction (revision 3.2) -------------------------------------------


def test_new_discovered_external_order_redacts_the_raw_response_before_storage():
    order = new_discovered_external_order(
        environment="PROD", account_no="1", symbol="AAPL", side=OrderSide.BUY,
        broker_order_id="B-1", quantity_requested=10,
        raw_response={
            "account_no": "12345678-01",
            "access_token": "SUPER_SECRET_TOKEN",
            "authorization": "Bearer abc123",
            "symbol": "AAPL",
        },
    )
    assert order.redacted_response["access_token"] == "[REDACTED]"
    assert "SUPER_SECRET_TOKEN" not in str(order.redacted_response)
    assert "Bearer abc123" not in str(order.redacted_response)
    assert "12345678-01" not in str(order.redacted_response)
    assert order.redacted_response["symbol"] == "AAPL"
    assert order.response_hash  # integrity/dedup hash was computed


def test_new_discovered_external_order_redacts_nested_sensitive_fields():
    order = new_discovered_external_order(
        environment="PROD", account_no="1", symbol="AAPL", side=OrderSide.BUY,
        broker_order_id="B-1",
        raw_response={"nested": {"password": "hunter2", "ok": "fine"}},
    )
    assert order.redacted_response["nested"]["password"] == "[REDACTED]"
    assert order.redacted_response["nested"]["ok"] == "fine"


# --- ExternalOrderDisposition transition table -------------------------


_VALID_TRANSITION_PAIRS = [
    (frm, to) for frm in ExternalOrderDisposition for to in allowed_disposition_transitions(frm)
]


@pytest.mark.parametrize("frm,to", _VALID_TRANSITION_PAIRS)
def test_every_encoded_valid_transition_is_accepted(frm, to):
    validate_disposition_transition(frm, to)


@pytest.mark.parametrize(
    "frm,to",
    [
        (ExternalOrderDisposition.DISCOVERED_UNOWNED, ExternalOrderDisposition.USER_ADOPTED),
        (ExternalOrderDisposition.DISCOVERED_UNOWNED, ExternalOrderDisposition.DISMISSED_TERMINAL),
    ],
)
def test_every_documented_transition_table_row_matches_the_written_contract(frm, to):
    validate_disposition_transition(frm, to)


@pytest.mark.parametrize(
    "terminal_state", [ExternalOrderDisposition.USER_ADOPTED, ExternalOrderDisposition.DISMISSED_TERMINAL]
)
def test_terminal_dispositions_have_no_outbound_transitions(terminal_state):
    for candidate in ExternalOrderDisposition:
        if candidate == terminal_state:
            continue
        with pytest.raises(InvalidExternalOrderDispositionTransitionError):
            validate_disposition_transition(terminal_state, candidate)


# --- UNRECONCILED_BROKER_ORDER-equivalent derivation (INV-7 extended) ---


def test_unreconciled_equivalent_is_true_only_while_discovered_unowned():
    assert is_unreconciled_external_order(ExternalOrderDisposition.DISCOVERED_UNOWNED) is True
    assert is_unreconciled_external_order(ExternalOrderDisposition.USER_ADOPTED) is False
    assert is_unreconciled_external_order(ExternalOrderDisposition.DISMISSED_TERMINAL) is False


# --- A4b's core rule: never attach/cancel/reserve automatically ---------


def test_a4b_creates_a_discovered_external_order_not_an_execution_order_record():
    ext = _external()
    assert isinstance(ext, DiscoveredExternalOrder)
    assert not isinstance(ext, ExecutionOrderRecord)


# --- Adoption (INV-22) ---------------------------------------------------


def test_adopt_action_is_the_only_path_to_a_user_adopted_execution_order_record():
    ext = _external(quantity_requested=25, limit_price=101.5, filled_quantity=10)
    record = adopt_external_order(ext, adopted_by="tony")

    assert isinstance(record, ExecutionOrderRecord)
    assert record.origin == OrderOrigin.USER_ADOPTED
    assert record.broker_identity_status == BrokerIdentityStatus.EXACT
    assert record.broker_order_id == "B-999"
    assert record.submitted_quantity == 25
    assert record.submitted_limit_price == 101.5
    assert record.filled_quantity == 10
    assert record.adopted_from_external_order_id == ext.external_order_id
    assert record.recovery_state == OrderRecoveryState.NONE


def test_adoption_preserves_the_original_discovered_external_order_as_an_audit_trail():
    """The original record is never mutated into an ExecutionOrderRecord --
    it stays as its own immutable-aside-from-disposition record."""
    ext = _external()
    adopt_external_order(ext, adopted_by="tony")

    assert ext.disposition == ExternalOrderDisposition.USER_ADOPTED
    assert ext.adopted_by == "tony"
    assert ext.adopted_at is not None
    # Still the original record's own type and identity -- not rewritten.
    assert isinstance(ext, DiscoveredExternalOrder)
    assert not isinstance(ext, ExecutionOrderRecord)


def test_adoption_requires_a_non_blank_attribution():
    ext = _external()
    with pytest.raises(ValueError):
        adopt_external_order(ext, adopted_by="")
    assert ext.disposition == ExternalOrderDisposition.DISCOVERED_UNOWNED  # unchanged


def test_adoption_of_an_already_adopted_order_raises():
    ext = _external()
    adopt_external_order(ext, adopted_by="tony")
    with pytest.raises(InvalidExternalOrderDispositionTransitionError):
        adopt_external_order(ext, adopted_by="someone-else")


def test_adoption_of_a_dismissed_terminal_order_raises():
    ext = _external()
    ext.disposition = ExternalOrderDisposition.DISMISSED_TERMINAL
    with pytest.raises(InvalidExternalOrderDispositionTransitionError):
        adopt_external_order(ext, adopted_by="tony")


# --- AdoptedOrderPermission scoping (revision 3.2) --------------------------


def test_adoption_defaults_to_no_permissions_granted():
    """permissions is never defaulted to "everything" merely because
    adoption happened -- the caller must explicitly widen it."""
    ext = _external()
    record = adopt_external_order(ext, adopted_by="tony")
    assert record.adoption_permissions == frozenset()


def test_adoption_grants_exactly_the_permissions_passed():
    ext = _external()
    record = adopt_external_order(
        ext, adopted_by="tony", permissions=frozenset({AdoptedOrderPermission.CANCEL})
    )
    assert record.adoption_permissions == frozenset({AdoptedOrderPermission.CANCEL})
    assert AdoptedOrderPermission.REPLACE not in record.adoption_permissions


def test_a4b_never_auto_cancels_or_attaches_to_a_card():
    """There is no code path in this module that submits a broker call or
    touches a TradeCardState -- adoption only ever constructs local
    records. This test documents that boundary explicitly: the module
    exposes no such function."""
    import src.core.discovered_external_order as mod

    assert not hasattr(mod, "cancel_external_order")
    assert not hasattr(mod, "attach_to_card")
