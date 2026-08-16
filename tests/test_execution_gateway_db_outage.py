from __future__ import annotations

import pytest
from sqlalchemy import create_engine

from fakes.fake_execution_broker import FakeExecutionBroker
from src.core.execution_mode import ExecutionLease, ExecutionSource
from src.core.execution_request import CancelExecutionRequest, SubmitExecutionRequest
from src.core.execution_order_record import ExecutionOrderStatus
from src.core.order_state import OrderIntent, OrderSide
from src.services.emergency_journal import EmergencyJournal, EmergencyLeaseAllowance
from src.services.execution_command_gateway import (
    CanonicalDatabaseUnavailableError,
    EmergencyActionNotPermittedError,
    EmergencyJournalUnavailableError,
    ExecutionCommandGateway,
)
from src.services.execution_lease_protocol import FakeExecutionLeaseProtocol
from src.services.execution_order_repository import fetch_execution_order
from src.services.mutation_budget_protocol import AllowAllMutationBudget


def _request(lease: ExecutionLease, **overrides) -> SubmitExecutionRequest:
    fields = {
        "client_order_id": "EMERGENCY-CID-1",
        "environment": "PROD",
        "account_no": "12345678-01",
        "symbol": "AAPL",
        "side": OrderSide.SELL,
        "intent": OrderIntent.STOP_LOSS,
        "quantity": 4,
        "limit_price": 99.0,
        "lease": lease,
        "source": ExecutionSource.KANBAN_BOARD,
        "emergency": True,
    }
    fields.update(overrides)
    return SubmitExecutionRequest(**fields)


def _gateway(tmp_path, *, allowance=None, journal=None):
    writable = [True]
    lease = ExecutionLease("pc-1", "token-7", 7)
    broker = FakeExecutionBroker()
    gateway = ExecutionCommandGateway(
        real_broker=broker,
        engine=create_engine(f"sqlite:///{tmp_path / 'canonical.db'}", future=True),
        lease_protocol=FakeExecutionLeaseProtocol(current=lease, epoch_verified=True),
        mutation_budget=AllowAllMutationBudget(),
        buying_power_provider=lambda _environment, _account: 100_000.0,
        mode_override=True,
        emergency_journal=journal or EmergencyJournal(tmp_path / "emergency.jsonl"),
        emergency_lease_allowance=allowance or EmergencyLeaseAllowance(max_seconds=30),
        database_writable_provider=lambda: writable[0],
    )
    gateway.note_canonical_lease_verified(lease)
    writable[0] = False
    return gateway, broker, lease, writable


@pytest.mark.usefixtures("trading_enabled")
def test_database_outage_blocks_ordinary_command_before_broker(tmp_path):
    gateway, broker, lease, _ = _gateway(tmp_path)
    broker.queue_acceptance(broker_order_id="MUST-NOT-BE-USED")

    with pytest.raises(CanonicalDatabaseUnavailableError):
        gateway.submit_guarded(_request(lease, emergency=False))

    assert broker.submit_calls == []


@pytest.mark.usefixtures("trading_enabled")
def test_emergency_journal_is_fsynced_before_outage_broker_mutation(tmp_path):
    journal = EmergencyJournal(tmp_path / "emergency.jsonl")
    gateway, broker, lease, _ = _gateway(tmp_path, journal=journal)
    broker.queue_acceptance(broker_order_id="BR-EMERGENCY")

    result = gateway.submit_guarded(_request(lease))

    assert result.broker_order_id == "BR-EMERGENCY"
    assert len(broker.submit_calls) == 1
    assert [entry["event_type"] for entry in journal.load_entries()] == [
        "REQUESTED",
        "OUTCOME",
    ]


@pytest.mark.usefixtures("trading_enabled")
def test_local_journal_failure_prevents_destructive_call(tmp_path):
    class FailingJournal(EmergencyJournal):
        def append_requested(self, **_kwargs):
            raise OSError("disk unavailable")

    gateway, broker, lease, _ = _gateway(
        tmp_path, journal=FailingJournal(tmp_path / "emergency.jsonl")
    )
    broker.queue_acceptance(broker_order_id="MUST-NOT-BE-USED")

    with pytest.raises(EmergencyJournalUnavailableError, match="disk unavailable"):
        gateway.submit_guarded(_request(lease))

    assert broker.submit_calls == []


@pytest.mark.usefixtures("trading_enabled")
def test_expired_emergency_allowance_blocks_broker(tmp_path):
    monotonic = [10.0]
    allowance = EmergencyLeaseAllowance(
        max_seconds=5, monotonic=lambda: monotonic[0]
    )
    gateway, broker, lease, _ = _gateway(tmp_path, allowance=allowance)
    broker.queue_acceptance(broker_order_id="FIRST")
    gateway.submit_guarded(_request(lease))
    monotonic[0] = 15.0
    broker.queue_acceptance(broker_order_id="MUST-NOT-BE-USED")

    with pytest.raises(EmergencyActionNotPermittedError, match="expired"):
        gateway.submit_guarded(
            _request(lease, client_order_id="EMERGENCY-CID-2")
        )

    assert len(broker.submit_calls) == 1


@pytest.mark.usefixtures("trading_enabled")
def test_emergency_cancel_reconciles_terminal_state_after_database_recovery(
    tmp_path,
):
    gateway, broker, lease, writable = _gateway(tmp_path)
    writable[0] = True
    broker.queue_acceptance(broker_order_id="BR-WORKING")
    gateway.submit_guarded(
        _request(
            lease,
            client_order_id="SELL-CID",
            emergency=False,
            source=ExecutionSource.SYSTEM,
        )
    )
    writable[0] = False
    broker.queue_cancel_confirmed()

    result = gateway.cancel_guarded(
        CancelExecutionRequest(
            client_order_id="SELL-CID",
            cancel_command_id="SELL-CANCEL-1",
            environment="PROD",
            account_no="12345678-01",
            lease=lease,
            source=ExecutionSource.SYSTEM,
            emergency=True,
            symbol="AAPL",
            broker_order_id="BR-WORKING",
            quantity=4,
            side="SELL",
            exchange="NASD",
        )
    )
    assert result.status == ExecutionOrderStatus.CANCELLED
    assert len(broker.cancel_calls) == 1

    writable[0] = True
    assert gateway.reconcile_emergency_journal() == 1
    assert fetch_execution_order(
        gateway.database_engine, "SELL-CID"
    ).status == ExecutionOrderStatus.CANCELLED
