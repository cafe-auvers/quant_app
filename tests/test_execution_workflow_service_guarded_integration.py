"""Flag-enabled integration tests through the real workflow service
(docs/kanban_production_readiness.md, PR2 second review pass, finding 2):
``request_submit``/``request_cancel``/``request_replace`` -- not the
gateway's own methods called directly -- exercising the actual sequence a
real ``GUARDED_ENGINE`` caller would go through, including restart-safe
idempotency driven by the workflow layer's own stable identity, not the
gateway's internals.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

from src.core.execution_mode import ExecutionLease, ExecutionSource
from src.core.execution_order_record import ExecutionOrderStatus
from src.core.order_state import OrderIntent, OrderSide
from src.services import execution_workflow_service as workflow
from src.services.execution_command_gateway import ExecutionCommandGateway
from src.services.execution_command_repository import DuplicateCommandError
from src.services.execution_lease_protocol import FakeExecutionLeaseProtocol
from src.services.execution_order_repository import fetch_execution_order
from src.services.execution_ownership_repository import assign_ownership
from src.core.execution_ownership import ExecutionOwner, ExecutionOwnership
from src.services.mutation_budget_protocol import AllowAllMutationBudget
from fakes.fake_execution_broker import FakeExecutionBroker


def _make_engine(tmp_path):
    return create_engine(f"sqlite:///{tmp_path / 'workflow.db'}", future=True, poolclass=NullPool)


def _guarded_gateway(tmp_path, broker=None):
    engine = _make_engine(tmp_path)
    broker = broker or FakeExecutionBroker()
    lease_protocol = FakeExecutionLeaseProtocol(
        current=ExecutionLease(device_id="dev-1", lease_token="tok-1", lease_epoch=1)
    )
    gateway = ExecutionCommandGateway(
        real_broker=broker, engine=engine, mode_override=True,
        lease_protocol=lease_protocol, mutation_budget=AllowAllMutationBudget(),
    )
    # H1: these integration tests exercise submit/cancel/replace mechanics
    # through a KANBAN_BOARD caller, not ownership gating itself (that has
    # its own dedicated tests in test_execution_command_gateway.py) --
    # assign KANBAN ownership up front so the mechanics tests aren't
    # incidentally blocked by H2's LEGACY default.
    assign_ownership(
        engine,
        ExecutionOwnership(
            environment="PROD", account_no="12345678-01", symbol="AAPL", owner=ExecutionOwner.KANBAN,
            strategy_instance_id="test",
        ),
    )
    assign_ownership(
        engine,
        ExecutionOwnership(
            environment="PROD", account_no="12345678-01", symbol="MSFT", owner=ExecutionOwner.KANBAN,
            strategy_instance_id="test",
        ),
    )
    return gateway, broker, engine


LEASE = ExecutionLease(device_id="dev-1", lease_token="tok-1", lease_epoch=1)


@pytest.mark.usefixtures("trading_enabled")
def test_request_submit_through_the_real_workflow_reaches_acknowledged(tmp_path):
    gateway, broker, engine = _guarded_gateway(tmp_path)
    broker.queue_acceptance(broker_order_id="B-1")

    record = workflow.request_submit(
        source=ExecutionSource.KANBAN_BOARD, environment="PROD", account_no="12345678-01", symbol="AAPL",
        side=OrderSide.BUY, intent=OrderIntent.ENTRY, quantity=10, limit_price=100.0,
        gateway=gateway, client_order_id="WF-CID-1", lease=LEASE,
    )
    assert record.status == ExecutionOrderStatus.ACKNOWLEDGED
    assert record.broker_order_id == "B-1"

    persisted = fetch_execution_order(engine, "WF-CID-1")
    assert persisted.status == ExecutionOrderStatus.ACKNOWLEDGED


@pytest.mark.usefixtures("trading_enabled")
def test_request_submit_then_request_cancel_using_the_same_client_order_id_reaches_cancelled(tmp_path):
    gateway, broker, engine = _guarded_gateway(tmp_path)
    broker.queue_acceptance(broker_order_id="B-1")
    workflow.request_submit(
        source=ExecutionSource.KANBAN_BOARD, environment="PROD", account_no="12345678-01", symbol="AAPL",
        side=OrderSide.BUY, intent=OrderIntent.ENTRY, quantity=10, limit_price=100.0,
        gateway=gateway, client_order_id="WF-CID-2", lease=LEASE,
    )

    broker.queue_cancel_confirmed()
    cancelled = workflow.request_cancel(
        source=ExecutionSource.KANBAN_BOARD, client_order_id="WF-CID-2", gateway=gateway,
        environment="PROD", account_no="12345678-01", lease=LEASE,
    )
    assert cancelled.status == ExecutionOrderStatus.CANCELLED

    persisted = fetch_execution_order(engine, "WF-CID-2")
    assert persisted.status == ExecutionOrderStatus.CANCELLED


@pytest.mark.usefixtures("trading_enabled")
def test_request_replace_preserves_the_original_and_creates_a_linked_replacement(tmp_path):
    gateway, broker, engine = _guarded_gateway(tmp_path)
    broker.queue_acceptance(broker_order_id="B-1")
    workflow.request_submit(
        source=ExecutionSource.KANBAN_BOARD, environment="PROD", account_no="12345678-01", symbol="AAPL",
        side=OrderSide.BUY, intent=OrderIntent.ENTRY, quantity=10, limit_price=100.0,
        gateway=gateway, client_order_id="WF-CID-3", lease=LEASE,
    )

    broker.queue_cancel_confirmed()
    broker.queue_acceptance(broker_order_id="B-2")
    replacement = workflow.request_replace(
        source=ExecutionSource.KANBAN_BOARD, client_order_id="WF-CID-3", new_quantity=5,
        new_limit_price=101.0, gateway=gateway, environment="PROD", account_no="12345678-01", lease=LEASE,
        new_client_order_id="WF-CID-3-REPLACEMENT",
    )
    assert replacement.status == ExecutionOrderStatus.ACKNOWLEDGED
    assert replacement.replaces_execution_order_id == "WF-CID-3"

    original = fetch_execution_order(engine, "WF-CID-3")
    assert original.status == ExecutionOrderStatus.CANCELLED
    assert original.submitted_quantity == 10  # never mutated into the replacement


def test_request_replace_raises_in_legacy_compatibility_mode():
    broker = FakeExecutionBroker()
    gateway = ExecutionCommandGateway(real_broker=broker, mode_override=False)
    with pytest.raises(NotImplementedError):
        workflow.request_replace(
            source=ExecutionSource.KANBAN_BOARD, client_order_id="X", new_quantity=1, new_limit_price=1.0,
            gateway=gateway,
        )


@pytest.mark.usefixtures("trading_enabled")
def test_restart_idempotency_through_the_workflow_layers_own_stable_identity(tmp_path):
    """Finding 1's required test, driven through request_submit (the real
    entry point), not the gateway directly: a caller that remembers its
    own client_order_id across a restart and replays request_submit with
    it makes zero additional broker calls on a fresh gateway instance."""
    engine = _make_engine(tmp_path)
    assign_ownership(
        engine,
        ExecutionOwnership(
            environment="PROD", account_no="12345678-01", symbol="AAPL", owner=ExecutionOwner.KANBAN,
            strategy_instance_id="test",
        ),
    )
    first_broker = FakeExecutionBroker()
    first_broker.queue_timeout()
    lease_protocol = FakeExecutionLeaseProtocol(current=LEASE)
    first_gateway = ExecutionCommandGateway(
        real_broker=first_broker, engine=engine, mode_override=True,
        lease_protocol=lease_protocol, mutation_budget=AllowAllMutationBudget(),
    )

    from src.services.execution_command_gateway import GuardedSubmissionAmbiguousError

    with pytest.raises(GuardedSubmissionAmbiguousError):
        workflow.request_submit(
            source=ExecutionSource.KANBAN_BOARD, environment="PROD", account_no="12345678-01", symbol="AAPL",
            side=OrderSide.BUY, intent=OrderIntent.ENTRY, quantity=10, limit_price=100.0,
            gateway=first_gateway, client_order_id="RESTART-STABLE-ID", lease=LEASE,
        )
    assert len(first_broker.submit_calls) == 1

    # A fresh gateway instance (simulating a process restart) driven by
    # the same workflow-layer call, replaying the same client_order_id
    # the caller remembered.
    second_broker = FakeExecutionBroker()
    second_broker.queue_acceptance(broker_order_id="SHOULD-NEVER-BE-USED")
    second_gateway = ExecutionCommandGateway(
        real_broker=second_broker, engine=engine, mode_override=True,
        lease_protocol=lease_protocol, mutation_budget=AllowAllMutationBudget(),
    )
    with pytest.raises(DuplicateCommandError):
        workflow.request_submit(
            source=ExecutionSource.KANBAN_BOARD, environment="PROD", account_no="12345678-01", symbol="AAPL",
            side=OrderSide.BUY, intent=OrderIntent.ENTRY, quantity=10, limit_price=100.0,
            gateway=second_gateway, client_order_id="RESTART-STABLE-ID", lease=LEASE,
        )
    assert second_broker.submit_calls == []


@pytest.mark.usefixtures("trading_enabled")
def test_request_submit_without_a_client_order_id_mints_a_fresh_one_each_call(tmp_path):
    """A caller that does NOT supply client_order_id is making a new
    decision each time -- two such calls must not collide."""
    gateway, broker, engine = _guarded_gateway(tmp_path)
    broker.queue_acceptance(broker_order_id="B-1")
    broker.queue_acceptance(broker_order_id="B-2")

    first = workflow.request_submit(
        source=ExecutionSource.KANBAN_BOARD, environment="PROD", account_no="12345678-01",
        symbol="AAPL", side=OrderSide.BUY, intent=OrderIntent.ENTRY, quantity=10, limit_price=100.0,
        gateway=gateway, lease=LEASE,
    )
    second = workflow.request_submit(
        source=ExecutionSource.KANBAN_BOARD, environment="PROD", account_no="12345678-01",
        symbol="MSFT", side=OrderSide.BUY, intent=OrderIntent.ENTRY, quantity=5, limit_price=50.0,
        gateway=gateway, lease=LEASE,
    )
    assert first.client_order_id != second.client_order_id


@pytest.mark.usefixtures("trading_enabled")
def test_request_cancel_without_a_cancel_command_id_mints_a_fresh_one(tmp_path):
    gateway, broker, engine = _guarded_gateway(tmp_path)
    broker.queue_acceptance(broker_order_id="B-1")
    workflow.request_submit(
        source=ExecutionSource.KANBAN_BOARD, environment="PROD", account_no="12345678-01", symbol="AAPL",
        side=OrderSide.BUY, intent=OrderIntent.ENTRY, quantity=10, limit_price=100.0,
        gateway=gateway, client_order_id="WF-CID-4", lease=LEASE,
    )
    broker.queue_cancel_confirmed()
    cancelled = workflow.request_cancel(
        source=ExecutionSource.KANBAN_BOARD, client_order_id="WF-CID-4", gateway=gateway,
        environment="PROD", account_no="12345678-01", lease=LEASE,
    )
    assert cancelled.status == ExecutionOrderStatus.CANCELLED
