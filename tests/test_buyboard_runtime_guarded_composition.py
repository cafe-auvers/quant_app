"""End-to-end guarded composition coverage for the PR2 fourth pass."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.usefixtures("authorized_full_live")
from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

from fakes.fake_execution_broker import FakeExecutionBroker
from src.core import execution_config
from src.core.account_broker_snapshot import (
    AccountBrokerSnapshot,
    AccountHoldingSnapshot,
    SnapshotCompleteness,
)
from src.core.discovered_external_order import (
    ExternalOrderDisposition,
    new_discovered_external_order,
)
from src.core.capital_reservation import CapitalReservation
from src.core.execution_mode import ExecutionLease, ExecutionSource
from src.core.execution_order_record import (
    BrokerIdentityStatus,
    ExecutionOrderRecord,
    ExecutionOrderStatus,
)
from src.core.execution_request import (
    SubmitExecutionRequest,
    derive_execution_client_order_id,
)
from src.core.execution_ownership import ExecutionOwner, ExecutionOwnership
from src.core.execution_result import UnifiedExecutionStatus
from src.core.order_state import (
    BrokerOrderStatusSnapshot,
    OrderIntent,
    OrderSide,
    OrderStatus,
)
from src.core.trade_card_state import (
    BoardStatus,
    EntryRuntimeStatus,
    PositionRuntimeStatus,
    TradeCardState,
)
from src.risk.pre_trade import PreTradeRiskDecision
from src.services import (
    buyboard_runtime,
    capital_reservation_repository,
    discovered_external_order_repository,
    execution_order_repository,
)
from src.services import execution_command_gateway as gateway_module
from src.services import trade_card_repository
from src.services.execution_command_gateway import ExecutionCommandGateway
from src.services.execution_lease_protocol import FakeExecutionLeaseProtocol
from src.services.execution_order_repository import fetch_execution_order
from src.services.execution_ownership_repository import assign_ownership
from src.services.emergency_journal import EmergencyJournal, EmergencyLeaseAllowance
from src.services.mutation_budget_protocol import AllowAllMutationBudget
from src.services.discovered_external_order_repository import (
    record_discovered_external_order,
    save_discovered_external_order,
)
from src.services.realtime_market_data import QuoteSnapshot, RestPollingMarketDataService
from src.services import trading_engine as trading_engine_module
from src.risk.portfolio import PortfolioRiskLimits, PortfolioRiskManager
from src.services.account_reconciliation import (
    AccountLocalState,
    reduce_account_reconciliation,
)


LEASE = ExecutionLease(device_id="device-1", lease_token="lease-1", lease_epoch=7)
STRATEGY_ID = "orb-live-1"


def test_projected_exposure_counts_partial_buy_once_with_linked_reservation():
    card = _card()
    reservation = CapitalReservation.create(
        environment="PROD",
        account_no="1",
        symbol="AAPL",
        attempt_group_id="attempt-1",
        requested_notional=1_000.0,
        projected_open_risk=50.0,
    )
    reservation.consume(600.0)
    order = ExecutionOrderRecord(
        environment="PROD",
        account_no="1",
        symbol="AAPL",
        side=OrderSide.BUY,
        intent=OrderIntent.ENTRY,
        client_order_id="entry-1",
        broker_order_id="broker-1",
        submitted_quantity=10,
        submitted_limit_price=100.0,
        status=ExecutionOrderStatus.PARTIALLY_FILLED,
        filled_quantity=6,
        remaining_quantity=4,
        broker_identity_status=BrokerIdentityStatus.EXACT,
        capital_reservation_id=reservation.reservation_id,
    )

    exposures = buyboard_runtime._portfolio_projected_exposures(
        cards=(card,),
        execution_orders=(order,),
        active_reservations=(reservation,),
    )

    assert len(exposures) == 1
    assert exposures[0].source == "PENDING_BUY"
    assert exposures[0].gross_notional_usd == pytest.approx(400.0)
    assert exposures[0].open_risk_usd == pytest.approx(20.0)
    assert exposures[0].reservation_id == reservation.reservation_id


def _card(symbol: str = "AAPL", **overrides) -> TradeCardState:
    values = dict(
        environment="PROD",
        account_no="1",
        symbol=symbol,
        board_status=BoardStatus.BUY_TODAY,
        entry_runtime_status=EntryRuntimeStatus.EXECUTE_READY,
        # This helper bypasses the normal current-session ORB bridge, so it
        # must supply the same complete, internally consistent geometry that
        # production requires before TradingEngine may cross the broker gate.
        breakout_price=99.0,
        entry_trigger=100.0,
        entry_orb_high=100.0,
        entry_orb_low=95.0,
        entry_orb_window="5m",
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


def _make_runtime(
    tmp_path,
    monkeypatch,
    *,
    broker=None,
    buying_power=100_000.0,
    database_writable_provider=None,
    emergency_journal=None,
    emergency_lease_allowance=None,
    portfolio_risk_manager=None,
):
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
        database_writable_provider=database_writable_provider,
        emergency_journal=emergency_journal,
        emergency_lease_allowance=emergency_lease_allowance,
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
        portfolio_cards_provider=lambda environment, account_no: trade_card_repository.list_trade_cards(
            engine,
            environment=environment,
            account_no=account_no,
            raise_on_error=True,
        ),
        portfolio_risk_manager=portfolio_risk_manager,
        portfolio_orders_provider=lambda environment, account_no: execution_order_repository.list_execution_orders_for_account(
            engine,
            environment=environment,
            account_no=account_no,
        ),
        portfolio_reservations_provider=lambda environment, account_no: capital_reservation_repository.list_active_reservations(
            engine,
            environment=environment,
            account_no=account_no,
        ),
        portfolio_external_orders_provider=lambda environment, account_no: discovered_external_order_repository.list_discovered_external_orders_for_account(
            engine,
            environment=environment,
            account_no=account_no,
        ),
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


def _submit_commands(engine):
    with engine.connect() as conn:
        return conn.execute(
            text(
                "SELECT idempotency_key, status FROM execution_commands "
                "WHERE command_type = 'submit' ORDER BY id"
            )
        ).fetchall()


def _submit_guarded_entry(runtime, broker, market_data, card) -> None:
    broker.queue_acceptance(broker_order_id=f"B-{card.symbol}-ENTRY")
    market_data.subscribe([card.symbol])
    market_data.poll_once()
    runtime.trading_engine.run_heartbeat([card])
    assert card.board_status == BoardStatus.ENTRY_PENDING


@pytest.mark.usefixtures("trading_enabled")
def test_pending_order_reads_are_coalesced_without_slowing_engine_heartbeat(
    tmp_path, monkeypatch
):
    clock = [100.0]
    monkeypatch.setattr(buyboard_runtime, "monotonic", lambda: clock[0])
    runtime, broker, _gateway, engine, market_data = _make_runtime(
        tmp_path, monkeypatch
    )
    card = _persist_owned_card(engine, _card())
    _submit_guarded_entry(runtime, broker, market_data, card)

    list_calls = []
    fetch_calls = []
    real_list = buyboard_runtime.list_execution_orders_for_card
    real_fetch = buyboard_runtime.fetch_execution_order
    monkeypatch.setattr(
        buyboard_runtime,
        "list_execution_orders_for_card",
        lambda *args, **kwargs: list_calls.append(True)
        or real_list(*args, **kwargs),
    )
    monkeypatch.setattr(
        buyboard_runtime,
        "fetch_execution_order",
        lambda *args, **kwargs: fetch_calls.append(True)
        or real_fetch(*args, **kwargs),
    )

    lookup = runtime.trading_engine._entry_deadline_lookup
    clock[0] += execution_config.PENDING_ORDER_RECONCILIATION_SECONDS + 0.1
    first = lookup.find_open_entry_order(card)
    assert first is not None
    assert lookup.reconcile_order(first).client_order_id == first.client_order_id
    second = lookup.find_open_entry_order(card)
    assert second is not None
    assert lookup.reconcile_order(second).client_order_id == second.client_order_id

    # Every heartbeat stage shares the same canonical snapshot. The next
    # database read becomes due only after the two-second pending interval.
    assert len(list_calls) == 1
    assert fetch_calls == []

    clock[0] += execution_config.PENDING_ORDER_RECONCILIATION_SECONDS / 2
    within_interval = lookup.find_open_entry_order(card)
    assert within_interval is not None
    lookup.reconcile_order(within_interval)
    assert len(list_calls) == 1

    clock[0] += execution_config.PENDING_ORDER_RECONCILIATION_SECONDS / 2 + 0.1
    assert lookup.find_open_entry_order(card) is not None
    assert len(list_calls) == 2


@pytest.mark.usefixtures("trading_enabled")
def test_unknown_submission_keeps_one_second_order_read_cadence(
    tmp_path, monkeypatch
):
    clock = [100.0]
    monkeypatch.setattr(buyboard_runtime, "monotonic", lambda: clock[0])
    runtime, broker, _gateway, engine, market_data = _make_runtime(
        tmp_path, monkeypatch
    )
    card = _persist_owned_card(engine, _card())
    broker.queue_timeout()
    market_data.subscribe([card.symbol])
    market_data.poll_once()
    runtime.trading_engine.run_heartbeat([card])
    assert card.entry_submission_unresolved is True

    list_calls = []
    real_list = buyboard_runtime.list_execution_orders_for_card
    monkeypatch.setattr(
        buyboard_runtime,
        "list_execution_orders_for_card",
        lambda *args, **kwargs: list_calls.append(True)
        or real_list(*args, **kwargs),
    )

    lookup = runtime.trading_engine._entry_deadline_lookup
    clock[0] += execution_config.UNKNOWN_ORDER_RECONCILIATION_SECONDS + 0.1
    first = lookup.find_open_entry_order(card)
    assert first is not None
    lookup.reconcile_order(first)
    assert len(list_calls) == 1
    clock[0] += execution_config.UNKNOWN_ORDER_RECONCILIATION_SECONDS / 2
    within_interval = lookup.find_open_entry_order(card)
    assert within_interval is not None
    lookup.reconcile_order(within_interval)
    assert len(list_calls) == 1
    clock[0] += execution_config.UNKNOWN_ORDER_RECONCILIATION_SECONDS / 2 + 0.1
    assert lookup.find_open_entry_order(card) is not None
    assert len(list_calls) == 2


@pytest.mark.usefixtures("trading_enabled")
def test_runtime_pre_broker_abort_retries_with_attempt_two_and_a_fresh_identity(
    tmp_path, monkeypatch
):
    runtime, broker, gateway, engine, market_data = _make_runtime(
        tmp_path, monkeypatch
    )
    card = _persist_owned_card(engine, _card())
    raced_external = {}
    lease_checks = {"count": 0}
    real_require_lease = gateway._require_verified_lease

    def insert_fence_at_final_gate(lease):
        lease_checks["count"] += 1
        real_require_lease(lease)
        if lease_checks["count"] == 2:
            external = new_discovered_external_order(
                environment="PROD",
                account_no="1",
                symbol="AAPL",
                side=OrderSide.BUY,
                broker_order_id="B-RACED-ENTRY",
                broker_status=ExecutionOrderStatus.WORKING,
            )
            raced_external["order"] = record_discovered_external_order(
                engine, external
            )

    monkeypatch.setattr(
        gateway, "_require_verified_lease", insert_fence_at_final_gate
    )
    broker.queue_acceptance(broker_order_id="B-ATTEMPT-2")
    market_data.subscribe([card.symbol])
    market_data.poll_once()

    runtime.trading_engine.run_heartbeat([card])

    first_commands = _submit_commands(engine)
    assert broker.submit_calls == []
    assert len(first_commands) == 1
    assert first_commands[0].status == "PRE_BROKER_ABORTED"
    first_id = first_commands[0].idempotency_key.removeprefix("SUBMIT:")
    first_order = fetch_execution_order(engine, first_id)
    assert first_order.status == ExecutionOrderStatus.CANCELLED_LOCALLY
    assert first_order.attempt_number == 1
    assert card.entry_client_order_id == ""
    assert card.entry_attempt_count == 1
    assert card.next_retry_at is not None

    # Mirror the worker's normal changed-card persistence before the next
    # heartbeat/restart boundary.
    trade_card_repository.update_trade_card(
        engine, card, expected_version=card.version
    )

    external = raced_external["order"]
    expected_version = external.version
    external.broker_status = ExecutionOrderStatus.CANCELLED
    external.disposition = ExternalOrderDisposition.DISMISSED_TERMINAL
    save_discovered_external_order(
        engine, external, expected_version=expected_version
    )

    after_cooldown = card.next_retry_at + timedelta(milliseconds=1)
    runtime.entry_attempt_manager._clock = lambda: after_cooldown
    runtime.trading_engine._clock = lambda: after_cooldown
    market_data.poll_once()

    runtime.trading_engine.run_heartbeat([card])

    commands = _submit_commands(engine)
    assert len(commands) == 2
    second_id = commands[1].idempotency_key.removeprefix("SUBMIT:")
    assert second_id != first_id
    assert commands[1].status == "ACKNOWLEDGED"
    assert len(broker.submit_calls) == 1
    second_order = fetch_execution_order(engine, second_id)
    assert second_order.attempt_group_id == first_order.attempt_group_id
    assert second_order.attempt_number == 2
    assert card.entry_client_order_id == second_id
    assert card.entry_attempt_count == 2


@pytest.mark.usefixtures("trading_enabled")
def test_terminal_sell_all_restart_consumes_attempt_before_guarded_retry(
    tmp_path, monkeypatch
):
    runtime, broker, gateway, engine, market_data = _make_runtime(
        tmp_path, monkeypatch
    )
    group_id = "G-SELL-ALL-RESTART"
    first_id = derive_execution_client_order_id(
        attempt_group_id=group_id,
        attempt_number=1,
        environment="PROD",
        account_no="1",
        symbol="AAPL",
        intent=OrderIntent.MANUAL_EXIT,
    )
    crashed_card = _persist_owned_card(
        engine,
        _card(
            board_status=BoardStatus.SELL_ALL,
            position_runtime_status=PositionRuntimeStatus.LIQUIDATING,
            broker_quantity=10,
            orderable_quantity=10,
            exit_all_required=True,
            exit_attempt_group_id=group_id,
            exit_client_order_id=first_id,
            exit_pending_attempt_number=1,
            exit_attempt_count=0,
        ),
    )
    first_order = ExecutionOrderRecord(
        environment="PROD",
        account_no="1",
        symbol="AAPL",
        side=OrderSide.SELL,
        intent=OrderIntent.MANUAL_EXIT,
        client_order_id=first_id,
        broker_order_id="B-EXIT-1",
        attempt_group_id=group_id,
        attempt_number=1,
        submitted_quantity=10,
        remaining_quantity=10,
        submitted_limit_price=99.0,
        status=ExecutionOrderStatus.ACKNOWLEDGED,
        broker_identity_status=BrokerIdentityStatus.EXACT,
    )
    terminal = BrokerOrderStatusSnapshot(
        environment="PROD",
        account_no="1",
        symbol="AAPL",
        broker_order_id="B-EXIT-1",
        side=OrderSide.SELL,
        status=OrderStatus.CANCELLED,
        quantity_requested=10,
        remaining_quantity=10,
    )

    plan = reduce_account_reconciliation(
        AccountBrokerSnapshot(
            environment="PROD",
            account_no="1",
            completeness=SnapshotCompleteness(
                holdings_complete=True,
                open_orders_complete=True,
                history_complete=True,
                reserved_orders_complete=True,
                account_balance_complete=True,
            ),
            orders=(terminal,),
            holdings=(
                # Shares remain after the terminal attempt, so this is one
                # continuing Sell All chain rather than a flat lifecycle.
                AccountHoldingSnapshot(
                    symbol="AAPL",
                    quantity=10,
                    average_price=100.0,
                    sellable_quantity=10,
                ),
            ),
            observed_at=datetime.now(timezone.utc),
        ),
        AccountLocalState(cards=(crashed_card,), execution_orders=(first_order,)),
    )

    recovered = plan.card_updates[0]
    assert recovered.exit_attempt_group_id == group_id
    assert recovered.exit_attempt_count == 1
    assert recovered.exit_pending_attempt_number == 0
    assert recovered.exit_client_order_id == ""
    trade_card_repository.update_trade_card(
        engine, recovered, expected_version=recovered.version
    )

    market_data.subscribe([recovered.symbol])
    market_data.poll_once()
    broker.queue_acceptance(broker_order_id="B-EXIT-2")
    result = runtime.trading_engine._position_callbacks.submit_sell_order(
        environment="PROD",
        account_no="1",
        symbol="AAPL",
        quantity=10,
        reason="sell_all_retry",
        trade_card=recovered,
    )

    assert result.status == UnifiedExecutionStatus.ACKNOWLEDGED
    assert recovered.exit_client_order_id != first_id
    second_order = fetch_execution_order(engine, recovered.exit_client_order_id)
    assert second_order.attempt_group_id == group_id
    assert second_order.attempt_number == 2
    assert len(broker.submit_calls) == 1


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
def test_canonical_portfolio_position_limit_rejects_entry_before_broker(
    tmp_path, monkeypatch
):
    manager = PortfolioRiskManager(
        PortfolioRiskLimits(max_simultaneous_positions=1)
    )
    runtime, broker, gateway, engine, market_data = _make_runtime(
        tmp_path,
        monkeypatch,
        portfolio_risk_manager=manager,
    )
    entry = _persist_owned_card(engine, _card())
    existing = _card(
        symbol="MSFT",
        board_status=BoardStatus.OPEN_POSITION,
        entry_runtime_status=None,
        broker_quantity=10,
        orderable_quantity=10,
        average_entry_price=100.0,
        active_stop_price=90.0,
    )
    trade_card_repository.create_trade_card(engine, existing)
    market_data.subscribe([entry.symbol])
    market_data.poll_once()

    runtime.trading_engine.run_heartbeat([entry, existing])

    assert broker.submit_calls == []
    assert capital_reservation_repository.list_active_reservations(
        engine, environment="PROD", account_no="1"
    ) == []
    assert entry.entry_runtime_status == EntryRuntimeStatus.RETRY_COOLDOWN


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
def test_runtime_outage_cancels_completion_buy_then_submits_one_sell(
    tmp_path, monkeypatch
):
    class PositionBroker(FakeExecutionBroker):
        def get_positions(self, **_kwargs):
            return {
                "overseas": {
                    "holdings": [
                        {
                            "symbol": "AAPL",
                            "quantity": 4,
                            "orderable_quantity": 4,
                            "average_price": 100.0,
                        }
                    ]
                }
            }

    writable = [True]
    broker = PositionBroker()
    runtime, broker, gateway, engine, market_data = _make_runtime(
        tmp_path,
        monkeypatch,
        broker=broker,
        database_writable_provider=lambda: writable[0],
        emergency_journal=EmergencyJournal(tmp_path / "emergency.jsonl"),
        emergency_lease_allowance=EmergencyLeaseAllowance(max_seconds=30),
    )
    card = _persist_owned_card(
        engine,
        _card(
            board_status=BoardStatus.OPEN_POSITION,
            entry_runtime_status=EntryRuntimeStatus.SESSION_COMPLETE,
            position_runtime_status=PositionRuntimeStatus.OPEN,
            broker_quantity=4,
            orderable_quantity=4,
            entry_remaining_target_quantity=2,
            entry_attempt_group_id="entry-group",
            entry_attempt_count=1,
            entry_pending_attempt_number=1,
            entry_client_order_id="COMPLETION-BUY-1",
        ),
    )
    broker.queue_acceptance(broker_order_id="BR-COMPLETION-BUY")
    gateway.submit_guarded(
        SubmitExecutionRequest(
            client_order_id="COMPLETION-BUY-1",
            environment="PROD",
            account_no="1",
            symbol="AAPL",
            side=OrderSide.BUY,
            intent=OrderIntent.ENTRY,
            quantity=2,
            limit_price=100.0,
            attempt_group_id="entry-group",
            attempt_number=1,
            lease=LEASE,
            source=ExecutionSource.KANBAN_BOARD,
                strategy_instance_id=STRATEGY_ID,
                pre_trade_risk_decision=PreTradeRiskDecision.approve(
                    environment="PROD",
                    account_no="1",
                    symbol="AAPL",
                    side=OrderSide.BUY,
                    intent=OrderIntent.ENTRY,
                    quantity=2,
                    reference_price=100.0,
                    exchange="NASD",
                    execution_policy="REGULAR_LIMIT",
                    strategy_id="ORB",
                    plan_id="TEST:COMPLETION:AAPL",
                ),
                risk_strategy_id="ORB",
                risk_plan_id="TEST:COMPLETION:AAPL",
            )
    )
    assert runtime.trading_engine._entry_deadline_lookup.find_open_entry_order(card)
    market_data.subscribe([card.symbol])
    market_data.poll_once()

    writable[0] = False
    broker.queue_cancel_confirmed()
    runtime.trading_engine._initiate_sell_all(card)

    assert len(broker.cancel_calls) == 1
    assert broker.cancel_calls[0]["side"] == "BUY"
    assert card.board_status == BoardStatus.SELL_ALL

    broker.queue_acceptance(broker_order_id="BR-EMERGENCY-SELL")
    runtime.trading_engine.run_heartbeat([card])

    assert len(broker.submit_calls) == 2, (
        card.last_exit_error,
        card.entry_cancel_in_flight,
        card.entry_client_order_id,
        card.exit_client_order_id,
        card.next_exit_retry_at,
    )
    assert broker.submit_calls[-1]["side"] == OrderSide.SELL
    assert card.exit_client_order_id


@pytest.mark.usefixtures("trading_enabled")
def test_immediate_outage_after_canonical_sell_ack_does_not_submit_again(
    tmp_path, monkeypatch
):
    class PositionBroker(FakeExecutionBroker):
        def get_positions(self, **_kwargs):
            return {
                "overseas": {
                    "holdings": [
                        {
                            "symbol": "AAPL",
                            "quantity": 4,
                            "orderable_quantity": 4,
                            "average_price": 100.0,
                        }
                    ]
                }
            }

    writable = [True]
    runtime, broker, gateway, engine, market_data = _make_runtime(
        tmp_path,
        monkeypatch,
        broker=PositionBroker(),
        database_writable_provider=lambda: writable[0],
        emergency_journal=EmergencyJournal(tmp_path / "emergency.jsonl"),
        emergency_lease_allowance=EmergencyLeaseAllowance(max_seconds=30),
    )
    card = _persist_owned_card(
        engine,
        _card(
            board_status=BoardStatus.SELL_ALL,
            entry_runtime_status=EntryRuntimeStatus.SESSION_COMPLETE,
            position_runtime_status=PositionRuntimeStatus.LIQUIDATING,
            broker_quantity=4,
            orderable_quantity=4,
            exit_all_required=True,
        ),
    )
    market_data.subscribe([card.symbol])
    market_data.poll_once()
    broker.queue_acceptance(broker_order_id="BR-CANONICAL-SELL")

    runtime.trading_engine.run_heartbeat([card])

    assert len(broker.submit_calls) == 1
    canonical_client_order_id = card.exit_client_order_id
    assert canonical_client_order_id
    assert gateway.cached_execution_record(canonical_client_order_id) is not None

    # The DB disappears before any normal order lookup/reconciliation.  The
    # just-ACKed command must remain an active fence for every outage tick.
    writable[0] = False
    runtime.trading_engine.run_heartbeat([card])
    runtime.trading_engine.run_heartbeat([card])

    assert len(broker.submit_calls) == 1
    assert card.exit_client_order_id == canonical_client_order_id


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
    manager = PortfolioRiskManager(
        PortfolioRiskLimits(
            max_simultaneous_positions=1,
            max_total_open_risk_fraction=0.0001,
            max_gross_notional_fraction=0.0001,
        )
    )
    runtime, broker, gateway, engine, market_data = _make_runtime(
        tmp_path, monkeypatch, portfolio_risk_manager=manager
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
def test_guarded_sell_all_ttl_reprices_use_fresh_ids_and_consume_emergency_cap(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(execution_config, "EMERGENCY_EXIT_MAX_REPRICE_ATTEMPTS", 3)
    runtime, broker, gateway, engine, market_data = _make_runtime(
        tmp_path, monkeypatch
    )
    monkeypatch.setattr(
        broker,
        "get_positions",
        lambda **kwargs: {
            "overseas": {
                "holdings": [
                    {
                        "symbol": "AAPL",
                        "quantity": 10,
                        "orderable_quantity": 10,
                    }
                ]
            }
        },
    )
    card = _persist_owned_card(
        engine,
        _card(
            board_status=BoardStatus.SELL_ALL,
            position_runtime_status=PositionRuntimeStatus.LIQUIDATING,
            broker_quantity=10,
            orderable_quantity=10,
            exit_all_required=True,
            market_data_last_trusted_price=100.0,
        ),
    )
    market_data.subscribe([card.symbol])
    market_data.poll_once()
    # Force bounded last-trusted-price collars so every accepted reprice must
    # consume the same persisted counter that limits emergency attempts.
    market_data._connected = False
    current_time = [datetime.now(timezone.utc)]
    runtime.trading_engine._clock = lambda: current_time[0]

    broker.queue_acceptance(broker_order_id="B-EXIT-1")
    runtime.trading_engine.run_heartbeat([card])

    first_id = card.exit_client_order_id
    assert first_id
    assert card.exit_attempt_count == 1
    assert len(broker.submit_calls) == 1

    submitted_ids = [first_id]
    for attempt_number in (2, 3):
        current_time[0] += timedelta(
            seconds=execution_config.SELL_ALL_ATTEMPT_TTL_SECONDS + 1
        )
        broker.queue_cancel_confirmed()
        broker.queue_acceptance(broker_order_id=f"B-EXIT-{attempt_number}")

        runtime.trading_engine.run_heartbeat([card])

        submitted_ids.append(card.exit_client_order_id)
        assert card.exit_attempt_count == attempt_number
        assert len(broker.submit_calls) == attempt_number

    assert len(set(submitted_ids)) == 3
    records = [fetch_execution_order(engine, client_id) for client_id in submitted_ids]
    assert [record.attempt_number for record in records] == [1, 2, 3]
    assert len({record.attempt_group_id for record in records}) == 1

    # Cancelling attempt 3 leaves shares, but no fourth emergency submission
    # may cross the broker boundary after the configured cap is consumed.
    current_time[0] += timedelta(
        seconds=execution_config.SELL_ALL_ATTEMPT_TTL_SECONDS + 1
    )
    broker.queue_cancel_confirmed()
    runtime.trading_engine.run_heartbeat([card])

    assert len(broker.submit_calls) == 3
    assert card.exit_attempt_count == 3
    assert card.exit_client_order_id == ""
    assert "retry limit reached" in card.last_exit_error

    current_time[0] = card.next_exit_retry_at + timedelta(milliseconds=1)
    runtime.trading_engine.run_heartbeat([card])
    assert len(broker.submit_calls) == 3
    assert card.exit_attempt_count == 3


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
