"""Tests for src.ui.buyboard.runtime_worker (code review finding P0-1).

Exercises the worker's actual logic (startup reconciliation, one heartbeat
cycle, ORB-plan sync, quote-subscription sync, persistence, lease checks)
directly as plain method calls rather than through QThread.start()/run()'s
real background-thread machinery, which is inherently timing-dependent and
not suited to a deterministic unit test. ``self.runtime`` is set manually
in these tests the same way ``run()`` sets it, immediately before calling
the method under test.
"""
from __future__ import annotations

import datetime as dt
import os
import threading
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt5.QtWidgets import QApplication
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

from src.core import execution_config
from src.core.account_broker_snapshot import AccountBrokerSnapshot, SnapshotCompleteness
from src.core.board_workflow import (
    BoardActionContext,
    ReorderCard,
    RequestSellAll,
    SetManualStop,
)
from src.core.execution_mode import ExecutionLease
from src.core.runtime_readiness import EngineReadiness, RuntimeDeviceState
from src.core.execution_ownership import ExecutionOwner, ExecutionOwnership
from src.core.execution_order_record import (
    BrokerIdentityStatus,
    ExecutionOrderRecord,
    ExecutionOrderStatus,
)
from src.core.execution_queue import ExecutionQueueItem, OrbCandidate, OrbCandidateStatus
from src.core.order_state import (
    BrokerOrderDiscoveryResult,
    BrokerOrderStatusSnapshot,
    OrderIntent,
    OrderSide,
    OrderStatus,
)
from src.core.trade_card_state import (
    BoardStatus,
    EntryRuntimeStatus,
    PositionRuntimeStatus,
    StopType,
    TradeCardState,
)
from src.services import buyboard_runtime as runtime_module
from src.services import execution_workflow_service as workflow
from src.services import trade_card_repository as repo
from src.services.account_reconciliation import (
    AccountReconciliationResult,
    ReconciliationAlert,
    ReconciliationAlertSeverity,
    ReconciliationPlan,
    run_account_reconciliation_pass,
)
from src.services.broker import BrokerSubmissionResult
from src.services.execution_authority import ExecutionAuthority, LeaseExpiredError, LeaseHandle
from src.services.execution_command_gateway import ExecutionCommandGateway
from src.services.execution_lease_protocol import FakeExecutionLeaseProtocol
from src.services.execution_order_repository import record_execution_order
from src.services.execution_ownership_repository import assign_ownership
from src.services.external_alerting import CriticalAlertType
from src.services.mutation_budget_protocol import AllowAllMutationBudget
from src.services.position_manager import PositionManager
from src.services.runtime_device_state_repository import get_runtime_device_state
from src.services.kis_realtime_market_data import (
    KisRealtimeMarketDataService,
    StopRule,
    SubscriptionPriority,
)
from src.services.realtime_market_data import QuoteSnapshot, RestPollingMarketDataService
from src.services.stop_change_coordinator import (
    StopChangeCoordinator,
    stop_change_coordinator_for,
)
from src.ui.buyboard.runtime_worker import BuyboardRuntimeWorker
from fakes.fake_execution_broker import FakeExecutionBroker


def _dummy_market_data() -> RestPollingMarketDataService:
    """A lightweight, network-free quote source for tests that care about
    real elapsed time between _run_one_cycle() calls -- the default
    KIS-only quote fetcher build_buyboard_runtime() falls back to makes a
    genuine (slow, failing-without-credentials) network call for every
    subscribed symbol, which otherwise burns several real seconds per
    cycle and corrupts any test asserting on refresh-interval timing.
    """
    return RestPollingMarketDataService(
        quote_fetcher=lambda symbol: QuoteSnapshot(symbol=symbol, last_price=100.0)
    )


def _build_test_runtime(**kwargs):
    """Compose legacy wiring while engine-decision tests force heartbeat enabled."""
    original = runtime_module.execution_config.is_buyboard_engine_enabled
    runtime_module.execution_config.is_buyboard_engine_enabled = lambda: False
    try:
        return runtime_module.build_buyboard_runtime(**kwargs)
    finally:
        runtime_module.execution_config.is_buyboard_engine_enabled = original

_APP = None


def _ensure_app():
    global _APP
    _APP = QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _isolate_local_trade_card_snapshot(monkeypatch, tmp_path):
    monkeypatch.setattr(repo, "LOCAL_TRADE_CARDS_FILE", tmp_path / "trade_cards.json")


@pytest.fixture(autouse=True)
def _isolated_buying_power_cache():
    from src.services import buying_power_cache

    buying_power_cache.clear()
    yield
    buying_power_cache.clear()


class _FakeBroker:
    def __init__(self):
        self.discover_result = BrokerOrderDiscoveryResult(
            open_orders_complete=True, history_complete=True, reserved_orders_complete=True
        )
        self.positions = {"overseas": {"holdings": []}}
        # Optional per-account override -- when set, get_positions returns
        # positions_by_account.get(account_no) instead of the single shared
        # self.positions, so multi-account tests can give two accounts
        # genuinely different holdings.
        self.positions_by_account: dict = {}
        self.get_positions_calls: list = []
        self.cancel_calls: list = []

    def submit_order(self, **kwargs):
        return BrokerSubmissionResult(broker_order_id="B-1", raw_response={})

    def is_ambiguous_submission_error(self, error):
        return False

    def cancel_order(self, **kwargs):
        self.cancel_calls.append(kwargs)
        from src.core.order_state import BrokerOrderStatusSnapshot, OrderStatus

        return BrokerOrderStatusSnapshot(
            environment=kwargs.get("environment", "PROD"),
            account_no=kwargs.get("account_no", "1"),
            symbol=kwargs.get("symbol", ""),
            broker_order_id=kwargs.get("broker_order_id", ""),
            status=OrderStatus.CANCELLED,
        )

    def get_order(self, **kwargs):
        return []

    def discover_orders(self, *, environment, account_no):
        return self.discover_result

    def get_positions(self, *, environment, account_no=None):
        self.get_positions_calls.append(account_no)
        if self.positions_by_account:
            return self.positions_by_account.get(
                account_no, {"overseas": {"holdings": []}}
            )
        return self.positions


def _db_engine(tmp_path):
    return create_engine(f"sqlite:///{tmp_path / 'cards.db'}", future=True, poolclass=NullPool)


def _worker(
    tmp_path, *, broker=None, execution_authority=None, execution_lease=None,
    account_no="1", **kwargs,
):
    _ensure_app()
    engine = _db_engine(tmp_path)
    # account_discovery defaults to [] (not the real KIS-config-backed
    # discovery) so these tests stay hermetic regardless of what KIS
    # accounts happen to be configured in the developer's own .env --
    # tests that specifically want to exercise discovery override it.
    kwargs.setdefault("account_discovery", lambda: [])
    worker = BuyboardRuntimeWorker(
        db_engine=engine,
        environment="PROD",
        account_no=account_no,
        buying_power_provider=lambda env, acct: 100_000.0,
        broker=broker or _FakeBroker(),
        execution_authority=execution_authority,
        execution_lease=execution_lease,
        **kwargs,
    )
    return worker, engine


def _guarded_reconciliation_worker(tmp_path, monkeypatch, real_broker):
    from src.core import execution_config

    monkeypatch.setattr(execution_config, "is_buyboard_engine_enabled", lambda: True)
    engine = _db_engine(tmp_path)
    lease = ExecutionLease(device_id="device-1", lease_token="token-1", lease_epoch=1)
    lease_protocol = FakeExecutionLeaseProtocol(
        current=lease, epoch_verified=True
    )
    gateway = ExecutionCommandGateway(
        real_broker=real_broker,
        engine=engine,
        mode_override=True,
        lease_protocol=lease_protocol,
        mutation_budget=AllowAllMutationBudget(),
        buying_power_provider=lambda *_: 100_000.0,
    )
    assign_ownership(
        engine,
        ExecutionOwnership(
            environment="PROD",
            account_no="1",
            symbol="AAPL",
            owner=ExecutionOwner.KANBAN,
            strategy_instance_id="reconciliation-test",
        ),
    )
    market_data = _dummy_market_data()
    market_data.subscribe(["AAPL"])
    market_data.poll_once()
    worker = BuyboardRuntimeWorker(
        db_engine=engine,
        environment="PROD",
        account_no="1",
        buying_power_provider=lambda *_: 100_000.0,
        broker=gateway,
        execution_lease=lease,
        account_discovery=lambda: [],
    )
    worker.runtime = runtime_module.build_buyboard_runtime(
        buying_power_provider=worker._buying_power_provider,
        card_lookup=worker._card_lookup,
        capital_reservation_engine=engine,
        execution_lease=lease,
        broker=gateway,
        market_data=market_data,
        strategy_instance_id="reconciliation-test",
        persist_card_before_execution=worker._persist_execution_identity,
    )
    return worker, engine, gateway


def _seed_card(engine, **overrides):
    fields = dict(environment="PROD", account_no="1", symbol="AAPL")
    fields.update(overrides)
    return repo.create_trade_card(engine, TradeCardState(**fields))


def _ready_runtime_state(**overrides):
    values = dict(
        lease_current=True,
        startup_reconciliation_complete=True,
        account_reconciliation_fresh=True,
        websocket_connected=True,
        critical_trade_subscriptions_acked=True,
        critical_quote_subscriptions_acked=True,
        critical_quotes_fresh=True,
        accumulator_draining_within_budget=True,
        database_writable=True,
        device_active=True,
    )
    values.update(overrides)
    return EngineReadiness(**values)


# --- Construction does not build/start anything -----------------------------


def test_construction_builds_nothing(tmp_path):
    worker, _ = _worker(tmp_path)
    assert worker.runtime is None


def test_default_worker_scheduler_uses_production_spacing_and_no_retry(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(execution_config, "KIS_MUTATION_MIN_SPACING_SECONDS", 0.25)
    monkeypatch.setattr(execution_config, "KIS_MUTATION_MAX_CONFIRMED_ATTEMPTS", 1)

    worker, _ = _worker(tmp_path)

    assert worker.request_scheduler.min_mutation_spacing_seconds == 0.25
    assert worker.request_scheduler.max_confirmed_mutation_attempts == 1


def test_worker_activates_only_explicitly_verified_account_endpoint_budgets(
    tmp_path, monkeypatch
):
    worker, engine = _worker(tmp_path)
    card = _seed_card(engine)
    monkeypatch.setattr(execution_config, "KIS_MUTATION_BUDGET_VERIFIED", True)
    monkeypatch.setattr(execution_config, "KIS_SUBMIT_MUTATION_CAPACITY", 3)
    monkeypatch.setattr(execution_config, "KIS_CANCEL_MUTATION_CAPACITY", 2)
    monkeypatch.setattr(execution_config, "KIS_REPLACE_MUTATION_CAPACITY", 1)
    monkeypatch.setattr(execution_config, "KIS_MUTATION_BUDGET_WINDOW_SECONDS", 5.0)

    worker._configure_verified_mutation_budgets([card])

    snapshot = worker.request_scheduler.budget_snapshot()
    assert snapshot["MUTATION:1:submit_order"]["knowledge"] == "KNOWN"
    assert snapshot["MUTATION:1:submit_order"]["capacity"] == 3
    assert snapshot["MUTATION:1:cancel_order"]["capacity"] == 2
    assert snapshot["MUTATION:1:replace_order"]["capacity"] == 1


def test_database_recovery_forces_full_projection_before_reopening_commands(
    tmp_path, monkeypatch
):
    worker, _ = _worker(tmp_path)
    worker._recovery_reconciliation_required = True
    worker._accepting_commands = True
    worker.device_state = RuntimeDeviceState.ACTIVE
    calls = []

    def reconcile(*, execute_commands):
        calls.append(execute_commands)
        worker.startup_reconciliation_complete = True

    monkeypatch.setattr(worker, "_run_startup_reconciliation", reconcile)

    assert worker._complete_database_recovery() is True
    assert calls == [False]
    assert worker._recovery_reconciliation_required is False
    assert worker._accepting_commands is True


def test_tighter_pending_stop_catches_trade_detached_under_old_generation(
    tmp_path, monkeypatch
):
    """95 -> 100 with a 98 trade in the handoff cannot lose protection."""

    import src.services.trading_engine as trading_engine_module

    monkeypatch.setattr(
        trading_engine_module, "is_buyboard_engine_enabled", lambda: True
    )
    now = dt.datetime.now(dt.timezone.utc)

    class _Transport:
        def on_data(self, callback):
            pass

        def on_ack(self, callback):
            pass

        def on_connection(self, callback):
            pass

        def subscribe(self, subscriptions):
            pass

        def unsubscribe(self, subscriptions):
            pass

        def is_connected(self):
            return True

    service = KisRealtimeMarketDataService(
        transport=_Transport(),
        symbol_key_resolver=lambda symbol, channel: symbol,
        trade_capacity=10,
        quote_capacity=10,
        clock=lambda: now,
        regular_session_filter=lambda observed_at: True,
    )
    card = TradeCardState(
        environment="PROD",
        account_no="1",
        symbol="AAPL",
        board_status=BoardStatus.OPEN_POSITION,
        position_runtime_status=PositionRuntimeStatus.OPEN,
        broker_quantity=10,
        orderable_quantity=10,
        stop_type=StopType.ORB_LOW,
        active_stop_price=95.0,
        stop_quantity=10,
        pending_stop_type=StopType.MANUAL_PRICE,
        pending_stop_price=100.0,
        pending_stop_quantity=10,
        pending_stop_command_id="STOP-CHANGE-1",
        pending_stop_requested_at=now,
    )
    runtime = _build_test_runtime(
        buying_power_provider=lambda *_: 100_000.0,
        card_lookup=lambda *_: card,
        broker=_FakeBroker(),
        market_data=service,
    )
    worker, _ = _worker(tmp_path)
    worker.runtime = runtime

    # Old 95 protection is live. The 98 event lands after the durable UI
    # request but before the worker acquires the feed lock for the new 100.
    worker._sync_market_stop_rules([card], apply_pending_changes=False)
    observed = now + dt.timedelta(milliseconds=1)
    assert service.ingest_trade(
        QuoteSnapshot(
            symbol="AAPL",
            last_price=98.0,
            broker_event_at=observed,
            received_at=observed,
            processed_at=observed,
            channel="HDFSCNT0",
            payload_fingerprint="during-stop-handoff",
        )
    )
    assert worker._sync_market_stop_rules([card], apply_pending_changes=True)

    initiations = []
    real_initiate = runtime.trading_engine._initiate_sell_all

    def record_initiation(current, **kwargs):
        initiations.append(current.card_key)
        return real_initiate(current, **kwargs)

    monkeypatch.setattr(runtime.trading_engine, "_initiate_sell_all", record_initiation)
    for quote in service.poll_once():
        runtime.trading_engine.evaluate_quote([card], quote)
        runtime.trading_engine.evaluate_pending_stop_handoff([card], quote)

    assert worker._acknowledge_pending_stop_changes([card]) == [card]
    assert card.board_status == BoardStatus.SELL_ALL
    assert card.exit_all_required is True
    assert card.active_stop_price == 100.0
    assert card.pending_stop_command_id == ""
    assert initiations == [card.card_key]


def _stale_snapshot_stop_worker(tmp_path, monkeypatch, *, stop_price: float):
    import src.services.trading_engine as trading_engine_module

    monkeypatch.setattr(
        trading_engine_module, "is_buyboard_engine_enabled", lambda: True
    )
    now = dt.datetime.now(dt.timezone.utc)

    class _Transport:
        def on_data(self, callback):
            pass

        def on_ack(self, callback):
            pass

        def on_connection(self, callback):
            pass

        def subscribe(self, subscriptions):
            pass

        def unsubscribe(self, subscriptions):
            pass

        def is_connected(self):
            return True

    service = KisRealtimeMarketDataService(
        transport=_Transport(),
        symbol_key_resolver=lambda symbol, channel: symbol,
        trade_capacity=10,
        quote_capacity=10,
        clock=lambda: now,
        regular_session_filter=lambda observed_at: True,
    )
    worker, engine = _worker(tmp_path)
    card = _seed_card(
        engine,
        board_status=BoardStatus.OPEN_POSITION,
        position_runtime_status=PositionRuntimeStatus.OPEN,
        broker_quantity=10,
        orderable_quantity=10,
        stop_type=StopType.ORB_LOW,
        active_stop_price=stop_price,
        stop_quantity=10,
    )
    worker.runtime = _build_test_runtime(
        buying_power_provider=worker._buying_power_provider,
        card_lookup=worker._card_lookup,
        broker=worker._broker,
        market_data=service,
    )
    worker.runtime.trading_engine.run_heartbeat = lambda cards: []
    monkeypatch.setattr(
        worker, "_refresh_account_state_if_due", lambda *args, **kwargs: False
    )
    worker._sync_market_stop_rules([card], apply_pending_changes=False)
    return worker, engine, service, now


def test_ui_stop_commit_reaches_feed_when_worker_card_snapshot_is_stale(
    tmp_path, monkeypatch
):
    """A v11 stop commit cannot be missed by a worker still holding v10."""

    worker, engine, service, now = _stale_snapshot_stop_worker(
        tmp_path, monkeypatch, stop_price=95.0
    )
    original_sync = worker._sync_quote_subscriptions
    injected = False

    def commit_stop_after_worker_load(cards):
        nonlocal injected
        original_sync(cards)
        if injected:
            return
        injected = True
        canonical = repo.get_trade_card(engine, "PROD", "1", "AAPL")
        workflow.request_board_action(
            engine,
            SetManualStop(
                environment="PROD",
                account_no="1",
                symbol="AAPL",
                expected_card_version=canonical.version,
                price=100.0,
                requested_at=now,
            ),
            context=BoardActionContext(),
        )
        observed = now + dt.timedelta(milliseconds=1)
        assert service.ingest_trade(
            QuoteSnapshot(
                symbol="AAPL",
                last_price=98.0,
                broker_event_at=observed,
                received_at=observed,
                processed_at=observed,
                channel="HDFSCNT0",
                payload_fingerprint="stale-worker-stop-request",
            )
        )

    monkeypatch.setattr(worker, "_sync_quote_subscriptions", commit_stop_after_worker_load)

    worker._run_one_cycle()

    after_conflict = repo.get_trade_card(engine, "PROD", "1", "AAPL")
    assert after_conflict.pending_stop_command_id
    assert after_conflict.exit_all_required is False
    assert any(quote.breached_stop_versions for quote in service.poll_once())

    worker._run_one_cycle()

    durable = repo.get_trade_card(engine, "PROD", "1", "AAPL")
    assert durable.active_stop_price == 100.0
    assert durable.pending_stop_command_id == ""
    assert durable.exit_all_required is True
    assert durable.board_status == BoardStatus.SELL_ALL
    assert not any(quote.breached_stop_versions for quote in service.poll_once())


def test_main_standby_acknowledges_premarket_stop_without_broker_mutation(
    tmp_path, monkeypatch
):
    worker, engine, _service, now = _stale_snapshot_stop_worker(
        tmp_path, monkeypatch, stop_price=95.0
    )
    worker.device_state = RuntimeDeviceState.STANDBY
    worker._accepting_commands = False
    worker._lease_current = True
    broker_calls = []
    worker.runtime.broker.submit_order = lambda **kwargs: broker_calls.append(
        ("submit", kwargs)
    )
    worker.runtime.broker.cancel_order = lambda **kwargs: broker_calls.append(
        ("cancel", kwargs)
    )

    card = repo.get_trade_card(engine, "PROD", "1", "AAPL")
    requested = workflow.request_board_action(
        engine,
        SetManualStop(
            environment="PROD",
            account_no="1",
            symbol="AAPL",
            expected_card_version=card.version,
            price=100.0,
            requested_at=now,
        ),
        context=BoardActionContext(),
    ).card
    assert requested.pending_stop_command_id

    worker._run_one_cycle(allow_mutations=False)

    durable = repo.get_trade_card(engine, "PROD", "1", "AAPL")
    assert durable.active_stop_price == 100.0
    assert durable.stop_type == StopType.MANUAL_PRICE
    assert durable.pending_stop_command_id == ""
    assert worker.device_state == RuntimeDeviceState.STANDBY
    assert worker._accepting_commands is False
    assert broker_calls == []


def test_pull_only_standby_cannot_acknowledge_premarket_stop(
    tmp_path, monkeypatch
):
    worker, engine, _service, now = _stale_snapshot_stop_worker(
        tmp_path, monkeypatch, stop_price=95.0
    )
    worker._standby_only = True
    worker.device_state = RuntimeDeviceState.STANDBY_READY
    worker._accepting_commands = False
    worker._lease_current = False

    card = repo.get_trade_card(engine, "PROD", "1", "AAPL")
    requested = workflow.request_board_action(
        engine,
        SetManualStop(
            environment="PROD",
            account_no="1",
            symbol="AAPL",
            expected_card_version=card.version,
            price=100.0,
            requested_at=now,
        ),
        context=BoardActionContext(),
    ).card

    worker._run_one_cycle(allow_mutations=False)

    durable = repo.get_trade_card(engine, "PROD", "1", "AAPL")
    assert durable.active_stop_price == 95.0
    assert durable.pending_stop_command_id == requested.pending_stop_command_id
    assert durable.pending_stop_price == 100.0


def test_stop_breach_ack_waits_for_successful_card_cas(tmp_path, monkeypatch):
    worker, engine, service, now = _stale_snapshot_stop_worker(
        tmp_path, monkeypatch, stop_price=100.0
    )
    original_sync = worker._sync_quote_subscriptions
    injected = False

    def reorder_and_breach_after_worker_load(cards):
        nonlocal injected
        original_sync(cards)
        if injected:
            return
        injected = True
        canonical = repo.get_trade_card(engine, "PROD", "1", "AAPL")
        workflow.request_board_action(
            engine,
            ReorderCard(
                environment="PROD",
                account_no="1",
                symbol="AAPL",
                expected_card_version=canonical.version,
                target_priority=7,
            ),
            context=BoardActionContext(),
        )
        observed = now + dt.timedelta(milliseconds=1)
        assert service.ingest_trade(
            QuoteSnapshot(
                symbol="AAPL",
                last_price=99.0,
                broker_event_at=observed,
                received_at=observed,
                processed_at=observed,
                channel="HDFSCNT0",
                payload_fingerprint="breach-before-stale-cas",
            )
        )

    monkeypatch.setattr(
        worker, "_sync_quote_subscriptions", reorder_and_breach_after_worker_load
    )

    worker._run_one_cycle()

    after_conflict = repo.get_trade_card(engine, "PROD", "1", "AAPL")
    assert after_conflict.kanban_priority == 7
    assert after_conflict.exit_all_required is False
    assert any(quote.breached_stop_versions for quote in service.poll_once())

    worker._run_one_cycle()

    durable = repo.get_trade_card(engine, "PROD", "1", "AAPL")
    assert durable.kanban_priority == 7
    assert durable.exit_all_required is True
    assert durable.board_status == BoardStatus.SELL_ALL
    assert not any(quote.breached_stop_versions for quote in service.poll_once())


def _request_stop_then_sell_all(engine):
    card = _seed_card(
        engine,
        board_status=BoardStatus.OPEN_POSITION,
        position_runtime_status=PositionRuntimeStatus.OPEN,
        broker_quantity=10,
        orderable_quantity=10,
        stop_type=StopType.ORB_LOW,
        active_stop_price=95.0,
        stop_quantity=10,
    )
    stop_result = workflow.request_board_action(
        engine,
        SetManualStop(
            environment="PROD",
            account_no="1",
            symbol="AAPL",
            expected_card_version=card.version,
            price=100.0,
        ),
        context=BoardActionContext(),
    )
    sell_all = workflow.request_board_action(
        engine,
        RequestSellAll(
            environment="PROD",
            account_no="1",
            symbol="AAPL",
            expected_card_version=stop_result.card.version,
        ),
        context=BoardActionContext(),
    )
    return sell_all.card


def test_flat_confirmation_retires_pending_stop_before_next_trade_cycle(
    tmp_path,
):
    worker, engine = _worker(tmp_path)
    sell_all = _request_stop_then_sell_all(engine)
    coordinator = stop_change_coordinator_for(engine)
    pending = coordinator.pending_for(sell_all.card_key)
    assert pending is not None
    assert pending.request_card_version < sell_all.version

    sell_all.broker_quantity = 0
    sell_all.orderable_quantity = 0
    PositionManager().confirm_flat(sell_all)
    assert worker._persist_changed([sell_all]) == [sell_all]

    closed = repo.get_trade_card(engine, "PROD", "1", "AAPL")
    assert closed.board_status == BoardStatus.CLOSED
    assert coordinator.pending_for(closed.card_key) is None

    closed.board_status = BoardStatus.OPEN_POSITION
    closed.position_runtime_status = PositionRuntimeStatus.OPEN
    closed.broker_quantity = 10
    closed.orderable_quantity = 10
    closed.stop_type = StopType.ORB_LOW
    closed.active_stop_price = 90.0
    closed.stop_quantity = 10
    repo.update_trade_card(engine, closed, expected_version=closed.version)
    coordinator.overlay_pending([closed])

    assert closed.active_stop_price == 90.0
    assert closed.pending_stop_command_id == ""
    assert closed.pending_stop_price is None


def test_account_reconciliation_flat_retires_pending_stop(tmp_path):
    broker = _FakeBroker()
    broker.positions = {"overseas": {"holdings": []}}
    _, engine = _worker(tmp_path, broker=broker)
    sell_all = _request_stop_then_sell_all(engine)
    coordinator = stop_change_coordinator_for(engine)
    assert coordinator.pending_for(sell_all.card_key) is not None

    result = run_account_reconciliation_pass(
        broker=broker,
        engine=engine,
        environment="PROD",
        account_no="1",
        cards=[sell_all],
    )

    assert any(card.board_status == BoardStatus.CLOSED for card in result.plan.changed_cards)
    closed = repo.get_trade_card(engine, "PROD", "1", "AAPL")
    assert closed.board_status == BoardStatus.CLOSED
    assert closed.pending_stop_command_id == ""
    assert coordinator.pending_for(closed.card_key) is None


def test_new_stop_request_survives_race_with_old_request_retirement():
    coordinator = StopChangeCoordinator()
    requested_at = dt.datetime.now(dt.timezone.utc)
    old = TradeCardState(
        environment="PROD",
        account_no="1",
        symbol="AAPL",
        version=2,
        board_status=BoardStatus.OPEN_POSITION,
        position_runtime_status=PositionRuntimeStatus.OPEN,
        stop_type=StopType.ORB_LOW,
        active_stop_price=90.0,
        stop_quantity=10,
        pending_stop_type=StopType.MANUAL_PRICE,
        pending_stop_price=95.0,
        pending_stop_quantity=10,
        pending_stop_command_id="OLD-STOP",
        pending_stop_requested_at=requested_at,
    )
    coordinator.record_durable(old)

    retired = TradeCardState.from_dict(old.to_dict())
    retired.version = 3
    retired.broker_quantity = 0
    retired.orderable_quantity = 0
    PositionManager().confirm_flat(retired)

    new = TradeCardState(
        environment="PROD",
        account_no="1",
        symbol="AAPL",
        version=4,
        board_status=BoardStatus.OPEN_POSITION,
        position_runtime_status=PositionRuntimeStatus.OPEN,
        stop_type=StopType.ORB_LOW,
        active_stop_price=88.0,
        stop_quantity=12,
        pending_stop_type=StopType.MANUAL_PRICE,
        pending_stop_price=92.0,
        pending_stop_quantity=12,
        pending_stop_command_id="NEW-STOP",
        pending_stop_requested_at=requested_at + dt.timedelta(seconds=1),
    )
    barrier = threading.Barrier(2)

    def retire_old():
        barrier.wait()
        coordinator.reconcile_durable(retired)

    def record_new():
        barrier.wait()
        coordinator.record_durable(new)

    threads = [threading.Thread(target=retire_old), threading.Thread(target=record_new)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)
        assert not thread.is_alive()

    pending = coordinator.pending_for(new.card_key)
    assert pending is not None
    assert pending.command_id == "NEW-STOP"
    assert pending.request_card_version == 4


def test_action_readiness_uses_exact_symbol_not_another_quiet_symbol(tmp_path):
    worker, _ = _worker(tmp_path)
    observed_at = dt.datetime.now(dt.timezone.utc)

    class MarketData:
        @staticmethod
        def health_metrics(*, now):
            return SimpleNamespace(
                ws_connected=True,
                critical_trade_channels_missing=(),
                critical_quote_channels_missing=(),
                stale_symbols=("STIM",),
            )

        @staticmethod
        def is_symbol_execution_ready(symbol, *, now):
            return symbol == "AAPL"

        @staticmethod
        def is_symbol_feed_available(symbol):
            return symbol == "AAPL"

    worker.runtime = SimpleNamespace(market_data=MarketData())
    worker.startup_reconciliation_ran = True
    worker.startup_reconciliation_complete = True
    worker._database_writable = True
    worker.last_market_data_drain_at = observed_at
    worker.device_state = RuntimeDeviceState.ACTIVE

    global_readiness = worker.engine_readiness(now=observed_at)
    action_readiness = worker.engine_readiness(
        symbol="AAPL",
        action="NEW_ENTRY",
        now=observed_at,
    )

    assert global_readiness.critical_quotes_fresh is False
    assert action_readiness.critical_trade_subscriptions_acked is True
    assert action_readiness.critical_quote_subscriptions_acked is True
    assert action_readiness.critical_quotes_fresh is True


@pytest.mark.parametrize(
    "missing",
    [
        "startup_reconciliation_complete",
        "account_reconciliation_fresh",
        "websocket_connected",
        "critical_trade_subscriptions_acked",
        "critical_quote_subscriptions_acked",
        "critical_quotes_fresh",
        "accumulator_draining_within_budget",
        "database_writable",
    ],
)
def test_startup_sequence_does_not_allow_entries_before_every_step_confirms(
    tmp_path, monkeypatch, missing
):
    worker, _ = _worker(tmp_path)
    worker._accepting_commands = False
    worker.device_state = RuntimeDeviceState.STANDBY
    monkeypatch.setattr(
        worker,
        "engine_readiness",
        lambda **kwargs: _ready_runtime_state(**{missing: False}),
    )
    final_passes = []
    monkeypatch.setattr(
        worker,
        "_run_startup_reconciliation",
        lambda **kwargs: final_passes.append(kwargs),
    )

    worker._advance_startup_readiness()

    assert worker._accepting_commands is False
    assert worker.device_state == RuntimeDeviceState.STANDBY
    assert final_passes == []


def test_startup_promotes_standby_to_active_only_after_final_reconciliation(
    tmp_path, monkeypatch
):
    worker, _ = _worker(tmp_path)
    worker._accepting_commands = False
    worker.device_state = RuntimeDeviceState.STANDBY
    transitions = []
    final_passes = []
    monkeypatch.setattr(
        worker, "engine_readiness", lambda **kwargs: _ready_runtime_state()
    )
    monkeypatch.setattr(worker, "_lease_still_current", lambda: True)
    monkeypatch.setattr(
        worker,
        "_run_startup_reconciliation",
        lambda **kwargs: final_passes.append(kwargs),
    )

    def record_state(state, **kwargs):
        transitions.append(state)
        worker.device_state = state

    monkeypatch.setattr(worker, "_set_device_state", record_state)

    worker._advance_startup_readiness()

    assert transitions == [
        RuntimeDeviceState.STANDBY_READY,
        RuntimeDeviceState.ACTIVE,
    ]
    assert final_passes == [{"execute_commands": False}]
    assert worker._accepting_commands is True


def test_startup_refreshes_observation_after_slow_final_reconciliation(
    tmp_path, monkeypatch
):
    worker, _ = _worker(tmp_path)
    worker._accepting_commands = False
    worker.device_state = RuntimeDeviceState.STANDBY
    polls = []
    readiness_calls = []

    class _MarketData:
        def poll_once(self):
            polls.append(True)
            return []

    worker.runtime = SimpleNamespace(market_data=_MarketData())

    def readiness(**kwargs):
        readiness_calls.append(kwargs)
        return _ready_runtime_state(
            accumulator_draining_within_budget=(
                len(readiness_calls) == 1 or bool(polls)
            )
        )

    monkeypatch.setattr(worker, "engine_readiness", readiness)
    monkeypatch.setattr(worker, "_lease_still_current", lambda: True)

    def slow_final_reconciliation(**kwargs):
        worker.last_market_data_drain_at = dt.datetime.now(
            dt.timezone.utc
        ) - dt.timedelta(minutes=1)

    monkeypatch.setattr(
        worker, "_run_startup_reconciliation", slow_final_reconciliation
    )

    def record_state(state, **kwargs):
        worker.device_state = state

    monkeypatch.setattr(worker, "_set_device_state", record_state)

    worker._advance_startup_readiness()

    assert polls == [True]
    assert len(readiness_calls) == 2
    assert worker.last_market_data_drain_at is not None
    assert worker.device_state == RuntimeDeviceState.ACTIVE
    assert worker._accepting_commands is True


def test_pull_only_successor_reaches_standby_ready_but_never_active(
    tmp_path, monkeypatch
):
    worker, _ = _worker(tmp_path, standby_only=True, device_id="successor")
    worker._accepting_commands = False
    worker.device_state = RuntimeDeviceState.STANDBY
    transitions = []
    final_passes = []
    monkeypatch.setattr(
        worker, "engine_readiness", lambda **kwargs: _ready_runtime_state()
    )

    def record_state(state, **kwargs):
        transitions.append(state)
        worker.device_state = state

    monkeypatch.setattr(worker, "_set_device_state", record_state)
    monkeypatch.setattr(
        worker,
        "_run_startup_reconciliation",
        lambda **kwargs: final_passes.append(kwargs),
    )

    worker._advance_startup_readiness()

    assert transitions == [RuntimeDeviceState.STANDBY_READY]
    assert final_passes == [{"execute_commands": False}]
    assert worker._accepting_commands is False
    assert worker.device_state == RuntimeDeviceState.STANDBY_READY


def test_pull_only_successor_is_handoff_ready_before_market_open_without_quotes(
    tmp_path, monkeypatch
):
    worker, _ = _worker(
        tmp_path,
        standby_only=True,
        device_id="successor",
        regular_session_open=lambda: False,
    )
    worker._accepting_commands = False
    worker.device_state = RuntimeDeviceState.STANDBY
    transitions = []
    final_passes = []
    monkeypatch.setattr(
        worker,
        "engine_readiness",
        lambda **kwargs: _ready_runtime_state(critical_quotes_fresh=False),
    )
    monkeypatch.setattr(
        worker,
        "_run_startup_reconciliation",
        lambda **kwargs: final_passes.append(kwargs),
    )

    def record_state(state, **kwargs):
        transitions.append(state)
        worker.device_state = state

    monkeypatch.setattr(worker, "_set_device_state", record_state)

    worker._advance_startup_readiness()

    assert transitions == [RuntimeDeviceState.STANDBY_READY]
    assert final_passes == [{"execute_commands": False}]
    assert worker._accepting_commands is False


def test_pull_only_successor_cannot_waive_stale_quotes_during_regular_session(
    tmp_path, monkeypatch
):
    worker, _ = _worker(
        tmp_path,
        standby_only=True,
        device_id="successor",
        regular_session_open=lambda: True,
    )
    worker._accepting_commands = False
    worker.device_state = RuntimeDeviceState.STANDBY
    final_passes = []
    monkeypatch.setattr(
        worker,
        "engine_readiness",
        lambda **kwargs: _ready_runtime_state(critical_quotes_fresh=False),
    )
    monkeypatch.setattr(
        worker,
        "_run_startup_reconciliation",
        lambda **kwargs: final_passes.append(kwargs),
    )

    worker._advance_startup_readiness()

    assert worker.device_state == RuntimeDeviceState.STANDBY
    assert worker._accepting_commands is False
    assert final_passes == []


def test_premarket_handoff_does_not_waive_a_non_quote_dependency(
    tmp_path, monkeypatch
):
    worker, _ = _worker(
        tmp_path,
        standby_only=True,
        device_id="successor",
        regular_session_open=lambda: False,
    )
    worker._accepting_commands = False
    worker.device_state = RuntimeDeviceState.STANDBY
    monkeypatch.setattr(
        worker,
        "engine_readiness",
        lambda **kwargs: _ready_runtime_state(
            critical_quotes_fresh=False,
            account_reconciliation_fresh=False,
        ),
    )

    worker._advance_startup_readiness()

    assert worker.device_state == RuntimeDeviceState.STANDBY
    assert worker._accepting_commands is False


def test_main_lease_holder_waits_for_fresh_quote_before_active_execution(
    tmp_path, monkeypatch
):
    quote_fresh = False
    worker, _ = _worker(
        tmp_path,
        device_id="main-device",
        regular_session_open=lambda: False,
    )
    worker._accepting_commands = False
    worker.device_state = RuntimeDeviceState.STANDBY
    monkeypatch.setattr(
        worker,
        "engine_readiness",
        lambda **kwargs: _ready_runtime_state(critical_quotes_fresh=quote_fresh),
    )
    monkeypatch.setattr(worker, "_lease_still_current", lambda: True)
    monkeypatch.setattr(worker, "_run_startup_reconciliation", lambda **kwargs: None)

    worker._advance_startup_readiness()

    assert worker.device_state == RuntimeDeviceState.STANDBY
    assert worker._accepting_commands is False

    quote_fresh = True
    worker._advance_startup_readiness()

    assert worker.device_state == RuntimeDeviceState.ACTIVE
    assert worker._accepting_commands is True


def test_handoff_session_lookup_failure_is_fail_closed(tmp_path):
    def unavailable():
        raise RuntimeError("calendar unavailable")

    worker, _ = _worker(
        tmp_path,
        standby_only=True,
        regular_session_open=unavailable,
    )

    assert worker.lease_handoff_ready(
        _ready_runtime_state(critical_quotes_fresh=False)
    ) is False


def test_standby_readiness_loss_demotes_immediately(tmp_path, monkeypatch):
    worker, engine = _worker(tmp_path, standby_only=True, device_id="successor")
    worker._accepting_commands = False
    worker._set_device_state(RuntimeDeviceState.STANDBY_READY)
    monkeypatch.setattr(
        worker,
        "engine_readiness",
        lambda **kwargs: _ready_runtime_state(websocket_connected=False),
    )

    worker._advance_startup_readiness()

    record = get_runtime_device_state(engine, device_id="successor")
    assert worker.device_state == RuntimeDeviceState.STANDBY
    assert worker._accepting_commands is False
    assert record.state == RuntimeDeviceState.STANDBY


def test_active_persistence_failure_never_opens_the_command_gate(
    tmp_path, monkeypatch
):
    worker, _ = _worker(tmp_path, device_id="candidate")
    worker._accepting_commands = False
    worker.device_state = RuntimeDeviceState.STANDBY
    monkeypatch.setattr(
        worker, "engine_readiness", lambda **kwargs: _ready_runtime_state()
    )
    monkeypatch.setattr(worker, "_lease_still_current", lambda: True)
    monkeypatch.setattr(worker, "_run_startup_reconciliation", lambda **kwargs: None)

    def persist_state(state, **kwargs):
        if state == RuntimeDeviceState.ACTIVE:
            raise RuntimeError("ACTIVE write failed")
        worker.device_state = state

    monkeypatch.setattr(worker, "_set_device_state", persist_state)

    with pytest.raises(RuntimeError, match="ACTIVE write failed"):
        worker._advance_startup_readiness()

    assert worker.device_state == RuntimeDeviceState.STANDBY_READY
    assert worker._accepting_commands is False


def test_runtime_shutdown_orders_journal_reconciliation_and_market_data_close(
    tmp_path, monkeypatch
):
    worker, _ = _worker(tmp_path)
    calls = []

    class _MarketData:
        def configure_desired_channels(self, **kwargs):
            calls.append(("unsubscribe", kwargs))

        def stop(self):
            calls.append(("stop", None))

    worker.runtime = SimpleNamespace(market_data=_MarketData())
    worker._journal_flush = lambda: calls.append(("flush", None))
    monkeypatch.setattr(
        worker,
        "_run_startup_reconciliation",
        lambda **kwargs: calls.append(("reconcile", kwargs)),
    )

    def record_state(state, **kwargs):
        worker.device_state = state
        calls.append(("state", state))

    monkeypatch.setattr(worker, "_set_device_state", record_state)

    worker.request_stop()
    worker._perform_shutdown_sequence()

    assert worker._accepting_commands is False
    assert calls == [
        ("state", RuntimeDeviceState.SHUTTING_DOWN),
        ("flush", None),
        ("reconcile", {"execute_commands": False}),
        ("unsubscribe", {"trade_priorities": {}, "quote_priorities": {}}),
        ("stop", None),
        ("state", RuntimeDeviceState.STOPPED),
    ]
    assert worker.shutdown_prepared is True


def test_standby_stop_breach_survives_promotion_and_initiates_sell_all_once(
    tmp_path
):
    now = dt.datetime.now(dt.timezone.utc)

    class _Transport:
        def __init__(self):
            self.stopped = False

        def on_data(self, callback):
            pass

        def on_ack(self, callback):
            pass

        def on_connection(self, callback):
            pass

        def subscribe(self, subscriptions):
            pass

        def unsubscribe(self, subscriptions):
            pass

        def start(self):
            self.stopped = False

        def stop(self):
            self.stopped = True

        def is_connected(self):
            return True

    service = KisRealtimeMarketDataService(
        transport=_Transport(),
        symbol_key_resolver=lambda symbol, channel: symbol,
        trade_capacity=10,
        quote_capacity=10,
        clock=lambda: now,
    )
    card = TradeCardState(
        environment="PROD",
        account_no="1",
        symbol="AAPL",
        board_status=BoardStatus.OPEN_POSITION,
        broker_quantity=10,
        orderable_quantity=10,
        active_stop_price=100.0,
    )
    service.replace_stop_rules(
        "AAPL", [StopRule(card.card_key, 100.0, "1")]
    )
    for offset, price in enumerate((101.0, 99.0, 101.0)):
        observed = now + dt.timedelta(seconds=offset)
        assert service.ingest_trade(
            QuoteSnapshot(
                symbol="AAPL",
                last_price=price,
                broker_event_at=observed,
                received_at=observed,
                processed_at=observed,
                channel="HDFSCNT0",
                payload_fingerprint=str(offset),
            )
        )

    # Standby observes/drains but cannot acknowledge the breach.
    assert any(quote.breached_stop_versions for quote in service.poll_once())
    standby, _ = _worker(tmp_path, standby_only=True)
    standby.runtime = SimpleNamespace(market_data=service)
    standby._close_market_data()
    promoted_service = standby.runtime.market_data

    sell_all_calls = []

    class _TradingEngine:
        def evaluate_quote(self, cards, quote):
            if quote.breached_stop_versions:
                sell_all_calls.append(quote.breached_stop_versions)
                cards[0].exit_all_required = True
                cards[0].board_status = BoardStatus.SELL_ALL
                return cards
            return []

    active, _ = _worker(tmp_path)
    active.runtime = SimpleNamespace(
        market_data=promoted_service,
        trading_engine=_TradingEngine(),
    )
    for quote in promoted_service.poll_once():
        active.runtime.trading_engine.evaluate_quote([card], quote)
        candidates = set()
        active._collect_market_breach_ack_candidates(quote, [card], candidates)
        active._acknowledge_market_breach_candidates(
            candidates, {card.card_key}
        )
    for quote in promoted_service.poll_once():
        active.runtime.trading_engine.evaluate_quote([card], quote)

    assert card.board_status == BoardStatus.SELL_ALL
    assert len(sell_all_calls) == 1


# --- Startup reconciliation --------------------------------------------------


def test_startup_reconciliation_restores_retry_state_and_persists_changes(tmp_path):
    import datetime as dt

    worker, engine = _worker(tmp_path)
    worker.runtime = _build_test_runtime(
        buying_power_provider=worker._buying_power_provider,
        card_lookup=worker._card_lookup,
        broker=worker._broker,
    )
    retry_at = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=30)
    _seed_card(
        engine,
        board_status=BoardStatus.BUY_TODAY,
        entry_runtime_status=EntryRuntimeStatus.RETRY_COOLDOWN,
        next_retry_at=retry_at,
        entry_attempt_group_id="g1",
        entry_attempt_count=2,
    )

    emitted = []
    worker.board_changed.connect(lambda: emitted.append(True))

    worker._run_startup_reconciliation()

    key = ("PROD", "1", "AAPL")
    state = worker.runtime.entry_attempt_manager._state.get(key)
    assert state is not None
    assert state.attempt_group_id == "g1"
    assert state.attempt_count == 2


def test_startup_reconciliation_discovers_a_manual_broker_position(tmp_path):
    broker = _FakeBroker()
    broker.positions = {
        "overseas": {"holdings": [{"symbol": "NVDA", "quantity": 10, "average_price": 200.0}]}
    }
    worker, engine = _worker(tmp_path, broker=broker)
    worker.runtime = _build_test_runtime(
        buying_power_provider=worker._buying_power_provider,
        card_lookup=worker._card_lookup,
        broker=worker._broker,
    )

    emitted = []
    worker.board_changed.connect(lambda: emitted.append(True))

    worker._run_startup_reconciliation()

    card = repo.get_trade_card(engine, "PROD", "1", "NVDA")
    assert card is not None
    assert card.board_status == BoardStatus.OPEN_POSITION
    assert card.broker_quantity == 10
    assert emitted == [True]


def test_startup_reconciliation_scopes_each_account_to_its_own_holdings(tmp_path):
    """Regression for the multi-account bug: a single unscoped
    ``get_positions(account_no="")`` call previously reconciled *every*
    account's cards against one account's holdings, spuriously discovering
    phantom blank-account-no positions for every other account's real
    holdings. Each real account_no must be queried and reconciled
    independently.
    """
    broker = _FakeBroker()
    broker.positions_by_account = {
        "1": {"overseas": {"holdings": [{"symbol": "AAPL", "quantity": 10, "average_price": 100.0}]}},
        "2": {"overseas": {"holdings": [{"symbol": "MSFT", "quantity": 5, "average_price": 300.0}]}},
    }
    # account_no="" here models the real production wiring
    # (main_window.py's unscoped worker) -- self._distinct_account_numbers
    # must derive both real accounts purely from the seeded cards.
    worker, engine = _worker(tmp_path, broker=broker, account_no="")
    worker.runtime = _build_test_runtime(
        buying_power_provider=worker._buying_power_provider,
        card_lookup=worker._card_lookup,
        broker=worker._broker,
    )
    _seed_card(engine, account_no="1", symbol="AAPL", board_status=BoardStatus.WATCHLIST)
    _seed_card(engine, account_no="2", symbol="MSFT", board_status=BoardStatus.WATCHLIST)

    worker._run_startup_reconciliation()

    aapl = repo.get_trade_card(engine, "PROD", "1", "AAPL")
    msft = repo.get_trade_card(engine, "PROD", "2", "MSFT")
    assert aapl.board_status == BoardStatus.OPEN_POSITION
    assert aapl.broker_quantity == 10
    assert msft.board_status == BoardStatus.OPEN_POSITION
    assert msft.broker_quantity == 5
    # No phantom blank-account-no card was created for either symbol.
    assert repo.get_trade_card(engine, "PROD", "", "AAPL") is None
    assert repo.get_trade_card(engine, "PROD", "", "MSFT") is None
    assert set(broker.get_positions_calls) == {"1", "2"}


def test_distinct_account_numbers_includes_the_workers_own_scoped_account(tmp_path):
    """A specifically-scoped worker must always query its own account even
    before any card exists for it (needed to discover a first manual
    position with zero pre-existing cards)."""
    worker, _ = _worker(tmp_path, account_no="1")
    assert worker._distinct_account_numbers([]) == ["1"]


def test_startup_reconciliation_marks_incomplete_when_one_account_fails(tmp_path):
    """Review finding P0: "startup reconciliation reports success after
    account failures" -- one account's get_positions failure must not
    leave startup_reconciliation_complete True; the health check must be
    able to tell a genuinely-unreconciled account apart from a clean run.
    """

    class _PartiallyFailingBroker(_FakeBroker):
        def get_positions(self, *, environment, account_no=None):
            self.get_positions_calls.append(account_no)
            if account_no == "2":
                raise RuntimeError("simulated KIS outage for account 2")
            return {"overseas": {"holdings": []}}

    broker = _PartiallyFailingBroker()
    worker, engine = _worker(tmp_path, broker=broker, account_no="")
    worker.runtime = _build_test_runtime(
        buying_power_provider=worker._buying_power_provider,
        card_lookup=worker._card_lookup,
        broker=worker._broker,
    )
    _seed_card(engine, account_no="1", symbol="AAPL", board_status=BoardStatus.WATCHLIST)
    _seed_card(engine, account_no="2", symbol="MSFT", board_status=BoardStatus.WATCHLIST)

    worker._run_startup_reconciliation()

    assert worker.startup_reconciliation_complete is False
    assert "2" in worker.startup_reconciliation_errors
    assert "1" not in worker.startup_reconciliation_errors
    # The healthy account's own reconciliation still ran.
    assert "1" in worker._account_reconciled_at
    assert "2" not in worker._account_reconciled_at


def test_startup_reconciliation_does_not_cross_account_boundaries(tmp_path):
    """Review finding P0: "startup order reconciliation is still not
    account-scoped" -- account 1's own reconciliation must not even look
    at account 2's cards. Without scoping, account 1's iteration would
    also process account 2's ENTRY_PENDING card (the unfiltered cards
    list included it) and get wrongly marked failed by account 2's own
    discover_orders callback raising."""

    class _AccountTwoOrderDiscoveryFailingBroker(_FakeBroker):
        def discover_orders(self, *, environment, account_no):
            self.get_positions_calls.append(f"discover:{account_no}")
            if account_no == "2":
                raise RuntimeError("simulated KIS outage discovering account 2's orders")
            return self.discover_result

    broker = _AccountTwoOrderDiscoveryFailingBroker()
    worker, engine = _worker(tmp_path, broker=broker, account_no="")
    worker.runtime = _build_test_runtime(
        buying_power_provider=worker._buying_power_provider,
        card_lookup=worker._card_lookup,
        broker=worker._broker,
    )
    _seed_card(engine, account_no="1", symbol="AAPL", board_status=BoardStatus.WATCHLIST)
    _seed_card(engine, account_no="2", symbol="MSFT", board_status=BoardStatus.ENTRY_PENDING)

    worker._run_startup_reconciliation()

    assert "1" not in worker.startup_reconciliation_errors
    assert "2" in worker.startup_reconciliation_errors
    assert "1" in worker._account_reconciled_at


def test_startup_reconciliation_fully_resolves_a_buy_today_fill_in_one_pass(tmp_path):
    """The account reducer consumes holdings and orders together, so a
    startup fill cannot be stranded behind a second ordered sweep/pass."""
    broker = _FakeBroker()
    broker.discover_result = BrokerOrderDiscoveryResult(
        open_orders_complete=True, history_complete=True, reserved_orders_complete=True,
        snapshots=[
            BrokerOrderStatusSnapshot(
                environment="PROD", account_no="1", symbol="AAPL", side=OrderSide.BUY,
                status=OrderStatus.FILLED, quantity_requested=10, filled_quantity=10,
                avg_fill_price=101.0,
            )
        ],
    )
    broker.positions = {
        "overseas": {"holdings": [{"symbol": "AAPL", "quantity": 10, "average_price": 101.0}]}
    }
    worker, engine = _worker(tmp_path, broker=broker)
    worker.runtime = _build_test_runtime(
        buying_power_provider=worker._buying_power_provider,
        card_lookup=worker._card_lookup,
        broker=worker._broker,
    )
    _seed_card(
        engine, account_no="1", symbol="AAPL", board_status=BoardStatus.BUY_TODAY,
        planned_quantity=10,
    )

    worker._run_startup_reconciliation()

    card = repo.get_trade_card(engine, "PROD", "1", "AAPL")
    # Fully resolved in this one pass, not merely transitioned to
    # ENTRY_PENDING/DATA_UNAVAILABLE and left for a later cycle.
    assert card.board_status == BoardStatus.OPEN_POSITION
    assert card.broker_quantity == 10


def test_run_one_cycle_excludes_cards_from_accounts_with_startup_errors(tmp_path, monkeypatch):
    """Review finding P0: "the worker still enters its normal runtime loop
    ... regardless of whether reconciliation completed successfully" --
    Buy Board must not decide entries/exits for an account whose broker
    truth was never confirmed, even though the worker as a whole keeps
    running (and keeps retrying that account's periodic refresh)."""
    from src.core import execution_config

    monkeypatch.setattr(execution_config, "is_buyboard_engine_enabled", lambda: True)
    import src.services.trading_engine as trading_engine_module

    monkeypatch.setattr(trading_engine_module, "is_buyboard_engine_enabled", lambda: True)

    broker = _FakeBroker()
    worker, engine = _worker(tmp_path, broker=broker)
    worker.runtime = _build_test_runtime(
        buying_power_provider=worker._buying_power_provider,
        card_lookup=worker._card_lookup,
        broker=worker._broker,
        market_data=_dummy_market_data(),
    )
    card = _seed_card(
        engine,
        board_status=BoardStatus.BUY_TODAY,
        entry_runtime_status=EntryRuntimeStatus.RETRY_COOLDOWN,
    )
    import datetime as dt

    card.next_retry_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1)  # already due
    repo.update_trade_card(engine, card, expected_version=card.version)
    # Account "1" never actually reconciled at startup.
    worker.startup_reconciliation_errors = {"1": "simulated KIS outage"}
    # Prevent the periodic-refresh path (which legitimately still runs for
    # every account) from clearing the error out from under this test.
    worker._account_balance_refreshed_at["1"] = dt.datetime.now(dt.timezone.utc)
    worker._account_reconciled_at["1"] = dt.datetime.now(dt.timezone.utc)

    worker._run_one_cycle()

    stored = repo.get_trade_card(engine, "PROD", "1", "AAPL")
    # Never touched: still RETRY_COOLDOWN, not recovered to EXECUTE_READY.
    assert stored.entry_runtime_status == EntryRuntimeStatus.RETRY_COOLDOWN


def test_periodic_reconciliation_success_clears_startup_reconciliation_error(tmp_path, monkeypatch):
    """Review finding P0: "no periodic recovery path that removes an
    account from startup_reconciliation_errors ... this can leave the
    application permanently reporting the Buy Board as unhealthy.\""""
    import datetime as dt

    from src.core import execution_config

    monkeypatch.setattr(execution_config, "is_buyboard_engine_enabled", lambda: True)
    import src.services.trading_engine as trading_engine_module

    monkeypatch.setattr(trading_engine_module, "is_buyboard_engine_enabled", lambda: True)

    broker = _FakeBroker()
    worker, engine = _worker(tmp_path, broker=broker)
    worker.runtime = _build_test_runtime(
        buying_power_provider=worker._buying_power_provider,
        card_lookup=worker._card_lookup,
        broker=worker._broker,
        market_data=_dummy_market_data(),
    )
    _seed_card(engine, board_status=BoardStatus.WATCHLIST)
    # Simulates a prior startup failure for this account that has since
    # become reachable again.
    worker.startup_reconciliation_errors = {"1": "simulated KIS outage"}
    worker.startup_reconciliation_complete = False
    long_ago = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)
    worker._account_balance_refreshed_at["1"] = long_ago
    worker._account_reconciled_at["1"] = long_ago

    worker._run_one_cycle()

    assert "1" not in worker.startup_reconciliation_errors
    assert worker.startup_reconciliation_complete is True


def test_action_specific_readiness_keeps_protective_exit_available_when_unrelated_sources_fail(
    tmp_path,
):
    worker, _ = _worker(tmp_path)
    worker._latest_reconciliation_snapshots["1"] = AccountBrokerSnapshot(
        environment="PROD",
        account_no="1",
        completeness=SnapshotCompleteness(
            holdings_complete=True,
            open_orders_complete=True,
            history_complete=False,
            reserved_orders_complete=False,
            account_balance_complete=False,
        ),
    )
    exit_card = TradeCardState(
        environment="PROD",
        account_no="1",
        symbol="AAPL",
        board_status=BoardStatus.OPEN_POSITION,
        broker_quantity=10,
    )
    entry_card = TradeCardState(
        environment="PROD",
        account_no="1",
        symbol="MSFT",
        board_status=BoardStatus.BUY_TODAY,
    )

    assert worker._card_action_ready(exit_card) is True
    assert worker._card_action_ready(entry_card) is False


def test_due_reconciliation_failure_invalidates_cached_action_readiness(
    tmp_path, monkeypatch
):
    import datetime as dt

    import src.ui.buyboard.runtime_worker as worker_module

    worker, _ = _worker(tmp_path, account_no="1")
    worker.runtime = _build_test_runtime(
        buying_power_provider=worker._buying_power_provider,
        card_lookup=worker._card_lookup,
        broker=worker._broker,
        market_data=_dummy_market_data(),
    )
    worker._latest_reconciliation_snapshots["1"] = AccountBrokerSnapshot(
        environment="PROD",
        account_no="1",
        holdings=(),
        completeness=SnapshotCompleteness(
            holdings_complete=True,
            open_orders_complete=True,
            history_complete=True,
            reserved_orders_complete=True,
            account_balance_complete=True,
        ),
    )
    worker.startup_reconciled_accounts.add("1")
    old = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)
    worker._account_reconciled_at["1"] = old
    worker._account_balance_refreshed_at["1"] = old
    assert worker.account_action_ready("1", "AAPL", "NEW_ENTRY") is True

    def fail_reconciliation(**kwargs):
        raise RuntimeError("current broker snapshot unavailable")

    monkeypatch.setattr(
        worker_module, "run_account_reconciliation_pass", fail_reconciliation
    )
    worker._refresh_account_state_if_due([])

    assert "1" not in worker._latest_reconciliation_snapshots
    assert worker.account_action_ready("1", "AAPL", "NEW_ENTRY") is False
    assert worker.account_action_ready("1", "AAPL", "PROTECTIVE_EXIT") is False
    assert "1" in worker.startup_reconciliation_errors


def test_reconciliation_alert_incidents_are_account_scoped_and_rearm_after_resolution(
    tmp_path,
):
    worker, _ = _worker(tmp_path)
    messages = []
    worker.alert.connect(messages.append)

    def result(account_no, alerts):
        return AccountReconciliationResult(
            snapshot=AccountBrokerSnapshot(
                environment="PROD",
                account_no=account_no,
                completeness=SnapshotCompleteness(),
                snapshot_id=f"snapshot-{account_no}",
            ),
            plan=ReconciliationPlan(
                snapshot_id=f"snapshot-{account_no}", alerts=tuple(alerts)
            ),
        )

    incident = ReconciliationAlert(
        "DISCOVERED_UNOWNED_BROKER_ORDER",
        ReconciliationAlertSeverity.CRITICAL,
        "fenced",
        symbol="AAPL",
        broker_order_id="B-1",
    )
    worker._handle_reconciliation_result(result("1", (incident,)))
    worker._handle_reconciliation_result(result("2", (incident,)))
    worker._handle_reconciliation_result(result("1", ()))
    worker._handle_reconciliation_result(result("1", (incident,)))

    assert len(messages) == 3
    assert "incident 2" in messages[-1]


def test_startup_reconciliation_complete_when_every_account_succeeds(tmp_path):
    worker, engine = _worker(tmp_path, account_no="1")
    worker.runtime = _build_test_runtime(
        buying_power_provider=worker._buying_power_provider,
        card_lookup=worker._card_lookup,
        broker=worker._broker,
    )

    worker._run_startup_reconciliation()

    assert worker.startup_reconciliation_complete is True
    assert worker.startup_reconciliation_errors == {}


def test_distinct_account_numbers_discovers_cardless_configured_accounts(tmp_path):
    """Review finding P1: "accounts without existing cards remain
    undiscoverable" -- a configured KIS account with zero TradeCards must
    still be queried by the unscoped production worker."""
    worker, _ = _worker(
        tmp_path, account_no="", account_discovery=lambda: ["9", "1"]
    )
    card = TradeCardState(environment="PROD", account_no="1", symbol="AAPL")

    accounts = worker._distinct_account_numbers([card])

    assert set(accounts) == {"1", "9"}


def test_distinct_account_numbers_does_not_discover_for_a_scoped_worker(tmp_path):
    """A specifically-scoped worker must not reach into every configured
    account -- only its own."""
    discovery_calls = []
    worker, _ = _worker(
        tmp_path, account_no="1",
        account_discovery=lambda: discovery_calls.append(True) or ["9"],
    )

    accounts = worker._distinct_account_numbers([])

    assert accounts == ["1"]
    assert discovery_calls == []


# --- Periodic buying-power refresh / full reconciliation (review findings) --


def test_periodic_refresh_populates_buying_power_cache_on_first_cycle(tmp_path, monkeypatch):
    from src.core import execution_config
    from src.services import buying_power_cache

    monkeypatch.setattr(execution_config, "is_buyboard_engine_enabled", lambda: True)
    import src.services.trading_engine as trading_engine_module

    monkeypatch.setattr(trading_engine_module, "is_buyboard_engine_enabled", lambda: True)

    broker = _FakeBroker()
    broker.positions = {
        "overseas": {
            "holdings": [],
            "summary_by_exchange": {"NASD": {"cash_balance_usd": 5000.0}},
        }
    }
    worker, engine = _worker(tmp_path, broker=broker)
    worker.runtime = _build_test_runtime(
        buying_power_provider=worker._buying_power_provider,
        card_lookup=worker._card_lookup,
        broker=worker._broker,
        market_data=_dummy_market_data(),
    )
    _seed_card(engine, board_status=BoardStatus.BUY_TODAY)

    worker._run_one_cycle()

    snapshot = buying_power_cache.get_snapshot("PROD", "1")
    assert snapshot is not None
    assert snapshot.usable_buying_power_usd == 5000.0
    assert "1" in worker._account_balance_refreshed_at


def test_periodic_refresh_does_not_requery_before_its_interval(tmp_path, monkeypatch):
    from src.core import execution_config

    monkeypatch.setattr(execution_config, "is_buyboard_engine_enabled", lambda: True)
    import src.services.trading_engine as trading_engine_module

    monkeypatch.setattr(trading_engine_module, "is_buyboard_engine_enabled", lambda: True)

    broker = _FakeBroker()
    worker, engine = _worker(tmp_path, broker=broker)
    worker.runtime = _build_test_runtime(
        buying_power_provider=worker._buying_power_provider,
        card_lookup=worker._card_lookup,
        broker=worker._broker,
        market_data=_dummy_market_data(),
    )
    _seed_card(engine, board_status=BoardStatus.BUY_TODAY)

    worker._run_one_cycle()
    calls_after_first = len(broker.get_positions_calls)
    assert calls_after_first >= 1

    worker._run_one_cycle()  # immediately after -- well within the refresh interval
    assert len(broker.get_positions_calls) == calls_after_first


def test_periodic_refresh_requeries_once_the_interval_has_elapsed(tmp_path, monkeypatch):
    import datetime as dt

    from src.core import execution_config

    monkeypatch.setattr(execution_config, "is_buyboard_engine_enabled", lambda: True)
    import src.services.trading_engine as trading_engine_module

    monkeypatch.setattr(trading_engine_module, "is_buyboard_engine_enabled", lambda: True)

    broker = _FakeBroker()
    worker, engine = _worker(tmp_path, broker=broker)
    worker.runtime = _build_test_runtime(
        buying_power_provider=worker._buying_power_provider,
        card_lookup=worker._card_lookup,
        broker=worker._broker,
        market_data=_dummy_market_data(),
    )
    _seed_card(engine, board_status=BoardStatus.BUY_TODAY)

    worker._run_one_cycle()
    calls_after_first = len(broker.get_positions_calls)

    # Force both cadences to look overdue without waiting in real time.
    long_ago = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)
    worker._account_balance_refreshed_at["1"] = long_ago
    worker._account_reconciled_at["1"] = long_ago

    worker._run_one_cycle()
    assert len(broker.get_positions_calls) == calls_after_first + 1


def test_periodic_reconciliation_discovers_external_position_change(tmp_path, monkeypatch):
    """FULL_RECONCILIATION_SECONDS cadence: a manual sale made mid-session
    (broker quantity dropped to zero) must be discovered without waiting
    for another application restart."""
    import datetime as dt

    from src.core import execution_config

    monkeypatch.setattr(execution_config, "is_buyboard_engine_enabled", lambda: True)
    import src.services.trading_engine as trading_engine_module

    monkeypatch.setattr(trading_engine_module, "is_buyboard_engine_enabled", lambda: True)

    broker = _FakeBroker()
    broker.positions = {"overseas": {"holdings": []}}  # broker now reports flat
    worker, engine = _worker(tmp_path, broker=broker)
    worker.runtime = _build_test_runtime(
        buying_power_provider=worker._buying_power_provider,
        card_lookup=worker._card_lookup,
        broker=worker._broker,
        market_data=_dummy_market_data(),
    )
    _seed_card(
        engine,
        board_status=BoardStatus.OPEN_POSITION,
        broker_quantity=10,
        orderable_quantity=10,
    )
    worker._account_balance_refreshed_at["1"] = dt.datetime.now(dt.timezone.utc)
    worker._account_reconciled_at["1"] = dt.datetime.now(dt.timezone.utc) - dt.timedelta(
        seconds=execution_config.FULL_RECONCILIATION_SECONDS + 1
    )

    worker._run_one_cycle()

    card = repo.get_trade_card(engine, "PROD", "1", "AAPL")
    assert card.board_status == BoardStatus.CLOSED
    assert card.broker_quantity == 0


def test_periodic_refresh_also_reconciles_unresolved_entry_order_state(tmp_path, monkeypatch):
    """Review finding P1: "full reconciliation still reconciles positions,
    not the full account" -- the periodic pass must also resolve an
    ENTRY_PENDING card whose local order lookup finds nothing (not just
    broker positions), the same way startup reconciliation already does."""
    import datetime as dt

    from src.core import execution_config

    monkeypatch.setattr(execution_config, "is_buyboard_engine_enabled", lambda: True)
    import src.services.trading_engine as trading_engine_module

    monkeypatch.setattr(trading_engine_module, "is_buyboard_engine_enabled", lambda: True)

    broker = _FakeBroker()  # discover_orders defaults to a complete, empty result
    worker, engine = _worker(tmp_path, broker=broker)
    worker.runtime = _build_test_runtime(
        buying_power_provider=worker._buying_power_provider,
        card_lookup=worker._card_lookup,
        broker=worker._broker,
        market_data=_dummy_market_data(),
    )
    _seed_card(engine, board_status=BoardStatus.ENTRY_PENDING, broker_quantity=0)
    worker._account_balance_refreshed_at["1"] = dt.datetime.now(dt.timezone.utc)
    worker._account_reconciled_at["1"] = dt.datetime.now(dt.timezone.utc) - dt.timedelta(
        seconds=execution_config.FULL_RECONCILIATION_SECONDS + 1
    )

    worker._run_one_cycle()

    card = repo.get_trade_card(engine, "PROD", "1", "AAPL")
    # No order was ever actually submitted (empty local ledger); a complete
    # broker-wide discovery finding nothing means the entry never went
    # through -- returns to Buylist rather than sitting stuck forever.
    assert card.board_status == BoardStatus.BUYLIST


# --- Stalled-liquidation critical alert (review: "a card warning is --------
# --- insufficient when the user is asleep") ---------------------------------


def test_stalled_cancel_warning_fires_alert_exactly_once(tmp_path):
    worker, _ = _worker(tmp_path)
    alerts = []
    worker.alert.connect(alerts.append)
    card = TradeCardState(
        environment="PROD", account_no="1", symbol="AAPL",
        board_status=BoardStatus.SELL_ALL, warnings=["EXIT_CANCEL_STALLED"],
    )

    worker._emit_stalled_liquidation_alerts([card])
    worker._emit_stalled_liquidation_alerts([card])  # still stalled next tick

    assert len(alerts) == 1
    assert "CRITICAL" in alerts[0]
    assert "AAPL" in alerts[0]


def test_stalled_cancel_alert_fires_again_after_resolving_and_recurring(tmp_path):
    worker, _ = _worker(tmp_path)
    alerts = []
    worker.alert.connect(alerts.append)
    card = TradeCardState(
        environment="PROD", account_no="1", symbol="AAPL",
        board_status=BoardStatus.SELL_ALL, warnings=["EXIT_CANCEL_STALLED"],
    )

    worker._emit_stalled_liquidation_alerts([card])
    card.warnings = []  # resolved
    worker._emit_stalled_liquidation_alerts([card])
    card.warnings = ["EXIT_CANCEL_STALLED"]  # stalls again
    worker._emit_stalled_liquidation_alerts([card])

    assert len(alerts) == 2


def test_no_stalled_warning_does_not_alert(tmp_path):
    worker, _ = _worker(tmp_path)
    alerts = []
    worker.alert.connect(alerts.append)
    card = TradeCardState(
        environment="PROD", account_no="1", symbol="AAPL", board_status=BoardStatus.SELL_ALL,
    )

    worker._emit_stalled_liquidation_alerts([card])
    assert alerts == []


def test_execution_stale_warning_is_local_and_recovery_resolves_incident(tmp_path):
    worker, _ = _worker(tmp_path)
    native_alerts = []
    resolutions = []

    class Alerting:
        def resolve_alert(self, alert_type, dedupe_key, *, resolved_by):
            resolutions.append((alert_type, dedupe_key, resolved_by))
            return True

    worker._external_alerting = Alerting()
    worker.alert.connect(native_alerts.append)
    card = TradeCardState(
        environment="PROD",
        account_no="1",
        symbol="STIM",
        board_status=BoardStatus.CLOSED,
        warnings=["DATA_STALE"],
    )

    worker._emit_stalled_liquidation_alerts([card])
    assert native_alerts == []

    card.warnings = []
    worker._emit_stalled_liquidation_alerts([card])

    stale_resolutions = [
        item
        for item in resolutions
        if item[0] == CriticalAlertType.STALE_CRITICAL_SYMBOL
    ]
    assert len(stale_resolutions) == 1
    assert stale_resolutions[0][1].endswith(":STIM:DATA_STALE")


def test_unreconciled_broker_order_warning_fires_alert_exactly_once(tmp_path):
    """Review finding P1: "UNRECONCILED_BROKER_ORDER should be a critical
    notification" -- not merely a card warning."""
    worker, _ = _worker(tmp_path)
    alerts = []
    worker.alert.connect(alerts.append)
    card = TradeCardState(
        environment="PROD", account_no="1", symbol="AAPL",
        board_status=BoardStatus.ENTRY_PENDING, warnings=["UNRECONCILED_BROKER_ORDER"],
    )

    worker._emit_stalled_liquidation_alerts([card])
    worker._emit_stalled_liquidation_alerts([card])  # still present next tick

    assert len(alerts) == 1
    assert "CRITICAL" in alerts[0]
    assert "AAPL" in alerts[0]


def test_exit_cancel_stalled_and_unreconciled_broker_order_alert_independently(tmp_path):
    """Two different critical warnings on two different cards must each
    alert -- one warning's dedup state must not suppress the other."""
    worker, _ = _worker(tmp_path)
    alerts = []
    worker.alert.connect(alerts.append)
    stalled_exit = TradeCardState(
        environment="PROD", account_no="1", symbol="AAPL",
        board_status=BoardStatus.SELL_ALL, warnings=["EXIT_CANCEL_STALLED"],
    )
    unreconciled_order = TradeCardState(
        environment="PROD", account_no="1", symbol="MSFT",
        board_status=BoardStatus.ENTRY_PENDING, warnings=["UNRECONCILED_BROKER_ORDER"],
    )

    worker._emit_stalled_liquidation_alerts([stalled_exit, unreconciled_order])

    assert len(alerts) == 2
    assert any("AAPL" in message for message in alerts)
    assert any("MSFT" in message for message in alerts)


# --- One heartbeat cycle ------------------------------------------------------


def test_one_cycle_persists_engine_changes(tmp_path, monkeypatch):
    from src.core import execution_config

    monkeypatch.setattr(execution_config, "is_buyboard_engine_enabled", lambda: True)
    import src.services.trading_engine as trading_engine_module

    monkeypatch.setattr(trading_engine_module, "is_buyboard_engine_enabled", lambda: True)

    worker, engine = _worker(tmp_path)
    worker.runtime = _build_test_runtime(
        buying_power_provider=worker._buying_power_provider,
        card_lookup=worker._card_lookup,
        broker=worker._broker,
        market_data=_dummy_market_data(),
    )
    card = _seed_card(
        engine,
        board_status=BoardStatus.BUY_TODAY,
        entry_runtime_status=EntryRuntimeStatus.RETRY_COOLDOWN,
    )
    import datetime as dt

    card.next_retry_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1)  # already due
    repo.update_trade_card(engine, card, expected_version=card.version)

    emitted = []
    worker.board_changed.connect(lambda: emitted.append(True))

    worker._run_one_cycle()

    stored = repo.get_trade_card(engine, "PROD", "1", "AAPL")
    assert stored.entry_runtime_status == EntryRuntimeStatus.EXECUTE_READY
    assert emitted == [True]


def test_one_cycle_scoped_to_the_workers_account(tmp_path, monkeypatch):
    from src.core import execution_config

    monkeypatch.setattr(execution_config, "is_buyboard_engine_enabled", lambda: True)
    import src.services.trading_engine as trading_engine_module

    monkeypatch.setattr(trading_engine_module, "is_buyboard_engine_enabled", lambda: True)

    worker, engine = _worker(tmp_path)
    worker.runtime = _build_test_runtime(
        buying_power_provider=worker._buying_power_provider,
        card_lookup=worker._card_lookup,
        broker=worker._broker,
    )
    _seed_card(engine, account_no="2", board_status=BoardStatus.WATCHLIST)

    worker._run_one_cycle()  # must not raise looking up a foreign-account card


# --- ORB plan sync (review finding P0-2) -------------------------------------


def test_sync_orb_plans_applies_the_execution_queue_bridge(tmp_path):
    worker, engine = _worker(tmp_path)
    worker.runtime = _build_test_runtime(
        buying_power_provider=worker._buying_power_provider,
        card_lookup=worker._card_lookup,
        broker=worker._broker,
    )
    card = _seed_card(engine, board_status=BoardStatus.BUY_TODAY)
    candidate = OrbCandidate(
        symbol="AAPL", window="5m", orb_low=95.0, orb_high=101.0, entry_trigger=101.5,
        shares=10, status=OrbCandidateStatus.EXECUTE_READY,
    )
    item = ExecutionQueueItem(symbol="AAPL", environment="PROD")
    item.selected_candidate = candidate
    worker._execution_queue_item_lookup = lambda symbol, env: item

    changed = worker._sync_orb_plans([card])

    assert changed == [card]
    assert card.entry_runtime_status == EntryRuntimeStatus.EXECUTE_READY
    assert card.entry_trigger == 101.5


def test_sync_orb_plans_is_a_no_op_without_a_lookup_wired(tmp_path):
    worker, engine = _worker(tmp_path)
    card = _seed_card(engine, board_status=BoardStatus.BUY_TODAY)
    assert worker._sync_orb_plans([card]) == []


def test_sync_orb_plans_skips_positioned_cards(tmp_path):
    worker, engine = _worker(tmp_path)
    card = _seed_card(engine, board_status=BoardStatus.OPEN_POSITION, broker_quantity=10)
    called = []
    worker._execution_queue_item_lookup = lambda symbol, env: called.append(symbol) or None
    worker._sync_orb_plans([card])
    assert called == []


# --- Quote subscription sync -------------------------------------------------


def test_sync_quote_subscriptions_adds_and_removes(tmp_path):
    worker, engine = _worker(tmp_path)
    worker.runtime = _build_test_runtime(
        buying_power_provider=worker._buying_power_provider,
        card_lookup=worker._card_lookup,
        broker=worker._broker,
    )
    worker.runtime.market_data.subscribe(["STALE"])
    open_card = _seed_card(engine, symbol="AAPL", board_status=BoardStatus.OPEN_POSITION)

    worker._sync_quote_subscriptions([open_card])

    subscribed = set(worker.runtime.market_data.subscribed_symbols())
    assert "AAPL" in subscribed
    assert "STALE" not in subscribed


def test_controlled_live_planning_cards_are_display_only_and_not_executable(
    tmp_path, monkeypatch
):
    worker, _ = _worker(tmp_path)
    configured = {}

    class _MarketData:
        def configure_desired_channels(self, **kwargs):
            configured.update(kwargs)

    worker.runtime = SimpleNamespace(market_data=_MarketData())
    monkeypatch.setattr(
        execution_config, "is_buyboard_engine_enabled", lambda: True
    )
    monkeypatch.setattr(execution_config, "KIS_LIVE_EXECUTION_MODE", "CONTROLLED_LIVE")
    monkeypatch.setattr(execution_config, "KIS_CONTROLLED_LIVE_SYMBOLS", ("STIM",))
    planning = TradeCardState(
        environment="PROD",
        account_no="1",
        symbol="CDNA",
        board_status=BoardStatus.BUY_TODAY,
    )
    position = TradeCardState(
        environment="PROD",
        account_no="1",
        symbol="STIM",
        board_status=BoardStatus.OPEN_POSITION,
        broker_quantity=10,
    )

    worker._sync_quote_subscriptions([planning, position])

    assert configured["trade_priorities"] == {
        "CDNA": int(SubscriptionPriority.DISPLAY_ONLY),
        "STIM": int(SubscriptionPriority.OPEN_POSITION),
    }
    assert configured["quote_priorities"] == {
        "CDNA": int(SubscriptionPriority.DISPLAY_ONLY),
        "STIM": int(SubscriptionPriority.CRITICAL_EXIT),
    }
    assert worker._card_in_execution_scope(planning) is False
    assert worker._card_in_execution_scope(position) is True


def test_controlled_live_allowlisted_buy_today_remains_execution_critical(
    tmp_path, monkeypatch
):
    worker, _ = _worker(tmp_path)
    configured = {}

    class _MarketData:
        def configure_desired_channels(self, **kwargs):
            configured.update(kwargs)

    worker.runtime = SimpleNamespace(market_data=_MarketData())
    monkeypatch.setattr(
        execution_config, "is_buyboard_engine_enabled", lambda: True
    )
    monkeypatch.setattr(execution_config, "KIS_LIVE_EXECUTION_MODE", "CONTROLLED_LIVE")
    monkeypatch.setattr(execution_config, "KIS_CONTROLLED_LIVE_SYMBOLS", ("STIM",))
    candidate = TradeCardState(
        environment="PROD",
        account_no="1",
        symbol="STIM",
        board_status=BoardStatus.BUY_TODAY,
    )

    worker._sync_quote_subscriptions([candidate])

    assert configured["trade_priorities"]["STIM"] == int(
        SubscriptionPriority.BUY_TODAY
    )
    assert configured["quote_priorities"]["STIM"] == int(
        SubscriptionPriority.BUY_TODAY
    )
    assert worker._card_in_execution_scope(candidate) is True


class _AccountExecutionBroker(FakeExecutionBroker):
    def __init__(self):
        super().__init__()
        self.discovery = BrokerOrderDiscoveryResult(
            open_orders_complete=True,
            history_complete=True,
            reserved_orders_complete=True,
        )
        self.positions = {
            "overseas": {
                "holdings": [
                    {
                        "symbol": "AAPL",
                        "quantity": 10,
                        "sellable_quantity": 10,
                        "average_price": 100.0,
                    }
                ],
                "summary_by_exchange": {
                    "NASD": {"cash_balance_usd": 100_000.0}
                },
            }
        }

    def discover_orders(self, **kwargs):
        return self.discovery

    def get_positions(self, **kwargs):
        return self.positions


@pytest.mark.usefixtures("trading_enabled")
def test_external_buy_snapshot_fences_the_following_runtime_heartbeat(
    tmp_path, monkeypatch
):
    broker = _AccountExecutionBroker()
    broker.positions["overseas"]["holdings"] = []
    broker.discovery = BrokerOrderDiscoveryResult(
        open_orders_complete=True,
        history_complete=True,
        reserved_orders_complete=True,
        snapshots=[
            BrokerOrderStatusSnapshot(
                environment="PROD",
                account_no="1",
                symbol="AAPL",
                broker_order_id="B-EXTERNAL-BUY",
                side=OrderSide.BUY,
                status=OrderStatus.WORKING,
                quantity_requested=10,
                remaining_quantity=10,
                limit_price=100.0,
            )
        ],
    )
    worker, engine, _ = _guarded_reconciliation_worker(
        tmp_path, monkeypatch, broker
    )
    worker.runtime.trading_engine._market_is_open_fn = lambda: True
    _seed_card(
        engine,
        board_status=BoardStatus.BUY_TODAY,
        entry_runtime_status=EntryRuntimeStatus.EXECUTE_READY,
        planned_quantity=10,
        target_position_quantity=10,
        entry_trigger=100.0,
        breakout_price=100.0,
        entry_orb_low=95.0,
        selected_orb_window="5m",
        risk_percent=0.01,
    )

    worker._run_startup_reconciliation()
    worker._run_one_cycle()

    assert broker.submit_calls == []


@pytest.mark.usefixtures("trading_enabled")
def test_reconciliation_emergency_command_reaches_fake_broker(tmp_path, monkeypatch):
    broker = _AccountExecutionBroker()
    broker.queue_acceptance(broker_order_id="B-EMERGENCY")
    worker, engine, _ = _guarded_reconciliation_worker(
        tmp_path, monkeypatch, broker
    )
    _seed_card(
        engine,
        board_status=BoardStatus.SELL_ALL,
        broker_quantity=10,
        orderable_quantity=10,
        exit_all_required=True,
    )

    worker._run_startup_reconciliation()

    assert len(broker.submit_calls) == 1
    assert broker.submit_calls[0]["side"] == OrderSide.SELL
    assert broker.submit_calls[0]["quantity"] == 10


@pytest.mark.usefixtures("trading_enabled")
def test_reconciliation_cancel_command_reaches_fake_broker(tmp_path, monkeypatch):
    broker = _AccountExecutionBroker()
    broker.discovery = BrokerOrderDiscoveryResult(
        open_orders_complete=True,
        history_complete=True,
        reserved_orders_complete=True,
        snapshots=[
            BrokerOrderStatusSnapshot(
                environment="PROD",
                account_no="1",
                symbol="AAPL",
                broker_order_id="B-WORKING-SELL",
                side=OrderSide.SELL,
                status=OrderStatus.WORKING,
                quantity_requested=4,
                remaining_quantity=4,
            )
        ],
    )
    broker.queue_cancel_confirmed()
    worker, engine, _ = _guarded_reconciliation_worker(
        tmp_path, monkeypatch, broker
    )
    _seed_card(
        engine,
        board_status=BoardStatus.SELL_ALL,
        broker_quantity=10,
        orderable_quantity=6,
        exit_all_required=True,
        exit_client_order_id="SELL-1",
    )
    record_execution_order(
        engine,
        ExecutionOrderRecord(
            environment="PROD",
            account_no="1",
            symbol="AAPL",
            side=OrderSide.SELL,
            intent=OrderIntent.MANUAL_EXIT,
            client_order_id="SELL-1",
            broker_order_id="B-WORKING-SELL",
            submitted_quantity=4,
            submitted_limit_price=100.0,
            status=ExecutionOrderStatus.WORKING,
            broker_identity_status=BrokerIdentityStatus.EXACT,
            remaining_quantity=4,
        ),
    )

    worker._run_startup_reconciliation()

    assert len(broker.cancel_calls) == 1
    assert broker.cancel_calls[0]["broker_order_id"] == "B-WORKING-SELL"
    assert broker.submit_calls == []


@pytest.mark.usefixtures("trading_enabled")
def test_ambiguous_reconciliation_cancel_is_not_called_twice(
    tmp_path, monkeypatch
):
    broker = _AccountExecutionBroker()
    broker.discovery = BrokerOrderDiscoveryResult(
        open_orders_complete=True,
        history_complete=True,
        reserved_orders_complete=True,
        snapshots=[
            BrokerOrderStatusSnapshot(
                environment="PROD",
                account_no="1",
                symbol="AAPL",
                broker_order_id="B-WORKING-SELL",
                side=OrderSide.SELL,
                status=OrderStatus.WORKING,
                quantity_requested=4,
                remaining_quantity=4,
            )
        ],
    )
    broker.queue_cancel_timeout()
    worker, engine, _ = _guarded_reconciliation_worker(
        tmp_path, monkeypatch, broker
    )
    _seed_card(
        engine,
        board_status=BoardStatus.SELL_ALL,
        broker_quantity=10,
        orderable_quantity=6,
        exit_all_required=True,
        exit_client_order_id="SELL-1",
    )
    record_execution_order(
        engine,
        ExecutionOrderRecord(
            environment="PROD",
            account_no="1",
            symbol="AAPL",
            side=OrderSide.SELL,
            intent=OrderIntent.MANUAL_EXIT,
            client_order_id="SELL-1",
            broker_order_id="B-WORKING-SELL",
            submitted_quantity=4,
            submitted_limit_price=100.0,
            status=ExecutionOrderStatus.WORKING,
            broker_identity_status=BrokerIdentityStatus.EXACT,
            remaining_quantity=4,
        ),
    )

    worker._run_startup_reconciliation()
    first_card = repo.get_trade_card(engine, "PROD", "1", "AAPL")
    stable_cancel_id = first_card.exit_cancel_command_id
    worker._run_startup_reconciliation()

    assert len(broker.cancel_calls) == 1
    stored = repo.get_trade_card(engine, "PROD", "1", "AAPL")
    assert stored.exit_cancel_command_id == stable_cancel_id
    assert stored.exit_cancel_in_flight is True


@pytest.mark.usefixtures("trading_enabled")
def test_ambiguous_reconciliation_submission_is_not_called_twice(
    tmp_path, monkeypatch
):
    broker = _AccountExecutionBroker()
    broker.queue_timeout()
    worker, engine, _ = _guarded_reconciliation_worker(
        tmp_path, monkeypatch, broker
    )
    _seed_card(
        engine,
        board_status=BoardStatus.SELL_ALL,
        broker_quantity=10,
        orderable_quantity=10,
        exit_all_required=True,
    )

    worker._run_startup_reconciliation()
    worker._run_startup_reconciliation()

    assert len(broker.submit_calls) == 1
    stored = repo.get_trade_card(engine, "PROD", "1", "AAPL")
    assert stored.exit_client_order_id
    assert stored.exit_submission_unresolved is True


# --- Persistence isolation ----------------------------------------------------


def test_persist_changed_swallows_a_stale_version_conflict(tmp_path):
    worker, engine = _worker(tmp_path)
    card = _seed_card(engine, board_status=BoardStatus.WATCHLIST)
    card.version = 999  # deliberately stale
    # Must not raise -- the worker logs and moves on to the next cycle.
    worker._persist_changed([card])


# --- Lease fencing (review finding P0-1) -------------------------------------


def test_lease_still_current_true_without_execution_authority(tmp_path):
    worker, _ = _worker(tmp_path)
    assert worker._lease_still_current() is True


def test_lease_still_current_false_once_expired(tmp_path):
    class _AlwaysExpired(ExecutionAuthority):
        def require_current_lease(self, engine, expected):
            raise LeaseExpiredError("expired")

    worker, _ = _worker(
        tmp_path,
        execution_authority=_AlwaysExpired(),
        execution_lease=LeaseHandle(device_id="other-device", lease_token="tok"),
    )
    assert worker._lease_still_current() is False
