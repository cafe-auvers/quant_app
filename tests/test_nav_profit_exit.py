"""NAV profit exits: durable intent, exact fills, operator priority and fences."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine

from src.core import execution_config
from src.core.account_broker_snapshot import AccountBrokerSnapshot, AccountHoldingSnapshot, SnapshotCompleteness
from src.core.board_workflow import BoardActionContext, CancelPartialSell, RequestPartialSell, SetManualStop
from src.core.execution_order_record import BrokerIdentityStatus, ExecutionOrderRecord, ExecutionOrderStatus
from src.core.execution_ownership import ExecutionOwner, ExecutionOwnership
from src.core.discovered_external_order import new_discovered_external_order
from src.core.nav_profit_exit import (
    NavProfitExitState, initialize_nav_profit_exit, nav_profit_sell_quantity,
)
from src.core.order_state import BrokerOrderStatusSnapshot, OrderIntent, OrderSide, OrderStatus
from src.core.runtime_readiness import RuntimeDeviceState
from src.core.trade_card_state import BoardStatus, PositionRuntimeStatus, StopType, TradeCardState
from src.services import trade_card_repository as repo
from src.services.account_reconciliation import AccountLocalState, reduce_account_reconciliation
from src.services.execution_ownership_repository import assign_ownership, get_ownership
from src.services.discovered_external_order_repository import record_discovered_external_order
from src.services.execution_workflow_service import BoardCommandRejectedError, request_board_action
from src.services.position_manager import PositionManager, initialize_manual_position_stop
from src.services.realtime_market_data import QuoteSnapshot
from src.ui.buyboard.runtime_worker import BuyboardRuntimeWorker


@pytest.fixture(autouse=True)
def isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(repo, "LOCAL_TRADE_CARDS_FILE", tmp_path / "cards.json")
    monkeypatch.setattr(execution_config, "is_buyboard_engine_enabled", lambda: True)
    monkeypatch.setattr("src.services.trading_state.is_trading_locked_disabled", lambda: False)
    monkeypatch.setattr("src.services.trading_state._trading_enabled", True)
    monkeypatch.setattr("src.services.trading_state._authoritative_provider", None)
    monkeypatch.setattr("src.ui.buyboard.runtime_worker.is_regular_session_open", lambda: True)
    monkeypatch.setattr("src.ui.buyboard.runtime_worker.is_us_exit_session_open", lambda: True)


def card_for(*, quantity=100, stop_type=StopType.MANUAL_PRICE):
    card = TradeCardState(
        environment="PROD", account_no="1", symbol="AAPL",
        board_status=BoardStatus.OPEN_POSITION, broker_quantity=quantity,
        orderable_quantity=quantity, average_entry_price=10,
        position_runtime_status=PositionRuntimeStatus.OPEN,
        stop_type=stop_type, active_stop_price=9, stop_quantity=quantity,
    )
    assert initialize_nav_profit_exit(card, 11000)
    return card


def request_for(card, **overrides):
    values = dict(environment="PROD", account_no="1", symbol="AAPL",
                  expected_card_version=card.version, quantity=card.broker_quantity // 2,
                  nav_profit_trigger_price=12.2,
                  command_id="nav-profit-" + card.nav_profit_exit.cycle_id)
    values.update(overrides)
    return RequestPartialSell(**values)


@pytest.mark.parametrize("quantity,price,expected", [
    (100, 12.1999, 0), (100, 12.2, 50), (100, 13, 50),
    (101, 12.2, 50), (1, 230, 0), (1000, 10.22, 500),
])
def test_target_is_two_percent_of_nav_and_sells_half_once(quantity, price, expected):
    card = card_for(quantity=quantity)
    assert card.nav_profit_exit.target_profit_usd == 220
    assert nav_profit_sell_quantity(card, price) == expected
    frozen = card.nav_profit_exit.to_dict()
    assert not initialize_nav_profit_exit(card, 15000)
    assert card.nav_profit_exit.to_dict() == frozen


@pytest.mark.parametrize("nav", [None, 0, -1, float("nan"), float("inf")])
def test_unavailable_nav_waits_without_a_price_percentage_fallback(nav):
    card = card_for()
    card.nav_profit_exit = NavProfitExitState()
    assert not initialize_nav_profit_exit(card, nav)
    assert nav_profit_sell_quantity(card, 100) == 0
    assert initialize_nav_profit_exit(card, 11000)


@pytest.mark.parametrize("field,value", [
    ("orderable_quantity", 0), ("exit_all_required", True), ("stop_loss_triggered", True),
    ("pending_stop_command_id", "pending"), ("entry_remaining_target_quantity", 1),
    ("entry_submission_unresolved", True), ("entry_cancel_in_flight", True),
    ("exit_client_order_id", "working"), ("exit_submission_unresolved", True),
    ("board_status", BoardStatus.PARTIAL_SELL),
])
def test_conflicting_or_unconfirmed_state_does_not_start_profit_exit(field, value):
    card = card_for()
    setattr(card, field, value)
    assert nav_profit_sell_quantity(card, 12.2) == 0


@pytest.mark.parametrize("kind", [StopType.MANUAL_PRICE, StopType.ORB_LOW])
def test_guarded_request_claims_ownership_without_changing_manual_or_strategy_stop(tmp_path, kind):
    engine = create_engine(f"sqlite:///{tmp_path / 'state.db'}")
    card = repo.create_trade_card(engine, card_for(stop_type=kind))
    request_board_action(engine, request_for(card), context=BoardActionContext(), claim_kanban_ownership=True)
    saved = repo.get_trade_card(engine, "PROD", "1", "AAPL")
    assert saved.board_status == BoardStatus.PARTIAL_SELL
    assert saved.pending_partial_sell_quantity == 50
    assert saved.pending_partial_sell_price is None
    assert saved.nav_profit_exit.status == "SELLING"
    assert saved.nav_profit_exit.target_quantity == 50
    assert saved.active_stop_price == 9 and saved.stop_type == kind
    assert not initialize_manual_position_stop(saved, account_nav=11000)
    assert get_ownership(engine, environment="PROD", account_no="1", symbol="AAPL").owner == ExecutionOwner.KANBAN
    engine.dispose()
    engine = create_engine(f"sqlite:///{tmp_path / 'state.db'}")
    restored = repo.get_trade_card(engine, "PROD", "1", "AAPL")
    assert nav_profit_sell_quantity(restored, 20) == 0
    assert restored.nav_profit_exit.to_dict() == saved.nav_profit_exit.to_dict()


def test_manual_stop_edit_preserves_target_and_custom_partial_overrides_it(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'state.db'}")
    card = repo.create_trade_card(engine, card_for())
    request_board_action(engine, SetManualStop(environment="PROD", account_no="1", symbol="AAPL",
                        expected_card_version=card.version, price=8.5), context=BoardActionContext(), claim_kanban_ownership=True)
    card = repo.get_trade_card(engine, "PROD", "1", "AAPL")
    assert card.nav_profit_exit.status == "ARMED"
    assert card.acknowledge_pending_stop_change()
    card = repo.update_trade_card(engine, card, expected_version=card.version)
    request_board_action(engine, request_for(card, quantity=20, limit_price=13,
                        nav_profit_trigger_price=None), context=BoardActionContext())
    card = repo.get_trade_card(engine, "PROD", "1", "AAPL")
    assert card.nav_profit_exit.status == "OVERRIDDEN"
    assert card.pending_partial_sell_price == 13
    assert card.active_stop_price == 8.5


def test_operator_cancel_disables_automatic_retrigger_for_this_position(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'state.db'}")
    card = repo.create_trade_card(engine, card_for())
    request_board_action(engine, request_for(card), context=BoardActionContext(), claim_kanban_ownership=True)
    card = repo.get_trade_card(engine, "PROD", "1", "AAPL")
    request_board_action(engine, CancelPartialSell(environment="PROD", account_no="1", symbol="AAPL",
                        expected_card_version=card.version), context=BoardActionContext())
    saved = repo.get_trade_card(engine, "PROD", "1", "AAPL")
    assert saved.board_status == BoardStatus.OPEN_POSITION
    assert nav_profit_sell_quantity(saved, 30) == 0


def test_explicit_manual_execution_ownership_remains_fenced(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'state.db'}")
    card = repo.create_trade_card(engine, card_for())
    assign_ownership(engine, ExecutionOwnership(environment="PROD", account_no="1", symbol="AAPL",
                     owner=ExecutionOwner.MANUAL, assigned_by="user"))
    with pytest.raises(BoardCommandRejectedError):
        request_board_action(engine, request_for(card), context=BoardActionContext(), claim_kanban_ownership=True)
    assert repo.get_trade_card(engine, "PROD", "1", "AAPL").nav_profit_exit.status == "ARMED"


def test_working_unowned_manual_broker_exit_prevents_duplicate_automatic_sale(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'state.db'}")
    card = repo.create_trade_card(engine, card_for())
    record_discovered_external_order(engine, new_discovered_external_order(
        environment="PROD", account_no="1", symbol="AAPL", side=OrderSide.SELL,
        broker_order_id="manual-limit", quantity_requested=50,
        broker_status=ExecutionOrderStatus.WORKING))
    with pytest.raises(BoardCommandRejectedError):
        request_board_action(engine, request_for(card), context=BoardActionContext(), claim_kanban_ownership=True)
    saved = repo.get_trade_card(engine, "PROD", "1", "AAPL")
    assert saved.nav_profit_exit.status == "ARMED"
    assert saved.board_status == BoardStatus.OPEN_POSITION


def worker_for(engine, quote, **overrides):
    worker = SimpleNamespace(
        _accepting_commands=True, _lease_current=True, _standby_only=False,
        device_state=RuntimeDeviceState.ACTIVE, _db_engine=engine, readiness_generation=5,
        reconciliation_accounts_in_progress=set(), _account_reconciliation_is_fresh=lambda _: True,
        _card_action_ready=lambda _: True,
        runtime=SimpleNamespace(market_data=SimpleNamespace(latest_quote=lambda _: quote)),
    )
    worker.__dict__.update(overrides)
    return worker


def test_worker_latches_one_durable_request_at_fresh_profit_threshold(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'state.db'}")
    card = repo.create_trade_card(engine, card_for())
    worker = worker_for(engine, QuoteSnapshot(symbol="AAPL", last_price=12.2))
    assert BuyboardRuntimeWorker._ensure_nav_profit_taking(worker, [card])
    assert not BuyboardRuntimeWorker._ensure_nav_profit_taking(worker, [card])
    saved = repo.get_trade_card(engine, "PROD", "1", "AAPL")
    assert saved.pending_partial_sell_quantity == 50
    assert saved.active_stop_price == 9


@pytest.mark.parametrize("quote_kwargs", [
    {"last_price": 12.19}, {"regular_session": False, "broker_event_at": datetime(2026, 11, 8, 12, tzinfo=timezone.utc)}, {"channel": "BOOK"},
    {"entry_trigger_eligible": False},
    {"received_at": datetime.now(timezone.utc) - timedelta(minutes=5)},
    {"breached_stop_versions": (("PROD:1:AAPL", "v1"),)},
])
def test_worker_never_uses_stale_book_or_protective_replay_prices(tmp_path, quote_kwargs):
    engine = create_engine(f"sqlite:///{tmp_path / 'state.db'}")
    card = repo.create_trade_card(engine, card_for())
    values = dict(symbol="AAPL", last_price=12.2)
    values.update(quote_kwargs)
    assert not BuyboardRuntimeWorker._ensure_nav_profit_taking(worker_for(engine, QuoteSnapshot(**values)), [card])


def test_flat_resets_policy_for_a_new_purchase_but_corrupt_saved_state_never_rearms():
    card = card_for()
    old_cycle = card.nav_profit_exit.cycle_id
    with pytest.raises(ValueError):
        PositionManager().confirm_flat(card)
    assert card.nav_profit_exit.cycle_id == old_cycle
    card.broker_quantity = 0
    PositionManager().confirm_flat(card)
    card.broker_quantity = card.orderable_quantity = 100
    card.board_status = BoardStatus.OPEN_POSITION
    assert initialize_nav_profit_exit(card, 12000)
    assert card.nav_profit_exit.cycle_id != old_cycle
    assert card.nav_profit_exit.target_profit_usd == 240
    data = card.to_dict()
    data["nav_profit_exit"] = {"status": "SELLING", "target_quantity": 50}
    restored = TradeCardState.from_dict(data)
    assert restored.nav_profit_exit.status == "BLOCKED"
    assert not initialize_nav_profit_exit(restored, 11000)


def order_for(client="C1", broker="B1", quantity=50, attempt=1):
    return ExecutionOrderRecord(environment="PROD", account_no="1", symbol="AAPL",
        client_order_id=client, broker_order_id=broker, side=OrderSide.SELL, intent=OrderIntent.PARTIAL_EXIT,
        submitted_quantity=quantity, submitted_limit_price=12.2, remaining_quantity=quantity,
        status=ExecutionOrderStatus.WORKING, broker_identity_status=BrokerIdentityStatus.EXACT,
        attempt_group_id="EXIT-G", attempt_number=attempt)


def snapshot_for(remaining, orders):
    now = datetime.now(timezone.utc)
    return AccountBrokerSnapshot(environment="PROD", account_no="1",
        completeness=SnapshotCompleteness(True, True, True, True, True),
        holdings=(AccountHoldingSnapshot("AAPL", remaining, 10, remaining),),
        orders=tuple(orders), account_equity=11000, account_balance_observed_at=now, observed_at=now)


def broker_order(client="B1", status=OrderStatus.CANCELLED, filled=20, quantity=50):
    return BrokerOrderStatusSnapshot(environment="PROD", account_no="1", symbol="AAPL",
        broker_order_id=client, side=OrderSide.SELL, status=status,
        quantity_requested=quantity, filled_quantity=filled)


def test_partial_fills_retry_only_unfilled_half_and_breakeven_follows_completion_after_restart():
    card = card_for()
    card.board_status = BoardStatus.PARTIAL_SELL
    card.nav_profit_exit.status = "SELLING"
    card.nav_profit_exit.target_quantity = 50
    card.pending_partial_sell_quantity = 50
    card.exit_client_order_id = "C1"
    card.exit_attempt_group_id = "EXIT-G"
    first = reduce_account_reconciliation(snapshot_for(80, [broker_order()]),
                AccountLocalState(cards=(card,), execution_orders=(order_for(),)))
    card = first.card_updates[0]
    assert card.pending_partial_sell_quantity == 30
    assert card.nav_profit_exit.filled_quantity == 20
    assert card.active_stop_price == 9
    assert card.board_status == BoardStatus.PARTIAL_SELL
    retry_at = card.next_exit_retry_at
    replay = reduce_account_reconciliation(snapshot_for(80, [broker_order()]),
                AccountLocalState(cards=(card,), execution_orders=first.order_updates))
    assert not replay.card_updates
    assert card.next_exit_retry_at == retry_at
    card = TradeCardState.from_dict(card.to_dict())
    card.exit_client_order_id = "C2"
    second = reduce_account_reconciliation(
        snapshot_for(50, [broker_order(), broker_order("B2", OrderStatus.FILLED, 30, 30)]),
        AccountLocalState(cards=(card,), execution_orders=(first.order_updates[0], order_for("C2", "B2", 30, 2))))
    finished = second.card_updates[0]
    assert finished.nav_profit_exit.filled_quantity == 50
    assert finished.nav_profit_exit.status == "DONE"
    assert finished.broker_quantity == 50
    assert finished.pending_partial_sell_quantity == 0
    assert finished.board_status == BoardStatus.OPEN_POSITION
    assert finished.active_stop_price == 10.02
    assert nav_profit_sell_quantity(finished, 100) == 0


@pytest.mark.parametrize("sellable", [0, 10])
def test_automatic_fill_completion_uses_total_holdings_and_preserves_reserved_runner(sellable):
    from dataclasses import replace
    from src.services.trading_engine import TradingEngine

    card = card_for()
    card.board_status = BoardStatus.PARTIAL_SELL
    card.nav_profit_exit.status = "SELLING"
    card.nav_profit_exit.target_quantity = 50
    card.pending_partial_sell_quantity = 50
    card.exit_client_order_id = "C1"
    card.exit_attempt_group_id = "EXIT-G"
    terminal = SimpleNamespace(status=OrderStatus.FILLED, client_order_id="C1",
                               filled_quantity=50, remaining_quantity=0)
    def unavailable_balance(*args):
        raise AssertionError("Free shares cannot establish total holdings")
    runtime = SimpleNamespace(_clock=lambda: datetime.now(timezone.utc),
        _position_callbacks=SimpleNamespace(find_open_sell_order=lambda _: terminal,
            reconcile_sell_order=lambda _: terminal, refresh_orderable_quantity=unavailable_balance))
    assert TradingEngine._reconcile_partial_sell_fills(runtime, [card]) == [card]
    assert card.nav_profit_exit.filled_quantity == 50
    assert card.nav_profit_exit.status == "SELLING"
    assert card.broker_quantity == 100 and card.active_stop_price == 9
    assert TradingEngine._process_partial_sell_requests(runtime, [card]) == []
    snapshot = replace(snapshot_for(50, [broker_order(status=OrderStatus.FILLED, filled=50)]),
                       holdings=(AccountHoldingSnapshot("AAPL", 50, 10, sellable),))
    result = reduce_account_reconciliation(snapshot,
                 AccountLocalState(cards=(card,), execution_orders=(order_for(),)))
    final = result.card_updates[0]
    assert final.broker_quantity == final.stop_quantity == 50
    assert final.orderable_quantity == sellable
    assert final.board_status == BoardStatus.OPEN_POSITION
    assert final.nav_profit_exit.status == "DONE"
    assert final.active_stop_price == 10.02


@pytest.mark.parametrize("control", ["off", "shared_off", "unavailable"])
def test_profit_threshold_does_not_queue_an_exit_while_live_control_is_disabled_or_unavailable(tmp_path, monkeypatch, control):
    def unavailable():
        raise RuntimeError("Canonical control unavailable")
    if control == "off":
        monkeypatch.setattr("src.services.trading_state._trading_enabled", False)
    else:
        monkeypatch.setattr("src.services.trading_state._authoritative_provider",
                            unavailable if control == "unavailable" else lambda: False)
    engine = create_engine(f"sqlite:///{tmp_path / 'state.db'}")
    card = repo.create_trade_card(engine, card_for())
    worker = worker_for(engine, QuoteSnapshot(symbol="AAPL", last_price=12.2))
    assert not BuyboardRuntimeWorker._ensure_nav_profit_taking(worker, [card])
    saved = repo.get_trade_card(engine, "PROD", "1", "AAPL")
    assert saved.nav_profit_exit.status == "ARMED"
    assert saved.pending_partial_sell_quantity == 0
    assert saved.board_status == BoardStatus.OPEN_POSITION
