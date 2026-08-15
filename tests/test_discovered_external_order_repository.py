"""Tests for src.services.discovered_external_order_repository.

docs/kanban_production_readiness.md, Workstream 2, A4b, revision 3.2:
"DiscoveredExternalOrder... need[s] real, restart-surviving tables"; and
"Adoption itself... must be one atomic transaction... never one committed
without the other."
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

from src.core.discovered_external_order import ExternalOrderDisposition, new_discovered_external_order
from src.core.execution_order_record import AdoptedOrderPermission, BrokerIdentityStatus, OrderOrigin
from src.core.order_state import OrderSide
from src.services import discovered_external_order_repository as repo
from src.services.execution_order_repository import fetch_execution_order


def _make_engine(tmp_path):
    return create_engine(
        f"sqlite:///{tmp_path / 'external_orders.db'}", future=True, poolclass=NullPool
    )


def _external(**overrides):
    fields = dict(
        environment="PROD", account_no="1", symbol="AAPL", side=OrderSide.BUY,
        broker_order_id="B-999", quantity_requested=25, limit_price=101.5,
    )
    fields.update(overrides)
    return new_discovered_external_order(**fields)


# --- CRUD / round trip ----------------------------------------------------


def test_record_then_fetch_round_trips(tmp_path):
    engine = _make_engine(tmp_path)
    ext = _external()
    repo.record_discovered_external_order(engine, ext)

    fetched = repo.fetch_discovered_external_order(engine, ext.external_order_id)
    assert fetched is not None
    assert fetched.broker_order_id == "B-999"
    assert fetched.disposition == ExternalOrderDisposition.DISCOVERED_UNOWNED
    assert fetched.version == 1


def test_fetch_returns_none_when_absent(tmp_path):
    engine = _make_engine(tmp_path)
    assert repo.fetch_discovered_external_order(engine, "does-not-exist") is None


def test_duplicate_external_order_id_is_rejected(tmp_path):
    engine = _make_engine(tmp_path)
    ext = _external()
    repo.record_discovered_external_order(engine, ext)
    with pytest.raises(repo.DuplicateExternalOrderError):
        repo.record_discovered_external_order(engine, ext)


def test_stored_redacted_response_survives_the_round_trip(tmp_path):
    engine = _make_engine(tmp_path)
    ext = _external(raw_response={"access_token": "SECRET", "symbol": "AAPL"})
    repo.record_discovered_external_order(engine, ext)
    fetched = repo.fetch_discovered_external_order(engine, ext.external_order_id)
    assert fetched.redacted_response["access_token"] == "[REDACTED]"
    assert fetched.response_hash == ext.response_hash


# --- Atomic adoption (revision 3.2) -----------------------------------------


def test_adopt_external_order_in_db_creates_a_persisted_execution_order(tmp_path):
    engine = _make_engine(tmp_path)
    ext = _external()
    repo.record_discovered_external_order(engine, ext)

    record = repo.adopt_external_order_in_db(
        engine, ext.external_order_id, adopted_by="tony",
        permissions=frozenset({AdoptedOrderPermission.CANCEL}),
    )

    assert record.origin == OrderOrigin.USER_ADOPTED
    assert record.broker_identity_status == BrokerIdentityStatus.EXACT
    assert record.broker_order_id == "B-999"
    assert record.adoption_permissions == frozenset({AdoptedOrderPermission.CANCEL})

    persisted = fetch_execution_order(engine, record.client_order_id)
    assert persisted is not None
    assert persisted.origin == OrderOrigin.USER_ADOPTED


def test_adopt_external_order_in_db_updates_the_external_order_disposition_atomically(tmp_path):
    engine = _make_engine(tmp_path)
    ext = _external()
    repo.record_discovered_external_order(engine, ext)

    repo.adopt_external_order_in_db(engine, ext.external_order_id, adopted_by="tony")

    refetched = repo.fetch_discovered_external_order(engine, ext.external_order_id)
    assert refetched.disposition == ExternalOrderDisposition.USER_ADOPTED
    assert refetched.adopted_by == "tony"
    assert refetched.adopted_at is not None
    # Original discovery fields preserved as the audit trail -- never
    # rewritten to look application-originated.
    assert refetched.broker_order_id == "B-999"


def test_adopting_an_already_adopted_order_raises_and_does_not_create_a_second_execution_order(tmp_path):
    engine = _make_engine(tmp_path)
    ext = _external()
    repo.record_discovered_external_order(engine, ext)
    first = repo.adopt_external_order_in_db(engine, ext.external_order_id, adopted_by="tony")

    with pytest.raises(Exception):
        repo.adopt_external_order_in_db(engine, ext.external_order_id, adopted_by="someone-else")

    # Still exactly one execution_orders row for this adoption -- the
    # rejected second attempt created nothing (the whole transaction is
    # atomic: no half-committed state).
    from src.services.execution_order_repository import ensure_execution_orders_table

    table = ensure_execution_orders_table(engine)
    with engine.begin() as conn:
        from sqlalchemy import select

        rows = conn.execute(select(table)).fetchall()
    assert len(rows) == 1
    assert rows[0].client_order_id == first.client_order_id


def test_adopt_external_order_in_db_raises_not_found_for_a_missing_external_order(tmp_path):
    engine = _make_engine(tmp_path)
    repo.ensure_discovered_external_orders_table(engine)
    with pytest.raises(repo.ExternalOrderNotFoundError):
        repo.adopt_external_order_in_db(engine, "does-not-exist", adopted_by="tony")


def test_adopt_external_order_in_db_requires_attribution(tmp_path):
    engine = _make_engine(tmp_path)
    ext = _external()
    repo.record_discovered_external_order(engine, ext)
    with pytest.raises(ValueError):
        repo.adopt_external_order_in_db(engine, ext.external_order_id, adopted_by="")

    # Nothing was persisted -- still DISCOVERED_UNOWNED.
    refetched = repo.fetch_discovered_external_order(engine, ext.external_order_id)
    assert refetched.disposition == ExternalOrderDisposition.DISCOVERED_UNOWNED
