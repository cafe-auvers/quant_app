"""Momentum is an advisory comparison, independent of order eligibility."""
import datetime as dt

import numpy as np
import pandas as pd
import pytest

from src.core.intraday_momentum import (
    assign_momentum_ranks, best_passed_orb, evaluate_momentum,
)
from src.utils.market_calendar import US_MARKET_ZONE


def moment(time="10:05", day="2026-10-02"):
    return dt.datetime.fromisoformat(f"{day}T{time}:00").replace(tzinfo=US_MARKET_ZONE)


def session(*, day="2026-10-02", until="10:04", start=100, end=102, volume=3000):
    index = pd.date_range(moment("09:30", day), moment(until, day), freq="min")
    prices = np.linspace(start, end, len(index))
    return pd.DataFrame({"Open": prices, "High": prices + 0.05, "Low": prices - 0.05,
                         "Close": prices, "Volume": volume}, index=index)


def history(**kwargs):
    return pd.concat([
        *(session(day=day, volume=1000) for day in ("2026-09-28", "2026-09-29", "2026-09-30", "2026-10-01")),
        session(**kwargs),
    ])


def card(**kwargs):
    return {"symbol": "AAA", "quote_status": "CURRENT", "breakout_price": 101,
            "orb": [{"window": "5m", "price_status": "PASS", "position_status": "PASS",
                     "liquidity_status": "PASS", "score": 90, "execution_price": 101.5}], **kwargs}


def evaluate(frame=None, market=None, row=None, **kwargs):
    return evaluate_momentum(
        card() if row is None else row, history() if frame is None else frame,
        session(start=500, end=500.5) if market is None else market,
        **{"now": moment(), "adr": 4, **kwargs},
    )


def test_score_explains_open_relative_strength_volume_vwap_and_entry():
    result = evaluate()
    assert result["status"] == "LEADING"
    assert result["candidate"] is True
    assert result["rs_open_pp"] == pytest.approx(1.9)
    assert result["stock_return_open_percent"] == pytest.approx(2)
    assert result["benchmark_return_open_percent"] == pytest.approx(0.1)
    assert result["rvol"] == 3
    assert result["rvol_sessions"] == 4
    assert result["vwap"] == pytest.approx(101)
    assert result["as_of"] == moment().isoformat()
    parts = result["components"]
    assert parts["relative_strength"] == 95
    assert result["score"] == pytest.approx(
        parts["relative_strength"] * .4 + parts["relative_volume"] * .25
        + parts["trend"] * .25 + parts["entry"] * .1, abs=.1,
    )


def test_large_overnight_gap_does_not_hide_fading_intraday_momentum():
    fading = evaluate(history(start=120, end=119.8), row=card(change_percent=20))
    assert fading["status"] == "WATCH"
    assert fading["stock_return_open_percent"] < 0
    assert fading["rs_open_pp"] < 0
    assert fading["components"]["relative_strength"] == 0
    assert fading["score"] < evaluate()["score"]
    assert "Not advancing from the open" in fading["reason"]
    assert "At or below VWAP" in fading["reason"]


def test_strength_is_normalized_by_stock_adr_rather_than_rewarding_volatility():
    assert evaluate(adr=8)["components"]["relative_strength"] == pytest.approx(47.5)
    assert evaluate(adr=4)["components"]["relative_strength"] == 95


def test_synchronized_prices_and_volume_ignore_forming_minutes_and_future_data():
    original = evaluate()
    stock = pd.concat([history(), session(until="10:05").tail(1) * 999])
    stock = pd.concat([stock, session(day="2026-10-05", volume=99_999)])
    market = session(start=500, end=500.5)
    market.loc[moment(), :] = [900, 900, 900, 900, 9999]
    assert evaluate(stock, market) == original


def test_different_quote_times_use_the_latest_common_completed_bar():
    market = session(start=500, end=500.5, until="10:03")
    result = evaluate(market=market)
    expected = (history().loc[moment("10:03"), "Close"] / 100 - 1) * 100 - .1
    assert result["rs_open_pp"] == pytest.approx(expected)
    assert result["as_of"] == moment("10:04").isoformat()


@pytest.mark.parametrize("which", ["stock", "market"])
def test_missing_opening_bar_cannot_be_replaced_with_a_later_open(which):
    frames = {"frame": history(), "market": session(start=500, end=500.5)}
    key = "frame" if which == "stock" else "market"
    frames[key] = frames[key].drop(moment("09:30"))
    result = evaluate(**frames)
    assert result["score"] is None
    assert "incomplete" in result["reason"]


@pytest.mark.parametrize("volume", [float("nan"), float("inf"), -1])
def test_invalid_volume_never_becomes_zero_or_a_high_rank(volume):
    frame = history()
    frame["Volume"] = frame["Volume"].astype(float)
    frame.loc[moment("09:45"), "Volume"] = volume
    assert evaluate(frame)["score"] is None


def test_rvol_excludes_incomplete_prior_sessions_and_never_uses_full_day_totals():
    frame = history()
    frame.loc[moment("15:59", "2026-10-01")] = [100, 101, 99, 100, 999_999]
    assert evaluate(frame)["rvol"] == 3
    frame = frame.drop(moment("09:45", "2026-10-01"))
    assert evaluate(frame)["rvol_sessions"] == 3
    frame = frame.drop(moment("09:45", "2026-09-30"))
    result = evaluate(frame)
    assert result["score"] is None
    assert "3 complete prior sessions" in result["reason"]


def test_rvol_uses_median_so_one_previous_volume_spike_does_not_dominate():
    frame = history()
    frame.loc[frame.index.date == dt.date(2026, 10, 1), "Volume"] = 100_000
    assert evaluate(frame)["rvol"] == 3


@pytest.mark.parametrize("kwargs, phrase", [
    ({"benchmark_failed": True}, "SPY"),
    ({"market": pd.DataFrame()}, "SPY"),
    ({"market": session(until="09:45")}, "stale"),
    ({"row": card(quote_status="STALE")}, "stale"),
    ({"frame": session()}, "3 complete"),
    ({"adr": None}, "ADR"),
    ({"adr": 0}, "ADR"),
    ({"row": card(orb=[])}, "passing ORB"),
])
def test_missing_or_stale_inputs_remain_unranked_with_a_reason(kwargs, phrase):
    result = evaluate(**kwargs)
    assert result["score"] is result["rank"] is None
    assert result["candidate"] is False
    assert phrase in result["reason"]


@pytest.mark.parametrize("time, day", [("09:29", "2026-10-02"), ("16:00", "2026-10-02"),
                                      ("10:05", "2026-10-04"), ("13:00", "2026-11-27")])
def test_no_live_momentum_rank_outside_regular_hours(time, day):
    assert evaluate(now=moment(time, day))["status"] == "CLOSED"


def test_extended_move_and_low_volume_are_watch_candidates_even_after_an_orb_pass():
    result = evaluate(history(end=110, volume=1000))
    assert result["status"] == "WATCH"
    assert "RVOL below 1.5x" in result["reason"]
    assert "0.5 ADR above planned entry" in result["reason"]
    assert result["components"]["entry"] == 0


def test_price_far_below_the_configured_entry_is_not_called_a_leading_candidate():
    row = card()
    row["orb"][0]["execution_price"] = 110
    result = evaluate(row=row)
    assert result["status"] == "WATCH"
    assert "0.5 ADR below planned entry" in result["reason"]


def test_utc_input_has_identical_results_and_naive_data_stays_unavailable():
    frame, market = history(), session(start=500, end=500.5)
    frame.index, market.index = frame.index.tz_convert("UTC"), market.index.tz_convert("UTC")
    assert evaluate(frame, market) == evaluate()
    frame.index = frame.index.tz_localize(None)
    assert evaluate(frame)["score"] is None


def test_leading_group_precedes_watch_then_score_and_symbol_determine_rank():
    rows = [
        {"symbol": "WATCH", "momentum": {"candidate": False, "score": 99}},
        {"symbol": "BBB", "momentum": {"candidate": True, "score": 70}},
        {"symbol": "AAA", "momentum": {"candidate": True, "score": 70}},
        {"symbol": "STALE", "momentum": {"candidate": False, "score": None, "rank": 1}},
    ]
    assign_momentum_ranks(rows)
    assert [row["momentum"]["rank"] for row in rows] == [3, 2, 1, None]


def test_orb_geometry_uses_existing_best_plan_without_changing_it():
    row = card()
    row["orb"].append({**row["orb"][0], "window": "1m"})
    assert best_passed_orb(row)["window"] == "1m"
    before = [dict(plan) for plan in row["orb"]]
    evaluate(row=row)
    assert row["orb"] == before
