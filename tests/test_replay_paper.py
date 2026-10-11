import datetime as dt

import pytest

from src.core.replay_paper import buy_plan, paper_ledger


DAY = dt.date(2025, 2, 3)
NEXT = dt.date(2025, 2, 4)


def candle(day, minute, price, low=None, high=None):
    stamp = int(dt.datetime.combine(day, dt.time(14, minute), dt.timezone.utc).timestamp())
    return {"time": stamp, "open": price, "high": high if high is not None else price,
        "low": low if low is not None else price, "close": price, "volume": 1000}


def action(bar, day=DAY, side="buy", price=None, stop=5.225, identity="buy", percent=50):
    return {"id": identity, "day": day, "time": bar["time"], "action": side,
        "price": price or bar["close"], "stop": stop, "percent": percent, "sequence": 0}


def run(actions, sessions):
    dates = sorted(sessions)
    return paper_ledger(actions, dates, lambda day: {"bars": sessions[day], "source": "test", "complete": True}, dates[-1])


def test_whole_share_sizing_caps_both_cash_and_one_percent_nav_risk():
    plan = buy_plan(10000, 10000, 5.5, 5.225)
    assert plan["shares"] == 363
    assert plan["risk_budget"] == 100
    assert plan["risk_amount"] == pytest.approx(99.825)
    assert plan["notional"] == pytest.approx(1996.5)
    assert buy_plan(50, 10000, 10, 9)["shares"] == 5
    assert buy_plan(10000, 10000, 100, 99)["shares"] == 100
    assert buy_plan(8000, 10000, 100, 95, quantity=20, average=100)["shares"] == 0


def test_partial_exit_then_sell_all_preserves_cash_cost_basis_and_nav():
    bars = [candle(DAY, 30, 5.5), candle(DAY, 35, 6), candle(DAY, 40, 7)]
    result = run([action(bars[0]), action(bars[1], side="partial_sell", identity="partial"),
        action(bars[2], side="sell_all", identity="all")], {DAY: bars})
    assert [fill["shares"] for fill in result["fills"]] == [363, 181, 182]
    assert result["quantity"] == 0
    assert result["cash"] == result["nav"] == pytest.approx(10363.5)
    assert result["realized_pnl"] == pytest.approx(363.5)
    assert result["unrealized_pnl"] == 0


def test_next_day_wide_range_uses_intraday_stop_time_instead_of_daily_high():
    bought = candle(DAY, 30, 5.5)
    bars = [candle(NEXT, 30, 6, low=5.6, high=10.01), candle(NEXT, 35, 10, low=4.95, high=10.01)]
    result = run([action(bought)], {DAY: [bought], NEXT: bars})
    stopped = result["fills"][-1]
    assert stopped["reason"] == "stop_loss"
    assert stopped["time"] == bars[1]["time"]
    assert stopped["price"] == 5.225
    assert stopped["realized_pnl"] == pytest.approx(-99.825)
    assert result["nav"] == pytest.approx(9900.175)


def test_gap_below_stop_fills_at_open_and_can_exceed_planned_risk():
    bought = candle(DAY, 30, 5.5)
    gap = candle(NEXT, 30, 4.95, high=10.01)
    result = run([action(bought)], {DAY: [bought], NEXT: [gap]})
    assert result["fills"][-1]["reason"] == "stop_gap"
    assert result["fills"][-1]["price"] == 4.95
    assert result["nav"] == pytest.approx(9800.35)


def test_partial_before_future_auto_stop_changes_only_remaining_stop_quantity():
    bars = [candle(DAY, 30, 5.5), candle(DAY, 35, 6), candle(DAY, 40, 6, low=5)]
    original = run([action(bars[0])], {DAY: bars})
    amended = run([action(bars[0]), action(bars[1], side="partial_sell", identity="partial")], {DAY: bars})
    assert original["fills"][-1]["shares"] == 363
    assert amended["fills"][-1]["shares"] == 182
    assert amended["nav"] == pytest.approx(10040.45)


def test_ambiguous_same_candle_stop_is_conservative_and_labeled():
    bar = candle(DAY, 30, 5.4, low=5, high=6)
    result = run([action(bar, price=5.5)], {DAY: [bar]})
    assert result["fills"][-1]["reason"] == "stop_same_bar"
    assert result["quantity"] == 0
    assert "order" in result["warnings"][0]


def test_missing_intraday_session_preserves_last_verified_nav_and_blocks_later_actions():
    bought = candle(DAY, 30, 5.5)
    later = dt.date(2025, 2, 5)
    sessions = {DAY: [bought], NEXT: [], later: [candle(later, 30, 10)]}
    result = run([action(bought)], sessions)
    assert result["pending"]["day"] == "2025-02-04"
    assert result["nav"] == 10000
    assert result["quantity"] == 363
    assert len(result["fills"]) == 1


def test_unknown_fill_prices_and_selling_without_a_position_are_rejected():
    bar = candle(DAY, 30, 5.5)
    with pytest.raises(ValueError, match="range"):
        run([action(bar, price=10)], {DAY: [bar]})
    with pytest.raises(ValueError, match="no open position"):
        run([action(bar, side="sell_all")], {DAY: [bar]})
