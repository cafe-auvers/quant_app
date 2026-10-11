"""Optional opening-history proofs stored atomically beside the PC bar cache."""
import json

from sqlalchemy import Column, DateTime, MetaData, String, Table, Text, select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import SQLAlchemyError

from ..time_utils import _utcnow_naive


def coverage_table() -> Table:
    return Table(
        "intraday_history_coverage",
        MetaData(),
        Column("symbol", String(20), primary_key=True),
        Column("interval", String(10), primary_key=True),
        Column("source", String(20), primary_key=True),
        Column("payload", Text, nullable=False),
        Column("updated_at", DateTime, nullable=False),
    )


def save_intraday_coverage(conn, table, *, symbol, interval, source, coverage) -> None:
    values = {
        "symbol": symbol.upper(),
        "interval": interval,
        "source": source,
        "payload": json.dumps(coverage, separators=(",", ":")),
        "updated_at": _utcnow_naive(),
    }
    insert = mysql_insert(table) if conn.dialect.name == "mysql" else sqlite_insert(table)
    stmt = insert.values(**values)
    if conn.dialect.name == "mysql":
        stmt = stmt.on_duplicate_key_update(
            payload=stmt.inserted.payload, updated_at=stmt.inserted.updated_at
        )
    else:
        stmt = stmt.on_conflict_do_update(
            index_elements=["symbol", "interval", "source"],
            set_={"payload": stmt.excluded.payload, "updated_at": stmt.excluded.updated_at},
        )
    conn.execute(stmt)


def load_intraday_coverage(conn, *, symbol, interval, source) -> dict:
    table = coverage_table()
    try:
        raw = conn.execute(
            select(table.c.payload).where(
                table.c.symbol == symbol.upper(),
                table.c.interval == interval,
                table.c.source == source,
            )
        ).scalar_one_or_none()
        value = json.loads(raw) if raw else {}
        return value if isinstance(value, dict) else {}
    except (SQLAlchemyError, ValueError, TypeError):
        # Legacy caches remain usable under the original exact-opening-bar gate.
        return {}
