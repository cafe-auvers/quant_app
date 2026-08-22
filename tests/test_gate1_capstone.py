"""PR8-only cross-workstream deterministic Gate-1 scenarios.

These tests intentionally cross component boundaries that earlier PR slices
could only verify separately.  No real KIS endpoint, credential, clock, or
production feature flag is used.
"""
from __future__ import annotations

from datetime import datetime, timezone
import os
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt5.QtCore import QCoreApplication
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

from fakes.fake_execution_broker import FakeExecutionBroker
from gate1.contract import evaluate_post_failure_properties
from gate1.observation import (
    MutationBoundaryEvidence,
    build_gate1_system_observation,
)
from src.core.discovered_external_order import new_discovered_external_order
from src.core.board_workflow import BoardActionContext, RequestSellAll
from src.core.execution_mode import ExecutionLease, ExecutionSource
from src.core.execution_order_record import (
    AdoptedOrderPermission,
    ExecutionOrderStatus,
)
from src.core.execution_ownership import ExecutionOwner, ExecutionOwnership
from src.core.execution_request import CancelExecutionRequest, SubmitExecutionRequest
from src.core.order_recovery_state import OrderRecoveryState
from src.core.order_state import (
    BrokerOrderDiscoveryResult,
    BrokerOrderStatusSnapshot,
    OrderIntent,
    OrderSide,
    OrderStatus,
)
from src.core.runtime_readiness import RuntimeDeviceState
from src.core import execution_config
from src.core.trade_card_state import (
    BoardStatus,
    PositionRuntimeStatus,
    StopType,
    TradeCardState,
)
from src.risk.pre_trade import PreTradeRiskDecision
from src.services import state_sync
from src.services import buyboard_runtime
from src.services import execution_workflow_service as workflow
from src.services import trading_engine as trading_engine_module
from src.services import trade_card_repository as card_repo
from src.services.account_reconciliation import run_account_reconciliation_pass
from src.services.discovered_external_order_repository import (
    ActiveExternalOrderFenceError,
    adopt_external_order_in_db,
    record_discovered_external_order,
)
from src.services.emergency_journal import EmergencyJournal, EmergencyLeaseAllowance
from src.services.execution_command_gateway import (
    ExecutionCommandGateway,
    GuardedSubmissionAmbiguousError,
    GuardedSubmissionPreBrokerAbortedError,
    LeaseNotVerifiedError,
)
from src.services.execution_command_repository import DuplicateCommandError
from src.services.execution_lease_protocol import (
    DefaultExecutionLeaseProtocol,
    FakeExecutionLeaseProtocol,
)
from src.services.execution_authority import ExecutionAuthority, LeaseHandle
from src.services.execution_order_repository import (
    fetch_execution_order,
)
from src.services.execution_ownership_repository import assign_ownership
from src.services.kis_realtime_market_data import PendingMarketStateAccumulator, StopRule
from src.services.kis_request_scheduler import BudgetPolicy, KisRequestScheduler, RequestPriority
from src.services.mutation_budget_protocol import AllowAllMutationBudget
from src.services.realtime_market_data import QuoteSnapshot
from src.services.realtime_market_data import RestPollingMarketDataService
from src.services.runtime_device_state_repository import (
    confirm_standby_handoff,
    get_runtime_device_state,
)
from src.services.schema_migration import MigrationPhase, SchemaMigrationManager
from src.ui.buyboard.runtime_worker import BuyboardRuntimeWorker


ENVIRONMENT = "PROD"
ACCOUNT = "12345678-01"
SYMBOL = "AAPL"
STRATEGY = "gate1"


class CapstoneBroker(FakeExecutionBroker):
    """Scripted mutations plus mutable, complete account broker truth."""

    def __init__(self) -> None:
        super().__init__()
        self.order_snapshots: list[BrokerOrderStatusSnapshot] = []
        self.holdings: dict[str, tuple[int, float, int]] = {}
        self.submit_boundary_evidence: list[MutationBoundaryEvidence] = []
        self.cancel_boundary_evidence: list[MutationBoundaryEvidence] = []
        self.lease_snapshot_provider = lambda: None
        self.market_data_fresh_provider = lambda _symbol: None
        self.discovery_calls = 0

    def _boundary_evidence(self, symbol: str) -> MutationBoundaryEvidence:
        return MutationBoundaryEvidence(
            lease=self.lease_snapshot_provider(),
            market_data_fresh=self.market_data_fresh_provider(symbol),
        )

    def submit_order(self, **kwargs):
        self.submit_boundary_evidence.append(
            self._boundary_evidence(str(kwargs.get("symbol") or ""))
        )
        return super().submit_order(**kwargs)

    def cancel_order(self, **kwargs):
        self.cancel_boundary_evidence.append(
            self._boundary_evidence(str(kwargs.get("symbol") or ""))
        )
        return super().cancel_order(**kwargs)

    def discover_orders(self, **_kwargs):
        self.discovery_calls += 1
        return BrokerOrderDiscoveryResult(
            snapshots=list(self.order_snapshots),
            open_orders_complete=True,
            history_complete=True,
            reserved_orders_complete=True,
        )

    def get_positions(self, **_kwargs):
        return {
            "domestic": {"holdings": []},
            "overseas": {
                "holdings": [
                    {
                        "symbol": symbol,
                        "quantity": quantity,
                        "average_price": average_price,
                        "orderable_quantity": sellable,
                    }
                    for symbol, (quantity, average_price, sellable) in sorted(
                        self.holdings.items()
                    )
                ]
            },
        }


@pytest.fixture(autouse=True)
def _isolate_trade_card_snapshot(monkeypatch, tmp_path):
    monkeypatch.setattr(
        card_repo, "LOCAL_TRADE_CARDS_FILE", tmp_path / "gate1-trade-cards.json"
    )


def _engine(tmp_path, name: str):
    return create_engine(
        f"sqlite:///{tmp_path / name}", future=True, poolclass=NullPool
    )


def _assign_kanban(engine) -> None:
    assign_ownership(
        engine,
        ExecutionOwnership(
            environment=ENVIRONMENT,
            account_no=ACCOUNT,
            symbol=SYMBOL,
            owner=ExecutionOwner.KANBAN,
            strategy_instance_id=STRATEGY,
        ),
    )


def _gateway(
    engine,
    broker,
    lease_protocol,
    *,
    mutation_budget=None,
    journal=None,
    writable_provider=None,
    market_data=None,
):
    _assign_kanban(engine)
    if isinstance(broker, CapstoneBroker):
        if isinstance(lease_protocol, FakeExecutionLeaseProtocol):
            broker.lease_snapshot_provider = lambda: lease_protocol.current
        else:
            def current_durable_lease():
                owner = state_sync.get_main_device(engine)
                if not owner.success or owner.main_device is None:
                    return None
                return _lease(owner.main_device)

            broker.lease_snapshot_provider = current_durable_lease
        broker.market_data_fresh_provider = (
            (lambda symbol: market_data.is_symbol_execution_ready(symbol))
            if market_data is not None
            else (lambda _symbol: None)
        )
    return ExecutionCommandGateway(
        real_broker=broker,
        engine=engine,
        mode_override=True,
        lease_protocol=lease_protocol,
        mutation_budget=mutation_budget or AllowAllMutationBudget(),
        buying_power_provider=lambda *_: 100_000.0,
        emergency_journal=journal,
        emergency_lease_allowance=EmergencyLeaseAllowance(max_seconds=60),
        database_writable_provider=writable_provider,
    )


def _request(
    lease: ExecutionLease,
    client_order_id: str,
    *,
    side=OrderSide.SELL,
    intent=OrderIntent.STOP_LOSS,
    quantity=10,
    limit_price=99.0,
    emergency=True,
    attempt_group_id="gate1-exit",
):
    fields = dict(
        client_order_id=client_order_id,
        environment=ENVIRONMENT,
        account_no=ACCOUNT,
        symbol=SYMBOL,
        side=side,
        intent=intent,
        quantity=quantity,
        limit_price=limit_price,
        lease=lease,
        source=ExecutionSource.KANBAN_BOARD,
        strategy_instance_id=STRATEGY,
        emergency=emergency,
        attempt_group_id=attempt_group_id,
        attempt_number=1,
    )
    if side == OrderSide.BUY and intent == OrderIntent.ENTRY:
        risk_plan_id = f"GATE1:CAPSTONE:{SYMBOL}"
        fields.update(
            pre_trade_risk_decision=PreTradeRiskDecision.approve(
                environment=ENVIRONMENT,
                account_no=ACCOUNT,
                symbol=SYMBOL,
                side=side,
                intent=intent,
                quantity=quantity,
                reference_price=limit_price,
                exchange="NASD",
                execution_policy="REGULAR_LIMIT",
                strategy_id="ORB",
                plan_id=risk_plan_id,
            ),
            risk_strategy_id="ORB",
            risk_plan_id=risk_plan_id,
        )
    return SubmitExecutionRequest(**fields)


def _lease(main_device) -> ExecutionLease:
    return ExecutionLease(
        main_device.device_id,
        main_device.lease_token,
        main_device.lease_epoch,
    )


def _working_snapshot(
    client_order_id: str,
    broker_order_id: str,
    *,
    side=OrderSide.SELL,
    quantity=10,
):
    return BrokerOrderStatusSnapshot(
        environment=ENVIRONMENT,
        account_no=ACCOUNT,
        symbol=SYMBOL,
        broker_order_id=broker_order_id,
        client_order_id=client_order_id,
        side=side,
        status=OrderStatus.WORKING,
        quantity_requested=quantity,
        remaining_quantity=quantity,
        limit_price=99.0,
    )


def _assert_gate1(engine, broker) -> None:
    observation = build_gate1_system_observation(
        engine=engine,
        broker=broker,
        environment=ENVIRONMENT,
        account_no=ACCOUNT,
    )
    assert evaluate_post_failure_properties(observation) == ()


@pytest.mark.usefixtures("trading_enabled")
def test_gate1_open_position_handoff_reconciles_before_transfer_and_rejects_old_device(
    tmp_path, monkeypatch,
):
    _qt_app = QCoreApplication.instance() or QCoreApplication([])
    monkeypatch.setattr(execution_config, "is_buyboard_engine_enabled", lambda: True)
    monkeypatch.setattr(
        trading_engine_module, "is_buyboard_engine_enabled", lambda: True
    )
    monkeypatch.setattr(buyboard_runtime, "is_regular_session_open", lambda: True)
    monkeypatch.setattr(buyboard_runtime, "_eod_window_reached", lambda: False)
    engine = _engine(tmp_path, "open-position-handoff.db")
    old_role = state_sync.LocalDeviceRole("pc-old", "pc-old", True)
    new_role = state_sync.LocalDeviceRole("pc-new", "pc-new", False)
    old_claim = state_sync.claim_main_device(engine, old_role)
    assert old_claim.success and old_claim.main_device is not None
    old_lease = _lease(old_claim.main_device)

    open_card = card_repo.create_trade_card(
        engine,
        TradeCardState(
            environment=ENVIRONMENT,
            account_no=ACCOUNT,
            symbol=SYMBOL,
            board_status=BoardStatus.OPEN_POSITION,
            position_runtime_status=PositionRuntimeStatus.OPEN,
            broker_quantity=10,
            orderable_quantity=10,
            average_entry_price=100.0,
            stop_type=StopType.ORB_LOW,
            active_stop_price=95.0,
            stop_quantity=10,
        ),
    )
    requested = workflow.request_board_action(
        engine,
        RequestSellAll(
            environment=ENVIRONMENT,
            account_no=ACCOUNT,
            symbol=SYMBOL,
            expected_card_version=open_card.version,
        ),
        context=BoardActionContext(),
    ).card
    assert requested.board_status == BoardStatus.SELL_ALL
    assert requested.exit_all_required is True

    broker = CapstoneBroker()
    broker.holdings[SYMBOL] = (10, 100.0, 10)
    market_data = RestPollingMarketDataService(
        quote_fetcher=lambda symbol: QuoteSnapshot(
            symbol=symbol,
            last_price=99.0,
            bid=98.9,
            ask=99.0,
        )
    )
    market_data.subscribe([SYMBOL])
    market_data.poll_once()
    old_gateway = _gateway(
        engine,
        broker,
        DefaultExecutionLeaseProtocol(engine=engine),
        market_data=market_data,
    )
    old_runtime = buyboard_runtime.build_buyboard_runtime(
        buying_power_provider=lambda *_: 100_000.0,
        account_equity_provider=lambda *_: 100_000.0,
        card_lookup=lambda environment, account_no, symbol: card_repo.get_trade_card(
            engine, environment, account_no, symbol
        ),
        capital_reservation_engine=engine,
        broker=old_gateway,
        execution_lease=old_lease,
        lease_engine=engine,
        strategy_instance_id=STRATEGY,
        persist_card_before_execution=lambda card: card_repo.update_trade_card(
            engine, card, expected_version=card.version
        ),
        market_data=market_data,
    )
    broker.queue_acceptance(broker_order_id="B-HANDOFF-SELL")
    changed = old_runtime.trading_engine.run_heartbeat([requested])
    assert changed == [requested]
    card_repo.update_trade_card(engine, requested, expected_version=requested.version)
    assert requested.exit_client_order_id
    assert len(broker.submit_calls) == 1
    broker.order_snapshots = [
        _working_snapshot(requested.exit_client_order_id, "B-HANDOFF-SELL")
    ]

    # The pull-only production worker performs its initial reconciliation,
    # then _advance_startup_readiness performs the required final pass before
    # it is allowed to publish STANDBY_READY.
    standby = BuyboardRuntimeWorker(
        db_engine=engine,
        environment=ENVIRONMENT,
        account_no=ACCOUNT,
        buying_power_provider=lambda *_: 100_000.0,
        broker=broker,
        market_data=market_data,
        account_discovery=lambda: [],
        standby_only=True,
        device_id=new_role.device_id,
        hostname=new_role.hostname,
    )
    standby.runtime = buyboard_runtime.build_buyboard_runtime(
        buying_power_provider=lambda *_: 100_000.0,
        card_lookup=lambda environment, account_no, symbol: card_repo.get_trade_card(
            engine, environment, account_no, symbol
        ),
        broker=broker,
        market_data=market_data,
        observation_only=True,
    )
    standby._database_probe_completed = True
    standby._database_writable = True
    standby._accepting_commands = False
    standby.device_state = RuntimeDeviceState.STANDBY
    standby.last_market_data_drain_at = datetime.now(timezone.utc)
    standby._run_startup_reconciliation(execute_commands=False)
    discovery_calls_before_final = broker.discovery_calls
    standby._advance_startup_readiness()
    assert broker.discovery_calls > discovery_calls_before_final
    assert standby.device_state == RuntimeDeviceState.STANDBY_READY
    assert standby._accepting_commands is False

    ready = get_runtime_device_state(
        engine, device_id=new_role.device_id
    )
    assert ready is not None and ready.state == RuntimeDeviceState.STANDBY_READY
    assert confirm_standby_handoff(
        engine,
        device_id=new_role.device_id,
        readiness_generation=ready.readiness_generation,
        outgoing_lease_epoch=old_lease.lease_epoch,
    )
    assert state_sync.release_main_device(
        engine,
        old_role,
        expected_lease_token=old_lease.lease_token,
        expected_lease_epoch=old_lease.lease_epoch,
    ).success
    new_claim = state_sync.claim_main_device_if_unclaimed(
        engine,
        new_role,
        expected_standby_generation=ready.readiness_generation,
    )
    assert new_claim.success and new_claim.main_device is not None
    new_lease = _lease(new_claim.main_device)

    # Promotion uses the same production runtime readiness method. It runs
    # another final reconciliation with the command gate closed, rechecks the
    # newly acquired lease, persists ACTIVE, and only then accepts commands.
    successor_gateway = _gateway(
        engine,
        broker,
        DefaultExecutionLeaseProtocol(engine=engine),
        market_data=market_data,
    )
    successor_runtime = buyboard_runtime.build_buyboard_runtime(
        buying_power_provider=lambda *_: 100_000.0,
        account_equity_provider=lambda *_: 100_000.0,
        card_lookup=lambda environment, account_no, symbol: card_repo.get_trade_card(
            engine, environment, account_no, symbol
        ),
        capital_reservation_engine=engine,
        broker=successor_gateway,
        execution_lease=new_lease,
        lease_engine=engine,
        strategy_instance_id=STRATEGY,
        persist_card_before_execution=lambda card: card_repo.update_trade_card(
            engine, card, expected_version=card.version
        ),
        market_data=market_data,
    )
    successor = BuyboardRuntimeWorker(
        db_engine=engine,
        environment=ENVIRONMENT,
        account_no=ACCOUNT,
        buying_power_provider=lambda *_: 100_000.0,
        broker=successor_gateway,
        market_data=market_data,
        execution_authority=ExecutionAuthority(),
        execution_lease=LeaseHandle(
            device_id=new_lease.device_id,
            lease_token=new_lease.lease_token,
            lease_epoch=new_lease.lease_epoch,
        ),
        lease_engine=engine,
        account_discovery=lambda: [],
        strategy_instance_id=STRATEGY,
        device_id=new_role.device_id,
        hostname=new_role.hostname,
    )
    successor.runtime = successor_runtime
    successor.execution_gateway = successor_gateway
    successor._database_probe_completed = True
    successor._database_writable = True
    successor._accepting_commands = False
    successor.device_state = RuntimeDeviceState.STANDBY
    successor.last_market_data_drain_at = datetime.now(timezone.utc)
    successor._run_startup_reconciliation(execute_commands=False)
    successor._lease_current = successor._lease_still_current()
    discovery_calls_before_activation = broker.discovery_calls
    successor._advance_startup_readiness()
    assert broker.discovery_calls > discovery_calls_before_activation
    assert successor.device_state == RuntimeDeviceState.ACTIVE
    assert successor._accepting_commands is True
    assert successor.engine_readiness(account_no=ACCOUNT).healthy

    with pytest.raises((LeaseNotVerifiedError, GuardedSubmissionPreBrokerAbortedError)):
        old_gateway.submit_guarded(_request(old_lease, "STALE-DEVICE-SELL"))
    assert len(broker.submit_calls) == 1

    continued = card_repo.get_trade_card(engine, ENVIRONMENT, ACCOUNT, SYMBOL)
    assert continued.active_stop_price == 95.0
    assert continued.exit_client_order_id == requested.exit_client_order_id
    assert continued.broker_quantity == 10
    assert continued.exit_all_required is True

    _assert_gate1(engine, broker)


@pytest.mark.usefixtures("trading_enabled")
def test_gate1_ambiguous_submission_restart_reconciles_without_resubmitting(tmp_path):
    engine = _engine(tmp_path, "ambiguous-restart.db")
    lease = ExecutionLease("pc", "token", 1)
    protocol = FakeExecutionLeaseProtocol(current=lease)
    broker = CapstoneBroker()
    market_data = RestPollingMarketDataService(
        quote_fetcher=lambda symbol: QuoteSnapshot(
            symbol=symbol,
            last_price=99.0,
            bid=98.9,
            ask=99.0,
        )
    )
    market_data.subscribe([SYMBOL])
    market_data.poll_once()
    gateway = _gateway(engine, broker, protocol, market_data=market_data)
    broker.queue_timeout()

    with pytest.raises(GuardedSubmissionAmbiguousError):
        gateway.submit_guarded(
            _request(
                lease,
                "AMBIGUOUS-CID",
                side=OrderSide.BUY,
                intent=OrderIntent.ENTRY,
                emergency=False,
            )
        )
    ambiguous = fetch_execution_order(engine, "AMBIGUOUS-CID")
    assert ambiguous.status == ExecutionOrderStatus.UNKNOWN_SUBMISSION_STATE
    broker.order_snapshots = [
        BrokerOrderStatusSnapshot(
            environment=ENVIRONMENT,
            account_no=ACCOUNT,
            symbol=SYMBOL,
            broker_order_id="B-AMBIGUOUS",
            side=OrderSide.BUY,
            status=OrderStatus.WORKING,
            quantity_requested=10,
            remaining_quantity=10,
            limit_price=99.0,
            submitted_at=ambiguous.submission_started_at,
        )
    ]
    run_account_reconciliation_pass(
        broker=broker,
        engine=engine,
        environment=ENVIRONMENT,
        account_no=ACCOUNT,
        cards=[],
        account_balance_provider=lambda *_: 100_000.0,
    )
    reconciled = fetch_execution_order(engine, "AMBIGUOUS-CID")
    assert reconciled.recovery_state == OrderRecoveryState.BROKER_IDENTITY_UNCERTAIN

    restarted = _gateway(engine, broker, protocol, market_data=market_data)
    with pytest.raises(DuplicateCommandError):
        restarted.submit_guarded(
            _request(
                lease,
                "AMBIGUOUS-CID",
                side=OrderSide.BUY,
                intent=OrderIntent.ENTRY,
                emergency=False,
            )
        )
    assert len(broker.submit_calls) == 1
    assert reconciled.recovery_candidate_broker_order_ids == ("B-AMBIGUOUS",)
    _assert_gate1(engine, broker)


@pytest.mark.usefixtures("trading_enabled")
def test_gate1_database_outage_open_exposure_replays_emergency_journal_on_restart(
    tmp_path,
):
    engine = _engine(tmp_path, "outage-restart.db")
    lease = ExecutionLease("pc", "token", 4)
    protocol = FakeExecutionLeaseProtocol(current=lease)
    writable = [True]
    journal = EmergencyJournal(tmp_path / "gate1-emergency.jsonl")
    broker = CapstoneBroker()
    card = card_repo.create_trade_card(
        engine,
        TradeCardState(
            environment=ENVIRONMENT,
            account_no=ACCOUNT,
            symbol=SYMBOL,
            board_status=BoardStatus.SELL_ALL,
            position_runtime_status=PositionRuntimeStatus.LIQUIDATING,
            broker_quantity=7,
            orderable_quantity=7,
            average_entry_price=100.0,
            exit_all_required=True,
            exit_attempt_group_id="outage-exit",
            exit_client_order_id="OUTAGE-SELL-1",
            exit_pending_attempt_number=1,
        ),
    )
    gateway = _gateway(
        engine,
        broker,
        protocol,
        journal=journal,
        writable_provider=lambda: writable[0],
    )
    gateway.note_canonical_lease_verified(lease)
    gateway.note_canonical_ownership_verified(
        environment=ENVIRONMENT,
        account_no=ACCOUNT,
        symbol=SYMBOL,
        source=ExecutionSource.KANBAN_BOARD,
        strategy_instance_id=STRATEGY,
    )
    writable[0] = False
    broker.queue_acceptance(broker_order_id="B-OUTAGE-SELL")
    gateway.submit_guarded(
        _request(
            lease,
            "OUTAGE-SELL-1",
            quantity=7,
            attempt_group_id="outage-exit",
        )
    )

    writable[0] = True
    restarted = _gateway(
        engine,
        broker,
        protocol,
        journal=journal,
        writable_provider=lambda: writable[0],
    )
    assert restarted.reconcile_emergency_journal() == 1
    broker.holdings[SYMBOL] = (7, 100.0, 7)
    broker.order_snapshots = [
        _working_snapshot("OUTAGE-SELL-1", "B-OUTAGE-SELL", quantity=7)
    ]
    card = card_repo.get_trade_card(engine, ENVIRONMENT, ACCOUNT, SYMBOL)
    run_account_reconciliation_pass(
        broker=broker,
        engine=engine,
        environment=ENVIRONMENT,
        account_no=ACCOUNT,
        cards=[card],
        account_balance_provider=lambda *_: 100_000.0,
    )
    recovered_order = fetch_execution_order(engine, "OUTAGE-SELL-1")
    recovered_card = card_repo.get_trade_card(engine, ENVIRONMENT, ACCOUNT, SYMBOL)
    assert recovered_order.broker_order_id == "B-OUTAGE-SELL"
    assert recovered_card.broker_quantity == 7
    assert len(broker.submit_calls) == 1

    _assert_gate1(engine, broker)


@pytest.mark.usefixtures("trading_enabled")
def test_gate1_stop_breach_survives_device_handoff_and_submits_once(tmp_path):
    engine = _engine(tmp_path, "breach-handoff.db")
    old_lease = ExecutionLease("pc-old", "old-token", 1)
    new_lease = ExecutionLease("pc-new", "new-token", 2)
    protocol = FakeExecutionLeaseProtocol(current=old_lease)
    broker = CapstoneBroker()
    gateway = _gateway(engine, broker, protocol)
    accumulator = PendingMarketStateAccumulator()
    card_key = f"{ENVIRONMENT}:{ACCOUNT}:{SYMBOL}"
    accumulator.replace_stop_rules(
        SYMBOL, [StopRule(card_key=card_key, price=100.0, version="stop-v1")]
    )
    observed_at = datetime(2026, 8, 17, 14, 30, tzinfo=timezone.utc)
    accumulator.publish_trade(
        QuoteSnapshot(
            symbol=SYMBOL,
            last_price=99.0,
            received_at=observed_at,
            broker_event_at=observed_at,
            processed_at=observed_at,
            source="KIS_WS",
            channel="TRADE",
            payload_fingerprint="gate1-breach",
        )
    )

    protocol.grant(new_lease)
    with pytest.raises((LeaseNotVerifiedError, GuardedSubmissionPreBrokerAbortedError)):
        gateway.submit_guarded(_request(old_lease, "OLD-LEASE-STOP"))
    assert broker.submit_calls == []

    replay = accumulator.drain(SYMBOL)
    assert replay.pending.breached_stop_versions == {(card_key, "stop-v1")}
    broker.queue_acceptance(broker_order_id="B-NEW-STOP")
    gateway.submit_guarded(_request(new_lease, "NEW-LEASE-STOP"))
    assert accumulator.acknowledge_breach(SYMBOL, card_key, "stop-v1")
    assert not accumulator.drain(SYMBOL).pending.stop_breach_latched
    assert len(broker.submit_calls) == 1

    _assert_gate1(engine, broker)


@pytest.mark.usefixtures("trading_enabled")
def test_gate1_external_order_fence_requires_exact_adopted_cancel_before_emergency_exit(
    tmp_path,
):
    engine = _engine(tmp_path, "external-emergency.db")
    lease = ExecutionLease("pc", "token", 1)
    broker = CapstoneBroker()
    gateway = _gateway(engine, broker, FakeExecutionLeaseProtocol(current=lease))
    external = record_discovered_external_order(
        engine,
        new_discovered_external_order(
            environment=ENVIRONMENT,
            account_no=ACCOUNT,
            symbol=SYMBOL,
            side=OrderSide.BUY,
            broker_order_id="B-EXTERNAL-BUY",
            quantity_requested=3,
            broker_status=ExecutionOrderStatus.WORKING,
        ),
    )

    with pytest.raises(ActiveExternalOrderFenceError):
        gateway.submit_guarded(_request(lease, "FENCED-EMERGENCY-SELL"))
    assert broker.submit_calls == []

    adopted = adopt_external_order_in_db(
        engine,
        external.external_order_id,
        adopted_by="gate1-operator",
        permissions=frozenset({AdoptedOrderPermission.CANCEL}),
    )
    broker.queue_cancel_confirmed()
    gateway.cancel_guarded(
        CancelExecutionRequest(
            client_order_id=adopted.client_order_id,
            cancel_command_id="CANCEL-EXTERNAL-BUY",
            environment=ENVIRONMENT,
            account_no=ACCOUNT,
            lease=lease,
            source=ExecutionSource.KANBAN_BOARD,
            strategy_instance_id=STRATEGY,
        )
    )
    broker.queue_acceptance(broker_order_id="B-EMERGENCY-SELL")
    gateway.submit_guarded(_request(lease, "UNFENCED-EMERGENCY-SELL"))
    assert len(broker.cancel_calls) == 1
    assert len(broker.submit_calls) == 1

    _assert_gate1(engine, broker)


def test_gate1_migration_restart_runs_broker_reconciliation_before_entries_ready(
    tmp_path, monkeypatch
):
    engine = _engine(tmp_path, "migration-reconciliation.db")
    card_path = tmp_path / "legacy-cards.json"
    card_repo.save_local_trade_cards_snapshot(
        [TradeCardState(environment=ENVIRONMENT, account_no=ACCOUNT, symbol=SYMBOL)],
        path=card_path,
    )
    monkeypatch.setattr("src.services.schema_migration.ORDERS_FILE", tmp_path / "none-orders.json")
    monkeypatch.setattr("src.services.schema_migration.LOCAL_TRADE_CARDS_FILE", card_path)
    monkeypatch.setattr("src.services.schema_migration.RESERVATIONS_FILE", tmp_path / "none-reservations.json")
    lease = ExecutionLease("pc-main", "migration-token", 8)
    protocol = FakeExecutionLeaseProtocol(current=lease)

    def crash(point: str) -> None:
        if point == "after_cutover":
            raise RuntimeError("gate1 crash after cutover")

    manager = SchemaMigrationManager(
        engine,
        backup_path=tmp_path / "migration-backup.json",
        legacy_paths=(card_path,),
        lease_protocol=protocol,
        fault_hook=crash,
    )
    with pytest.raises(RuntimeError, match="after cutover"):
        manager.prepare_cutover(
            device_id=lease.device_id,
            lease_token=lease.lease_token,
            lease_epoch=lease.lease_epoch,
        )
    assert manager.state.phase == MigrationPhase.AWAITING_RECONCILIATION

    restarted = SchemaMigrationManager(
        engine,
        backup_path=tmp_path / "migration-backup.json",
        legacy_paths=(card_path,),
        lease_protocol=protocol,
    )
    assert restarted.prepare_cutover(
        device_id=lease.device_id,
        lease_token=lease.lease_token,
        lease_epoch=lease.lease_epoch,
    ).phase == MigrationPhase.AWAITING_RECONCILIATION
    broker = CapstoneBroker()
    broker.holdings[SYMBOL] = (5, 101.0, 5)
    migrated_cards = card_repo.list_trade_cards(
        engine, environment=ENVIRONMENT, account_no=ACCOUNT
    )
    run_account_reconciliation_pass(
        broker=broker,
        engine=engine,
        environment=ENVIRONMENT,
        account_no=ACCOUNT,
        cards=migrated_cards,
        account_balance_provider=lambda *_: 100_000.0,
    )
    assert restarted.mark_reconciliation_complete().phase == MigrationPhase.READY
    restarted.require_entries_ready()
    reconciled = card_repo.get_trade_card(engine, ENVIRONMENT, ACCOUNT, SYMBOL)
    assert reconciled.broker_quantity == 5

    _assert_gate1(engine, broker)


@pytest.mark.usefixtures("trading_enabled")
def test_gate1_rate_limit_pressure_prioritizes_real_emergency_liquidation(tmp_path):
    engine = _engine(tmp_path, "rate-pressure.db")
    lease = ExecutionLease("pc", "token", 1)
    order: list[str] = []

    class OrderedBroker(CapstoneBroker):
        def submit_order(self, **kwargs):
            order.append("emergency-exit")
            return super().submit_order(**kwargs)

    broker = OrderedBroker()
    broker.queue_acceptance(broker_order_id="B-RATE-EXIT")
    scheduler = KisRequestScheduler(
        read_policy=BudgetPolicy(20, 60),
        mutation_policy=BudgetPolicy(5, 60),
        sleeper=lambda _seconds: None,
    )
    gateway = _gateway(
        engine,
        broker,
        FakeExecutionLeaseProtocol(current=lease),
        mutation_budget=scheduler,
    )
    first_started = threading.Event()
    release_first = threading.Event()
    failures: list[BaseException] = []

    def first_display():
        first_started.set()
        release_first.wait()
        order.append("display-0")

    def run(callable_):
        try:
            callable_()
        except BaseException as exc:  # surfaced in the main test thread
            failures.append(exc)

    threads = [
        threading.Thread(
            target=lambda: run(
                lambda: scheduler.execute_read(
                    first_display,
                    account_no=ACCOUNT,
                    endpoint="quote",
                    priority=RequestPriority.DISPLAY_REFRESH,
                )
            )
        )
    ]
    threads[0].start()
    assert first_started.wait(2)
    for label in ("display-1", "display-2"):
        thread = threading.Thread(
            target=lambda name=label: run(
                lambda: scheduler.execute_read(
                    lambda: order.append(name),
                    account_no=ACCOUNT,
                    endpoint="quote",
                    priority=RequestPriority.DISPLAY_REFRESH,
                )
            )
        )
        threads.append(thread)
        thread.start()
    emergency = threading.Thread(
        target=lambda: run(
            lambda: gateway.submit_guarded(_request(lease, "RATE-EMERGENCY-SELL"))
        )
    )
    threads.append(emergency)
    emergency.start()
    deadline = time.time() + 10
    while scheduler.metrics().queued_requests < 3 and time.time() < deadline:
        time.sleep(0.005)
    queue_ready = scheduler.metrics().queued_requests >= 3
    release_first.set()
    for thread in threads:
        thread.join(2)
        assert not thread.is_alive()

    assert failures == []
    assert queue_ready, "all competing requests must be queued before release"
    assert order[0:2] == ["display-0", "emergency-exit"]
    assert len(broker.submit_calls) == 1
    _assert_gate1(engine, broker)
