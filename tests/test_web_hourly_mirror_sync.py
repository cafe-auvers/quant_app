import datetime as dt

import pytest
from sqlalchemy import create_engine, insert, select, text

from scripts.sync_web_hourly_mirror import ordered_symbols, sync_groups
from src.infrastructure.database.schema import _ensure_hourly_price_history_table


def test_all_source_symbols_survive_priority_selection():
    assert ordered_symbols(["B", "A", "OUTSIDE_SCANNER", "A"], ["A", "A", "MISSING"]) == ["A", "B", "OUTSIDE_SCANNER"]


def test_sync_covers_unselected_symbols_and_resumes_without_source_writes(tmp_path):
    pc = create_engine("sqlite:///"+str(tmp_path / "pc.db"), future=True)
    local = create_engine("sqlite:///"+str(tmp_path / "mirror.db"), future=True)
    source_table = _ensure_hourly_price_history_table(pc)
    mirror_table = _ensure_hourly_price_history_table(local)
    rows = [{"symbol": symbol, "timestamp": dt.datetime(2026, 10, 5, 19, 30), "source": "yfinance",
             "open": 10, "high": 11, "low": 9, "close": 10, "adj_close": 10, "volume": 100,
             "updated_at": dt.datetime(2026, 10, 5, 23)} for symbol in ("SELECTED", "UNSELECTED")]
    with pc.begin() as conn:
        conn.execute(insert(source_table), rows)
        conn.execute(text("CREATE TABLE trading_control (live_enabled INTEGER)"))
        conn.execute(text("INSERT INTO trading_control VALUES (1)"))
    with local.begin() as conn:
        conn.execute(insert(mirror_table), rows[:1])
        conn.execute(text("CREATE TABLE daily_sentinel (close REAL)"))
        conn.execute(text("INSERT INTO daily_sentinel VALUES (42)"))
    changes = []
    assert sync_groups(pc, local, ["SELECTED", "UNSELECTED"], group_size=1, pause_seconds=0,
                       progress=lambda done, total, copied, group: changes.append((done, total, group))) == 1
    assert changes == [(1, 2, ["SELECTED"]), (2, 2, ["UNSELECTED"])]
    assert sync_groups(pc, local, ["SELECTED", "UNSELECTED"], group_size=1, pause_seconds=0) == 0
    with local.connect() as conn:
        assert set(conn.execute(select(mirror_table.c.symbol)).scalars()) == {"SELECTED", "UNSELECTED"}
        assert conn.execute(text("SELECT close FROM daily_sentinel")).scalar() == 42
    with pc.connect() as conn:
        assert conn.execute(text("SELECT live_enabled FROM trading_control")).scalar() == 1
        assert len(conn.execute(select(source_table)).all()) == 2
    pc.dispose()
    local.dispose()


def test_cancelled_sync_does_not_start_a_group():
    with pytest.raises(InterruptedError):
        sync_groups(None, None, ["A"], cancelled=lambda: True, pause_seconds=0)


def test_invalid_group_size_rejected_before_database_use():
    with pytest.raises(ValueError):
        sync_groups(None, None, ["A"], group_size=0)
