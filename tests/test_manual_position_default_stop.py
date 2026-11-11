"""Automatic manual-holding protection, with real persistence/ownership fences."""
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine

from src.core import execution_config
from src.core.board_workflow import BoardActionContext, SetManualStop
from src.core.execution_ownership import ExecutionOwner, ExecutionOwnership
from src.core.discovered_external_order import new_discovered_external_order
from src.core.execution_order_record import ExecutionOrderStatus
from src.core.order_state import OrderSide
from src.core.runtime_readiness import RuntimeDeviceState
from src.core.trade_card_state import BoardStatus, StopType, TradeCardState
from src.services import trade_card_repository as repo
from src.services.execution_ownership_repository import assign_ownership, get_ownership
from src.services.execution_workflow_service import request_board_action
from src.services.discovered_external_order_repository import record_discovered_external_order
from src.services.position_manager import (
    AUTO_MANUAL_STOP_PENDING, AUTO_MANUAL_STOP_NAV_REQUIRED, BrokerHolding, PositionManager,
    initialize_manual_position_stop, manual_position_nav_stop_price, minimum_manual_stop_price,
)
from src.services.stop_change_coordinator import stop_change_coordinator_for
from src.ui.buyboard.runtime_worker import BuyboardRuntimeWorker


@pytest.fixture(autouse=True)
def isolated_defaults(monkeypatch, tmp_path):
    monkeypatch.setattr(repo, "LOCAL_TRADE_CARDS_FILE", tmp_path / "cards.json")
    monkeypatch.setattr(execution_config, "is_buyboard_engine_enabled", lambda: True)


def discovered(average=100.0, account_nav=35000):
    return PositionManager().discover_manual_position(
        None, environment="PROD", account_no="1", symbol="SMX", name="SMX",
        broker_quantity=350, average_entry_price=average, account_nav=account_nav,
    )


def worker_for(engine, **overrides):
    worker = SimpleNamespace(
        _accepting_commands=True, _lease_current=True, _standby_only=False,
        device_state=RuntimeDeviceState.ACTIVE, _db_engine=engine,
        readiness_generation=5, reconciliation_accounts_in_progress=set(),
        _account_reconciliation_is_fresh=lambda account: True,
        _card_action_ready=lambda card: True,
    )
    worker.__dict__.update(overrides)
    return worker


def enroll(worker, cards):
    return BuyboardRuntimeWorker._ensure_manual_position_stop_protection(worker, cards)


@pytest.mark.parametrize("average,quantity,nav,expected", [
    (10, 100, 11000, 8.9), (100, 100, 10000, 99),
    (3.5, 350, 1000, 3.48), (0.1234, 350, 100, 0.1206),
    (1.01, 350, 1000, 0.9815), (3.5, 10, 10000, 0.0001),
])
def test_default_price_rounds_up_without_exceeding_total_nav_budget(average, quantity, nav, expected):
    price = manual_position_nav_stop_price(average, quantity, nav)
    assert price == expected
    assert (average - price) * quantity <= nav * 0.01 + 1e-10


@pytest.mark.parametrize("nav", [None, 0, -1, float("nan"), float("inf")])
def test_missing_or_invalid_nav_never_falls_back_to_price_percentage(nav):
    card = discovered(account_nav=nav)
    assert card.active_stop_price is None
    assert "STOP_REQUIRED" in card.warnings
    assert AUTO_MANUAL_STOP_NAV_REQUIRED in card.warnings
    assert card.entry_block_reason == "manual_position_requires_fresh_nav"
    assert initialize_manual_position_stop(card, account_nav=35000)
    assert card.active_stop_price == 99
    assert AUTO_MANUAL_STOP_NAV_REQUIRED not in card.warnings


def test_user_can_supply_initial_stop_while_nav_is_unavailable(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'state.db'}")
    card = repo.create_trade_card(engine, discovered(account_nav=None))
    request_board_action(engine, SetManualStop(
        environment="PROD", account_no="1", symbol="SMX",
        expected_card_version=card.version, price=95,
    ), context=BoardActionContext(), claim_kanban_ownership=True)
    saved = repo.get_trade_card(engine, "PROD", "1", "SMX")
    assert saved.pending_stop_price == 95
    assert AUTO_MANUAL_STOP_NAV_REQUIRED not in saved.warnings
    assert "STOP_REQUIRED" not in saved.warnings
    assert saved.entry_block_reason == ""
    assert saved.acknowledge_pending_stop_change()
    assert not initialize_manual_position_stop(saved, account_nav=35000)
    assert saved.active_stop_price == 95


def test_fresh_nav_clears_nav_warning_even_when_below_cost_tick_is_unrepresentable():
    card = discovered(account_nav=None)
    card.average_entry_price = 0.0001
    assert not initialize_manual_position_stop(card, account_nav=10000)
    assert card.active_stop_price is None
    assert "STOP_REQUIRED" in card.warnings
    assert AUTO_MANUAL_STOP_NAV_REQUIRED not in card.warnings


@pytest.mark.parametrize("average", [0, -1, float("nan"), float("inf"), 0.0001])
def test_invalid_or_unrepresentable_cost_stays_visibly_unprotected(average):
    card = discovered(average)
    assert card.active_stop_price is None
    assert "STOP_REQUIRED" in card.warnings
    assert AUTO_MANUAL_STOP_PENDING not in card.warnings


def test_existing_and_pending_stops_are_never_overwritten():
    for kind in StopType:
        card = discovered()
        card.stop_type, card.active_stop_price = kind, 96
        assert not initialize_manual_position_stop(card)
        assert card.active_stop_price == 96
    card = discovered(0)
    card.pending_stop_type = StopType.MANUAL_PRICE
    card.pending_stop_price = 94
    card.pending_stop_command_id = "user-request"
    card.average_entry_price = 100
    assert not initialize_manual_position_stop(card)
    assert card.pending_stop_price == 94
    assert card.active_stop_price is None


def test_default_survives_restart_cost_changes_and_topups(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'state.db'}")
    card = repo.create_trade_card(engine, discovered())
    engine.dispose()
    engine = create_engine(f"sqlite:///{tmp_path / 'state.db'}")
    restored = repo.get_trade_card(engine, "PROD", "1", "SMX")
    changed = PositionManager().reconcile_broker_positions(
        [restored], [BrokerHolding(symbol="SMX", quantity=400, average_price=105)],
        environment="PROD", account_no="1",
    )
    assert changed == [restored]
    assert restored.active_stop_price == 99
    assert restored.average_entry_price == 105
    assert restored.stop_quantity == 400
    assert AUTO_MANUAL_STOP_PENDING in restored.warnings


def test_preexisting_stop_required_card_is_initialized_even_with_unchanged_facts():
    card = discovered(0)
    card.average_entry_price = 100
    changed = PositionManager().reconcile_broker_positions(
        [card], [BrokerHolding(symbol="SMX", quantity=350, average_price=100)],
        environment="PROD", account_no="1",
        account_nav=35000,
    )
    assert changed == [card]
    assert card.active_stop_price == 99


def test_flat_then_repurchase_gets_a_fresh_default():
    manager = PositionManager()
    card = discovered()
    card.broker_quantity = 0
    manager.confirm_flat(card)
    manager.discover_manual_position(
        card, environment="PROD", account_no="1", symbol="SMX", name="SMX",
        broker_quantity=100, average_entry_price=50, account_nav=10000,
    )
    assert card.active_stop_price == 49


def test_enrollment_claims_ownership_and_publishes_stop_once(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'state.db'}")
    card = repo.create_trade_card(engine, discovered())
    worker = worker_for(engine)
    assert enroll(worker, [card])
    saved = repo.get_trade_card(engine, "PROD", "1", "SMX")
    ownership = get_ownership(engine, environment="PROD", account_no="1", symbol="SMX")
    assert ownership.owner == ExecutionOwner.KANBAN
    assert ownership.strategy_instance_id == execution_config.KANBAN_STRATEGY_INSTANCE_ID
    assert saved.pending_stop_price == 99
    assert saved.pending_stop_quantity == 350
    assert saved.pending_stop_command_id
    assert AUTO_MANUAL_STOP_PENDING not in saved.warnings
    assert saved.entry_block_reason == ""
    pending = stop_change_coordinator_for(engine).pending_for(saved.card_key)
    assert pending.price == 99
    assert pending.quantity == 350
    assert not enroll(worker, [saved])


@pytest.mark.parametrize("overrides", [
    {"_accepting_commands": False}, {"_lease_current": False},
    {"_standby_only": True}, {"device_state": RuntimeDeviceState.STANDBY},
    {"_account_reconciliation_is_fresh": lambda account: False},
    {"_card_action_ready": lambda card: False},
    {"reconciliation_accounts_in_progress": {"1"}},
])
def test_runtime_fences_leave_default_pending_without_claiming(tmp_path, overrides):
    engine = create_engine(f"sqlite:///{tmp_path / 'state.db'}")
    card = repo.create_trade_card(engine, discovered())
    assert not enroll(worker_for(engine, **overrides), [card])
    saved = repo.get_trade_card(engine, "PROD", "1", "SMX")
    assert AUTO_MANUAL_STOP_PENDING in saved.warnings
    assert not saved.pending_stop_command_id
    assert get_ownership(engine, environment="PROD", account_no="1", symbol="SMX").owner == ExecutionOwner.LEGACY


def test_explicit_manual_ownership_is_not_stolen(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'state.db'}")
    card = repo.create_trade_card(engine, discovered())
    assign_ownership(engine, ExecutionOwnership(
        environment="PROD", account_no="1", symbol="SMX",
        owner=ExecutionOwner.MANUAL, assigned_by="user",
    ))
    assert not enroll(worker_for(engine), [card])
    assert get_ownership(engine, environment="PROD", account_no="1", symbol="SMX").owner == ExecutionOwner.MANUAL
    assert AUTO_MANUAL_STOP_PENDING in repo.get_trade_card(engine, "PROD", "1", "SMX").warnings


def test_unresolved_external_order_defers_enrollment_until_broker_reconciles(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'state.db'}")
    card = repo.create_trade_card(engine, discovered())
    record_discovered_external_order(engine, new_discovered_external_order(
        environment="PROD", account_no="1", symbol="SMX",
        side=OrderSide.BUY, broker_order_id="manual-working-buy",
        quantity_requested=350, broker_status=ExecutionOrderStatus.WORKING,
    ))
    assert not enroll(worker_for(engine), [card])
    saved = repo.get_trade_card(engine, "PROD", "1", "SMX")
    assert AUTO_MANUAL_STOP_PENDING in saved.warnings
    assert not saved.pending_stop_command_id
    assert get_ownership(engine, environment="PROD", account_no="1", symbol="SMX").owner == ExecutionOwner.LEGACY


@pytest.mark.parametrize("price", [92, 95])
def test_user_edit_wins_over_stale_default_enrollment_and_later_sync(tmp_path, price):
    engine = create_engine(f"sqlite:///{tmp_path / 'state.db'}")
    card = repo.create_trade_card(engine, discovered())
    request_board_action(engine, SetManualStop(
        environment="PROD", account_no="1", symbol="SMX",
        expected_card_version=card.version, price=price,
    ), context=BoardActionContext(), claim_kanban_ownership=True)
    assert not enroll(worker_for(engine), [card])
    saved = repo.get_trade_card(engine, "PROD", "1", "SMX")
    assert saved.pending_stop_price == price
    assert saved.acknowledge_pending_stop_change()
    PositionManager().discover_manual_position(
        saved, environment="PROD", account_no="1", symbol="SMX", name="SMX",
        broker_quantity=350, average_entry_price=110,
    )
    assert saved.active_stop_price == price
    assert not initialize_manual_position_stop(saved)
    assert AUTO_MANUAL_STOP_PENDING not in saved.warnings


def test_profit_protection_retains_existing_tightening_only_floor():
    card = discovered()
    assert minimum_manual_stop_price(card) == 0.0001
    card.stop_type, card.active_stop_price = StopType.BREAKEVEN, 100.15
    assert minimum_manual_stop_price(card) >= 100.15


def test_default_enrollment_preserves_queued_partial_sell_and_its_price(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'state.db'}")
    card = discovered(0)
    card.average_entry_price = 100
    card.board_status = BoardStatus.PARTIAL_SELL
    card.pending_partial_sell_quantity = 50
    card.pending_partial_sell_price = 110
    assert initialize_manual_position_stop(card, account_nav=35000)
    card = repo.create_trade_card(engine, card)
    assert enroll(worker_for(engine), [card])
    saved = repo.get_trade_card(engine, "PROD", "1", "SMX")
    assert saved.board_status == BoardStatus.PARTIAL_SELL
    assert saved.pending_partial_sell_quantity == 50
    assert saved.pending_partial_sell_price == 110
    assert saved.pending_stop_price == 99


@pytest.mark.parametrize("field,value", [
    ("entry_client_order_id", "app-entry"), ("entry_submission_unresolved", True),
    ("exit_all_required", True), ("stop_loss_triggered", True),
])
def test_default_does_not_replace_an_application_entry_or_liquidation(field, value):
    card = discovered(0)
    card.average_entry_price = 100
    setattr(card, field, value)
    assert not initialize_manual_position_stop(card)
    assert card.active_stop_price is None
