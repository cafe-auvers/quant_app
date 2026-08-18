"""Tests for src.core.kanban_transitions."""
from __future__ import annotations

import pytest

from src.core.kanban_transitions import (
    DuplicateCardError,
    InvalidBoardTransitionError,
    migrate_legacy_status_to_board_status,
    require_single_card_per_symbol,
    restore_closed_card_membership,
    validate_board_transition,
    validate_single_card_per_symbol,
)
from src.core.trade_card_state import BoardStatus, TradeCardState


def _card(symbol="AAPL", account_no="1", board_status=BoardStatus.WATCHLIST, **kw):
    return TradeCardState(
        environment="PROD",
        account_no=account_no,
        symbol=symbol,
        board_status=board_status,
        **kw,
    )


# --- Kanban column-move tests (spec section 26) -----------------------------


def test_watchlist_to_buylist_allowed():
    validate_board_transition(BoardStatus.WATCHLIST, BoardStatus.BUYLIST)


def test_buylist_to_buy_today_allowed():
    validate_board_transition(BoardStatus.BUYLIST, BoardStatus.BUY_TODAY)


def test_buy_today_to_buylist_without_order_allowed():
    validate_board_transition(BoardStatus.BUY_TODAY, BoardStatus.BUYLIST)


def test_buy_today_to_open_position_directly_blocked():
    """An unresolved order exists in between -- entry pending is mandatory."""
    with pytest.raises(InvalidBoardTransitionError):
        validate_board_transition(BoardStatus.BUY_TODAY, BoardStatus.OPEN_POSITION)


def test_watchlist_to_buy_today_directly_blocked():
    with pytest.raises(InvalidBoardTransitionError):
        validate_board_transition(BoardStatus.WATCHLIST, BoardStatus.BUY_TODAY)


def test_sell_all_only_reaches_closed_from_itself():
    validate_board_transition(BoardStatus.SELL_ALL, BoardStatus.CLOSED)
    with pytest.raises(InvalidBoardTransitionError):
        validate_board_transition(BoardStatus.OPEN_POSITION, BoardStatus.CLOSED)


def test_partial_sell_can_escalate_to_sell_all_on_stop_trigger():
    validate_board_transition(BoardStatus.PARTIAL_SELL, BoardStatus.SELL_ALL)


def test_unsubmitted_sell_all_can_be_reduced_to_partial_sell():
    validate_board_transition(BoardStatus.SELL_ALL, BoardStatus.PARTIAL_SELL)


def test_closed_has_no_direct_outgoing_drag_transition():
    with pytest.raises(InvalidBoardTransitionError):
        validate_board_transition(BoardStatus.CLOSED, BoardStatus.BUYLIST)


def test_same_status_move_is_a_no_op():
    validate_board_transition(BoardStatus.OPEN_POSITION, BoardStatus.OPEN_POSITION)


def test_restore_closed_card_membership():
    stuck = _card(board_status=BoardStatus.CLOSED, return_to_buylist_after_close=False)
    assert restore_closed_card_membership(stuck) == BoardStatus.CLOSED

    returning = _card(board_status=BoardStatus.CLOSED, return_to_buylist_after_close=True)
    assert restore_closed_card_membership(returning) == BoardStatus.BUYLIST


# --- One symbol cannot exist in two visible columns (spec section 43-47) ---


def test_single_card_per_symbol_holds_for_distinct_symbols():
    cards = [_card(symbol="AAPL"), _card(symbol="MSFT")]
    assert validate_single_card_per_symbol(cards) == []
    require_single_card_per_symbol(cards)  # does not raise


def test_duplicate_card_for_same_symbol_detected():
    cards = [
        _card(symbol="AAPL", board_status=BoardStatus.BUYLIST),
        _card(symbol="AAPL", board_status=BoardStatus.OPEN_POSITION),
    ]
    violations = validate_single_card_per_symbol(cards)
    assert len(violations) == 1
    with pytest.raises(DuplicateCardError):
        require_single_card_per_symbol(cards)


def test_same_symbol_different_account_is_not_a_duplicate():
    cards = [_card(symbol="AAPL", account_no="1"), _card(symbol="AAPL", account_no="2")]
    assert validate_single_card_per_symbol(cards) == []


# --- Migration mapping (spec section 25 table) ------------------------------


@pytest.mark.parametrize(
    "monitoring_status,orb_monitor_enabled,shares_held,expected",
    [
        ("WATCHING", False, 0, BoardStatus.BUYLIST),
        ("ACTIVE", False, 0, BoardStatus.BUY_TODAY),
        ("WATCHING", True, 0, BoardStatus.BUY_TODAY),  # orb_monitor_enabled wins
        ("ORDER_PENDING", False, 0, BoardStatus.ENTRY_PENDING),
        ("ORDER_SUBMITTED", False, 0, BoardStatus.ENTRY_PENDING),
        ("UNKNOWN_SUBMISSION_STATE", False, 0, BoardStatus.ENTRY_PENDING),
        ("BUY_PARTIAL", False, 50, BoardStatus.OPEN_POSITION),
        ("BUY_PARTIAL", False, 0, BoardStatus.BUYLIST),
        ("BOUGHT", False, 100, BoardStatus.OPEN_POSITION),
        ("PARTIAL_EXIT_SUBMITTED", False, 50, BoardStatus.PARTIAL_SELL),
        ("SELL_SUBMITTED", False, 100, BoardStatus.SELL_ALL),
        ("SELL_RESERVED", False, 100, BoardStatus.SELL_ALL),
        ("SOLD", False, 0, BoardStatus.CLOSED),
    ],
)
def test_migration_mapping_table(monitoring_status, orb_monitor_enabled, shares_held, expected):
    assert (
        migrate_legacy_status_to_board_status(
            monitoring_status,
            orb_monitor_enabled=orb_monitor_enabled,
            shares_held=shares_held,
        )
        == expected
    )


def test_unrecognized_status_with_broker_position_favors_open_position():
    assert (
        migrate_legacy_status_to_board_status("ERROR", shares_held=25)
        == BoardStatus.OPEN_POSITION
    )


def test_unrecognized_status_without_position_defaults_to_buylist():
    assert migrate_legacy_status_to_board_status("ERROR", shares_held=0) == BoardStatus.BUYLIST
