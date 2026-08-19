"""Tests for src.services.trade_card_orb_bridge (code review finding P0-2)."""
from __future__ import annotations

from datetime import datetime, timezone

from src.core.execution_queue import ExecutionQueueItem, OrbCandidate, OrbCandidateStatus
from src.core.trade_card_state import BoardStatus, EntryRuntimeStatus, TradeCardState
from src.services.trade_card_orb_bridge import TradeCardOrbEvaluator


def _card(**overrides):
    fields = dict(environment="PROD", account_no="1", symbol="AAPL", board_status=BoardStatus.BUY_TODAY)
    fields.update(overrides)
    return TradeCardState(**fields)


def _candidate(**overrides):
    fields = dict(
        symbol="AAPL",
        window="5m",
        orb_high=101.0,
        orb_low=95.0,
        breakout_price=101.5,
        entry_trigger=101.6,
        stop_adr=45.0,
        shares=20,
        risk_percent=0.01,
        status=OrbCandidateStatus.EXECUTE_READY,
        valid=True,
    )
    fields.update(overrides)
    return OrbCandidate(**fields)


def _queue_item(candidate=None, **overrides):
    fields = dict(symbol="AAPL", environment="PROD", name="Apple Inc.", breakout_price=101.5)
    fields.update(overrides)
    item = ExecutionQueueItem(**fields)
    item.selected_candidate = candidate
    if candidate is not None:
        item.selected_window = candidate.window
    return item


def test_execute_ready_candidate_populates_entry_plan_fields():
    card = _card()
    item = _queue_item(_candidate())

    result = TradeCardOrbEvaluator().update_card(card, item)

    assert result is card
    assert card.entry_runtime_status == EntryRuntimeStatus.EXECUTE_READY
    assert card.selected_orb_window == "5m"
    assert card.entry_orb_high == 101.0
    assert card.entry_orb_low == 95.0
    assert card.entry_trigger == 101.6
    assert card.stop_adr == 45.0
    assert card.planned_quantity == 20
    assert card.target_position_quantity == 20
    assert card.risk_percent == 0.01
    assert card.name == "Apple Inc."
    assert card.entry_block_reason == ""


def test_forming_candidate_status_maps_to_orb_forming():
    card = _card()
    item = _queue_item(_candidate(status=OrbCandidateStatus.FORMING, shares=0))

    now = datetime(2026, 8, 19, 14, 0, tzinfo=timezone.utc)  # 10:00 ET
    TradeCardOrbEvaluator(clock=lambda: now).update_card(card, item)
    assert card.entry_runtime_status == EntryRuntimeStatus.ORB_FORMING


def test_waiting_breakout_status_maps_correctly():
    card = _card()
    item = _queue_item(_candidate(status=OrbCandidateStatus.WAITING_BREAKOUT))

    TradeCardOrbEvaluator().update_card(card, item)
    assert card.entry_runtime_status == EntryRuntimeStatus.WAITING_BREAKOUT


def test_valid_status_maps_to_armed():
    card = _card()
    item = _queue_item(_candidate(status=OrbCandidateStatus.VALID))

    TradeCardOrbEvaluator().update_card(card, item)
    assert card.entry_runtime_status == EntryRuntimeStatus.ARMED


def test_risk_invalid_candidate_blocks_with_reason():
    card = _card()
    item = _queue_item(
        _candidate(status=OrbCandidateStatus.RISK_INVALID, reason="Capital allocation exceeds 30%")
    )

    TradeCardOrbEvaluator().update_card(card, item)
    assert card.entry_runtime_status == EntryRuntimeStatus.RISK_INVALID
    assert card.entry_block_reason == "Capital allocation exceeds 30%"


def test_rejected_candidate_blocks_with_reason():
    card = _card()
    item = _queue_item(_candidate(status=OrbCandidateStatus.REJECTED, reason="No valid ORB window"))

    TradeCardOrbEvaluator().update_card(card, item)
    assert card.entry_runtime_status == EntryRuntimeStatus.RISK_INVALID
    assert card.entry_block_reason == "No valid ORB window"


def test_no_selected_candidate_yet_leaves_card_forming():
    card = _card()
    item = _queue_item(None)

    now = datetime(2026, 8, 19, 14, 0, tzinfo=timezone.utc)  # 10:00 ET
    TradeCardOrbEvaluator(clock=lambda: now).update_card(card, item)
    assert card.entry_runtime_status == EntryRuntimeStatus.ORB_FORMING
    assert card.entry_orb_low is None  # nothing to size an entry off of yet


def test_expired_orb_snapshot_keeps_durable_target_plan_executable():
    now = datetime(2026, 8, 19, 14, 1, tzinfo=timezone.utc)  # 10:01 ET
    card = _card(
        entry_trigger=99.0,
        entry_orb_high=99.0,
        entry_orb_low=95.0,
        planned_quantity=25,
        target_position_quantity=25,
    )
    forming = {
        window: _candidate(
            window=window,
            orb_high=None,
            orb_low=None,
            entry_trigger=None,
            breakout_trigger=None,
            shares=0,
            status=OrbCandidateStatus.FORMING,
            valid=False,
        )
        for window in ("1m", "5m", "30m")
    }
    item = _queue_item(
        None,
        current_price=102.25,
        candidates=forming,
        selected_window="1m",
        last_updated=datetime(2026, 8, 19, 12, 30, tzinfo=timezone.utc),
    )

    TradeCardOrbEvaluator(clock=lambda: now).update_card(card, item)

    assert card.entry_runtime_status == EntryRuntimeStatus.EXECUTE_READY
    assert card.entry_block_reason == ""
    assert card.breakout_price == 101.5
    assert card.market_data_last_trusted_price == 102.25
    assert card.entry_trigger is None
    assert card.entry_orb_high is None
    assert card.entry_orb_low is None
    assert card.planned_quantity == 25
    assert card.target_position_quantity == 25


def test_elapsed_forming_orb_falls_back_to_target_allocation_plan():
    now = datetime(2026, 8, 19, 14, 0, tzinfo=timezone.utc)  # 10:00 ET
    card = _card(position_percent=20.0)
    item = _queue_item(
        _candidate(
            window="1m",
            orb_high=None,
            orb_low=None,
            entry_trigger=None,
            breakout_trigger=None,
            shares=0,
            status=OrbCandidateStatus.FORMING,
            valid=False,
        ),
        selected_window="1m",
        last_updated=now,
    )

    TradeCardOrbEvaluator(clock=lambda: now).update_card(card, item)

    assert card.entry_runtime_status == EntryRuntimeStatus.EXECUTE_READY
    assert card.breakout_price == 101.5
    assert card.position_percent == 20.0
    assert card.selected_orb_window is None


def test_preopen_snapshot_remains_forming_before_the_session_starts():
    now = datetime(2026, 8, 19, 13, 0, tzinfo=timezone.utc)  # 09:00 ET
    item = _queue_item(
        _candidate(
            window="1m",
            orb_high=None,
            orb_low=None,
            shares=0,
            status=OrbCandidateStatus.FORMING,
            valid=False,
        ),
        last_updated=datetime(2026, 8, 19, 12, 30, tzinfo=timezone.utc),
    )
    card = _card()

    TradeCardOrbEvaluator(clock=lambda: now).update_card(card, item)

    assert card.entry_runtime_status == EntryRuntimeStatus.ORB_FORMING


def test_forming_plan_becomes_session_complete_after_market_close():
    now = datetime(2026, 8, 19, 20, 5, tzinfo=timezone.utc)  # 16:05 ET
    card = _card()
    item = _queue_item(
        _candidate(
            status=OrbCandidateStatus.FORMING,
            shares=0,
        ),
        last_updated=now,
    )

    TradeCardOrbEvaluator(clock=lambda: now).update_card(card, item)

    assert card.entry_runtime_status == EntryRuntimeStatus.SESSION_COMPLETE
    assert card.entry_block_reason == "Regular session is complete"
    assert card.planned_quantity == 0


def test_best_waiting_plan_is_visible_before_it_becomes_execute_ready():
    card = _card()
    one_minute = _candidate(
        window="1m",
        status=OrbCandidateStatus.WAITING_BREAKOUT,
        valid=False,
        score=10.0,
    )
    five_minute = _candidate(
        window="5m",
        status=OrbCandidateStatus.WAITING_BREAKOUT,
        valid=False,
        score=25.0,
    )
    thirty_minute = _candidate(
        window="30m",
        status=OrbCandidateStatus.FORMING,
        valid=False,
        score=0.0,
        shares=0,
    )
    item = _queue_item(
        None,
        candidates={
            "1m": one_minute,
            "5m": five_minute,
            "30m": thirty_minute,
        },
    )

    TradeCardOrbEvaluator().update_card(card, item)

    assert card.entry_runtime_status == EntryRuntimeStatus.WAITING_BREAKOUT
    assert card.selected_orb_window == "5m"
    assert card.entry_orb_low == five_minute.orb_low
    assert card.planned_quantity == five_minute.shares


def test_manual_forming_plan_keeps_its_window_visible():
    card = _card()
    forming = _candidate(
        window="30m",
        status=OrbCandidateStatus.FORMING,
        valid=False,
        shares=0,
    )
    item = _queue_item(
        None,
        candidates={"30m": forming},
        selected_window="30m",
        locked=True,
        manual_window_lock=True,
    )

    now = datetime(2026, 8, 19, 14, 0, tzinfo=timezone.utc)  # 10:00 ET
    TradeCardOrbEvaluator(clock=lambda: now).update_card(card, item)

    assert card.entry_runtime_status == EntryRuntimeStatus.ORB_FORMING
    assert card.selected_orb_window == "30m"


def test_does_not_overwrite_a_frozen_entry_plan_once_position_is_open():
    """Section 620: once a card has a real (frozen) position, the ORB
    bridge must be a complete no-op -- it must never recompute/overwrite
    the entry values that were frozen at first fill."""
    card = _card(
        board_status=BoardStatus.OPEN_POSITION,
        entry_orb_low=90.0,
        entry_orb_window="1m",
        entry_trigger=91.0,
    )
    item = _queue_item(_candidate(orb_low=999.0, window="30m", entry_trigger=999.0))

    result = TradeCardOrbEvaluator().update_card(card, item)

    assert result is card
    assert card.entry_orb_low == 90.0
    assert card.entry_orb_window == "1m"
    assert card.entry_trigger == 91.0


def test_does_not_touch_existing_warnings():
    card = _card(warnings=["STOP_REQUIRED"])
    item = _queue_item(_candidate(status=OrbCandidateStatus.RISK_INVALID, reason="bad plan"))

    TradeCardOrbEvaluator().update_card(card, item)
    assert card.warnings == ["STOP_REQUIRED"]


def test_falls_back_to_breakout_trigger_when_no_entry_trigger():
    card = _card()
    item = _queue_item(_candidate(entry_trigger=None, breakout_trigger=102.2))

    TradeCardOrbEvaluator().update_card(card, item)
    assert card.entry_trigger == 102.2


def test_watchlist_and_buylist_cards_are_also_updated():
    for status in (BoardStatus.WATCHLIST, BoardStatus.BUYLIST):
        card = _card(board_status=status)
        item = _queue_item(_candidate())
        TradeCardOrbEvaluator().update_card(card, item)
        assert card.entry_runtime_status == EntryRuntimeStatus.EXECUTE_READY
