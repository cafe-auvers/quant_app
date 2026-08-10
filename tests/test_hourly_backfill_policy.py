import datetime as dt
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
from sqlalchemy import create_engine

import src.utils.db_loader as db_loader


def _load_once_script():
    script_path = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "backfill_hourly_history_200d_once.py"
    )
    spec = importlib.util.spec_from_file_location(
        "backfill_hourly_history_200d_once_for_tests", script_path
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_hourly_period_policy_keeps_routine_d10_separate_from_d200_backfill():
    assert db_loader._period_for_hourly_refresh() == "10d"
    assert db_loader._period_for_hourly_refresh(backfill=True) == "200d"


def test_hourly_refresh_uses_d10_normally_and_d200_only_when_forced(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    calls = []
    monkeypatch.setattr(
        db_loader,
        "download_price_history",
        lambda *args, **kwargs: calls.append(kwargs["period"]) or pd.DataFrame(),
    )
    try:
        db_loader.refresh_universe_hourly_history_to_db(
            ["AAPL"], engine, chunk_size=1, batch_sleep=0
        )
        db_loader.refresh_universe_hourly_history_to_db(
            ["AAPL"], engine, chunk_size=1, batch_sleep=0, backfill=True
        )
    finally:
        engine.dispose()

    assert calls == ["10d", "200d"]


def test_laptop_hourly_staleness_checks_each_actionable_symbol(monkeypatch):
    expected = dt.date(2026, 8, 7)
    latest = {
        "SPY": dt.datetime(2026, 8, 7, 20),
        "AAPL": dt.datetime(2026, 8, 6, 20),
    }
    monkeypatch.setattr(
        db_loader,
        "get_latest_hourly_price_history_timestamps",
        lambda *args, **kwargs: latest,
    )
    monkeypatch.setattr(
        db_loader, "get_chronically_failing_symbols", lambda *args, **kwargs: set()
    )

    assert db_loader.local_mirror_hourly_is_stale(object(), expected, ["AAPL"])

    latest["AAPL"] = dt.datetime(2026, 8, 7, 20)
    assert not db_loader.local_mirror_hourly_is_stale(object(), expected, ["AAPL"])


def test_once_script_rejects_a_laptop_connected_to_pc_mysql(monkeypatch):
    script = _load_once_script()

    class Engine:
        def dispose(self):
            self.disposed = True

    engine = Engine()
    monkeypatch.setattr(
        script,
        "resolve_data_engine",
        lambda: SimpleNamespace(engine=engine, source="pc"),
    )
    monkeypatch.setattr(script.platform, "node", lambda: "DEV-LAPTOP")
    monkeypatch.setattr(
        script, "database_server_hostname", lambda _engine: "DATA-PC"
    )

    assert script.main([]) == 2
    assert engine.disposed is True


def test_once_script_runs_forced_backfill_and_writes_marker(monkeypatch, tmp_path):
    script = _load_once_script()
    marker = tmp_path / "completed.json"
    calls = []
    monkeypatch.setattr(script, "COMPLETION_MARKER", marker)
    monkeypatch.setattr(
        script, "verify_running_on_database_pc", lambda: ("data-pc", "data-pc")
    )
    monkeypatch.setattr(
        script.subprocess,
        "run",
        lambda command, **kwargs: calls.append((command, kwargs))
        or SimpleNamespace(returncode=0),
    )

    assert script.main([]) == 0
    assert calls[0][0][-3:] == ["--mode", "1h", "--backfill"]
    assert json.loads(marker.read_text(encoding="utf-8"))["period"] == "200d"

    calls.clear()
    assert script.main([]) == 0
    assert calls == []
