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
    assert card.risk_percent == pytest.approx(0.01)
    assert card.to_dict()["risk_unit"] == "fraction"


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
        rejected_orb_snapshot={
            "session_date": "2026-08-17",
            "combinations": [{"window": "1m", "reason": "rejected"}],
        },
        kis_ws_symbol_key="DNASAAPL",
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
    assert restored.rejected_orb_snapshot == card.rejected_orb_snapshot
    assert restored.kis_ws_symbol_key == "DNASAAPL"


def test_non_finite_floats_are_dropped_to_none():
    card = _make_card(breakout_price=float("nan"), entry_orb_high=float("inf"))
    assert card.breakout_price is None
    assert card.entry_orb_high is None


def test_unknown_enum_strings_fall_back_to_default_on_load():
    data = _make_card().to_dict()
    data["board_status"] = "NOT_A_REAL_STATUS"
    restored = TradeCardState.from_dict(data)
    assert restored.board_status == BoardStatus.WATCHLIST


def test_unmarked_legacy_risk_percentage_points_migrate_once_to_fraction():
    legacy = _make_card().to_dict()
    legacy.pop("risk_unit")
    legacy["risk_percent"] = 1.0

    restored = TradeCardState.from_dict(legacy)

    assert restored.risk_percent == pytest.approx(0.01)
    assert restored.to_dict()["risk_unit"] == "fraction"
    assert TradeCardState.from_dict(restored.to_dict()).risk_percent == pytest.approx(
        0.01
    )


def test_unmarked_custom_orb_risk_above_standard_grid_remains_canonical():
    payload = _make_card(
        risk_percent=0.03,
        selected_orb_window="5m",
        entry_orb_high=101.0,
        entry_orb_low=99.0,
        entry_trigger=101.1,
    ).to_dict()
    payload.pop("risk_unit")

    restored = TradeCardState.from_dict(payload)

    assert restored.risk_percent == pytest.approx(0.03)
    assert restored.to_dict()["risk_unit"] == "fraction"


def test_unmarked_passive_buylist_risk_above_standard_grid_migrates():
    payload = _make_card(risk_percent=0.5).to_dict()
    payload.pop("risk_unit")

    assert TradeCardState.from_dict(payload).risk_percent == pytest.approx(0.005)


def test_marked_canonical_risk_fraction_is_never_double_converted():
    payload = _make_card(risk_percent=0.4).to_dict()

    assert TradeCardState.from_dict(payload).risk_percent == pytest.approx(0.4)


@pytest.mark.parametrize("risk", [-0.01, 1.01, float("nan"), float("inf")])
def test_trade_card_rejects_or_repairs_invalid_risk_fraction(risk):
    if risk != risk or risk == float("inf"):
        assert _make_card(risk_percent=risk).risk_percent == pytest.approx(0.01)
    else:
        with pytest.raises(ValueError, match="account-risk fraction"):
            _make_card(risk_percent=risk)
