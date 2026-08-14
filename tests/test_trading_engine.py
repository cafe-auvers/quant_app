"""Tests for src.services.trading_engine."""
from __future__ import annotations

import datetime as dt

import pytest

from src.core import execution_config
from src.core.order_state import BrokerOrder, OrderIntent, OrderSide, OrderStatus
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
from src.services.trading_engine import EntryDeadlineLookup, TradingEngine


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
    manager = EntryAttemptManager(
        buying_power_provider=lambda e, a: buying_power,
        submit_order=submit_order or (lambda **kw: BrokerOrder.create(
            environment=kw["environment"], account_no=kw["account_no"], symbol=kw["symbol"],
            side=OrderSide.BUY, intent=OrderIntent.ENTRY, quantity_requested=kw["quantity"],
            limit_price=kw["limit_price"], status=OrderStatus.ACCEPTED,
        )),
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


def test_entry_pending_card_returns_to_buy_today_on_zero_fill_timeout(tmp_path):
    order = _pending_order(status=OrderStatus.ACCEPTED, filled=0)
    engine = _make_engine(tmp_path, find_order=lambda card: order, reconcile_order=lambda o: o)
    card = _open_card(board_status=BoardStatus.ENTRY_PENDING, broker_quantity=0, orderable_quantity=0)

    changed = engine.run_heartbeat([card])

    assert changed == [card]
    assert card.board_status == BoardStatus.BUY_TODAY
    assert card.entry_runtime_status == EntryRuntimeStatus.RETRY_COOLDOWN


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


def test_cancel_requested_zero_fill_moves_straight_to_buylist_no_retry(tmp_path):
    """The buyboard controller only *flags* cancel_requested (never moves
    the card off ENTRY_PENDING itself) -- the engine must resolve it here,
    and a user-requested cancel must never bounce to BUY_TODAY for a retry."""
    order = _pending_order(status=OrderStatus.ACCEPTED, filled=0, deadline_seconds_ago=-1000)  # far from its deadline
    engine = _make_engine(tmp_path, find_order=lambda card: order, reconcile_order=lambda o: o)
    card = _open_card(board_status=BoardStatus.ENTRY_PENDING, broker_quantity=0, orderable_quantity=0)
    card.entry_block_reason = "cancel_requested"

    changed = engine.run_heartbeat([card])

    assert changed == [card]
    assert card.board_status == BoardStatus.BUYLIST
    assert card.entry_runtime_status is None
    assert card.entry_block_reason == ""


def test_cancel_requested_partial_fill_keeps_shares_but_stops_completion(tmp_path):
    order = _pending_order(
        status=OrderStatus.PARTIALLY_FILLED, filled=4, avg_fill_price=100.0, deadline_seconds_ago=-1000
    )
    engine = _make_engine(tmp_path, find_order=lambda card: order, reconcile_order=lambda o: o)
    card = _open_card(board_status=BoardStatus.ENTRY_PENDING, broker_quantity=0, orderable_quantity=0)
    card.target_position_quantity = 10
    card.entry_block_reason = "cancel_requested"

    changed = engine.run_heartbeat([card])

    assert changed == [card]
    assert card.board_status == BoardStatus.OPEN_POSITION  # real shares kept
    assert card.broker_quantity == 4
    assert card.entry_remaining_target_quantity == 0  # not retried
    assert card.position_runtime_status == PositionRuntimeStatus.OPEN


def test_cancel_requested_before_deadline_still_resolves_immediately(tmp_path):
    """A cancel request must not wait for the 15s attempt deadline."""
    order = _pending_order(status=OrderStatus.ACCEPTED, filled=0, deadline_seconds_ago=-1000)
    engine = _make_engine(tmp_path, find_order=lambda card: order, reconcile_order=lambda o: o)
    card = _open_card(board_status=BoardStatus.ENTRY_PENDING)
    card.entry_block_reason = "cancel_requested"

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
    working = _pending_order(status=OrderStatus.WORKING, filled=0)
    submitted = []
    engine = _make_engine(tmp_path)
    engine._position_callbacks = PositionActionCallbacks(
        cancel_order=lambda cid: None,
        submit_sell_order=lambda **kw: submitted.append(kw),
        refresh_orderable_quantity=lambda *a: 260,
        find_open_sell_order=lambda card: working,
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
