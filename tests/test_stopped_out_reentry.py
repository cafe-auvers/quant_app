"""Explicit new trade cycles use isolated state and never contact KIS."""
from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

from src.core.board_workflow import ActivateForToday, BoardActionContext, MoveToBuylist
from src.core.capital_reservation import CapitalReservation, CapitalReservationStatus
from src.core.execution_order_record import BrokerIdentityStatus, ExecutionOrderRecord, ExecutionOrderStatus
from src.core.order_state import OrderIntent, OrderSide
from src.core.trade_card_state import BoardStatus, EntryRuntimeStatus, PositionRuntimeStatus, TradeCardState
from src.services import trade_card_repository as cards
from src.services.execution_command_repository import ExecutionCommand, record_command
from src.services.execution_order_repository import record_execution_order, list_execution_orders_for_card
from src.services.execution_workflow_service import (
    BoardCommandRejectedError, _cancel_target_is_confirmed_terminal, request_board_action,
)
from src.services.position_manager import PositionManager
from src.services.capital_reservation_repository import save_reservation_strict


@pytest.fixture
def engine(tmp_path, monkeypatch):
    monkeypatch.setattr(cards, "LOCAL_TRADE_CARDS_FILE", tmp_path / "cards.json")
    return create_engine(f"sqlite:///{tmp_path / 'reentry.db'}", poolclass=NullPool)


def closed_card(engine, **overrides):
    values = dict(
        environment="PROD", account_no="test", symbol="TEST", buylist_member=True,
        board_status=BoardStatus.CLOSED, position_runtime_status=PositionRuntimeStatus.CLOSED,
        breakout_price=100.0, risk_percent=0.005, average_entry_price=98.0,
        entry_execution_price=98.0, entry_orb_low=97.0, planned_quantity=50,
        target_position_quantity=50, selected_orb_window="1m",
        entry_attempt_group_id="old-entry", entry_client_order_id="old-buy",
        exit_attempt_group_id="old-exit", exit_client_order_id="old-sell",
        last_buy_today_session_date=date(2026, 10, 5), buy_today_note="Stopped out",
    )
    values.update(overrides)
    return cards.create_trade_card(engine, TradeCardState(**values))


def command(card, kind=ActivateForToday):
    return kind(environment=card.environment, account_no=card.account_no, symbol=card.symbol,
                expected_card_version=card.version)


@pytest.mark.parametrize("kind", [ActivateForToday, MoveToBuylist])
def test_closed_cycle_restarts_explicitly_and_preserves_completed_fill_history(engine, kind):
    card = closed_card(engine)
    record_execution_order(engine, ExecutionOrderRecord(
        environment="PROD", account_no="test", symbol="TEST", side=OrderSide.BUY,
        intent=OrderIntent.ENTRY, client_order_id="old-buy", status=ExecutionOrderStatus.FILLED,
        broker_order_id="old-broker-buy", broker_identity_status=BrokerIdentityStatus.EXACT,
        submitted_quantity=50, filled_quantity=50, remaining_quantity=0,
        prepared_at="2026-10-05T13:31:00+00:00",
    ))
    result = request_board_action(engine, command(card, kind), context=BoardActionContext(
        local_operator_control=True, session_date=date(2026, 10, 5)))
    updated = result.card
    assert updated.board_status == (BoardStatus.BUY_TODAY if kind is ActivateForToday else BoardStatus.BUYLIST)
    assert updated.breakout_price == 100.0 and updated.risk_percent == 0.005
    assert updated.broker_quantity == updated.orderable_quantity == 0
    assert updated.average_entry_price == updated.planned_quantity == updated.target_position_quantity == 0
    assert updated.entry_client_order_id == updated.entry_attempt_group_id == ""
    assert updated.exit_client_order_id == updated.exit_attempt_group_id == ""
    assert updated.entry_execution_price is None and updated.active_stop_price is None
    assert not updated.exit_all_required and updated.buy_today_note == ""
    if kind is ActivateForToday:
        assert updated.entry_runtime_status == EntryRuntimeStatus.ORB_FORMING
        assert updated.session_date == date(2026, 10, 5)
    assert len(list_execution_orders_for_card(engine, environment="PROD", account_no="test", symbol="TEST")) == 1


@pytest.mark.parametrize("unsafe", [
    {"broker_quantity": 1}, {"orderable_quantity": 1}, {"position_runtime_status": PositionRuntimeStatus.OPEN},
    {"exit_all_required": True}, {"exit_submission_unresolved": True}, {"entry_submission_unresolved": True},
    {"exit_cancel_in_flight": True}, {"entry_cancel_in_flight": True}, {"reserved_sell_quantity": 1},
    {"pending_stop_price": 97.0}, {"pending_partial_sell_quantity": 1}, {"entry_remaining_target_quantity": 1},
    {"capital_reservation_id": "still-reserved"},
])
def test_reentry_cannot_orphan_any_unresolved_exposure(engine, unsafe):
    card = closed_card(engine, **unsafe)
    with pytest.raises(BoardCommandRejectedError, match="from CLOSED while"):
        request_board_action(engine, command(card), context=BoardActionContext())
    unchanged = cards.get_trade_card(engine, "PROD", "test", "TEST")
    assert unchanged.board_status == BoardStatus.CLOSED and unchanged.version == card.version


@pytest.mark.parametrize("side", [OrderSide.BUY, OrderSide.SELL])
def test_reentry_blocks_active_owned_orders(engine, side):
    card = closed_card(engine)
    record_execution_order(engine, ExecutionOrderRecord(
        environment="PROD", account_no="test", symbol="TEST", side=side,
        intent=OrderIntent.ENTRY if side == OrderSide.BUY else OrderIntent.STOP_LOSS,
        client_order_id="working", submitted_quantity=1, remaining_quantity=1,
        broker_order_id="working-broker", broker_identity_status=BrokerIdentityStatus.EXACT,
        status=ExecutionOrderStatus.WORKING,
    ))
    with pytest.raises(BoardCommandRejectedError, match="order is still active"):
        request_board_action(engine, command(card), context=BoardActionContext())


@pytest.mark.parametrize("status", ["REQUESTED", "AMBIGUOUS"])
def test_reentry_blocks_unresolved_broker_command_without_an_order_row(engine, status):
    card = closed_card(engine)
    record_command(engine, ExecutionCommand(idempotency_key="unresolved", command_type="submit",
        environment="PROD", account_no="test", symbol="TEST", status=status, lease_epoch=0))
    with pytest.raises(BoardCommandRejectedError, match="broker command is unresolved"):
        request_board_action(engine, command(card), context=BoardActionContext())


@pytest.mark.parametrize("terminal", [ExecutionOrderStatus.FILLED,
    ExecutionOrderStatus.CANCELLED, ExecutionOrderStatus.EXPIRED])
def test_reentry_can_retire_ambiguous_cancel_with_later_exact_terminal_observation(engine, terminal):
    card = closed_card(engine)
    order = ExecutionOrderRecord(environment="PROD", account_no="test", symbol="TEST",
        side=OrderSide.BUY, intent=OrderIntent.ENTRY, client_order_id="old-buy",
        broker_order_id="exact-old", broker_identity_status=BrokerIdentityStatus.EXACT,
        status=terminal, submitted_quantity=10, remaining_quantity=0,
        last_broker_seen_at="2026-08-28T15:22:41+00:00",
        last_reconciled_at="2026-08-28T15:22:41+00:00")
    record_execution_order(engine, order)
    record_command(engine, ExecutionCommand(idempotency_key="old-cancel", command_type="cancel",
        environment="PROD", account_no="test", symbol="TEST", status="AMBIGUOUS", lease_epoch=0,
        target_broker_order_id="exact-old", requested_at="2026-08-28T15:22:14"))
    updated = request_board_action(engine, command(card), context=BoardActionContext()).card
    assert updated.board_status == BoardStatus.BUY_TODAY
    assert len(list_execution_orders_for_card(engine, environment="PROD", account_no="test", symbol="TEST")) == 1


@pytest.mark.parametrize("changes", [
    {"target_broker_order_id": "wrong-target"}, {"target_broker_order_id": ""},
    {"status": "REQUESTED"}, {"command_type": "submit"}, {"command_type": "replace"},
    {"requested_at": "2026-08-28T15:22:42+00:00"},
])
def test_terminal_order_does_not_excuse_other_unresolved_commands(engine, changes):
    card = closed_card(engine)
    record_execution_order(engine, ExecutionOrderRecord(environment="PROD", account_no="test",
        symbol="TEST", side=OrderSide.BUY, intent=OrderIntent.ENTRY, client_order_id="old-buy",
        broker_order_id="exact-old", broker_identity_status=BrokerIdentityStatus.EXACT,
        status=ExecutionOrderStatus.FILLED, remaining_quantity=0,
        last_broker_seen_at="2026-08-28T15:22:41+00:00", last_reconciled_at="2026-08-28T15:22:41+00:00"))
    fields = dict(idempotency_key="unresolved", command_type="cancel", environment="PROD",
        account_no="test", symbol="TEST", status="AMBIGUOUS", lease_epoch=0,
        target_broker_order_id="exact-old", requested_at="2026-08-28T15:22:14+00:00")
    fields.update(changes)
    record_command(engine, ExecutionCommand(**fields))
    with pytest.raises(BoardCommandRejectedError, match="broker command is unresolved"):
        request_board_action(engine, command(card), context=BoardActionContext())


@pytest.mark.parametrize("changes", [
    {"last_broker_seen_at": None}, {"last_reconciled_at": None},
    {"last_broker_seen_at": "invalid"}, {"last_reconciled_at": "invalid"},
    {"last_broker_seen_at": "2026-08-28T15:22:13+00:00"},
    {"last_reconciled_at": "2026-08-28T15:22:13+00:00"},
    {"last_reconciled_at": "2099-01-01T00:00:00+00:00"}, {"remaining_quantity": 1},
])
def test_terminal_cancel_requires_complete_later_broker_evidence(engine, changes):
    card = closed_card(engine)
    fields = dict(environment="PROD", account_no="test", symbol="TEST", side=OrderSide.BUY,
        intent=OrderIntent.ENTRY, client_order_id="old-buy", broker_order_id="exact-old",
        broker_identity_status=BrokerIdentityStatus.EXACT, status=ExecutionOrderStatus.CANCELLED,
        submitted_quantity=10, remaining_quantity=0, last_broker_seen_at="2026-08-28T15:22:41+00:00",
        last_reconciled_at="2026-08-28T15:22:41+00:00")
    fields.update(changes)
    record_execution_order(engine, ExecutionOrderRecord(**fields))
    record_command(engine, ExecutionCommand(idempotency_key="old-cancel", command_type="cancel",
        environment="PROD", account_no="test", symbol="TEST", status="AMBIGUOUS", lease_epoch=0,
        target_broker_order_id="exact-old", requested_at="2026-08-28T15:22:14+00:00"))
    with pytest.raises(BoardCommandRejectedError, match="broker command is unresolved"):
        request_board_action(engine, command(card), context=BoardActionContext())


def test_cancel_terminal_evidence_rejects_invalid_request_timestamp_and_duplicate_target():
    old_cancel = ExecutionCommand(idempotency_key="old-cancel", command_type="cancel",
        environment="PROD", account_no="test", symbol="TEST", status="AMBIGUOUS", lease_epoch=0,
        target_broker_order_id="exact-old", requested_at="invalid")
    terminal = ExecutionOrderRecord(environment="PROD", account_no="test", symbol="TEST",
        side=OrderSide.BUY, intent=OrderIntent.ENTRY, client_order_id="old-buy", broker_order_id="exact-old",
        broker_identity_status=BrokerIdentityStatus.EXACT, status=ExecutionOrderStatus.FILLED,
        last_broker_seen_at="2026-08-28T15:22:41+00:00", last_reconciled_at="2026-08-28T15:22:41+00:00")
    assert not _cancel_target_is_confirmed_terminal(old_cancel, [terminal])
    old_cancel.requested_at = "2026-08-28T15:22:14+00:00"
    assert not _cancel_target_is_confirmed_terminal(old_cancel, [terminal, terminal])


def test_multiple_stopped_out_cycles_need_a_new_explicit_activation_each_time(engine):
    card = closed_card(engine)
    for _cycle in range(3):
        card = request_board_action(engine, command(card), context=BoardActionContext()).card
        assert card.board_status == BoardStatus.BUY_TODAY
        card.broker_quantity = card.orderable_quantity = 10
        card.position_runtime_status = PositionRuntimeStatus.OPEN
        card.board_status = BoardStatus.OPEN_POSITION
        PositionManager().apply_first_fill_stop(card, entry_orb_low=97.0, entry_orb_window="1m")
        PositionManager().evaluate_tick(card, current_price=96.0)
        assert card.exit_all_required
        card.board_status = BoardStatus.SELL_ALL
        card.broker_quantity = card.orderable_quantity = 0
        PositionManager().confirm_flat(card)
        card = cards.update_trade_card(engine, card, expected_version=card.version)
        assert card.board_status == BoardStatus.CLOSED


@pytest.mark.parametrize("price", [None, 0, -1])
def test_reentry_requires_a_breakout_price(engine, price):
    card = closed_card(engine, breakout_price=price)
    with pytest.raises(BoardCommandRejectedError, match="breakout price"):
        request_board_action(engine, command(card), context=BoardActionContext())


@pytest.mark.parametrize("status", [CapitalReservationStatus.CONSUMED,
    CapitalReservationStatus.RELEASED, CapitalReservationStatus.EXPIRED])
def test_closed_card_can_retire_a_verified_terminal_reservation_reference(engine, status):
    reservation = CapitalReservation(reservation_id="completed", environment="PROD",
        account_no="test", symbol="TEST", attempt_group_id="old-entry", requested_notional=100,
        remaining_reserved_notional=0, remaining_projected_open_risk=0, status=status)
    save_reservation_strict(engine, reservation)
    card = closed_card(engine, capital_reservation_id=reservation.reservation_id)
    updated = request_board_action(engine, command(card), context=BoardActionContext()).card
    assert updated.board_status == BoardStatus.BUY_TODAY
    assert updated.capital_reservation_id == ""


@pytest.mark.parametrize("values", [
    {"remaining_reserved_notional": 1}, {"remaining_projected_open_risk": 1},
    {"account_no": "another"}, {"symbol": "OTHER"},
    {"status": CapitalReservationStatus.RESERVED},
    {"status": CapitalReservationStatus.PARTIALLY_CONSUMED},
])
def test_reentry_cannot_ignore_an_active_inconsistent_or_wrong_scope_reservation(engine, values):
    fields = dict(reservation_id="unsafe", environment="PROD", account_no="test", symbol="TEST",
        attempt_group_id="old-entry", requested_notional=100, remaining_reserved_notional=0,
        remaining_projected_open_risk=0, status=CapitalReservationStatus.RELEASED)
    fields.update(values)
    save_reservation_strict(engine, CapitalReservation(**fields))
    card = closed_card(engine, capital_reservation_id="unsafe")
    with pytest.raises(BoardCommandRejectedError, match="capital remains reserved"):
        request_board_action(engine, command(card), context=BoardActionContext())


def test_reentry_blocks_active_symbol_reservation_even_without_a_card_reference(engine):
    save_reservation_strict(engine, CapitalReservation(reservation_id="unlinked", environment="PROD",
        account_no="test", symbol="TEST", attempt_group_id="old-entry", requested_notional=100,
        remaining_reserved_notional=100))
    card = closed_card(engine)
    with pytest.raises(BoardCommandRejectedError, match="capital remains reserved"):
        request_board_action(engine, command(card), context=BoardActionContext())
