"""Sparse broker history may omit 09:30; truncated history must stay closed."""
from copy import deepcopy

import pandas as pd
import pytest
from sqlalchemy import create_engine

from src.api.kis_intraday import KisIntradayClient
from src.core.execution_queue import OrbCandidateStatus, build_orb_candidate
from src.core.intraday_coverage import KIS_COVERAGE_ATTR, annotate_kis_history_coverage
from src.core.orb import calculate_orb_range
from src.infrastructure.database.repositories.market_bars import (
    load_intraday_history_from_db, save_intraday_history_to_db,
)
from src.services.intraday_provider import resample_ohlcv_bars


def _sparse_bars(start="2026-10-05 09:43", through="2026-10-05 10:01"):
    index = pd.DatetimeIndex([pd.Timestamp("2026-10-02 15:59"), *pd.date_range(start, through, freq="min")])
    frame = pd.DataFrame({"Open": 2.49, "High": 2.51, "Low": 2.48, "Close": 2.50, "Volume": 10.0}, index=index)
    annotate_kis_history_coverage(frame, "CURV")
    return frame


def _candidate(frame, window="30m", symbol="CURV"):
    return build_orb_candidate(symbol=symbol, window=window, intraday=frame,
        breakout_price=2.47, current_price=2.50, account_size=14_154.80,
        risk_percent=0.0025, adr_percent=5.0)


@pytest.mark.parametrize("interval", ["1m", "5m"])
def test_verified_sparse_history_survives_cache_and_latest_session_filter(interval):
    bars = _sparse_bars()
    if interval == "5m":
        bars = resample_ohlcv_bars(bars, "5m")
    engine = create_engine("sqlite:///:memory:")
    try:
        assert save_intraday_history_to_db("CURV", bars, engine, interval=interval, source="kis")
        loaded = load_intraday_history_from_db("CURV", engine, interval=interval, source="kis")
        loaded = loaded[loaded.index.date == pd.Timestamp("2026-10-05").date()]
        result = calculate_orb_range("CURV", loaded, "30m")
        assert result is not None
        assert result.high == 2.51 and result.low == 2.48
        assert result.start.strftime("%H:%M") == "09:30"
        assert result.end.strftime("%H:%M") == "10:00"
        assert not (loaded.index.time == pd.Timestamp("09:30").time()).any()
    finally:
        engine.dispose()


@pytest.mark.parametrize("window,end", [("1m", "09:31"), ("5m", "09:35")])
def test_no_broker_bars_in_short_window_stays_unavailable_with_clear_reason(window, end):
    candidate = _candidate(_sparse_bars(), window)
    assert candidate.status == OrbCandidateStatus.NOT_AVAILABLE
    assert not candidate.valid
    assert f"09:30–{end} ET" in candidate.reason
    assert "first available bar is 09:43 ET" in candidate.reason
    assert "opening bar is unavailable" not in candidate.reason


@pytest.mark.parametrize("damage", ["unproven", "truncated", "changed", "wrong_symbol", "wrong_session", "unfinished", "invalid_end", "invalid_hashes"])
def test_sparse_range_refuses_missing_or_invalid_download_proof(damage):
    bars = _sparse_bars()
    bars.attrs = deepcopy(bars.attrs)
    if damage == "unproven":
        bars.attrs.clear()
    elif damage == "truncated":
        bars = bars.iloc[1:].copy()
        bars.attrs.clear()
        annotate_kis_history_coverage(bars, "CURV")
    elif damage == "changed":
        bars.loc[pd.Timestamp("2026-10-05 09:43"), "High"] = 2.90
    elif damage == "wrong_symbol":
        bars.attrs[KIS_COVERAGE_ATTR]["symbol"] = "ODD"
    elif damage == "wrong_session":
        bars.attrs[KIS_COVERAGE_ATTR]["opening_hashes"] = {"2026-10-02": "wrong"}
    elif damage == "unfinished":
        bars.attrs[KIS_COVERAGE_ATTR]["covered_through"] = "2026-10-05T09:59:00"
    elif damage == "invalid_end":
        bars.attrs[KIS_COVERAGE_ATTR]["covered_through"] = "NaT"
    elif damage == "invalid_hashes":
        bars.attrs[KIS_COVERAGE_ATTR]["opening_hashes"] = []
    assert calculate_orb_range("CURV", bars, "30m") is None


def test_sparse_history_waits_until_actual_window_end():
    bars = _sparse_bars(through="2026-10-05 09:59")
    assert _candidate(bars).status == OrbCandidateStatus.FORMING


def test_calculated_later_range_still_rejects_breakout_above_its_high():
    result = build_orb_candidate(symbol="CURV", window="30m", intraday=_sparse_bars(),
        breakout_price=2.52, current_price=2.50, account_size=14_154.80,
        risk_percent=0.0025, adr_percent=5.0)
    assert result.orb_high == 2.51 and result.orb_low == 2.48
    assert result.status == OrbCandidateStatus.REJECTED and not result.valid
    assert "No valid passive-pullback execution zone" in result.reason


def test_invalid_broker_row_cannot_prove_absent_opening_trade(monkeypatch):
    import src.api.kis_intraday as api
    monkeypatch.setattr(api, "is_kis_intraday_enabled", lambda: True)
    mapping = {"endpoint": "/mock", "tr_id": "MOCK", "output_field": "output2", "date_field": "xymd",
        "time_field": "xhms", "open_field": "open", "high_field": "high", "low_field": "low",
        "close_field": "close", "volume_field": "volume"}
    monkeypatch.setattr(api, "_load_intraday_endpoint_config", lambda: mapping)
    raw = [{"xymd": timestamp.strftime("%Y%m%d"), "xhms": timestamp.strftime("%H%M%S"),
        "open": 2.49, "high": 2.51, "low": 2.48, "close": 2.50, "volume": 10}
        for timestamp in _sparse_bars().index]
    raw.append({**raw[-1], "xhms": "093000", "high": "nan"})
    client = KisIntradayClient(object())
    monkeypatch.setattr(client, "_fetch_paginated_rows", lambda **kwargs: raw)
    result = client.fetch_overseas_1m("CURV", exchanges=("NYS",), window_days=1)
    assert result.bars.attrs[KIS_COVERAGE_ATTR]["data_valid"] is False
    assert calculate_orb_range("CURV", result.bars, "30m") is None


@pytest.mark.parametrize("session_date", ["2026-10-05", "2026-11-09"])
def test_late_kis_local_bar_is_never_mistaken_for_a_utc_opening_bar(session_date):
    bars = pd.DataFrame(
        {"Open": 2.49, "High": 2.51, "Low": 2.48, "Close": 2.50, "Volume": 10.0},
        index=pd.date_range(f"{session_date} 14:30", periods=40, freq="min"),
    )
    annotate_kis_history_coverage(bars, "CURV")
    assert calculate_orb_range("CURV", bars, "30m") is None
    assert _candidate(bars).status == OrbCandidateStatus.NOT_AVAILABLE
    engine = create_engine("sqlite:///:memory:")
    try:
        assert save_intraday_history_to_db("CURV", bars, engine, interval="1m", source="kis")
        loaded = load_intraday_history_from_db("CURV", engine, interval="1m", source="kis")
        assert calculate_orb_range("CURV", loaded, "30m") is None
    finally:
        engine.dispose()


def test_no_sparse_opening_proof_is_attached_to_yahoo_cache():
    engine = create_engine("sqlite:///:memory:")
    try:
        assert save_intraday_history_to_db("CURV", _sparse_bars(), engine, interval="1m", source="yfinance")
        loaded = load_intraday_history_from_db("CURV", engine, interval="1m", source="yfinance")
        assert KIS_COVERAGE_ATTR not in loaded.attrs
        assert calculate_orb_range("CURV", loaded, "30m") is None
    finally:
        engine.dispose()


def test_changed_cached_opening_rows_invalidate_previous_proof():
    engine = create_engine("sqlite:///:memory:")
    try:
        bars = _sparse_bars()
        assert save_intraday_history_to_db("CURV", bars, engine, interval="1m", source="kis")
        changed = bars.iloc[1:2].copy()
        changed.attrs.clear()
        changed["High"] = 2.90
        assert save_intraday_history_to_db("CURV", changed, engine, interval="1m", source="kis")
        loaded = load_intraday_history_from_db("CURV", engine, interval="1m", source="kis")
        assert calculate_orb_range("CURV", loaded, "30m") is None
    finally:
        engine.dispose()


def test_bar_and_coverage_save_roll_back_together(monkeypatch):
    import src.infrastructure.database.repositories.market_bars as cache
    engine = create_engine("sqlite:///:memory:")
    try:
        def fail(*args, **kwargs):
            raise RuntimeError("coverage write failed")
        monkeypatch.setattr(cache, "save_intraday_coverage", fail)
        with pytest.raises(RuntimeError, match="coverage write failed"):
            save_intraday_history_to_db("CURV", _sparse_bars(), engine, interval="1m", source="kis")
        assert load_intraday_history_from_db("CURV", engine, interval="1m", source="kis").empty
    finally:
        engine.dispose()
