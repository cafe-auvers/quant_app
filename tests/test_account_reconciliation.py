"""Workstream 4 account-level reconciliation coverage (PR3)."""
from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine

from src.core.account_broker_snapshot import (
    AccountBrokerSnapshot,
    AccountHoldingSnapshot,
    ReconciliationAction,
    SnapshotCompleteness,
)
from src.core.capital_reservation import CapitalReservation
from src.core.discovered_external_order import (
    ExternalOrderDisposition,
    new_discovered_external_order,
)
from src.core.execution_order_record import (
    BrokerIdentityStatus,
    ExecutionOrderRecord,
    ExecutionOrderStatus,
)
from src.core.order_recovery_state import OrderRecoveryState
from src.core.order_state import (
    RESERVED_MOO_EXECUTION,
    BrokerOrderDiscoveryResult,
    BrokerOrderStatusSnapshot,
    OrderIntent,
    OrderSide,
    OrderStatus,
)
from src.core.trade_card_state import (
    BoardStatus,
    PositionRuntimeStatus,
    StopType,
    TradeCardState,
)
from src.services.account_reconciliation import (
    AccountLocalState,
    ReconciliationPlan,
    ReconciliationCategory,
    ReconciliationCommandType,
    apply_reconciliation_plan,
    classify_execution_order,
    decide_emergency_sell,
    fetch_account_broker_snapshot,
    reduce_account_reconciliation,
    run_account_reconciliation_pass,
)


NOW = datetime(2026, 8, 15, 14, 0, tzinfo=timezone.utc)


def _completeness(**overrides) -> SnapshotCompleteness:
    values = dict(
        holdings_complete=True,
        open_orders_complete=True,
        history_complete=True,
        reserved_orders_complete=True,
        account_balance_complete=True,
    )
    values.update(overrides)
    return SnapshotCompleteness(**values)


def _snapshot(**overrides) -> AccountBrokerSnapshot:
    values = dict(
        environment="PROD",
        account_no="1",
        completeness=_completeness(),
        observed_at=NOW,
        session_date=NOW.date(),
        snapshot_id="snapshot-1",
    )
    values.update(overrides)
    return AccountBrokerSnapshot(**values)


def _card(symbol="AAPL", **overrides) -> TradeCardState:
    values = dict(
        environment="PROD",
        account_no="1",
        symbol=symbol,
        board_status=BoardStatus.ENTRY_PENDING,
        planned_quantity=10,
        target_position_quantity=10,
        entry_trigger=100.0,
        entry_orb_low=95.0,
        selected_orb_window="5m",
        entry_attempt_group_id="G-1",
        exit_attempt_group_id="G-1",
    )
    values.update(overrides)
    return TradeCardState(**values)


def _order(**overrides) -> ExecutionOrderRecord:
    values = dict(
        environment="PROD",
        account_no="1",
        symbol="AAPL",
        side=OrderSide.BUY,
        intent=OrderIntent.ENTRY,
        client_order_id="C-1",
        broker_order_id="B-1",
        submitted_quantity=10,
        submitted_limit_price=100.0,
        status=ExecutionOrderStatus.WORKING,
        broker_identity_status=BrokerIdentityStatus.EXACT,
        remaining_quantity=10,
        capital_reservation_id="R-1",
        attempt_group_id="G-1",
    )
    values.update(overrides)
    return ExecutionOrderRecord(**values)


def _holding(quantity=10, *, sellable_quantity=10, symbol="AAPL"):
    return AccountHoldingSnapshot(
        symbol=symbol,
        quantity=quantity,
        average_price=100.0,
        sellable_quantity=sellable_quantity,
    )


def _reservation(reservation_id="R-1", symbol="AAPL"):
    return CapitalReservation(
        reservation_id=reservation_id,
        environment="PROD",
        account_no="1",
        symbol=symbol,
        attempt_group_id="G-1",
        requested_notional=1000.0,
        remaining_reserved_notional=1000.0,
    )


def test_action_completeness_is_per_source_not_one_global_flag():
    completeness = _completeness(reserved_orders_complete=False)
    assert completeness.allows(ReconciliationAction.NEW_ENTRY) is True
    assert completeness.allows(ReconciliationAction.EMERGENCY_SELL_ALL) is True
    assert completeness.allows(ReconciliationAction.RESERVED_MOO_RECONCILIATION) is False


def test_one_snapshot_is_fetched_and_reused_across_every_card_in_an_account():
    class Broker:
        positions_calls = 0
        discovery_calls = 0

        def get_positions(self, **kwargs):
            self.positions_calls += 1
            return {
                "overseas": {
                    "holdings": [
                        {"symbol": "AAPL", "quantity": 10, "average_price": 100},
                        {"symbol": "MSFT", "quantity": 5, "average_price": 200},
                    ]
                }
            }

        def discover_orders(self, **kwargs):
            self.discovery_calls += 1
            return BrokerOrderDiscoveryResult(
                open_orders_complete=True,
                history_complete=True,
                reserved_orders_complete=True,
            )

    broker = Broker()
    engine = create_engine("sqlite://", future=True)
    result = run_account_reconciliation_pass(
        broker=broker,
        engine=engine,
        environment="PROD",
        account_no="1",
        cards=(_card(), _card("MSFT")),
        account_balance_provider=lambda environment, account_no: 100_000,
        clock=lambda: NOW,
        persist=False,
    )
    assert broker.positions_calls == 1
    assert broker.discovery_calls == 1
    assert {card.symbol for card in result.plan.card_updates} == {"AAPL", "MSFT"}


def test_incomplete_reserved_orders_does_not_block_an_emergency_sell_all():
    card = _card(
        board_status=BoardStatus.SELL_ALL,
        broker_quantity=10,
        orderable_quantity=10,
        exit_all_required=True,
    )
    snapshot = _snapshot(
        holdings=(_holding(),),
        completeness=_completeness(reserved_orders_complete=False),
    )
    plan = reduce_account_reconciliation(snapshot, AccountLocalState(cards=(card,)))
    assert plan.commands[0].command_type == ReconciliationCommandType.EMERGENCY_SELL_ALL
    assert plan.commands[0].quantity == 10


def test_emergency_sell_all_subtracts_known_owned_outstanding_sell_quantity():
    sell = _order(
        side=OrderSide.SELL,
        intent=OrderIntent.MANUAL_EXIT,
        client_order_id="SELL-1",
        remaining_quantity=4,
        capital_reservation_id="",
    )
    decision = decide_emergency_sell(
        _snapshot(holdings=(_holding(),)),
        symbol="AAPL",
        execution_orders=(sell,),
    )
    assert decision.quantity == 6
    assert decision.cancel_client_order_id == "SELL-1"


def test_emergency_sell_all_uses_broker_sellable_quantity_when_outstanding_is_uncertain():
    snapshot = _snapshot(
        holdings=(_holding(sellable_quantity=7),),
        completeness=_completeness(open_orders_complete=False),
    )
    decision = decide_emergency_sell(snapshot, symbol="AAPL", execution_orders=())
    assert decision.quantity == 7
    assert decision.manual_intervention_required is False


def test_emergency_sell_all_alerts_rather_than_guesses_when_neither_is_available():
    snapshot = _snapshot(
        holdings=(_holding(sellable_quantity=None),),
        completeness=_completeness(open_orders_complete=False),
    )
    decision = decide_emergency_sell(snapshot, symbol="AAPL", execution_orders=())
    assert decision.quantity == 0
    assert decision.manual_intervention_required is True


def test_ambiguous_cancel_preserves_cancel_pending_and_emits_no_new_command():
    card = _card(
        board_status=BoardStatus.SELL_ALL,
        broker_quantity=10,
        exit_all_required=True,
        exit_cancel_in_flight=True,
        exit_cancel_command_id="CANCEL-STABLE",
    )
    sell = _order(
        side=OrderSide.SELL,
        intent=OrderIntent.MANUAL_EXIT,
        client_order_id="SELL-1",
        status=ExecutionOrderStatus.CANCEL_PENDING,
        recovery_state=OrderRecoveryState.DISCOVERING,
        remaining_quantity=4,
        capital_reservation_id="",
    )
    still_working = BrokerOrderStatusSnapshot(
        environment="PROD",
        account_no="1",
        symbol="AAPL",
        broker_order_id="B-1",
        side=OrderSide.SELL,
        status=OrderStatus.WORKING,
        quantity_requested=4,
        remaining_quantity=4,
    )

    plan = reduce_account_reconciliation(
        _snapshot(orders=(still_working,), holdings=(_holding(),)),
        AccountLocalState(cards=(card,), execution_orders=(sell,)),
    )

    assert plan.order_updates[0].status == ExecutionOrderStatus.CANCEL_PENDING
    assert plan.order_updates[0].recovery_state == OrderRecoveryState.DISCOVERING
    assert plan.commands == ()


def test_reducer_is_a_pure_function_of_snapshot_and_local_state():
    order = _order()
    card = _card()
    reservation = _reservation()
    state = AccountLocalState(
        cards=(card,), execution_orders=(order,), capital_reservations=(reservation,)
    )
    original = deepcopy(state)
    broker_order = BrokerOrderStatusSnapshot(
        environment="PROD",
        account_no="1",
        symbol="AAPL",
        broker_order_id="B-1",
        side=OrderSide.BUY,
        status=OrderStatus.PARTIALLY_FILLED,
        quantity_requested=10,
        filled_quantity=3,
        remaining_quantity=7,
        avg_fill_price=101.0,
    )
    snapshot = _snapshot(orders=(broker_order,), holdings=(_holding(3),))
    first = reduce_account_reconciliation(snapshot, state)
    second = reduce_account_reconciliation(snapshot, state)
    assert state == original
    assert first == second


def test_reducer_generated_ids_and_timestamps_are_snapshot_deterministic():
    external = BrokerOrderStatusSnapshot(
        environment="PROD",
        account_no="1",
        symbol="MSFT",
        broker_order_id="B-EXTERNAL",
        side=OrderSide.BUY,
        status=OrderStatus.WORKING,
        quantity_requested=2,
    )
    state = AccountLocalState(capital_reservations=(_reservation(),))
    original = deepcopy(state)
    snapshot = _snapshot(
        orders=(external,),
        holdings=(_holding(symbol="TSLA"),),
    )

    first = reduce_account_reconciliation(snapshot, state)
    second = reduce_account_reconciliation(snapshot, state)

    assert first == second
    assert state == original
    assert first.external_order_creates[0].discovered_at == NOW.isoformat()
    assert first.card_creates[0].created_at == NOW
    assert first.reservation_updates[0].absence_count == 1
    assert first.reservation_updates[0].released_at is None
    assert any(
        alert.code == "ORPHAN_CAPITAL_RESERVATION_REQUIRES_REVIEW"
        for alert in first.alerts
    )


def test_first_complete_absence_does_not_resolve_terminal():
    order = _order(recovery_state=OrderRecoveryState.AWAITING_CANCEL_CONFIRMATION)
    plan = reduce_account_reconciliation(
        _snapshot(holdings=(_holding(),)),
        AccountLocalState(
            cards=(_card(broker_quantity=10),),
            execution_orders=(order,),
            capital_reservations=(_reservation(),),
        ),
    )
    updated = plan.order_updates[0]
    assert updated.absence_count == 1
    assert updated.recovery_state == OrderRecoveryState.AWAITING_CANCEL_CONFIRMATION


def test_second_absence_from_same_generation_does_not_count():
    order = _order(
        recovery_state=OrderRecoveryState.AWAITING_CANCEL_CONFIRMATION,
        absence_count=1,
        last_absence_snapshot_id="snapshot-1",
        last_absence_observed_at=NOW.isoformat(),
        last_absence_session_date=NOW.date().isoformat(),
        last_absence_broker_order_id="B-1",
        last_absence_holding_quantity=10,
    )
    plan = reduce_account_reconciliation(
        _snapshot(holdings=(_holding(),)),
        AccountLocalState(execution_orders=(order,), capital_reservations=(_reservation(),)),
    )
    assert plan.order_updates == ()
    assert order.absence_count == 1


def test_second_absence_within_minimum_interval_does_not_count():
    order = _order(
        recovery_state=OrderRecoveryState.AWAITING_CANCEL_CONFIRMATION,
        absence_count=1,
        last_absence_snapshot_id="snapshot-1",
        last_absence_observed_at=NOW.isoformat(),
        last_absence_session_date=NOW.date().isoformat(),
        last_absence_broker_order_id="B-1",
        last_absence_holding_quantity=10,
    )
    plan = reduce_account_reconciliation(
        _snapshot(
            snapshot_id="snapshot-2",
            observed_at=NOW + timedelta(seconds=30),
            holdings=(_holding(),),
        ),
        AccountLocalState(execution_orders=(order,), capital_reservations=(_reservation(),)),
    )
    assert plan.order_updates == ()


def test_second_qualifying_absence_with_fresh_holdings_and_no_contradiction_resolves_terminal():
    order = _order(
        recovery_state=OrderRecoveryState.AWAITING_CANCEL_CONFIRMATION,
        absence_count=1,
        last_absence_snapshot_id="snapshot-1",
        last_absence_observed_at=NOW.isoformat(),
        last_absence_session_date=NOW.date().isoformat(),
        last_absence_broker_order_id="B-1",
        last_absence_holding_quantity=10,
    )
    plan = reduce_account_reconciliation(
        _snapshot(
            snapshot_id="snapshot-2",
            observed_at=NOW + timedelta(seconds=61),
            holdings=(_holding(),),
        ),
        AccountLocalState(execution_orders=(order,), capital_reservations=(_reservation(),)),
    )
    updated = plan.order_updates[0]
    assert updated.absence_count == 2
    assert updated.recovery_state == OrderRecoveryState.TERMINAL_RECONCILED


def test_contradictory_evidence_between_absences_resets_the_counter():
    order = _order(
        recovery_state=OrderRecoveryState.AWAITING_CANCEL_CONFIRMATION,
        absence_count=1,
        last_absence_snapshot_id="snapshot-1",
        last_absence_observed_at=NOW.isoformat(),
        last_absence_session_date=NOW.date().isoformat(),
        last_absence_broker_order_id="B-1",
        last_absence_holding_quantity=10,
    )
    plan = reduce_account_reconciliation(
        _snapshot(
            snapshot_id="snapshot-2",
            observed_at=NOW + timedelta(seconds=61),
            holdings=(_holding(9),),
        ),
        AccountLocalState(execution_orders=(order,), capital_reservations=(_reservation(),)),
    )
    assert plan.order_updates[0].absence_count == 0
    assert any(alert.code == "ORDER_ABSENCE_CONTRADICTION_RESET" for alert in plan.alerts)


def test_a_broker_order_used_as_an_a4a_candidate_is_not_also_created_as_an_external_order():
    ambiguous = _order(
        broker_order_id="",
        status=ExecutionOrderStatus.UNKNOWN_SUBMISSION_STATE,
        broker_identity_status=BrokerIdentityStatus.AMBIGUOUS,
        recovery_state=OrderRecoveryState.DISCOVERING,
        submission_started_at=NOW.isoformat(),
    )
    candidate = BrokerOrderStatusSnapshot(
        environment="PROD",
        account_no="1",
        symbol="AAPL",
        broker_order_id="B-CANDIDATE",
        side=OrderSide.BUY,
        status=OrderStatus.WORKING,
        quantity_requested=10,
        limit_price=100.0,
        submitted_at=NOW.isoformat(),
    )
    plan = reduce_account_reconciliation(
        _snapshot(orders=(candidate,)),
        AccountLocalState(execution_orders=(ambiguous,), capital_reservations=(_reservation(),)),
    )
    assert plan.external_order_creates == ()
    assert plan.order_updates[0].broker_identity_status == BrokerIdentityStatus.AMBIGUOUS
    assert plan.order_updates[0].recovery_state == OrderRecoveryState.BROKER_IDENTITY_UNCERTAIN
    assert plan.commands == ()


def test_a4a_candidate_requires_a_real_submission_time_not_query_time():
    ambiguous = _order(
        broker_order_id="",
        status=ExecutionOrderStatus.UNKNOWN_SUBMISSION_STATE,
        broker_identity_status=BrokerIdentityStatus.AMBIGUOUS,
        submission_started_at=NOW.isoformat(),
    )
    no_broker_submission_time = BrokerOrderStatusSnapshot(
        environment="PROD",
        account_no="1",
        symbol="AAPL",
        broker_order_id="B-UNVERIFIED-TIME",
        side=OrderSide.BUY,
        status=OrderStatus.WORKING,
        quantity_requested=10,
        limit_price=100.0,
        checked_at=(NOW + timedelta(seconds=1)).isoformat(),
    )
    plan = reduce_account_reconciliation(
        _snapshot(orders=(no_broker_submission_time,)),
        AccountLocalState(execution_orders=(ambiguous,)),
    )
    assert len(plan.external_order_creates) == 1
    assert plan.order_updates[0].recovery_state == OrderRecoveryState.MANUAL_INTERVENTION_REQUIRED
    assert not plan.commands


def test_a4b_creates_a_discovered_external_order_not_an_execution_order_record():
    external_snapshot = BrokerOrderStatusSnapshot(
        environment="PROD",
        account_no="1",
        symbol="TSLA",
        broker_order_id="B-EXTERNAL",
        side=OrderSide.BUY,
        status=OrderStatus.WORKING,
        quantity_requested=2,
        limit_price=250.0,
    )
    plan = reduce_account_reconciliation(
        _snapshot(orders=(external_snapshot,)), AccountLocalState()
    )
    assert plan.order_updates == ()
    assert len(plan.external_order_creates) == 1
    assert plan.external_order_creates[0].disposition == ExternalOrderDisposition.DISCOVERED_UNOWNED
    assert plan.commands == ()


def test_first_sighting_of_terminal_external_history_is_audit_only():
    historical = BrokerOrderStatusSnapshot(
        environment="PROD",
        account_no="1",
        symbol="TSLA",
        broker_order_id="B-HISTORICAL",
        side=OrderSide.BUY,
        status=OrderStatus.FILLED,
        quantity_requested=2,
        filled_quantity=2,
    )

    plan = reduce_account_reconciliation(
        _snapshot(orders=(historical,)), AccountLocalState()
    )

    assert len(plan.external_order_creates) == 1
    assert (
        plan.external_order_creates[0].disposition
        == ExternalOrderDisposition.DISMISSED_TERMINAL
    )
    assert not any(
        alert.code == "DISCOVERED_UNOWNED_BROKER_ORDER"
        for alert in plan.alerts
    )


def test_unrecognized_open_external_order_still_creates_a_durable_fence():
    external_snapshot = BrokerOrderStatusSnapshot(
        environment="PROD",
        account_no="1",
        symbol="AAPL",
        broker_order_id="B-UNKNOWN-EXTERNAL",
        side=OrderSide.SELL,
        status=OrderStatus.UNKNOWN,
        quantity_requested=4,
    )
    plan = reduce_account_reconciliation(
        _snapshot(orders=(external_snapshot,), holdings=(_holding(),)),
        AccountLocalState(
            cards=(
                _card(
                    board_status=BoardStatus.SELL_ALL,
                    broker_quantity=10,
                    exit_all_required=True,
                ),
            )
        ),
    )
    assert len(plan.external_order_creates) == 1
    assert plan.external_order_creates[0].broker_status == (
        ExecutionOrderStatus.UNKNOWN_SUBMISSION_STATE
    )
    assert plan.commands == ()
    assert any(
        alert.code == "UNRECOGNIZED_BROKER_ORDER_STATUS"
        for alert in plan.alerts
    )


def test_concurrent_a4b_create_plan_is_idempotent_at_the_unique_broker_identity(tmp_path):
    broker_order = BrokerOrderStatusSnapshot(
        environment="PROD",
        account_no="1",
        symbol="MSFT",
        broker_order_id="B-CONCURRENT",
        side=OrderSide.BUY,
        status=OrderStatus.WORKING,
    )
    plan = reduce_account_reconciliation(
        _snapshot(orders=(broker_order,)), AccountLocalState()
    )
    engine = create_engine(f"sqlite:///{tmp_path / 'external.db'}", future=True)

    apply_reconciliation_plan(engine, plan)
    apply_reconciliation_plan(engine, plan)

    from src.services.discovered_external_order_repository import (
        list_discovered_external_orders_for_account,
    )

    stored = list_discovered_external_orders_for_account(
        engine, environment="PROD", account_no="1"
    )
    assert len(stored) == 1


@pytest.mark.parametrize(
    ("order", "card", "expected"),
    [
        (_order(), _card(), ReconciliationCategory.ENTRY_BUY),
        (
            _order(client_order_id="C-2"),
            _card(board_status=BoardStatus.OPEN_POSITION, entry_remaining_target_quantity=5),
            ReconciliationCategory.ENTRY_COMPLETION_BUY,
        ),
        (
            _order(side=OrderSide.SELL, intent=OrderIntent.PARTIAL_EXIT, client_order_id="C-3"),
            _card(board_status=BoardStatus.PARTIAL_SELL),
            ReconciliationCategory.PARTIAL_SELL,
        ),
        (
            _order(side=OrderSide.SELL, intent=OrderIntent.MANUAL_EXIT, client_order_id="C-4"),
            _card(board_status=BoardStatus.SELL_ALL),
            ReconciliationCategory.SELL_ALL,
        ),
        (
            _order(side=OrderSide.SELL, intent=OrderIntent.STOP_LOSS, client_order_id="C-5"),
            _card(board_status=BoardStatus.SELL_ALL),
            ReconciliationCategory.STOP_LOSS_SELL,
        ),
        (
            _order(
                side=OrderSide.SELL,
                intent=OrderIntent.MANUAL_EXIT,
                execution_policy=RESERVED_MOO_EXECUTION,
                client_order_id="C-6",
            ),
            _card(board_status=BoardStatus.SELL_ALL),
            ReconciliationCategory.RESERVED_MOO_SELL,
        ),
        (
            _order(
                broker_order_id="",
                status=ExecutionOrderStatus.UNKNOWN_SUBMISSION_STATE,
                broker_identity_status=BrokerIdentityStatus.AMBIGUOUS,
                client_order_id="C-7",
            ),
            _card(),
            ReconciliationCategory.UNKNOWN_SUBMISSION,
        ),
        (
            _order(status=ExecutionOrderStatus.FILLED, filled_quantity=10, remaining_quantity=0, client_order_id="C-8"),
            _card(),
            ReconciliationCategory.TERMINAL_ORDER,
        ),
    ],
)
def test_every_local_order_category_has_an_explicit_branch(order, card, expected):
    assert classify_execution_order(order, card) == expected


def test_manual_position_orphan_reservation_and_live_order_without_reservation_are_explicit():
    live_without_reservation = _order(capital_reservation_id="")
    orphan = _reservation("R-ORPHAN", "MSFT")
    plan = reduce_account_reconciliation(
        _snapshot(holdings=(_holding(symbol="TSLA"),)),
        AccountLocalState(
            execution_orders=(live_without_reservation,),
            capital_reservations=(orphan,),
        ),
    )
    categories = {item.category for item in plan.classifications}
    assert ReconciliationCategory.MANUAL_BROKER_POSITION in categories
    assert ReconciliationCategory.ORPHAN_CAPITAL_RESERVATION in categories
    assert ReconciliationCategory.LIVE_ORDER_WITHOUT_RESERVATION in categories
    assert plan.reservation_updates[0].absence_count == 1
    assert plan.reservation_updates[0].is_open() is True
    assert any(
        alert.code == "ORPHAN_CAPITAL_RESERVATION_REQUIRES_REVIEW"
        for alert in plan.alerts
    )


def test_orphan_reservation_releases_only_after_two_complete_generations():
    orphan = _reservation("R-ORPHAN", "MSFT")
    orphan.absence_count = 1
    orphan.last_absence_snapshot_id = "snapshot-1"
    orphan.last_absence_observed_at = NOW.isoformat()
    orphan.last_absence_session_date = NOW.date().isoformat()

    plan = reduce_account_reconciliation(
        _snapshot(
            snapshot_id="snapshot-2",
            observed_at=NOW + timedelta(seconds=61),
        ),
        AccountLocalState(capital_reservations=(orphan,)),
    )

    repaired = plan.reservation_updates[0]
    assert repaired.absence_count == 2
    assert repaired.status.value == "RELEASED"
    assert repaired.released_at == NOW + timedelta(seconds=61)


def test_orphan_reservation_does_not_advance_on_incomplete_broker_evidence():
    orphan = _reservation("R-ORPHAN", "MSFT")
    plan = reduce_account_reconciliation(
        _snapshot(completeness=_completeness(history_complete=False)),
        AccountLocalState(capital_reservations=(orphan,)),
    )
    assert plan.reservation_updates == ()


def test_discovered_external_order_is_dismissed_only_after_terminal_broker_evidence():
    external = new_discovered_external_order(
        environment="PROD",
        account_no="1",
        symbol="TSLA",
        side=OrderSide.BUY,
        broker_order_id="B-EXT",
        broker_status=ExecutionOrderStatus.WORKING,
    )
    terminal = BrokerOrderStatusSnapshot(
        environment="PROD",
        account_no="1",
        symbol="TSLA",
        broker_order_id="B-EXT",
        side=OrderSide.BUY,
        status=OrderStatus.CANCELLED,
    )
    plan = reduce_account_reconciliation(
        _snapshot(orders=(terminal,)), AccountLocalState(external_orders=(external,))
    )
    assert plan.external_order_updates[0].disposition == ExternalOrderDisposition.DISMISSED_TERMINAL


# C5 ports the old ordered-sweep regressions to the account reducer.  Exact
# durable identity is now the mutation boundary; heuristic broker matches
# are intentionally alert-only, which is stricter than the deleted sweeps.


def test_exact_entry_fill_applies_current_holding_and_stop_in_one_reducer_pass():
    broker_order = BrokerOrderStatusSnapshot(
        environment="PROD",
        account_no="1",
        symbol="AAPL",
        broker_order_id="B-1",
        side=OrderSide.BUY,
        status=OrderStatus.FILLED,
        quantity_requested=10,
        filled_quantity=10,
        avg_fill_price=101.0,
    )
    plan = reduce_account_reconciliation(
        _snapshot(orders=(broker_order,), holdings=(_holding(7),)),
        AccountLocalState(
            cards=(_card(),),
            execution_orders=(_order(),),
            capital_reservations=(_reservation(),),
        ),
    )
    card = plan.card_updates[0]
    order = plan.order_updates[0]
    assert card.board_status == BoardStatus.OPEN_POSITION
    assert card.broker_quantity == 7
    assert card.stop_type is not None
    assert order.status == ExecutionOrderStatus.FILLED


def test_exact_entry_cancellation_with_zero_fill_returns_card_to_buylist():
    cancelled = BrokerOrderStatusSnapshot(
        environment="PROD",
        account_no="1",
        symbol="AAPL",
        broker_order_id="B-1",
        side=OrderSide.BUY,
        status=OrderStatus.CANCELLED,
        quantity_requested=10,
    )
    plan = reduce_account_reconciliation(
        _snapshot(orders=(cancelled,)),
        AccountLocalState(
            cards=(_card(),),
            execution_orders=(_order(),),
            capital_reservations=(_reservation(),),
        ),
    )
    assert plan.card_updates[0].board_status == BoardStatus.BUYLIST
    assert plan.order_updates[0].status == ExecutionOrderStatus.CANCELLED


def test_old_exact_terminal_order_does_not_project_onto_current_buylist_cycle():
    current_card = _card(
        board_status=BoardStatus.BUYLIST,
        entry_attempt_group_id="CURRENT-CYCLE",
        entry_client_order_id="",
    )
    historical_order = _order(
        client_order_id="OLD-CLIENT-ID",
        attempt_group_id="PRIOR-CYCLE",
        capital_reservation_id="",
    )
    historical_fill = BrokerOrderStatusSnapshot(
        environment="PROD",
        account_no="1",
        symbol="AAPL",
        broker_order_id="B-1",
        side=OrderSide.BUY,
        status=OrderStatus.FILLED,
        quantity_requested=10,
        filled_quantity=10,
        avg_fill_price=101.0,
    )

    plan = reduce_account_reconciliation(
        _snapshot(orders=(historical_fill,)),
        AccountLocalState(
            cards=(current_card,), execution_orders=(historical_order,)
        ),
    )

    assert plan.order_updates[0].status == ExecutionOrderStatus.FILLED
    assert plan.card_updates == ()


def test_exact_working_entry_remains_tracked_without_a_cancel_command():
    working = BrokerOrderStatusSnapshot(
        environment="PROD",
        account_no="1",
        symbol="AAPL",
        broker_order_id="B-1",
        side=OrderSide.BUY,
        status=OrderStatus.WORKING,
        quantity_requested=10,
        remaining_quantity=10,
    )
    plan = reduce_account_reconciliation(
        _snapshot(orders=(working,)),
        AccountLocalState(
            cards=(_card(),),
            execution_orders=(_order(),),
            capital_reservations=(_reservation(),),
        ),
    )
    assert not plan.commands
    assert all(card.board_status == BoardStatus.ENTRY_PENDING for card in plan.card_updates)


def test_incomplete_order_discovery_never_resolves_a_missing_order():
    card = _card()
    order = _order()
    plan = reduce_account_reconciliation(
        _snapshot(
            completeness=_completeness(
                open_orders_complete=False,
                history_complete=False,
            )
        ),
        AccountLocalState(
            cards=(card,),
            execution_orders=(order,),
            capital_reservations=(_reservation(),),
        ),
    )
    assert not plan.card_updates
    assert not plan.order_updates


def test_durable_open_entry_moves_a_stale_buy_today_card_into_tracking_scope():
    card = _card(board_status=BoardStatus.BUY_TODAY)
    plan = reduce_account_reconciliation(
        _snapshot(
            completeness=_completeness(
                open_orders_complete=False,
                history_complete=False,
            )
        ),
        AccountLocalState(
            cards=(card,),
            execution_orders=(_order(),),
            capital_reservations=(_reservation(),),
        ),
    )
    assert plan.card_updates[0].board_status == BoardStatus.ENTRY_PENDING


def test_untracked_working_order_is_alert_only_even_when_it_matches_a_card():
    external = BrokerOrderStatusSnapshot(
        environment="PROD",
        account_no="1",
        symbol="AAPL",
        broker_order_id="B-EXTERNAL",
        side=OrderSide.BUY,
        status=OrderStatus.WORKING,
        quantity_requested=10,
        remaining_quantity=10,
        limit_price=100.0,
    )
    card = _card()
    plan = reduce_account_reconciliation(
        _snapshot(orders=(external,)), AccountLocalState(cards=(card,))
    )
    assert plan.external_order_creates
    assert not plan.commands
    assert not plan.card_updates


def test_one_ambiguous_local_submission_can_consume_only_one_broker_candidate():
    ambiguous = _order(
        broker_order_id="",
        broker_identity_status=BrokerIdentityStatus.AMBIGUOUS,
        status=ExecutionOrderStatus.UNKNOWN_SUBMISSION_STATE,
        capital_reservation_id="",
        submission_started_at=NOW.isoformat(),
    )
    candidates = tuple(
        BrokerOrderStatusSnapshot(
            environment="PROD",
            account_no="1",
            symbol="AAPL",
            broker_order_id=broker_id,
            side=OrderSide.BUY,
            status=OrderStatus.WORKING,
            quantity_requested=10,
            limit_price=100.0,
            submitted_at=NOW.isoformat(),
        )
        for broker_id in ("B-CANDIDATE-1", "B-CANDIDATE-2")
    )
    plan = reduce_account_reconciliation(
        _snapshot(orders=candidates),
        AccountLocalState(cards=(_card(),), execution_orders=(ambiguous,)),
    )
    assert len(plan.external_order_creates) == 1
    assert not plan.commands


def test_existing_card_with_broker_holding_is_recovered_without_order_sweep():
    plan = reduce_account_reconciliation(
        _snapshot(holdings=(_holding(10),)),
        AccountLocalState(cards=(_card(board_status=BoardStatus.WATCHLIST),)),
    )
    recovered = plan.card_updates[0]
    assert recovered.board_status == BoardStatus.OPEN_POSITION
    assert recovered.broker_quantity == 10
    assert "STOP_REQUIRED" in recovered.warnings


def test_stale_entry_pending_without_a_durable_order_returns_to_buylist_only_on_complete_absence():
    plan = reduce_account_reconciliation(
        _snapshot(), AccountLocalState(cards=(_card(),))
    )
    assert plan.card_updates[0].board_status == BoardStatus.BUYLIST


def test_live_exit_order_does_not_require_an_entry_capital_reservation():
    exit_order = _order(
        side=OrderSide.SELL,
        intent=OrderIntent.PARTIAL_EXIT,
        client_order_id="C-EXIT",
        capital_reservation_id="",
    )
    plan = reduce_account_reconciliation(
        _snapshot(
            completeness=_completeness(
                open_orders_complete=False,
                history_complete=False,
            )
        ),
        AccountLocalState(
            cards=(_card(board_status=BoardStatus.PARTIAL_SELL),),
            execution_orders=(exit_order,),
        ),
    )
    assert ReconciliationCategory.LIVE_ORDER_WITHOUT_RESERVATION not in {
        item.category for item in plan.classifications
    }


def test_unrecognized_local_order_combination_emits_a_critical_alert():
    unrecognized = _order(
        side=OrderSide.BUY,
        intent=OrderIntent.MANUAL_EXIT,
        client_order_id="C-UNKNOWN",
    )
    plan = reduce_account_reconciliation(
        _snapshot(
            completeness=_completeness(
                open_orders_complete=False,
                history_complete=False,
            )
        ),
        AccountLocalState(execution_orders=(unrecognized,)),
    )
    assert plan.classifications[0].category == ReconciliationCategory.UNRECOGNIZED
    assert plan.alerts[0].code == "UNRECOGNIZED_LOCAL_ORDER_COMBINATION"


def test_absence_generation_persists_across_account_passes(tmp_path):
    from src.services import execution_order_repository

    engine = create_engine(f"sqlite:///{tmp_path / 'reconciliation.db'}", future=True)
    execution_order_repository.record_execution_order(
        engine,
        _order(
            status=ExecutionOrderStatus.CANCEL_PENDING,
            recovery_state=OrderRecoveryState.AWAITING_CANCEL_CONFIRMATION,
        ),
    )

    class Broker:
        def get_positions(self, **kwargs):
            return {"overseas": {"holdings": []}}

        def discover_orders(self, **kwargs):
            return BrokerOrderDiscoveryResult(
                open_orders_complete=True,
                history_complete=True,
                reserved_orders_complete=True,
            )

    broker = Broker()
    first = run_account_reconciliation_pass(
        broker=broker,
        engine=engine,
        environment="PROD",
        account_no="1",
        cards=(),
        account_balance_provider=lambda environment, account_no: 100_000,
        clock=lambda: NOW,
    )
    assert first.plan.order_updates[0].absence_count == 1

    second = run_account_reconciliation_pass(
        broker=broker,
        engine=engine,
        environment="PROD",
        account_no="1",
        cards=(),
        account_balance_provider=lambda environment, account_no: 100_000,
        clock=lambda: NOW + timedelta(seconds=61),
    )
    assert second.plan.order_updates[0].absence_count == 2
    stored = execution_order_repository.fetch_execution_order(engine, "C-1")
    assert stored.recovery_state == OrderRecoveryState.TERMINAL_RECONCILED


def test_working_partial_entry_restores_remaining_target_and_completion_state():
    broker_order = BrokerOrderStatusSnapshot(
        environment="PROD",
        account_no="1",
        symbol="AAPL",
        broker_order_id="B-1",
        side=OrderSide.BUY,
        status=OrderStatus.PARTIALLY_FILLED,
        quantity_requested=10,
        filled_quantity=3,
        remaining_quantity=7,
        avg_fill_price=101.0,
    )
    plan = reduce_account_reconciliation(
        _snapshot(orders=(broker_order,), holdings=(_holding(3),)),
        AccountLocalState(
            cards=(_card(),),
            execution_orders=(_order(),),
            capital_reservations=(_reservation(),),
        ),
    )

    card = plan.card_updates[0]
    assert card.board_status == BoardStatus.OPEN_POSITION
    assert card.entry_remaining_target_quantity == 7
    assert card.position_runtime_status == PositionRuntimeStatus.ENTRY_COMPLETING
    assert card.entry_runtime_status is not None
    assert plan.reservation_updates[0].remaining_reserved_notional == pytest.approx(697.0)


def test_filled_partial_exit_projects_breakeven_and_clears_pending_state():
    card = _card(
        board_status=BoardStatus.PARTIAL_SELL,
        broker_quantity=10,
        orderable_quantity=6,
        average_entry_price=100.0,
        pending_partial_sell_quantity=4,
        exit_client_order_id="SELL-1",
    )
    order = _order(
        side=OrderSide.SELL,
        intent=OrderIntent.PARTIAL_EXIT,
        client_order_id="SELL-1",
        submitted_quantity=4,
        remaining_quantity=4,
        capital_reservation_id="",
    )
    broker_order = BrokerOrderStatusSnapshot(
        environment="PROD",
        account_no="1",
        symbol="AAPL",
        broker_order_id="B-1",
        side=OrderSide.SELL,
        status=OrderStatus.FILLED,
        quantity_requested=4,
        filled_quantity=4,
        remaining_quantity=0,
        avg_fill_price=110.0,
    )

    plan = reduce_account_reconciliation(
        _snapshot(orders=(broker_order,), holdings=(_holding(6),)),
        AccountLocalState(cards=(card,), execution_orders=(order,)),
    )

    updated = plan.card_updates[0]
    assert updated.board_status == BoardStatus.OPEN_POSITION
    assert updated.pending_partial_sell_quantity == 0
    assert updated.exit_client_order_id == ""
    assert updated.stop_type == StopType.BREAKEVEN
    assert updated.stop_quantity == 6


@pytest.mark.parametrize(
    ("intent", "execution_policy", "expected_runtime", "queued"),
    [
        (OrderIntent.MANUAL_EXIT, "REGULAR_LIMIT", PositionRuntimeStatus.LIQUIDATING, False),
        (OrderIntent.STOP_LOSS, "REGULAR_LIMIT", PositionRuntimeStatus.LIQUIDATING, False),
        (OrderIntent.MANUAL_EXIT, RESERVED_MOO_EXECUTION, PositionRuntimeStatus.QUEUED_FOR_OPEN, True),
    ],
)
def test_working_liquidation_categories_project_explicit_card_state(
    intent, execution_policy, expected_runtime, queued
):
    card = _card(
        board_status=BoardStatus.SELL_ALL,
        broker_quantity=10,
        exit_all_required=True,
    )
    order = _order(
        side=OrderSide.SELL,
        intent=intent,
        client_order_id="SELL-ALL-1",
        execution_policy=execution_policy,
        capital_reservation_id="",
    )
    broker_order = BrokerOrderStatusSnapshot(
        environment="PROD",
        account_no="1",
        symbol="AAPL",
        broker_order_id="B-1",
        side=OrderSide.SELL,
        status=OrderStatus.WORKING,
        quantity_requested=10,
        remaining_quantity=10,
    )

    plan = reduce_account_reconciliation(
        _snapshot(orders=(broker_order,), holdings=(_holding(),)),
        AccountLocalState(cards=(card,), execution_orders=(order,)),
    )

    updated = plan.card_updates[0]
    assert updated.position_runtime_status == expected_runtime
    assert updated.sell_all_at_market_open is queued
    assert updated.exit_client_order_id == "SELL-ALL-1"


def test_terminal_sell_all_with_confirmed_zero_holding_closes_card():
    card = _card(
        board_status=BoardStatus.SELL_ALL,
        broker_quantity=10,
        exit_all_required=True,
        exit_client_order_id="SELL-ALL-1",
    )
    order = _order(
        side=OrderSide.SELL,
        intent=OrderIntent.MANUAL_EXIT,
        client_order_id="SELL-ALL-1",
        capital_reservation_id="",
    )
    terminal = BrokerOrderStatusSnapshot(
        environment="PROD",
        account_no="1",
        symbol="AAPL",
        broker_order_id="B-1",
        side=OrderSide.SELL,
        status=OrderStatus.FILLED,
        quantity_requested=10,
        filled_quantity=10,
    )

    plan = reduce_account_reconciliation(
        _snapshot(orders=(terminal,), holdings=()),
        AccountLocalState(cards=(card,), execution_orders=(order,)),
    )

    assert plan.card_updates[0].board_status == BoardStatus.CLOSED
    assert plan.card_updates[0].exit_all_required is False


@pytest.mark.parametrize(
    ("intent", "execution_policy", "alert_code"),
    [
        (OrderIntent.PARTIAL_EXIT, "REGULAR_LIMIT", "PARTIAL_EXIT_TERMINAL_WITHOUT_HOLDINGS"),
        (OrderIntent.MANUAL_EXIT, "REGULAR_LIMIT", "LIQUIDATION_TERMINAL_WITHOUT_HOLDINGS"),
        (OrderIntent.STOP_LOSS, "REGULAR_LIMIT", "LIQUIDATION_TERMINAL_WITHOUT_HOLDINGS"),
        (OrderIntent.MANUAL_EXIT, RESERVED_MOO_EXECUTION, "LIQUIDATION_TERMINAL_WITHOUT_HOLDINGS"),
    ],
)
def test_terminal_exit_categories_fail_closed_without_fresh_holdings(
    intent, execution_policy, alert_code
):
    board_status = (
        BoardStatus.PARTIAL_SELL
        if intent == OrderIntent.PARTIAL_EXIT
        else BoardStatus.SELL_ALL
    )
    card = _card(
        board_status=board_status,
        broker_quantity=10,
        pending_partial_sell_quantity=(4 if board_status == BoardStatus.PARTIAL_SELL else 0),
        exit_all_required=(board_status == BoardStatus.SELL_ALL),
    )
    order = _order(
        side=OrderSide.SELL,
        intent=intent,
        execution_policy=execution_policy,
        submitted_quantity=4,
        remaining_quantity=4,
        capital_reservation_id="",
    )
    terminal = BrokerOrderStatusSnapshot(
        environment="PROD",
        account_no="1",
        symbol="AAPL",
        broker_order_id="B-1",
        side=OrderSide.SELL,
        status=OrderStatus.FILLED,
        quantity_requested=4,
        filled_quantity=4,
    )

    plan = reduce_account_reconciliation(
        _snapshot(
            orders=(terminal,),
            holdings=(),
            completeness=_completeness(holdings_complete=False),
        ),
        AccountLocalState(cards=(card,), execution_orders=(order,)),
    )

    assert any(alert.code == alert_code for alert in plan.alerts)
    assert plan.card_updates[0].exit_submission_unresolved is True


@pytest.mark.parametrize(
    ("side", "intent", "execution_policy"),
    [
        (OrderSide.BUY, OrderIntent.ENTRY, "REGULAR_LIMIT"),
        (OrderSide.SELL, OrderIntent.PARTIAL_EXIT, "REGULAR_LIMIT"),
        (OrderSide.SELL, OrderIntent.MANUAL_EXIT, "REGULAR_LIMIT"),
        (OrderSide.SELL, OrderIntent.STOP_LOSS, "REGULAR_LIMIT"),
        (OrderSide.SELL, OrderIntent.MANUAL_EXIT, RESERVED_MOO_EXECUTION),
    ],
)
def test_every_terminal_c4_category_settles_its_linked_reservation(
    side, intent, execution_policy
):
    reservation = CapitalReservation(
        reservation_id="R-C4",
        environment="PROD",
        account_no="1",
        symbol="AAPL",
        attempt_group_id="G-C4",
        requested_notional=(1000.0 if side == OrderSide.BUY else 0.0),
        remaining_reserved_notional=(1000.0 if side == OrderSide.BUY else 0.0),
    )
    order = _order(
        side=side,
        intent=intent,
        execution_policy=execution_policy,
        capital_reservation_id="R-C4",
    )
    terminal = BrokerOrderStatusSnapshot(
        environment="PROD",
        account_no="1",
        symbol="AAPL",
        broker_order_id="B-1",
        side=side,
        status=OrderStatus.CANCELLED,
        quantity_requested=10,
    )

    plan = reduce_account_reconciliation(
        _snapshot(orders=(terminal,)),
        AccountLocalState(
            execution_orders=(order,), capital_reservations=(reservation,)
        ),
    )

    assert plan.reservation_updates[0].status.value == "RELEASED"
    assert plan.reservation_updates[0].remaining_reserved_notional == 0


def test_exact_terminal_disagreement_always_escalates():
    local = _order(
        status=ExecutionOrderStatus.FILLED,
        filled_quantity=10,
        remaining_quantity=0,
        recovery_state=OrderRecoveryState.TERMINAL_RECONCILED,
    )
    contradictory = BrokerOrderStatusSnapshot(
        environment="PROD",
        account_no="1",
        symbol="AAPL",
        broker_order_id="B-1",
        side=OrderSide.BUY,
        status=OrderStatus.CANCELLED,
        quantity_requested=10,
    )

    plan = reduce_account_reconciliation(
        _snapshot(orders=(contradictory,), holdings=(_holding(),)),
        AccountLocalState(cards=(_card(),), execution_orders=(local,)),
    )

    assert any(alert.code == "BROKER_STATUS_CONTRADICTION" for alert in plan.alerts)
    assert plan.order_updates[0].recovery_state == OrderRecoveryState.MANUAL_INTERVENTION_REQUIRED


def test_first_regular_absence_requires_complete_holdings_evidence():
    plan = reduce_account_reconciliation(
        _snapshot(
            completeness=_completeness(holdings_complete=False),
            holdings=(),
        ),
        AccountLocalState(execution_orders=(_order(),)),
    )
    assert plan.order_updates == ()


def test_reserved_moo_absence_requires_reserved_order_completeness():
    reserved = _order(
        side=OrderSide.SELL,
        intent=OrderIntent.MANUAL_EXIT,
        execution_policy=RESERVED_MOO_EXECUTION,
        capital_reservation_id="",
    )
    plan = reduce_account_reconciliation(
        _snapshot(
            completeness=_completeness(reserved_orders_complete=False),
            holdings=(_holding(),),
        ),
        AccountLocalState(execution_orders=(reserved,)),
    )
    assert plan.order_updates == ()


def test_snapshot_session_date_uses_us_market_calendar_not_utc_date():
    class Broker:
        def get_positions(self, **kwargs):
            return {"overseas": {"holdings": []}}

        def discover_orders(self, **kwargs):
            return BrokerOrderDiscoveryResult(
                open_orders_complete=True,
                history_complete=True,
                reserved_orders_complete=True,
            )

    snapshot = fetch_account_broker_snapshot(
        broker=Broker(),
        environment="PROD",
        account_no="1",
        account_balance_provider=lambda *_: 1000.0,
        clock=lambda: datetime(2026, 8, 18, 1, 0, tzinfo=timezone.utc),
    )
    assert snapshot.session_date == date(2026, 8, 17)


def test_external_working_sell_blocks_emergency_submission_even_with_sellable_quantity():
    external_sell = BrokerOrderStatusSnapshot(
        environment="PROD",
        account_no="1",
        symbol="AAPL",
        broker_order_id="B-EXTERNAL-SELL",
        side=OrderSide.SELL,
        status=OrderStatus.WORKING,
        quantity_requested=40,
        remaining_quantity=40,
    )
    decision = decide_emergency_sell(
        _snapshot(orders=(external_sell,), holdings=(_holding(100, sellable_quantity=60),)),
        symbol="AAPL",
        execution_orders=(),
    )
    assert decision.quantity == 0
    assert decision.manual_intervention_required is True
    assert "sellable quantity is 60" in decision.reason


def test_atomic_plan_application_rolls_back_earlier_writes_on_later_failure(
    tmp_path, monkeypatch
):
    from src.services import discovered_external_order_repository as external_repo
    from src.services import trade_card_repository

    engine = create_engine(f"sqlite:///{tmp_path / 'atomic-plan.db'}", future=True)
    original = trade_card_repository.create_trade_card(
        engine,
        _card(board_status=BoardStatus.BUYLIST),
    )
    updated = deepcopy(original)
    updated.board_status = BoardStatus.BUY_TODAY
    external = new_discovered_external_order(
        environment="PROD",
        account_no="1",
        symbol="MSFT",
        side=OrderSide.BUY,
        broker_order_id="B-ATOMIC-FAIL",
        broker_status=ExecutionOrderStatus.WORKING,
    )
    plan = ReconciliationPlan(
        snapshot_id="atomic-plan",
        card_updates=(updated,),
        external_order_creates=(external,),
    )

    monkeypatch.setattr(
        external_repo,
        "insert_discovered_external_order",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("injected")),
    )
    with pytest.raises(RuntimeError, match="injected"):
        apply_reconciliation_plan(engine, plan)

    stored = trade_card_repository.get_trade_card(engine, "PROD", "1", "AAPL")
    assert stored.board_status == BoardStatus.BUYLIST
    assert stored.version == 1


def test_stale_reconciliation_plan_cannot_overwrite_newer_reservation_state(
    tmp_path,
):
    from src.services import capital_reservation_repository as reservation_repo
    from src.services.capital_reservation_repository import (
        CapitalReservationVersionConflictError,
    )

    engine = create_engine(f"sqlite:///{tmp_path / 'reservation-cas.db'}", future=True)
    reservation = _reservation("R-CAS")
    reservation_repo.save_reservation(engine, reservation)
    stale = reservation_repo.fetch_reservation(engine, "R-CAS")
    newer = reservation_repo.fetch_reservation(engine, "R-CAS")

    newer.release()
    with engine.begin() as conn:
        reservation_repo.update_reservation(
            conn, newer, expected_version=newer.version
        )

    stale.absence_count = 1
    stale.last_absence_snapshot_id = "stale-snapshot"
    plan = ReconciliationPlan(
        snapshot_id="stale-plan", reservation_updates=(stale,)
    )

    with pytest.raises(CapitalReservationVersionConflictError):
        apply_reconciliation_plan(engine, plan)

    stored = reservation_repo.fetch_reservation(engine, "R-CAS")
    assert stored.version == 2
    assert stored.status.value == "RELEASED"
    assert stored.absence_count == 0
