from copy import deepcopy
from datetime import date, datetime, timezone

import pytest

from src.core.account_broker_snapshot import AccountBrokerSnapshot, SnapshotCompleteness
from src.core.capital_reservation import CapitalReservation
from src.core.execution_order_record import BrokerIdentityStatus, ExecutionOrderRecord, ExecutionOrderStatus
from src.core.order_state import BrokerOrderStatusSnapshot, OrderIntent, OrderSide, OrderStatus
from src.core.trade_card_state import BoardStatus, EntryRuntimeStatus, TradeCardState
from src.services.account_reconciliation import AccountLocalState, reduce_account_reconciliation


def _time(value):
    return datetime.fromisoformat('2026-10-27T' + value + '+00:00')


def _incident():
    card = TradeCardState(
        environment='PROD', account_no='1', symbol='KOD',
        board_status=BoardStatus.BUY_TODAY, session_date=date(2026, 10, 27),
        planned_quantity=32, target_position_quantity=32,
        entry_trigger=90.8, entry_execution_price=90.8, entry_orb_low=89.61,
        selected_orb_window='5m', entry_orb_window='5m',
        entry_candidate_created_at=_time('13:54:49'),
        entry_range_closed_at=_time('13:35:00'),
        entry_breakout_confirmed_at=_time('13:54:52'),
        entry_attempt_group_id='NEW-UNSUBMITTED', entry_client_order_id='NEW-CLIENT',
        entry_pending_attempt_number=2, entry_block_reason='Maximum single-position notional',
    )
    order = ExecutionOrderRecord(
        environment='PROD', account_no='1', symbol='KOD', side=OrderSide.BUY,
        intent=OrderIntent.ENTRY, client_order_id='ORIGINAL-CLIENT', broker_order_id='BROKER-1',
        attempt_group_id='ORIGINAL-GROUP', capital_reservation_id='R-1',
        submitted_quantity=32, remaining_quantity=32, submitted_limit_price=90.8,
        status=ExecutionOrderStatus.WORKING, broker_identity_status=BrokerIdentityStatus.EXACT,
        prepared_at=_time('13:54:55').isoformat(),
        submission_started_at=_time('13:54:56').isoformat(),
        acknowledged_at=_time('13:54:58').isoformat(),
    )
    reservation = CapitalReservation(
        reservation_id='R-1', environment='PROD', account_no='1', symbol='KOD',
        attempt_group_id='ORIGINAL-GROUP', requested_notional=2905.6,
        remaining_reserved_notional=2905.6, projected_open_risk=38.08,
        remaining_projected_open_risk=38.08,
    )
    broker = BrokerOrderStatusSnapshot(
        environment='PROD', account_no='1', symbol='KOD', broker_order_id='BROKER-1',
        side=OrderSide.BUY, status=OrderStatus.WORKING, quantity_requested=32,
        remaining_quantity=32, limit_price=90.8, raw_response={'ord_dt':'20261027'},
    )
    return card, order, reservation, broker


def _reduce(card, order, reservation, broker, *, extra_orders=(), extra_broker=(), complete=True):
    return reduce_account_reconciliation(
        AccountBrokerSnapshot(
            environment='PROD', account_no='1', orders=(broker, *extra_broker),
            observed_at=_time('16:40:00'), session_date=date(2026, 10, 27),
            completeness=SnapshotCompleteness(holdings_complete=True, open_orders_complete=complete,
                history_complete=True, reserved_orders_complete=True, account_balance_complete=True),
        ),
        AccountLocalState(cards=(card,), execution_orders=(order, *extra_orders),
            capital_reservations=(reservation,)),
    )


def test_detached_working_entry_recovers_original_obligation_without_new_exposure():
    card, order, reservation, broker = _incident()
    original = deepcopy(card.to_dict())
    plan = _reduce(card, order, reservation, broker)
    repaired, = plan.card_updates
    assert repaired.board_status == BoardStatus.ENTRY_PENDING
    assert repaired.entry_runtime_status == EntryRuntimeStatus.ORDER_PENDING
    assert repaired.entry_client_order_id == order.client_order_id
    assert repaired.entry_attempt_group_id == order.attempt_group_id
    assert repaired.entry_attempt_count == repaired.entry_pending_attempt_number == 1
    assert repaired.capital_reservation_id == reservation.reservation_id
    assert repaired.entry_parent_intent_id == order.attempt_group_id
    assert repaired.entry_order_generation == 1
    assert repaired.entry_block_reason == ''
    assert repaired.next_retry_at is None
    assert repaired.planned_quantity == repaired.target_position_quantity == 32
    assert repaired.entry_orb_low == 89.61
    assert not plan.commands
    assert not plan.reservation_updates
    assert card.to_dict() == original
    assert _reduce(repaired, order, reservation, broker).card_updates == ()


@pytest.mark.parametrize('case', [
    'terminal', 'filled', 'prior_broker_session', 'new_candidate', 'changed_stop',
    'changed_quantity', 'wrong_reservation_group', 'wrong_reservation_notional',
    'wrong_reservation_risk', 'incomplete_orders', 'foreign_account',
    'cancel_intent', 'replacement', 'different_parent', 'missing_submission',
    'second_local_order', 'second_broker_order', 'stop_exit', 'partial_exit',
    'reserved_exit', 'unresolved_exit', 'cancel_exit', 'tracked_exit',
])
def test_detached_entry_recovery_rejects_unproven_or_conflicting_lifecycle(case):
    card, order, reservation, broker = _incident()
    kwargs = {}
    if case == 'terminal':
        broker.status = OrderStatus.CANCELLED
        broker.remaining_quantity = 0
    elif case == 'filled':
        broker.filled_quantity, broker.remaining_quantity = 1, 31
        broker.status = OrderStatus.PARTIALLY_FILLED
    elif case == 'prior_broker_session':
        broker.raw_response['ord_dt'] = '20261026'
    elif case == 'new_candidate':
        card.entry_candidate_created_at = _time('14:00:00')
    elif case == 'changed_stop':
        card.entry_orb_low = 89.0
    elif case == 'changed_quantity':
        card.planned_quantity = 33
    elif case == 'wrong_reservation_group':
        reservation.attempt_group_id = 'OTHER'
    elif case == 'wrong_reservation_notional':
        reservation.remaining_reserved_notional = 100
    elif case == 'wrong_reservation_risk':
        reservation.remaining_projected_open_risk = 100
    elif case == 'incomplete_orders':
        kwargs['complete'] = False
    elif case == 'foreign_account':
        broker.account_no = 'OTHER'
    elif case == 'cancel_intent':
        card.entry_block_reason = 'cancel_requested'
    elif case == 'replacement':
        card.pending_entry_replacement = {'state':'QUALIFIED'}
    elif case == 'different_parent':
        card.entry_parent_intent_id = 'OTHER-CYCLE'
    elif case == 'missing_submission':
        order.submission_started_at = None
    elif case == 'second_local_order':
        other = deepcopy(order)
        other.client_order_id, other.broker_order_id = 'OTHER-CLIENT', 'OTHER-BROKER'
        kwargs['extra_orders'] = (other,)
    elif case == 'second_broker_order':
        other = deepcopy(broker)
        other.broker_order_id = 'OTHER-BROKER'
        kwargs['extra_broker'] = (other,)
    elif case == 'stop_exit':
        card.stop_loss_triggered = True
    elif case == 'partial_exit':
        card.pending_partial_sell_quantity = 1
    elif case == 'reserved_exit':
        card.reserved_sell_quantity = 1
    elif case == 'unresolved_exit':
        card.exit_submission_unresolved = True
    elif case == 'cancel_exit':
        card.exit_cancel_in_flight = True
    elif case == 'tracked_exit':
        card.exit_client_order_id = 'EXIT-1'
    plan = _reduce(card, order, reservation, broker, **kwargs)
    assert all(updated.entry_client_order_id != order.client_order_id for updated in plan.card_updates)
    assert not plan.commands
