"""Tests for src.core.trade_card_state.TradeCardState."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.core.trade_card_state import (
    BoardStatus,
    EntryRuntimeStatus,
    PositionRuntimeStatus,
    StopType,
    TradeCardState,
)


def _make_card(**overrides) -> TradeCardState:
    fields = dict(environment="PROD", account_no="12345678-01", symbol="aapl")
    fields.update(overrides)
    return TradeCardState(**fields)


def test_symbol_is_upper_cased_and_required():
    card = _make_card(symbol="aapl")
    assert card.symbol == "AAPL"
    with pytest.raises(ValueError):
        _make_card(symbol="")


def test_non_prod_environment_rejected():
    with pytest.raises(ValueError):
        _make_card(environment="SIM")


def test_defaults():
    card = _make_card()
    assert card.board_status == BoardStatus.WATCHLIST
    assert card.previous_board_status is None
    assert card.version == 1
    assert card.kanban_priority == 0
    assert card.position_runtime_status == PositionRuntimeStatus.NONE
    assert card.stop_type is None
    assert card.exit_all_required is False
    assert card.card_key == "PROD:12345678-01:AAPL"


def test_to_dict_from_dict_round_trip():
    card = _make_card(
        name="Apple Inc.",
        board_status=BoardStatus.OPEN_POSITION,
        previous_board_status=BoardStatus.ENTRY_PENDING,
        entry_runtime_status=EntryRuntimeStatus.EXECUTE_READY,
        position_runtime_status=PositionRuntimeStatus.OPEN,
        broker_quantity=100,
        orderable_quantity=100,
        average_entry_price=190.25,
        stop_type=StopType.ORB_LOW,
        active_stop_price=188.0,
        stop_quantity=100,
        pending_stop_type=StopType.MANUAL_PRICE,
        pending_stop_price=189.0,
        pending_stop_quantity=100,
        pending_stop_command_id="STOP-1",
        pending_stop_requested_at=datetime(2026, 8, 17, tzinfo=timezone.utc),
        exit_all_required=False,
        buy_today_note="All ORB plans were invalid.",
        warnings=["migrated_from_buylist"],
    )
    restored = TradeCardState.from_dict(card.to_dict())
    assert restored.to_dict() == card.to_dict()
    assert restored.board_status == BoardStatus.OPEN_POSITION
    assert restored.previous_board_status == BoardStatus.ENTRY_PENDING
    assert restored.entry_runtime_status == EntryRuntimeStatus.EXECUTE_READY
    assert restored.stop_type == StopType.ORB_LOW
    assert restored.pending_stop_type == StopType.MANUAL_PRICE
    assert restored.pending_stop_command_id == "STOP-1"
    assert restored.buy_today_note == "All ORB plans were invalid."


def test_non_finite_floats_are_dropped_to_none():
    card = _make_card(breakout_price=float("nan"), entry_orb_high=float("inf"))
    assert card.breakout_price is None
    assert card.entry_orb_high is None


def test_unknown_enum_strings_fall_back_to_default_on_load():
    data = _make_card().to_dict()
    data["board_status"] = "NOT_A_REAL_STATUS"
    restored = TradeCardState.from_dict(data)
    assert restored.board_status == BoardStatus.WATCHLIST
