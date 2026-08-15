"""Tests for src.services.eod_trading_service."""
from __future__ import annotations

from src.core.order_state import (
    BrokerOrder,
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
)
from src.services.entry_attempt_manager import EntryAttemptManager
from src.services.position_manager import PositionManager


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


def _service(tmp_path, *, find_order=None, reconcile_order=None):
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


def test_buy_today_with_a_working_order_moves_to_entry_pending_instead_of_staying_stranded(
    tmp_path,
):
    """Review finding P0: "a local order with stale BUY_TODAY card state
    remains uncorrected" -- a real local order already exists (e.g. a
    crash landed between the broker confirming submission and the card's
    board_status being updated), so leaving board_status at BUY_TODAY kept
    it outside continuous entry-order tracking entirely (no fill ever got
    a stop attached). It must move to ENTRY_PENDING so a later
    ENTRY_PENDING pass (EOD's own two-phase cancel, or the normal
    heartbeat) actually resolves this exact order instead of leaving it
    stranded -- this single run_eod_cleanup pass only makes the
    transition; run_eod_cleanup's own per-card if/elif dispatch means the
    ENTRY_PENDING branch itself only runs on a subsequent pass."""
    order = _order(status=OrderStatus.ACCEPTED)
    service, cancelled, _ = _service(tmp_path, find_order=lambda card: order)
    card = _card(board_status=BoardStatus.BUY_TODAY)

    changed = service.run_eod_cleanup([card])
    assert changed == [card]
    assert card.board_status == BoardStatus.ENTRY_PENDING
    assert card.entry_runtime_status == EntryRuntimeStatus.ORDER_PENDING


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


# --- Account reconciliation owns discovery; EOD consumes local state ----


def test_buy_today_with_no_local_order_resets_after_account_reconciliation(tmp_path):
    service, _, _ = _service(tmp_path)
    card = _card(board_status=BoardStatus.BUY_TODAY, entry_orb_high=105.0)

    changed = service.run_eod_cleanup([card])

    assert changed == [card]
    assert card.board_status == BoardStatus.BUYLIST
    assert card.entry_orb_high is None


def test_entry_pending_without_a_local_order_waits_for_the_account_reducer(tmp_path):
    service, cancelled, _ = _service(tmp_path)
    card = _card(board_status=BoardStatus.ENTRY_PENDING)

    changed = service.run_eod_cleanup([card])

    assert changed == [card]
    assert card.board_status == BoardStatus.ENTRY_PENDING
    assert card.entry_runtime_status == EntryRuntimeStatus.DATA_UNAVAILABLE
    assert cancelled == []
