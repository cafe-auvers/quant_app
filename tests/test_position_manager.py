"""Tests for src.services.position_manager."""
from __future__ import annotations

import pytest

from src.core.order_state import BrokerOrder, OrderIntent, OrderSide, OrderStatus
from src.core.trade_card_state import (
    BoardStatus,
    PositionRuntimeStatus,
    StopType,
    TradeCardState,
)
from src.services.position_manager import (
    BrokerHolding,
    PositionActionCallbacks,
    PositionManager,
    compute_breakeven_stop_price,
    evaluate_stop_trigger,
    extract_overseas_holdings,
    minimum_manual_stop_price,
    round_up_to_valid_tick,
)


def _open_card(**overrides) -> TradeCardState:
    fields = dict(
        environment="PROD",
        account_no="1",
        symbol="AAPL",
        board_status=BoardStatus.OPEN_POSITION,
        position_runtime_status=PositionRuntimeStatus.OPEN,
        broker_quantity=300,
        orderable_quantity=300,
        average_entry_price=100.0,
    )
    fields.update(overrides)
    return TradeCardState(**fields)


# --- Tick rounding / breakeven math (spec section 622-644) ------------------


def test_round_up_to_valid_tick_rounds_up_not_down():
    assert round_up_to_valid_tick(100.001) == pytest.approx(100.01)
    assert round_up_to_valid_tick(100.00) == pytest.approx(100.00)


def test_round_up_to_valid_tick_sub_dollar_uses_finer_tick():
    assert round_up_to_valid_tick(0.5001) == pytest.approx(0.5001)
    assert round_up_to_valid_tick(0.50005) == pytest.approx(0.5001)


def test_compute_breakeven_stop_price_applies_buffer_and_rounds_up():
    price = compute_breakeven_stop_price(100.0, breakeven_buffer_bps=15.0)
    # raw = 100 * 1.0015 = 100.15 -- already tick-aligned.
    assert price == pytest.approx(100.15)


def test_compute_breakeven_stop_price_uses_configured_default_when_unspecified():
    from src.core import execution_config

    price = compute_breakeven_stop_price(100.0)
    expected_raw = 100.0 * (1 + execution_config.BREAKEVEN_BUFFER_BPS / 10_000)
    assert price == pytest.approx(round_up_to_valid_tick(expected_raw))


# --- Manual stop floor (spec section 646-659) -------------------------------


def test_minimum_manual_stop_is_max_of_breakeven_and_active_stop():
    card = _open_card(stop_type=StopType.ORB_LOW, active_stop_price=95.0)
    breakeven = compute_breakeven_stop_price(card.average_entry_price)
    assert breakeven > 95.0
    assert minimum_manual_stop_price(card) == pytest.approx(breakeven)


def test_minimum_manual_stop_uses_active_stop_when_higher_than_breakeven():
    card = _open_card(stop_type=StopType.MANUAL_PRICE, active_stop_price=150.0)
    assert minimum_manual_stop_price(card) == pytest.approx(150.0)


def test_apply_manual_stop_rejects_widening_risk():
    card = _open_card(stop_type=StopType.ORB_LOW, active_stop_price=95.0)
    manager = PositionManager()
    minimum = minimum_manual_stop_price(card)
    with pytest.raises(ValueError):
        manager.apply_manual_stop(card, minimum - 1.0)


def test_apply_manual_stop_allows_tightening():
    card = _open_card(stop_type=StopType.ORB_LOW, active_stop_price=95.0)
    manager = PositionManager()
    minimum = minimum_manual_stop_price(card)
    manager.apply_manual_stop(card, minimum + 1.0)
    assert card.stop_type == StopType.MANUAL_PRICE
    assert card.active_stop_price == pytest.approx(minimum + 1.0)


# --- Stop trigger stickiness (spec section 661-669) -------------------------


def test_stop_not_triggered_above_stop_price():
    card = _open_card(stop_type=StopType.ORB_LOW, active_stop_price=95.0)
    assert evaluate_stop_trigger(card, 96.0) is False


def test_stop_triggered_at_or_below_stop_price():
    card = _open_card(stop_type=StopType.ORB_LOW, active_stop_price=95.0)
    assert evaluate_stop_trigger(card, 95.0) is True
    assert evaluate_stop_trigger(card, 94.0) is True


def test_stop_trigger_is_sticky_after_price_recovery():
    card = _open_card(stop_type=StopType.ORB_LOW, active_stop_price=95.0)
    manager = PositionManager()
    manager.evaluate_tick(card, 90.0)
    assert card.exit_all_required is True
    manager.evaluate_tick(card, 150.0)  # price recovers well above the stop
    assert card.exit_all_required is True  # must still be triggered


# --- First fill / partial exit stop management (section 606-620, 596-603) --


def test_apply_first_fill_stop_persists_entry_orb_values():
    card = _open_card(broker_quantity=100)
    manager = PositionManager()
    manager.apply_first_fill_stop(card, entry_orb_low=97.5, entry_orb_window="5m")
    assert card.stop_type == StopType.ORB_LOW
    assert card.active_stop_price == 97.5
    assert card.stop_quantity == 100
    assert card.entry_orb_window == "5m"
    assert card.entry_orb_low == 97.5


def test_on_partial_exit_filled_moves_stop_to_breakeven_not_raw_avg_cost():
    """Section 644: replaces the legacy max(existing_stop, avg_cost) logic."""
    card = _open_card(broker_quantity=300, stop_type=StopType.ORB_LOW, active_stop_price=95.0)
    manager = PositionManager()
    manager.on_partial_exit_filled(card, refreshed_broker_quantity=200)
    assert card.broker_quantity == 200
    assert card.stop_type == StopType.BREAKEVEN
    assert card.active_stop_price == pytest.approx(compute_breakeven_stop_price(100.0))
    assert card.active_stop_price > card.average_entry_price  # not raw avg cost
    assert card.board_status == BoardStatus.OPEN_POSITION


# --- Stop triggered during partial sell (spec section 671-697) -------------


def test_stop_triggered_during_partial_sell_sequence():
    """Cancels the working partial-sell order and sets intent; per code
    review finding P1-4, actually submitting the liquidation is deferred to
    the trading engine's retry stage (which only submits once the cancel is
    broker-confirmed) rather than done inline here."""
    card = _open_card(broker_quantity=300, orderable_quantity=300)
    card.board_status = BoardStatus.PARTIAL_SELL
    card.pending_partial_sell_quantity = 100

    cancelled = []
    submitted = []
    callbacks = PositionActionCallbacks(
        cancel_order=cancelled.append,
        submit_sell_order=lambda **kw: submitted.append(kw),
        refresh_orderable_quantity=lambda *a: 260,
    )

    manager = PositionManager()
    manager.handle_stop_triggered_during_partial_sell(
        card, callbacks=callbacks, working_partial_sell_client_order_id="co-1"
    )

    assert card.exit_all_required is True
    assert cancelled == ["co-1"]
    assert card.pending_partial_sell_quantity == 0
    assert card.board_status == BoardStatus.SELL_ALL
    assert card.position_runtime_status == PositionRuntimeStatus.LIQUIDATING
    assert submitted == []  # no order submitted inline


def test_stop_triggered_during_partial_sell_without_a_working_order_does_not_cancel():
    card = _open_card(broker_quantity=0, orderable_quantity=0)
    card.board_status = BoardStatus.PARTIAL_SELL

    cancelled = []
    callbacks = PositionActionCallbacks(
        cancel_order=cancelled.append,
        submit_sell_order=lambda **kw: None,
        refresh_orderable_quantity=lambda *a: 0,
    )
    PositionManager().handle_stop_triggered_during_partial_sell(
        card, callbacks=callbacks, working_partial_sell_client_order_id=None
    )
    assert cancelled == []
    assert card.board_status == BoardStatus.SELL_ALL


# --- Sell All workflow (spec section 699-732) -------------------------------


def test_start_sell_all_cancels_buy_but_does_not_submit_inline():
    """Submission is owned by the trading engine's retry stage (P1-4)."""
    card = _open_card(broker_quantity=300, orderable_quantity=300)
    cancelled = []
    submitted = []
    callbacks = PositionActionCallbacks(
        cancel_order=cancelled.append,
        submit_sell_order=lambda **kw: submitted.append(kw),
        refresh_orderable_quantity=lambda *a: 300,
    )
    PositionManager().start_sell_all(card, callbacks=callbacks, working_buy_client_order_id="buy-co-1")

    assert card.exit_all_required is True
    assert cancelled == ["buy-co-1"]
    assert card.board_status == BoardStatus.SELL_ALL
    assert card.position_runtime_status == PositionRuntimeStatus.LIQUIDATING
    assert submitted == []


def test_queue_sell_all_at_market_open():
    card = _open_card()
    PositionManager().queue_sell_all_at_market_open(card)
    assert card.board_status == BoardStatus.SELL_ALL
    assert card.position_runtime_status == PositionRuntimeStatus.QUEUED_FOR_OPEN
    assert card.sell_all_at_market_open is True
    assert card.exit_all_required is True


def test_confirm_flat_requires_zero_broker_quantity():
    card = _open_card(broker_quantity=5)
    card.board_status = BoardStatus.SELL_ALL
    with pytest.raises(ValueError):
        PositionManager().confirm_flat(card)


def test_confirm_flat_closes_card_and_clears_stop():
    card = _open_card(broker_quantity=0, stop_type=StopType.BREAKEVEN, active_stop_price=101.0)
    card.board_status = BoardStatus.SELL_ALL
    PositionManager().confirm_flat(card)
    assert card.board_status == BoardStatus.CLOSED
    assert card.position_runtime_status == PositionRuntimeStatus.CLOSED
    assert card.stop_type is None
    assert card.active_stop_price is None
    assert card.exit_all_required is False


# --- Manual purchase/sale discovery (spec section 14) -----------------------


def test_discover_manual_position_creates_new_card_with_membership_preserved():
    """Section 534-562, corrected per code review finding P1-3: a manual
    position with no known stop must NOT get one silently invented at
    average cost (which could immediately trigger a Sell All if the price
    is already below cost) -- it stays unprotected-but-flagged until the
    user sets a real stop."""
    manager = PositionManager()
    card = manager.discover_manual_position(
        None,
        environment="PROD",
        account_no="1",
        symbol="TSLA",
        name="Tesla Inc.",
        broker_quantity=50,
        average_entry_price=250.0,
    )
    assert card.board_status == BoardStatus.OPEN_POSITION
    assert card.watchlist_member is True
    assert card.buylist_member is True
    assert card.return_to_buylist_after_close is True
    assert card.broker_quantity == 50
    assert card.stop_type is None
    assert card.active_stop_price is None
    assert card.entry_block_reason == "manual_position_requires_stop"
    assert "STOP_REQUIRED" in card.warnings


def test_discover_manual_position_stop_required_never_auto_triggers_stop():
    manager = PositionManager()
    card = manager.discover_manual_position(
        None,
        environment="PROD",
        account_no="1",
        symbol="TSLA",
        name="Tesla Inc.",
        broker_quantity=50,
        average_entry_price=250.0,
    )
    # Price already crashed below cost at discovery time -- must not
    # auto-trigger a stop with no active_stop_price set.
    assert evaluate_stop_trigger(card, 10.0) is False


def test_discover_manual_position_updates_existing_card_without_overwriting_known_stop():
    existing = _open_card(stop_type=StopType.ORB_LOW, active_stop_price=95.0, broker_quantity=100)
    manager = PositionManager()
    updated = manager.discover_manual_position(
        existing,
        environment="PROD",
        account_no="1",
        symbol="AAPL",
        name="Apple Inc.",
        broker_quantity=150,  # user bought 50 more manually
        average_entry_price=102.0,
    )
    assert updated.broker_quantity == 150
    assert updated.stop_type == StopType.ORB_LOW  # untouched -- known provenance kept
    assert updated.active_stop_price == 95.0


def test_reconcile_manual_exit_closes_card_when_broker_position_is_zero():
    card = _open_card(broker_quantity=100)
    card.board_status = BoardStatus.SELL_ALL
    PositionManager().reconcile_manual_exit(card, broker_quantity=0)
    assert card.board_status == BoardStatus.CLOSED


def test_reconcile_manual_exit_shrinks_position_without_closing():
    card = _open_card(broker_quantity=100, stop_quantity=100)
    PositionManager().reconcile_manual_exit(card, broker_quantity=40)
    assert card.broker_quantity == 40
    assert card.stop_quantity == 40
    assert card.board_status == BoardStatus.OPEN_POSITION


# --- extract_overseas_holdings ----------------------------------------------


def test_extract_overseas_holdings_parses_and_filters_zero_quantity():
    snapshot = {
        "overseas": {
            "holdings": [
                {"symbol": "aapl", "quantity": "10", "average_price": "190.5"},
                {"symbol": "MSFT", "quantity": 0, "average_price": 300.0},
                {"quantity": 5, "average_price": 10.0},  # no symbol -- dropped
            ]
        }
    }
    holdings = extract_overseas_holdings(snapshot)
    assert holdings == [BrokerHolding(symbol="AAPL", quantity=10, average_price=190.5)]


def test_extract_overseas_holdings_handles_missing_or_empty_snapshot():
    assert extract_overseas_holdings(None) == []
    assert extract_overseas_holdings({}) == []


# --- reconcile_broker_positions (spec section 14, 534-562, 1028-1032) ------


def test_reconcile_discovers_manual_purchase_with_no_local_card():
    manager = PositionManager()
    holdings = [BrokerHolding(symbol="TSLA", quantity=50, average_price=250.0)]

    changed = manager.reconcile_broker_positions(
        [], holdings, environment="PROD", account_no="1", symbol_name_lookup=lambda s: "Tesla Inc."
    )

    assert len(changed) == 1
    card = changed[0]
    assert card.symbol == "TSLA"
    assert card.board_status == BoardStatus.OPEN_POSITION
    assert card.broker_quantity == 50
    assert card.watchlist_member is True
    assert card.buylist_member is True


def test_reconcile_updates_quantity_mismatch_on_existing_card():
    card = _open_card(symbol="AAPL", broker_quantity=100)
    holdings = [BrokerHolding(symbol="AAPL", quantity=150, average_price=101.0)]

    changed = PositionManager().reconcile_broker_positions(
        [card], holdings, environment="PROD", account_no="1"
    )

    assert changed == [card]
    assert card.broker_quantity == 150


def test_reconcile_leaves_already_correct_card_untouched():
    card = _open_card(symbol="AAPL", broker_quantity=100)
    holdings = [BrokerHolding(symbol="AAPL", quantity=100, average_price=100.0)]

    changed = PositionManager().reconcile_broker_positions(
        [card], holdings, environment="PROD", account_no="1"
    )
    assert changed == []


def test_reconcile_discovers_manual_sale_when_broker_reports_zero():
    card = _open_card(symbol="AAPL", broker_quantity=100)
    changed = PositionManager().reconcile_broker_positions(
        [card], [], environment="PROD", account_no="1"
    )
    assert changed == [card]
    assert card.board_status == BoardStatus.CLOSED


def test_reconcile_scoped_to_requested_account_only():
    """A card belonging to a different account must never be matched
    against another account's holdings -- a same-symbol holding under
    account "1" with no card of its own is a newly discovered position for
    account "1", not a mutation of account "2"'s card."""
    other_account_card = _open_card(symbol="AAPL", account_no="2", broker_quantity=100)
    holdings = [BrokerHolding(symbol="AAPL", quantity=75, average_price=100.0)]

    changed = PositionManager().reconcile_broker_positions(
        [other_account_card], holdings, environment="PROD", account_no="1"
    )

    assert len(changed) == 1
    discovered = changed[0]
    assert discovered.account_no == "1"
    assert discovered.broker_quantity == 75
    assert other_account_card.broker_quantity == 100  # untouched


# --- Reconciliation preserves in-progress intent (code review P1-1/P1-2) --


def test_reconcile_preserves_sell_all_intent_when_quantity_matches():
    card = _open_card(broker_quantity=100, orderable_quantity=0)
    card.board_status = BoardStatus.SELL_ALL
    card.exit_all_required = True
    holdings = [BrokerHolding(symbol="AAPL", quantity=100, average_price=100.0)]

    changed = PositionManager().reconcile_broker_positions(
        [card], holdings, environment="PROD", account_no="1"
    )

    assert changed == []
    assert card.board_status == BoardStatus.SELL_ALL
    assert card.exit_all_required is True


def test_reconcile_updates_facts_but_preserves_sell_all_status_on_change():
    card = _open_card(broker_quantity=100, orderable_quantity=0)
    card.board_status = BoardStatus.SELL_ALL
    card.exit_all_required = True
    holdings = [BrokerHolding(symbol="AAPL", quantity=60, average_price=100.0)]  # partial fill happened

    changed = PositionManager().reconcile_broker_positions(
        [card], holdings, environment="PROD", account_no="1"
    )

    assert changed == [card]
    assert card.broker_quantity == 60
    assert card.board_status == BoardStatus.SELL_ALL  # intent preserved
    assert card.exit_all_required is True


def test_reconcile_preserves_partial_sell_and_entry_pending_intent():
    partial = _open_card(symbol="AAPL", broker_quantity=100)
    partial.board_status = BoardStatus.PARTIAL_SELL
    pending = _open_card(
        symbol="MSFT", broker_quantity=0, orderable_quantity=0, average_entry_price=0.0
    )
    pending.board_status = BoardStatus.ENTRY_PENDING
    holdings = [
        BrokerHolding(symbol="AAPL", quantity=100, average_price=100.0),
        BrokerHolding(symbol="MSFT", quantity=0, average_price=0.0),
    ]

    changed = PositionManager().reconcile_broker_positions(
        [partial, pending], holdings, environment="PROD", account_no="1"
    )

    assert changed == []
    assert partial.board_status == BoardStatus.PARTIAL_SELL
    assert pending.board_status == BoardStatus.ENTRY_PENDING


def test_reconcile_detects_average_price_change_even_when_quantity_matches():
    card = _open_card(symbol="AAPL", broker_quantity=100, average_entry_price=100.0)
    holdings = [BrokerHolding(symbol="AAPL", quantity=100, average_price=105.0)]

    changed = PositionManager().reconcile_broker_positions(
        [card], holdings, environment="PROD", account_no="1"
    )

    assert changed == [card]
    assert card.average_entry_price == 105.0


def test_reconcile_sell_all_position_fully_liquidated_zeroes_quantity_without_forcing_closed():
    card = _open_card(broker_quantity=100, orderable_quantity=0)
    card.board_status = BoardStatus.SELL_ALL

    changed = PositionManager().reconcile_broker_positions(
        [card], [], environment="PROD", account_no="1"
    )

    assert changed == [card]
    assert card.broker_quantity == 0
    # Reconciliation zeroes the quantity but does not itself flip the card
    # to Closed -- that stays owned by the Sell All retry stage so the rest
    # of its bookkeeping (stop clearing etc.) happens through one path.
    assert card.board_status == BoardStatus.SELL_ALL
