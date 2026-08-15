"""Tests for src.services.eod_trading_service."""
from __future__ import annotations

import pytest

from src.core.order_state import (
    BrokerOrder,
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
from src.services import capital_allocator
from src.services.eod_trading_service import (
    EodActionCallbacks,
    EodTradingService,
    run_startup_reconciliation,
)
from src.services.entry_attempt_manager import EntryAttemptManager
from src.services.position_manager import BrokerHolding, PositionManager


def _card(**overrides):
    fields = dict(environment="PROD", account_no="1", symbol="AAPL")
    fields.update(overrides)
    return TradeCardState(**fields)


def _order(*, status, filled=0, avg_fill_price=0.0, reservation_id=""):
    order = BrokerOrder.create(
        environment="PROD", account_no="1", symbol="AAPL", side=OrderSide.BUY,
        intent=OrderIntent.ENTRY, quantity_requested=10, limit_price=100.0,
        status=status, capital_reservation_id=reservation_id,
    )
    order.filled_quantity = filled
    order.avg_fill_price = avg_fill_price
    return order


def _snapshot(*, status, filled=0, avg_fill_price=0.0, symbol="AAPL", account_no="1", side=OrderSide.BUY):
    return BrokerOrderStatusSnapshot(
        environment="PROD", account_no=account_no, symbol=symbol, side=side,
        status=status, quantity_requested=10, filled_quantity=filled,
        avg_fill_price=avg_fill_price,
    )


def _service(tmp_path, *, find_order=None, reconcile_order=None, discover_all_orders=None):
    cancelled = []
    manager = EntryAttemptManager(
        buying_power_provider=lambda e, a: 100_000.0,
        submit_order=lambda **kw: None,
        reservations_path=tmp_path / "reservations.json",
    )
    callbacks = EodActionCallbacks(
        find_open_entry_order=find_order or (lambda card: None),
        reconcile_order=reconcile_order or (lambda order: order),
        cancel_order=cancelled.append,
        **(
            {"discover_all_orders": discover_all_orders}
            if discover_all_orders is not None
            else {}
        ),
    )
    service = EodTradingService(
        entry_attempt_manager=manager,
        position_manager=PositionManager(),
        callbacks=callbacks,
        reservations_path=tmp_path / "reservations.json",
    )
    return service, cancelled, manager


# --- Buy Today with no submitted order (section 512-516) -------------------


def test_buy_today_with_no_order_returns_to_buylist_and_clears_runtime_values(tmp_path):
    service, cancelled, manager = _service(tmp_path)
    card = _card(
        board_status=BoardStatus.BUY_TODAY,
        entry_runtime_status=EntryRuntimeStatus.ARMED,
        entry_orb_high=105.0,
        entry_orb_low=95.0,
        entry_trigger=106.0,
        capital_reservation_id="unused-res",
    )

    changed = service.run_eod_cleanup([card])

    assert changed == [card]
    assert card.board_status == BoardStatus.BUYLIST
    assert card.entry_runtime_status is None
    assert card.entry_orb_high is None
    assert card.entry_orb_low is None
    assert card.entry_trigger is None
    assert card.capital_reservation_id == ""
    assert cancelled == []  # nothing to cancel -- no order exists


def test_buy_today_with_a_working_order_is_left_for_the_entry_pending_branch(tmp_path):
    order = _order(status=OrderStatus.ACCEPTED)
    service, cancelled, _ = _service(tmp_path, find_order=lambda card: order)
    card = _card(board_status=BoardStatus.BUY_TODAY)

    changed = service.run_eod_cleanup([card])
    assert changed == []
    assert card.board_status == BoardStatus.BUY_TODAY


# --- Entry Pending with zero fills (section 517-521) ------------------------


def test_entry_pending_zero_fill_cancels_releases_capital_and_returns_to_buylist(tmp_path):
    """Review finding P0-7: EOD must not assume a requested cancellation is
    already complete -- it now drives the same two-phase
    request-then-confirm state machine the intraday heartbeat uses, so a
    still-working order only reaches AWAIT_CANCEL_CONFIRMATION on the first
    pass and only resolves to Buylist once a *later* pass sees the broker
    actually confirm CANCELLED.
    """
    reservation = capital_allocator.reserve_capital_for_entry(
        environment="PROD", account_no="1", symbol="AAPL", attempt_group_id="g1",
        requested_notional=1000.0, buying_power_provider=lambda: 10_000.0,
        path=tmp_path / "reservations.json",
    )
    order = _order(status=OrderStatus.ACCEPTED, filled=0, reservation_id=reservation.reservation_id)
    service, cancelled, _ = _service(tmp_path, find_order=lambda card: order, reconcile_order=lambda o: o)
    card = _card(board_status=BoardStatus.ENTRY_PENDING)

    changed = service.run_eod_cleanup([card])
    assert changed == [card]
    assert cancelled == [order.client_order_id]
    assert card.board_status == BoardStatus.ENTRY_PENDING  # not moved yet
    assert card.entry_cancel_in_flight is True
    stored = capital_allocator.load_reservations(tmp_path / "reservations.json")[0]
    assert stored.is_open()  # not released until the cancel is confirmed

    order.status = OrderStatus.CANCELLED  # broker confirms, asynchronously
    changed = service.run_eod_cleanup([card])
    assert changed == [card]
    assert card.board_status == BoardStatus.BUYLIST
    assert card.entry_cancel_in_flight is False
    stored = capital_allocator.load_reservations(tmp_path / "reservations.json")[0]
    assert not stored.is_open()


def test_entry_pending_zero_fill_already_cancelled_does_not_double_cancel(tmp_path):
    order = _order(status=OrderStatus.CANCELLED, filled=0)
    service, cancelled, _ = _service(tmp_path, find_order=lambda card: order)
    card = _card(board_status=BoardStatus.ENTRY_PENDING)

    service.run_eod_cleanup([card])
    assert cancelled == []
    assert card.board_status == BoardStatus.BUYLIST


# --- Entry Pending with any fill (section 522-525) --------------------------


def test_entry_pending_with_partial_fill_cancels_remainder_and_moves_to_open_position(tmp_path):
    """Review finding P0-7: the partial fill is not locked in as final
    until a *later* pass sees the broker actually confirm the cancel."""
    order = _order(status=OrderStatus.PARTIALLY_FILLED, filled=30, avg_fill_price=101.0)
    service, cancelled, _ = _service(tmp_path, find_order=lambda card: order, reconcile_order=lambda o: o)
    card = _card(board_status=BoardStatus.ENTRY_PENDING, target_position_quantity=100)
    card.entry_orb_low = 95.0
    card.entry_orb_window = "5m"

    changed = service.run_eod_cleanup([card])
    assert changed == [card]
    assert cancelled == [order.client_order_id]
    assert card.board_status == BoardStatus.ENTRY_PENDING  # not moved yet
    assert card.entry_cancel_in_flight is True

    order.status = OrderStatus.CANCELLED  # broker confirms, 30 shares locked in
    changed = service.run_eod_cleanup([card])
    assert changed == [card]
    assert card.board_status == BoardStatus.OPEN_POSITION
    assert card.broker_quantity == 30
    assert card.average_entry_price == 101.0
    assert card.entry_remaining_target_quantity == 0  # stop attempting completion
    assert card.stop_type == StopType.ORB_LOW
    assert card.active_stop_price == 95.0
    assert card.entry_cancel_in_flight is False


def test_entry_pending_fully_filled_moves_to_open_position_without_cancel(tmp_path):
    order = _order(status=OrderStatus.FILLED, filled=100, avg_fill_price=100.2)
    service, cancelled, _ = _service(tmp_path, find_order=lambda card: order, reconcile_order=lambda o: o)
    card = _card(board_status=BoardStatus.ENTRY_PENDING, target_position_quantity=100)

    service.run_eod_cleanup([card])
    assert cancelled == []  # already terminal, nothing to cancel
    assert card.board_status == BoardStatus.OPEN_POSITION
    assert card.broker_quantity == 100


# --- Unknown order at EOD (section 526-529) ---------------------------------


def test_unknown_submission_state_stays_entry_pending_reconciling(tmp_path):
    order = _order(status=OrderStatus.UNKNOWN_SUBMISSION_STATE)
    service, cancelled, _ = _service(tmp_path, find_order=lambda card: order, reconcile_order=lambda o: o)
    card = _card(board_status=BoardStatus.ENTRY_PENDING)

    changed = service.run_eod_cleanup([card])

    assert changed == [card]
    assert card.board_status == BoardStatus.ENTRY_PENDING  # never assumed cancelled/moved
    assert cancelled == []


# --- Open position with incomplete target (section 530-533) ----------------


def test_open_position_with_incomplete_target_cancels_remainder_and_keeps_position(tmp_path):
    order = _order(status=OrderStatus.ACCEPTED)
    service, cancelled, _ = _service(tmp_path, find_order=lambda card: order)
    card = _card(
        board_status=BoardStatus.OPEN_POSITION,
        broker_quantity=30,
        entry_remaining_target_quantity=70,
        position_runtime_status=PositionRuntimeStatus.ENTRY_COMPLETING,
    )

    changed = service.run_eod_cleanup([card])

    assert changed == [card]
    assert cancelled == [order.client_order_id]
    assert card.broker_quantity == 30  # position preserved
    assert card.entry_remaining_target_quantity == 0
    assert card.position_runtime_status == PositionRuntimeStatus.OPEN


def test_open_position_with_completed_target_is_untouched(tmp_path):
    service, cancelled, _ = _service(tmp_path)
    card = _card(
        board_status=BoardStatus.OPEN_POSITION, broker_quantity=100, entry_remaining_target_quantity=0
    )
    assert service.run_eod_cleanup([card]) == []
    assert cancelled == []


# --- Startup reconciliation (section 1028-1032, 1070-1075) -----------------


def test_startup_reconciliation_discovers_position_with_no_local_card():
    changed = run_startup_reconciliation(
        [],
        environment="PROD",
        account_no="1",
        position_snapshot={"overseas": {"holdings": [{"symbol": "TSLA", "quantity": 20, "average_price": 240.0}]}},
        position_manager=PositionManager(),
    )
    assert len(changed) == 1
    assert changed[0].symbol == "TSLA"
    assert changed[0].board_status == BoardStatus.OPEN_POSITION


def test_startup_reconciliation_preserves_correct_open_position():
    card = _card(
        board_status=BoardStatus.OPEN_POSITION,
        broker_quantity=20,
        average_entry_price=100.0,
        position_runtime_status=PositionRuntimeStatus.OPEN,
    )
    changed = run_startup_reconciliation(
        [card],
        environment="PROD",
        account_no="1",
        position_snapshot={"overseas": {"holdings": [{"symbol": "AAPL", "quantity": 20, "average_price": 100.0}]}},
        position_manager=PositionManager(),
    )
    assert changed == []


def test_startup_reconciliation_handles_missing_snapshot_gracefully():
    changed = run_startup_reconciliation(
        [], environment="PROD", account_no="1", position_snapshot=None, position_manager=PositionManager()
    )
    assert changed == []


# --- P1-15: "no local order found" requires complete broker discovery -----


def test_buy_today_with_no_order_is_not_reset_when_discovery_is_incomplete(tmp_path):
    incomplete = BrokerOrderDiscoveryResult(
        open_orders_complete=False, history_complete=True, reserved_orders_complete=True
    )
    service, cancelled, _ = _service(tmp_path, discover_all_orders=lambda card: incomplete)
    card = _card(board_status=BoardStatus.BUY_TODAY, entry_orb_high=105.0)

    changed = service.run_eod_cleanup([card])

    assert changed == []
    assert card.board_status == BoardStatus.BUY_TODAY
    assert card.entry_orb_high == 105.0  # nothing was reset


def test_buy_today_with_no_order_resets_once_discovery_is_complete(tmp_path):
    complete = BrokerOrderDiscoveryResult(
        open_orders_complete=True, history_complete=True, reserved_orders_complete=True
    )
    service, cancelled, _ = _service(tmp_path, discover_all_orders=lambda card: complete)
    card = _card(board_status=BoardStatus.BUY_TODAY, entry_orb_high=105.0)

    changed = service.run_eod_cleanup([card])

    assert changed == [card]
    assert card.board_status == BoardStatus.BUYLIST
    assert card.entry_orb_high is None


def test_entry_pending_with_no_order_stays_reconciling_when_discovery_incomplete(tmp_path):
    incomplete = BrokerOrderDiscoveryResult(
        open_orders_complete=True, history_complete=False, reserved_orders_complete=True,
        errors=["KIS history query timed out"],
    )
    service, cancelled, _ = _service(tmp_path, discover_all_orders=lambda card: incomplete)
    card = _card(board_status=BoardStatus.ENTRY_PENDING)

    changed = service.run_eod_cleanup([card])

    assert changed == [card]
    assert card.board_status == BoardStatus.ENTRY_PENDING  # never assumed cancelled
    assert card.entry_runtime_status == EntryRuntimeStatus.DATA_UNAVAILABLE


def test_entry_pending_with_no_order_moves_to_buylist_once_discovery_confirms_nothing(tmp_path):
    complete = BrokerOrderDiscoveryResult(
        open_orders_complete=True, history_complete=True, reserved_orders_complete=True
    )
    service, cancelled, _ = _service(tmp_path, discover_all_orders=lambda card: complete)
    card = _card(board_status=BoardStatus.ENTRY_PENDING)

    changed = service.run_eod_cleanup([card])

    assert changed == [card]
    assert card.board_status == BoardStatus.BUYLIST


# --- P1-16: startup reconciliation also resolves unresolved orders --------


def test_startup_order_reconciliation_applies_a_fill_that_happened_offline(tmp_path):
    from src.services.eod_trading_service import (
        EodActionCallbacks,
        reconcile_unresolved_orders_at_startup,
    )

    order = _order(status=OrderStatus.FILLED, filled=100, avg_fill_price=101.0)
    callbacks = EodActionCallbacks(
        find_open_entry_order=lambda card: order,
        reconcile_order=lambda o: o,
        cancel_order=lambda cid: None,
    )
    card = _card(board_status=BoardStatus.ENTRY_PENDING, target_position_quantity=100)
    card.entry_orb_low = 95.0

    changed = reconcile_unresolved_orders_at_startup(
        [card], position_manager=PositionManager(), callbacks=callbacks
    )

    assert changed == [card]
    assert card.board_status == BoardStatus.OPEN_POSITION
    assert card.broker_quantity == 100
    assert card.stop_type == StopType.ORB_LOW


def test_startup_order_reconciliation_confirms_cancellation_that_happened_offline(tmp_path):
    from src.services.eod_trading_service import (
        EodActionCallbacks,
        reconcile_unresolved_orders_at_startup,
    )

    order = _order(status=OrderStatus.CANCELLED, filled=0)
    callbacks = EodActionCallbacks(
        find_open_entry_order=lambda card: order,
        reconcile_order=lambda o: o,
        cancel_order=lambda cid: None,
    )
    card = _card(board_status=BoardStatus.ENTRY_PENDING)

    changed = reconcile_unresolved_orders_at_startup(
        [card], position_manager=PositionManager(), callbacks=callbacks
    )

    assert changed == [card]
    assert card.board_status == BoardStatus.BUYLIST


def test_startup_order_reconciliation_leaves_still_working_order_untouched(tmp_path):
    from src.services.eod_trading_service import (
        EodActionCallbacks,
        reconcile_unresolved_orders_at_startup,
    )

    order = _order(status=OrderStatus.WORKING, filled=0)
    callbacks = EodActionCallbacks(
        find_open_entry_order=lambda card: order,
        reconcile_order=lambda o: o,
        cancel_order=lambda cid: None,
    )
    card = _card(board_status=BoardStatus.ENTRY_PENDING)

    changed = reconcile_unresolved_orders_at_startup(
        [card], position_manager=PositionManager(), callbacks=callbacks
    )

    assert changed == []  # a restart must not force a cancellation
    assert card.board_status == BoardStatus.ENTRY_PENDING


def test_startup_order_reconciliation_respects_incomplete_discovery(tmp_path):
    from src.services.eod_trading_service import (
        EodActionCallbacks,
        reconcile_unresolved_orders_at_startup,
    )

    incomplete = BrokerOrderDiscoveryResult(open_orders_complete=False)
    callbacks = EodActionCallbacks(
        find_open_entry_order=lambda card: None,
        reconcile_order=lambda o: o,
        cancel_order=lambda cid: None,
        discover_all_orders=lambda card: incomplete,
    )
    card = _card(board_status=BoardStatus.ENTRY_PENDING)

    changed = reconcile_unresolved_orders_at_startup(
        [card], position_manager=PositionManager(), callbacks=callbacks
    )

    assert changed == [card]
    assert card.board_status == BoardStatus.ENTRY_PENDING
    assert card.entry_runtime_status == EntryRuntimeStatus.DATA_UNAVAILABLE


# --- P0: complete discovery must be matched against snapshots, not just ----
# --- trusted as "nothing exists" the moment the local lookup misses --------


def test_startup_order_reconciliation_recovers_a_filled_order_missing_from_ledger(tmp_path):
    """A complete discovery query finding a real FILLED broker order that
    the local ledger simply lost track of must protect the position, not
    silently move the card to Buylist and orphan a filled, unprotected
    position."""
    from src.services.eod_trading_service import (
        EodActionCallbacks,
        reconcile_unresolved_orders_at_startup,
    )

    complete = BrokerOrderDiscoveryResult(
        open_orders_complete=True, history_complete=True, reserved_orders_complete=True,
        snapshots=[_snapshot(status=OrderStatus.FILLED, filled=100, avg_fill_price=101.5)],
    )
    callbacks = EodActionCallbacks(
        find_open_entry_order=lambda card: None,
        reconcile_order=lambda o: o,
        cancel_order=lambda cid: None,
        discover_all_orders=lambda card: complete,
    )
    card = _card(board_status=BoardStatus.ENTRY_PENDING)
    card.entry_orb_low = 95.0

    changed = reconcile_unresolved_orders_at_startup(
        [card], position_manager=PositionManager(), callbacks=callbacks
    )

    assert changed == [card]
    assert card.board_status == BoardStatus.OPEN_POSITION
    assert card.broker_quantity == 100
    assert card.average_entry_price == 101.5
    assert card.stop_type == StopType.ORB_LOW
    assert "ORDER_RECOVERED_FROM_BROKER_DISCOVERY" in card.warnings


def test_startup_order_reconciliation_keeps_a_still_working_order_missing_from_ledger(tmp_path):
    """A matching broker order that is still open (not filled, not
    terminal) must not be abandoned to Buylist either -- there is nothing
    left tracking or eventually cancelling it."""
    from src.services.eod_trading_service import (
        EodActionCallbacks,
        reconcile_unresolved_orders_at_startup,
    )

    complete = BrokerOrderDiscoveryResult(
        open_orders_complete=True, history_complete=True, reserved_orders_complete=True,
        snapshots=[_snapshot(status=OrderStatus.WORKING, filled=0)],
    )
    callbacks = EodActionCallbacks(
        find_open_entry_order=lambda card: None,
        reconcile_order=lambda o: o,
        cancel_order=lambda cid: None,
        discover_all_orders=lambda card: complete,
    )
    card = _card(board_status=BoardStatus.ENTRY_PENDING)

    changed = reconcile_unresolved_orders_at_startup(
        [card], position_manager=PositionManager(), callbacks=callbacks
    )

    assert changed == [card]
    assert card.board_status == BoardStatus.ENTRY_PENDING  # not orphaned to Buylist
    assert card.entry_runtime_status == EntryRuntimeStatus.DATA_UNAVAILABLE
    assert "UNRECONCILED_BROKER_ORDER" in card.warnings


def test_startup_order_reconciliation_moves_to_buylist_only_when_genuinely_no_match(tmp_path):
    """Original behavior preserved: a complete discovery query that
    genuinely contains nothing for this symbol still safely returns the
    card to Buylist."""
    from src.services.eod_trading_service import (
        EodActionCallbacks,
        reconcile_unresolved_orders_at_startup,
    )

    complete = BrokerOrderDiscoveryResult(
        open_orders_complete=True, history_complete=True, reserved_orders_complete=True,
        snapshots=[_snapshot(status=OrderStatus.FILLED, filled=5, symbol="MSFT")],
    )
    callbacks = EodActionCallbacks(
        find_open_entry_order=lambda card: None,
        reconcile_order=lambda o: o,
        cancel_order=lambda cid: None,
        discover_all_orders=lambda card: complete,
    )
    card = _card(board_status=BoardStatus.ENTRY_PENDING, symbol="AAPL")

    changed = reconcile_unresolved_orders_at_startup(
        [card], position_manager=PositionManager(), callbacks=callbacks
    )

    assert changed == [card]
    assert card.board_status == BoardStatus.BUYLIST


def test_startup_order_reconciliation_terminal_zero_fill_match_moves_to_buylist(tmp_path):
    from src.services.eod_trading_service import (
        EodActionCallbacks,
        reconcile_unresolved_orders_at_startup,
    )

    complete = BrokerOrderDiscoveryResult(
        open_orders_complete=True, history_complete=True, reserved_orders_complete=True,
        snapshots=[_snapshot(status=OrderStatus.CANCELLED, filled=0)],
    )
    callbacks = EodActionCallbacks(
        find_open_entry_order=lambda card: None,
        reconcile_order=lambda o: o,
        cancel_order=lambda cid: None,
        discover_all_orders=lambda card: complete,
    )
    card = _card(board_status=BoardStatus.ENTRY_PENDING)

    changed = reconcile_unresolved_orders_at_startup(
        [card], position_manager=PositionManager(), callbacks=callbacks
    )

    assert changed == [card]
    assert card.board_status == BoardStatus.BUYLIST


def test_buy_today_reset_does_not_orphan_a_matching_broker_order(tmp_path):
    """Same fix, applied to EodTradingService's EOD "Buy Today with no
    submitted order" reset -- a matching snapshot means the card's status
    is stale (an order genuinely exists), not that resetting is safe."""
    complete = BrokerOrderDiscoveryResult(
        open_orders_complete=True, history_complete=True, reserved_orders_complete=True,
        snapshots=[_snapshot(status=OrderStatus.WORKING, filled=0)],
    )
    service, cancelled, _ = _service(tmp_path, discover_all_orders=lambda card: complete)
    card = _card(board_status=BoardStatus.BUY_TODAY)

    changed = service.run_eod_cleanup([card])

    assert changed == []
    assert card.board_status == BoardStatus.BUY_TODAY  # not reset out from under a live order


def test_entry_pending_eod_recovers_a_filled_order_missing_from_ledger(tmp_path):
    """Same fix, applied to EodTradingService's "Entry Pending at EOD"
    path."""
    complete = BrokerOrderDiscoveryResult(
        open_orders_complete=True, history_complete=True, reserved_orders_complete=True,
        snapshots=[_snapshot(status=OrderStatus.FILLED, filled=100, avg_fill_price=101.5)],
    )
    service, cancelled, _ = _service(tmp_path, discover_all_orders=lambda card: complete)
    card = _card(board_status=BoardStatus.ENTRY_PENDING)
    card.entry_orb_low = 95.0

    changed = service.run_eod_cleanup([card])

    assert changed == [card]
    assert card.board_status == BoardStatus.OPEN_POSITION
    assert card.broker_quantity == 100


def test_entry_pending_eod_keeps_a_still_working_order_missing_from_ledger(tmp_path):
    complete = BrokerOrderDiscoveryResult(
        open_orders_complete=True, history_complete=True, reserved_orders_complete=True,
        snapshots=[_snapshot(status=OrderStatus.WORKING, filled=0)],
    )
    service, cancelled, _ = _service(tmp_path, discover_all_orders=lambda card: complete)
    card = _card(board_status=BoardStatus.ENTRY_PENDING)

    changed = service.run_eod_cleanup([card])

    assert changed == [card]
    assert card.board_status == BoardStatus.ENTRY_PENDING
    assert "UNRECONCILED_BROKER_ORDER" in card.warnings
    assert cancelled == []  # never guesses a cancel against an unverified snapshot


def test_run_startup_reconciliation_composes_positions_and_orders(tmp_path):
    from src.services.eod_trading_service import EodActionCallbacks

    order = _order(status=OrderStatus.FILLED, filled=50, avg_fill_price=200.0)
    order_callbacks = EodActionCallbacks(
        find_open_entry_order=lambda card: order if card.symbol == "MSFT" else None,
        reconcile_order=lambda o: o,
        cancel_order=lambda cid: None,
    )
    open_position_card = _card(
        symbol="AAPL", board_status=BoardStatus.OPEN_POSITION, broker_quantity=20,
        average_entry_price=100.0, position_runtime_status=PositionRuntimeStatus.OPEN,
    )
    entry_pending_card = _card(symbol="MSFT", board_status=BoardStatus.ENTRY_PENDING)

    changed = run_startup_reconciliation(
        [open_position_card, entry_pending_card],
        environment="PROD",
        account_no="1",
        position_snapshot={"overseas": {"holdings": [{"symbol": "AAPL", "quantity": 20, "average_price": 100.0}]}},
        position_manager=PositionManager(),
        order_callbacks=order_callbacks,
    )

    assert entry_pending_card in changed
    assert entry_pending_card.board_status == BoardStatus.OPEN_POSITION
    assert entry_pending_card.broker_quantity == 50
    # The already-correct AAPL position must not show up as changed.
    assert open_position_card not in changed
