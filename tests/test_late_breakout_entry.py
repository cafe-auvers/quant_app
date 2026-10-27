from datetime import datetime, timezone

import pandas as pd
import pytest

from src.core.execution_queue import (
    ExecutionQueueItem,
    OrbCandidateStatus,
    build_orb_candidate,
)
from src.core.orb_combinations import build_orb_position_combinations
from src.core.trade_card_state import BoardStatus, EntryRuntimeStatus, TradeCardState
from src.services.trade_card_orb_bridge import TradeCardOrbEvaluator


@pytest.mark.parametrize("window", ["1m", "5m", "30m"])
def test_daily_breakout_after_1000_qualifies_an_opening_range_below_the_level(window):
    now = datetime(2026, 8, 19, 14, 20, tzinfo=timezone.utc)  # 10:20 New York
    bars = pd.DataFrame(
        {"Open": 99.0, "High": 99.8, "Low": 98.0, "Close": 99.5, "Volume": 1000},
        index=pd.date_range("2026-08-19 09:30", periods=51, freq="min", tz="America/New_York"),
    )
    bars.loc[bars.index[-1], ["High", "Close"]] = [100.3, 100.2]
    candidate = build_orb_candidate(
        symbol="AAPL", window=window, intraday=bars, breakout_price=100.0,
        current_price=100.2, account_size=100_000, risk_percent=0.005, adr_percent=5.0,
    )

    assert candidate.valid and not candidate.terminal_rejection
    assert candidate.status == OrbCandidateStatus.WAITING_BREAKOUT
    assert candidate.orb_high == 99.8
    assert candidate.breakout_trigger == candidate.execution_price == 100.0
    assert candidate.stop_loss == 98.0
    assert candidate.stop_loss_percent == pytest.approx(2.0)

    queue = ExecutionQueueItem(
        symbol="AAPL", account_no="1", breakout_price=100.0,
        candidates={window: candidate}, selected_candidate=candidate,
        selected_window=window, last_updated=now,
    )
    combinations = build_orb_position_combinations(queue, account_equity=100_000)
    assert any(option.valid and option.window == window for option in combinations)
    assert all(option.execution_price == 100.0 for option in combinations if option.window == window)

    card = TradeCardState(
        environment="PROD", account_no="1", symbol="AAPL",
        board_status=BoardStatus.BUY_TODAY, breakout_price=100.0,
        entry_runtime_status=EntryRuntimeStatus.WAITING_BREAKOUT,
    )
    evaluator = TradeCardOrbEvaluator(clock=lambda: now)
    assert not evaluator.select_crossed_candidate(card, queue, last_price=100.0)
    assert not candidate.breakout_confirmed
    assert evaluator.select_crossed_candidate(card, queue, last_price=100.2)
    assert candidate.breakout_confirmed
    assert card.entry_runtime_status == EntryRuntimeStatus.EXECUTE_READY
    assert card.entry_execution_price == card.entry_breakout_trigger == 100.0
    assert card.entry_orb_high == 99.8 and card.entry_orb_low == 98.0
    assert card.planned_quantity == candidate.shares


@pytest.mark.parametrize("window", ["1m", "5m", "30m"])
def test_late_breakout_keeps_risk_limits_when_daily_level_is_far_above_orh(window):
    bars = pd.DataFrame(
        {"Open": 99.0, "High": 99.8, "Low": 98.0, "Close": 99.5, "Volume": 1000},
        index=pd.date_range("2026-08-19 09:30", periods=51, freq="min"),
    )
    candidate = build_orb_candidate(
        symbol="AAPL", window=window, intraday=bars, breakout_price=110.0,
        current_price=111.0, account_size=100_000, risk_percent=0.005, adr_percent=5.0,
    )
    assert candidate.execution_price == candidate.breakout_trigger == 110.0
    assert candidate.status == OrbCandidateStatus.RISK_INVALID
    assert not candidate.valid
    assert "ADR" in candidate.reason
