"""Tests for src.services.execution_order_repository.

docs/kanban_production_readiness.md, Workstream 2, A1, revision 3.2:
"ExecutionOrderRecord... need[s] real, restart-surviving tables..., not
merely in-memory dataclasses."
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

from src.core.execution_order_record import (
    BrokerIdentityStatus,
    ExecutionOrderRecord,
    ExecutionOrderStatus,
)
from src.core.order_state import OrderIntent, OrderSide
from src.services import execution_order_repository as repo


def _make_engine(tmp_path):
    return create_engine(
        f"sqlite:///{tmp_path / 'execution_orders.db'}", future=True, poolclass=NullPool
    )


def _record(**overrides) -> ExecutionOrderRecord:
    fields = dict(
        environment="PROD", account_no="1", symbol="AAPL", side=OrderSide.BUY,
        intent=OrderIntent.ENTRY, client_order_id="CID-1", submitted_quantity=10,
    )
    fields.update(overrides)
    return ExecutionOrderRecord(**fields)


# --- CRUD / round trip ----------------------------------------------------


def test_record_then_fetch_round_trips(tmp_path):
    engine = _make_engine(tmp_path)
    saved = repo.record_execution_order(engine, _record())
    assert saved.version == 1

    fetched = repo.fetch_execution_order(engine, "CID-1")
    assert fetched is not None
    assert fetched.client_order_id == "CID-1"
    assert fetched.symbol == "AAPL"
    assert fetched.version == 1


def test_round_trip_preserves_enum_and_permission_fields(tmp_path):
    from src.core.execution_order_record import AdoptedOrderPermission, OrderOrigin

    engine = _make_engine(tmp_path)
    rec = _record(
        origin=OrderOrigin.USER_ADOPTED,
        status=ExecutionOrderStatus.ACKNOWLEDGED,
        broker_identity_status=BrokerIdentityStatus.EXACT,
        broker_order_id="B-1",
        adopted_from_external_order_id="EXT-1",
        adoption_permissions=frozenset({AdoptedOrderPermission.CANCEL}),
    )
    repo.record_execution_order(engine, rec)
    fetched = repo.fetch_execution_order(engine, "CID-1")
    assert fetched.origin == OrderOrigin.USER_ADOPTED
    assert fetched.broker_identity_status == BrokerIdentityStatus.EXACT
    assert fetched.adoption_permissions == frozenset({AdoptedOrderPermission.CANCEL})


def test_fetch_returns_none_when_absent(tmp_path):
    engine = _make_engine(tmp_path)
    assert repo.fetch_execution_order(engine, "does-not-exist") is None


def test_duplicate_client_order_id_is_rejected(tmp_path):
    engine = _make_engine(tmp_path)
    repo.record_execution_order(engine, _record())
    with pytest.raises(repo.DuplicateExecutionOrderError):
        repo.record_execution_order(engine, _record())


# --- Optimistic concurrency -------------------------------------------------


def test_save_with_correct_expected_version_succeeds(tmp_path):
    engine = _make_engine(tmp_path)
    repo.record_execution_order(engine, _record())
    fetched = repo.fetch_execution_order(engine, "CID-1")
    fetched.remaining_quantity = 5
    updated = repo.save_execution_order(engine, fetched, expected_version=fetched.version)
    assert updated.version == 2

    refetched = repo.fetch_execution_order(engine, "CID-1")
    assert refetched.remaining_quantity == 5
    assert refetched.version == 2


def test_save_with_stale_expected_version_raises(tmp_path):
    engine = _make_engine(tmp_path)
    repo.record_execution_order(engine, _record())
    fetched = repo.fetch_execution_order(engine, "CID-1")
    # Simulate another writer already having updated it.
    repo.save_execution_order(engine, fetched, expected_version=fetched.version)

    stale = repo.fetch_execution_order(engine, "CID-1")
    stale.version = 1  # pretend we're still holding the old version
    with pytest.raises(repo.ExecutionOrderVersionConflictError):
        repo.save_execution_order(engine, stale, expected_version=1)
    # A rejected write must leave the caller's in-memory object exactly as
    # it was -- record.version must only ever move to a value the database
    # actually confirmed, never optimistically mutated ahead of the write.
    assert stale.version == 1

    on_disk = repo.fetch_execution_order(engine, "CID-1")
    assert on_disk.version == 2


def test_save_missing_record_raises_not_found(tmp_path):
    engine = _make_engine(tmp_path)
    repo.ensure_execution_orders_table(engine)
    with pytest.raises(repo.ExecutionOrderNotFoundError):
        repo.save_execution_order(engine, _record(), expected_version=1)


def test_save_rejects_a_changed_environment_account_or_symbol(tmp_path):
    """environment/account_no/symbol are immutable identity fields
    (revision 3.2) -- a save that silently changes them would let the
    indexed relational columns and the JSON payload diverge."""
    engine = _make_engine(tmp_path)
    repo.record_execution_order(engine, _record())
    fetched = repo.fetch_execution_order(engine, "CID-1")
    fetched.symbol = "MSFT"
    with pytest.raises(repo.ImmutableFieldChangedError):
        repo.save_execution_order(engine, fetched, expected_version=fetched.version)

    # Nothing was written -- the stored row is untouched.
    on_disk = repo.fetch_execution_order(engine, "CID-1")
    assert on_disk.symbol == "AAPL"
    assert on_disk.version == 1


def test_the_database_constraint_rejects_a_duplicate_exact_identity_even_if_the_app_precheck_is_bypassed(
    tmp_path, monkeypatch
):
    """_check_broker_identity_conflict (the SELECT-then-check pre-check) is
    only a fast, friendly failure for the common non-racing case -- the
    real, race-safe authority is the database's own UNIQUE constraint on
    broker_identity_key. Proven by disabling the pre-check entirely (as a
    concurrent transaction effectively would, since it wouldn't see an
    uncommitted row from another in-flight transaction either) and
    confirming the write is still rejected by the constraint itself."""
    engine = _make_engine(tmp_path)
    monkeypatch.setattr(repo, "_check_broker_identity_conflict", lambda conn, record: None)

    repo.record_execution_order(
        engine,
        _record(
            client_order_id="CID-1", broker_order_id="B-1",
            status=ExecutionOrderStatus.ACKNOWLEDGED, broker_identity_status=BrokerIdentityStatus.EXACT,
        ),
    )
    with pytest.raises(repo.BrokerIdentityConflictError):
        repo.record_execution_order(
            engine,
            _record(
                client_order_id="CID-2", symbol="MSFT", broker_order_id="B-1",
                status=ExecutionOrderStatus.ACKNOWLEDGED, broker_identity_status=BrokerIdentityStatus.EXACT,
            ),
        )


def test_two_different_records_cannot_both_adopt_the_same_external_order(tmp_path):
    engine = _make_engine(tmp_path)
    repo.record_execution_order(
        engine,
        _record(
            client_order_id="CID-1",
            adopted_from_external_order_id="EXT-1",
        ),
    )
    with pytest.raises(repo.DuplicateAdoptionError):
        repo.record_execution_order(
            engine,
            _record(
                client_order_id="CID-2", symbol="MSFT",
                adopted_from_external_order_id="EXT-1",
            ),
        )


# --- broker identity uniqueness (revision 3.2) ------------------------------


def test_two_different_records_cannot_both_claim_the_same_exact_broker_order_id(tmp_path):
    engine = _make_engine(tmp_path)
    repo.record_execution_order(
        engine,
        _record(
            client_order_id="CID-1", broker_order_id="B-1",
            status=ExecutionOrderStatus.ACKNOWLEDGED, broker_identity_status=BrokerIdentityStatus.EXACT,
        ),
    )
    with pytest.raises(repo.BrokerIdentityConflictError):
        repo.record_execution_order(
            engine,
            _record(
                client_order_id="CID-2", symbol="MSFT", broker_order_id="B-1",
                status=ExecutionOrderStatus.ACKNOWLEDGED, broker_identity_status=BrokerIdentityStatus.EXACT,
            ),
        )


def test_ambiguous_identity_does_not_trigger_the_broker_identity_conflict_check(tmp_path):
    """Two different records with the same broker_order_id are only a
    conflict once both actually claim EXACT identity -- an AMBIGUOUS
    candidate match (A4a) is not itself a claim."""
    engine = _make_engine(tmp_path)
    repo.record_execution_order(
        engine,
        _record(
            client_order_id="CID-1", broker_order_id="B-1",
            status=ExecutionOrderStatus.ACKNOWLEDGED, broker_identity_status=BrokerIdentityStatus.EXACT,
        ),
    )
    # A second record that merely carries the same broker_order_id string
    # without EXACT identity status must not trip the conflict check --
    # this mirrors mark_broker_identity_exact's own same-record rule,
    # extended across records. AMBIGUOUS is only legal alongside
    # SUBMITTING/UNKNOWN_SUBMISSION_STATE.
    other = _record(
        client_order_id="CID-2", symbol="MSFT", status=ExecutionOrderStatus.SUBMITTING,
        broker_identity_status=BrokerIdentityStatus.AMBIGUOUS,
    )
    other.broker_order_id = "B-1"
    repo.record_execution_order(engine, other)  # must not raise


def test_updating_a_record_to_claim_an_already_exact_broker_order_id_elsewhere_is_rejected(tmp_path):
    engine = _make_engine(tmp_path)
    repo.record_execution_order(
        engine,
        _record(
            client_order_id="CID-1", broker_order_id="B-1",
            status=ExecutionOrderStatus.ACKNOWLEDGED, broker_identity_status=BrokerIdentityStatus.EXACT,
        ),
    )
    other = repo.record_execution_order(
        engine,
        _record(
            client_order_id="CID-2", symbol="MSFT",
            status=ExecutionOrderStatus.SUBMITTING, broker_identity_status=BrokerIdentityStatus.AMBIGUOUS,
        ),
    )
    other.status = ExecutionOrderStatus.ACKNOWLEDGED
    other.broker_order_id = "B-1"
    other.broker_identity_status = BrokerIdentityStatus.EXACT
    with pytest.raises(repo.BrokerIdentityConflictError):
        repo.save_execution_order(engine, other, expected_version=other.version)
