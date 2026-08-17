"""L3's eight-row legacy/Kanban behavioral parity matrix.

These tests compare durable domain intents and normalized workflow results.
They deliberately do not assert on columns, labels, or any other UI-only
state.  Broker rows exercise both ``ExecutionSource`` values through the
shared :mod:`execution_workflow_service` boundary.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace

from sqlalchemy import create_engine

from src.core.account_broker_snapshot import (
    AccountBrokerSnapshot,
    AccountHoldingSnapshot,
    SnapshotCompleteness,
)
from src.core.board_workflow import (
    ActivateForToday,
    BoardActionContext,
    CancelEntry,
    RequestPartialSell,
    RequestSellAll,
    SetBreakevenStop,
    SetManualStop,
)
from src.core.execution_mode import ExecutionSource
from src.core.execution_order_record import (
    BrokerIdentityStatus,
    ExecutionOrderRecord,
    ExecutionOrderStatus,
)
from src.core.execution_request import CancelIntent
from src.core.execution_result import ExecutionSubmissionResult
from src.core.order_state import (
    REGULAR_LIMIT_EXECUTION,
    RESERVED_MOO_EXECUTION,
    BrokerOrder,
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
from src.services import execution_workflow_service as workflow
from src.services import buyboard_runtime as runtime_module
from src.services import trade_card_repository as card_repo
from src.services.account_reconciliation import (
    AccountLocalState,
    reduce_account_reconciliation,
)
from src.services.entry_attempt_manager import EntryAttemptManager
from src.services import eod_trading_service as eod_module
from src.services.eod_trading_service import EodActionCallbacks, EodTradingService
from src.services.position_manager import PositionManager
from src.services.realtime_market_data import QuoteSnapshot, RestPollingMarketDataService
from src.ui.buylist import orders as legacy_orders_module
from src.ui.buylist import actions as legacy_actions_module
from src.ui.buylist import monitoring as legacy_monitoring_module
from src.ui.buylist.actions import BuylistActionsMixin
from src.ui.buylist.orders import BuylistOrdersMixin
from src.ui.buylist.monitoring import BuylistMonitoringMixin


def _engine(tmp_path, name: str):
    return create_engine(f"sqlite:///{tmp_path / name}", future=True)


def _card(**overrides) -> TradeCardState:
    fields = dict(
        environment="PROD",
        account_no="1",
        symbol="AAPL",
        board_status=BoardStatus.OPEN_POSITION,
        position_runtime_status=PositionRuntimeStatus.OPEN,
        broker_quantity=100,
        orderable_quantity=100,
        average_entry_price=100.0,
        entry_orb_low=95.0,
        stop_type=StopType.ORB_LOW,
        active_stop_price=95.0,
    )
    fields.update(overrides)
    return TradeCardState(**fields)


def _persist_card(engine, **overrides) -> TradeCardState:
    return card_repo.create_trade_card(engine, _card(**overrides))


def _command(command_type, card: TradeCardState, **kwargs):
    return command_type(
        environment=card.environment,
        account_no=card.account_no,
        symbol=card.symbol,
        expected_card_version=card.version,
        **kwargs,
    )


def _capture_real_legacy_exit_command(
    monkeypatch,
    *,
    quantity: int,
    reason: str,
    regular_session_open: bool,
    order_price: float | None = None,
):
    captured = []

    class _Signal:
        def connect(self, callback):
            pass

    class _Worker:
        def __init__(self, *args, **kwargs):
            captured.append(kwargs["exit_command"])
            self.finished_order = _Signal()
            self.error_occurred = _Signal()

        def start(self):
            pass

    window = BuylistOrdersMixin()
    window._first_account_no_for_environment = lambda environment: "1"
    window._has_open_sell_order = lambda *args: False
    window._manual_sell_execution_policy = lambda environment: (
        REGULAR_LIMIT_EXECUTION
        if regular_session_open
        else RESERVED_MOO_EXECUTION
    )
    window._current_execution_lease_kwargs = lambda: {}
    window.append_log = lambda message: None
    monkeypatch.setattr(legacy_orders_module, "KisOrderWorker", _Worker)
    submit_kwargs = {}
    if order_price is not None:
        submit_kwargs["order_price"] = order_price
    window._submit_kis_sell_order(
        SimpleNamespace(
            symbol="AAPL",
            environment="PROD",
            kis_account_no="1",
            current_price=100.0,
            _stop_order_pending=False,
            _exit_order_pending=False,
        ),
        quantity,
        reason,
        **submit_kwargs,
    )
    return captured[0]


def _capture_real_kanban_exit_command(
    monkeypatch,
    *,
    card: TradeCardState,
    quantity: int,
    reason: str,
    regular_session_open: bool,
):
    captured = []

    def capture(*, command, **kwargs):
        captured.append(command)
        order = BrokerOrder.create(
            environment=command.environment,
            account_no=command.account_no,
            symbol=command.symbol,
            side=command.side,
            intent=command.intent,
            quantity_requested=command.quantity,
            limit_price=command.limit_price,
            execution_policy=command.execution_policy,
            status=OrderStatus.ACCEPTED,
        )
        return ExecutionSubmissionResult.from_broker_order(order)

    monkeypatch.setattr(runtime_module, "request_exit_submit", capture)
    market_data = RestPollingMarketDataService(
        quote_fetcher=lambda symbol: QuoteSnapshot(
            symbol=symbol, last_price=100.0, bid=100.0
        )
    )
    market_data.subscribe([card.symbol])
    market_data.poll_once()
    runtime = runtime_module.build_buyboard_runtime(
        buying_power_provider=lambda *_: 100_000.0,
        card_lookup=lambda *_: card,
        broker=object(),
        market_data=market_data,
    )
    runtime.trading_engine._market_is_open_fn = lambda: regular_session_open
    result = runtime.trading_engine._position_callbacks.submit_sell_order(
        environment=card.environment,
        account_no=card.account_no,
        symbol=card.symbol,
        quantity=quantity,
        reason=reason,
        trade_card=card,
    )
    return captured[0], result


def test_l3_add_to_buy_today_produces_the_same_entry_monitoring_command(
    tmp_path, monkeypatch
):
    from src.core.entry_monitoring_command import build_entry_monitoring_command

    captured = {}

    def legacy_builder(**kwargs):
        captured["legacy"] = build_entry_monitoring_command(**kwargs)
        return captured["legacy"]

    def kanban_builder(**kwargs):
        captured["kanban"] = build_entry_monitoring_command(**kwargs)
        return captured["kanban"]

    monkeypatch.setattr(
        legacy_actions_module, "build_entry_monitoring_command", legacy_builder
    )
    monkeypatch.setattr(workflow, "build_entry_monitoring_command", kanban_builder)
    monkeypatch.setattr(
        legacy_actions_module.QMessageBox,
        "information",
        lambda *args, **kwargs: None,
    )
    legacy_item = SimpleNamespace(
        symbol="AAPL",
        monitoring_status="WAITING_BREAKOUT",
        kis_account_no="",
        orb_monitor_enabled=False,
    )
    legacy = BuylistActionsMixin()
    legacy._buylist_selected_item = lambda environment: legacy_item
    legacy._is_execution_queue_buylist_item = lambda item: True
    legacy._selected_order_account_for_item = lambda item, environment: "1"
    legacy._buylist_prod_monitor_active = True
    legacy._save_state = lambda: None
    legacy.populate_buylist_dashboard = lambda: None
    legacy._buylist_activate_selected("PROD")

    kanban_engine = _engine(tmp_path, "kanban-activation.db")
    kanban_card = _persist_card(
        kanban_engine,
        board_status=BoardStatus.BUYLIST,
        position_runtime_status=PositionRuntimeStatus.NONE,
        broker_quantity=0,
        orderable_quantity=0,
    )

    kanban_command = _command(ActivateForToday, kanban_card)
    kanban_result = workflow.request_board_action(
        kanban_engine, kanban_command, context=BoardActionContext()
    ).card

    assert captured["legacy"] == captured["kanban"]
    assert legacy_item.orb_monitor_enabled is True
    assert kanban_result.board_status == BoardStatus.BUY_TODAY


def test_l3_pending_entry_cancel_produces_the_same_cancel_intent(monkeypatch):
    calls = []
    terminal = BrokerOrder.create(
        environment="PROD",
        account_no="1",
        symbol="AAPL",
        side=OrderSide.BUY,
        intent=OrderIntent.ENTRY,
        quantity_requested=10,
        limit_price=100.0,
    )
    terminal.client_order_id = "ENTRY-CID"
    terminal.broker_order_id = "ENTRY-BROKER-ID"
    terminal.status = OrderStatus.CANCELLED

    def fake_cancel(client_order_id, *, path, broker):
        calls.append((broker._source, client_order_id))
        return deepcopy(terminal)

    monkeypatch.setattr(workflow, "cancel_and_reconcile_order", fake_cancel)
    legacy = workflow.request_cancel(
        source=ExecutionSource.LEGACY_BUY_DASHBOARD,
        client_order_id="ENTRY-CID",
        gateway=object(),
    )
    kanban = workflow.request_cancel_intent(
        CancelIntent(
            client_order_id="ENTRY-CID",
            cancel_command_id="KANBAN-CANCEL",
            environment="PROD",
            account_no="1",
            source=ExecutionSource.KANBAN_BOARD,
            lease=None,
            strategy_instance_id="",
        ),
        gateway=object(),
    )

    assert [source for source, _ in calls] == [
        ExecutionSource.LEGACY_BUY_DASHBOARD,
        ExecutionSource.KANBAN_BOARD,
    ]
    assert calls[0][1] == calls[1][1] == "ENTRY-CID"
    assert legacy == kanban


def test_l3_partial_sell_produces_equal_submission_result_and_command(
    monkeypatch, tmp_path
):
    engine = _engine(tmp_path, "partial-sell.db")
    card = _persist_card(engine)
    requested = workflow.request_board_action(
        engine,
        _command(RequestPartialSell, card, quantity=30),
        context=BoardActionContext(),
    ).card

    legacy = _capture_real_legacy_exit_command(
        monkeypatch,
        quantity=requested.pending_partial_sell_quantity,
        reason="partial sell",
        regular_session_open=True,
    )
    kanban, result = _capture_real_kanban_exit_command(
        monkeypatch,
        card=requested,
        quantity=requested.pending_partial_sell_quantity,
        reason="partial_sell",
        regular_session_open=True,
    )
    assert legacy == kanban
    assert result.status.value == "ACKNOWLEDGED"
    assert kanban.intent == OrderIntent.PARTIAL_EXIT
    assert kanban.quantity == 30


def test_l3_sell_all_produces_equal_submission_result_and_command(
    monkeypatch, tmp_path
):
    engine = _engine(tmp_path, "sell-all.db")
    card = _persist_card(engine)
    requested = workflow.request_board_action(
        engine,
        _command(RequestSellAll, card),
        context=BoardActionContext(),
    ).card

    legacy = _capture_real_legacy_exit_command(
        monkeypatch,
        quantity=requested.orderable_quantity,
        reason="manual sell all",
        regular_session_open=True,
    )
    kanban, result = _capture_real_kanban_exit_command(
        monkeypatch,
        card=requested,
        quantity=requested.orderable_quantity,
        reason="sell_all",
        regular_session_open=True,
    )
    assert requested.exit_all_required is True
    assert legacy == kanban
    assert result.status.value == "ACKNOWLEDGED"
    assert kanban.intent == OrderIntent.MANUAL_EXIT
    assert kanban.quantity == 100


def test_l3_stop_change_produces_the_same_protective_domain_state(
    tmp_path, monkeypatch
):
    from src.core.stop_change_command import build_stop_change_command

    captured = {}

    def legacy_builder(**kwargs):
        captured["legacy"] = build_stop_change_command(**kwargs)
        return captured["legacy"]

    def kanban_builder(**kwargs):
        captured["kanban"] = build_stop_change_command(**kwargs)
        return captured["kanban"]

    monkeypatch.setattr(
        legacy_actions_module, "build_stop_change_command", legacy_builder
    )
    monkeypatch.setattr(workflow, "build_stop_change_command", kanban_builder)
    monkeypatch.setattr(
        legacy_actions_module.QMessageBox,
        "question",
        lambda *args, **kwargs: legacy_actions_module.QMessageBox.Yes,
    )
    item = SimpleNamespace(
        symbol="AAPL",
        environment="PROD",
        kis_account_no="1",
        monitoring_status="BOUGHT",
        shares_held=100,
        avg_cost=100.0,
        entry_price=100.0,
        stop_loss=95.0,
    )
    legacy = BuylistActionsMixin()
    legacy._buylist_selected_item = lambda environment: item
    legacy._save_state = lambda: None
    legacy.populate_buylist_dashboard = lambda: None
    legacy.append_log = lambda message: None
    legacy._buylist_move_to_breakeven_selected("PROD")

    engine = _engine(tmp_path, "stop-change.db")
    kanban_card = _persist_card(engine)
    kanban_result = workflow.request_board_action(
        engine,
        _command(SetBreakevenStop, kanban_card),
        context=BoardActionContext(),
    ).card

    assert captured["legacy"] == captured["kanban"]
    assert kanban_result.acknowledge_pending_stop_change()
    assert (
        captured["legacy"].stop_type,
        item.stop_loss,
        captured["legacy"].quantity,
    ) == (
        kanban_result.stop_type,
        kanban_result.active_stop_price,
        kanban_result.stop_quantity,
    )


def test_l3_premarket_sell_all_produces_the_same_next_open_intent(
    tmp_path, monkeypatch
):
    legacy_commands = []

    class _Signal:
        def connect(self, callback):
            pass

    class _LegacyWorker:
        def __init__(self, *args, **kwargs):
            legacy_commands.append(kwargs["exit_command"])
            self.finished_order = _Signal()
            self.error_occurred = _Signal()

        def start(self):
            pass

    legacy = BuylistOrdersMixin()
    legacy._first_account_no_for_environment = lambda environment: "1"
    legacy._has_open_sell_order = lambda *args: False
    legacy._manual_sell_execution_policy = lambda environment: RESERVED_MOO_EXECUTION
    legacy._current_execution_lease_kwargs = lambda: {}
    legacy.append_log = lambda message: None
    monkeypatch.setattr(legacy_orders_module, "KisOrderWorker", _LegacyWorker)
    legacy._submit_kis_sell_order(
        SimpleNamespace(
            symbol="AAPL",
            environment="PROD",
            kis_account_no="1",
            _stop_order_pending=False,
            _exit_order_pending=False,
        ),
        100,
        "manual sell all",
    )

    engine = _engine(tmp_path, "premarket-sell-all.db")
    card = _persist_card(engine)
    kanban = workflow.request_board_action(
        engine,
        _command(RequestSellAll, card),
        context=BoardActionContext(regular_session_open=False),
    ).card
    kanban_commands = []

    def capture_kanban(*, command, **kwargs):
        kanban_commands.append(command)
        order = BrokerOrder.create(
            environment=command.environment,
            account_no=command.account_no,
            symbol=command.symbol,
            side=command.side,
            intent=command.intent,
            quantity_requested=command.quantity,
            limit_price=command.limit_price,
            execution_policy=command.execution_policy,
            status=OrderStatus.ACCEPTED,
        )
        return ExecutionSubmissionResult.from_broker_order(order)

    class _Broker:
        def get_positions(self, **kwargs):
            return {
                "overseas": {
                    "holdings": [
                        {
                            "symbol": "AAPL",
                            "quantity": 100,
                            "orderable_quantity": 100,
                        }
                    ]
                }
            }

        def discover_orders(self, **kwargs):
            return None

    monkeypatch.setattr(runtime_module, "request_exit_submit", capture_kanban)
    monkeypatch.setattr(runtime_module, "_find_open_order", lambda **kwargs: None)
    import src.services.trading_engine as trading_engine_module

    monkeypatch.setattr(
        trading_engine_module, "is_buyboard_engine_enabled", lambda: True
    )
    market_data = RestPollingMarketDataService(
        quote_fetcher=lambda symbol: QuoteSnapshot(symbol=symbol, last_price=100.0)
    )
    runtime = runtime_module.build_buyboard_runtime(
        buying_power_provider=lambda *_: 100_000.0,
        card_lookup=lambda *_: kanban,
        broker=_Broker(),
        market_data=market_data,
    )
    runtime.trading_engine._market_is_open_fn = lambda: False
    runtime.trading_engine.run_heartbeat([kanban])

    assert len(legacy_commands) == len(kanban_commands) == 1
    assert legacy_commands[0] == kanban_commands[0]
    assert kanban_commands[0].execution_policy == RESERVED_MOO_EXECUTION
    assert kanban_commands[0].limit_price == 0.0


def test_l3_eod_unfilled_entry_produces_the_same_authoritative_transition(
    tmp_path, monkeypatch
):
    import src.core.entry_monitoring_command as entry_command_module

    real_builder = entry_command_module.build_entry_monitoring_command
    captured = {}

    def legacy_builder(**kwargs):
        captured["legacy"] = real_builder(**kwargs)
        return captured["legacy"]

    def kanban_builder(**kwargs):
        captured["kanban"] = real_builder(**kwargs)
        return captured["kanban"]

    monkeypatch.setattr(
        legacy_monitoring_module, "build_entry_monitoring_command", legacy_builder
    )
    monkeypatch.setattr(eod_module, "build_entry_monitoring_command", kanban_builder)
    legacy_item = SimpleNamespace(
        symbol="AAPL",
        environment="PROD",
        kis_account_no="1",
        monitoring_status="WAITING_BREAKOUT",
        status="WAITING_BREAKOUT",
        orb_monitor_enabled=True,
        _buy_order_pending=False,
        _auto_order_block_notice_logged=False,
        _orb_queue_required_notice_logged=False,
    )
    legacy = BuylistMonitoringMixin()
    legacy.buylist_manager = SimpleNamespace(items=[legacy_item])
    legacy._is_execution_queue_buylist_item = lambda item: True
    legacy._clear_buylist_auto_order_block = lambda item: None
    legacy._save_buylist_state = lambda: None
    legacy._save_execution_queue_state = lambda: None
    legacy.populate_buylist_dashboard = lambda: None
    legacy.append_log = lambda message: None
    legacy._deactivate_pre_entry_orb_monitoring()

    def service(path):
        return EodTradingService(
            entry_attempt_manager=EntryAttemptManager(
                buying_power_provider=lambda environment, account_no: 100_000.0,
                reservations_path=path,
            ),
            position_manager=PositionManager(),
            callbacks=EodActionCallbacks(
                find_open_entry_order=lambda card: None,
                reconcile_order=lambda order: order,
                cancel_order=lambda intent: None,
            ),
            reservations_path=path,
        )

    kanban_card = _card(
        board_status=BoardStatus.BUY_TODAY,
        position_runtime_status=PositionRuntimeStatus.NONE,
        broker_quantity=0,
        orderable_quantity=0,
        entry_runtime_status=EntryRuntimeStatus.ARMED,
    )
    service(tmp_path / "kanban-reservations.json").run_eod_cleanup([kanban_card])

    assert captured["legacy"] == captured["kanban"]
    assert legacy_item.orb_monitor_enabled is False
    assert legacy_item.monitoring_status == "WATCHING"
    assert kanban_card.board_status == BoardStatus.BUYLIST


def test_l3_partial_fill_produces_the_same_reconciled_position_and_order_tracking(
    monkeypatch,
):
    observed_at = datetime(2026, 8, 17, 14, 0, tzinfo=timezone.utc)
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
        holdings=(
            AccountHoldingSnapshot(
                symbol="AAPL", quantity=4, average_price=100.0, sellable_quantity=4
            ),
        ),
        orders=(
            BrokerOrderStatusSnapshot(
                environment="PROD",
                account_no="1",
                symbol="AAPL",
                broker_order_id="PARTIAL-BROKER-ID",
                side=OrderSide.BUY,
                status=OrderStatus.PARTIALLY_FILLED,
                quantity_requested=10,
                filled_quantity=4,
                remaining_quantity=6,
                avg_fill_price=100.0,
            ),
        ),
        account_buying_power=100_000.0,
        account_equity=100_000.0,
        observed_at=observed_at,
        session_date=observed_at.date(),
        snapshot_id="PARTIAL-SNAPSHOT",
    )
    card = _card(
        board_status=BoardStatus.ENTRY_PENDING,
        position_runtime_status=PositionRuntimeStatus.NONE,
        broker_quantity=0,
        orderable_quantity=0,
        target_position_quantity=10,
        entry_attempt_group_id="ENTRY-GROUP",
        entry_client_order_id="PARTIAL-CID",
    )
    order = ExecutionOrderRecord(
        environment="PROD",
        account_no="1",
        symbol="AAPL",
        side=OrderSide.BUY,
        intent=OrderIntent.ENTRY,
        client_order_id="PARTIAL-CID",
        broker_order_id="PARTIAL-BROKER-ID",
        attempt_group_id="ENTRY-GROUP",
        submitted_quantity=10,
        status=ExecutionOrderStatus.WORKING,
        broker_identity_status=BrokerIdentityStatus.EXACT,
        remaining_quantity=10,
    )

    legacy_item = SimpleNamespace(
        symbol="AAPL",
        environment="PROD",
        kis_account_no="",
        shares_held=0,
        avg_cost=0.0,
        buy_date=None,
        position_percent=0.0,
        monitoring_status="ORDER_SUBMITTED",
    )
    legacy_order = BrokerOrder.create(
        environment="PROD",
        account_no="1",
        symbol="AAPL",
        side=OrderSide.BUY,
        intent=OrderIntent.ENTRY,
        quantity_requested=10,
        limit_price=100.0,
        status=OrderStatus.PARTIALLY_FILLED,
    )
    legacy_order.client_order_id = "PARTIAL-CID"
    legacy_order.broker_order_id = "PARTIAL-BROKER-ID"
    legacy_order.filled_quantity = 4
    legacy_order.remaining_quantity = 6
    legacy_order.avg_fill_price = 100.0
    legacy = BuylistOrdersMixin()
    legacy.buylist_manager = SimpleNamespace(
        get=lambda symbol, environment=None: legacy_item
    )
    legacy._is_execution_queue_buylist_item = lambda item: False
    legacy.append_log = lambda message: None
    legacy._save_buylist_state = lambda: None
    legacy.populate_buylist_dashboard = lambda: None
    monkeypatch.setattr(legacy_orders_module, "update_order", lambda order: order)
    monkeypatch.setattr(legacy_orders_module, "load_order_ledger", lambda: [])
    monkeypatch.setattr(
        legacy_orders_module, "record_event", lambda *args, **kwargs: None
    )
    legacy.apply_confirmed_order_fills_to_buylist([legacy_order])

    kanban_plan = reduce_account_reconciliation(
        snapshot,
        AccountLocalState(cards=(deepcopy(card),), execution_orders=(deepcopy(order),)),
    )
    kanban = kanban_plan.card_updates[0]
    assert kanban_plan.order_updates[0].status == ExecutionOrderStatus.PARTIALLY_FILLED
    assert legacy_item.shares_held == kanban.broker_quantity == 4
    assert legacy_order.remaining_quantity == kanban.entry_remaining_target_quantity == 6
    assert legacy_item.monitoring_status == "BUY_PARTIAL"
    assert kanban.board_status == BoardStatus.OPEN_POSITION
    assert kanban.entry_client_order_id == "PARTIAL-CID"
