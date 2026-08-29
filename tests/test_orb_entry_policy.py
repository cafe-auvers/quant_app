from __future__ import annotations

import pytest

from src.strategy.orb.entry_policy import (
    breakout_trade_confirms,
    build_passive_pullback_plan,
    is_strict_higher_timeframe_upgrade,
    passive_limit_submission_ready,
)


def test_passive_pullback_plan_uses_approved_floor_and_trigger():
    plan = build_passive_pullback_plan(
        orb_high=105.0,
        orb_low=98.0,
        breakout_price=100.0,
        execution_price=102.0,
    )

    assert plan is not None
    assert plan.floor_price == 100.0
    assert plan.breakout_trigger == 105.0
    assert plan.execution_price == 102.0


def test_breakout_price_above_orh_becomes_the_confirmation_trigger():
    plan = build_passive_pullback_plan(
        orb_high=105.0,
        orb_low=98.0,
        breakout_price=106.0,
        execution_price=104.0,
    )

    assert plan is None  # floor 106 cannot be below an execution at/below ORH 105


@pytest.mark.parametrize("execution_price", [100.0, 105.01, 0.0])
def test_execution_price_outside_open_floor_closed_orh_interval_is_rejected(
    execution_price,
):
    assert (
        build_passive_pullback_plan(
            orb_high=105.0,
            orb_low=98.0,
            breakout_price=100.0,
            execution_price=execution_price,
        )
        is None
    )


def test_missing_execution_price_defaults_to_orh_for_automated_candidates():
    plan = build_passive_pullback_plan(
        orb_high=105.0,
        orb_low=98.0,
        breakout_price=100.0,
        execution_price=None,
    )

    assert plan is not None
    assert plan.execution_price == 105.0


def test_execution_price_equal_to_orh_is_valid():
    assert (
        build_passive_pullback_plan(
            orb_high=105.0,
            orb_low=98.0,
            breakout_price=100.0,
            execution_price=105.0,
        )
        is not None
    )


def test_breakout_confirmation_is_strictly_above_trigger():
    assert not breakout_trade_confirms(trade_price=105.0, breakout_trigger=105.0)
    assert breakout_trade_confirms(trade_price=105.01, breakout_trigger=105.0)


@pytest.mark.parametrize(
    ("last_trade", "best_ask"),
    [(102.0, 103.0), (103.0, 102.0), (102.0, 102.0), (101.0, 103.0)],
)
def test_passive_submission_fails_when_last_or_ask_is_not_strictly_above_execution(
    last_trade,
    best_ask,
):
    assert not passive_limit_submission_ready(
        last_trade=last_trade,
        best_ask=best_ask,
        execution_price=102.0,
    )


def test_passive_submission_can_wait_below_an_already_crossed_orh():
    assert passive_limit_submission_ready(
        last_trade=108.0,
        best_ask=108.01,
        execution_price=102.0,
    )


@pytest.mark.parametrize(
    ("current", "candidate"),
    [("1m", "5m"), ("1m", "30m"), ("5m", "30m")],
)
def test_only_strictly_better_higher_timeframes_upgrade(current, candidate):
    assert is_strict_higher_timeframe_upgrade(
        current_window=current,
        current_score=50.0,
        candidate_window=candidate,
        candidate_score=50.1,
    )


@pytest.mark.parametrize(
    ("current", "current_score", "candidate", "candidate_score"),
    [
        ("1m", 50.0, "5m", 50.0),
        ("5m", 50.0, "1m", 60.0),
        ("30m", 50.0, "30m", 60.0),
        ("1h", 50.0, "30m", 60.0),
    ],
)
def test_ties_downgrades_same_window_and_unsupported_windows_do_not_upgrade(
    current,
    current_score,
    candidate,
    candidate_score,
):
    assert not is_strict_higher_timeframe_upgrade(
        current_window=current,
        current_score=current_score,
        candidate_window=candidate,
        candidate_score=candidate_score,
    )
