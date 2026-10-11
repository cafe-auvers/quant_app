import datetime as dt
from io import BytesIO
import base64
import json
import uuid

import numpy as np
import pandas as pd
from PIL import Image
import pytest
from sqlalchemy import create_engine, delete, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import StaticPool
from fastapi.testclient import TestClient

from src.core.breakout_replay import daily_chart, metrics_as_of, next_session, opening_moves, orb_check, rank_and_scan
from src.infrastructure.database.schema import _ensure_price_history_table
from src.infrastructure.database.repositories.breakout_replay import ReplayRepository, encode, now
from src.services.breakout_replay import ReplayService
from src.services.replay_intraday import ReplayIntradayProvider, RequestLimiter
from src.services.replay_daily import DailyReplayArchive
from src.web.replay import create_replay_app


def history(symbol="A", count=160):
    index = pd.bdate_range(end="2025-01-07", periods=count)
    close = np.linspace(30, 80, count)
    return pd.DataFrame({"Open": close*.995, "High": close*1.03, "Low": close*.97,
        "Close": close, "Volume": 1_000_000}, index=index)


@pytest.fixture
def service(tmp_path):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    _ensure_price_history_table(engine)
    repo = ReplayRepository(engine)
    for symbol in ("A", "SPY"):
        records = [{"symbol": symbol, "date": stamp.to_pydatetime(), "interval": "1d", "updated_at": now(),
            **{name.lower(): float(row[name]) for name in ("Open", "High", "Low", "Close", "Volume")}}
            for stamp, row in history().iterrows()]
        repo.upsert(repo.daily, records, ("symbol", "date", "interval"))
    repo.upsert(repo.scans, [{"id": "scan", "requested_day": dt.date(2025, 1, 2), "scan_day": dt.date(2025, 1, 2),
        "status": "complete", "payload": encode({}), "created_at": now()}], ("id",))
    repo.upsert(repo.results, [{"scan_id": "scan", "symbol": "A", "metrics": encode({"return_1m": 15})}], ("scan_id", "symbol"))
    instance = ReplayService(repo, tmp_path / "snapshots")
    yield instance
    instance.pool.shutdown(wait=True)
    engine.dispose()


def test_future_prices_do_not_change_historical_metrics_or_indicators():
    bars = history()
    day = dt.date(2025, 1, 2)
    before = metrics_as_of("A", bars, bars, day)
    chart = daily_chart(bars, bars, day)
    changed = bars.copy()
    changed.loc[changed.index > pd.Timestamp(day), ["Open", "High", "Low", "Close"]] *= 90
    assert metrics_as_of("A", changed, bars, day) == before
    assert daily_chart(changed, bars, day) == chart
    assert all(row["time"] <= day.isoformat() for row in chart["bars"])


def test_historical_chart_includes_loaded_past_beyond_replay_warmup():
    bars = history(count=700)
    bars.index = pd.bdate_range(end="2026-10-08", periods=len(bars))
    archive = DailyReplayArchive("A", dt.date(2026, 10, 1), {"A": bars, "SPY": bars})
    backtest = archive.visible(dt.date(2026, 10, 1))
    full = archive.visible(dt.date(2026, 10, 8), full_history=True)
    assert full["bars"][0]["time"] < backtest["bars"][0]["time"]
    assert len(full["bars"]) == len(bars) and full["bars"][-1]["time"] == "2026-10-08"
    assert archive.visible(dt.date(2026, 10, 1)) == backtest


def test_view_modes_hide_future_data_and_preserve_selected_day_and_saved_work(service, monkeypatch):
    monkeypatch.setattr("src.services.breakout_replay.yf.download", lambda *a, **k: pytest.fail("Use the local daily archive"))
    initial = service.open_session("scan", "A")
    sid = initial["session"]["id"]
    service.save_review(sid, "expected", notes="Resistance at 80")
    before = service.repo.review_rows("A")
    historical = service.set_mode(sid, "historical")
    assert historical["session"]["cursor"] == initial["session"]["cursor"]
    assert historical["session"]["mode"] == "historical"
    assert historical["bars"][-1]["time"] == historical["chart_through"] == "2025-01-07"
    assert historical["intraday_days"][-1] == "2025-01-07"
    assert historical["metrics"] == initial["metrics"] and historical["paper"]["nav"] == 10000
    assert not historical["can_predict"]
    assert all(stamp > "2025-01-07" for stamp in historical["projection_days"])
    hidden = service.set_mode(sid, "backtesting")
    assert hidden["bars"] == initial["bars"] and hidden["metrics"] == initial["metrics"]
    assert hidden["intraday_days"][-1] == hidden["chart_through"] == "2025-01-02"
    assert hidden["current_review"]["notes"] == "Resistance at 80"
    assert hidden["reviewed_count"] == historical["reviewed_count"] == 1
    assert not hidden["can_predict"]  # Seeing full history cannot restore a blind observation.
    assert service.repo.review_rows("A") == before
    assert ReplayRepository(service.repo.engine).get_session(sid)["mode"] == "backtesting"


def test_backtesting_api_blocks_future_intraday_fills_and_annotations_before_loading(service, monkeypatch):
    sid = service.open_session("scan", "A")["session"]["id"]
    service.repo.archive_intraday("A", intraday_frame(), "alpaca_sip")
    monkeypatch.setattr(service.provider, "fetch", lambda *a: pytest.fail("Never fetch hidden sessions"))
    day = dt.date(2025, 1, 3)
    point = {"time": int(intraday_frame().index[0].timestamp()), "price": 100}
    with TestClient(create_replay_app(service)) as client:
        base = f"/api/sessions/{sid}"
        assert client.get(base+"/intraday", params={"day": str(day)}).status_code == 400
        assert client.get(base+"/paper", params={"day": str(day)}).status_code == 400
        assert client.post(base+"/paper/studies", json={"day": str(day), "request_id": str(uuid.uuid4())}).status_code == 400
        assert client.post(base+"/paper/trades", json={"day": str(day), "time": point["time"], "price": 100,
            "action": "buy", "request_id": str(uuid.uuid4())}).status_code == 400
        assert client.post(base+"/drawings", json={"day": str(day), "frame": "5m", "kind": "horizontal", "points": [point]}).status_code == 400
        assert client.post(base+"/mode", json={"mode": "invalid"}).status_code == 422
        assert client.post(base+"/mode", json={"mode": "historical"}).status_code == 200
        result = client.get(base+"/intraday", params={"day": str(day)}).json()
        assert result["cached"] and result["mode"] == "historical" and not result["entry_preview"]
        assert result["daily_asof"] == "2025-01-02" and len(result["bars"]) == 4
    assert not service.repo.paper_action_rows("A") and not service.repo.review_rows("A")


def test_historical_day_selection_and_arrows_do_not_mark_skipped_days_reviewed(service):
    with TestClient(create_replay_app(service)) as client:
        opened = client.post("/api/sessions", json={"scan_id": "scan", "symbol": "A", "mode": "historical"}).json()
        base = f"/api/sessions/{opened['session']['id']}"
        selected = client.post(base+"/day", json={"day": "2025-01-07"}).json()
        assert selected["session"]["cursor"] == "2025-01-07" and selected["bars"] == opened["bars"]
        assert selected["metrics"] == metrics_as_of("A", history(), history(), dt.date(2025, 1, 7))
        assert selected["reviewed_count"] == 0
        assert client.post(base+"/day", json={"day": "2025-01-04"}).status_code == 400
        client.post(base+"/step", json={"direction": -1})
        client.post(base+"/step", json={"direction": 1})
        assert not service.repo.review_rows("A")
        client.post(base+"/mode", json={"mode": "backtesting"})
        assert client.post(base+"/day", json={"day": "2025-01-02"}).status_code == 400
        assert client.get(base).json()["session"]["mode"] == "backtesting"


def test_historical_expected_day_and_drawings_do_not_change_cursor_or_leak_on_rewind(service):
    initial = service.open_session("scan", "A", "historical")
    sid = initial["session"]["id"]
    selected = service.mark_expected_day(sid, dt.date(2025, 1, 7))
    assert selected["session"]["cursor"] == "2025-01-02"
    expected = selected["reviews"][0]
    assert expected["day"] == expected["expected_day"] == "2025-01-07"
    assert expected["metrics"]["retrospective_revision"] and not expected["metrics"]["blind_observation"]
    drawn = service.add_drawing(sid, "daily", [{"time": "2025-01-07", "price": 80}], "horizontal")
    assert drawn["drawings"][0]["day"] == "2025-01-07"
    hidden = service.set_mode(sid, "backtesting")
    assert hidden["session"]["cursor"] == "2025-01-02" and not hidden["reviews"] and not hidden["drawings"]
    assert len(service.repo.review_rows("A")) == 1
    service.set_mode(sid, "historical")
    service.select_day(sid, dt.date(2025, 1, 7))
    passed = service.save_review(sid, "pass")["current_review"]
    assert passed["metrics"]["observation_mode"] == "historical"
    assert not passed["metrics"]["blind_observation"]


def test_daily_preload_warms_fifty_sessions_and_avoids_repeated_history_reads(service, monkeypatch):
    future = pd.bdate_range("2025-01-08", periods=70)
    for symbol in ("A", "SPY"):
        records = [{"symbol": symbol, "date": stamp.to_pydatetime(), "interval": "1d", "updated_at": now(),
            "open": 80+i, "high": 83+i, "low": 77+i, "close": 81+i, "volume": 1000000}
            for i, stamp in enumerate(future)]
        service.repo.upsert(service.repo.daily, records, ("symbol", "date", "interval"))
    calls = []
    original = service.repo.histories
    def tracked(*args):
        calls.append(args)
        return original(*args)
    monkeypatch.setattr(service.repo, "histories", tracked)
    monkeypatch.setattr("src.services.breakout_replay.yf.download", lambda *a, **k: pytest.fail("Cached replay must not fetch Yahoo"))
    initial = service.open_session("scan", "A")
    sid = initial["session"]["id"]
    archive = service.daily_cache.get(service.repo.get_session(sid))
    assert initial["daily_preload"]["ahead"] >= 50
    assert len(archive.metrics) == 51
    assert len(calls) == 1 and calls[0][2] >= future[-1].date()
    assert all(row["time"] <= "2025-01-02" for row in initial["bars"])
    for _ in range(52):
        payload = service.step(sid, 1)
    assert len(calls) == 1
    day = dt.date.fromisoformat(payload["session"]["cursor"])
    expected = daily_chart(archive.chart_history, archive.chart_spy, day)
    assert payload["bars"] == expected["bars"]
    assert payload["metrics"] == metrics_as_of("A", archive.chart_history, archive.chart_spy, day)
    assert all(row["time"] <= day.isoformat() for row in payload["bars"])


def test_selecting_stock_again_refreshes_daily_archive_from_local_database(service):
    initial = service.open_session("scan", "A")
    assert initial["latest_day"] == "2025-01-07"
    for symbol in ("A", "SPY"):
        service.repo.upsert(service.repo.daily, [{"symbol": symbol, "date": dt.datetime(2025, 1, 8),
            "interval": "1d", "updated_at": now(), "open": 81, "high": 85, "low": 80,
            "close": 84, "volume": 1000000}], ("symbol", "date", "interval"))
    refreshed = service.open_session("scan", "A")
    assert refreshed["latest_day"] == "2025-01-08"
    assert refreshed["intraday_days"][-1] == "2025-01-02"
    assert service.set_mode(refreshed["session"]["id"], "historical")["intraday_days"][-1] == "2025-01-08"


def test_hindsight_intraday_opens_any_available_day_without_moving_daily_cursor(service):
    frame = intraday_frame()
    frame.index += pd.Timedelta(days=4)
    service.repo.archive_intraday("A", frame, "alpaca_sip")
    initial = service.open_session("scan", "A", "historical")
    sid = initial["session"]["id"]
    assert initial["intraday_days"] == [stamp.date().isoformat() for stamp in history().index]
    with TestClient(create_replay_app(service)) as client:
        result = client.get(f"/api/sessions/{sid}/intraday", params={"day": "2025-01-07"})
        assert result.status_code == 200
        assert result.json()["hindsight"] and result.json()["cached"]
        assert result.json()["daily_asof"] == "2025-01-02"
        assert result.json()["bars"][0]["time"] == int(frame.index[0].timestamp())
        assert client.get(f"/api/sessions/{sid}/intraday", params={"day": "2025-01-08"}).status_code == 400
    after = service.session_payload(sid)
    assert after["session"]["cursor"] == initial["session"]["cursor"]
    assert after["bars"] == initial["bars"] and after["metrics"] == initial["metrics"]
    assert not after["can_predict"] and after["reviewed_count"] == 0


def test_hindsight_drawings_and_snapshots_are_available_in_study_with_original_asof(service):
    day = dt.date(2025, 1, 7)
    frame = intraday_frame()
    future = frame.copy()
    future.index += pd.Timedelta(days=4)
    service.repo.archive_intraday("A", future, "alpaca_sip")
    service.repo.archive_intraday("A", frame, "alpaca_sip")
    sid = service.open_session("scan", "A", "historical")["session"]["id"]
    service.intraday(sid, day)
    earlier = dt.date(2025, 1, 3)
    point = {"time": int(frame.index[0].timestamp()), "price": 100}
    result = service.add_drawing(sid, "5m", [point], "horizontal", earlier)
    assert len(result["drawings"]) == 1
    assert len(result["study_drawings"]) == 1
    drawing = result["study_drawings"][0]
    assert drawing["day"] == "2025-01-07" and drawing["payload"]["session_day"] == "2025-01-03"
    study = service.intraday(sid, earlier)
    assert study["drawings"][0]["id"] == drawing["id"] and study["hindsight"]
    stream = BytesIO(); Image.new("RGB", (20, 20), "black").save(stream, format="PNG")
    service.save_snapshot(sid, "data:image/png;base64," + base64.b64encode(stream.getvalue()).decode(), "5m", earlier)
    snapshot = service.repo.rows(select(service.repo.snapshots))[0]
    assert snapshot["replay_asof"] == day
    assert not service.set_mode(sid, "backtesting")["snapshots"]
    service.set_mode(sid, "historical")
    assert service.delete_drawing(sid, drawing["id"])["study_drawings"] == []


def test_growth_percentile_is_calculated_before_filters():
    metrics = [{"symbol": str(i), "return_1m": i, "return_3m": i, "volume": 0 if i == 3 else 100} for i in range(4)]
    results = rank_and_scan(metrics, [{"attribute": "growth_rank_1m", "operator": ">=", "threshold": 75},
        {"attribute": "volume", "operator": ">=", "threshold": 1}])
    assert [row["symbol"] for row in results] == ["2"]
    assert results[0]["growth_rank_1m"] == 75


def test_scanner_selection_is_persisted_and_uses_separate_rules(service, monkeypatch, tmp_path):
    from src.ui.filter_catalog import DEFAULT_SCANNER_SETUPS

    monkeypatch.setattr("src.services.breakout_replay.DATA_DIR", tmp_path)
    monkeypatch.setattr(service, "_prepare_daily", lambda *args: None)
    monkeypatch.setattr(service.pool, "submit", lambda fn, *args: fn(*args))
    monkeypatch.setattr("src.services.breakout_replay.yf.download", lambda *a, **k: pytest.fail("Use cached prices"))
    with TestClient(create_replay_app(service)) as client:
        status = client.get("/api/status").json()
        assert status["scanner_setups"]["Setup 2"]["rules"] == DEFAULT_SCANNER_SETUPS["Setup 2"]["rules"]
        scan = client.post("/api/scans", json={"day": "2025-01-02", "setup_name": "Setup 2"}).json()
        result = client.get(f"/api/scans/{scan['id']}").json()
        assert result["scan"]["status"] == "complete"
        assert result["scan"]["setup_name"] == "Setup 2"
        assert result["scan"]["payload"]["rules"] == DEFAULT_SCANNER_SETUPS["Setup 2"]["rules"]
        assert result["results"][0]["metrics"]["adr_20_high_low"] > 5
        session = client.post("/api/sessions", json={"scan_id": scan["id"], "symbol": "A"}).json()
        assert session["session"]["setup_name"] == "Setup 2"
        service.repo.archive_intraday("A", intraday_frame(), "alpaca_sip")
        service.step(session["session"]["id"], 1)
        intraday = service.intraday(session["session"]["id"], dt.date(2025, 1, 3))
        assert intraday["adr"] == pytest.approx(session["metrics"]["adr_20_high_low"])
        original = client.post("/api/scans", json={"day": "2025-01-02", "setup_name": "Setup 1"}).json()
        original = client.get(f"/api/scans/{original['id']}").json()
        assert original["scan"]["setup_name"] == "Setup 1"
        assert original["scan"]["payload"]["rules"] == DEFAULT_SCANNER_SETUPS["Setup 1"]["rules"]
        assert client.post("/api/scans", json={"day": "2025-01-02", "setup_name": "Unknown"}).status_code == 400


def test_overlap_resumes_without_skipping_unreviewed_gaps(service):
    first = service.open_session("scan", "A")
    session_id = first["session"]["id"]
    assert first["session"]["cursor"] == "2025-01-02"
    service.save_review(session_id, "expected", notes="Tight resistance")
    stepped = service.step(session_id, 1)
    assert stepped["session"]["cursor"] == "2025-01-03"
    second = service.open_session("scan", "A")
    assert second["session"]["cursor"] == "2025-01-03"
    assert second["reviewed_count"] == 1
    rewind = service.step(session_id, -1)
    assert not rewind["can_predict"]
    revised = service.save_review(session_id, "pass")
    assert revised["current_review"]["metrics"]["retrospective_revision"]


def test_prediction_and_later_assessment_remain_separate_and_hidden_on_rewind(service):
    payload = service.open_session("scan", "A")
    sid = payload["session"]["id"]
    service.save_review(sid, "expected")
    service.step(sid, 1)
    service.save_review(sid, "pass", outcome="actual")
    service.step(sid, 1)
    service.step(sid, -1)
    service.save_review(sid, "pass", outcome="failed")
    rewind = service.session_payload(sid)
    assert rewind["current_review"]["outcome"] is None
    forecast = next(r for r in service.repo.review_rows("A") if r["decision"] == "expected")
    assert forecast["expected_day"] == dt.date(2025, 1, 3)
    assert forecast["outcome"] is None


def intraday_frame():
    index = pd.date_range("2025-01-03 09:30", periods=4, freq="5min", tz="America/New_York")
    return pd.DataFrame({"Open": [100,100,101,102], "High": [101,100.8,102,104],
        "Low": [99,99.8,100,101], "Close": [100,100.5,101.5,103], "Volume": 1000}, index=index)


def opening_frame(day="2025-01-03"):
    bars = pd.DataFrame({"Open": 100.0, "High": 100.8, "Low": 99.8, "Close": 100.0, "Volume": 1000},
        index=pd.date_range(f"{day} 09:30", periods=78, freq="5min", tz="America/New_York"))
    bars.iloc[0, :4] = [100, 101, 99, 100.5]
    bars.iloc[3, :4] = [100, 104, 99.8, 103]
    bars.iloc[5, :4] = [101, 103, 100, 102]
    bars.iloc[6, :4] = [102, 105, 101, 104]
    bars.iloc[7, :4] = [104, 105, 98, 102]
    return bars


@pytest.mark.parametrize("day,offset", [("2025-01-03", "-05:00"), ("2025-07-02", "-04:00")])
def test_opening_gains_use_window_close_and_peak_without_later_prices(day, offset):
    bars = opening_frame(day)
    result = opening_moves(bars, dt.date.fromisoformat(day))
    first, thirty = result["windows"]["5"], result["windows"]["30"]
    assert first["close_pct"] == pytest.approx(.5) and first["high_pct"] == pytest.approx(1)
    assert thirty["close_pct"] == pytest.approx(2) and thirty["high_pct"] == pytest.approx(4)
    assert thirty["volume"] == 6000 and thirty["end_time"].endswith("10:00:00"+offset)
    bars.iloc[6:, :4] *= 100
    assert opening_moves(bars, dt.date.fromisoformat(day)) == result


def test_opening_stats_and_thirty_minute_orb_require_complete_opening_bars():
    bars = opening_frame().drop(opening_frame().index[2])
    moves = opening_moves(bars, dt.date(2025, 1, 3))
    assert moves["windows"]["5"]["available"] and not moves["windows"]["30"]["available"]
    assert not orb_check(bars, dt.date(2025, 1, 3), 10, {}, opening_minutes=30)["available"]
    missing_open = opening_moves(bars.iloc[1:], dt.date(2025, 1, 3))
    assert not missing_open["windows"]["5"]["available"]


def test_thirty_minute_orb_uses_first_six_bars_and_triggers_after_ten():
    bars = opening_frame()
    five = orb_check(bars, dt.date(2025, 1, 3), 10, {"stop_adr_min_percent": 0}, opening_minutes=5)
    thirty = orb_check(bars, dt.date(2025, 1, 3), 10, {"stop_adr_min_percent": 0}, opening_minutes=30)
    assert five["entry_time"] == bars.index[3].isoformat()
    assert thirty["or_high"] == 104 and thirty["stop"] == 99
    assert thirty["entry_time"] == bars.index[6].isoformat()
    assert thirty["stop_time"] == bars.index[7].isoformat()


def test_entry_window_uses_same_archive_and_persists_in_trades_snapshots_and_events(service):
    bars = opening_frame()
    day = dt.date(2025, 1, 3)
    service.repo.archive_intraday("A", bars, "alpaca_sip")
    sid = service.open_session("scan", "A")["session"]["id"]
    service.step(sid, 1)
    five = service.intraday(sid, day)
    with TestClient(create_replay_app(service)) as client:
        path = f"/api/sessions/{sid}/intraday"
        result = client.get(path, params={"day": str(day), "interval_minutes": 30})
        thirty = result.json()
        assert result.status_code == 200 and thirty["cached"]
        assert thirty["bars"] == five["bars"] and thirty["opening_moves"] == five["opening_moves"]
        assert thirty["orb"]["opening_minutes"] == 30
        assert client.get(path, params={"day": str(day), "interval_minutes": 15}).status_code == 422
        trade = {"request_id": str(uuid.uuid4()), "day": str(day), "time": int(bars.index[6].timestamp()),
            "action": "buy", "price": 102, "stop_pct": 5, "interval_minutes": 30}
        assert client.post(f"/api/sessions/{sid}/paper/trades", json={**trade,
            "time": int(bars.index[0].timestamp()), "price": 100}).status_code == 400
        saved = client.post(f"/api/sessions/{sid}/paper/trades", json=trade)
        assert saved.status_code == 200 and saved.json()["interval_minutes"] == 30
    assert service.repo.paper_action_rows("A")[0]["entry_window_minutes"] == 30
    stream = BytesIO(); Image.new("RGB", (20, 20), "black").save(stream, format="PNG")
    service.save_snapshot(sid, "data:image/png;base64," + base64.b64encode(stream.getvalue()).decode(),
        "5m", day, interval_minutes=30)
    snapshot = service.repo.rows(select(service.repo.snapshots))[0]
    assert snapshot["frame"] == "30m" and "_30m_" in snapshot["path"]
    metadata = json.loads(snapshot["metadata"])["intraday"]
    assert metadata["interval_minutes"] == 30 and metadata["opening_moves"] == five["opening_moves"]
    event = service.repo.rows(select(service.repo.events).where(service.repo.events.c.kind == "intraday_opened"))[-1]
    assert json.loads(event["payload"])["interval_minutes"] == 30


def test_orb_uses_opening_low_not_future_day_low_and_requires_opening_bar():
    frame = intraday_frame()
    frame.iloc[-1, frame.columns.get_loc("Low")] = 20
    result = orb_check(frame, dt.date(2025, 1, 3), 5, {"stop_adr_min_percent": 15, "stop_adr_max_percent": 70})
    assert result["stop"] == 99
    assert result["trigger_time"].startswith("2025-01-03T09:40")
    assert result["stop_valid"]
    assert not result["complete"]
    missing = orb_check(frame.iloc[1:], dt.date(2025, 1, 3), 5, {})
    assert not missing["available"]


def test_orb_marks_entry_and_first_stop_after_entry_not_before():
    frame = intraday_frame()
    frame.loc[frame.index[1], "Low"] = 98
    entered = orb_check(frame, dt.date(2025, 1, 3), 5, {})
    assert entered["entry_taken"]
    assert entered["entry_time"] == frame.index[2].isoformat()
    assert entered["stop_hit"] is False
    frame.loc[frame.index[3], "Low"] = 98
    stopped = orb_check(frame, dt.date(2025, 1, 3), 5, {})
    assert stopped["trade_status"] == "stopped"
    assert stopped["stop_hit"] is True
    assert stopped["stop_time"] == frame.index[3].isoformat()
    assert stopped["stop"] == 99


@pytest.mark.parametrize("adr,trigger,reason", [
    (1, True, "outside configured bounds"),
    (None, True, "ADR unavailable"),
    (5, False, "not reached"),
])
def test_orb_distinguishes_no_entry_from_a_failed_trade(adr, trigger, reason):
    frame = intraday_frame()
    if not trigger:
        frame.loc[frame.index[1:], "High"] = 101
    result = orb_check(frame, dt.date(2025, 1, 3), adr, {})
    assert result["triggered"] is trigger
    assert result["trade_status"] == "no_entry"
    assert not result["entry_taken"]
    assert result["entry_time"] is None
    assert result["stop_hit"] is False and result["stop_time"] is None
    assert reason in result["no_entry_reason"]
    assert result["capital_ignored"]


@pytest.mark.parametrize("gap_open", [False, True])
def test_orb_handles_entry_and_stop_within_same_candle_without_inventing_order(gap_open):
    frame = intraday_frame()
    frame.loc[frame.index[2], "Low"] = 98
    if gap_open:
        frame.loc[frame.index[2], "Open"] = 102
    result = orb_check(frame, dt.date(2025, 1, 3), 5, {})
    assert result["entry_taken"]
    if gap_open:
        assert result["stop_hit"] is True
        assert result["stop_time"] == result["entry_time"]
        assert result["uncertain_stop_time"] is None
    else:
        assert result["stop_hit"] is None
        assert result["stop_time"] is None
        assert result["uncertain_stop_time"] == result["entry_time"]
        assert result["trade_status"] == "ambiguous"


def test_full_session_verdict_and_logged_trade_evidence(service):
    day = dt.date(2025, 1, 3)
    frame = pd.DataFrame({"Open": 100, "High": 100.8, "Low": 99.8, "Close": 100, "Volume": 1000},
        index=pd.date_range("2025-01-03 09:30", periods=78, freq="5min", tz="America/New_York"))
    frame.loc[frame.index[0], ["High", "Low"]] = [101, 99]
    frame.loc[frame.index[10], "High"] = 102
    frame.loc[frame.index[50], "Low"] = 98
    sid = service.open_session("scan", "A")["session"]["id"]
    service.step(sid, 1)
    service.repo.archive_intraday("A", frame, "alpaca_sip")
    data = service.intraday(sid, day)
    assert len(data["bars"]) == data["visible_bars"] == data["total_bars"] == 78
    assert data["orb"]["complete"]
    assert data["orb"]["entry_time"] == frame.index[10].isoformat()
    assert data["orb"]["stop_time"] == frame.index[50].isoformat()
    logged = service.repo.rows(select(service.repo.events).where(service.repo.events.c.kind == "intraday_opened"))[-1]
    assert json.loads(logged["payload"])["orb"] == data["orb"]


def paper_setup(service):
    sid = service.open_session("scan", "A")["session"]["id"]
    service.step(sid, 1)
    frame = intraday_frame()
    service.repo.archive_intraday("A", frame, "alpaca_sip")
    return sid, frame


def paper_action(service, sid, frame, side="buy", bar=0, price=100, stop_pct=5, request_id=None):
    return service.paper_trade(sid, frame.index[bar].date(), int(frame.index[bar].timestamp()),
        side, price, stop_pct, 50, "auto", request_id or str(uuid.uuid4()))


def test_mode_switches_keep_open_study_and_future_fill_markers_follow_visibility(service):
    sid, frame = paper_setup(service)
    bought = paper_action(service, sid, frame)
    identity = bought["paper"]["study_id"]
    action_rows = service.repo.rows(select(service.repo.paper_actions))
    historical = service.set_mode(sid, "historical")
    assert historical["paper"]["study_id"] == identity
    assert historical["paper"]["quantity"] == bought["paper"]["quantity"] == 20
    assert historical["paper"]["nav"] == bought["paper"]["nav"]
    service.step(sid, -1)
    earlier = service.session_payload(sid)
    assert earlier["paper"]["quantity"] == 0  # Money reflects the selected day.
    assert len(earlier["paper"]["chart_fills"]) == 1  # Saved buy remains visible in all-history view.
    hidden = service.set_mode(sid, "backtesting")
    assert not hidden["paper"]["chart_fills"]
    restored = service.step(sid, 1)
    assert restored["paper"]["study_id"] == identity and restored["paper"]["quantity"] == 20
    assert service.repo.rows(select(service.repo.paper_actions)) == action_rows


def test_historical_snapshot_and_later_reviews_are_visible_only_in_historical_view(service):
    initial = service.open_session("scan", "A")
    sid = initial["session"]["id"]
    service.set_mode(sid, "historical")
    service.select_day(sid, dt.date(2025, 1, 7))
    service.save_review(sid, "expected", outcome="actual", notes="Actual entry day")
    service.select_day(sid, dt.date(2025, 1, 2))
    stream = BytesIO(); Image.new("RGB", (20, 20), "black").save(stream, format="PNG")
    saved = service.save_snapshot(sid, "data:image/png;base64," + base64.b64encode(stream.getvalue()).decode())
    assert "_historical_" in saved["path"]
    metadata = json.loads(service.repo.rows(select(service.repo.snapshots))[0]["metadata"])
    assert metadata["mode"] == "historical" and metadata["chart_through"] == "2025-01-07"
    assert metadata["metrics"] == initial["metrics"]
    history = service.session_payload(sid)
    assert history["reviews"][0]["outcome"] == "actual" and len(history["snapshots"]) == 1
    hidden = service.set_mode(sid, "backtesting")
    assert not hidden["reviews"] and not hidden["snapshots"]
    assert hidden["bars"] == initial["bars"]


def test_independent_studies_keep_closed_results_without_compounding(service):
    sid, frame = paper_setup(service)
    bought = paper_action(service, sid, frame)
    old_id = bought["paper"]["study_id"]
    sold = paper_action(service, sid, frame, "sell_all", 3, 103)
    assert sold["paper"]["nav"] == 10060
    with pytest.raises(ValueError, match="study has closed"):
        paper_action(service, sid, frame, bar=3, price=103)
    fresh_id = service.create_paper_study(sid, frame.index[0].date(), str(uuid.uuid4()))["study_id"]
    assert fresh_id != old_id
    fresh = service.paper_state(sid, frame.index[0].date(), study_id=fresh_id)
    assert fresh["nav"] == 10000 and fresh["risk_budget"] == 100 and fresh["fills"] == []
    saved = service.paper_state(sid, frame.index[0].date(), study_id=old_id)
    assert saved["nav"] == 10060 and saved["realized_pnl"] == 60
    assert service.paper.reconcile("SPY", frame.index[0].date())["nav"] == 10000
    # New entries on the same stock and day can be tested earlier in their own study.
    new = service.paper_trade(sid, frame.index[0].date(), int(frame.index[0].timestamp()),
        "buy", 100, 5, 50, "auto", str(uuid.uuid4()), study_id=fresh_id)
    assert new["paper"]["fills"][0]["risk_budget"] == 100
    assert service.paper_state(sid, frame.index[0].date(), study_id=old_id)["nav"] == 10060
    assert len(service.repo.rows(select(service.repo.paper_fills))) == 3
    restarted = ReplayService(ReplayRepository(service.repo.engine), service.snapshot_dir)
    try:
        assert restarted.paper_state(sid, frame.index[0].date(), study_id=old_id)["nav"] == 10060
        assert restarted.paper_state(sid, frame.index[0].date(), study_id=fresh_id)["quantity"] == 20
    finally:
        restarted.pool.shutdown(wait=True)


def test_open_study_follows_future_stops_then_next_entry_starts_fresh(service):
    sid, frame = paper_setup(service)
    first = paper_action(service, sid, frame)
    identity = first["paper"]["study_id"]
    next_day = frame.copy()
    next_day.index += pd.Timedelta(days=3)
    next_day.iloc[0, next_day.columns.get_loc("Low")] = 94
    service.repo.archive_intraday("A", next_day, "alpaca_sip")
    service.step(sid, 1)
    result = service.paper_state(sid, next_day.index[0].date())
    assert result["study_id"] == identity and result["nav"] == 9900
    assert result["fills"][-1]["reason"] == "stop_loss"
    service.step(sid, 1)
    future = service.paper_state(sid, dt.date(2025, 1, 7))
    assert future["study_id"] is None and future["nav"] == 10000 and future["risk_budget"] == 100
    old = service.paper_state(sid, dt.date(2025, 1, 7), study_id=identity)
    assert old["nav"] == 9900


def test_saved_cumulative_account_remains_available_without_affecting_new_studies(service):
    sid, frame = paper_setup(service)
    action = {"id": str(uuid.uuid4()), "day": frame.index[0].date(), "time": int(frame.index[0].timestamp()),
        "action": "buy", "price": 100, "stop": 95, "percent": 50, "sequence": 0, "shares": 20}
    result = service.paper._calculate("A", frame.index[0].date(), [action], "auto")
    service.repo.save_paper_projection("A", result, action)
    before = service.repo.rows(select(service.repo.paper_accounts))[0]["payload"]
    fresh = service.paper_state(sid, frame.index[0].date())
    assert fresh["nav"] == 10000 and any(s["id"] == "legacy" for s in fresh["studies"])
    old = service.paper_state(sid, frame.index[0].date(), study_id="legacy")
    assert old["nav"] == 10060 and old["read_only"]
    paper_action(service, sid, frame)
    assert service.repo.rows(select(service.repo.paper_accounts))[0]["payload"] == before
    assert service.paper_state(sid, frame.index[0].date(), study_id="legacy")["nav"] == 10060


def test_2024_replay_and_entry_are_independent_of_saved_2025_study(service):
    sid, frame = paper_setup(service)
    old = paper_action(service, sid, frame)
    earlier = frame.copy()
    earlier.index = pd.date_range("2024-06-03 09:30", periods=4, freq="5min", tz="America/New_York")
    service.repo.archive_intraday("A", earlier, "alpaca_sip")
    service.repo.upsert(service.repo.scans, [{"id": "2024", "requested_day": dt.date(2024, 6, 3),
        "scan_day": dt.date(2024, 6, 3), "status": "complete", "payload": encode({}), "created_at": now()}], ("id",))
    service.repo.upsert(service.repo.results, [{"scan_id": "2024", "symbol": "A", "metrics": encode({})}], ("scan_id", "symbol"))
    replay = service.open_session("2024", "A")
    assert replay["session"]["cursor"] == "2024-06-03" and replay["paper"]["nav"] == 10000
    result = paper_action(service, replay["session"]["id"], earlier)
    assert result["paper"]["fills"][0]["risk_budget"] == 100
    assert result["paper"]["study_id"] != old["paper"]["study_id"]
    assert service.paper_state(sid, frame.index[0].date(), study_id=old["paper"]["study_id"])["nav"] == 10060


def test_future_chart_drawings_are_saved_without_revealing_prices(service):
    replay = service.open_session("scan", "A")
    sid = replay["session"]["id"]
    points = [{"time": "2025-01-02", "price": 100}, {"time": "2026-06-01", "price": 120}]
    result = service.add_drawing(sid, "daily", points, "line")
    assert result["drawings"][0]["payload"]["points"] == points
    assert result["drawings"][0]["day"] == "2025-01-02"
    assert result["bars"] == replay["bars"] and result["metrics"] == replay["metrics"]
    assert len(result["projection_days"]) == 120
    assert all(day > "2025-01-02" for day in result["projection_days"])
    assert all(set(bar) != {"time"} and bar["time"] <= "2025-01-02" for bar in result["bars"])
    assert result["can_predict"]


def test_passed_day_can_be_marked_expected_without_moving_replay_or_losing_notes(service):
    sid = service.open_session("scan", "A")["session"]["id"]
    service.step(sid, 1)
    service.save_review(sid, "pass", notes="Tight consolidation", pattern="Up flat flag")
    later = service.step(sid, 1)
    result = service.mark_expected_day(sid, dt.date(2025, 1, 3))
    assert result["session"]["cursor"] == later["session"]["cursor"]
    revised = next(r for r in service.repo.review_rows("A") if r["day"] == dt.date(2025, 1, 3))
    assert revised["decision"] == "expected" and revised["expected_day"] == dt.date(2025, 1, 3)
    assert revised["notes"] == "Tight consolidation" and revised["pattern"] == "Up flat flag"
    assert revised["metrics"]["blind_observation"] is False and revised["metrics"]["retrospective_revision"]
    audit = service.repo.rows(select(service.repo.events).where(service.repo.events.c.kind == "review_revised"))[-1]
    assert json.loads(audit["payload"])["before"]["decision"] == "pass"


def test_study_http_creation_is_idempotent_and_does_not_replace_other_stock(service):
    sid, frame = paper_setup(service)
    identity = str(uuid.uuid4())
    with TestClient(create_replay_app(service)) as client:
        body = {"day": "2025-01-03", "request_id": identity}
        path = f"/api/sessions/{sid}/paper/studies"
        first = client.post(path, json=body)
        assert first.status_code == 200 and first.json()["study_id"] == identity
        assert client.post(path, json=body).json() == first.json()
        assert client.post(path, json={**body, "day": "2025-01-08"}).status_code == 400
        invalid = client.get(f"/api/sessions/{sid}/paper", params={"day": "2025-01-03", "study_id": "unknown"})
        assert invalid.status_code == 400
    assert len(service.repo.paper_study_rows("A")) == 1
    with pytest.raises(ValueError, match="different stock"):
        service.paper.create_study("SPY", frame.index[0].date(), identity)
    assert service.repo.paper_study_rows("A")[0]["id"] == identity


def test_snapshot_and_revision_http_use_the_selected_study_and_day(service):
    sid, frame = paper_setup(service)
    old = paper_action(service, sid, frame)
    paper_action(service, sid, frame, "sell_all", 3, 103)
    service.create_paper_study(sid, frame.index[0].date(), str(uuid.uuid4()))
    stream = BytesIO()
    Image.new("RGB", (20, 20), "black").save(stream, format="PNG")
    service.save_snapshot(sid, "data:image/png;base64," + base64.b64encode(stream.getvalue()).decode(),
        "5m", frame.index[0].date(), study_id=old["paper"]["study_id"])
    snapshot = service.repo.rows(select(service.repo.snapshots))[-1]
    saved = json.loads(snapshot["metadata"])["intraday"]["paper"]
    assert saved["study_id"] == old["paper"]["study_id"] and saved["nav"] == 10060
    service.step(sid, 1)
    with TestClient(create_replay_app(service)) as client:
        result = client.post(f"/api/sessions/{sid}/expect-day", json={"day": "2025-01-03"})
        assert result.status_code == 200
        assert result.json()["session"]["cursor"] == "2025-01-06"
        csv = client.get("/api/export.csv").text
        assert "retrospective_revision" in csv and "True" in csv


def test_paper_stop_checks_reuse_loaded_bars_and_candle_preview_does_not_truncate_cache(service, monkeypatch):
    sid, frame = paper_setup(service)
    paper_action(service, sid, frame)
    monkeypatch.setattr(service.paper, "archive_loader", lambda *a: pytest.fail("Loaded stop data should stay in memory"))
    prefix = service.paper.reconcile("A", dt.date(2025, 1, 3), cutoff=int(frame.index[0].timestamp()))
    assert prefix["nav"] == 10000
    assert service.paper.reconcile("A", dt.date(2025, 1, 3))["nav"] == 10060


def test_paper_buy_partial_sell_and_sell_all_are_durable_and_marked_to_market(service):
    sid, frame = paper_setup(service)
    bought = paper_action(service, sid, frame)
    assert bought["paper"]["quantity"] == 20
    assert bought["paper"]["cash"] == 8000
    assert bought["paper"]["nav"] == 10060
    assert bought["paper"]["unrealized_pnl"] == 60
    at_entry = service.paper_state(sid, frame.index[0].date(), time=int(frame.index[0].timestamp()))
    assert at_entry["nav"] == 10000
    partial = paper_action(service, sid, frame, "partial_sell", 2, 101.5)
    assert partial["paper"]["quantity"] == 10
    assert partial["paper"]["realized_pnl"] == 15
    sold = paper_action(service, sid, frame, "sell_all", 3, 103)
    assert sold["paper"]["quantity"] == 0
    assert sold["paper"]["cash"] == sold["paper"]["nav"] == 10045
    restarted = ReplayService(ReplayRepository(service.repo.engine), service.snapshot_dir)
    try:
        assert restarted.paper_state(sid, frame.index[0].date())["nav"] == 10045
        assert len(restarted.repo.paper_action_rows("A")) == 3
        assert restarted.paper.reconcile("SPY", frame.index[0].date())["nav"] == 10000
    finally:
        restarted.pool.shutdown(wait=True)


def test_paper_retry_is_idempotent_and_rewind_hides_future_trades(service):
    sid, frame = paper_setup(service)
    identity = str(uuid.uuid4())
    first = paper_action(service, sid, frame, request_id=identity)
    again = paper_action(service, sid, frame, request_id=identity)
    assert first["paper"]["fills"] == again["paper"]["fills"]
    assert len(service.repo.paper_action_rows("A")) == 1
    rewind = service.paper_state(sid, dt.date(2025, 1, 2))
    assert rewind["nav"] == 10000 and not rewind["fills"]
    with pytest.raises(ValueError, match="chronologically"):
        service.paper.trade("A", dt.date(2025, 1, 3), int(frame.index[0].timestamp()) - 300,
            "buy", 100, 5, 50, "auto", str(uuid.uuid4()), study_id=first["paper"]["study_id"])


def test_partial_sale_before_projected_stop_rebuilds_remaining_shares_and_same_bar_order(service):
    sid, frame = paper_setup(service)
    frame.iloc[-1, frame.columns.get_loc("Low")] = 90
    service.repo.archive_intraday("A", frame, "alpaca_sip")
    first = paper_action(service, sid, frame)
    assert first["paper"]["nav"] == 9900
    partial = paper_action(service, sid, frame, "partial_sell", 0, 100)
    assert partial["paper"]["nav"] == 9950
    assert [f["shares"] for f in partial["paper"]["fills"]] == [20, 10, 10]
    rows = service.repo.rows(select(service.repo.paper_fills))
    stop = next(json.loads(row["payload"]) for row in rows if row["id"].startswith("stop:"))
    assert stop["shares"] == 10
    at_entry = service.paper_state(sid, frame.index[0].date(), time=int(frame.index[0].timestamp()))
    assert at_entry["quantity"] == 10 and at_entry["cash"] == 9000


def test_paper_snapshot_saves_nav_and_trade_evidence(service):
    sid, frame = paper_setup(service)
    paper_action(service, sid, frame)
    stream = BytesIO()
    Image.new("RGB", (20, 20), "black").save(stream, format="PNG")
    image = "data:image/png;base64," + base64.b64encode(stream.getvalue()).decode()
    service.save_snapshot(sid, image, "5m", frame.index[0].date())
    saved = service.repo.rows(select(service.repo.snapshots))[0]
    paper = json.loads(saved["metadata"])["intraday"]["paper"]
    assert paper["nav"] == 10060
    assert paper["fills"][0]["shares"] == 20


def test_paper_stop_is_checked_on_later_daily_step_and_reuses_raw_intraday_cache(service):
    sid, frame = paper_setup(service)
    paper_action(service, sid, frame)
    next_day = frame.copy()
    next_day.index += pd.Timedelta(days=3)
    next_day.iloc[0, next_day.columns.get_loc("Open")] = 90
    next_day.iloc[0, next_day.columns.get_loc("Low")] = 89
    service.repo.archive_intraday("A", next_day, "alpaca_sip")
    class Offline:
        def resolve(self, *_): pytest.fail("Saved bars must be reused")
        def fetch(self, *_): pytest.fail("Saved bars must be reused")
    service.provider = Offline()
    stepped = service.step(sid, 1)
    assert stepped["paper"]["quantity"] == 0
    assert stepped["paper"]["nav"] == 9800
    assert stepped["paper"]["fills"][-1]["reason"] == "stop_gap"
    assert stepped["paper"]["fills"][-1]["price"] == 90
    service.session_payload(sid)
    stops = service.repo.rows(select(service.repo.events).where(service.repo.events.c.kind == "paper_stop_fill"))
    assert len(stops) == 1


def test_paper_missing_stop_session_is_pending_and_cannot_be_bypassed_by_future_sell(service):
    sid, frame = paper_setup(service)
    paper_action(service, sid, frame)
    from src.services.replay_intraday import IntradayUnavailable
    class Missing:
        def resolve(self, source): return "alpaca_sip"
        def fetch(self, *_): raise IntradayUnavailable("No bars for this session")
    service.provider = Missing()
    stepped = service.step(sid, 1)
    assert stepped["paper"]["pending"]["day"] == "2025-01-06"
    assert stepped["paper"]["nav"] == 10060
    with pytest.raises(ValueError, match="missing five-minute"):
        service.paper.trade("A", dt.date(2025, 1, 6), int(frame.index[0].timestamp()) + 86400*3,
            "sell_all", 100, 5, 50, "auto", str(uuid.uuid4()))
    assert len(service.repo.paper_action_rows("A")) == 1


def test_daily_navigation_never_waits_for_uncached_intraday_stop_check(service, monkeypatch):
    sid, frame = paper_setup(service)
    paper_action(service, sid, frame)
    monkeypatch.setattr(service.paper, "archive_loader",
        lambda *args: pytest.fail("Daily navigation must not fetch missing intraday bars"))
    stepped = service.step(sid, 1)
    assert stepped["paper"]["pending"]["day"] == "2025-01-06"
    assert "intraday study" in stepped["paper"]["pending"]["reason"]
    assert stepped["paper"]["nav"] == 10060
    assert stepped["paper"]["quantity"] == 20


def test_paper_http_trade_validation_and_future_session_guard(service):
    sid, frame = paper_setup(service)
    body = {"request_id": str(uuid.uuid4()), "day": "2025-01-03", "time": int(frame.index[0].timestamp()),
        "action": "buy", "price": 100, "stop_pct": 5}
    with TestClient(create_replay_app(service)) as client:
        path = f"/api/sessions/{sid}/paper/trades"
        assert client.post(path, json={**body, "stop_pct": 0}).status_code == 422
        assert client.post(path, json={**body, "day": "2025-01-06", "time": body["time"]+86400*3}).status_code == 400
        result = client.post(path, json=body)
        assert result.status_code == 200 and result.json()["paper"]["quantity"] == 20
        assert client.post(path, json=body).json()["paper"]["quantity"] == 20
        assert client.post(path, json=body, headers={"Origin": "https://evil.test"}).status_code == 403
        state = client.get(f"/api/sessions/{sid}/paper", params={"day": "2025-01-03", "time": body["time"]})
        assert state.status_code == 200 and state.json()["nav"] == 10000


def test_intraday_response_returns_full_session_and_reuses_mysql_archive(service):
    class Provider:
        calls = 0
        def resolve(self, source): return "alpaca_sip"
        def fetch(self, symbol, day, source):
            self.calls += 1
            return intraday_frame(), source
    provider = Provider()
    service.provider = provider
    sid = service.open_session("scan", "A", "historical")["session"]["id"]
    study = service.intraday(sid, dt.date(2025, 1, 3))
    assert study["hindsight"] and not study["entry_preview"]
    assert service.session_payload(sid)["session"]["cursor"] == "2025-01-02"
    service.step(sid, 1)
    initial = service.intraday(sid, dt.date(2025, 1, 3))
    assert len(initial["bars"]) == initial["total_bars"] == 4
    assert initial["orb"]["triggered"]
    repeated = service.intraday(sid, dt.date(2025, 1, 3))
    assert repeated["bars"] == initial["bars"]
    assert repeated["cached"]
    assert provider.calls == 1


def test_request_limiter_enforces_rolling_minute_even_across_pages():
    current = [0.0]
    sleeps = []
    def sleep(seconds):
        sleeps.append(seconds)
        current[0] += seconds
    limiter = RequestLimiter(limit=2, clock=lambda: current[0], sleep=sleep)
    limiter.acquire(); limiter.acquire(); limiter.acquire()
    assert sleeps and current[0] >= 60
    assert len(limiter.timestamps) <= 2


def test_alpaca_uses_sip_pagination_headers_and_dedicated_data_endpoint(monkeypatch):
    values = {"ALPACA_API_KEY_ID": "test-key", "ALPACA_API_SECRET_KEY": "test-secret", "ALPACA_ENDPOINT": "https://paper-api.alpaca.markets/v2"}
    monkeypatch.setattr("src.services.replay_intraday.get_env_value", lambda key, default=None: values.get(key, default))
    calls = []
    class Response:
        status_code = 200
        def __init__(self, page): self.page = page
        def json(self):
            return {"bars": {"A": [{"t": "2025-01-03T14:30:00Z" if self.page == 1 else "2025-01-03T14:35:00Z", "o": 1, "h": 2, "l": 1, "c": 2, "v": 100}]}, "next_page_token": "next" if self.page == 1 else None}
    def request(url, **kwargs):
        calls.append((url, dict(kwargs["params"]), kwargs["headers"]))
        return Response(len(calls))
    monkeypatch.setattr("src.services.replay_intraday.requests.get", request)
    provider = ReplayIntradayProvider()
    frame, source = provider.fetch("A", dt.date(2025, 1, 3), "alpaca_sip")
    assert source == "alpaca_sip" and len(frame) == 2
    assert calls[0][0] == "https://data.alpaca.markets/v2/stocks/bars"
    assert calls[0][1]["feed"] == "sip" and calls[0][1]["adjustment"] == "raw"
    assert calls[1][1]["page_token"] == "next"
    assert len(provider.alpaca_limiter.timestamps) == 2
    assert provider.alpaca_limiter.limit == 180


def test_expected_entry_stays_hidden_until_backtesting_replay_advances(service, monkeypatch):
    monkeypatch.setattr(service.provider, "resolve", lambda source: "alpaca_sip")
    service.repo.archive_intraday("A", intraday_frame(), "alpaca_sip")
    sid = service.open_session("scan", "A")["session"]["id"]
    predicted = service.save_review(sid, "expected", notes="Tight range")
    assert predicted["entry_preview_day"] is None
    with pytest.raises(ValueError, match="Future intraday sessions are hidden"):
        service.intraday(sid, dt.date(2025, 1, 3))
    assert service.session_payload(sid)["can_predict"]
    service.step(sid, 1)
    entry = service.intraday(sid, dt.date(2025, 1, 3))
    assert not entry["entry_preview"] and len(entry["bars"]) == 4
    replay = service.session_payload(sid)
    assert replay["session"]["cursor"] == "2025-01-03"
    assert all(row["time"] <= "2025-01-03" for row in replay["bars"])
    assert replay["reviews"][0]["notes"] == "Tight range"
    assert replay["reviews"][0]["expected_day"] == "2025-01-03"
    with pytest.raises(ValueError, match="available completed"):
        service.intraday(sid, dt.date(2025, 1, 8))
    stream = BytesIO(); Image.new("RGB", (20, 20), "black").save(stream, format="PNG")
    service.save_snapshot(sid, "data:image/png;base64," + base64.b64encode(stream.getvalue()).decode(), "5m", dt.date(2025, 1, 3))
    saved = service.repo.rows(select(service.repo.snapshots))[0]
    metadata = json.loads(saved["metadata"])
    assert metadata["metrics"]["price"] == replay["metrics"]["price"]
    assert metadata["mode"] == "backtesting" and metadata["chart_through"] == "2025-01-03"
    assert len(metadata["intraday"]["bars"]) == 4
    assert "5m_unreviewed_" in saved["path"]
    point = {"time": int(intraday_frame().index[0].timestamp()), "price": 100}
    drawn = service.add_drawing(sid, "5m", [point], "horizontal", dt.date(2025, 1, 3))
    assert drawn["drawings"][0]["payload"]["intraday_asof"] == int(intraday_frame().index[-1].timestamp())
    assert not service.delete_drawing(sid, drawn["drawings"][0]["id"])["drawings"]
    point["time"] = int(intraday_frame().index[-1].timestamp()) + 300
    with pytest.raises(ValueError, match="revealed intraday"):
        service.add_drawing(sid, "5m", [point], "horizontal", dt.date(2025, 1, 3))
    assert service.step(sid, 1)["session"]["cursor"] == "2025-01-06"


def test_snapshot_saved_with_named_path_and_database_metadata(service):
    sid = service.open_session("scan", "A")["session"]["id"]
    stream = BytesIO(); Image.new("RGB", (20, 20), "black").save(stream, format="PNG")
    saved = service.save_snapshot(sid, "data:image/png;base64," + base64.b64encode(stream.getvalue()).decode())
    assert "A_2025-01-02_daily_unreviewed_" in saved["path"]
    assert service.repo.rows(select(service.repo.snapshots))[0]["id"] == saved["id"]


def test_http_rejects_cross_site_mutations_and_unsupported_steps(service):
    with TestClient(create_replay_app(service)) as client:
        bad = client.post("/api/scans", json={"day": "2025-01-02"}, headers={"Origin": "https://unrelated.example"})
        assert bad.status_code == 403
        opened = client.post("/api/sessions", json={"scan_id": "scan", "symbol": "A"}).json()
        sid = opened["session"]["id"]
        assert client.post(f"/api/sessions/{sid}/step", json={"direction": 20}).status_code == 400
        assert client.get("/").status_code == 200


def test_next_session_skips_new_year_and_weekends():
    assert next_session(dt.date(2024, 12, 31)) == dt.date(2025, 1, 2)
    assert next_session(dt.date(2025, 1, 3)) == dt.date(2025, 1, 6)
    assert next_session(dt.date(2025, 1, 8)) == dt.date(2025, 1, 10)


def test_earlier_sessions_can_be_revised_with_retrospective_evidence(service):
    sid = service.open_session("scan", "A")["session"]["id"]
    service.repo.set_cursor(sid, "A", dt.date(2025, 1, 7))
    service.repo.set_cursor(sid, "A", dt.date(2025, 1, 2))
    revised = service.save_review(sid, "expected")
    assert revised["current_review"]["metrics"]["retrospective_revision"]
    assert service.step(sid, 1)["session"]["cursor"] == "2025-01-03"
    review = service.repo.review_rows("A")[0]
    assert review["decision"] == "expected"
    assert review["metrics"]["blind_observation"] is False
    assert review["metrics"]["observation_asof"] == "2025-01-07"


def test_later_intraday_drawings_and_snapshots_are_hidden_on_rewind(service, monkeypatch):
    monkeypatch.setattr(service.provider, "resolve", lambda source: "alpaca_sip")
    sid = service.open_session("scan", "A")["session"]["id"]
    service.step(sid, 1)
    service.step(sid, 1)
    day = dt.date(2025, 1, 3)
    service.repo.archive_intraday("A", intraday_frame(), "alpaca_sip")
    point = {"time": int(intraday_frame().index[0].timestamp()), "price": 100}
    service.add_drawing(sid, "5m", [point], "horizontal", day)
    stream = BytesIO(); Image.new("RGB", (20, 20), "black").save(stream, format="PNG")
    image = "data:image/png;base64," + base64.b64encode(stream.getvalue()).decode()
    service.save_snapshot(sid, image, "5m", day, visible_bars=1, source="alpaca_sip")
    row = service.repo.rows(select(service.repo.snapshots))[0]
    metadata = json.loads(row["metadata"])
    assert row["replay_asof"] == dt.date(2025, 1, 6)
    assert len(metadata["intraday"]["bars"]) == 1
    assert metadata["metrics"]["price"] == float(history().loc["2025-01-03", "Close"])
    assert not service.step(sid, -1)["drawings"]
    assert not service.session_payload(sid)["snapshots"]
    with pytest.raises(ValueError, match="Invalid snapshot"):
        service.save_snapshot(sid, image, "daily", dt.date(2025, 1, 2))


def test_scan_date_does_not_jump_months_when_spy_cache_is_short(service, monkeypatch):
    captured = []
    monkeypatch.setattr(service.pool, "submit", lambda *args: captured.append(args))
    scan = service.start_scan(dt.date(2025, 1, 1))
    assert service.repo.get_scan(scan["id"])["scan_day"] == dt.date(2025, 1, 2)
    assert captured[0][2] == dt.date(2025, 1, 2)


def test_daily_backfill_is_persisted_and_reused_on_later_scans(service, monkeypatch):
    calls = []
    with service.repo.engine.begin() as conn:
        conn.execute(delete(service.repo.daily))
    service.repo.upsert(service.repo.daily, [{"symbol": symbol, "date": dt.datetime(2023, 12, 29),
        "interval": "1d", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1000, "updated_at": now()}
        for symbol in ("A", "SPY")], ("symbol", "date", "interval"))
    monkeypatch.setattr("src.services.breakout_replay.expected_latest_market_data_date", lambda: dt.date(2025, 1, 7))
    frame = pd.concat({"A": history(), "SPY": history()}, axis=1)
    def download(symbols, **kwargs):
        calls.append(symbols)
        assert kwargs["auto_adjust"] is True and kwargs["interval"] == "1d"
        return frame
    monkeypatch.setattr("src.services.breakout_replay.yf.download", download)
    service._prepare_daily("scan", dt.date(2025, 1, 2), [])
    manifests = service.repo.rows(select(service.repo.daily_imports))
    assert len(manifests) == 2 and all(row["status"] == "complete" for row in manifests)
    service._prepare_daily("scan", dt.date(2025, 1, 6), [])
    assert len(calls) == 1
    restarted = ReplayService(ReplayRepository(service.repo.engine), service.snapshot_dir)
    try:
        restarted._prepare_daily("scan", dt.date(2025, 1, 7), [])
        assert len(calls) == 1
    finally:
        restarted.pool.shutdown(wait=True)


def test_new_market_session_uses_incremental_daily_download(service, monkeypatch):
    service.repo.upsert(service.repo.daily_imports, [{"symbol": symbol, "start_day": dt.date(2024, 1, 1),
        "end_day": dt.date(2025, 1, 7), "status": "complete", "row_count": 160, "updated_at": now()}
        for symbol in ("A", "SPY")], ("symbol",))
    monkeypatch.setattr("src.services.breakout_replay.expected_latest_market_data_date", lambda: dt.date(2025, 1, 8))
    calls = []
    def download(symbols, **kwargs):
        calls.append(kwargs)
        bars = history()
        bars.loc[:, ["Open", "High", "Low", "Close"]] = 999
        bars.loc[pd.Timestamp("2025-01-08")] = [81, 83, 80, 82, 1000000]
        return pd.concat({"A": bars, "SPY": bars}, axis=1)
    monkeypatch.setattr("src.services.breakout_replay.yf.download", download)
    service._prepare_daily("scan", dt.date(2025, 1, 8), [])
    assert len(calls) == 1
    assert calls[0]["start"] == "2025-01-08"
    assert calls[0]["end"] == "2025-01-09"
    saved = service.repo.histories(["A"], dt.date(2025, 1, 7), dt.date(2025, 1, 8))["A"]
    assert list(saved["Close"]) == [80, 82]
    service._prepare_daily("scan", dt.date(2025, 1, 8), [])
    assert len(calls) == 1


def test_unavailable_daily_symbols_are_retried_after_negative_cache_expires(service, monkeypatch):
    end = dt.date(2025, 1, 7)
    monkeypatch.setattr("src.services.breakout_replay.expected_latest_market_data_date", lambda: end)
    with service.repo.engine.begin() as conn:
        conn.execute(delete(service.repo.daily).where(service.repo.daily.c.symbol == "A"))
    service.repo.upsert(service.repo.daily, [{"symbol": "A", "date": dt.datetime(2023, 12, 29),
        "interval": "1d", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1000, "updated_at": now()}],
        ("symbol", "date", "interval"))
    service.repo.upsert(service.repo.daily_imports, [
        {"symbol": symbol, "start_day": dt.date(2024, 1, 1), "end_day": end,
            "status": "complete" if symbol == "SPY" else "unavailable", "row_count": 0, "updated_at": now()}
        for symbol in ("A", "SPY")], ("symbol",))
    calls = []
    def download(symbols, **kwargs):
        calls.append(symbols)
        return history()
    monkeypatch.setattr("src.services.breakout_replay.yf.download", download)
    service._prepare_daily("scan", end, [])
    assert not calls
    with service.repo.engine.begin() as conn:
        conn.execute(update(service.repo.daily_imports).where(service.repo.daily_imports.c.symbol == "A")
            .values(updated_at=now()-dt.timedelta(hours=7)))
    service._prepare_daily("scan", end, [])
    assert calls == [["A"]]


def test_cached_historical_dates_do_not_refresh_when_latest_market_date_advances(service, monkeypatch):
    monkeypatch.setattr("src.services.breakout_replay.expected_latest_market_data_date", lambda: dt.date(2026, 10, 8))
    calls = []
    monkeypatch.setattr("src.services.breakout_replay.yf.download", lambda *args, **kwargs: calls.append(args))
    # Existing daily bars also work without this workspace's import manifest.
    for day in (dt.date(2025, 1, 2), dt.date(2025, 1, 3), dt.date(2025, 1, 7)):
        service._prepare_daily("scan", day, [])
    assert not calls
    assert not service.repo.rows(select(service.repo.daily_requests))


def test_empty_missing_tail_preserves_saved_history_and_uses_negative_cache(service, monkeypatch):
    service.repo.upsert(service.repo.daily_imports, [{"symbol": symbol, "start_day": dt.date(2024, 1, 1),
        "end_day": dt.date(2025, 1, 7), "status": "complete", "row_count": 160, "updated_at": now()}
        for symbol in ("A", "SPY")], ("symbol",))
    monkeypatch.setattr("src.services.breakout_replay.expected_latest_market_data_date", lambda: dt.date(2025, 1, 8))
    calls = []
    def empty(symbols, **kwargs):
        calls.append(kwargs)
        return pd.DataFrame()
    monkeypatch.setattr("src.services.breakout_replay.yf.download", empty)
    for day in (dt.date(2025, 1, 8), dt.date(2025, 1, 2), dt.date(2025, 1, 8)):
        service._prepare_daily("scan", day, [])
    assert len(calls) == 1 and calls[0]["start"] == "2025-01-08"
    assert len(service.repo.daily_dates("A")) == 160
    assert all(row["status"] == "complete" and row["end_day"] == dt.date(2025, 1, 7)
        for row in service.repo.rows(select(service.repo.daily_imports)))
    assert all(row["status"] == "empty" for row in service.repo.rows(select(service.repo.daily_requests)))


def test_failed_daily_fallback_is_retried_only_after_its_cooldown(service, monkeypatch):
    monkeypatch.setattr("src.services.breakout_replay.expected_latest_market_data_date", lambda: dt.date(2025, 1, 8))
    calls = []
    def unavailable(symbols, **kwargs):
        calls.append(kwargs)
        raise ConnectionError("Offline")
    monkeypatch.setattr("src.services.breakout_replay.yf.download", unavailable)
    service._prepare_daily("scan", dt.date(2025, 1, 8), [])
    service._prepare_daily("scan", dt.date(2025, 1, 8), [])
    assert len(calls) == 1
    assert all(row["status"] == "failed" for row in service.repo.rows(select(service.repo.daily_requests)))
    with service.repo.engine.begin() as conn:
        conn.execute(update(service.repo.daily_requests).values(updated_at=now()-dt.timedelta(hours=7)))
    service._prepare_daily("scan", dt.date(2025, 1, 8), [])
    assert len(calls) == 2
    assert all(call["start"] == "2025-01-08" for call in calls)
    assert len(service.repo.daily_dates("A")) == 160


def test_saved_short_ipo_history_does_not_expire_or_trigger_warmup_refetch(service, monkeypatch):
    with service.repo.engine.begin() as conn:
        conn.execute(delete(service.repo.daily).where(service.repo.daily.c.symbol == "A",
            service.repo.daily.c.date < dt.datetime(2025, 1, 2)))
    service.repo.upsert(service.repo.daily_imports, [{"symbol": "A", "start_day": dt.date(2024, 1, 1),
        "end_day": dt.date(2025, 1, 7), "status": "complete", "row_count": 4,
        "updated_at": now()-dt.timedelta(days=700)}], ("symbol",))
    monkeypatch.setattr("src.services.breakout_replay.expected_latest_market_data_date", lambda: dt.date(2026, 10, 8))
    monkeypatch.setattr("src.services.breakout_replay.yf.download", lambda *args, **kwargs: pytest.fail("Saved IPO data has no TTL"))
    service._prepare_daily("scan", dt.date(2025, 1, 2), [])
    assert len(service.repo.daily_dates("A")) == 4


def test_missing_session_inside_existing_daily_history_fetches_only_that_day(service, monkeypatch):
    with service.repo.engine.begin() as conn:
        conn.execute(delete(service.repo.daily).where(service.repo.daily.c.symbol == "A",
            service.repo.daily.c.date == dt.datetime(2025, 1, 3)))
    monkeypatch.setattr("src.services.breakout_replay.expected_latest_market_data_date", lambda: dt.date(2025, 1, 7))
    calls = []
    def download(symbols, **kwargs):
        calls.append((symbols, kwargs))
        return history()
    monkeypatch.setattr("src.services.breakout_replay.yf.download", download)
    service._prepare_daily("scan", dt.date(2025, 1, 3), [])
    assert len(calls) == 1 and calls[0][0] == ["A"]
    assert calls[0][1]["start"] == "2025-01-03" and calls[0][1]["end"] == "2025-01-04"
    service._prepare_daily("scan", dt.date(2025, 1, 3), [])
    assert len(calls) == 1


@pytest.mark.parametrize("source", ["auto", "yfinance"])
def test_saved_yahoo_intraday_is_available_offline_after_restart(service, source):
    day = dt.date(2025, 1, 3)
    service.repo.archive_intraday("A", intraday_frame(), "yfinance")
    sid = service.open_session("scan", "A")["session"]["id"]
    service.step(sid, 1)
    class OfflineProvider:
        def resolve(self, _source):
            pytest.fail("Cached bars must be read before resolving credentials or providers")
        def fetch(self, *args):
            pytest.fail("Cached bars must not be fetched again")
    restarted = ReplayService(ReplayRepository(service.repo.engine), service.snapshot_dir, OfflineProvider())
    try:
        first = restarted.intraday(sid, day, source, visible_bars=1)
        full = restarted.intraday(sid, day, source, visible_bars=4)
        assert first["cached"] and first["source"] == "yfinance" and len(first["bars"]) == 1
        assert full["cached"] and len(full["bars"]) == 4
    finally:
        restarted.pool.shutdown(wait=True)


def test_auto_intraday_chooses_one_complete_cached_source_without_mixing_bars(service, monkeypatch):
    day = dt.date(2025, 1, 3)
    service.repo.archive_intraday("A", intraday_frame(), "yfinance")
    service.repo.archive_intraday("A", intraday_frame().iloc[:1], "alpaca_sip")
    sid = service.open_session("scan", "A")["session"]["id"]
    service.step(sid, 1)
    monkeypatch.setattr(service.provider, "resolve", lambda _: pytest.fail("Do not call a provider for cached data"))
    auto = service.intraday(sid, day, visible_bars=4)
    assert auto["source"] == "yfinance" and len(auto["bars"]) == 4
    explicit = service.intraday(sid, day, "alpaca_sip")
    assert explicit["cached"] and explicit["source"] == "alpaca_sip" and len(explicit["bars"]) == 1


def test_historical_data_check_fetches_once_then_reuses_its_archive(service):
    class Provider:
        calls = 0
        def resolve(self, source): return "alpaca_sip"
        def fetch(self, symbol, day, source):
            self.calls += 1
            frame = intraday_frame()
            frame.index -= pd.Timedelta(days=1)
            return frame, source
    provider = Provider()
    service.provider = provider
    with TestClient(create_replay_app(service)) as client:
        first = client.post("/api/provider/check", json={})
        second = client.post("/api/provider/check", json={})
    assert first.status_code == second.status_code == 200
    assert not first.json()["cached"] and second.json()["cached"]
    assert second.json()["bars"] == 4 and "local archive" in second.json()["message"]
    assert provider.calls == 1


def test_status_keeps_cached_metadata_during_temporary_database_timeout(service, monkeypatch):
    first = service.status()
    service._bounds_cached_at = 0
    def busy():
        raise SQLAlchemyError("temporary import contention")
    monkeypatch.setattr(service.repo, "date_bounds", busy)
    monkeypatch.setattr(service.repo, "bounds", lambda: pytest.fail("Do not rescan the whole universe for status"))
    assert service.status()["bounds"] == first["bounds"]
