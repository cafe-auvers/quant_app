"""Local mirror construction, handoff guards, and engine resolution."""

import datetime as dt
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional, Tuple

from sqlalchemy import (Boolean, Column, DateTime, Integer, MetaData, String,
                        Table, create_engine, event, inspect, select, text)
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from src.utils.config import DATA_DIR

from .engine import init_mysql_engine
from .schema import (_ensure_chart_indicator_manifests_table,
                     _ensure_chart_indicators_table,
                     _ensure_hourly_price_history_table,
                     _ensure_price_history_table,
                     _ensure_scanner_metric_snapshots_table,
                     _ensure_scanner_metrics_table,
                     _ensure_symbol_refresh_failures_table)
from .settings import logger

# --- Local mirror (offline fallback when the PC's MySQL is unreachable) -----
#
# The PC's MySQL database remains the canonical market-data cache (see
# docs/pc_sync_data_pipeline.md).  The SQLite file is the laptop's offline
# mirror. Runtime mirroring is strictly PC -> laptop and MySQL is
# authoritative. The explicit reconciliation helper below remains available
# for maintenance/tests, but normal app startup and recovery never promote
# laptop market data or wait for mirror equality.

LOCAL_MIRROR_DB_PATH = DATA_DIR / "local_mirror.db"
LOCAL_MIRROR_ENABLED_ENV = "QUANT_LOCAL_MIRROR_ENABLED"

# (table_name, watermark_column) -- the column used both to decide what a
# resumed sync still needs to pull and to judge staleness. Deliberately
# excludes intraday_price_history (7-day-pruned, live-trading-only, low value
# offline) and app_state_sync / app_runtime_status (cross-machine
# coordination rows tied to the one shared PC database, not market data --
# mirroring them would be meaningless and could confuse main-device
# ownership).
MIRRORED_TABLES: Tuple[Tuple[str, str], ...] = (
    ("price_history", "date"),
    ("hourly_price_history", "timestamp"),
    ("chart_indicators", "date"),
    ("chart_indicator_manifests", "completed_at"),
    ("scanner_metrics", "date"),
    ("scanner_metric_snapshots", "snapshot_date"),
    ("symbol_refresh_failures", "last_attempt_at"),
)
HOURLY_MIRROR_TABLES: Tuple[Tuple[str, str], ...] = (
    ("hourly_price_history", "timestamp"),
)


@dataclass(frozen=True)
class LocalMirrorReconciliationResult:
    """Outcome of the guarded local-mirror -> PC transition reconciliation."""

    success: bool
    local_to_pc_rows: Dict[str, int]
    pc_to_local_rows: Dict[str, int]
    affected_daily_symbols: Tuple[str, ...] = ()
    affected_hourly_symbols: Tuple[str, ...] = ()
    errors: Tuple[str, ...] = ()
    local_handoff_ready: bool = False

    @property
    def total_local_to_pc_rows(self) -> int:
        return sum(self.local_to_pc_rows.values())

    @property
    def total_pc_to_local_rows(self) -> int:
        return sum(self.pc_to_local_rows.values())


class LocalMirrorNeedsReconciliationError(RuntimeError):
    """The active-PC mirror found local writes that require a guarded merge."""


@dataclass(frozen=True)
class _RawMirrorSpec:
    table_name: str
    watermark_column: str
    group_columns: Tuple[str, ...]
    required_value: Optional[Tuple[str, str]] = None


_RAW_MIRROR_SPECS: Tuple[_RawMirrorSpec, ...] = (
    _RawMirrorSpec(
        "price_history",
        "date",
        ("symbol", "interval"),
        required_value=("interval", "1d"),
    ),
    _RawMirrorSpec(
        "hourly_price_history",
        "timestamp",
        ("symbol", "source"),
    ),
)


@dataclass(frozen=True)
class _ReconcileTableSpec:
    table_name: str
    primary_key: Tuple[str, ...]
    partition_columns: Tuple[str, ...]
    watermark_column: str
    revision_column: str
    reverse_raw: bool = False


_RECONCILE_TABLE_SPECS: Tuple[_ReconcileTableSpec, ...] = (
    _ReconcileTableSpec(
        "price_history",
        ("symbol", "date", "interval"),
        ("symbol", "interval"),
        "date",
        "updated_at",
        reverse_raw=True,
    ),
    _ReconcileTableSpec(
        "hourly_price_history",
        ("symbol", "timestamp", "source"),
        ("symbol", "source"),
        "timestamp",
        "updated_at",
        reverse_raw=True,
    ),
    _ReconcileTableSpec(
        "chart_indicators",
        ("symbol", "date"),
        ("symbol",),
        "date",
        "updated_at",
    ),
    _ReconcileTableSpec(
        "chart_indicator_manifests",
        ("symbol",),
        ("symbol",),
        "completed_at",
        "completed_at",
    ),
    _ReconcileTableSpec(
        "scanner_metrics",
        ("symbol", "date"),
        ("date",),
        "date",
        "updated_at",
    ),
    _ReconcileTableSpec(
        "scanner_metric_snapshots",
        ("snapshot_date",),
        ("snapshot_date",),
        "snapshot_date",
        "completed_at",
    ),
    _ReconcileTableSpec(
        "symbol_refresh_failures",
        ("symbol", "interval"),
        ("interval",),
        "last_attempt_at",
        "last_attempt_at",
    ),
)


@dataclass(frozen=True)
class _PartitionFingerprint:
    raw_key: Tuple[object, ...]
    row_count: int
    digest: str


_BOOLEAN_RECONCILE_COLUMNS = frozenset(
    {
        "is_ti65_bullish",
        "is_ti65_bearish",
        "is_9m_volume",
        "is_plus_4pct_change",
        "is_minus_4pct_change",
        "is_rs_cross_up",
        "above_sma_20",
        "above_ema_50",
        "ma_alignment",
        "breakout_20d",
        "breakout_50d",
        "parabolic_flag",
        "rs_above_sma_50",
    }
)

_LOCAL_MIRROR_HANDOFF_TABLE = "local_mirror_handoff_state"
_LOCAL_MIRROR_SYNC_STATE_TABLE = "local_mirror_sync_state"


@dataclass(frozen=True)
class _MirrorTableSignature:
    row_count: int
    max_revision: Optional[dt.datetime]


@dataclass(frozen=True)
class _MirrorSyncCheckpoint:
    table_name: str
    scope_hash: str
    signature: _MirrorTableSignature
    synced_at: dt.datetime


def _local_mirror_handoff_table(metadata: MetaData) -> Table:
    return Table(
        _LOCAL_MIRROR_HANDOFF_TABLE,
        metadata,
        Column("id", Integer, primary_key=True),
        Column("dirty", Boolean, nullable=False, default=True),
    )


def _local_mirror_sync_state_table(metadata: MetaData) -> Table:
    return Table(
        _LOCAL_MIRROR_SYNC_STATE_TABLE,
        metadata,
        Column("table_name", String(64), primary_key=True),
        Column("scope_hash", String(64), primary_key=True),
        Column("pc_row_count", Integer, nullable=False),
        Column("pc_max_revision", DateTime, nullable=True),
        Column("synced_at", DateTime, nullable=False),
    )


def _ensure_local_mirror_sync_state(engine: Engine) -> None:
    if engine.dialect.name != "sqlite":
        return
    metadata = MetaData()
    _local_mirror_sync_state_table(metadata)
    metadata.create_all(engine)


def _ensure_local_mirror_handoff_tracking(engine: Engine) -> None:
    """Install SQLite triggers that mark any mirrored-table write as dirty."""
    if engine.dialect.name != "sqlite":
        return
    metadata = MetaData()
    handoff = _local_mirror_handoff_table(metadata)
    metadata.create_all(engine)
    existing_tables = set(inspect(engine).get_table_names())
    with engine.begin() as conn:
        conn.execute(
            sqlite_insert(handoff)
            .values(id=1, dirty=True)
            .on_conflict_do_nothing(index_elements=["id"])
        )
        for table_name, _watermark in MIRRORED_TABLES:
            if table_name not in existing_tables:
                continue
            for operation in ("INSERT", "UPDATE", "DELETE"):
                trigger_name = (
                    f"local_mirror_dirty_{table_name}_{operation.lower()}"
                )
                conn.exec_driver_sql(
                    f'CREATE TRIGGER IF NOT EXISTS "{trigger_name}" '
                    f'AFTER {operation} ON "{table_name}" '
                    "BEGIN "
                    f'UPDATE "{_LOCAL_MIRROR_HANDOFF_TABLE}" '
                    'SET "dirty" = 1 WHERE "id" = 1 AND "dirty" = 0; '
                    "END"
                )


def _mark_local_mirror_handoff_clean(conn) -> None:
    handoff = _local_mirror_handoff_table(MetaData())
    conn.execute(
        handoff.update().where(handoff.c.id == 1).values(dirty=False)
    )


def acquire_local_mirror_handoff_guard(local_engine: Engine):
    """Lock SQLite and prove no mirrored write occurred after reconciliation."""
    if local_engine is None or local_engine.dialect.name != "sqlite":
        raise RuntimeError("A SQLite local mirror is required for database handoff.")
    conn = local_engine.connect()
    try:
        # The GUI must retry rather than freeze if an external writer currently
        # owns SQLite.  The normal mirror connection default is intentionally
        # longer for background work.
        conn.exec_driver_sql("PRAGMA busy_timeout=1000")
        conn.exec_driver_sql("BEGIN IMMEDIATE")
        handoff = _local_mirror_handoff_table(MetaData())
        dirty = conn.execute(
            select(handoff.c.dirty).where(handoff.c.id == 1)
        ).scalar_one()
        if bool(dirty):
            raise RuntimeError("Local mirror changed after reconciliation.")
        return conn
    except Exception:
        try:
            conn.rollback()
        finally:
            try:
                conn.exec_driver_sql("PRAGMA busy_timeout=30000")
            except Exception:
                pass
            conn.close()
        raise


def release_local_mirror_handoff_guard(conn) -> None:
    if conn is None:
        return
    try:
        conn.rollback()
    finally:
        try:
            conn.exec_driver_sql("PRAGMA busy_timeout=30000")
        except Exception:
            pass
        conn.close()


def init_local_mirror_engine(db_path=None) -> Optional[Engine]:
    """Open (creating if needed) the laptop-local SQLite market-data mirror."""
    engine: Optional[Engine] = None
    try:
        path = Path(db_path or LOCAL_MIRROR_DB_PATH)
        path.parent.mkdir(parents=True, exist_ok=True)
        engine = create_engine(
            f"sqlite:///{path}",
            future=True,
            connect_args={"check_same_thread": False, "timeout": 30.0},
        )

        @event.listens_for(engine, "connect")
        def _set_sqlite_pragmas(dbapi_connection, connection_record) -> None:
            # WAL lets the GUI thread read while a background sync worker (or
            # a separate historical.py process) writes at the same time.
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA busy_timeout=30000")
            cursor.close()

        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        _ensure_price_history_table(engine)
        _ensure_hourly_price_history_table(engine)
        _ensure_chart_indicators_table(engine)
        _ensure_chart_indicator_manifests_table(engine)
        _ensure_scanner_metrics_table(engine)
        _ensure_scanner_metric_snapshots_table(engine)
        _ensure_symbol_refresh_failures_table(engine)
        _ensure_local_mirror_handoff_tracking(engine)
        _ensure_local_mirror_sync_state(engine)
        return engine
    except (ImportError, OSError, SQLAlchemyError, ValueError, TypeError) as exc:
        if engine is not None:
            engine.dispose()
        logger.info("Local data mirror unavailable: %s", exc)
        return None


@dataclass(frozen=True)
class DataEngineResolution:
    """Which engine the app should use for market-data reads/writes.

    ``engine`` is PC MySQL when reachable, otherwise the local SQLite mirror
    (or None if neither is available) -- callers that only care about market
    data (scanner, charts, historical refresh) should always use ``engine``.
    ``pc_engine`` is PC MySQL only, always None when the PC is unreachable --
    callers doing cross-machine state coordination (state sync, runtime
    heartbeats, main-device ownership) must use ``pc_engine``, never the
    local-mirror fallback.
    """

    engine: Optional[Engine]
    source: str  # "pc" | "local_mirror" | "none"
    pc_engine: Optional[Engine]


def _local_mirror_enabled() -> bool:
    value = os.environ.get(LOCAL_MIRROR_ENABLED_ENV, "1").strip().lower()
    return value not in {"0", "false", "no", "off"}


def resolve_data_engine(*, ensure_pc_schema: bool = True) -> DataEngineResolution:
    """Resolve the market-data engine: PC MySQL first, local mirror fallback."""
    pc_engine = init_mysql_engine(ensure_schema=ensure_pc_schema)
    if pc_engine is not None:
        return DataEngineResolution(pc_engine, "pc", pc_engine)
    if not _local_mirror_enabled():
        return DataEngineResolution(None, "none", None)
    local_engine = init_local_mirror_engine()
    if local_engine is not None:
        return DataEngineResolution(local_engine, "local_mirror", None)
    return DataEngineResolution(None, "none", None)


def mirror_table_stats(engine: Engine, table_name: str, watermark_column: str) -> Dict[str, object]:
    """Row count and latest watermark value for one table, for reporting."""
    try:
        with engine.connect() as conn:
            if not inspect(engine).has_table(table_name):
                return {"row_count": 0, "latest": None}
            count = conn.execute(text(f"SELECT COUNT(*) FROM `{table_name}`")).scalar()
            latest = conn.execute(
                text(f"SELECT MAX(`{watermark_column}`) FROM `{table_name}`")
            ).scalar()
        return {"row_count": int(count or 0), "latest": latest}
    except SQLAlchemyError as exc:
        return {"row_count": 0, "latest": None, "error": str(exc)}


