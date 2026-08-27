"""Tests for src.services.trade_card_orb_bridge (code review finding P0-2)."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from src.core.execution_queue import ExecutionQueueItem, OrbCandidate, OrbCandidateStatus
from src.core.trade_card_state import BoardStatus, EntryRuntimeStatus, TradeCardState
from src.services.trade_card_orb_bridge import (
    TradeCardOrbEvaluator,
    orb_candidate_stale_for_current_session,
)


def _card(**overrides):
    fields = dict(
        environment="PROD",
        account_no="1",
        symbol="AAPL",
        board_status=BoardStatus.BUY_TODAY,
        breakout_price=101.5,
    )
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
        source_session_date=datetime.now(ZoneInfo("America/New_York")).date().isoformat(),
        stop_adr=45.0,
        shares=20,
        risk_percent=0.01,
        status=OrbCandidateStatus.EXECUTE_READY,
        valid=True,
        terminal_rejection=False,
    )
    fields.update(overrides)
    return OrbCandidate(**fields)


def _queue_item(candidate=None, **overrides):
    fields = dict(
        symbol="AAPL",
        environment="PROD",
        account_no="1",
        name="Apple Inc.",
        breakout_price=101.5,
    )
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
    item = _queue_item(
        _candidate(
            status=OrbCandidateStatus.FORMING,
            shares=0,
            source_session_date="2026-08-19",
        )
    )

    now = datetime(2026, 8, 19, 14, 0, tzinfo=timezone.utc)  # 10:00 ET
    TradeCardOrbEvaluator(clock=lambda: now).update_card(card, item)
    assert card.entry_runtime_status == EntryRuntimeStatus.ORB_FORMING


def test_unavailable_candidate_status_maps_to_data_unavailable_with_reason():
    card = _card()
    item = _queue_item(
        _candidate(
            status=OrbCandidateStatus.NOT_AVAILABLE,
            valid=False,
            shares=0,
            reason="09:30 opening bar is unavailable",
            source_session_date="2026-08-19",
        )
    )

    now = datetime(2026, 8, 19, 14, 0, tzinfo=timezone.utc)  # 10:00 ET
    TradeCardOrbEvaluator(clock=lambda: now).update_card(card, item)

    assert card.entry_runtime_status == EntryRuntimeStatus.DATA_UNAVAILABLE
    assert card.entry_block_reason == "09:30 opening bar is unavailable"


def test_waiting_breakout_status_maps_correctly():
    card = _card()
    item = _queue_item(_candidate(status=OrbCandidateStatus.WAITING_BREAKOUT))

    TradeCardOrbEvaluator().update_card(card, item)
    assert card.entry_runtime_status == EntryRuntimeStatus.WAITING_BREAKOUT


def test_periodic_orb_sync_cannot_bypass_retry_cooldown():
    now = datetime(2026, 8, 19, 14, 5, tzinfo=timezone.utc)
    candidate = _candidate(
        window="5m",
        orb_high=101.5,
        orb_low=95.0,
        breakout_price=100.0,
        entry_trigger=101.5,
        source_session_date="2026-08-19",
        status=OrbCandidateStatus.EXECUTE_READY,
    )
    card = _card(
        breakout_price=100.0,
        entry_runtime_status=EntryRuntimeStatus.RETRY_COOLDOWN,
        entry_block_reason="Broker rejected the previous attempt",
        next_retry_at=now + timedelta(seconds=30),
    )
    item = _queue_item(
        candidate,
        breakout_price=100.0,
        candidates={"5m": candidate},
        last_updated=now,
    )

    TradeCardOrbEvaluator(clock=lambda: now).update_card(card, item)

    assert card.entry_runtime_status == EntryRuntimeStatus.RETRY_COOLDOWN
    assert card.entry_block_reason == "Broker rejected the previous attempt"
    assert card.entry_trigger == 101.5
    assert card.entry_orb_low == 95.0


def test_live_cross_selects_lower_trigger_risk_valid_window_in_auto_mode():
    now = datetime(2026, 8, 19, 14, 5, tzinfo=timezone.utc)
    one_minute = _candidate(
        window="1m",
        orb_high=197.71,
        orb_low=190.0,
        breakout_price=190.0,
        entry_trigger=197.71,
        source_session_date="2026-08-19",
        score=10.0,
        status=OrbCandidateStatus.WAITING_BREAKOUT,
    )
    five_minute = _candidate(
        window="5m",
        orb_high=200.0,
        orb_low=192.0,
        breakout_price=190.0,
        entry_trigger=200.0,
        source_session_date="2026-08-19",
        score=20.0,
        status=OrbCandidateStatus.WAITING_BREAKOUT,
    )
    card = _card(breakout_price=190.0, buffer_pct=0.001)
    item = _queue_item(
        five_minute,
        breakout_price=190.0,
        candidates={"1m": one_minute, "5m": five_minute},
        last_updated=now,
    )
    evaluator = TradeCardOrbEvaluator(clock=lambda: now)
    evaluator.update_card(card, item)
    assert card.entry_trigger == 200.0

    changed = evaluator.select_crossed_candidate(card, item, last_price=198.0)

    assert changed is True
    assert card.selected_orb_window == "1m"
    assert card.entry_trigger == 197.71
    assert card.entry_orb_high == 197.71
    assert card.planned_quantity == one_minute.shares


def test_live_cross_respects_manual_window_lock():
    now = datetime(2026, 8, 19, 14, 5, tzinfo=timezone.utc)
    one_minute = _candidate(
        window="1m",
        orb_high=197.71,
        orb_low=190.0,
        breakout_price=190.0,
        entry_trigger=197.71,
        source_session_date="2026-08-19",
        status=OrbCandidateStatus.WAITING_BREAKOUT,
    )
    five_minute = _candidate(
        window="5m",
        orb_high=200.0,
        orb_low=192.0,
        breakout_price=190.0,
        entry_trigger=200.0,
        source_session_date="2026-08-19",
        status=OrbCandidateStatus.WAITING_BREAKOUT,
    )
    card = _card(breakout_price=190.0, buffer_pct=0.001)
    item = _queue_item(
        five_minute,
        breakout_price=190.0,
        candidates={"1m": one_minute, "5m": five_minute},
        selected_window="5m",
        manual_window_lock=True,
        locked=True,
        last_updated=now,
    )
    evaluator = TradeCardOrbEvaluator(clock=lambda: now)
    evaluator.update_card(card, item)

    changed = evaluator.select_crossed_candidate(card, item, last_price=198.0)

    assert changed is False
    assert card.selected_orb_window == "5m"
    assert card.entry_trigger == 200.0


def test_live_cross_does_not_switch_a_nonmanual_order_locked_queue_row():
    now = datetime(2026, 8, 19, 14, 5, tzinfo=timezone.utc)
    candidate = _candidate(
        window="1m",
        orb_high=197.71,
        orb_low=190.0,
        breakout_price=190.0,
        entry_trigger=197.71,
        source_session_date="2026-08-19",
        status=OrbCandidateStatus.WAITING_BREAKOUT,
    )
    card = _card(
        breakout_price=190.0,
        buffer_pct=0.001,
        entry_runtime_status=EntryRuntimeStatus.WAITING_BREAKOUT,
    )
    item = _queue_item(
        candidate,
        breakout_price=190.0,
        candidates={"1m": candidate},
        locked=True,
        manual_window_lock=False,
        order_status="SUBMITTED",
        last_updated=now,
    )

    changed = TradeCardOrbEvaluator(clock=lambda: now).select_crossed_candidate(
        card,
        item,
        last_price=198.0,
    )

    assert changed is False
    assert card.entry_trigger is None


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


def test_all_three_terminal_invalid_orb_plans_return_card_to_buylist_with_note():
    now = datetime(2026, 8, 19, 14, 5, tzinfo=timezone.utc)  # 10:05 ET
    candidates = {
        "1m": _candidate(
            window="1m",
            source_session_date="2026-08-19",
            status=OrbCandidateStatus.REJECTED,
            valid=False,
            terminal_rejection=True,
            reason="ORB high did not clear breakout",
        ),
        "5m": _candidate(
            window="5m",
            source_session_date="2026-08-19",
            status=OrbCandidateStatus.RISK_INVALID,
            valid=False,
            terminal_rejection=True,
            reason="stop is too wide",
        ),
        "30m": _candidate(
            window="30m",
            source_session_date="2026-08-19",
            status=OrbCandidateStatus.REJECTED,
            valid=False,
            terminal_rejection=True,
            reason="ORB strategy rejected",
        ),
    }
    card = _card(
        entry_trigger=101.0,
        entry_orb_high=101.0,
        entry_orb_low=95.0,
        planned_quantity=20,
        target_position_quantity=20,
    )
    item = _queue_item(None, candidates=candidates, last_updated=now)

    TradeCardOrbEvaluator(clock=lambda: now).update_card(card, item)

    assert card.board_status == BoardStatus.BUYLIST
    assert card.previous_board_status == BoardStatus.BUY_TODAY
    assert card.entry_runtime_status is None
    assert card.entry_trigger is None
    assert card.planned_quantity == 0
    assert card.session_date is None
    assert card.buylist_member is True
    assert card.last_buy_today_session_date == date(2026, 8, 19)
    assert "all ORB plans invalid" in card.buy_today_note
    assert "1m: ORB high did not clear breakout" in card.buy_today_note
    assert "5m: stop is too wide" in card.buy_today_note
    assert "30m: ORB strategy rejected" in card.buy_today_note
    assert card.rejected_orb_snapshot["session_date"] == "2026-08-19"
    assert len(card.rejected_orb_snapshot["combinations"]) == 24
    assert set(card.rejected_orb_snapshot["queue_item"]["candidates"]) == {
        "1m",
        "5m",
        "30m",
    }
    assert all(
        combination["status"] in {"REJECTED", "RISK_INVALID"}
        for combination in card.rejected_orb_snapshot["combinations"]
    )


def test_forming_window_prevents_automatic_return_to_buylist():
    now = datetime(2026, 8, 19, 14, 5, tzinfo=timezone.utc)  # 10:05 ET
    candidates = {
        "1m": _candidate(
            window="1m",
            source_session_date="2026-08-19",
            status=OrbCandidateStatus.REJECTED,
            valid=False,
        ),
        "5m": _candidate(
            window="5m",
            source_session_date="2026-08-19",
            status=OrbCandidateStatus.RISK_INVALID,
            valid=False,
        ),
        "30m": _candidate(
            window="30m",
            source_session_date="2026-08-19",
            status=OrbCandidateStatus.FORMING,
            valid=False,
        ),
    }
    card = _card()
    item = _queue_item(None, candidates=candidates, last_updated=now)

    TradeCardOrbEvaluator(clock=lambda: now).update_card(card, item)

    assert card.board_status == BoardStatus.BUY_TODAY
    assert card.buy_today_note == ""


def test_legacy_terminal_status_without_explicit_proof_stays_in_buy_today():
    now = datetime(2026, 8, 19, 14, 5, tzinfo=timezone.utc)
    candidates = {
        window: _candidate(
            window=window,
            source_session_date="2026-08-19",
            status=OrbCandidateStatus.REJECTED,
            valid=False,
            terminal_rejection=False,
            reason="legacy rejection without sizing proof",
        )
        for window in ("1m", "5m", "30m")
    }
    card = _card()
    item = _queue_item(None, candidates=candidates, last_updated=now)

    TradeCardOrbEvaluator(clock=lambda: now).update_card(card, item)

    assert card.board_status == BoardStatus.BUY_TODAY
    assert card.buy_today_note == ""


def test_duplicate_order_rejections_do_not_auto_return_card_to_buylist():
    now = datetime(2026, 8, 19, 14, 5, tzinfo=timezone.utc)
    candidates = {
        window: _candidate(
            window=window,
            source_session_date="2026-08-19",
            status=OrbCandidateStatus.REJECTED,
            valid=False,
            terminal_rejection=False,
            reason="Duplicate pending/submitted order exists for symbol",
        )
        for window in ("1m", "5m", "30m")
    }
    card = _card()
    item = _queue_item(None, candidates=candidates, last_updated=now)

    TradeCardOrbEvaluator(clock=lambda: now).update_card(card, item)

    assert card.board_status == BoardStatus.BUY_TODAY
    assert card.buy_today_note == ""


def test_terminal_orb_rejection_never_hides_an_existing_entry_identity():
    now = datetime(2026, 8, 19, 14, 5, tzinfo=timezone.utc)  # 10:05 ET
    candidates = {
        window: _candidate(
            window=window,
            source_session_date="2026-08-19",
            status=OrbCandidateStatus.REJECTED,
            valid=False,
            terminal_rejection=True,
            reason="invalid",
        )
        for window in ("1m", "5m", "30m")
    }
    card = _card(entry_client_order_id="known-buy-order")
    item = _queue_item(None, candidates=candidates, last_updated=now)

    TradeCardOrbEvaluator(clock=lambda: now).update_card(card, item)

    assert card.board_status == BoardStatus.BUY_TODAY
    assert card.entry_client_order_id == "known-buy-order"


def test_no_selected_candidate_yet_leaves_card_forming():
    card = _card()
    item = _queue_item(None)

    now = datetime(2026, 8, 19, 14, 0, tzinfo=timezone.utc)  # 10:00 ET
    TradeCardOrbEvaluator(clock=lambda: now).update_card(card, item)
    assert card.entry_runtime_status == EntryRuntimeStatus.ORB_FORMING
    assert card.entry_orb_low is None  # nothing to size an entry off of yet


def test_expired_orb_snapshot_cannot_fall_back_to_durable_target_plan():
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

    assert card.entry_runtime_status == EntryRuntimeStatus.DATA_UNAVAILABLE
    assert "Current-session" in card.entry_block_reason
    assert card.breakout_price == 101.5
    assert card.market_data_last_trusted_price == 102.25
    assert card.entry_trigger is None
    assert card.entry_orb_high is None
    assert card.entry_orb_low is None
    assert card.planned_quantity == 0
    assert card.target_position_quantity == 0


def test_elapsed_forming_orb_never_falls_back_to_target_allocation_plan():
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
            source_session_date="2026-08-19",
        ),
        selected_window="1m",
        last_updated=now,
    )

    TradeCardOrbEvaluator(clock=lambda: now).update_card(card, item)

    assert card.entry_runtime_status == EntryRuntimeStatus.ORB_FORMING
    assert card.breakout_price == 101.5
    assert card.position_percent == 20.0
    assert card.selected_orb_window == "1m"


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
            source_session_date="2026-08-19",
        ),
        last_updated=datetime(2026, 8, 19, 12, 30, tzinfo=timezone.utc),
    )
    card = _card()

    TradeCardOrbEvaluator(clock=lambda: now).update_card(card, item)

    assert card.entry_runtime_status == EntryRuntimeStatus.ORB_FORMING


def test_yesterday_source_bars_are_rejected_even_if_queue_refreshed_today():
    now = datetime(2026, 8, 19, 13, 35, tzinfo=timezone.utc)  # 09:35 ET
    candidate = _candidate(
        window="1m",
        source_session_date="2026-08-18",
    )
    item = _queue_item(candidate, last_updated=now)
    card = _card()

    assert orb_candidate_stale_for_current_session(item, candidate, now=now)

    TradeCardOrbEvaluator(clock=lambda: now).update_card(card, item)

    assert card.entry_runtime_status == EntryRuntimeStatus.DATA_UNAVAILABLE
    assert card.entry_trigger is None
    assert card.entry_orb_high is None
    assert card.entry_orb_low is None
    assert card.planned_quantity == 0
    assert "Current-session" in card.entry_block_reason


def test_current_session_source_bars_remain_eligible():
    now = datetime(2026, 8, 19, 13, 35, tzinfo=timezone.utc)  # 09:35 ET
    candidate = _candidate(
        window="1m",
        source_session_date="2026-08-19",
    )
    item = _queue_item(
        candidate,
        last_updated=datetime(2026, 8, 18, 13, 35, tzinfo=timezone.utc),
    )
    card = _card()

    assert not orb_candidate_stale_for_current_session(item, candidate, now=now)

    TradeCardOrbEvaluator(clock=lambda: now).update_card(card, item)

    assert card.entry_runtime_status == EntryRuntimeStatus.EXECUTE_READY
    assert card.entry_trigger == candidate.entry_trigger


def test_queue_candidate_sized_for_another_account_is_never_applied():
    card = _card(account_no="account-2")
    item = _queue_item(_candidate(), account_no="account-1")

    TradeCardOrbEvaluator().update_card(card, item)

    assert card.entry_runtime_status == EntryRuntimeStatus.RISK_INVALID
    assert card.entry_trigger is None
    assert "not this card's account" in card.entry_block_reason


def test_queue_candidate_without_account_provenance_is_never_applied():
    card = _card(entry_trigger=99.0, planned_quantity=99)
    item = _queue_item(_candidate(), account_no="")

    TradeCardOrbEvaluator().update_card(card, item)

    assert card.entry_runtime_status == EntryRuntimeStatus.RISK_INVALID
    assert card.entry_trigger is None
    assert card.planned_quantity == 0
    assert "no account sizing provenance" in card.entry_block_reason


def test_changed_canonical_target_cannot_execute_a_stale_queue_plan():
    now = datetime(2026, 8, 19, 13, 35, tzinfo=timezone.utc)
    candidate = _candidate(source_session_date="2026-08-19")
    item = _queue_item(candidate, last_updated=now)
    card = _card(
        breakout_price=102.0,
        entry_trigger=101.6,
        entry_orb_high=101.6,
        entry_orb_low=95.0,
        planned_quantity=20,
        target_position_quantity=20,
    )

    TradeCardOrbEvaluator(clock=lambda: now).update_card(card, item)

    assert card.breakout_price == 102.0
    assert card.entry_runtime_status == EntryRuntimeStatus.DATA_UNAVAILABLE
    assert card.entry_trigger is None
    assert card.entry_orb_high is None
    assert card.entry_orb_low is None
    assert card.planned_quantity == 0
    assert "does not match" in card.entry_block_reason


def test_missing_canonical_target_is_never_restored_from_queue():
    card = _card(
        breakout_price=None,
        entry_trigger=101.6,
        planned_quantity=20,
    )
    item = _queue_item(_candidate())

    TradeCardOrbEvaluator().update_card(card, item)

    assert card.breakout_price is None
    assert card.entry_runtime_status == EntryRuntimeStatus.DATA_UNAVAILABLE
    assert card.entry_trigger is None
    assert card.planned_quantity == 0
    assert "canonical breakout target is unavailable" in card.entry_block_reason


def test_candidate_without_source_session_provenance_is_never_applied():
    now = datetime(2026, 8, 19, 13, 35, tzinfo=timezone.utc)
    candidate = _candidate(source_session_date=None)
    item = _queue_item(candidate, last_updated=now)
    card = _card(entry_trigger=99.0, planned_quantity=99)

    assert orb_candidate_stale_for_current_session(item, candidate, now=now)

    TradeCardOrbEvaluator(clock=lambda: now).update_card(card, item)

    assert card.entry_runtime_status == EntryRuntimeStatus.DATA_UNAVAILABLE
    assert card.entry_trigger is None
    assert card.planned_quantity == 0


def test_forming_plan_becomes_session_complete_after_market_close():
    now = datetime(2026, 8, 19, 20, 5, tzinfo=timezone.utc)  # 16:05 ET
    card = _card()
    item = _queue_item(
        _candidate(
            status=OrbCandidateStatus.FORMING,
            shares=0,
            source_session_date="2026-08-19",
        ),
        last_updated=now,
    )

    TradeCardOrbEvaluator(clock=lambda: now).update_card(card, item)

    assert card.entry_runtime_status == EntryRuntimeStatus.SESSION_COMPLETE
    assert card.entry_block_reason == "Regular session is complete"
    assert card.planned_quantity == 0


def test_future_session_buy_today_stays_forming_after_prior_session_close():
    now = datetime(2026, 8, 21, 20, 5, tzinfo=timezone.utc)  # Friday 16:05 ET
    card = _card(session_date=date(2026, 8, 24))
    item = _queue_item(
        _candidate(
            status=OrbCandidateStatus.FORMING,
            shares=0,
            source_session_date="2026-08-21",
        ),
        last_updated=now,
    )

    TradeCardOrbEvaluator(clock=lambda: now).update_card(card, item)

    assert card.board_status == BoardStatus.BUY_TODAY
    assert card.entry_runtime_status == EntryRuntimeStatus.ORB_FORMING
    assert card.entry_block_reason == ""


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
        source_session_date="2026-08-19",
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


def test_missing_entry_trigger_never_falls_back_to_breakout_trigger():
    card = _card()
    item = _queue_item(_candidate(entry_trigger=None, breakout_trigger=102.2))

    TradeCardOrbEvaluator().update_card(card, item)
    assert card.entry_trigger is None
    assert card.entry_runtime_status == EntryRuntimeStatus.RISK_INVALID
    assert "missing executable plan fields" in card.entry_block_reason


def test_watchlist_and_buylist_cards_ignore_orb_runtime_state():
    for status in (BoardStatus.WATCHLIST, BoardStatus.BUYLIST):
        card = _card(board_status=status, breakout_price=99.0)
        item = _queue_item(_candidate())
        TradeCardOrbEvaluator().update_card(card, item)
        assert card.breakout_price == 99.0
        assert card.entry_runtime_status is None
