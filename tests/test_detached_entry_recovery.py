from copy import deepcopy
from datetime import date, datetime, timezone

import pytest

from src.core.account_broker_snapshot import AccountBrokerSnapshot, AccountHoldingSnapshot, SnapshotCompleteness
from src.core.capital_reservation import CapitalReservation
from src.core.execution_order_record import BrokerIdentityStatus, ExecutionOrderRecord, ExecutionOrderStatus
from src.core.order_state import BrokerOrderStatusSnapshot, OrderIntent, OrderSide, OrderStatus
from src.core.trade_card_state import BoardStatus, EntryRuntimeStatus, TradeCardState
from src.services.account_reconciliation import AccountLocalState, reduce_account_reconciliation
from src.services.buyboard_runtime import _portfolio_projected_exposures, _unrepresented_entry_fills
from src.core.capital_reservation import CapitalReservationStatus


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


def _reduce(card, order, reservation, broker, *, extra_orders=(), extra_broker=(), complete=True, holdings=(), holdings_complete=True):
    return reduce_account_reconciliation(
        AccountBrokerSnapshot(
            environment='PROD', account_no='1', orders=(broker, *extra_broker),
            holdings=holdings,
            observed_at=_time('16:40:00'), session_date=date(2026, 10, 27),
            completeness=SnapshotCompleteness(holdings_complete=holdings_complete, open_orders_complete=complete,
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


def _filled_incident():
    card, order, reservation, broker = _incident()
    order.status, order.filled_quantity, order.remaining_quantity = ExecutionOrderStatus.FILLED, 32, 0
    order.average_fill_price = 90.8
    broker.status, broker.filled_quantity, broker.remaining_quantity = OrderStatus.FILLED, 32, 0
    broker.avg_fill_price = 90.8
    return card, order, reservation, broker


def test_filled_order_stays_in_risk_projection_until_holdings_arrive():
    card, order, _, _ = _filled_incident()
    exposures = _portfolio_projected_exposures(cards=(card,), execution_orders=(order,))
    fill, = exposures
    assert fill.source == 'FILLED_ENTRY_AWAITING_POSITION'
    assert fill.gross_notional_usd == pytest.approx(2905.6)
    assert fill.open_risk_usd == pytest.approx(38.08)
    assert not fill.reservation_id
    assert _unrepresented_entry_fills(card, (order,)) == (order,)
    card.broker_quantity = 32
    assert not _portfolio_projected_exposures(cards=(card,), execution_orders=(order,))


def test_filled_entry_fence_does_not_resurrect_an_older_candidate():
    card, order, _, _ = _filled_incident()
    card.entry_candidate_created_at = _time('15:00:00')
    assert not _unrepresented_entry_fills(card, (order,))
    assert not _portfolio_projected_exposures(cards=(card,), execution_orders=(order,))


@pytest.mark.parametrize('sell_time', ['2026-10-27T15:00:00+00:00', '2026-10-28T15:00:00+00:00'])
def test_partial_exit_is_deducted_from_proven_fill_exposure(sell_time):
    card, order, _, _ = _filled_incident()
    order.filled_quantity = order.submitted_quantity = 64
    sell = deepcopy(order)
    sell.client_order_id, sell.broker_order_id = 'SELL-1', 'BROKER-SELL'
    sell.side, sell.intent = OrderSide.SELL, OrderIntent.PARTIAL_EXIT
    sell.submission_started_at = sell_time
    sell.filled_quantity = sell.submitted_quantity = 32
    card.broker_quantity = 32
    assert not _unrepresented_entry_fills(card, (order, sell))
    assert not _portfolio_projected_exposures(cards=(card,), execution_orders=(order, sell))


def test_confirmed_fill_retains_stop_across_repeated_empty_holdings_then_exit():
    card, order, reservation, broker = _filled_incident()
    card.entry_client_order_id = order.client_order_id
    card.entry_attempt_group_id = order.attempt_group_id
    first = _reduce(card, order, reservation, broker)
    repaired, = first.card_updates
    assert repaired.broker_quantity == repaired.stop_quantity == 32
    assert repaired.active_stop_price == 89.61
    updated_order = first.order_updates[0] if first.order_updates else order
    second = _reduce(repaired, updated_order, reservation, broker)
    preserved = second.card_updates[0] if second.card_updates else repaired
    assert preserved.broker_quantity == preserved.stop_quantity == 32
    assert preserved.active_stop_price == 89.61
    assert any(item.code == 'HOLDINGS_LAG_ENTRY_FILL' for item in second.alerts)
    sell = deepcopy(order)
    sell.side, sell.intent = OrderSide.SELL, OrderIntent.PARTIAL_EXIT
    sell.client_order_id, sell.broker_order_id = 'SELL-1', 'BROKER-SELL'
    sell.submission_started_at = _time('15:00:00').isoformat()
    closed = _reduce(preserved, updated_order, reservation, broker, extra_orders=(sell,))
    flat, = closed.card_updates
    assert flat.broker_quantity == 0
    assert flat.active_stop_price is None


@pytest.mark.parametrize('quantity', [16, 32])
def test_detached_fill_recovers_original_stop_after_fresh_holdings(quantity):
    card, order, reservation, broker = _filled_incident()
    if quantity == 16:
        order.status, broker.status = ExecutionOrderStatus.PARTIALLY_FILLED, OrderStatus.PARTIALLY_FILLED
    order.filled_quantity = broker.filled_quantity = quantity
    order.remaining_quantity = broker.remaining_quantity = 32 - quantity
    reservation.remaining_reserved_notional = (32 - quantity) * 90.8
    reservation.remaining_projected_open_risk = (32 - quantity) * 1.19
    holding = AccountHoldingSnapshot(symbol='KOD', quantity=quantity, average_price=90.8, sellable_quantity=quantity)
    plan = _reduce(card, order, reservation, broker, holdings=(holding,))
    repaired, = plan.card_updates
    assert repaired.board_status == BoardStatus.OPEN_POSITION
    assert repaired.broker_quantity == repaired.stop_quantity == quantity
    assert repaired.active_stop_price == 89.61
    assert 'STOP_REQUIRED' not in repaired.warnings
    assert not plan.commands


def test_partial_fill_and_pending_remainder_keep_the_full_commitment():
    card, order, reservation, _ = _filled_incident()
    order.status = ExecutionOrderStatus.PARTIALLY_FILLED
    order.filled_quantity = order.remaining_quantity = 16
    reservation.remaining_reserved_notional = 16 * 90.8
    reservation.remaining_projected_open_risk = 16 * 1.19
    exposures = _portfolio_projected_exposures(cards=(card,), execution_orders=(order,), active_reservations=(reservation,))
    assert sum(item.gross_notional_usd for item in exposures) == pytest.approx(2905.6)


def test_two_confirmed_fills_only_project_quantity_missing_from_holdings():
    card, order, _, _ = _filled_incident()
    other = deepcopy(order)
    other.client_order_id, other.broker_order_id = 'SECOND', 'SECOND-BROKER'
    card.broker_quantity = 32
    exposures = _portfolio_projected_exposures(cards=(card,), execution_orders=(order, other))
    exposure, = exposures
    assert exposure.gross_notional_usd == pytest.approx(2905.6)


def test_fresh_partial_fill_recovers_before_same_plan_reservation_settlement():
    card, order, reservation, broker = _incident()
    broker.status = OrderStatus.PARTIALLY_FILLED
    broker.filled_quantity = broker.remaining_quantity = 16
    broker.avg_fill_price = 90.8
    holding = AccountHoldingSnapshot(symbol='KOD', quantity=16, average_price=90.8, sellable_quantity=16)
    plan = _reduce(card, order, reservation, broker, holdings=(holding,))
    repaired, = plan.card_updates
    assert repaired.broker_quantity == repaired.stop_quantity == 16
    assert repaired.active_stop_price == 89.61


def test_loader_fetches_consumed_candidate_proof_on_fresh_and_cached_reads():
    from sqlalchemy import create_engine
    from src.services.account_reconciliation import load_account_local_state
    from src.services.capital_reservation_repository import save_reservation_strict
    from src.services.execution_order_repository import ensure_execution_orders_table, insert_execution_order
    card, order, reservation, _ = _filled_incident()
    reservation.status = CapitalReservationStatus.CONSUMED
    reservation.remaining_reserved_notional = reservation.remaining_projected_open_risk = 0
    engine = create_engine('sqlite:///:memory:')
    try:
        ensure_execution_orders_table(engine)
        with engine.begin() as conn:
            insert_execution_order(conn, order)
        save_reservation_strict(engine, reservation)
        for _ in range(2):
            state = load_account_local_state(engine, environment='PROD', account_no='1', cards=(card,))
            assert len(state.capital_reservations) == 1
            assert state.capital_reservations[0].status == CapitalReservationStatus.CONSUMED
    finally:
        engine.dispose()


@pytest.mark.parametrize('remaining', [0, 16])
def test_external_exit_after_holdings_account_for_fill_does_not_leave_phantom_exposure(remaining):
    card, order, reservation, broker = _filled_incident()
    holding = AccountHoldingSnapshot(symbol='KOD', quantity=32, average_price=90.8, sellable_quantity=32)
    opened = _reduce(card, order, reservation, broker, holdings=(holding,))
    card = TradeCardState.from_dict(opened.card_updates[0].to_dict())
    manual = deepcopy(broker)
    manual.broker_order_id, manual.side = 'MANUAL-SELL', OrderSide.SELL
    manual.filled_quantity = manual.quantity_requested = 32 - remaining
    # KIS has no verified normalized submission timestamp. Holdings proof
    # must work without inventing one or adopting the external order.
    assert not manual.submitted_at
    holdings = (AccountHoldingSnapshot(symbol='KOD', quantity=remaining, average_price=90.8,
                                     sellable_quantity=remaining),) if remaining else ()
    reduced = _reduce(card, order, reservation, broker, extra_broker=(manual,), holdings=holdings)
    repaired, = reduced.card_updates
    assert repaired.broker_quantity == repaired.stop_quantity == remaining
    assert not _unrepresented_entry_fills(repaired, (order,))
    assert not _portfolio_projected_exposures(cards=(repaired,), execution_orders=(order,))
    assert not any(alert.code == 'HOLDINGS_LAG_ENTRY_FILL' for alert in reduced.alerts)
    assert not reduced.commands
    assert all(external.disposition.value != 'ADOPTED' for external in reduced.external_order_updates)
    repeated = _reduce(repaired, order, reservation, broker, extra_broker=(manual,), holdings=holdings)
    final = repeated.card_updates[0] if repeated.card_updates else repaired
    assert final.broker_quantity == remaining
    assert not _unrepresented_entry_fills(final, (order,))


def test_new_partial_fill_after_manual_exit_stays_fenced_until_holdings_catch_up():
    from src.core.entry_fill_exposure import reconcile_candidate_fill_holdings
    card, order, _, _ = _filled_incident()
    order.filled_quantity = 16
    reconcile_candidate_fill_holdings(card, (order,), 16)
    reconcile_candidate_fill_holdings(card, (order,), 0)
    card.broker_quantity = 0
    assert not _unrepresented_entry_fills(card, (order,))
    order.filled_quantity = 32
    reconcile_candidate_fill_holdings(card, (order,), 0)
    assert _unrepresented_entry_fills(card, (order,)) == (order,)
    projected, = _portfolio_projected_exposures(cards=(card,), execution_orders=(order,))
    assert projected.gross_notional_usd == pytest.approx(16 * 90.8)
    reconcile_candidate_fill_holdings(card, (order,), 16)
    card.broker_quantity = 16
    assert not _unrepresented_entry_fills(card, (order,))


def test_holdings_checkpoint_is_candidate_scoped_and_reset_with_generation():
    from src.core.entry_fill_exposure import reconcile_candidate_fill_holdings
    card, order, _, _ = _filled_incident()
    reconcile_candidate_fill_holdings(card, (order,), 32)
    reconcile_candidate_fill_holdings(card, (order,), 0)
    assert not _unrepresented_entry_fills(card, (order,))
    card.entry_candidate_created_at = _time('13:54:50')
    assert _unrepresented_entry_fills(card, (order,)) == (order,)
    card.clear_orb_generation_metadata()
    assert card.entry_fill_reconciliation == {}


def test_projected_fill_alone_never_acknowledges_complete_holdings():
    card, order, reservation, broker = _filled_incident()
    card.entry_client_order_id = order.client_order_id
    card.entry_attempt_group_id = order.attempt_group_id
    first = _reduce(card, order, reservation, broker)
    card = first.card_updates[0]
    assert card.broker_quantity == 32
    assert not card.entry_fill_reconciliation
    repeated = _reduce(card, order, reservation, broker)
    assert any(alert.code == 'HOLDINGS_LAG_ENTRY_FILL' for alert in repeated.alerts)


def test_incomplete_holdings_cannot_retire_previously_observed_entry():
    card, order, reservation, broker = _filled_incident()
    holding = AccountHoldingSnapshot(symbol='KOD', quantity=32, average_price=90.8, sellable_quantity=32)
    opened = _reduce(card, order, reservation, broker, holdings=(holding,))
    card = opened.card_updates[0]
    before = card.to_dict()
    incomplete = _reduce(card, order, reservation, broker, holdings_complete=False)
    final = incomplete.card_updates[0] if incomplete.card_updates else card
    assert final.broker_quantity == 32
    assert final.entry_fill_reconciliation == before['entry_fill_reconciliation']


def test_second_buy_after_external_reduction_still_projects_missing_quantity():
    from src.core.entry_fill_exposure import reconcile_candidate_fill_holdings
    card, order, _, _ = _filled_incident()
    reconcile_candidate_fill_holdings(card, (order,), 32)
    reconcile_candidate_fill_holdings(card, (order,), 16)
    card.broker_quantity = 16
    second = deepcopy(order)
    second.client_order_id, second.broker_order_id = 'NEW-BUY', 'NEW-BROKER'
    second.filled_quantity = second.submitted_quantity = 16
    reconcile_candidate_fill_holdings(card, (order, second), 16)
    exposure, = _portfolio_projected_exposures(cards=(card,), execution_orders=(order, second))
    assert exposure.gross_notional_usd == pytest.approx(16 * 90.8)
