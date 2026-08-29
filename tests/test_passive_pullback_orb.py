from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.core.execution_queue import (
    ExecutionQueueItem,
    OrbCandidate,
    OrbCandidateStatus,
)
from src.core.orb_entry_logic import passive_entry_prices, score_strictly_higher
from src.core.trade_card_state import BoardStatus, EntryRuntimeStatus, TradeCardState
from src.services.trade_card_orb_bridge import TradeCardOrbEvaluator


@pytest.mark.parametrize(
    ("breakout", "orl", "execution", "orh", "valid"),
    [
        (99.0, 95.0, 100.0, 100.0, True),
        (99.0, 95.0, 99.5, 100.0, True),
        (99.0, 95.0, 99.0, 100.0, False),
        (99.0, 99.5, 99.5, 100.0, False),
        (99.0, 95.0, 100.01, 100.0, False),
        (100.0, 95.0, 100.0, 100.0, False),
    ],
)
def test_passive_execution_zone_relationships(breakout, orl, execution, orh, valid):
    _floor, _trigger, _execution, reason = passive_entry_prices(
        breakout_price=breakout,
        orb_high=orh,
        orb_low=orl,
        execution_price=execution,
    )
    assert (not reason) is valid


def _candidate(window: str, score: float, *, high: float, low: float) -> OrbCandidate:
    now = datetime(2026, 8, 28, 14, 0, tzinfo=timezone.utc)
    return OrbCandidate(
        symbol="AAPL",
        window=window,
        orb_high=high,
        orb_low=low,
        breakout_price=99.0,
        breakout_trigger=max(99.0, high),
        entry_trigger=high,
        execution_price=high,
        floor_price=max(99.0, low),
        source_session_date="2026-08-28",
        range_closed_at=now.isoformat(),
        candidate_created_at=now.isoformat(),
        score=score,
        score_version="ORB_POSITION_SCORE_V1",
        stop_adr=40.0,
        shares=10,
        risk_percent=0.01,
        status=OrbCandidateStatus.WAITING_BREAKOUT,
        valid=True,
    )


def _working_card(**overrides) -> TradeCardState:
    fields = dict(
        environment="PROD",
        account_no="1",
        symbol="AAPL",
        board_status=BoardStatus.ENTRY_PENDING,
        entry_runtime_status=EntryRuntimeStatus.ORDER_PENDING,
        breakout_price=99.0,
        selected_orb_window="1m",
        entry_orb_window="1m",
        entry_orb_high=100.0,
        entry_orb_low=95.0,
        entry_trigger=100.0,
        entry_execution_price=100.0,
        entry_floor_price=99.0,
        entry_breakout_trigger=100.0,
        entry_orb_score=50.0,
        entry_score_version="ORB_POSITION_SCORE_V1",
        planned_quantity=10,
        target_position_quantity=10,
        entry_order_generation=1,
        entry_client_order_id="OLD-1",
    )
    fields.update(overrides)
    return TradeCardState(**fields)


def _queue(*candidates: OrbCandidate) -> ExecutionQueueItem:
    return ExecutionQueueItem(
        symbol="AAPL",
        account_no="1",
        breakout_price=99.0,
        candidates={candidate.window: candidate for candidate in candidates},
    )


def test_later_strictly_higher_score_qualifies_replacement():
    now = datetime(2026, 8, 28, 14, 0, tzinfo=timezone.utc)
    card = _working_card()
    item = _queue(
        _candidate("1m", 50.0, high=100.0, low=95.0),
        _candidate("5m", 60.0, high=102.0, low=96.0),
    )

    changed = TradeCardOrbEvaluator(clock=lambda: now).select_replacement_candidate(
        card, item, last_price=105.0, best_ask_price=105.1
    )

    assert changed
    assert card.pending_entry_replacement["state"] == "QUALIFIED"
    assert card.pending_entry_replacement["window"] == "5m"
    assert card.pending_entry_replacement["execution_price"] == 102.0
    assert card.pending_entry_replacement["quantity"] == 10
    assert card.entry_orb_window == "1m"  # active generation is immutable


@pytest.mark.parametrize("score", [50.0, 49.9])
def test_equal_or_lower_later_score_keeps_active_order(score):
    now = datetime(2026, 8, 28, 14, 0, tzinfo=timezone.utc)
    card = _working_card()
    item = _queue(_candidate("5m", score, high=102.0, low=96.0))

    TradeCardOrbEvaluator(clock=lambda: now).select_replacement_candidate(
        card, item, last_price=105.0, best_ask_price=105.1
    )

    assert card.pending_entry_replacement == {}


def test_score_comparison_ignores_sub_precision_float_noise():
    assert not score_strictly_higher(50.00000001, 50.0)
    assert score_strictly_higher(50.1, 50.0)


@pytest.mark.parametrize(
    ("active_window", "replacement_window"),
    [("1m", "5m"), ("1m", "30m"), ("5m", "30m")],
)
def test_all_supported_later_timeframe_upgrade_paths_qualify(
    active_window, replacement_window
):
    now = datetime(2026, 8, 28, 14, 0, tzinfo=timezone.utc)
    card = _working_card(
        selected_orb_window=active_window,
        entry_orb_window=active_window,
    )
    candidate = _candidate(replacement_window, 60.0, high=102.0, low=96.0)

    changed = TradeCardOrbEvaluator(clock=lambda: now).select_replacement_candidate(
        card, _queue(candidate), last_price=105.0, best_ask_price=105.1
    )

    assert changed
    assert card.pending_entry_replacement["window"] == replacement_window
    assert card.pending_entry_replacement["quantity"] == 10


def test_shorter_timeframe_never_downgrades_active_order():
    now = datetime(2026, 8, 28, 14, 0, tzinfo=timezone.utc)
    card = _working_card(
        selected_orb_window="5m", entry_orb_window="5m", entry_orb_score=50.0
    )
    item = _queue(_candidate("1m", 90.0, high=100.0, low=95.0))

    TradeCardOrbEvaluator(clock=lambda: now).select_replacement_candidate(
        card, item, last_price=105.0, best_ask_price=105.1
    )

    assert card.pending_entry_replacement == {}


def test_manual_execution_price_must_fit_replacement_zone():
    now = datetime(2026, 8, 28, 14, 0, tzinfo=timezone.utc)
    card = _working_card(
        entry_execution_price=100.0,
        entry_trigger=100.0,
        entry_execution_price_manual=True,
    )
    # Replacement floor is 101, so the manual 100 price cannot carry.
    candidate = _candidate("5m", 60.0, high=102.0, low=101.0)
    item = _queue(candidate)

    TradeCardOrbEvaluator(clock=lambda: now).select_replacement_candidate(
        card, item, last_price=105.0, best_ask_price=105.1
    )

    assert card.pending_entry_replacement == {}
    assert card.entry_replacement_history[-1]["state"] == "UPGRADE_REJECTED"
    assert "Manual execution price" in card.entry_replacement_history[-1]["reason"]


def test_compatible_manual_execution_price_is_carried_without_change():
    now = datetime(2026, 8, 28, 14, 0, tzinfo=timezone.utc)
    card = _working_card(
        entry_execution_price=101.0,
        entry_trigger=101.0,
        entry_execution_price_manual=True,
    )
    candidate = _candidate("5m", 60.0, high=102.0, low=96.0)

    TradeCardOrbEvaluator(clock=lambda: now).select_replacement_candidate(
        card, _queue(candidate), last_price=105.0, best_ask_price=105.1
    )

    assert card.pending_entry_replacement["execution_price"] == 101.0
    assert card.pending_entry_replacement["execution_price_manual"] is True


def test_confirmed_breakout_remains_latched_during_valid_pullback():
    now = datetime(2026, 8, 28, 14, 0, tzinfo=timezone.utc)
    card = _working_card()
    candidate = _candidate("5m", 60.0, high=102.0, low=96.0)
    candidate.execution_price = 101.0
    candidate.entry_trigger = 101.0
    candidate.breakout_confirmed = True
    candidate.breakout_confirmed_at = now.isoformat()

    TradeCardOrbEvaluator(clock=lambda: now).select_replacement_candidate(
        card,
        _queue(candidate),
        last_price=101.5,
        best_ask_price=101.6,
    )

    assert card.pending_entry_replacement["window"] == "5m"
    assert card.pending_entry_replacement["execution_price"] == 101.0


def test_trade_and_ask_must_both_remain_above_replacement_limit():
    now = datetime(2026, 8, 28, 14, 0, tzinfo=timezone.utc)
    candidate = _candidate("5m", 60.0, high=102.0, low=96.0)
    for trade, ask in ((102.0, 105.0), (105.0, 102.0)):
        card = _working_card()
        TradeCardOrbEvaluator(clock=lambda: now).select_replacement_candidate(
            card, _queue(candidate), last_price=trade, best_ask_price=ask
        )
        assert card.pending_entry_replacement == {}


def test_trade_card_round_trip_preserves_generation_and_replacement_audit():
    confirmed_at = datetime(2026, 8, 28, 14, 1, tzinfo=timezone.utc)
    card = _working_card(
        entry_execution_price=99.5,
        entry_execution_price_manual=True,
        entry_breakout_confirmed_at=confirmed_at,
        entry_order_generation=2,
        pending_entry_replacement={"state": "CANCEL_PENDING", "window": "30m"},
        entry_replacement_history=[{"state": "REPLACED", "new_generation": 2}],
    )

    restored = TradeCardState.from_dict(card.to_dict())

    assert restored.entry_execution_price == 99.5
    assert restored.entry_execution_price_manual is True
    assert restored.entry_breakout_confirmed_at == confirmed_at
    assert restored.entry_order_generation == 2
    assert restored.pending_entry_replacement["state"] == "CANCEL_PENDING"
    assert restored.entry_replacement_history[-1]["new_generation"] == 2


def test_legacy_card_defaults_execution_price_to_entry_trigger():
    card = TradeCardState.from_dict(
        {
            "environment": "PROD",
            "account_no": "1",
            "symbol": "AAPL",
            "entry_trigger": 100.0,
            "entry_orb_high": 100.0,
        }
    )

    assert card.entry_execution_price == 100.0
