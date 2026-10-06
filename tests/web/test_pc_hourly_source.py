import asyncio
import datetime as dt
import gzip
import json
import sqlite3

import pytest
from sqlalchemy import create_engine, event, insert, update

from src.infrastructure.database.schema import _ensure_hourly_price_history_table
from src.web.cache import ChartBundleCache, ChartLoadCoordinator
from src.web.config import WebConfigError, _from_mapping
from src.web.hourly_source import ReadOnlyPCHourlyHistory
from src.web.market_data import MarketDataUnavailable, ReadOnlyMirrorMarketDataSource


def sources(tmp_path):
    mirror = tmp_path / "mirror.db"
    with sqlite3.connect(mirror) as conn:
        conn.execute("CREATE TABLE price_history (symbol TEXT,interval TEXT,date TEXT,open REAL,high REAL,low REAL,close REAL,adj_close REAL,volume REAL)")
        conn.execute("INSERT INTO price_history VALUES ('UNSELECTED','1d','2026-10-05',10,12,9,11,11,1000)")
    pc_path = tmp_path / "pc.db"
    engine = create_engine("sqlite:///"+str(pc_path), future=True)
    table = _ensure_hourly_price_history_table(engine)
    stamps = [dt.datetime.now(dt.timezone.utc).replace(tzinfo=None, minute=30, second=0, microsecond=0)-dt.timedelta(hours=offset) for offset in (3, 2, 1)]
    rows = [{"symbol": symbol, "timestamp": stamp, "source": "fixture", "open": 10,
             "high": 12, "low": 9, "close": 11, "adj_close": 11, "volume": 1000,
             "updated_at": stamp} for symbol in ("UNSELECTED", "SPY") for stamp in stamps]
    with engine.begin() as conn:
        conn.execute(insert(table), rows)
    reader = ReadOnlyPCHourlyHistory(engine)
    return mirror, pc_path, engine, table, reader


def test_hourly_chart_loads_symbol_absent_from_mirror_without_any_writes(tmp_path):
    mirror, pc_path, engine, table, reader = sources(tmp_path)
    before = (mirror.read_bytes(), pc_path.read_bytes())
    statements = []
    event.listen(engine, "before_cursor_execute", lambda conn, cursor, statement, parameters, context, many: statements.append(statement))
    source = ReadOnlyMirrorMarketDataSource(mirror, hourly_reader=reader)
    hourly = source.chart_bundle("UNSELECTED", "1H", daily_bars=50, hourly_months=6)
    daily = source.chart_bundle("UNSELECTED", "1D", daily_bars=50, hourly_months=6)
    assert len(hourly["bars"]) == 3 and len(daily["bars"]) == 1
    assert hourly["coverage"]["bar_source"] == "PC_MYSQL_READ_ONLY"
    assert all(statement.lstrip().upper().startswith("SELECT") for statement in statements)
    assert before == (mirror.read_bytes(), pc_path.read_bytes())
    source.close()


def test_pc_hourly_revision_invalidates_existing_chart_cache(tmp_path):
    mirror, pc_path, engine, table, reader = sources(tmp_path)
    source = ReadOnlyMirrorMarketDataSource(mirror, hourly_reader=reader)
    async def run():
        coordinator = ChartLoadCoordinator(source, ChartBundleCache(tmp_path / "charts"), daily_bars=50, hourly_months=6)
        first, hit = await coordinator.get_compressed("UNSELECTED", "1H")
        assert not hit
        repeated, hit = await coordinator.get_compressed("UNSELECTED", "1H")
        assert hit and first.content == repeated.content
        with engine.begin() as conn:
            conn.execute(update(table).where(table.c.symbol == "UNSELECTED").values(close=10.5, adj_close=10.5, updated_at=dt.datetime.now()))
        reader._revisions.clear()
        changed, hit = await coordinator.get_compressed("UNSELECTED", "1H")
        assert not hit
        assert json.loads(gzip.decompress(changed.content))["bars"][-1]["close"] == 10.5
    asyncio.run(run())
    source.close()


def test_missing_pc_hourly_history_is_not_fabricated(tmp_path):
    mirror, pc_path, engine, table, reader = sources(tmp_path)
    source = ReadOnlyMirrorMarketDataSource(mirror, hourly_reader=reader)
    with pytest.raises(MarketDataUnavailable, match="history is unavailable"):
        source.chart_bundle("MISSING", "1H", daily_bars=50, hourly_months=6)
    source.close()


def test_unavailable_pc_history_is_reported_without_sql_or_secrets():
    class MissingEngine:
        def connect(self):
            raise RuntimeError("credential-containing database failure")
    reader = ReadOnlyPCHourlyHistory(MissingEngine())
    with pytest.raises(MarketDataUnavailable, match="source unavailable") as error:
        reader.read("SYMBOL", dt.datetime.now())
    assert "credential" not in str(error.value)


@pytest.mark.parametrize("values", [{"pc_hourly_reads": True}, {"mode": "CONNECTED", "pc_hourly_reads": True}])
def test_pc_hourly_reads_require_explicit_pc_scope(values):
    with pytest.raises(WebConfigError, match="PC hourly reads require"):
        _from_mapping(values)
