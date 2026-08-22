from __future__ import annotations

import pytest
from sqlalchemy import create_engine

from fakes.fake_execution_broker import FakeExecutionBroker
from src.core.execution_mode import ExecutionLease, ExecutionSource
from src.core.execution_ownership import ExecutionOwner, ExecutionOwnership
from src.core.execution_request import CancelExecutionRequest, SubmitExecutionRequest
from src.core.execution_order_record import ExecutionOrderStatus
from src.core.order_state import OrderIntent, OrderSide
from src.core.trade_card_state import BoardStatus, PositionRuntimeStatus, TradeCardState
from src.services.emergency_journal import EmergencyJournal, EmergencyLeaseAllowance
from src.services.execution_command_gateway import (
    CanonicalDatabaseUnavailableError,
    EmergencyActionNotPermittedError,
    EmergencyJournalUnavailableError,
    ExecutionCommandGateway,
)
from src.services.execution_lease_protocol import FakeExecutionLeaseProtocol
from src.services.execution_order_repository import fetch_execution_order
from src.services.execution_ownership_repository import assign_ownership
from src.services import trade_card_repository
from src.services import trading_state
from src.services.mutation_budget_protocol import AllowAllMutationBudget
from src.risk.pre_trade import PreTradeRiskDecision


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
        "strategy_instance_id": "strategy-1",
        "emergency": True,
    }
    fields.update(overrides)
    if (
        fields["side"] == OrderSide.BUY
        and fields["intent"] == OrderIntent.ENTRY
        and fields.get("pre_trade_risk_decision") is None
    ):
        fields["risk_strategy_id"] = "ORB"
        fields["risk_plan_id"] = "db-outage-test-plan"
        fields["pre_trade_risk_decision"] = PreTradeRiskDecision.approve(
            environment=fields["environment"],
            account_no=fields["account_no"],
            symbol=fields["symbol"],
            side=fields["side"],
            intent=fields["intent"],
            quantity=fields["quantity"],
            reference_price=fields["limit_price"],
            exchange=fields.get("exchange", "NASD"),
            execution_policy=fields.get("execution_policy", "REGULAR_LIMIT"),
            strategy_id=fields["risk_strategy_id"],
            plan_id=fields["risk_plan_id"],
        )
    return SubmitExecutionRequest(**fields)


def _gateway(tmp_path, *, allowance=None, journal=None):
    writable = [True]
    lease = ExecutionLease("pc-1", "token-7", 7)
    broker = FakeExecutionBroker()
    engine = create_engine(f"sqlite:///{tmp_path / 'canonical.db'}", future=True)
    assign_ownership(
        engine,
        ExecutionOwnership(
            environment="PROD",
            account_no="12345678-01",
            symbol="AAPL",
            owner=ExecutionOwner.KANBAN,
            strategy_instance_id="strategy-1",
        ),
    )
    gateway = ExecutionCommandGateway(
        real_broker=broker,
        engine=engine,
        lease_protocol=FakeExecutionLeaseProtocol(current=lease, epoch_verified=True),
        mutation_budget=AllowAllMutationBudget(),
        buying_power_provider=lambda _environment, _account: 100_000.0,
        mode_override=True,
        emergency_journal=journal or EmergencyJournal(tmp_path / "emergency.jsonl"),
        emergency_lease_allowance=allowance or EmergencyLeaseAllowance(max_seconds=30),
        database_writable_provider=lambda: writable[0],
    )
    gateway.note_canonical_lease_verified(lease)
    gateway.note_canonical_ownership_verified(
        environment="PROD",
        account_no="12345678-01",
        symbol="AAPL",
        source=ExecutionSource.KANBAN_BOARD,
        strategy_instance_id="strategy-1",
    )
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
    proof = journal.load_entries()[0]["ownership_proof"]
    assert proof["owner"] == "KANBAN"
    assert proof["strategy_instance_id"] == "strategy-1"
    assert proof["version"] == 1


@pytest.mark.usefixtures("trading_enabled")
def test_database_outage_preserves_only_last_confirmed_on_for_emergency_sell(
    tmp_path,
):
    gateway, broker, lease, _ = _gateway(tmp_path)
    trading_state.set_authoritative_provider(
        lambda: (_ for _ in ()).throw(RuntimeError("canonical DB down"))
    )
    broker.queue_acceptance(broker_order_id="BR-EMERGENCY")

    result = gateway.submit_guarded(_request(lease))

    assert result.broker_order_id == "BR-EMERGENCY"
    assert len(broker.submit_calls) == 1
    with pytest.raises(trading_state.TradingDisabledError):
        trading_state.require_trading_enabled("PROD", "AAPL")


@pytest.mark.usefixtures("trading_enabled")
def test_shared_off_blocks_emergency_sell_before_local_journal_or_broker(tmp_path):
    journal = EmergencyJournal(tmp_path / "emergency.jsonl")
    gateway, broker, lease, _ = _gateway(tmp_path, journal=journal)
    trading_state.set_authoritative_provider(lambda: False)
    broker.queue_acceptance(broker_order_id="MUST-NOT-BE-USED")

    with pytest.raises(trading_state.TradingDisabledError):
        gateway.submit_guarded(_request(lease))

    assert broker.submit_calls == []
    assert journal.load_entries() == []


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
            source=ExecutionSource.KANBAN_BOARD,
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
            source=ExecutionSource.KANBAN_BOARD,
            strategy_instance_id="strategy-1",
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


@pytest.mark.usefixtures("trading_enabled")
def test_emergency_mutation_without_cached_ownership_proof_fails_closed(tmp_path):
    gateway, broker, lease, _ = _gateway(tmp_path)
    gateway._cached_ownership_proofs.clear()
    broker.queue_acceptance(broker_order_id="MUST-NOT-BE-USED")

    with pytest.raises(EmergencyActionNotPermittedError, match="ownership"):
        gateway.submit_guarded(_request(lease))

    assert broker.submit_calls == []


@pytest.mark.usefixtures("trading_enabled")
def test_outage_cancels_exact_completion_buy_before_protective_sell(tmp_path):
    gateway, broker, lease, writable = _gateway(tmp_path)
    writable[0] = True
    broker.queue_acceptance(broker_order_id="BR-COMPLETION-BUY")
    gateway.submit_guarded(
        _request(
            lease,
            client_order_id="ENTRY-COMPLETION-1",
            side=OrderSide.BUY,
            intent=OrderIntent.ENTRY,
            quantity=2,
            limit_price=101.0,
            emergency=False,
        )
    )

    writable[0] = False
    broker.queue_cancel_confirmed()
    cancelled = gateway.cancel_guarded(
        CancelExecutionRequest(
            client_order_id="ENTRY-COMPLETION-1",
            cancel_command_id="ENTRY-COMPLETION-CANCEL-1",
            environment="PROD",
            account_no="12345678-01",
            lease=lease,
            source=ExecutionSource.KANBAN_BOARD,
            strategy_instance_id="strategy-1",
            emergency=True,
            protective_entry_completion=True,
            symbol="AAPL",
            broker_order_id="BR-COMPLETION-BUY",
            quantity=2,
            side="BUY",
            exchange="NASD",
        )
    )
    assert cancelled.status == ExecutionOrderStatus.CANCELLED

    # The caller refreshes authoritative sellable quantity only after the
    # exact conflicting BUY is terminal, then submits the liquidation.
    sellable_quantity = 4
    broker.queue_acceptance(broker_order_id="BR-PROTECTIVE-SELL")
    sold = gateway.submit_guarded(
        _request(
            lease,
            client_order_id="EMERGENCY-SELL-2",
            side=OrderSide.SELL,
            intent=OrderIntent.STOP_LOSS,
            quantity=sellable_quantity,
            limit_price=98.0,
            attempt_group_id="exit-group",
            attempt_number=1,
        )
    )

    assert sold.broker_order_id == "BR-PROTECTIVE-SELL"
    assert [call["side"] for call in broker.submit_calls] == [
        OrderSide.BUY,
        OrderSide.SELL,
    ]
    assert len(broker.cancel_calls) == 1


@pytest.mark.usefixtures("trading_enabled")
def test_recovery_folds_emergency_sell_correlation_into_sticky_card(tmp_path):
    gateway, broker, lease, writable = _gateway(tmp_path)
    card = TradeCardState(
        environment="PROD",
        account_no="12345678-01",
        symbol="AAPL",
        board_status=BoardStatus.OPEN_POSITION,
        position_runtime_status=PositionRuntimeStatus.OPEN,
        broker_quantity=4,
        orderable_quantity=4,
        active_stop_price=99.0,
    )
    trade_card_repository.create_trade_card(gateway.database_engine, card)
    writable[0] = False
    broker.queue_acceptance(broker_order_id="BR-OFFLINE-SELL")

    gateway.submit_guarded(
        _request(
            lease,
            client_order_id="OFFLINE-SELL-1",
            attempt_group_id="offline-exit-group",
            attempt_number=1,
        )
    )
    # A recovered market price must not clear the sticky liquidation intent
    # while the canonical database is unavailable.
    card.exit_all_required = False
    card.board_status = BoardStatus.OPEN_POSITION

    writable[0] = True
    assert gateway.reconcile_emergency_journal() == 1
    recovered = trade_card_repository.get_trade_card(
        gateway.database_engine, "PROD", "12345678-01", "AAPL"
    )
    assert recovered.board_status == BoardStatus.SELL_ALL
    assert recovered.exit_all_required is True
    assert recovered.exit_client_order_id == "OFFLINE-SELL-1"
    assert recovered.exit_attempt_group_id == "offline-exit-group"
    assert recovered.exit_attempt_count == 1
    assert len(broker.submit_calls) == 1
