"""SQL persistence for Market Pulse configuration and metric snapshots."""

from __future__ import annotations

import datetime as dt
from typing import Iterable, Mapping, Optional

from sqlalchemy import MetaData, func, select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine

from src.core.market_pulse import (
    COMPONENT_RANK_METHOD,
    MarketPulseInstrument,
    MarketPulseRow,
    MarketPulseSnapshot,
    rank_market_pulse_rows,
)
from src.infrastructure.database.schema import (
    _ensure_market_pulse_tables,
    _get_market_pulse_instruments_table,
    _get_market_pulse_snapshots_table,
)
from src.infrastructure.database.time_utils import _utcnow_naive


class MarketPulseSnapshotRepository:
    """Batch-oriented, idempotent repository for the optional market DB."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def _tables(self):
        metadata = MetaData()
        instruments = _get_market_pulse_instruments_table(metadata)
        snapshots = _get_market_pulse_snapshots_table(metadata)
        _ensure_market_pulse_tables(self.engine)
        return instruments, snapshots

    @staticmethod
    def _upsert(conn, table, records, keys, update_columns, dialect_name) -> None:
        if not records:
            return
        if dialect_name == "mysql":
            stmt = mysql_insert(table).values(records)
            conn.execute(
                stmt.on_duplicate_key_update(
                    **{name: stmt.inserted[name] for name in update_columns}
                )
            )
            return
        if dialect_name == "sqlite":
            stmt = sqlite_insert(table).values(records)
            conn.execute(
                stmt.on_conflict_do_update(
                    index_elements=list(keys),
                    set_={name: getattr(stmt.excluded, name) for name in update_columns},
                )
            )
            return
        conn.execute(table.insert(), records)

    def seed_instruments(
        self,
        values: Iterable[MarketPulseInstrument],
    ) -> Mapping[tuple[str, str], int]:
        instruments, _snapshots = self._tables()
        now = _utcnow_naive()
        records = [
            {
                "section": item.section,
                "display_name": item.display_name,
                "ticker": item.ticker,
                "display_order": item.display_order,
                "is_active": item.is_active,
                "created_at": now,
                "updated_at": now,
            }
            for item in values
        ]
        with self.engine.begin() as conn:
            self._upsert(
                conn,
                instruments,
                records,
                ("section", "ticker"),
                ("display_name", "display_order", "is_active", "updated_at"),
                self.engine.dialect.name,
            )
            rows = conn.execute(
                select(
                    instruments.c.id,
                    instruments.c.section,
                    instruments.c.ticker,
                )
            ).all()
        return {
            (str(row.section), str(row.ticker).upper()): int(row.id)
            for row in rows
        }

    def upsert_snapshot(
        self,
        snapshot: MarketPulseSnapshot,
        configured_instruments: Iterable[MarketPulseInstrument],
    ) -> int:
        instruments, snapshots = self._tables()
        del instruments
        instrument_ids = self.seed_instruments(configured_instruments)
        refreshed_at = snapshot.refreshed_at.astimezone(dt.timezone.utc).replace(
            tzinfo=None
        )
        records = []
        for row in snapshot.rows:
            instrument_id = instrument_ids.get((row.section, row.ticker))
            if instrument_id is None:
                continue
            records.append(
                {
                    "instrument_id": instrument_id,
                    "as_of_date": snapshot.as_of_date,
                    "close": row.close,
                    "intraday_return": row.intraday_return,
                    "daily_return": row.daily_return,
                    "weekly_return": row.weekly_return,
                    "monthly_return": row.monthly_return,
                    "pct_above_52w_low": row.pct_above_52w_low,
                    "pct_below_52w_high": row.pct_below_52w_high,
                    "stock1": row.stock1,
                    "stock2": row.stock2,
                    "stock3": row.stock3,
                    "stock4": row.stock4,
                    "component_rank_method": snapshot.component_rank_method,
                    "status": row.status,
                    "error_message": row.error or None,
                    "source_session_date": row.source_session_date,
                    "source": snapshot.source,
                    "refreshed_at": refreshed_at,
                }
            )
        with self.engine.begin() as conn:
            self._upsert(
                conn,
                snapshots,
                records,
                ("instrument_id", "as_of_date"),
                (
                    "close",
                    "intraday_return",
                    "daily_return",
                    "weekly_return",
                    "monthly_return",
                    "pct_above_52w_low",
                    "pct_below_52w_high",
                    "stock1",
                    "stock2",
                    "stock3",
                    "stock4",
                    "component_rank_method",
                    "status",
                    "error_message",
                    "source_session_date",
                    "source",
                    "refreshed_at",
                ),
                self.engine.dialect.name,
            )
        return len(records)

    def load_latest_snapshot(self) -> Optional[MarketPulseSnapshot]:
        instruments, snapshots = self._tables()
        with self.engine.connect() as conn:
            as_of_date = conn.execute(select(func.max(snapshots.c.as_of_date))).scalar()
            if as_of_date is None:
                return None
            records = conn.execute(
                select(
                    instruments.c.section,
                    instruments.c.display_name,
                    instruments.c.ticker,
                    instruments.c.display_order,
                    snapshots.c.close,
                    snapshots.c.intraday_return,
                    snapshots.c.daily_return,
                    snapshots.c.weekly_return,
                    snapshots.c.monthly_return,
                    snapshots.c.pct_above_52w_low,
                    snapshots.c.pct_below_52w_high,
                    snapshots.c.stock1,
                    snapshots.c.stock2,
                    snapshots.c.stock3,
                    snapshots.c.stock4,
                    snapshots.c.component_rank_method,
                    snapshots.c.status,
                    snapshots.c.error_message,
                    snapshots.c.source_session_date,
                    snapshots.c.source,
                    snapshots.c.refreshed_at,
                )
                .join(snapshots, snapshots.c.instrument_id == instruments.c.id)
                .where(snapshots.c.as_of_date == as_of_date)
                .order_by(
                    instruments.c.section,
                    instruments.c.display_order,
                    instruments.c.ticker,
                )
            ).all()
        if not records:
            return None
        rows = tuple(
            MarketPulseRow(
                section=str(item.section),
                display_name=str(item.display_name),
                ticker=str(item.ticker).upper(),
                display_order=int(item.display_order),
                rank=0,
                close=item.close,
                intraday_return=item.intraday_return,
                daily_return=item.daily_return,
                weekly_return=item.weekly_return,
                monthly_return=item.monthly_return,
                pct_above_52w_low=item.pct_above_52w_low,
                pct_below_52w_high=item.pct_below_52w_high,
                stock1=(
                    item.stock1
                    if item.component_rank_method == COMPONENT_RANK_METHOD
                    else None
                ),
                stock2=(
                    item.stock2
                    if item.component_rank_method == COMPONENT_RANK_METHOD
                    else None
                ),
                stock3=(
                    item.stock3
                    if item.component_rank_method == COMPONENT_RANK_METHOD
                    else None
                ),
                stock4=(
                    item.stock4
                    if item.component_rank_method == COMPONENT_RANK_METHOD
                    else None
                ),
                status=str(item.status),
                error=str(item.error_message or ""),
                source_session_date=item.source_session_date,
            )
            for item in records
        )
        failures = {
            row.ticker: row.error
            for row in rows
            if row.status != "available" and row.error
        }
        refreshed_at = max(item.refreshed_at for item in records).replace(
            tzinfo=dt.timezone.utc
        )
        return MarketPulseSnapshot(
            as_of_date=as_of_date,
            refreshed_at=refreshed_at,
            source=str(records[0].source),
            rows=rank_market_pulse_rows(rows),
            failures=failures,
            component_rank_method=str(
                records[0].component_rank_method or ""
            ),
        )
