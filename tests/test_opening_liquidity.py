from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import pandas as pd
import pytest

from src.core.execution_queue import (
    ExecutionQueueManager,
    ExecutionQueueItem,
    OrbCandidate,
    OrbCandidateStatus,
    build_orb_candidate,
)
from src.core.intraday_coverage import annotate_kis_history_coverage
from src.core.opening_liquidity import opening_liquidity_rejection
from src.core.trade_card_state import BoardStatus, TradeCardState
from src.risk.orb_position import (
    DEFAULT_ORB_SETTINGS,
    OrbSettings,
    configure_orb_settings,
    get_orb_settings,
)
from src.risk.pre_trade import assess_orb_entry_candidate, orb_candidate_plan_id
from src.services.buyboard_runtime import _revalidate_and_approve
from src.services.intraday_provider import resample_ohlcv_bars
from src.services.trade_card_orb_bridge import TradeCardOrbEvaluator


@pytest.fixture(autouse=True)
def opening_settings():
    original = get_orb_settings()
    configure_orb_settings(DEFAULT_ORB_SETTINGS)
    yield
    configure_orb_settings(original)


def _bars(minutes=30, volume=200):
    return pd.DataFrame(
        {"Open": 2.49, "High": 2.51, "Low": 2.48, "Close": 2.50, "Volume": volume},
        index=pd.date_range("2026-10-05 09:30", periods=minutes + 1, freq="min", tz="America/New_York"),
    )


def _candidate(bars, window="30m"):
    return build_orb_candidate(
        symbol="TEST", window=window, intraday=bars,
        breakout_price=2.47, current_price=2.55,
        account_size=14154.8, risk_percent=0.0025,
        adr_percent=5.0, lock_risk_percent=True,
    )


@pytest.mark.parametrize("window,minutes", [("1m", 1), ("5m", 5), ("30m", 30)])
def test_minimum_is_inclusive_and_does_not_count_the_unfinished_bar(window, minutes):
    bars = _bars(minutes)
    bars.iloc[-1, bars.columns.get_loc("Volume")] = 1_000_000
    candidate = _candidate(bars, window)
    assert candidate.valid
    assert candidate.opening_volume == minutes * 200
    assert candidate.opening_volume_minutes == minutes

    bars.iloc[0, bars.columns.get_loc("Volume")] -= 1
    blocked = _candidate(bars, window)
    assert not blocked.valid
    assert blocked.status == OrbCandidateStatus.RISK_INVALID
    assert "Low opening liquidity" in blocked.reason
    assert not blocked.terminal_rejection


def test_opening_average_allows_quiet_individual_minutes():
    bars = _bars(5, 0)
    bars.iloc[0, bars.columns.get_loc("Volume")] = 1000
    candidate = _candidate(bars, "5m")
    assert candidate.valid
    assert candidate.opening_volume == 1000
    assert candidate.opening_volume_minutes == 5


def test_every_timeframe_uses_elapsed_opening_minutes_capped_at_thirty():
    bars = _bars(90, 0)
    bars.loc[bars.index[:30], "Volume"] = 200
    bars.loc[bars.index[30:], "Volume"] = 1_000_000
    for window in ("1m", "5m", "30m"):
        candidate = _candidate(bars, window)
        assert candidate.valid
        assert candidate.opening_volume == 6000
        assert candidate.opening_volume_minutes == 30
    bars.loc[bars.index[:30], "Volume"] = 100
    assert "Low opening liquidity" in _candidate(bars).reason


def test_previous_day_and_premarket_volume_cannot_rescue_a_quiet_opening():
    bars = _bars(30, 10)
    earlier = bars.iloc[:2].copy()
    earlier.index = pd.DatetimeIndex([
        "2026-10-02 15:59", "2026-10-05 09:29"
    ]).tz_localize("America/New_York")
    earlier["Volume"] = 10_000_000
    candidate = _candidate(pd.concat([earlier, bars]))
    assert candidate.opening_volume == 300
    assert candidate.opening_volume_minutes == 30
    assert not candidate.valid


def test_sparse_minutes_count_as_elapsed_minutes_and_not_just_present_rows():
    bars = _bars().loc[lambda frame: frame.index.minute.isin([43, 44, 0])].copy()
    bars["Volume"] = 3000
    previous = _bars().iloc[:1].copy()
    previous.index = pd.DatetimeIndex(["2026-10-02 15:59"]).tz_localize("America/New_York")
    bars = pd.concat([previous, bars])
    annotate_kis_history_coverage(bars, "TEST")
    candidate = _candidate(bars)
    assert candidate.valid
    assert candidate.opening_volume == 6000
    assert candidate.opening_volume_minutes == 30


def test_aggregated_bars_and_utc_timestamps_use_the_same_opening_total():
    bars = _bars(35)
    bars.index = bars.index.tz_convert("UTC")
    for frame in (bars, resample_ohlcv_bars(bars, "5m")):
        candidate = _candidate(frame)
        assert candidate.valid
        assert candidate.opening_volume == 6000
        assert candidate.opening_volume_minutes == 30


def test_queue_uses_the_existing_minute_data_for_every_completed_timeframe():
    bars = _bars(6)
    coarse = resample_ohlcv_bars(bars.iloc[:-1], "5m")
    item = SimpleNamespace(symbol="TEST", name="", breakout_price=2.47, stop_loss=None)
    queue = ExecutionQueueManager().build_or_update_from_watchlist_item(
        item, {"1m": bars, "5m": coarse, "30m": coarse},
        current_price=2.55, account_size=14154.8, risk_percent=0.0025, adr_percent=5.0,
    )
    for window in ("1m", "5m"):
        assert queue.candidates[window].valid
        assert queue.candidates[window].opening_volume == 1200
        assert queue.candidates[window].opening_volume_minutes == 6


@pytest.mark.parametrize("bad_volume", [None, -1, float("nan"), float("inf"), "invalid"])
def test_missing_or_invalid_opening_volume_blocks_entry(bad_volume):
    bars = _bars()
    bars["Volume"] = bars["Volume"].astype(object)
    bars.iloc[0, bars.columns.get_loc("Volume")] = bad_volume
    assert "Opening liquidity unavailable" in _candidate(bars).reason
    assert not _candidate(bars.drop(columns="Volume")).valid


def test_invalid_later_volume_does_not_affect_opening_liquidity():
    bars = _bars(35)
    bars.loc[bars.index[30:], "Volume"] = float("nan")
    assert _candidate(bars).valid


def test_duplicate_opening_bars_cannot_double_count_volume():
    bars = _bars()
    assert not _candidate(pd.concat([bars, bars.iloc[:1]])).valid


def test_changed_setting_rechecks_the_same_volume_without_new_data():
    candidate = _candidate(_bars())
    configure_orb_settings(replace(DEFAULT_ORB_SETTINGS, opening_min_shares_per_minute=201))
    assert opening_liquidity_rejection(candidate.opening_volume, candidate.opening_volume_minutes)
    configure_orb_settings(replace(DEFAULT_ORB_SETTINGS, opening_min_shares_per_minute=100))
    assert not opening_liquidity_rejection(candidate.opening_volume, candidate.opening_volume_minutes)
    configure_orb_settings(replace(DEFAULT_ORB_SETTINGS, opening_min_shares_per_minute=0))
    assert _candidate(_bars().drop(columns="Volume")).valid


def test_candidate_round_trip_keeps_volume_and_legacy_candidate_needs_refresh():
    candidate = _candidate(_bars())
    assert OrbCandidate.from_dict(candidate.to_dict()).opening_volume == 6000
    legacy = candidate.to_dict()
    legacy.pop("opening_volume")
    legacy.pop("opening_volume_minutes")
    restored = OrbCandidate.from_dict(legacy)
    assert "Opening liquidity unavailable" in opening_liquidity_rejection(
        restored.opening_volume, restored.opening_volume_minutes
    )


def test_preserved_breakout_does_not_promote_a_newly_illiquid_candidate():
    manager = ExecutionQueueManager()
    prior = _candidate(_bars())
    prior.breakout_confirmed = True
    prior.status = OrbCandidateStatus.EXECUTE_READY
    manager.upsert_item(symbol="TEST", breakout_price=2.47, candidates={"30m": prior})
    blocked = _candidate(_bars(volume=10))
    updated = manager.upsert_item(symbol="TEST", breakout_price=2.47, candidates={"30m": blocked})
    assert updated.candidates["30m"].breakout_confirmed
    assert updated.candidates["30m"].status == OrbCandidateStatus.RISK_INVALID
    assert not updated.candidates["30m"].valid


@pytest.mark.parametrize("missing", [False, True])
def test_bridge_cannot_arm_or_cross_a_cached_plan_that_fails_liquidity(missing):
    candidate = _candidate(_bars())
    if missing:
        candidate.opening_volume = None
    else:
        configure_orb_settings(replace(DEFAULT_ORB_SETTINGS, opening_min_shares_per_minute=201))
    card = TradeCardState(environment="PROD", account_no="offline", symbol="TEST",
                          board_status=BoardStatus.BUY_TODAY, breakout_price=2.47)
    queue = ExecutionQueueItem(symbol="TEST", account_no="offline", breakout_price=2.47,
                               candidates={"30m": candidate}, selected_candidate=candidate)
    evaluator = TradeCardOrbEvaluator(clock=lambda: datetime(2026, 10, 5, 14, 1, tzinfo=timezone.utc))
    evaluator.update_card(card, queue)
    assert "opening liquidity" in card.entry_block_reason.lower()
    assert not evaluator.select_crossed_candidate(card, queue, last_price=2.55)


def test_legacy_pretrade_boundary_rechecks_the_live_minimum():
    candidate = _candidate(_bars())
    candidate.status = OrbCandidateStatus.EXECUTE_READY
    kwargs = dict(
        candidate=candidate, environment="PROD", account_no="offline", symbol="TEST",
        quantity=candidate.shares, reference_price=candidate.execution_price,
        plan_id=orb_candidate_plan_id(candidate),
    )
    assert assess_orb_entry_candidate(**kwargs).approved
    configure_orb_settings(replace(DEFAULT_ORB_SETTINGS, opening_min_shares_per_minute=201))
    decision = assess_orb_entry_candidate(**kwargs)
    assert not decision.approved
    assert any("Low opening liquidity" in reason for reason in decision.reasons)


def test_buyboard_boundary_requires_volume_evidence_and_rechecks_setting():
    candidate = _candidate(_bars())
    card = TradeCardState(
        environment="PROD", account_no="offline", symbol="TEST",
        board_status=BoardStatus.BUY_TODAY, selected_orb_window="30m",
        entry_orb_low=candidate.orb_low, entry_trigger=candidate.execution_price,
        stop_adr=candidate.stop_adr, orb_candidate_states={"30m": candidate.to_dict()},
    )
    kwargs = dict(card=card, quantity=candidate.shares, limit_price=candidate.execution_price,
                  exchange="NASD", account_size=14154.8)
    assert _revalidate_and_approve(**kwargs).approved
    configure_orb_settings(replace(DEFAULT_ORB_SETTINGS, opening_min_shares_per_minute=201))
    assert not _revalidate_and_approve(**kwargs).approved
    card.orb_candidate_states = {}
    assert any("Opening liquidity unavailable" in reason
               for reason in _revalidate_and_approve(**kwargs).reasons)


def test_existing_settings_keep_position_bounds_and_get_the_new_default():
    settings = OrbSettings.from_mapping({"capital_max_percent": 35})
    assert settings.capital_max_percent == 35
    assert settings.opening_min_shares_per_minute == 200
    assert OrbSettings.from_mapping(settings.to_dict()) == settings


@pytest.mark.parametrize("invalid", [-1, float("nan"), float("inf")])
def test_invalid_minimum_cannot_be_configured(invalid):
    with pytest.raises(ValueError):
        replace(DEFAULT_ORB_SETTINGS, opening_min_shares_per_minute=invalid)
