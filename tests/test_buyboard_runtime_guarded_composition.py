"""End-to-end guarded composition coverage for the PR2 fourth pass."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

from fakes.fake_execution_broker import FakeExecutionBroker
from src.core import execution_config
from src.core.account_broker_snapshot import AccountBrokerSnapshot, SnapshotCompleteness
from src.core.execution_mode import ExecutionLease
from src.core.execution_order_record import (
    BrokerIdentityStatus,
    ExecutionOrderRecord,
    ExecutionOrderStatus,
)
from src.core.execution_ownership import ExecutionOwner, ExecutionOwnership
from src.core.execution_result import UnifiedExecutionStatus
from src.core.order_state import (
    BrokerOrderStatusSnapshot,
    OrderIntent,
    OrderSide,
    OrderStatus,
)
from src.core.trade_card_state import BoardStatus, EntryRuntimeStatus, TradeCardState
from src.services import buyboard_runtime, capital_reservation_repository
from src.services import execution_command_gateway as gateway_module
from src.services import trade_card_repository
from src.services.execution_command_gateway import ExecutionCommandGateway
from src.services.execution_lease_protocol import FakeExecutionLeaseProtocol
from src.services.execution_order_repository import fetch_execution_order
from src.services.execution_ownership_repository import assign_ownership
from src.services.mutation_budget_protocol import AllowAllMutationBudget
from src.services.realtime_market_data import QuoteSnapshot, RestPollingMarketDataService
from src.services import trading_engine as trading_engine_module
from src.services.account_reconciliation import (
    AccountLocalState,
    reduce_account_reconciliation,
)


LEASE = ExecutionLease(device_id="device-1", lease_token="lease-1", lease_epoch=7)
STRATEGY_ID = "orb-live-1"


def _card(symbol: str = "AAPL", **overrides) -> TradeCardState:
    values = dict(
        environment="PROD",
        account_no="1",
        symbol=symbol,
        board_status=BoardStatus.BUY_TODAY,
        entry_runtime_status=EntryRuntimeStatus.EXECUTE_READY,
        entry_trigger=100.0,
        entry_orb_low=95.0,
        stop_adr=30.0,
        risk_percent=0.01,
        selected_orb_window="5m",
        planned_quantity=100,
        target_position_quantity=100,
    )
    values.update(overrides)
    return TradeCardState(**values)


def _enable_guarded(monkeypatch) -> None:
    monkeypatch.setattr(execution_config, "is_buyboard_engine_enabled", lambda: True)
    monkeypatch.setattr(
        trading_engine_module, "is_buyboard_engine_enabled", lambda: True
    )
    monkeypatch.setattr(buyboard_runtime, "is_regular_session_open", lambda: True)
    monkeypatch.setattr(buyboard_runtime, "_eod_window_reached", lambda: False)


def _make_runtime(tmp_path, monkeypatch, *, broker=None, buying_power=100_000.0):
    _enable_guarded(monkeypatch)
    engine = create_engine(
        f"sqlite:///{tmp_path / 'composition.db'}", future=True, poolclass=NullPool
    )
    broker = broker or FakeExecutionBroker()
    gateway = ExecutionCommandGateway(
        real_broker=broker,
        engine=engine,
        mode_override=True,
        lease_protocol=FakeExecutionLeaseProtocol(current=LEASE),
        mutation_budget=AllowAllMutationBudget(),
        buying_power_provider=lambda environment, account_no: buying_power,
    )

    def lookup(environment, account_no, symbol):
        return trade_card_repository.get_trade_card(
            engine, environment, account_no, symbol
        )

    def persist(card):
        trade_card_repository.update_trade_card(
            engine, card, expected_version=card.version
        )

    market_data = RestPollingMarketDataService(
        quote_fetcher=lambda symbol: QuoteSnapshot(
            symbol=symbol, last_price=100.0, bid=99.5, ask=100.0
        )
    )
    runtime = buyboard_runtime.build_buyboard_runtime(
        buying_power_provider=lambda environment, account_no: buying_power,
        account_equity_provider=lambda environment, account_no: 100_000.0,
        card_lookup=lookup,
        broker=gateway,
        execution_lease=LEASE,
        strategy_instance_id=STRATEGY_ID,
        persist_card_before_execution=persist,
        market_data=market_data,
    )
    return runtime, broker, gateway, engine, market_data


def _persist_owned_card(engine, card: TradeCardState) -> TradeCardState:
    trade_card_repository.create_trade_card(engine, card)
    assign_ownership(
        engine,
        ExecutionOwnership(
            environment=card.environment,
            account_no=card.account_no,
            symbol=card.symbol,
            owner=ExecutionOwner.KANBAN,
            strategy_instance_id=STRATEGY_ID,
        ),
    )
    return card


def _cancel_commands(engine):
    with engine.connect() as conn:
        return conn.execute(
            text(
                "SELECT idempotency_key, status FROM execution_commands "
                "WHERE command_type = 'cancel' ORDER BY id"
            )
        ).fetchall()


def _submit_guarded_entry(runtime, broker, market_data, card) -> None:
    broker.queue_acceptance(broker_order_id=f"B-{card.symbol}-ENTRY")
    market_data.subscribe([card.symbol])
    market_data.poll_once()
    runtime.trading_engine.run_heartbeat([card])
    assert card.board_status == BoardStatus.ENTRY_PENDING


@pytest.mark.usefixtures("trading_enabled")
def test_closed_cycle_retires_attempt_group_before_next_cycle_and_old_history_is_ignored(
    tmp_path, monkeypatch
):
    runtime, broker, gateway, engine, market_data = _make_runtime(
        tmp_path, monkeypatch
    )
    cycle_one_group = "CYCLE-1"
    cycle_one_client_id = "CYCLE-1-ENTRY"
    card = _persist_owned_card(
        engine,
        _card(
            board_status=BoardStatus.OPEN_POSITION,
            entry_attempt_group_id=cycle_one_group,
            entry_client_order_id=cycle_one_client_id,
            exit_attempt_group_id="CYCLE-1-EXIT",
            broker_quantity=0,
            orderable_quantity=0,
        ),
    )

    runtime.position_manager.confirm_flat(card)
    assert card.entry_attempt_group_id == ""
    assert card.exit_attempt_group_id == ""

    card.board_status = BoardStatus.BUYLIST
    card.board_status = BoardStatus.BUY_TODAY
    runtime.trading_engine._prepare_entry_attempt(card)
    cycle_two_group = card.entry_attempt_group_id
    assert cycle_two_group
    assert cycle_two_group != cycle_one_group
    assert card.entry_client_order_id != cycle_one_client_id

    historical_order = ExecutionOrderRecord(
        environment="PROD",
        account_no="1",
        symbol="AAPL",
        side=OrderSide.BUY,
        intent=OrderIntent.ENTRY,
        client_order_id=cycle_one_client_id,
        broker_order_id="B-CYCLE-1",
        attempt_group_id=cycle_one_group,
        submitted_quantity=100,
        remaining_quantity=100,
        status=ExecutionOrderStatus.WORKING,
        broker_identity_status=BrokerIdentityStatus.EXACT,
    )
    historical_fill = BrokerOrderStatusSnapshot(
        environment="PROD",
        account_no="1",
        symbol="AAPL",
        broker_order_id="B-CYCLE-1",
        side=OrderSide.BUY,
        status=OrderStatus.FILLED,
        quantity_requested=100,
        filled_quantity=100,
        remaining_quantity=0,
        avg_fill_price=99.0,
    )
    snapshot = AccountBrokerSnapshot(
        environment="PROD",
        account_no="1",
        completeness=SnapshotCompleteness(
            holdings_complete=True,
            open_orders_complete=True,
            history_complete=True,
            reserved_orders_complete=True,
            account_balance_complete=True,
        ),
        orders=(historical_fill,),
        observed_at=datetime.now(timezone.utc),
    )

    plan = reduce_account_reconciliation(
        snapshot,
        AccountLocalState(cards=(card,), execution_orders=(historical_order,)),
    )

    assert plan.order_updates[0].status == ExecutionOrderStatus.FILLED
    assert plan.card_updates == ()


@pytest.mark.usefixtures("trading_enabled")
def test_entry_uses_one_gateway_owned_reservation_and_common_result(
    tmp_path, monkeypatch
):
    runtime, broker, gateway, engine, market_data = _make_runtime(
        tmp_path, monkeypatch
    )
    card = _persist_owned_card(engine, _card())
    broker.queue_acceptance(broker_order_id="B-ENTRY")
    market_data.subscribe([card.symbol])
    market_data.poll_once()

    runtime.trading_engine.run_heartbeat([card])

    reservations = capital_reservation_repository.list_active_reservations(
        engine, environment="PROD", account_no="1"
    )
    assert len(reservations) == 1
    assert reservations[0].requested_notional == pytest.approx(10_000.0)
    assert len(broker.submit_calls) == 1
    assert card.board_status == BoardStatus.ENTRY_PENDING
    assert card.capital_reservation_id == reservations[0].reservation_id
    assert card.entry_client_order_id

    # The callback itself exposes one workflow result shape in guarded mode.
    persisted = fetch_execution_order(engine, card.entry_client_order_id)
    assert persisted.status == ExecutionOrderStatus.ACKNOWLEDGED


@pytest.mark.usefixtures("trading_enabled")
def test_insufficient_capital_rolls_back_the_atomic_submission(tmp_path, monkeypatch):
    runtime, broker, gateway, engine, market_data = _make_runtime(
        tmp_path, monkeypatch, buying_power=5_000.0
    )
    card = _persist_owned_card(engine, _card())
    market_data.subscribe([card.symbol])
    market_data.poll_once()

    runtime.trading_engine.run_heartbeat([card])

    assert broker.submit_calls == []
    assert capital_reservation_repository.list_active_reservations(
        engine, environment="PROD", account_no="1"
    ) == []
    assert card.entry_runtime_status == EntryRuntimeStatus.WAITING_FOR_CAPITAL


@pytest.mark.usefixtures("trading_enabled")
def test_persisted_identity_survives_restart_and_unresolved_submit_is_not_retried(
    tmp_path, monkeypatch
):
    first_broker = FakeExecutionBroker()
    runtime, broker, gateway, engine, market_data = _make_runtime(
        tmp_path, monkeypatch, broker=first_broker
    )
    card = _persist_owned_card(engine, _card())
    first_broker.queue_timeout()
    market_data.subscribe([card.symbol])
    market_data.poll_once()

    changed = runtime.trading_engine.run_heartbeat([card])
    trade_card_repository.update_trade_card(
        engine, card, expected_version=card.version
    )
    stable_id = card.entry_client_order_id
    assert stable_id
    assert card.entry_submission_unresolved is True
    assert len(first_broker.submit_calls) == 1

    reloaded = trade_card_repository.get_trade_card(engine, "PROD", "1", "AAPL")
    assert reloaded.entry_client_order_id == stable_id
    second_broker = FakeExecutionBroker()
    second_gateway = ExecutionCommandGateway(
        real_broker=second_broker,
        engine=engine,
        mode_override=True,
        lease_protocol=FakeExecutionLeaseProtocol(current=LEASE),
        mutation_budget=AllowAllMutationBudget(),
        buying_power_provider=lambda environment, account_no: 100_000.0,
    )
    second_runtime = buyboard_runtime.build_buyboard_runtime(
        buying_power_provider=lambda environment, account_no: 100_000.0,
        card_lookup=lambda environment, account_no, symbol: trade_card_repository.get_trade_card(
            engine, environment, account_no, symbol
        ),
        broker=second_gateway,
        execution_lease=LEASE,
        strategy_instance_id=STRATEGY_ID,
        persist_card_before_execution=lambda current: trade_card_repository.update_trade_card(
            engine, current, expected_version=current.version
        ),
        market_data=market_data,
    )

    second_runtime.trading_engine.run_heartbeat([reloaded])
    assert second_broker.submit_calls == []
    assert reloaded.entry_client_order_id == stable_id


@pytest.mark.usefixtures("trading_enabled")
def test_tracked_cancel_carries_full_context_to_cancel_guarded(tmp_path, monkeypatch):
    runtime, broker, gateway, engine, market_data = _make_runtime(
        tmp_path, monkeypatch
    )
    card = _persist_owned_card(engine, _card())
    broker.queue_acceptance(broker_order_id="B-CANCEL")
    market_data.subscribe([card.symbol])
    market_data.poll_once()
    runtime.trading_engine.run_heartbeat([card])

    broker.queue_cancel_confirmed()
    intent = runtime.trading_engine._position_callbacks.cancel_intent_factory(
        card, card.entry_client_order_id, "ENTRY"
    )
    runtime.trading_engine._position_callbacks.cancel_order(intent)

    assert intent.environment == "PROD"
    assert intent.account_no == "1"
    assert intent.lease == LEASE
    assert intent.strategy_instance_id == STRATEGY_ID
    assert intent.cancel_command_id == card.entry_cancel_command_id
    assert len(broker.cancel_calls) == 1
    assert fetch_execution_order(engine, card.entry_client_order_id).status == (
        ExecutionOrderStatus.CANCELLED
    )


@pytest.mark.usefixtures("trading_enabled")
def test_entry_cancel_rejection_clears_id_and_next_heartbeat_uses_a_new_one(
    tmp_path, monkeypatch
):
    runtime, broker, gateway, engine, market_data = _make_runtime(
        tmp_path, monkeypatch
    )
    card = _persist_owned_card(engine, _card())
    _submit_guarded_entry(runtime, broker, market_data, card)
    card.entry_block_reason = "cancel_requested"

    broker.queue_cancel_rejected()
    runtime.trading_engine.run_heartbeat([card])

    first_commands = _cancel_commands(engine)
    assert len(broker.cancel_calls) == 1
    assert len(first_commands) == 1
    assert first_commands[0].status == "FAILED"
    first_id = first_commands[0].idempotency_key.removeprefix("CANCEL:")
    persisted = trade_card_repository.get_trade_card(engine, "PROD", "1", "AAPL")
    assert card.entry_cancel_command_id == ""
    assert card.entry_cancel_in_flight is False
    assert persisted.entry_cancel_command_id == ""
    assert persisted.entry_cancel_in_flight is False

    broker.queue_cancel_confirmed()
    runtime.trading_engine.run_heartbeat([card])

    second_commands = _cancel_commands(engine)
    assert len(broker.cancel_calls) == 2
    assert len(second_commands) == 2
    assert card.entry_cancel_command_id
    assert card.entry_cancel_command_id != first_id
    assert second_commands[1].idempotency_key == f"CANCEL:{card.entry_cancel_command_id}"


@pytest.mark.usefixtures("trading_enabled")
def test_exit_cancel_rejection_clears_id_and_next_heartbeat_uses_a_new_one(
    tmp_path, monkeypatch
):
    runtime, broker, gateway, engine, market_data = _make_runtime(
        tmp_path, monkeypatch
    )
    card = _persist_owned_card(
        engine,
        _card(
            board_status=BoardStatus.PARTIAL_SELL,
            pending_partial_sell_quantity=2,
            broker_quantity=10,
            orderable_quantity=10,
        ),
    )
    market_data.subscribe([card.symbol])
    market_data.poll_once()
    broker.queue_acceptance(broker_order_id="B-AAPL-EXIT")
    runtime.trading_engine._position_callbacks.submit_sell_order(
        environment="PROD",
        account_no="1",
        symbol="AAPL",
        quantity=2,
        reason="partial_sell",
        attempt_deadline_at=(
            datetime.now(timezone.utc) - timedelta(seconds=1)
        ).isoformat(),
        trade_card=card,
    )

    broker.queue_cancel_rejected()
    runtime.trading_engine.run_heartbeat([card])

    first_commands = _cancel_commands(engine)
    assert len(broker.cancel_calls) == 1
    assert len(first_commands) == 1
    assert first_commands[0].status == "FAILED"
    first_id = first_commands[0].idempotency_key.removeprefix("CANCEL:")
    persisted = trade_card_repository.get_trade_card(engine, "PROD", "1", "AAPL")
    assert card.exit_cancel_command_id == ""
    assert card.exit_cancel_in_flight is False
    assert card.exit_cancel_requested_at is None
    assert persisted.exit_cancel_command_id == ""
    assert persisted.exit_cancel_in_flight is False

    broker.queue_cancel_confirmed()
    runtime.trading_engine.run_heartbeat([card])

    second_commands = _cancel_commands(engine)
    assert len(broker.cancel_calls) == 2
    assert len(second_commands) == 2
    assert card.exit_cancel_command_id
    assert card.exit_cancel_command_id != first_id
    assert second_commands[1].idempotency_key == f"CANCEL:{card.exit_cancel_command_id}"


@pytest.mark.usefixtures("trading_enabled")
def test_ambiguous_cancel_preserves_id_and_blocks_additional_broker_calls(
    tmp_path, monkeypatch
):
    runtime, broker, gateway, engine, market_data = _make_runtime(
        tmp_path, monkeypatch
    )
    card = _persist_owned_card(engine, _card())
    _submit_guarded_entry(runtime, broker, market_data, card)
    card.entry_block_reason = "cancel_requested"

    broker.queue_cancel_timeout()
    runtime.trading_engine.run_heartbeat([card])

    cancel_id = card.entry_cancel_command_id
    persisted = trade_card_repository.get_trade_card(engine, "PROD", "1", "AAPL")
    assert cancel_id
    assert card.entry_cancel_in_flight is True
    assert persisted.entry_cancel_command_id == cancel_id
    assert persisted.entry_cancel_in_flight is True
    assert len(broker.cancel_calls) == 1

    runtime.trading_engine.run_heartbeat([card])

    persisted = trade_card_repository.get_trade_card(engine, "PROD", "1", "AAPL")
    assert len(broker.cancel_calls) == 1
    assert card.entry_cancel_command_id == cancel_id
    assert persisted.entry_cancel_command_id == cancel_id
    assert persisted.entry_cancel_in_flight is True


@pytest.mark.usefixtures("trading_enabled")
def test_partial_sell_and_sell_all_reach_submit_guarded(tmp_path, monkeypatch):
    runtime, broker, gateway, engine, market_data = _make_runtime(
        tmp_path, monkeypatch
    )
    partial = _persist_owned_card(
        engine,
        _card(
            "AAPL",
            board_status=BoardStatus.PARTIAL_SELL,
            pending_partial_sell_quantity=2,
            broker_quantity=10,
            orderable_quantity=10,
        ),
    )
    sell_all = _persist_owned_card(
        engine,
        _card(
            "MSFT",
            board_status=BoardStatus.SELL_ALL,
            broker_quantity=10,
            orderable_quantity=10,
        ),
    )
    market_data.subscribe(["AAPL", "MSFT"])
    market_data.poll_once()
    broker.queue_acceptance(broker_order_id="B-PARTIAL")
    broker.queue_acceptance(broker_order_id="B-ALL")

    partial_result = runtime.trading_engine._position_callbacks.submit_sell_order(
        environment="PROD",
        account_no="1",
        symbol="AAPL",
        quantity=2,
        reason="partial_sell",
        trade_card=partial,
    )
    all_result = runtime.trading_engine._position_callbacks.submit_sell_order(
        environment="PROD",
        account_no="1",
        symbol="MSFT",
        quantity=10,
        reason="sell_all_retry",
        trade_card=sell_all,
    )

    assert partial_result.status == UnifiedExecutionStatus.ACKNOWLEDGED
    assert all_result.status == UnifiedExecutionStatus.ACKNOWLEDGED
    assert [call["side"] for call in broker.submit_calls] == [
        OrderSide.SELL,
        OrderSide.SELL,
    ]


@pytest.mark.usefixtures("trading_enabled")
def test_blank_or_mismatched_strategy_identity_cannot_execute(tmp_path, monkeypatch):
    runtime, broker, gateway, engine, market_data = _make_runtime(
        tmp_path, monkeypatch
    )
    with pytest.raises(RuntimeError, match="strategy_instance_id"):
        buyboard_runtime.build_buyboard_runtime(
            buying_power_provider=lambda environment, account_no: 100_000.0,
            card_lookup=lambda environment, account_no, symbol: None,
            broker=gateway,
            execution_lease=LEASE,
            strategy_instance_id="",
            persist_card_before_execution=lambda card: None,
            market_data=market_data,
        )

    card = _persist_owned_card(engine, _card())
    assign_ownership(
        engine,
        ExecutionOwnership(
            environment="PROD",
            account_no="1",
            symbol="AAPL",
            owner=ExecutionOwner.KANBAN,
            strategy_instance_id="another-strategy",
        ),
    )
    market_data.subscribe([card.symbol])
    market_data.poll_once()
    runtime.trading_engine.run_heartbeat([card])
    assert broker.submit_calls == []


@pytest.mark.usefixtures("trading_enabled")
def test_post_broker_persistence_ambiguity_stays_unresolved_without_retry(
    tmp_path, monkeypatch
):
    runtime, broker, gateway, engine, market_data = _make_runtime(
        tmp_path, monkeypatch
    )
    card = _persist_owned_card(engine, _card())
    broker.queue_acceptance(broker_order_id="B-AMBIGUOUS")
    market_data.subscribe([card.symbol])
    market_data.poll_once()
    real_update = gateway_module.update_execution_order
    calls = {"count": 0}

    def fail_after_broker(conn, record, *, expected_version):
        calls["count"] += 1
        if calls["count"] == 2:
            raise RuntimeError("simulated post-broker persistence failure")
        return real_update(conn, record, expected_version=expected_version)

    monkeypatch.setattr(gateway_module, "update_execution_order", fail_after_broker)

    runtime.trading_engine.run_heartbeat([card])
    runtime.trading_engine.run_heartbeat([card])

    assert len(broker.submit_calls) == 1
    assert card.entry_submission_unresolved is True
    assert card.entry_runtime_status == EntryRuntimeStatus.ORDER_PENDING
