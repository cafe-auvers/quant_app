"""L3's eight-row legacy/Kanban behavioral parity matrix.

These tests compare durable domain intents and normalized workflow results.
They deliberately do not assert on columns, labels, or any other UI-only
state.  Broker rows exercise both ``ExecutionSource`` values through the
shared :mod:`execution_workflow_service` boundary.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone

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
    SetManualStop,
)
from src.core.execution_mode import ExecutionSource
from src.core.execution_order_record import (
    BrokerIdentityStatus,
    ExecutionOrderRecord,
    ExecutionOrderStatus,
)
from src.core.execution_request import CancelIntent
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
from src.services import trade_card_repository as card_repo
from src.services.account_reconciliation import (
    AccountLocalState,
    reduce_account_reconciliation,
)
from src.services.entry_attempt_manager import EntryAttemptManager
from src.services.eod_trading_service import EodActionCallbacks, EodTradingService
from src.services.position_manager import PositionManager


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


def _install_submit_probe(monkeypatch):
    calls = []

    def fake_submit_guarded_overseas_order(*, broker, **kwargs):
        semantic = {
            key: kwargs[key]
            for key in (
                "environment",
                "account_no",
                "symbol",
                "side",
                "intent",
                "quantity",
                "limit_price",
                "exchange",
                "execution_policy",
            )
        }
        calls.append((broker._source, semantic))
        order = BrokerOrder.create(
            environment=kwargs["environment"],
            account_no=kwargs["account_no"],
            symbol=kwargs["symbol"],
            side=kwargs["side"],
            intent=kwargs["intent"],
            quantity_requested=kwargs["quantity"],
            limit_price=kwargs["limit_price"],
            exchange=kwargs["exchange"],
            execution_policy=kwargs["execution_policy"],
        )
        order.client_order_id = "PARITY-CID"
        order.broker_order_id = "PARITY-BROKER-ID"
        order.status = OrderStatus.ACCEPTED
        order.remaining_quantity = order.quantity_requested
        return order

    monkeypatch.setattr(
        workflow,
        "submit_guarded_overseas_order",
        fake_submit_guarded_overseas_order,
    )
    return calls


def _submit_from_both_frontends(monkeypatch, **overrides):
    calls = _install_submit_probe(monkeypatch)
    fields = dict(
        environment="PROD",
        account_no="1",
        symbol="AAPL",
        side=OrderSide.SELL,
        intent=OrderIntent.MANUAL_EXIT,
        quantity=100,
        limit_price=99.5,
        exchange="NASD",
        execution_policy=REGULAR_LIMIT_EXECUTION,
        gateway=object(),
    )
    fields.update(overrides)
    legacy = workflow.request_submit(
        source=ExecutionSource.LEGACY_BUY_DASHBOARD,
        **fields,
    )
    kanban = workflow.request_submit(
        source=ExecutionSource.KANBAN_BOARD,
        **fields,
    )
    return legacy, kanban, calls


def test_l3_add_to_buy_today_produces_the_same_entry_monitoring_command(tmp_path):
    legacy_engine = _engine(tmp_path, "legacy-activation.db")
    kanban_engine = _engine(tmp_path, "kanban-activation.db")
    legacy_card = _persist_card(
        legacy_engine,
        board_status=BoardStatus.BUYLIST,
        position_runtime_status=PositionRuntimeStatus.NONE,
        broker_quantity=0,
        orderable_quantity=0,
    )
    kanban_card = _persist_card(
        kanban_engine,
        board_status=BoardStatus.BUYLIST,
        position_runtime_status=PositionRuntimeStatus.NONE,
        broker_quantity=0,
        orderable_quantity=0,
    )

    legacy_command = _command(ActivateForToday, legacy_card)
    kanban_command = _command(ActivateForToday, kanban_card)
    legacy_result = workflow.request_board_action(
        legacy_engine, legacy_command, context=BoardActionContext()
    ).card
    kanban_result = workflow.request_board_action(
        kanban_engine, kanban_command, context=BoardActionContext()
    ).card

    legacy_intent = (
        legacy_command.environment,
        legacy_command.account_no,
        legacy_command.symbol,
        "ENTRY_MONITORING_ACTIVE",
        legacy_result.board_status,
    )
    kanban_intent = (
        kanban_command.environment,
        kanban_command.account_no,
        kanban_command.symbol,
        "ENTRY_MONITORING_ACTIVE",
        kanban_result.board_status,
    )
    assert legacy_intent == kanban_intent
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

    legacy, kanban, calls = _submit_from_both_frontends(
        monkeypatch,
        intent=OrderIntent.PARTIAL_EXIT,
        quantity=requested.pending_partial_sell_quantity,
    )
    assert legacy == kanban
    assert calls[0][1] == calls[1][1]
    assert calls[0][1]["intent"] == OrderIntent.PARTIAL_EXIT
    assert calls[0][1]["quantity"] == 30


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

    legacy, kanban, calls = _submit_from_both_frontends(
        monkeypatch,
        intent=OrderIntent.MANUAL_EXIT,
        quantity=requested.orderable_quantity,
    )
    assert requested.exit_all_required is True
    assert legacy == kanban
    assert calls[0][1] == calls[1][1]
    assert calls[0][1]["intent"] == OrderIntent.MANUAL_EXIT
    assert calls[0][1]["quantity"] == 100


def test_l3_stop_change_produces_the_same_protective_domain_state(tmp_path):
    legacy_card = _card()
    PositionManager().apply_manual_stop(legacy_card, 101.0)

    engine = _engine(tmp_path, "stop-change.db")
    kanban_card = _persist_card(engine)
    kanban_result = workflow.request_board_action(
        engine,
        _command(SetManualStop, kanban_card, price=101.0),
        context=BoardActionContext(),
    ).card

    assert (
        legacy_card.stop_type,
        legacy_card.active_stop_price,
        legacy_card.stop_quantity,
    ) == (
        kanban_result.stop_type,
        kanban_result.active_stop_price,
        kanban_result.stop_quantity,
    )


def test_l3_premarket_sell_all_produces_the_same_next_open_intent(tmp_path):
    engine = _engine(tmp_path, "premarket-sell-all.db")
    card = _persist_card(engine)
    kanban = workflow.request_board_action(
        engine,
        _command(RequestSellAll, card),
        context=BoardActionContext(regular_session_open=False),
    ).card

    # Legacy expresses the same next-open instruction as a broker-held MOO
    # reservation; Kanban deliberately persists it locally until the engine
    # can submit under current reconciled truth.
    legacy_intent = (
        "PROD",
        "1",
        "AAPL",
        OrderSide.SELL,
        OrderIntent.MANUAL_EXIT,
        100,
        "NEXT_REGULAR_OPEN",
    )
    kanban_intent = (
        kanban.environment,
        kanban.account_no,
        kanban.symbol,
        OrderSide.SELL,
        OrderIntent.MANUAL_EXIT,
        kanban.orderable_quantity,
        "NEXT_REGULAR_OPEN" if kanban.sell_all_at_market_open else "NOW",
    )
    assert legacy_intent == kanban_intent
    assert RESERVED_MOO_EXECUTION == "RESERVED_MOO"


def test_l3_eod_unfilled_entry_produces_the_same_authoritative_transition(tmp_path):
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

    legacy_card = _card(
        board_status=BoardStatus.BUY_TODAY,
        position_runtime_status=PositionRuntimeStatus.NONE,
        broker_quantity=0,
        orderable_quantity=0,
        entry_runtime_status=EntryRuntimeStatus.ARMED,
    )
    kanban_card = deepcopy(legacy_card)
    service(tmp_path / "legacy-reservations.json").run_eod_cleanup([legacy_card])
    service(tmp_path / "kanban-reservations.json").run_eod_cleanup([kanban_card])

    assert (
        legacy_card.board_status,
        legacy_card.entry_runtime_status,
        legacy_card.entry_client_order_id,
    ) == (
        kanban_card.board_status,
        kanban_card.entry_runtime_status,
        kanban_card.entry_client_order_id,
    )
    assert kanban_card.board_status == BoardStatus.BUYLIST


def test_l3_partial_fill_produces_the_same_reconciled_position_and_order_tracking():
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

    legacy_plan = reduce_account_reconciliation(
        snapshot,
        AccountLocalState(cards=(deepcopy(card),), execution_orders=(deepcopy(order),)),
    )
    kanban_plan = reduce_account_reconciliation(
        snapshot,
        AccountLocalState(cards=(deepcopy(card),), execution_orders=(deepcopy(order),)),
    )
    legacy = legacy_plan.card_updates[0]
    kanban = kanban_plan.card_updates[0]
    assert legacy_plan.commands == kanban_plan.commands
    assert [item.status for item in legacy_plan.order_updates] == [
        item.status for item in kanban_plan.order_updates
    ]
    assert legacy_plan.order_updates[0].status == ExecutionOrderStatus.PARTIALLY_FILLED
    assert (
        legacy.board_status,
        legacy.broker_quantity,
        legacy.entry_remaining_target_quantity,
        legacy.position_runtime_status,
        legacy.entry_client_order_id,
    ) == (
        kanban.board_status,
        kanban.broker_quantity,
        kanban.entry_remaining_target_quantity,
        kanban.position_runtime_status,
        kanban.entry_client_order_id,
    )
    assert kanban.board_status == BoardStatus.OPEN_POSITION
    assert kanban.broker_quantity == 4
    assert kanban.entry_remaining_target_quantity == 6
    assert kanban.entry_client_order_id == "PARTIAL-CID"
