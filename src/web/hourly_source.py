"""Bounded hourly chart reads from the PC cache, without schema or broker access."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import threading
import time

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import URL

from .canonical_planning import _read_env_mapping, _read_json_mapping
from .market_data import MarketDataUnavailable


class ReadOnlyPCHourlyHistory:
    def __init__(self, engine):
        self.engine = engine
        self._revisions = {}
        self._lock = threading.Lock()

    def close(self):
        self.engine.dispose()

    def revision(self, symbol, start):
        key = (symbol, str(start)[:13])
        now = time.monotonic()
        with self._lock:
            cached = self._revisions.get(key)
            if cached and now-cached[0] < 5:
                return cached[1]
        try:
            with self.engine.connect() as conn:
                values = []
                for target in dict.fromkeys((symbol, "SPY")):
                    row = conn.execute(text(
                        "SELECT COUNT(*) AS bars,MAX(updated_at) AS revision,MAX(timestamp) AS last_bar "
                        "FROM hourly_price_history WHERE symbol=:symbol AND timestamp>=:start"
                    ), {"symbol": target, "start": start}).mappings().one()
                    values.append((target, row["bars"], str(row["revision"]), str(row["last_bar"])))
            revision = hashlib.sha256(json.dumps(values).encode()).hexdigest()
        except Exception as exc:
            raise MarketDataUnavailable("PC hourly source unavailable; fresh hourly history cannot be verified") from exc
        with self._lock:
            if len(self._revisions) >= 2000:
                self._revisions.clear()
            self._revisions[key] = (now, revision)
        return revision

    def read(self, symbol, start):
        try:
            with self.engine.connect() as conn:
                rows = [dict(row) for row in conn.execute(text(
                    "SELECT timestamp,open,high,low,close,adj_close,volume,source "
                    "FROM hourly_price_history WHERE symbol=:symbol AND timestamp>=:start ORDER BY timestamp"
                ), {"symbol": symbol, "start": start}).mappings()]
                benchmark = [dict(row) for row in conn.execute(text(
                    "SELECT timestamp,close FROM hourly_price_history "
                    "WHERE symbol='SPY' AND timestamp>=:start ORDER BY timestamp"
                ), {"start": start}).mappings()]
            return rows, benchmark
        except Exception as exc:
            raise MarketDataUnavailable("PC hourly source unavailable; hourly chart cannot be loaded") from exc


def build_pc_hourly_reader(repository: Path):
    values = {}
    values.update(_read_json_mapping(repository / "config/runtime.json"))
    values.update(_read_json_mapping(repository / "config/runtime.local.json"))
    values.update(_read_env_mapping(repository / ".env"))
    required = ("MYSQL_HOST", "MYSQL_USER", "MYSQL_PASSWORD")
    if any(not str(values.get(key) or "").strip() for key in required):
        raise MarketDataUnavailable("PC hourly source credentials are incomplete")
    url = URL.create("mysql+pymysql", username=values["MYSQL_USER"], password=values["MYSQL_PASSWORD"],
                     host=values["MYSQL_HOST"], port=int(values.get("MYSQL_PORT") or 3306),
                     database=values.get("MYSQL_DB") or "quant_app", query={"charset": "utf8mb4"})
    engine = create_engine(url, future=True, pool_pre_ping=True, pool_size=2, max_overflow=2,
                           pool_recycle=300, connect_args={"connect_timeout": 2, "read_timeout": 3, "write_timeout": 3})
    @event.listens_for(engine, "connect")
    def enforce_read_only(connection, record):
        with connection.cursor() as cursor:
            cursor.execute("SET SESSION TRANSACTION READ ONLY")
            cursor.execute("SET SESSION MAX_EXECUTION_TIME=2000")
    return ReadOnlyPCHourlyHistory(engine)
