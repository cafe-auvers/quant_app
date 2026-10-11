import datetime as dt
import json

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.pool import StaticPool

from src.core.breakout_replay import daily_chart, metrics_as_of, rank_and_scan
from src.core.replay_precompute import daily_study_artifact, rank_daily_universe
from src.infrastructure.database.repositories.breakout_replay import ReplayRepository, now
from src.infrastructure.database.repositories.study_history import missing_study_ranges
from src.infrastructure.database.schema import _ensure_price_history_table
from src.services.breakout_replay import ReplayService
from src.services.replay_preparation import ReplayPreparation
from src.ui.filter_catalog import DEFAULT_SCANNER_SETUPS


def bars(symbol, end="2025-01-07", count=170):
    dates = pd.bdate_range(end=end, periods=count)
    step = {"A": .8, "B": .03, "SPY": .01}[symbol]
    close = 50 + np.arange(count) * step
    return pd.DataFrame({"Open": close, "High": close * 1.04, "Low": close * .96,
        "Close": close, "Volume": 1_000_000}, index=dates)


@pytest.fixture
def repo():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    _ensure_price_history_table(engine)
    repository = ReplayRepository(engine)
    for symbol in ("A", "B", "SPY"):
        frame = bars(symbol)
        repository.upsert(repository.daily, [{"symbol": symbol, "interval": "1d", "date": stamp.to_pydatetime(),
            "updated_at": now(), **{name.lower(): float(row[name]) for name in ("Open", "High", "Low", "Close", "Volume")}}
            for stamp, row in frame.iterrows()], ("symbol", "date", "interval"))
    yield repository
    engine.dispose()


def test_vectorized_studies_match_causal_scanner_and_chart_with_gaps_and_ties():
    spy = bars("SPY")
    histories = {"A": bars("A"), "B": bars("B").drop(bars("B").index[-3]), "C": bars("A")}
    artifacts, frames = {}, {}
    for symbol, history in histories.items():
        artifacts[symbol], frames[symbol] = daily_study_artifact(symbol, history, spy)
    days = [dt.date(2025, 1, 2), dt.date(2025, 1, 3), dt.date(2025, 1, 7)]
    rules = [dict(rule) for rule in DEFAULT_SCANNER_SETUPS["Setup 2"]["rules"]]
    rules[-1]["threshold"] = 50  # Exercise ties instead of excluding both tied leaders.
    ranked = rank_daily_universe(frames, days, rules)
    for day in days:
        rows = [metrics_as_of(s, h, spy, day) for s, h in histories.items()]
        expected = rank_and_scan([row for row in rows if row], rules)
        assert [row["symbol"] for row in ranked[day]["results"]] == [row["symbol"] for row in expected]
        for actual, reference in zip(ranked[day]["results"], expected):
            for field in ("return_1m", "return_3m", "return_6m", "adr_20_high_low", "adr_20",
                          "avg_dollar_volume_20d", "momentum_rank_any", "momentum_rank_1m"):
                assert actual[field] == pytest.approx(reference[field])
        cutoff = str(day)
        prepared = [row for row in artifacts["A"]["chart"]["bars"] if row["time"] <= cutoff]
        for actual, reference in zip(prepared, daily_chart(histories["A"], spy, day)["bars"]):
            for field in ("close", "ma10", "ma20", "ma50", "rs", "ti65"):
                assert actual[field] == pytest.approx(reference[field])


def test_prepared_scans_and_cold_daily_charts_need_no_download_or_calculation(repo, monkeypatch):
    worker = ReplayPreparation(repo, lambda *_: None)
    end = dt.date(2025, 1, 7)
    published = worker.run(start=dt.date(2025, 1, 2), end=end)
    monkeypatch.setattr(worker, "signatures", lambda: published["signatures"])
    monkeypatch.setattr(repo, "histories", lambda *a: pytest.fail("Read prepared daily charts"))
    assert worker.run(start=dt.date(2025, 1, 2), end=end) == published
    service = ReplayService(repo)
    monkeypatch.setattr(service, "_prepare_daily", lambda *a: pytest.fail("Do not download on date selection"))
    monkeypatch.setattr(service.pool, "submit", lambda *a: pytest.fail("No scan calculation on selection"))
    monkeypatch.setattr("src.services.replay_daily.metrics_as_of", lambda *a: pytest.fail("Use prepared metrics"))
    original_rows = repo.rows
    def prepared_rows(statement):
        assert "price_history" not in str(statement), "Use saved calendars for review coverage"
        return original_rows(statement)
    monkeypatch.setattr(repo, "rows", prepared_rows)
    try:
        result = service.start_scan(dt.date(2025, 1, 2))
        assert result["prepared"]
        assert service.start_scan(dt.date(2025, 1, 2)) == result
        rows = service.scan_results(result["id"])
        assert rows["scan"]["setup_name"] == "Setup 2"
        assert rows["results"][0]["symbol"] == "A"
        replay = service.open_session(result["id"], "A")
        assert all(row["time"] <= "2025-01-02" for row in replay["bars"])
        assert replay["metrics"]["return_1m"] == pytest.approx(metrics_as_of("A", bars("A"), bars("SPY"), dt.date(2025, 1, 2))["return_1m"])
        assert service.set_mode(replay["session"]["id"], "historical")["bars"][-1]["time"] == "2025-01-07"
    finally:
        service.pool.shutdown(wait=True)


def test_saved_calendars_support_compressed_mysql_transfer(repo, monkeypatch):
    import zlib
    from types import SimpleNamespace
    from src.infrastructure.database.repositories.replay_prepared import PreparedReplayRepository

    ReplayPreparation(repo, lambda *_: None).run(start=dt.date(2025, 1, 2), end=dt.date(2025, 1, 7))
    expected = repo.prepared.calendars(["A", "SPY"])
    assert expected["A"][-1] == "2025-01-07"
    assert repo.prepared.calendars([]) == {}
    mysql = PreparedReplayRepository.__new__(PreparedReplayRepository)
    mysql.engine = SimpleNamespace(dialect=SimpleNamespace(name="mysql"))
    mysql.archives = repo.prepared.archives
    def rows(statement):
        assert "compress(" in str(statement).lower()
        result = []
        for symbol, days in expected.items():
            payload = json.dumps(days).encode()
            result.append({"symbol": symbol, "days": len(payload).to_bytes(4, "little") + zlib.compress(payload)})
        return result
    monkeypatch.setattr(mysql, "rows", rows)
    assert mysql.calendars(["A", "SPY"]) == expected


def test_new_suffix_only_rebuilds_new_scan_dates(repo, monkeypatch):
    prepare = ReplayPreparation(repo, lambda *_: None)
    prepare.run(start=dt.date(2025, 1, 2), end=dt.date(2025, 1, 7))
    old = repo.prepared.rows(select(repo.prepared.scans).where(repo.prepared.scans.c.day == dt.date(2025, 1, 2)))[0]
    for symbol in ("A", "B", "SPY"):
        repo.upsert(repo.daily, [{"symbol": symbol, "interval": "1d", "date": dt.datetime(2025, 1, 8),
            "open": 200, "high": 210, "low": 195, "close": 205, "volume": 1_000_000, "updated_at": now()}], ("symbol", "date", "interval"))
    updated = prepare.run(start=dt.date(2025, 1, 2), end=dt.date(2025, 1, 8))
    assert updated["end_day"] == "2025-01-08"
    assert repo.prepared.rows(select(repo.prepared.scans).where(repo.prepared.scans.c.day == dt.date(2025, 1, 2)))[0] == old
    assert repo.prepared.archive("A")["chart"]["bars"][-1]["time"] == "2025-01-08"


def test_added_warmup_and_revised_daily_bars_rebuild_historical_scans(repo):
    prepare = ReplayPreparation(repo, lambda *_: None)
    start, end = dt.date(2025, 1, 2), dt.date(2025, 1, 7)
    initial = prepare.run(start=start, end=end)
    table = repo.prepared.scans
    before = repo.prepared.rows(select(table).where(table.c.day == start))[0]
    earliest = bars("A").index[0].to_pydatetime() - dt.timedelta(days=1)
    repo.upsert(repo.daily, [{"symbol": "A", "interval": "1d", "date": earliest,
        "open": 40, "high": 43, "low": 39, "close": 42, "volume": 1_000_000,
        "updated_at": now()}], ("symbol", "date", "interval"))
    extended = prepare.run(start=start, end=end)
    after = repo.prepared.rows(select(table).where(table.c.day == start))[0]
    assert extended["source_key"] != initial["source_key"]
    assert after["updated_at"] > before["updated_at"]
    revised_at = now() + dt.timedelta(seconds=1)
    repo.upsert(repo.daily, [{"symbol": "A", "interval": "1d", "date": dt.datetime(2025, 1, 7),
        "open": 100, "high": 110, "low": 90, "close": 105, "volume": 1_000_000,
        "updated_at": revised_at}], ("symbol", "date", "interval"))
    corrected = prepare.run(start=start, end=end)
    assert corrected["source_key"] != extended["source_key"]
    assert repo.prepared.archive("A")["chart"]["bars"][-1]["close"] == 105


def test_prepared_scans_are_invalidated_when_rules_change(repo, monkeypatch):
    start, end = dt.date(2025, 1, 2), dt.date(2025, 1, 7)
    prepare = ReplayPreparation(repo, lambda *_: None)
    initial = prepare.run(start=start, end=end)
    changed = [{"attribute": "adr_20_high_low", "operator": ">=", "threshold": 100}]
    monkeypatch.setattr("src.services.replay_preparation.scanner_rules", lambda *_: changed)
    updated = prepare.run(start=start, end=end)
    assert updated["rules_key"] != initial["rules_key"]
    assert repo.prepared.scan(start, "Setup 2", initial["rules_key"]) is None
    assert repo.prepared.scan(start, "Setup 2", updated["rules_key"])["results"] == []


def test_force_refresh_reads_corrections_before_the_latest_daily_bar(repo, monkeypatch):
    prepare = ReplayPreparation(repo, lambda *_: None)
    start, end = dt.date(2025, 1, 2), dt.date(2025, 1, 7)
    initial = prepare.run(start=start, end=end)
    service = ReplayService(repo)
    before = service.start_scan(dt.date(2025, 1, 3))
    repo.upsert(repo.daily, [{"symbol": "A", "interval": "1d", "date": dt.datetime(2025, 1, 3),
        "open": 100, "high": 110, "low": 90, "close": 105, "volume": 1_000_000,
        "updated_at": now()}], ("symbol", "date", "interval"))
    published = prepare.run(start=start, end=end, force=True)
    try:
        assert published["source_key"] != initial["source_key"]
        assert service.start_scan(dt.date(2025, 1, 3))["id"] != before["id"]
    finally:
        service.pool.shutdown(wait=True)
    revised = next(row for row in repo.prepared.archive("A")["chart"]["bars"] if row["time"] == "2025-01-03")
    assert revised["close"] == 105
    monkeypatch.setattr(repo, "histories", lambda *_: pytest.fail("Reuse the forced revision on unchanged inputs"))
    assert prepare.run(start=start, end=end) == published


def test_ready_load_checks_do_not_record_replay_reviews_or_trades(repo, tmp_path, monkeypatch):
    import io
    from urllib.parse import urlsplit
    import src.services.replay_performance as module
    from fastapi.testclient import TestClient
    from src.web.replay import create_replay_app

    published = ReplayPreparation(repo, lambda *_: None).run(
        start=dt.date(2025, 1, 2), end=dt.date(2025, 1, 7))
    service = ReplayService(repo)
    monkeypatch.setattr(module, "DATA_DIR", tmp_path)
    (tmp_path / "breakout_replay").mkdir()
    with TestClient(create_replay_app(service)) as client:
        def urlopen(request, **options):
            if isinstance(request, str):
                response = client.get(urlsplit(request).path)
            else:
                response = client.post(urlsplit(request.full_url).path,
                    json=json.loads(request.data))
            response.raise_for_status()
            return io.BytesIO(response.content)
        monkeypatch.setattr(module.urllib.request, "urlopen", urlopen)
        result = module.check_prepared_loads(repo, published, lambda *_: None)
        assert len(result["checks"]) == 3
        assert result["checks"][0]["day"] == "2025-01-02"
        assert result["checks"][-1]["day"] == "2025-01-07"
        assert not repo.rows(select(repo.sessions))
        assert not repo.rows(select(repo.reviews))
        assert not repo.rows(select(repo.progress))
        assert not repo.rows(select(repo.paper_actions))
        monkeypatch.setattr(module.urllib.request, "urlopen", lambda *_a, **_k: pytest.fail("Reuse this revision's timing report"))
        assert module.check_prepared_loads(repo, published) == result
    assert (tmp_path / "breakout_replay" / "performance_ready.json").exists()


def test_missing_daily_planner_preserves_saved_ranges_and_backoff():
    start, end = dt.date(2023, 1, 1), dt.date(2025, 1, 7)
    asof = dt.datetime(2025, 1, 8)
    bounds = {"A": {"first": dt.datetime(2024, 1, 2), "last": dt.datetime(2025, 1, 6)},
              "B": {"first": dt.datetime(2024, 1, 2), "last": dt.datetime(2025, 1, 7)}}
    requests = [{"symbol": "B", "start_day": start, "end_day": dt.date(2024, 1, 1),
                 "status": "complete", "updated_at": asof - dt.timedelta(days=500)}]
    assert missing_study_ranges(["A", "B"], bounds, requests, start, end, asof) == [
        ("A", start, dt.date(2024, 1, 1)), ("A", end, end)]


def test_daily_extension_uses_existing_batch_downloader_and_saves_only_missing_dates(repo, monkeypatch):
    from src.infrastructure.database.refresh import refresh_universe_history_to_db
    import src.infrastructure.database.repositories.study_history as module

    monkeypatch.setattr(module, "expected_latest_market_data_date", lambda: dt.date(2025, 1, 7))
    calls = []
    def download(symbols, **options):
        calls.append(options)
        frames = {symbol: bars(symbol).loc["2024-01-01":] for symbol in symbols}
        # A provider may send extra dates; existing dates must not be rewritten.
        return pd.concat(frames, axis=1)
    monkeypatch.setattr(module, "_download_yfinance_batch", download)
    first = bars("A").index[0].date()
    refresh_universe_history_to_db(["A", "B", "SPY"], repo.engine, study_start_date=first - dt.timedelta(days=30), batch_sleep=0)
    assert calls and all(dt.date.fromisoformat(row["end"]) == first for row in calls)
    existing = repo.histories(["A"], dt.date(2025, 1, 7), dt.date(2025, 1, 7))["A"]
    assert existing.Close.iloc[0] == bars("A").Close.iloc[-1]
