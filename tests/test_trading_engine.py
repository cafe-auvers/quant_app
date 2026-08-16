"""Tests for src.services.trading_engine."""
from __future__ import annotations

import datetime as dt

import pytest

from src.core import execution_config
from src.core.order_state import BrokerOrder, OrderIntent, OrderSide, OrderStatus
from src.core.execution_result import ExecutionSubmissionResult
from src.core.trade_card_state import (
    BoardStatus,
    EntryRuntimeStatus,
    PositionRuntimeStatus,
    StopType,
    TradeCardState,
)
from src.services.entry_attempt_manager import EntryAttemptManager
from src.services.position_manager import PositionActionCallbacks, PositionManager
from src.services.realtime_market_data import QuoteSnapshot, RestPollingMarketDataService
from src.services.trading_engine import (
    EntryDeadlineLookup,
    TradingEngine,
    classify_market_data_outage_risk,
)


@pytest.fixture(autouse=True)
def _engine_enabled(monkeypatch):
    monkeypatch.setattr(execution_config, "is_buyboard_engine_enabled", lambda: True)
    import src.services.trading_engine as trading_engine_module

    monkeypatch.setattr(trading_engine_module, "is_buyboard_engine_enabled", lambda: True)


def _open_card(**overrides):
    fields = dict(
        environment="PROD",
        account_no="1",
        symbol="AAPL",
        board_status=BoardStatus.OPEN_POSITION,
        position_runtime_status=PositionRuntimeStatus.OPEN,
        broker_quantity=100,
        orderable_quantity=100,
        average_entry_price=100.0,
        stop_type=StopType.ORB_LOW,
        active_stop_price=95.0,
    )
    fields.update(overrides)
    return TradeCardState(**fields)


def _buy_today_card(**overrides):
    fields = dict(
        environment="PROD",
        account_no="1",
        symbol="AAPL",
        board_status=BoardStatus.BUY_TODAY,
        entry_runtime_status=EntryRuntimeStatus.EXECUTE_READY,
        planned_quantity=10,
        entry_trigger=100.0,
        kanban_priority=0,
    )
    fields.update(overrides)
    return TradeCardState(**fields)


def _make_engine(tmp_path, *, submit_order=None, buying_power=100_000.0, find_order=None, reconcile_order=None):
    raw_submit = submit_order or (lambda **kw: BrokerOrder.create(
        environment=kw["environment"], account_no=kw["account_no"], symbol=kw["symbol"],
        side=OrderSide.BUY, intent=OrderIntent.ENTRY, quantity_requested=kw["quantity"],
        limit_price=kw["limit_price"], status=OrderStatus.ACCEPTED,
    ))

    def normalized_submit(**kwargs):
        result = raw_submit(**kwargs)
        if isinstance(result, BrokerOrder):
            return ExecutionSubmissionResult.from_broker_order(result)
        return result

    manager = EntryAttemptManager(
        buying_power_provider=lambda e, a: buying_power,
        submit_order=normalized_submit,
        # Isolated per-test reservations ledger -- never the real
        # production data/capital_reservations.json (see the identical
        # concern documented in test_entry_attempt_manager.py).
        reservations_path=tmp_path / "reservations.json",
    )
    position_manager = PositionManager()
    market_data = RestPollingMarketDataService(quote_fetcher=lambda s: QuoteSnapshot(symbol=s, last_price=100.0))
    callbacks = PositionActionCallbacks(
        cancel_order=lambda cid: None,
        submit_sell_order=lambda **kw: None,
        refresh_orderable_quantity=lambda *a: 100,
    )
    lookup = EntryDeadlineLookup(
        find_open_entry_order=find_order or (lambda card: None),
        reconcile_order=reconcile_order or (lambda order: order),
    )
    return TradingEngine(
        entry_attempt_manager=manager,
        position_manager=position_manager,
        market_data=market_data,
        position_callbacks=callbacks,
        entry_deadline_lookup=lookup,
    )


# --- Disabled engine is a strict no-op --------------------------------------


def test_disabled_engine_ignores_everything(tmp_path, monkeypatch):
    import src.services.trading_engine as trading_engine_module

    monkeypatch.setattr(trading_engine_module, "is_buyboard_engine_enabled", lambda: False)
    engine = _make_engine(tmp_path)
    card = _open_card(active_stop_price=1000.0)  # would trigger if enabled
    quote = QuoteSnapshot(symbol="AAPL", last_price=1.0)

    assert engine.evaluate_quote([card], quote) == []
    assert card.exit_all_required is False
    assert engine.run_heartbeat([card]) == []


# --- Quote-tick stop trigger (spec section 766-770, 784-788) ---------------


def test_quote_tick_triggers_stop_and_starts_sell_all(tmp_path):
    engine = _make_engine(tmp_path)
    card = _open_card()
    quote = QuoteSnapshot(symbol="AAPL", last_price=90.0)  # below the 95.0 stop

    changed = engine.evaluate_quote([card], quote)

    assert changed == [card]
    assert card.exit_all_required is True
    assert card.board_status == BoardStatus.SELL_ALL


def test_quote_tick_above_stop_does_not_change_card(tmp_path):
    engine = _make_engine(tmp_path)
    card = _open_card()
    quote = QuoteSnapshot(symbol="AAPL", last_price=99.0)

    changed = engine.evaluate_quote([card], quote)
    assert changed == []
    assert card.board_status == BoardStatus.OPEN_POSITION


def test_quote_tick_during_partial_sell_runs_stop_sequence(tmp_path):
    engine = _make_engine(tmp_path)
    card = _open_card(board_status=BoardStatus.PARTIAL_SELL, pending_partial_sell_quantity=50)
    quote = QuoteSnapshot(symbol="AAPL", last_price=90.0)

    changed = engine.evaluate_quote([card], quote)

    assert changed == [card]
    assert card.board_status == BoardStatus.SELL_ALL
    assert card.pending_partial_sell_quantity == 0
    assert card.position_runtime_status == PositionRuntimeStatus.LIQUIDATING


def test_quote_tick_ignores_unrelated_symbol(tmp_path):
    engine = _make_engine(tmp_path)
    card = _open_card(symbol="AAPL")
    quote = QuoteSnapshot(symbol="NVDA", last_price=1.0)
    assert engine.evaluate_quote([card], quote) == []


# --- BUY_TODAY entry evaluation + stale-quote gate --------------------------


def test_stale_quote_blocks_entry_and_flags_data_unavailable(tmp_path):
    engine = _make_engine(tmp_path)
    # No quote has been polled at all -> latest_quote() is None -> stale.
    card = _buy_today_card()

    changed = engine.run_heartbeat([card])

    assert changed == [card]
    assert card.entry_runtime_status == EntryRuntimeStatus.DATA_UNAVAILABLE
    assert card.board_status == BoardStatus.BUY_TODAY  # never attempted


def test_fresh_quote_allows_entry_submission_and_moves_to_entry_pending(tmp_path):
    engine = _make_engine(tmp_path)
    engine._market_data.subscribe(["AAPL"])
    engine._market_data.poll_once()  # seeds a fresh quote
    card = _buy_today_card()

    changed = engine.run_heartbeat([card])

    assert changed == [card]
    assert card.board_status == BoardStatus.ENTRY_PENDING
    assert card.entry_runtime_status == EntryRuntimeStatus.ORDER_PENDING


def test_card_without_execute_ready_status_is_ignored(tmp_path):
    engine = _make_engine(tmp_path)
    engine._market_data.subscribe(["AAPL"])
    engine._market_data.poll_once()
    card = _buy_today_card(entry_runtime_status=EntryRuntimeStatus.ORB_FORMING)

    assert engine.run_heartbeat([card]) == []
    assert card.board_status == BoardStatus.BUY_TODAY


def test_insufficient_capital_sets_waiting_for_capital_badge(tmp_path):
    engine = _make_engine(tmp_path, buying_power=1.0)
    engine._market_data.subscribe(["AAPL"])
    engine._market_data.poll_once()
    card = _buy_today_card()

    changed = engine.run_heartbeat([card])
    assert changed == [card]
    assert card.entry_runtime_status == EntryRuntimeStatus.WAITING_FOR_CAPITAL
    assert card.board_status == BoardStatus.BUY_TODAY


# --- Entry-attempt deadline detection (spec section 401-444, 789-799) ------


def _pending_order(*, status, filled=0, avg_fill_price=0.0, deadline_seconds_ago=20):
    deadline = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=deadline_seconds_ago)
    order = BrokerOrder.create(
        environment="PROD", account_no="1", symbol="AAPL", side=OrderSide.BUY,
        intent=OrderIntent.ENTRY, quantity_requested=10, limit_price=100.0,
        status=status, attempt_deadline_at=deadline.isoformat(),
    )
    order.filled_quantity = filled
    order.avg_fill_price = avg_fill_price
    return order


def test_entry_pending_card_moves_to_open_position_on_full_fill_at_deadline(tmp_path):
    order = _pending_order(status=OrderStatus.FILLED, filled=10, avg_fill_price=100.5)
    engine = _make_engine(tmp_path, find_order=lambda card: order, reconcile_order=lambda o: o)
    card = _open_card(board_status=BoardStatus.ENTRY_PENDING, broker_quantity=0, orderable_quantity=0)
    card.entry_orb_low = 97.0
    card.entry_orb_window = "5m"

    changed = engine.run_heartbeat([card])

    assert changed == [card]
    assert card.board_status == BoardStatus.OPEN_POSITION
    assert card.broker_quantity == 10
    assert card.average_entry_price == 100.5
    assert card.stop_type == StopType.ORB_LOW
    assert card.active_stop_price == 97.0


class _StatefulOrderTracker:
    """Simulates the real two-phase cancel lifecycle for tests: the order's
    status only becomes CANCEL_REQUESTED once ``cancel_order`` actually
    fires, and only advances to a terminal status when the test calls
    ``resolve(...)`` -- modeling a later heartbeat tick observing the
    broker's actual confirmation instead of assuming it happened
    immediately (review finding P0-7).
    """

    def __init__(self, order):
        self.order = order
        self.cancel_calls: list[str] = []

    def find(self, card):
        return self.order

    def reconcile(self, order):
        return self.order

    def cancel_order(self, client_order_id):
        self.cancel_calls.append(client_order_id)
        self.order.status = OrderStatus.CANCEL_REQUESTED

    def resolve(self, status, *, filled=None, avg_fill_price=None):
        self.order.status = status
        if filled is not None:
            self.order.filled_quantity = filled
        if avg_fill_price is not None:
            self.order.avg_fill_price = avg_fill_price


def test_entry_pending_zero_fill_timeout_only_requests_cancel_first_tick(tmp_path):
    """P0-7: the first tick past the deadline must only *request* the
    cancel -- capital stays reserved and board_status stays ENTRY_PENDING
    until a later tick observes the broker's actual confirmation."""
    tracker = _StatefulOrderTracker(_pending_order(status=OrderStatus.ACCEPTED, filled=0))
    engine = _make_engine(tmp_path, find_order=tracker.find, reconcile_order=tracker.reconcile)
    engine._position_callbacks.cancel_order = tracker.cancel_order
    card = _open_card(board_status=BoardStatus.ENTRY_PENDING, broker_quantity=0, orderable_quantity=0)

    changed = engine.run_heartbeat([card])

    assert changed == [card]
    assert card.board_status == BoardStatus.ENTRY_PENDING  # not yet resolved
    assert card.entry_cancel_in_flight is True
    assert tracker.cancel_calls == [tracker.order.client_order_id]

    # A second tick before the broker confirms anything must not resend the
    # cancel or change the card.
    changed_again = engine.run_heartbeat([card])
    assert changed_again == []
    assert len(tracker.cancel_calls) == 1


def test_entry_pending_card_returns_to_buy_today_once_cancel_confirmed(tmp_path):
    tracker = _StatefulOrderTracker(_pending_order(status=OrderStatus.ACCEPTED, filled=0))
    engine = _make_engine(tmp_path, find_order=tracker.find, reconcile_order=tracker.reconcile)
    engine._position_callbacks.cancel_order = tracker.cancel_order
    card = _open_card(board_status=BoardStatus.ENTRY_PENDING, broker_quantity=0, orderable_quantity=0)

    engine.run_heartbeat([card])  # requests the cancel
    tracker.resolve(OrderStatus.CANCELLED)  # broker confirms on a later tick
    changed = engine.run_heartbeat([card])

    assert changed == [card]
    assert card.board_status == BoardStatus.BUY_TODAY
    assert card.entry_runtime_status == EntryRuntimeStatus.RETRY_COOLDOWN
    assert card.entry_cancel_in_flight is False
    assert card.next_retry_at is not None


def test_entry_pending_card_before_deadline_is_untouched(tmp_path):
    order = _pending_order(status=OrderStatus.ACCEPTED, filled=0, deadline_seconds_ago=-5)  # 5s in the future
    engine = _make_engine(tmp_path, find_order=lambda card: order, reconcile_order=lambda o: o)
    card = _open_card(board_status=BoardStatus.ENTRY_PENDING)

    assert engine.run_heartbeat([card]) == []
    assert card.board_status == BoardStatus.ENTRY_PENDING


def test_unknown_submission_state_blocks_only_that_symbol_and_keeps_capital(tmp_path):
    order = _pending_order(status=OrderStatus.UNKNOWN_SUBMISSION_STATE)
    engine = _make_engine(tmp_path, find_order=lambda card: order, reconcile_order=lambda o: o)
    aapl = _open_card(symbol="AAPL", board_status=BoardStatus.ENTRY_PENDING)
    nvda = _buy_today_card(symbol="NVDA")
    engine._market_data.subscribe(["NVDA"])
    engine._market_data.poll_once()

    changed = engine.run_heartbeat([aapl, nvda])

    assert aapl in changed
    assert aapl.entry_runtime_status == EntryRuntimeStatus.ORDER_PENDING
    assert aapl.board_status == BoardStatus.ENTRY_PENDING  # unresolved, not bounced back
    assert aapl.entry_cancel_in_flight is False  # never even attempted a cancel
    # NVDA (a different symbol) must still be able to submit normally.
    assert nvda.board_status == BoardStatus.ENTRY_PENDING


# --- Queued market-open Sell All (spec section 720-732) --------------------


def test_queued_sell_all_fires_once_market_is_open(tmp_path):
    submitted = []
    engine = _make_engine(tmp_path)
    engine._position_callbacks = PositionActionCallbacks(
        cancel_order=lambda cid: None,
        submit_sell_order=lambda **kw: submitted.append(kw),
        refresh_orderable_quantity=lambda *a: 100,
    )
    card = _open_card(board_status=BoardStatus.SELL_ALL, sell_all_at_market_open=True)

    changed = engine.run_heartbeat([card])

    assert changed == [card]
    assert card.sell_all_at_market_open is False
    assert len(submitted) == 1


def test_queued_sell_all_does_not_fire_before_market_open(tmp_path):
    engine = _make_engine(tmp_path)
    engine._market_is_open = lambda: False
    card = _open_card(board_status=BoardStatus.SELL_ALL, sell_all_at_market_open=True)

    assert engine.run_heartbeat([card]) == []
    assert card.sell_all_at_market_open is True


# --- User-initiated CancelEntry on a working order (spec section 298-304) --


def test_cancel_requested_zero_fill_moves_to_buylist_once_confirmed(tmp_path):
    """The buyboard controller only *flags* cancel_requested (never moves
    the card off ENTRY_PENDING itself) -- the engine resolves it here. Per
    P0-7, the cancel is only requested on the first tick and only finalized
    (Buylist, no retry) once a later tick sees it broker-confirmed."""
    tracker = _StatefulOrderTracker(
        _pending_order(status=OrderStatus.ACCEPTED, filled=0, deadline_seconds_ago=-1000)
    )
    engine = _make_engine(tmp_path, find_order=tracker.find, reconcile_order=tracker.reconcile)
    engine._position_callbacks.cancel_order = tracker.cancel_order
    card = _open_card(board_status=BoardStatus.ENTRY_PENDING, broker_quantity=0, orderable_quantity=0)
    card.entry_block_reason = "cancel_requested"

    first = engine.run_heartbeat([card])
    assert first == [card]
    assert card.board_status == BoardStatus.ENTRY_PENDING  # not yet finalized
    assert tracker.cancel_calls  # but the cancel was requested right away, not deferred

    tracker.resolve(OrderStatus.CANCELLED)
    second = engine.run_heartbeat([card])

    assert second == [card]
    assert card.board_status == BoardStatus.BUYLIST
    assert card.entry_runtime_status is None
    assert card.entry_block_reason == ""


def test_cancel_requested_partial_fill_keeps_shares_but_stops_completion(tmp_path):
    tracker = _StatefulOrderTracker(
        _pending_order(
            status=OrderStatus.PARTIALLY_FILLED,
            filled=4,
            avg_fill_price=100.0,
            deadline_seconds_ago=-1000,
        )
    )
    engine = _make_engine(tmp_path, find_order=tracker.find, reconcile_order=tracker.reconcile)
    engine._position_callbacks.cancel_order = tracker.cancel_order
    card = _open_card(board_status=BoardStatus.ENTRY_PENDING, broker_quantity=0, orderable_quantity=0)
    card.target_position_quantity = 10
    card.entry_block_reason = "cancel_requested"

    first = engine.run_heartbeat([card])
    # The fill itself is protected immediately (P0-6), even before the
    # cancel of the remainder is confirmed.
    assert first == [card]
    assert card.board_status == BoardStatus.OPEN_POSITION
    assert card.broker_quantity == 4

    tracker.resolve(OrderStatus.CANCELLED, filled=4)
    second = engine.run_heartbeat([card])

    assert second == [card]
    assert card.board_status == BoardStatus.OPEN_POSITION  # real shares kept
    assert card.broker_quantity == 4
    assert card.entry_remaining_target_quantity == 0  # not retried
    assert card.position_runtime_status == PositionRuntimeStatus.OPEN


def test_cancel_requested_before_deadline_still_requests_immediately(tmp_path):
    """A cancel request must not wait for the 15s attempt deadline."""
    tracker = _StatefulOrderTracker(
        _pending_order(status=OrderStatus.ACCEPTED, filled=0, deadline_seconds_ago=-1000)
    )
    engine = _make_engine(tmp_path, find_order=tracker.find, reconcile_order=tracker.reconcile)
    engine._position_callbacks.cancel_order = tracker.cancel_order
    card = _open_card(board_status=BoardStatus.ENTRY_PENDING, broker_quantity=0, orderable_quantity=0)
    card.entry_block_reason = "cancel_requested"

    engine.run_heartbeat([card])
    assert tracker.cancel_calls  # requested on the very first tick, not deferred to a deadline

    tracker.resolve(OrderStatus.CANCELLED)
    changed = engine.run_heartbeat([card])
    assert changed == [card]
    assert card.board_status == BoardStatus.BUYLIST


# --- Stop hit while completing entry / during partial sell (section 500-504, 671-697) --


def test_stop_hit_while_completing_entry_cancels_remaining_buy_order(tmp_path):
    remaining_buy_order = _pending_order(status=OrderStatus.ACCEPTED, filled=0)
    engine = _make_engine(tmp_path, find_order=lambda card: remaining_buy_order)
    cancelled = []
    engine._position_callbacks = PositionActionCallbacks(
        cancel_order=cancelled.append,
        submit_sell_order=lambda **kw: None,
        refresh_orderable_quantity=lambda *a: 40,
    )
    card = _open_card(
        broker_quantity=40, orderable_quantity=40, entry_remaining_target_quantity=60,
        position_runtime_status=PositionRuntimeStatus.ENTRY_COMPLETING,
    )
    quote = QuoteSnapshot(symbol="AAPL", last_price=90.0)  # below the 95.0 stop

    changed = engine.evaluate_quote([card], quote)

    assert changed == [card]
    assert cancelled == [remaining_buy_order.client_order_id]
    assert card.board_status == BoardStatus.SELL_ALL
    assert card.entry_remaining_target_quantity == 0


def test_stop_triggered_during_partial_sell_cancels_working_sell_order(tmp_path):
    working_sell_order = _pending_order(status=OrderStatus.WORKING, filled=0)
    engine = _make_engine(tmp_path)
    cancelled = []
    engine._position_callbacks = PositionActionCallbacks(
        cancel_order=cancelled.append,
        submit_sell_order=lambda **kw: None,
        refresh_orderable_quantity=lambda *a: 260,
        find_open_sell_order=lambda card: working_sell_order,
    )
    card = _open_card(
        board_status=BoardStatus.PARTIAL_SELL, broker_quantity=300, orderable_quantity=300,
        pending_partial_sell_quantity=100,
    )
    quote = QuoteSnapshot(symbol="AAPL", last_price=90.0)

    changed = engine.evaluate_quote([card], quote)

    assert changed == [card]
    assert cancelled == [working_sell_order.client_order_id]
    assert card.board_status == BoardStatus.SELL_ALL


# --- Sell All reprice/retry until flat (section 707-709) -------------------


def test_sell_all_retries_remainder_when_no_order_is_working(tmp_path):
    submitted = []
    engine = _make_engine(tmp_path)
    engine._position_callbacks = PositionActionCallbacks(
        cancel_order=lambda cid: None,
        submit_sell_order=lambda **kw: submitted.append(kw),
        refresh_orderable_quantity=lambda *a: 150,  # fewer shares left than the card believes
        find_open_sell_order=lambda card: None,
    )
    card = _open_card(board_status=BoardStatus.SELL_ALL, broker_quantity=260, orderable_quantity=260)

    changed = engine.run_heartbeat([card])

    assert changed == [card]
    assert card.broker_quantity == 150
    assert len(submitted) == 1
    assert submitted[0]["quantity"] == 150


def test_sell_all_does_not_resubmit_while_an_order_is_still_working(tmp_path):
    # A not-yet-expired deadline: this tests "no resubmission while still
    # working," not the separate TTL-cancel-escalation behavior below.
    working = _pending_order(status=OrderStatus.WORKING, filled=0, deadline_seconds_ago=-30)
    submitted = []
    engine = _make_engine(tmp_path)
    engine._position_callbacks = PositionActionCallbacks(
        cancel_order=lambda cid: None,
        submit_sell_order=lambda **kw: submitted.append(kw),
        refresh_orderable_quantity=lambda *a: 260,
        find_open_sell_order=lambda card: working,
        reconcile_sell_order=lambda order: order,
    )
    card = _open_card(board_status=BoardStatus.SELL_ALL, broker_quantity=260, orderable_quantity=260)

    changed = engine.run_heartbeat([card])
    assert changed == []
    assert submitted == []


def test_sell_all_closes_once_broker_confirms_zero(tmp_path):
    engine = _make_engine(tmp_path)
    engine._position_callbacks = PositionActionCallbacks(
        cancel_order=lambda cid: None,
        submit_sell_order=lambda **kw: pytest.fail("must not submit once flat"),
        refresh_orderable_quantity=lambda *a: 0,
        find_open_sell_order=lambda card: None,
    )
    card = _open_card(board_status=BoardStatus.SELL_ALL, broker_quantity=50, orderable_quantity=50)

    changed = engine.run_heartbeat([card])

    assert changed == [card]
    assert card.board_status == BoardStatus.CLOSED
    assert card.broker_quantity == 0


# --- WebSocket/market-data disconnect blocks entries (section 827-832) -----


def test_disconnected_market_data_blocks_entry_even_with_a_fresh_cached_quote(tmp_path):
    engine = _make_engine(tmp_path)
    engine._market_data.subscribe(["AAPL"])
    engine._market_data.poll_once()  # connects and seeds a fresh quote
    assert engine._market_data.is_connected() is True

    # Simulate a disconnect that hasn't yet aged the cached quote past
    # QUOTE_STALE_AFTER_SECONDS -- the connection state itself must still
    # block the attempt, not just quote age.
    engine._market_data._connected = False
    card = _buy_today_card()

    changed = engine.run_heartbeat([card])

    assert changed == [card]
    assert card.entry_runtime_status == EntryRuntimeStatus.DATA_UNAVAILABLE
    assert card.board_status == BoardStatus.BUY_TODAY


# --- P0-4: retry-state recovery ---------------------------------------------


def test_recovers_retry_cooldown_card_once_next_retry_at_passes(tmp_path):
    clock_time = dt.datetime(2026, 1, 5, 14, 30, tzinfo=dt.timezone.utc)
    engine = _make_engine(tmp_path)
    engine._clock = lambda: clock_time
    engine._market_data.subscribe(["AAPL"])
    engine._market_data.poll_once()
    card = _buy_today_card(entry_runtime_status=EntryRuntimeStatus.RETRY_COOLDOWN)
    card.next_retry_at = clock_time - dt.timedelta(seconds=1)  # already due

    changed = engine.run_heartbeat([card])

    assert card in changed
    assert card.board_status == BoardStatus.ENTRY_PENDING or card.entry_runtime_status != EntryRuntimeStatus.RETRY_COOLDOWN


def test_does_not_recover_retry_cooldown_card_before_next_retry_at(tmp_path):
    clock_time = dt.datetime(2026, 1, 5, 14, 30, tzinfo=dt.timezone.utc)
    engine = _make_engine(tmp_path)
    engine._clock = lambda: clock_time
    card = _buy_today_card(entry_runtime_status=EntryRuntimeStatus.RETRY_COOLDOWN)
    card.next_retry_at = clock_time + dt.timedelta(seconds=30)  # not due yet

    engine.run_heartbeat([card])
    assert card.entry_runtime_status == EntryRuntimeStatus.RETRY_COOLDOWN


def test_recovers_waiting_for_capital_card_every_tick(tmp_path):
    engine = _make_engine(tmp_path)
    engine._market_data.subscribe(["AAPL"])
    engine._market_data.poll_once()
    card = _buy_today_card(entry_runtime_status=EntryRuntimeStatus.WAITING_FOR_CAPITAL)

    changed = engine.run_heartbeat([card])

    assert card in changed
    assert card.board_status == BoardStatus.ENTRY_PENDING


def test_recovers_data_unavailable_card_once_quote_is_fresh_again(tmp_path):
    engine = _make_engine(tmp_path)
    card = _buy_today_card(entry_runtime_status=EntryRuntimeStatus.DATA_UNAVAILABLE)

    engine.run_heartbeat([card])
    assert card.entry_runtime_status == EntryRuntimeStatus.DATA_UNAVAILABLE

    engine._market_data.subscribe(["AAPL"])
    engine._market_data.poll_once()
    changed = engine.run_heartbeat([card])

    assert card in changed
    assert card.board_status == BoardStatus.ENTRY_PENDING


# --- P0-3: Partial Sell is actually submitted and reconciled ---------------


def test_partial_sell_request_is_actually_submitted(tmp_path):
    submitted = []
    engine = _make_engine(tmp_path)
    engine._position_callbacks = PositionActionCallbacks(
        cancel_order=lambda cid: None,
        submit_sell_order=lambda **kw: submitted.append(kw),
        refresh_orderable_quantity=lambda *a: 300,
        find_open_sell_order=lambda card: None,
    )
    card = _open_card(board_status=BoardStatus.PARTIAL_SELL, broker_quantity=300, orderable_quantity=300)
    card.pending_partial_sell_quantity = 100

    changed = engine.run_heartbeat([card])

    assert changed == [card]
    assert len(submitted) == 1
    assert submitted[0]["quantity"] == 100
    assert submitted[0]["reason"] == "partial_sell"


def test_partial_sell_does_not_resubmit_while_order_is_working(tmp_path):
    # A not-yet-expired deadline: this tests "no resubmission while still
    # working," not the separate TTL-cancel-escalation behavior below.
    working = _pending_order(status=OrderStatus.WORKING, filled=0, deadline_seconds_ago=-30)
    submitted = []
    engine = _make_engine(tmp_path)
    engine._position_callbacks = PositionActionCallbacks(
        cancel_order=lambda cid: None,
        submit_sell_order=lambda **kw: submitted.append(kw),
        refresh_orderable_quantity=lambda *a: 300,
        find_open_sell_order=lambda card: working,
        reconcile_sell_order=lambda order: order,
    )
    card = _open_card(board_status=BoardStatus.PARTIAL_SELL, broker_quantity=300, orderable_quantity=300)
    card.pending_partial_sell_quantity = 100

    engine.run_heartbeat([card])
    assert submitted == []


def test_partial_sell_fill_moves_stop_to_breakeven_and_returns_to_open_position(tmp_path):
    filled_order = _pending_order(status=OrderStatus.FILLED, filled=100, avg_fill_price=101.0)
    engine = _make_engine(tmp_path)
    engine._position_callbacks = PositionActionCallbacks(
        cancel_order=lambda cid: None,
        submit_sell_order=lambda **kw: None,
        refresh_orderable_quantity=lambda *a: 200,
        find_open_sell_order=lambda card: filled_order,
        reconcile_sell_order=lambda order: order,
    )
    card = _open_card(
        board_status=BoardStatus.PARTIAL_SELL,
        broker_quantity=300,
        orderable_quantity=200,
        stop_type=StopType.ORB_LOW,
        active_stop_price=95.0,
    )
    card.pending_partial_sell_quantity = 100

    changed = engine.run_heartbeat([card])

    assert changed == [card]
    assert card.board_status == BoardStatus.OPEN_POSITION
    assert card.broker_quantity == 200
    assert card.pending_partial_sell_quantity == 0
    assert card.stop_type == StopType.BREAKEVEN


# --- P0-2: Sell All is actually submitted the first tick --------------------


def test_sell_all_submits_on_the_very_first_tick(tmp_path):
    submitted = []
    engine = _make_engine(tmp_path)
    engine._position_callbacks = PositionActionCallbacks(
        cancel_order=lambda cid: None,
        submit_sell_order=lambda **kw: submitted.append(kw),
        refresh_orderable_quantity=lambda *a: 300,
        find_open_sell_order=lambda card: None,
    )
    card = _open_card(broker_quantity=300, orderable_quantity=300)
    card.board_status = BoardStatus.SELL_ALL
    card.exit_all_required = True

    changed = engine.run_heartbeat([card])

    assert changed == [card]
    assert len(submitted) == 1
    assert submitted[0]["quantity"] == 300


# --- P0-5: partial entry fill's remaining target is actually retried -------


def test_entry_completion_resubmits_for_remaining_target(tmp_path):
    submitted = []

    def fake_submit(**kwargs):
        submitted.append(kwargs)
        return BrokerOrder.create(
            environment=kwargs["environment"], account_no=kwargs["account_no"], symbol=kwargs["symbol"],
            side=OrderSide.BUY, intent=OrderIntent.ENTRY, quantity_requested=kwargs["quantity"],
            limit_price=kwargs["limit_price"], status=OrderStatus.ACCEPTED,
        )

    engine = _make_engine(tmp_path, submit_order=fake_submit)
    engine._market_data.subscribe(["AAPL"])
    engine._market_data.poll_once()
    card = _open_card(broker_quantity=30, orderable_quantity=30)
    card.entry_remaining_target_quantity = 70
    card.entry_trigger = 100.0
    card.position_runtime_status = PositionRuntimeStatus.ENTRY_COMPLETING

    changed = engine.run_heartbeat([card])

    assert changed == [card]
    assert len(submitted) == 1
    assert submitted[0]["quantity"] == 70
    assert card.entry_runtime_status == EntryRuntimeStatus.ORDER_PENDING


def test_entry_completion_does_not_retry_while_stop_triggered(tmp_path):
    submitted = []
    engine = _make_engine(tmp_path, submit_order=lambda **kw: submitted.append(kw))
    engine._market_data.subscribe(["AAPL"])
    engine._market_data.poll_once()
    card = _open_card(broker_quantity=30, orderable_quantity=30, exit_all_required=True)
    card.entry_remaining_target_quantity = 70
    card.entry_trigger = 100.0

    engine.run_heartbeat([card])
    assert submitted == []


def test_entry_completion_does_not_retry_while_an_order_is_already_working(tmp_path):
    working = _pending_order(status=OrderStatus.WORKING, filled=0)
    submitted = []
    engine = _make_engine(
        tmp_path, submit_order=lambda **kw: submitted.append(kw), find_order=lambda card: working
    )
    engine._market_data.subscribe(["AAPL"])
    engine._market_data.poll_once()
    card = _open_card(broker_quantity=30, orderable_quantity=30)
    card.entry_remaining_target_quantity = 70
    card.entry_trigger = 100.0

    engine.run_heartbeat([card])
    assert submitted == []


# --- P0-9: marketable price uses the live quote -----------------------------


def test_marketable_entry_price_uses_higher_of_trigger_and_live_quote(tmp_path):
    submitted = []

    def fake_submit(**kwargs):
        submitted.append(kwargs)
        return BrokerOrder.create(
            environment=kwargs["environment"], account_no=kwargs["account_no"], symbol=kwargs["symbol"],
            side=OrderSide.BUY, intent=OrderIntent.ENTRY, quantity_requested=kwargs["quantity"],
            limit_price=kwargs["limit_price"], status=OrderStatus.ACCEPTED,
        )

    engine = _make_engine(tmp_path, submit_order=fake_submit)
    engine._market_data = RestPollingMarketDataService(
        quote_fetcher=lambda s: QuoteSnapshot(symbol=s, last_price=105.0, bid=104.9, ask=105.1)
    )
    engine._market_data.subscribe(["AAPL"])
    engine._market_data.poll_once()
    card = _buy_today_card(entry_trigger=100.0)

    engine.run_heartbeat([card])

    assert len(submitted) == 1
    assert submitted[0]["limit_price"] == pytest.approx(105.1)


# --- P1-6: stale-quote protection for existing positions --------------------


def test_open_position_flagged_data_stale_when_quote_missing(tmp_path):
    engine = _make_engine(tmp_path)
    card = _open_card()

    changed = engine.run_heartbeat([card])

    assert changed == [card]
    assert "DATA_STALE" in card.warnings


def test_open_position_stale_flag_clears_once_fresh_quote_arrives(tmp_path):
    engine = _make_engine(tmp_path)
    card = _open_card()
    engine.run_heartbeat([card])
    assert "DATA_STALE" in card.warnings

    engine._market_data.subscribe(["AAPL"])
    engine._market_data.poll_once()
    changed = engine.run_heartbeat([card])

    assert changed == [card]
    assert "DATA_STALE" not in card.warnings


# --- P1-14: EOD cleanup is actually invoked from the heartbeat -------------


def test_eod_cleanup_runs_when_wired_and_window_reached(tmp_path):
    from src.services.eod_trading_service import EodActionCallbacks, EodTradingService

    engine = _make_engine(tmp_path)
    eod_callbacks = EodActionCallbacks(
        find_open_entry_order=lambda card: None,
        reconcile_order=lambda order: order,
        cancel_order=lambda cid: None,
    )
    engine._eod_service = EodTradingService(
        entry_attempt_manager=engine._entry_attempt_manager,
        position_manager=engine._position_manager,
        callbacks=eod_callbacks,
        reservations_path=tmp_path / "reservations.json",
    )
    engine._eod_window_reached = lambda: True
    card = _buy_today_card(entry_runtime_status=EntryRuntimeStatus.ARMED)

    changed = engine.run_heartbeat([card])

    assert card in changed
    assert card.board_status == BoardStatus.BUYLIST


def test_eod_cleanup_does_not_run_outside_the_window(tmp_path):
    from src.services.eod_trading_service import EodActionCallbacks, EodTradingService

    engine = _make_engine(tmp_path)
    eod_callbacks = EodActionCallbacks(
        find_open_entry_order=lambda card: None,
        reconcile_order=lambda order: order,
        cancel_order=lambda cid: None,
    )
    engine._eod_service = EodTradingService(
        entry_attempt_manager=engine._entry_attempt_manager,
        position_manager=engine._position_manager,
        callbacks=eod_callbacks,
        reservations_path=tmp_path / "reservations.json",
    )
    # engine._eod_window_reached left at its default (False)
    card = _buy_today_card(entry_runtime_status=EntryRuntimeStatus.ARMED)

    engine.run_heartbeat([card])
    assert card.board_status == BoardStatus.BUY_TODAY


def test_eod_cleanup_is_a_no_op_without_an_eod_service_wired(tmp_path):
    engine = _make_engine(tmp_path)  # no eod_service passed
    engine._eod_window_reached = lambda: True
    card = _buy_today_card(entry_runtime_status=EntryRuntimeStatus.ARMED)

    engine.run_heartbeat([card])


# --- P0-5: cumulative fill accounting across multiple entry attempts -------


def test_second_attempt_fill_accumulates_via_broker_refresh(tmp_path):
    """A completion order's own filled_quantity starts back at 0 -- without
    a broker-truth refresh this either gets ignored (below the running
    total) or overwrites it. Wiring refresh_broker_position makes the
    cumulative total correct regardless."""
    from src.services.position_manager import BrokerHolding

    second_attempt_order = _pending_order(status=OrderStatus.FILLED, filled=10, avg_fill_price=105.0)
    engine = _make_engine(tmp_path, find_order=lambda card: second_attempt_order, reconcile_order=lambda o: o)
    engine._entry_deadline_lookup.refresh_broker_position = (
        lambda card: BrokerHolding(symbol="AAPL", quantity=40, average_price=101.25)
    )
    # Simulates a card already holding 30 shares from a first attempt,
    # still trying to complete a target of 100.
    card = _open_card(
        board_status=BoardStatus.ENTRY_PENDING,
        broker_quantity=30,
        orderable_quantity=30,
        average_entry_price=100.0,
        target_position_quantity=100,
        stop_type=StopType.ORB_LOW,
        active_stop_price=95.0,
    )

    changed = engine.run_heartbeat([card])

    assert changed == [card]
    # Broker truth (40, not 30+10=40 coincidentally matching here by design
    # of the fixture, but read from the refresh callback, not computed
    # locally) drives the cumulative total.
    assert card.broker_quantity == 40
    assert card.average_entry_price == 101.25
    assert card.entry_remaining_target_quantity == 60
    assert card.board_status == BoardStatus.OPEN_POSITION


def test_second_attempt_fill_accumulates_via_delta_fallback(tmp_path):
    """Without a broker-refresh callback wired, the fallback adds only the
    *new* delta of this specific order's fill (order.applied_filled_quantity
    tracks what was already folded in), instead of comparing/overwriting
    against the card's running cumulative total."""
    second_attempt_order = _pending_order(status=OrderStatus.FILLED, filled=10, avg_fill_price=110.0)
    engine = _make_engine(tmp_path, find_order=lambda card: second_attempt_order, reconcile_order=lambda o: o)
    card = _open_card(
        board_status=BoardStatus.ENTRY_PENDING,
        broker_quantity=30,
        orderable_quantity=30,
        average_entry_price=100.0,
        target_position_quantity=100,
    )

    changed = engine.run_heartbeat([card])

    assert changed == [card]
    assert card.broker_quantity == 40  # 30 (attempt 1) + 10 (attempt 2), not overwritten to 10
    assert card.average_entry_price == pytest.approx((30 * 100.0 + 10 * 110.0) / 40)
    assert card.entry_remaining_target_quantity == 60


def test_protected_fill_does_not_double_count_within_the_same_tick(tmp_path):
    """MOVE_TO_OPEN_POSITION calls _protect_new_fill a second time in the
    same tick (once via the fill_protected check, once inside resolution) --
    the delta-fallback must not apply the same fill twice."""
    order = _pending_order(status=OrderStatus.FILLED, filled=10, avg_fill_price=100.0)
    engine = _make_engine(tmp_path, find_order=lambda card: order, reconcile_order=lambda o: o)
    card = _open_card(board_status=BoardStatus.ENTRY_PENDING, broker_quantity=0, orderable_quantity=0)

    engine.run_heartbeat([card])

    assert card.broker_quantity == 10  # not 20


# --- P0-6: only an automatic TTL cancel preserves/retries the remainder ----


def test_ttl_cancel_with_partial_fill_preserves_remaining_target(tmp_path):
    """Contrast with test_cancel_requested_partial_fill_keeps_shares_but_stops_completion
    (a USER_CANCEL): an automatic TTL-deadline cancellation with a partial
    fill must keep entry_remaining_target_quantity so the remainder is
    retried, per the agreed rule ("cancel the remainder and reattempt while
    the entry remains valid")."""
    tracker = _StatefulOrderTracker(
        _pending_order(status=OrderStatus.PARTIALLY_FILLED, filled=4, avg_fill_price=100.0)
    )
    engine = _make_engine(tmp_path, find_order=tracker.find, reconcile_order=tracker.reconcile)
    engine._position_callbacks.cancel_order = tracker.cancel_order
    card = _open_card(board_status=BoardStatus.ENTRY_PENDING, broker_quantity=0, orderable_quantity=0)
    card.target_position_quantity = 10
    # No entry_block_reason set -- this is a plain TTL deadline cancel, not
    # a user cancel.

    first = engine.run_heartbeat([card])
    assert first == [card]
    assert card.board_status == BoardStatus.OPEN_POSITION
    assert card.broker_quantity == 4

    tracker.resolve(OrderStatus.CANCELLED, filled=4)
    second = engine.run_heartbeat([card])

    assert second == [card]
    assert card.board_status == BoardStatus.OPEN_POSITION
    assert card.broker_quantity == 4
    assert card.entry_remaining_target_quantity == 6  # preserved, will be retried
    assert card.position_runtime_status == PositionRuntimeStatus.ENTRY_COMPLETING
    assert card.entry_cancel_in_flight is False


def test_ttl_cancelled_remainder_is_actually_resubmitted_on_a_later_tick(tmp_path):
    """The preserved remaining target from a TTL cancel is picked back up
    by _process_entry_completion once no order is working."""
    submitted = []

    def submit_order(**kw):
        submitted.append(kw)
        return BrokerOrder.create(
            environment=kw["environment"], account_no=kw["account_no"], symbol=kw["symbol"],
            side=OrderSide.BUY, intent=OrderIntent.ENTRY, quantity_requested=kw["quantity"],
            limit_price=kw["limit_price"], status=OrderStatus.ACCEPTED,
        )

    engine = _make_engine(tmp_path, submit_order=submit_order, find_order=lambda card: None)
    engine._market_data.subscribe(["AAPL"])
    engine._market_data.poll_once()
    card = _open_card(
        board_status=BoardStatus.OPEN_POSITION,
        broker_quantity=6,
        orderable_quantity=6,
        target_position_quantity=10,
        entry_remaining_target_quantity=4,
        entry_trigger=100.0,
    )

    changed = engine.run_heartbeat([card])

    assert changed == [card]
    assert len(submitted) == 1
    assert submitted[0]["quantity"] == 4
    assert card.entry_runtime_status == EntryRuntimeStatus.ORDER_PENDING


# --- P0-8: injectable market-session hooks ----------------------------------


def test_market_closed_blocks_new_entry_attempts(tmp_path):
    engine = _make_engine(tmp_path)
    engine._market_is_open_fn = lambda: False
    engine._market_data.subscribe(["AAPL"])
    engine._market_data.poll_once()
    card = _buy_today_card()

    assert engine.run_heartbeat([card]) == []
    assert card.board_status == BoardStatus.BUY_TODAY
    assert card.entry_runtime_status == EntryRuntimeStatus.EXECUTE_READY  # untouched


def test_market_closed_blocks_entry_completion_retry(tmp_path):
    engine = _make_engine(tmp_path)
    engine._market_is_open_fn = lambda: False
    engine._market_data.subscribe(["AAPL"])
    engine._market_data.poll_once()
    card = _open_card(
        board_status=BoardStatus.OPEN_POSITION,
        entry_remaining_target_quantity=4,
        target_position_quantity=10,
        entry_trigger=100.0,
    )

    assert engine.run_heartbeat([card]) == []


def test_market_closed_defers_an_immediate_sell_all_retry(tmp_path):
    """A stop-triggered Sell All (not the queued-at-open path) must not
    fire a regular-hours order while the market happens to be closed."""
    submitted = []
    engine = _make_engine(tmp_path)
    engine._market_is_open_fn = lambda: False
    engine._position_callbacks = PositionActionCallbacks(
        cancel_order=lambda cid: None,
        submit_sell_order=lambda **kw: submitted.append(kw),
        refresh_orderable_quantity=lambda *a: 260,
        find_open_sell_order=lambda card: None,
    )
    card = _open_card(board_status=BoardStatus.SELL_ALL, broker_quantity=260, orderable_quantity=260)

    changed = engine.run_heartbeat([card])
    assert submitted == []
    # broker_quantity/orderable_quantity are still refreshed even though no
    # order is submitted while the market is closed.
    assert changed == []


# --- P1-2: a rejected/zero-fill partial sell is not a completed exit -------


def test_partial_sell_rejected_returns_to_open_position_without_breakeven(tmp_path):
    rejected_order = _pending_order(status=OrderStatus.REJECTED, filled=0)
    engine = _make_engine(tmp_path)
    engine._position_callbacks = PositionActionCallbacks(
        cancel_order=lambda cid: None,
        submit_sell_order=lambda **kw: None,
        refresh_orderable_quantity=lambda *a: 300,  # unchanged -- nothing sold
        find_open_sell_order=lambda card: rejected_order,
        reconcile_sell_order=lambda order: order,
    )
    card = _open_card(
        board_status=BoardStatus.PARTIAL_SELL,
        broker_quantity=300,
        orderable_quantity=300,
        stop_type=StopType.ORB_LOW,
        active_stop_price=95.0,
    )
    card.pending_partial_sell_quantity = 100
    card.reserved_sell_quantity = 100

    changed = engine.run_heartbeat([card])

    assert changed == [card]
    assert card.board_status == BoardStatus.OPEN_POSITION
    assert card.broker_quantity == 300
    assert card.pending_partial_sell_quantity == 0
    assert card.reserved_sell_quantity == 0
    # The original stop stands -- must NOT have been moved to breakeven.
    assert card.stop_type == StopType.ORB_LOW
    assert card.active_stop_price == 95.0


# --- P1-3: orderable_quantity stays broker-authoritative --------------------


def test_partial_sell_submission_does_not_guess_orderable_quantity(tmp_path):
    engine = _make_engine(tmp_path)
    engine._position_callbacks = PositionActionCallbacks(
        cancel_order=lambda cid: None,
        submit_sell_order=lambda **kw: None,
        refresh_orderable_quantity=lambda *a: 300,
        find_open_sell_order=lambda card: None,
    )
    card = _open_card(board_status=BoardStatus.PARTIAL_SELL, broker_quantity=300, orderable_quantity=300)
    card.pending_partial_sell_quantity = 100

    changed = engine.run_heartbeat([card])

    assert changed == [card]
    # Broker-authoritative fields are untouched until the order actually
    # resolves -- only the informational reserved_sell_quantity moves.
    assert card.orderable_quantity == 300
    assert card.broker_quantity == 300
    assert card.reserved_sell_quantity == 100


# --- P1-4: exit retry backoff after a rejected submission -------------------


def test_sell_all_rejection_backs_off_instead_of_retrying_every_tick(tmp_path):
    attempts = []

    def submit_sell_order(**kw):
        attempts.append(kw)
        return BrokerOrder.create(
            environment=kw["environment"], account_no=kw["account_no"], symbol=kw["symbol"],
            side=OrderSide.SELL, intent=OrderIntent.MANUAL_EXIT, quantity_requested=kw["quantity"],
            limit_price=100.0, status=OrderStatus.REJECTED,
        )

    engine = _make_engine(tmp_path)
    engine._position_callbacks = PositionActionCallbacks(
        cancel_order=lambda cid: None,
        submit_sell_order=submit_sell_order,
        refresh_orderable_quantity=lambda *a: 260,
        find_open_sell_order=lambda card: None,
    )
    card = _open_card(board_status=BoardStatus.SELL_ALL, broker_quantity=260, orderable_quantity=260)

    engine.run_heartbeat([card])
    assert len(attempts) == 1
    assert card.exit_attempt_count == 1
    assert card.next_exit_retry_at is not None
    assert card.last_exit_error

    # A second tick immediately after must not resubmit yet.
    engine.run_heartbeat([card])
    assert len(attempts) == 1

    # Once the cooldown has passed, it tries again.
    card.next_exit_retry_at = card.next_exit_retry_at.replace(year=2000)
    engine.run_heartbeat([card])
    assert len(attempts) == 2
    assert card.exit_attempt_count == 2


def test_partial_sell_rejection_backs_off_instead_of_retrying_every_tick(tmp_path):
    attempts = []

    def submit_sell_order(**kw):
        attempts.append(kw)
        return BrokerOrder.create(
            environment=kw["environment"], account_no=kw["account_no"], symbol=kw["symbol"],
            side=OrderSide.SELL, intent=OrderIntent.PARTIAL_EXIT, quantity_requested=kw["quantity"],
            limit_price=100.0, status=OrderStatus.REJECTED,
        )

    engine = _make_engine(tmp_path)
    engine._position_callbacks = PositionActionCallbacks(
        cancel_order=lambda cid: None,
        submit_sell_order=submit_sell_order,
        refresh_orderable_quantity=lambda *a: 300,
        find_open_sell_order=lambda card: None,
    )
    card = _open_card(board_status=BoardStatus.PARTIAL_SELL, broker_quantity=300, orderable_quantity=300)
    card.pending_partial_sell_quantity = 100

    engine.run_heartbeat([card])
    assert len(attempts) == 1
    assert card.next_exit_retry_at is not None

    engine.run_heartbeat([card])
    assert len(attempts) == 1  # still cooling down


# --- Exit order TTL / cancel-confirm / reprice cycle (previously unbounded) -
# Code review: "does not appear to impose a deadline on a still-working Sell
# All order... a significant unattended-trading risk." Mirrors the entry
# side's two-phase cancel (request -> broker confirmation), applied to both
# Partial Sell (PARTIAL_EXIT_ATTEMPT_TTL_SECONDS) and Sell All
# (SELL_ALL_ATTEMPT_TTL_SECONDS), plus EXIT_CANCEL_CONFIRMATION_TIMEOUT_SECONDS
# stall detection.


def _working_sell_order(*, filled=0, deadline_seconds_ago=20, intent=OrderIntent.MANUAL_EXIT, now=None):
    reference = now or dt.datetime.now(dt.timezone.utc)
    deadline = reference - dt.timedelta(seconds=deadline_seconds_ago)
    order = BrokerOrder.create(
        environment="PROD", account_no="1", symbol="AAPL", side=OrderSide.SELL,
        intent=intent, quantity_requested=100, limit_price=90.0,
        status=OrderStatus.WORKING, attempt_deadline_at=deadline.isoformat(),
    )
    order.filled_quantity = filled
    return order


def test_sell_all_cancels_stuck_order_once_ttl_passes(tmp_path):
    """SELL_ALL_ATTEMPT_TTL_SECONDS: a working Sell All order past its
    deadline is cancelled -- previously this could sit open indefinitely."""
    tracker = _StatefulOrderTracker(_working_sell_order())
    submitted = []
    engine = _make_engine(tmp_path)
    engine._position_callbacks = PositionActionCallbacks(
        cancel_order=tracker.cancel_order,
        submit_sell_order=lambda **kw: submitted.append(kw),
        refresh_orderable_quantity=lambda *a: 260,
        find_open_sell_order=tracker.find,
        reconcile_sell_order=tracker.reconcile,
    )
    card = _open_card(board_status=BoardStatus.SELL_ALL, broker_quantity=260, orderable_quantity=260)

    changed = engine.run_heartbeat([card])

    assert changed == [card]
    assert card.exit_cancel_in_flight is True
    assert tracker.cancel_calls == [tracker.order.client_order_id]
    assert submitted == []  # never overlaps a replacement with the unconfirmed cancel

    # A second tick before confirmation must not resend the cancel.
    changed_again = engine.run_heartbeat([card])
    assert changed_again == []
    assert len(tracker.cancel_calls) == 1


def test_sell_all_resubmits_remainder_once_prior_ttl_cancel_is_gone_from_open_orders(tmp_path):
    """Once a TTL-cancelled Sell All order is confirmed and no longer
    reported "open" by the broker/ledger, the remainder is resubmitted --
    the exit-side counterpart of
    test_ttl_cancelled_remainder_is_actually_resubmitted_on_a_later_tick."""
    submitted = []

    def submit_sell_order(**kw):
        submitted.append(kw)
        return BrokerOrder.create(
            environment=kw["environment"], account_no=kw["account_no"], symbol=kw["symbol"],
            side=OrderSide.SELL, intent=OrderIntent.MANUAL_EXIT, quantity_requested=kw["quantity"],
            limit_price=90.0, status=OrderStatus.ACCEPTED,
        )

    engine = _make_engine(tmp_path)
    engine._position_callbacks = PositionActionCallbacks(
        cancel_order=lambda cid: None,
        submit_sell_order=submit_sell_order,
        refresh_orderable_quantity=lambda *a: 220,  # 40 shares already filled by the cancelled attempt
        find_open_sell_order=lambda card: None,
    )
    card = _open_card(board_status=BoardStatus.SELL_ALL, broker_quantity=260, orderable_quantity=260)
    card.exit_cancel_in_flight = True  # a prior tick already requested the cancel
    card.exit_cancel_requested_at = dt.datetime.now(dt.timezone.utc)

    changed = engine.run_heartbeat([card])

    assert changed == [card]
    assert card.exit_cancel_in_flight is False  # cleared once no order is left open
    assert card.broker_quantity == 220
    assert len(submitted) == 1
    assert submitted[0]["quantity"] == 220


def test_partial_sell_cancels_stuck_order_once_ttl_passes_and_applies_partial_fill(tmp_path):
    tracker = _StatefulOrderTracker(_working_sell_order(filled=30, intent=OrderIntent.PARTIAL_EXIT))
    engine = _make_engine(tmp_path)
    engine._position_callbacks = PositionActionCallbacks(
        cancel_order=tracker.cancel_order,
        submit_sell_order=lambda **kw: None,
        refresh_orderable_quantity=lambda *a: 270,  # 300 - 30 already filled
        find_open_sell_order=tracker.find,
        reconcile_sell_order=tracker.reconcile,
    )
    card = _open_card(
        board_status=BoardStatus.PARTIAL_SELL, broker_quantity=300, orderable_quantity=300,
        stop_type=StopType.ORB_LOW, active_stop_price=95.0,
    )
    card.pending_partial_sell_quantity = 100

    first = engine.run_heartbeat([card])
    assert first == [card]
    assert card.exit_cancel_in_flight is True
    assert tracker.cancel_calls == [tracker.order.client_order_id]
    assert card.board_status == BoardStatus.PARTIAL_SELL  # not yet resolved

    tracker.resolve(OrderStatus.CANCELLED, filled=30)
    second = engine.run_heartbeat([card])

    assert second == [card]
    assert card.exit_cancel_in_flight is False
    assert card.board_status == BoardStatus.OPEN_POSITION
    assert card.broker_quantity == 270
    assert card.stop_type == StopType.BREAKEVEN  # a real (partial) fill happened


def test_partial_sell_ttl_cancel_before_deadline_is_untouched(tmp_path):
    working = _working_sell_order(deadline_seconds_ago=-30, intent=OrderIntent.PARTIAL_EXIT)
    engine = _make_engine(tmp_path)
    engine._market_data.subscribe(["AAPL"])
    engine._market_data.poll_once()  # avoid an unrelated DATA_STALE change
    engine._position_callbacks = PositionActionCallbacks(
        cancel_order=lambda cid: pytest.fail("must not cancel before the deadline"),
        submit_sell_order=lambda **kw: None,
        refresh_orderable_quantity=lambda *a: 300,
        find_open_sell_order=lambda card: working,
        reconcile_sell_order=lambda order: order,
    )
    card = _open_card(board_status=BoardStatus.PARTIAL_SELL, broker_quantity=300, orderable_quantity=300)
    card.pending_partial_sell_quantity = 100

    assert engine.run_heartbeat([card]) == []


def test_exit_cancel_unconfirmed_past_timeout_flags_warning_without_resending_cancel(tmp_path):
    clock_time = dt.datetime(2026, 1, 5, 14, 30, tzinfo=dt.timezone.utc)
    tracker = _StatefulOrderTracker(_working_sell_order(now=clock_time))
    engine = _make_engine(tmp_path)
    engine._position_callbacks = PositionActionCallbacks(
        cancel_order=tracker.cancel_order,
        submit_sell_order=lambda **kw: None,
        refresh_orderable_quantity=lambda *a: 260,
        find_open_sell_order=tracker.find,
        reconcile_sell_order=tracker.reconcile,
    )
    engine._clock = lambda: clock_time
    card = _open_card(board_status=BoardStatus.SELL_ALL, broker_quantity=260, orderable_quantity=260)

    engine.run_heartbeat([card])  # requests the cancel at clock_time
    assert len(tracker.cancel_calls) == 1

    # Advance the clock well past EXIT_CANCEL_CONFIRMATION_TIMEOUT_SECONDS
    # without the broker ever confirming the cancel.
    engine._clock = lambda: clock_time + dt.timedelta(
        seconds=execution_config.EXIT_CANCEL_CONFIRMATION_TIMEOUT_SECONDS + 1
    )
    changed = engine.run_heartbeat([card])

    assert changed == [card]
    assert "EXIT_CANCEL_STALLED" in card.warnings
    assert len(tracker.cancel_calls) == 1  # never resent

    # Once the broker finally confirms, the warning clears.
    tracker.resolve(OrderStatus.CANCELLED)
    engine.run_heartbeat([card])
    assert "EXIT_CANCEL_STALLED" not in card.warnings


# --- P1-5: one bad card must not block every other card's tick -------------


def test_one_symbols_broker_error_does_not_block_other_symbols_entry(tmp_path):
    def flaky_find_order(card):
        if card.symbol == "AAPL":
            raise RuntimeError("simulated KIS outage for AAPL")
        return None

    submitted = []

    def submit_order(**kw):
        submitted.append(kw["symbol"])
        return BrokerOrder.create(
            environment=kw["environment"], account_no=kw["account_no"], symbol=kw["symbol"],
            side=OrderSide.BUY, intent=OrderIntent.ENTRY, quantity_requested=kw["quantity"],
            limit_price=kw["limit_price"], status=OrderStatus.ACCEPTED,
        )

    engine = _make_engine(tmp_path, submit_order=submit_order)
    # AAPL already has an (erroring) open order to reconcile; NVDA is a
    # fresh Buy Today candidate that should still get processed this tick.
    engine._entry_deadline_lookup.find_open_entry_order = flaky_find_order
    engine._market_data.subscribe(["AAPL", "NVDA"])
    engine._market_data.poll_once()

    aapl = _open_card(symbol="AAPL", board_status=BoardStatus.ENTRY_PENDING, broker_quantity=0)
    nvda = _buy_today_card(symbol="NVDA")

    changed = engine.run_heartbeat([aapl, nvda])

    assert nvda in changed
    assert nvda.board_status == BoardStatus.ENTRY_PENDING
    assert submitted == ["NVDA"]


def test_one_symbols_reconciliation_error_does_not_block_stale_quote_flagging(tmp_path):
    """A hard failure processing one card in a stage must not prevent a
    later, unrelated stage from processing every card that tick."""

    def flaky_find_open_sell_order(card):
        raise RuntimeError("simulated broker error")

    engine = _make_engine(tmp_path)
    engine._position_callbacks = PositionActionCallbacks(
        cancel_order=lambda cid: None,
        submit_sell_order=lambda **kw: None,
        refresh_orderable_quantity=lambda *a: 300,
        find_open_sell_order=flaky_find_open_sell_order,
    )
    partial = _open_card(symbol="AAPL", board_status=BoardStatus.PARTIAL_SELL)
    partial.pending_partial_sell_quantity = 100
    other_open = _open_card(symbol="NVDA")  # no live quote polled -> goes stale

    changed = engine.run_heartbeat([partial, other_open])

    assert other_open in changed
    assert "DATA_STALE" in other_open.warnings


# --- P1-6: a quote tick must reach every card holding that symbol ----------


def test_evaluate_quote_updates_every_account_holding_the_same_symbol(tmp_path):
    engine = _make_engine(tmp_path)
    account_one = _open_card(account_no="1")
    account_two = _open_card(account_no="2")
    quote = QuoteSnapshot(symbol="AAPL", last_price=90.0)  # below both stops

    changed = engine.evaluate_quote([account_one, account_two], quote)

    assert account_one in changed
    assert account_two in changed
    assert account_one.exit_all_required is True
    assert account_two.exit_all_required is True


def test_evaluate_quote_one_accounts_failure_does_not_block_the_other(tmp_path):
    engine = _make_engine(tmp_path)
    broken = _open_card(account_no="1", board_status=BoardStatus.PARTIAL_SELL)
    healthy = _open_card(account_no="2")
    engine._position_callbacks.find_open_sell_order = lambda card: (
        (_ for _ in ()).throw(RuntimeError("simulated failure")) if card.account_no == "1" else None
    )
    quote = QuoteSnapshot(symbol="AAPL", last_price=90.0)

    changed = engine.evaluate_quote([broken, healthy], quote)

    assert healthy in changed
    assert healthy.exit_all_required is True


# --- Workstream 5: tiered feed-outage policy ------------------------------


def _seed_then_disconnect(engine, symbol="AAPL"):
    engine._market_data.subscribe([symbol])
    engine._market_data.poll_once()
    engine._market_data._connected = False


def test_high_tier_position_liquidates_after_short_grace_period(tmp_path, monkeypatch):
    monkeypatch.setattr(execution_config, "MARKET_DATA_OUTAGE_GRACE_SECONDS", 5)
    now = [dt.datetime.now(dt.timezone.utc)]
    engine = _make_engine(tmp_path)
    engine._clock = lambda: now[0]
    card = _open_card(active_stop_price=99.5)
    _seed_then_disconnect(engine)

    engine.run_heartbeat([card])
    assert card.market_data_outage_risk_tier == "HIGH"
    assert not card.exit_all_required

    now[0] += dt.timedelta(seconds=6)
    engine.run_heartbeat([card])
    assert card.exit_all_required
    assert card.board_status == BoardStatus.SELL_ALL


def test_low_tier_position_holds_until_hard_ceiling(tmp_path, monkeypatch):
    monkeypatch.setattr(execution_config, "MARKET_DATA_OUTAGE_GRACE_SECONDS", 2)
    monkeypatch.setattr(execution_config, "MARKET_DATA_OUTAGE_MAX_HOLD_SECONDS", 10)
    now = [dt.datetime.now(dt.timezone.utc)]
    engine = _make_engine(tmp_path)
    engine._clock = lambda: now[0]
    card = _open_card(active_stop_price=50.0)
    _seed_then_disconnect(engine)

    engine.run_heartbeat([card])
    assert card.market_data_outage_risk_tier == "LOW"
    now[0] += dt.timedelta(seconds=5)
    engine.run_heartbeat([card])
    assert not card.exit_all_required
    now[0] += dt.timedelta(seconds=6)
    engine.run_heartbeat([card])
    assert card.exit_all_required


def test_broader_market_signal_escalates_frozen_low_tier(tmp_path):
    signal = [False]
    now = [dt.datetime.now(dt.timezone.utc)]
    engine = _make_engine(tmp_path)
    engine._clock = lambda: now[0]
    engine._broader_market_risk_signal = lambda card: signal[0]
    card = _open_card(active_stop_price=50.0)
    _seed_then_disconnect(engine)

    engine.run_heartbeat([card])
    assert card.market_data_outage_risk_tier == "LOW"
    signal[0] = True
    engine.run_heartbeat([card])
    assert card.market_data_outage_risk_tier == "HIGH"


def test_tier_classification_considers_account_level_risk_factors(monkeypatch):
    monkeypatch.setattr(execution_config, "MARKET_DATA_OUTAGE_CONCENTRATION_PCT", 0.20)
    card = _open_card(
        broker_quantity=300,
        active_stop_price=50.0,
        average_entry_price=100.0,
    )
    tier = classify_market_data_outage_risk(
        card, trusted_price=100.0, account_equity=100_000.0
    )
    assert tier == execution_config.MarketDataOutageRiskTier.HIGH


def test_rest_display_fallback_blocks_automatic_entries(tmp_path):
    engine = _make_engine(tmp_path)
    engine._market_data = RestPollingMarketDataService(
        quote_fetcher=lambda symbol: QuoteSnapshot(symbol=symbol, last_price=101.0),
        execution_grade=False,
    )
    engine._market_data.subscribe(["AAPL"])
    engine._market_data.poll_once()
    card = _buy_today_card()

    engine.run_heartbeat([card])

    assert card.entry_runtime_status == EntryRuntimeStatus.DATA_UNAVAILABLE


def test_outage_classification_never_guesses_from_average_entry(tmp_path):
    engine = _make_engine(tmp_path)
    engine._market_data._connected = False
    card = _open_card(active_stop_price=None, average_entry_price=100.0)

    engine.run_heartbeat([card])

    assert card.market_data_last_trusted_price is None
    assert card.market_data_outage_risk_tier == "HIGH"


def test_supervised_hold_only_cannot_disable_unattended_ceiling(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        execution_config, "MARKET_DATA_OUTAGE_SUPERVISED_HOLD_ONLY", True
    )
    monkeypatch.setattr(execution_config, "MARKET_DATA_OUTAGE_MAX_HOLD_SECONDS", 1)
    now = [dt.datetime.now(dt.timezone.utc)]
    engine = _make_engine(tmp_path)
    engine._clock = lambda: now[0]
    engine._unattended_session = lambda: True
    card = _open_card(active_stop_price=50.0)
    _seed_then_disconnect(engine)
    engine.run_heartbeat([card])
    now[0] += dt.timedelta(seconds=2)

    engine.run_heartbeat([card])

    assert card.exit_all_required
