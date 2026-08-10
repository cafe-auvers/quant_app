import os
import datetime as dt
import hashlib
import logging
import re
import time
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set, Tuple

import pandas as pd
import numpy as np
from sqlalchemy import (
    create_engine,
    event,
    MetaData,
    Table,
    Column,
    String,
    Float,
    DateTime,
    Boolean,
    Integer,
    select,
    text,
    func,
    delete,
    insert,
    inspect,
    tuple_,
)
from sqlalchemy.engine import Engine, URL
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from src.utils.config import DATA_DIR, get_mysql_config
from src.utils.data_loader import download_price_history, _extract_symbol_history, compute_stock_metrics
from src.utils.market_calendar import expected_latest_market_data_date

_ensured_engines: set = set()
_MYSQL_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9_]+$")
_MYSQL_HOST_FORBIDDEN_CHARACTERS = frozenset("/\\@?#")
MYSQL_CONNECT_TIMEOUT_SECONDS = 3
MYSQL_READ_WRITE_TIMEOUT_SECONDS = 15
MYSQL_POOL_RECYCLE_SECONDS = 1800
CACHE_QUERY_SYMBOL_CHUNK_SIZE = 200
HOURLY_CACHE_QUERY_SYMBOL_CHUNK_SIZE = 100
SCANNER_QUERY_SYMBOL_CHUNK_SIZE = 500
SCANNER_METRIC_WRITE_CHUNK_SIZE = 250
logger = logging.getLogger(__name__)

SCANNER_METRICS_CACHE_VERSION = 1
CHART_INDICATOR_CACHE_VERSION = 1
REFERENCE_SYMBOL = "SPY"


def validate_mysql_identifier(value: str, *, label: str = "database name") -> str:
    """Accept a deliberately narrow set of safe MySQL identifier characters."""
    identifier = str(value or "").strip()
    if (
        not identifier
        or len(identifier) > 64
        or _MYSQL_IDENTIFIER_PATTERN.fullmatch(identifier) is None
    ):
        raise ValueError(
            f"Invalid {label} {value!r}; use 1-64 ASCII letters, digits, or underscores"
        )
    return identifier


def validate_mysql_port(value: object) -> int:
    """Validate a TCP port before passing it to the database driver."""
    try:
        port = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid MySQL port {value!r}; use an integer from 1 to 65535") from exc
    if not 1 <= port <= 65535:
        raise ValueError(f"Invalid MySQL port {value!r}; use an integer from 1 to 65535")
    return port


def _validate_mysql_host(value: object) -> str:
    host = str(value or "").strip()
    if (
        not host
        or len(host) > 255
        or any(character.isspace() or character in _MYSQL_HOST_FORBIDDEN_CHARACTERS for character in host)
    ):
        raise ValueError("Invalid MySQL host; set MYSQL_HOST to a hostname or IP address")
    return host


def _validate_mysql_user(value: object) -> str:
    user = str(value or "").strip()
    if not user or len(user) > 128 or any(character.isspace() or ord(character) < 32 for character in user):
        raise ValueError("Invalid MySQL user; set MYSQL_USER to a non-empty database account")
    return user


def validate_mysql_config(config: Optional[Dict[str, str]] = None) -> Dict[str, object]:
    """Validate optional-cache settings and return normalized connection values.

    The app intentionally uses a pre-provisioned database.  Schema creation
    needs only rights within that database; it never needs a global CREATE
    DATABASE privilege.
    """
    raw = config or get_mysql_config()
    return {
        "host": _validate_mysql_host(raw.get("host")),
        "port": validate_mysql_port(raw.get("port")),
        "user": _validate_mysql_user(raw.get("user")),
        "password": str(raw.get("password") or ""),
        "database": validate_mysql_identifier(raw.get("database"), label="database name"),
    }

def _utcnow_naive() -> dt.datetime:
    """Return a naive UTC timestamp for existing DB columns and comparisons."""
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def get_mysql_connection_url(db_name: Optional[str] = None) -> URL:
    # Validate an explicit identifier before inspecting environment settings so
    # callers get the actionable injection-safety error they asked for.
    if db_name is not None:
        db_name = validate_mysql_identifier(db_name)
    config = validate_mysql_config()
    if db_name is None:
        db_name = config["database"]
    db_name = validate_mysql_identifier(str(db_name))

    host = str(config["host"])
    port = int(config["port"])
    user = str(config["user"])
    password = str(config["password"])

    return URL.create(
        drivername="mysql+pymysql",
        username=user or None,
        password=password or None,
        host=host,
        port=port,
        database=db_name,
        query={"charset": "utf8mb4"},
    )


def init_mysql_engine(
    db_name: Optional[str] = None, *, log_unavailable: bool = True
) -> Optional[Engine]:
    """Open the optional MySQL cache, returning ``None`` when unavailable.

    Periodic connectivity probes can set ``log_unavailable=False`` to keep an
    expected offline PC from producing the same INFO message on every poll.
    The failure remains available at DEBUG level for diagnostics.
    """
    engine: Optional[Engine] = None
    try:
        if db_name is None:
            db_name = str(validate_mysql_config()["database"])
        db_name = validate_mysql_identifier(db_name)
        engine = create_engine(
            get_mysql_connection_url(db_name=db_name),
            future=True,
            pool_pre_ping=True,
            pool_recycle=MYSQL_POOL_RECYCLE_SECONDS,
            connect_args={
                "connect_timeout": MYSQL_CONNECT_TIMEOUT_SECONDS,
                "read_timeout": MYSQL_READ_WRITE_TIMEOUT_SECONDS,
                "write_timeout": MYSQL_READ_WRITE_TIMEOUT_SECONDS,
            },
        )
        # Connect before doing schema work so a disabled/unreachable optional
        # cache fails quickly and cleanly instead of surfacing later in a UI
        # action.  The configured account must already have access to DB_NAME.
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        _ensure_price_history_table(engine)
        _ensure_hourly_price_history_table(engine)
        _ensure_chart_indicators_table(engine)
        _ensure_chart_indicator_manifests_table(engine)
        _ensure_intraday_price_history_table(engine)
        _ensure_scanner_metrics_table(engine)
        _ensure_scanner_metric_snapshots_table(engine)
        return engine
    except (ImportError, OSError, SQLAlchemyError, ValueError, TypeError) as exc:
        if engine is not None:
            engine.dispose()
        log = logger.info if log_unavailable else logger.debug
        log("MySQL cache disabled: %s", exc)
        return None


# --- Local mirror (offline fallback when the PC's MySQL is unreachable) -----
#
# The PC's MySQL database remains the canonical market-data cache (see
# docs/pc_sync_data_pipeline.md).  The SQLite file is the laptop's offline
# mirror.  Normal mirroring is PC -> laptop.  On a local -> PC runtime
# transition, a deliberately narrow reconciliation may promote *missing,
# completed raw bars* from the mirror without ever overwriting an existing PC
# bar; derived caches are rebuilt on the PC and then mirrored back down.

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


def _local_mirror_handoff_table(metadata: MetaData) -> Table:
    return Table(
        _LOCAL_MIRROR_HANDOFF_TABLE,
        metadata,
        Column("id", Integer, primary_key=True),
        Column("dirty", Boolean, nullable=False, default=True),
    )


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


def resolve_data_engine() -> DataEngineResolution:
    """Resolve the market-data engine: PC MySQL first, local mirror fallback."""
    pc_engine = init_mysql_engine()
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


def _normalized_watermark(value: object) -> Optional[dt.datetime]:
    if value is None or pd.isna(value):
        return None
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is not None:
        timestamp = timestamp.tz_convert("UTC").tz_localize(None)
    return timestamp.to_pydatetime()


def _raw_mirror_cutoff(spec: _RawMirrorSpec) -> dt.datetime:
    """Latest completed bar that is eligible for a local -> PC promotion."""
    if spec.table_name == "price_history":
        return dt.datetime.combine(expected_latest_market_data_date(), dt.time.max)
    # Do not promote the currently forming hourly candle.  Cached timestamps
    # are stored as naive UTC, so the start of the current UTC hour is the
    # first timestamp that is not yet safe to publish to the PC.
    current_hour = _utcnow_naive().replace(minute=0, second=0, microsecond=0)
    return current_hour - dt.timedelta(microseconds=1)


def _raw_group_watermarks(
    engine: Engine,
    spec: _RawMirrorSpec,
    *,
    cutoff: Optional[dt.datetime] = None,
) -> Dict[Tuple[object, ...], dt.datetime]:
    if not inspect(engine).has_table(spec.table_name):
        return {}
    table = Table(spec.table_name, MetaData(), autoload_with=engine)
    required_columns = {
        spec.watermark_column,
        *spec.group_columns,
    }
    missing = required_columns.difference(table.columns.keys())
    if missing:
        raise ValueError(
            f"{spec.table_name} is missing reconciliation column(s): "
            f"{', '.join(sorted(missing))}"
        )

    watermark = table.c[spec.watermark_column]
    group_columns = [table.c[name] for name in spec.group_columns]
    stmt = select(*group_columns, func.max(watermark).label("latest"))
    conditions = []
    if spec.required_value is not None:
        column_name, required_value = spec.required_value
        conditions.append(table.c[column_name] == required_value)
    if cutoff is not None:
        conditions.append(watermark <= cutoff)
    if conditions:
        stmt = stmt.where(*conditions)
    stmt = stmt.group_by(*group_columns)

    with engine.connect() as conn:
        rows = conn.execute(stmt).all()
    watermarks: Dict[Tuple[object, ...], dt.datetime] = {}
    for row in rows:
        latest = _normalized_watermark(row.latest)
        if latest is None:
            continue
        key = tuple(row[index] for index in range(len(group_columns)))
        watermarks[key] = latest
    return watermarks


def _validate_promoted_market_bar(
    spec: _RawMirrorSpec,
    record: Dict[str, object],
    cutoff: dt.datetime,
) -> None:
    """Reject malformed or incomplete local rows before they can reach MySQL."""
    symbol = str(record.get("symbol") or "").strip()
    if not symbol or len(symbol) > 20 or symbol != symbol.upper():
        raise ValueError(f"Invalid symbol in local {spec.table_name}: {symbol!r}")

    if spec.required_value is not None:
        column_name, required_value = spec.required_value
        if str(record.get(column_name) or "").strip().lower() != required_value:
            raise ValueError(
                f"Unsupported {column_name} in local {spec.table_name}: "
                f"{record.get(column_name)!r}"
            )
    if spec.table_name == "hourly_price_history":
        source = str(record.get("source") or "").strip()
        if not source or len(source) > 20:
            raise ValueError(
                f"Invalid source in local hourly_price_history: {source!r}"
            )

    watermark = _normalized_watermark(record.get(spec.watermark_column))
    if watermark is None or watermark > cutoff:
        raise ValueError(
            f"Incomplete/future local bar in {spec.table_name}: {watermark!r}"
        )

    prices: Dict[str, float] = {}
    for column_name in ("open", "high", "low", "close"):
        raw_value = record.get(column_name)
        try:
            value = float(raw_value)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"Invalid {column_name} in local {spec.table_name} for {symbol}"
            ) from exc
        if not np.isfinite(value) or value <= 0:
            raise ValueError(
                f"Invalid {column_name} in local {spec.table_name} for {symbol}"
            )
        prices[column_name] = value

    tolerance = max(1e-9, abs(prices["high"]) * 1e-9)
    if (
        prices["high"] + tolerance
        < max(prices["open"], prices["low"], prices["close"])
        or prices["low"] - tolerance
        > min(prices["open"], prices["high"], prices["close"])
    ):
        raise ValueError(f"Invalid OHLC range in local {spec.table_name} for {symbol}")

    volume = record.get("volume")
    if volume is not None:
        try:
            volume_value = float(volume)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"Invalid volume in local {spec.table_name} for {symbol}"
            ) from exc
        if not np.isfinite(volume_value) or volume_value < 0:
            raise ValueError(
                f"Invalid volume in local {spec.table_name} for {symbol}"
            )


def _normalize_reconcile_value(column, value: object) -> object:
    if value is None or pd.isna(value):
        return None
    if column.name in _BOOLEAN_RECONCILE_COLUMNS:
        return bool(value)
    if isinstance(column.type, DateTime):
        normalized = _normalized_watermark(value)
        return normalized.isoformat(timespec="microseconds") if normalized else None
    if isinstance(column.type, Boolean):
        return bool(value)
    if isinstance(column.type, Integer):
        return int(value)
    if isinstance(column.type, Float):
        numeric = float(value)
        if not np.isfinite(numeric):
            return str(numeric)
        return format(numeric, ".17g")
    if isinstance(value, bytes):
        return value.hex()
    return str(value)


def _normalized_reconcile_key(
    table: Table, column_names: Tuple[str, ...], record: Dict[str, object]
) -> Tuple[object, ...]:
    return tuple(
        _normalize_reconcile_value(table.c[name], record.get(name))
        for name in column_names
    )


def _validate_reconcile_table_schema(
    pc_table: Table,
    local_table: Table,
    spec: _ReconcileTableSpec,
) -> None:
    required_columns = set(spec.primary_key).union(
        spec.partition_columns,
        {spec.watermark_column, spec.revision_column},
    )
    missing_pc_required = required_columns.difference(pc_table.columns.keys())
    missing_local_required = required_columns.difference(
        local_table.columns.keys()
    )
    if missing_pc_required or missing_local_required:
        raise ValueError(
            f"Unsafe {spec.table_name} schema: missing required column(s); "
            f"PC {', '.join(sorted(missing_pc_required)) or 'none'}, local "
            f"{', '.join(sorted(missing_local_required)) or 'none'}."
        )
    pc_primary_key = tuple(column.name for column in pc_table.primary_key.columns)
    local_primary_key = tuple(
        column.name for column in local_table.primary_key.columns
    )
    if pc_primary_key != spec.primary_key or local_primary_key != spec.primary_key:
        raise ValueError(
            f"Unsafe {spec.table_name} schema: expected primary key "
            f"{spec.primary_key}, found PC {pc_primary_key} and local "
            f"{local_primary_key}."
        )
    missing_local_columns = set(pc_table.columns.keys()).difference(
        local_table.columns.keys()
    )
    if missing_local_columns:
        raise ValueError(
            f"Local {spec.table_name} is missing PC column(s): "
            f"{', '.join(sorted(missing_local_columns))}"
        )


def _partition_filter(table: Table, columns: Tuple[str, ...], keys: List[Tuple[object, ...]]):
    if len(columns) == 1:
        return table.c[columns[0]].in_([key[0] for key in keys])
    return tuple_(*(table.c[name] for name in columns)).in_(keys)


def _partition_fingerprints(
    engine: Engine,
    spec: _ReconcileTableSpec,
    *,
    raw_partition_keys: Optional[List[Tuple[object, ...]]] = None,
    connection=None,
) -> Dict[Tuple[object, ...], _PartitionFingerprint]:
    """Hash every ordered primary key and value per logical partition.

    Watermark/count checks alone cannot detect an interior gap or an older
    corrected row.  This digest is intentionally complete so ``success``
    means the compared PC/local partitions really contain the same rows.
    """
    bind = connection if connection is not None else engine
    if not inspect(bind).has_table(spec.table_name):
        return {}
    table = Table(spec.table_name, MetaData(), autoload_with=bind)
    required = set(spec.primary_key).union(spec.partition_columns)
    missing = required.difference(table.columns.keys())
    if missing:
        raise ValueError(
            f"{spec.table_name} is missing reconciliation column(s): "
            f"{', '.join(sorted(missing))}"
        )

    ordered_names = tuple(sorted(table.columns.keys()))
    selected_columns = [table.c[name] for name in ordered_names]
    value_indexes = {name: index for index, name in enumerate(ordered_names)}
    order_names = list(spec.partition_columns)
    order_names.extend(name for name in spec.primary_key if name not in order_names)
    stmt = select(*selected_columns)
    if raw_partition_keys is not None:
        if not raw_partition_keys:
            return {}
        stmt = stmt.where(
            _partition_filter(
                table, spec.partition_columns, raw_partition_keys
            )
        )
    stmt = stmt.order_by(*(table.c[name] for name in order_names))

    states: Dict[
        Tuple[object, ...], Tuple[Tuple[object, ...], int, object]
    ] = {}
    boolean_indexes = [
        value_indexes[name]
        for name in ordered_names
        if name in _BOOLEAN_RECONCILE_COLUMNS
    ]
    def consume(conn) -> None:
        result = conn.execution_options(stream_results=True).execute(stmt)
        for row in result:
            values = list(row)
            raw_partition = tuple(
                values[value_indexes[name]] for name in spec.partition_columns
            )
            normalized_partition = tuple(
                (
                    _normalized_watermark(value).isoformat(timespec="microseconds")
                    if isinstance(value, (dt.datetime, pd.Timestamp))
                    else value
                )
                for value in raw_partition
            )
            state = states.get(normalized_partition)
            if state is None:
                state = (raw_partition, 0, hashlib.sha256())
            raw_key, row_count, digest = state
            # MySQL reflects BOOLEAN as TINYINT(1), whereas SQLite reflects
            # it as Boolean.  Normalize known semantic boolean columns so the
            # same row hashes identically across both dialects.
            for index in boolean_indexes:
                if values[index] is not None:
                    values[index] = bool(values[index])
            payload = repr(tuple(values)).encode("utf-8")
            digest.update(len(payload).to_bytes(4, "big"))
            digest.update(payload)
            states[normalized_partition] = (raw_key, row_count + 1, digest)

    if connection is not None:
        consume(connection)
    else:
        with engine.connect() as conn:
            consume(conn)

    return {
        key: _PartitionFingerprint(raw_key, row_count, digest.hexdigest())
        for key, (raw_key, row_count, digest) in states.items()
    }


def _mismatched_partitions(
    pc_fingerprints: Dict[Tuple[object, ...], _PartitionFingerprint],
    local_fingerprints: Dict[Tuple[object, ...], _PartitionFingerprint],
) -> List[Tuple[object, ...]]:
    mismatched = []
    for key in sorted(
        set(pc_fingerprints).union(local_fingerprints),
        key=lambda value: repr(value),
    ):
        pc_value = pc_fingerprints.get(key)
        local_value = local_fingerprints.get(key)
        if (
            pc_value is None
            or local_value is None
            or pc_value.row_count != local_value.row_count
            or pc_value.digest != local_value.digest
        ):
            mismatched.append(key)
    return mismatched


def _fetch_partition_rows(
    engine: Engine,
    table: Table,
    spec: _ReconcileTableSpec,
    raw_partition_keys: List[Tuple[object, ...]],
    *,
    connection=None,
) -> Dict[Tuple[object, ...], Tuple[Dict[str, object], Tuple[object, ...]]]:
    if not raw_partition_keys:
        return {}
    stmt = select(table).where(
        _partition_filter(table, spec.partition_columns, raw_partition_keys)
    )
    rows: Dict[
        Tuple[object, ...], Tuple[Dict[str, object], Tuple[object, ...]]
    ] = {}
    ordered_names = tuple(sorted(table.columns.keys()))
    def consume(conn) -> None:
        for row in conn.execution_options(stream_results=True).execute(stmt):
            record = dict(row._mapping)
            primary_key = _normalized_reconcile_key(
                table, spec.primary_key, record
            )
            normalized_row = tuple(
                _normalize_reconcile_value(table.c[name], record.get(name))
                for name in ordered_names
            )
            rows[primary_key] = (record, normalized_row)

    if connection is not None:
        consume(connection)
    else:
        with engine.connect() as conn:
            consume(conn)
    return rows


def _raw_validation_spec(table_name: str) -> _RawMirrorSpec:
    for spec in _RAW_MIRROR_SPECS:
        if spec.table_name == table_name:
            return spec
    raise ValueError(f"No raw validation policy exists for {table_name!r}.")


def _insert_missing_local_raw_partitions(
    pc_engine: Engine,
    local_engine: Engine,
    spec: _ReconcileTableSpec,
    partition_keys: List[Tuple[object, ...]],
    pc_fingerprints: Dict[Tuple[object, ...], _PartitionFingerprint],
    local_fingerprints: Dict[Tuple[object, ...], _PartitionFingerprint],
    *,
    partition_chunk_size: int = 100,
) -> Tuple[int, Set[str]]:
    if not partition_keys:
        return 0, set()
    pc_table = Table(spec.table_name, MetaData(), autoload_with=pc_engine)
    local_table = Table(spec.table_name, MetaData(), autoload_with=local_engine)
    _validate_reconcile_table_schema(pc_table, local_table, spec)
    validation_spec = _raw_validation_spec(spec.table_name)
    cutoff = _raw_mirror_cutoff(validation_spec)
    inserted = 0
    affected_symbols: Set[str] = set()

    for key_chunk in _record_chunks(partition_keys, partition_chunk_size):
        raw_keys = [
            (
                local_fingerprints.get(key)
                or pc_fingerprints.get(key)
            ).raw_key
            for key in key_chunk
        ]
        local_rows = _fetch_partition_rows(
            local_engine, local_table, spec, raw_keys
        )
        pc_rows = _fetch_partition_rows(pc_engine, pc_table, spec, raw_keys)
        missing_records = []
        for primary_key, (record, _normalized_row) in local_rows.items():
            if primary_key in pc_rows:
                continue
            _validate_promoted_market_bar(validation_spec, record, cutoff)
            missing_records.append(
                {
                    name: value
                    for name, value in record.items()
                    if name in pc_table.columns
                }
            )
            affected_symbols.add(str(record.get("symbol") or "").upper())
        for records in _record_chunks(missing_records, 1000):
            with pc_engine.begin() as conn:
                # Existing PC keys were removed above.  A duplicate caused by
                # a concurrent PC refresh intentionally fails this pass so no
                # PC value can be overwritten or an INSERT warning concealed.
                conn.execute(insert(pc_table), records)
            inserted += len(records)
    return inserted, affected_symbols


def _delete_primary_keys(
    conn,
    table: Table,
    primary_key: Tuple[str, ...],
    raw_keys: List[Tuple[object, ...]],
) -> None:
    if not raw_keys:
        return
    max_keys = max(1, 800 // max(1, len(primary_key)))
    for key_chunk in _record_chunks(raw_keys, max_keys):
        if len(primary_key) == 1:
            condition = table.c[primary_key[0]].in_(
                [key[0] for key in key_chunk]
            )
        else:
            condition = tuple_(
                *(table.c[name] for name in primary_key)
            ).in_(key_chunk)
        conn.execute(delete(table).where(condition))


def _copy_pc_partitions_to_local_exactly(
    pc_engine: Engine,
    local_engine: Engine,
    spec: _ReconcileTableSpec,
    partition_keys: List[Tuple[object, ...]],
    pc_fingerprints: Dict[Tuple[object, ...], _PartitionFingerprint],
    local_fingerprints: Dict[Tuple[object, ...], _PartitionFingerprint],
    *,
    partition_chunk_size: int = 100,
    local_connection=None,
    preserve_local_raw: bool = True,
) -> int:
    """Apply PC-wins rows and remove local-only keys for changed partitions."""
    if not partition_keys:
        return 0
    pc_table = Table(spec.table_name, MetaData(), autoload_with=pc_engine)
    local_bind = local_connection if local_connection is not None else local_engine
    local_table = Table(spec.table_name, MetaData(), autoload_with=local_bind)
    _validate_reconcile_table_schema(pc_table, local_table, spec)
    changed = 0

    for key_chunk in _record_chunks(partition_keys, partition_chunk_size):
        raw_keys = [
            (
                pc_fingerprints.get(key)
                or local_fingerprints.get(key)
            ).raw_key
            for key in key_chunk
        ]
        pc_rows = _fetch_partition_rows(pc_engine, pc_table, spec, raw_keys)
        local_rows = _fetch_partition_rows(
            local_engine,
            local_table,
            spec,
            raw_keys,
            connection=local_connection,
        )
        upsert_records = []
        for primary_key, (pc_record, pc_normalized) in pc_rows.items():
            local_value = local_rows.get(primary_key)
            if local_value is None or local_value[1] != pc_normalized:
                upsert_records.append(
                    {
                        name: value
                        for name, value in pc_record.items()
                        if name in local_table.columns
                    }
                )
        # Raw local-only keys are the only data eligible for promotion on the
        # next pass.  If one arrives after the promotion snapshot, preserving
        # it makes the final equality fence fail and retry; deleting it here
        # could silently erase a just-completed offline refresh.  Derived and
        # operational rows remain PC-canonical and may be removed locally.
        delete_keys = [] if spec.reverse_raw and preserve_local_raw else [
            tuple(local_rows[key][0].get(name) for name in spec.primary_key)
            for key in local_rows.keys() - pc_rows.keys()
        ]
        def apply_changes(conn) -> None:
            _delete_primary_keys(conn, local_table, spec.primary_key, delete_keys)
            if upsert_records:
                _execute_bulk_upsert(
                    conn,
                    local_table,
                    upsert_records,
                    spec.primary_key,
                    local_engine.dialect.name,
                )
        if local_connection is not None:
            apply_changes(local_connection)
        else:
            with local_engine.begin() as conn:
                apply_changes(conn)
        changed += len(delete_keys) + len(upsert_records)
    return changed


def sync_mirror_table(
    pc_engine: Engine,
    local_engine: Engine,
    table_name: str,
    watermark_column: str,
    chunk_size: int = 5000,
) -> int:
    """Incrementally upsert rows newer than the local watermark from PC -> local.

    Reflection-based (autoload from ``pc_engine``) so every mirrored table is
    handled generically instead of re-declaring each table's columns here.
    """
    fetch_size = max(1, int(chunk_size or 1))
    pc_metadata = MetaData()
    pc_table = Table(table_name, pc_metadata, autoload_with=pc_engine)
    if watermark_column not in pc_table.columns:
        raise ValueError(
            f"Mirror watermark column {watermark_column!r} is missing from {table_name!r}."
        )
    pc_metadata.create_all(local_engine, tables=[pc_table])

    local_metadata = MetaData()
    local_table = Table(table_name, local_metadata, autoload_with=local_engine)

    since = None
    if watermark_column in local_table.columns:
        with local_engine.connect() as conn:
            since = conn.execute(select(func.max(local_table.c[watermark_column]))).scalar()

    pk_columns = tuple(column.name for column in pc_table.primary_key.columns)
    if not pk_columns:
        raise ValueError(f"Mirrored table {table_name!r} has no primary key.")

    stmt = select(pc_table)
    if since is not None:
        # Re-read the current boundary as well as newer rows.  A previous run
        # may have stopped halfway through a group of rows sharing the same
        # date/timestamp; the upsert makes replaying that boundary harmless.
        stmt = stmt.where(pc_table.c[watermark_column] >= since)

    # The watermark must lead the ordering.  If rows were streamed in primary
    # key order, the first committed chunk could contain a table-wide maximum
    # timestamp for one symbol.  A resumed run would then infer that later
    # symbols were already copied and skip their older rows permanently.
    order_columns = [pc_table.c[watermark_column]]
    order_columns.extend(
        pc_table.c[name] for name in pk_columns if name != watermark_column
    )
    stmt = stmt.order_by(*order_columns)

    rows_written = 0
    with pc_engine.connect() as pc_conn:
        pc_conn = pc_conn.execution_options(stream_results=True)
        result = pc_conn.execute(stmt)
        while True:
            batch = result.fetchmany(fetch_size)
            if not batch:
                break
            records = [dict(row._mapping) for row in batch]
            with local_engine.begin() as local_conn:
                rows_written += _execute_bulk_upsert(
                    local_conn, local_table, records, pk_columns, "sqlite"
                )
    return rows_written


def sync_local_mirror_from_pc(
    pc_engine: Engine,
    local_engine: Engine,
    tables: Tuple[Tuple[str, str], ...] = MIRRORED_TABLES,
    log_callback: Optional[Callable[[str], None]] = None,
    error_callback: Optional[Callable[[str], None]] = None,
) -> Dict[str, int]:
    """Best-effort PC -> laptop mirror sync; one table's failure doesn't abort the rest."""
    written: Dict[str, int] = {}
    for table_name, watermark_column in tables:
        try:
            count = sync_mirror_table(
                pc_engine, local_engine, table_name, watermark_column
            )
            written[table_name] = count
            if log_callback and count:
                log_callback(f"Local mirror: synced {count} row(s) for {table_name}.")
        except (SQLAlchemyError, ValueError, TypeError) as exc:
            message = f"Local mirror: could not sync {table_name}: {exc}"
            logger.warning(message)
            if log_callback:
                log_callback(message)
            if error_callback:
                error_callback(message)
    return written


def _pc_daily_watermarks_read_only(
    pc_engine: Engine, symbols: List[str]
) -> Dict[str, Tuple[Optional[dt.datetime], int]]:
    """Read daily input watermarks without running a PC schema initializer."""
    cleaned_symbols = _clean_symbols(symbols)
    if not cleaned_symbols:
        return {}
    table = Table("price_history", MetaData(), autoload_with=pc_engine)
    rows = []
    with pc_engine.connect() as conn:
        for chunk in _record_chunks(
            cleaned_symbols, CACHE_QUERY_SYMBOL_CHUNK_SIZE
        ):
            rows.extend(
                conn.execute(
                    select(
                        table.c.symbol,
                        func.max(table.c.date).label("latest_date"),
                        func.count().label("row_count"),
                    )
                    .where(
                        table.c.symbol.in_(chunk),
                        table.c.interval == "1d",
                    )
                    .group_by(table.c.symbol)
                ).all()
            )
    return {
        str(row.symbol).upper(): (row.latest_date, int(row.row_count or 0))
        for row in rows
    }


def _pc_reconciliation_universe_read_only(
    pc_engine: Engine, tickers: Optional[List[str]]
) -> List[str]:
    """Resolve the active universe using SELECTs only on the PC database."""
    symbols = _clean_symbols(tickers or [])
    if not symbols:
        try:
            from src.utils.data_loader import get_default_universe

            symbols = _clean_symbols(
                get_default_universe(max_symbols=None, refresh=False)
            )
        except Exception:
            symbols = []
    if not symbols:
        table = Table("price_history", MetaData(), autoload_with=pc_engine)
        with pc_engine.connect() as conn:
            symbols = _clean_symbols(
                list(
                    conn.execute(
                        select(table.c.symbol)
                        .where(table.c.interval == "1d")
                        .distinct()
                    ).scalars()
                )
            )
    return list(dict.fromkeys([REFERENCE_SYMBOL, *symbols]))


def _verify_pc_derived_data_current_read_only(
    pc_engine: Engine, tickers: Optional[List[str]]
) -> None:
    """Prove PC derived caches match raw inputs without changing the PC.

    The periodic mirror runs while the dashboard is already routed to the PC.
    It must therefore never rebuild, invalidate, or backfill PC cache rows. A
    stale manifest/snapshot means a refresh is still in progress (or needed),
    so this mirror attempt is retried later.
    """
    input_symbols = _pc_reconciliation_universe_read_only(pc_engine, tickers)
    history_watermarks = _pc_daily_watermarks_read_only(
        pc_engine, input_symbols
    )
    reference_values = _history_watermark_values(
        history_watermarks.get(REFERENCE_SYMBOL)
    )

    chart_symbols = [
        symbol
        for symbol in input_symbols
        if symbol != REFERENCE_SYMBOL
        and _history_watermark_values(history_watermarks.get(symbol)) is not None
    ]
    if chart_symbols:
        if reference_values is None:
            raise RuntimeError(
                f"PC {REFERENCE_SYMBOL} history is unavailable for derived-data verification."
            )
        manifests_table = Table(
            "chart_indicator_manifests", MetaData(), autoload_with=pc_engine
        )
        manifests: Dict[str, Dict[str, object]] = {}
        with pc_engine.connect() as conn:
            for chunk in _record_chunks(
                chart_symbols, CACHE_QUERY_SYMBOL_CHUNK_SIZE
            ):
                for row in conn.execute(
                    select(manifests_table).where(
                        manifests_table.c.symbol.in_(chunk)
                    )
                ):
                    manifests[str(row.symbol).upper()] = {
                        "reference_symbol": str(row.reference_symbol).upper(),
                        "source_latest_date": row.source_latest_date,
                        "source_row_count": int(row.source_row_count or 0),
                        "reference_latest_date": row.reference_latest_date,
                        "reference_row_count": int(row.reference_row_count or 0),
                        "cache_version": int(row.cache_version or 0),
                    }
        stale_chart_symbols = [
            symbol
            for symbol in chart_symbols
            if not _chart_indicator_manifest_matches(
                manifests.get(symbol),
                _history_watermark_values(history_watermarks.get(symbol)),
                reference_values,
                REFERENCE_SYMBOL,
            )
        ]
        if stale_chart_symbols:
            raise RuntimeError(
                "PC chart indicators are not current for "
                f"{len(stale_chart_symbols)} symbol(s)."
            )

    metric_symbols = [
        symbol for symbol in input_symbols if symbol != REFERENCE_SYMBOL
    ]
    if not metric_symbols:
        return
    snapshots_table = Table(
        "scanner_metric_snapshots", MetaData(), autoload_with=pc_engine
    )
    metrics_table = Table("scanner_metrics", MetaData(), autoload_with=pc_engine)
    snapshot_date = scanner_metrics_snapshot_date()
    expected_fingerprint = scanner_metrics_input_fingerprint(
        input_symbols, history_watermarks
    )
    with pc_engine.connect() as conn:
        snapshot = conn.execute(
            select(
                snapshots_table.c.input_fingerprint,
                snapshots_table.c.metric_count,
            ).where(snapshots_table.c.snapshot_date == snapshot_date)
        ).one_or_none()
        actual_count = 0
        if snapshot is not None:
            for chunk in _record_chunks(
                metric_symbols, SCANNER_QUERY_SYMBOL_CHUNK_SIZE
            ):
                actual_count += int(
                    conn.execute(
                        select(func.count())
                        .select_from(metrics_table)
                        .where(
                            metrics_table.c.symbol.in_(chunk),
                            metrics_table.c.date == snapshot_date,
                        )
                    ).scalar_one()
                )
    if (
        snapshot is None
        or snapshot.input_fingerprint != expected_fingerprint
        or int(snapshot.metric_count or 0) <= 0
        or actual_count != int(snapshot.metric_count or 0)
    ):
        raise RuntimeError("PC scanner metrics are not current.")


def _sync_clean_local_mirror_from_pc_exactly(
    pc_engine: Engine,
    local_engine: Engine,
    tables: Tuple[Tuple[str, str], ...],
    *,
    tickers: Optional[List[str]],
    verify_derived: bool,
) -> Dict[str, int]:
    """Implement the PC-read-only active mirror under one SQLite write lock."""
    if local_engine is None or local_engine.dialect.name != "sqlite":
        raise RuntimeError("An SQLite local mirror is required for atomic sync.")

    requested_names = {table_name for table_name, _watermark in tables}
    reconcile_specs = [
        spec
        for spec in _RECONCILE_TABLE_SPECS
        if spec.table_name in requested_names
    ]
    unsupported = requested_names.difference(
        spec.table_name for spec in reconcile_specs
    )
    if unsupported:
        raise RuntimeError(
            "No exact reconciliation policy for: "
            + ", ".join(sorted(unsupported))
        )

    local_conn = None
    try:
        with pc_engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        _ensure_local_mirror_handoff_tracking(local_engine)
        local_conn = local_engine.connect()
        local_conn.exec_driver_sql("BEGIN IMMEDIATE")
        handoff = _local_mirror_handoff_table(MetaData())
        dirty = local_conn.execute(
            select(handoff.c.dirty).where(handoff.c.id == 1)
        ).scalar_one()
        if bool(dirty):
            raise LocalMirrorNeedsReconciliationError(
                "Local mirror contains changes that require staged reconciliation."
            )

        # Equal or empty legacy tables must not bypass schema validation.
        for spec in reconcile_specs:
            pc_table = Table(
                spec.table_name, MetaData(), autoload_with=pc_engine
            )
            local_table = Table(
                spec.table_name, MetaData(), autoload_with=local_conn
            )
            _validate_reconcile_table_schema(pc_table, local_table, spec)

        pc_before = {
            spec.table_name: _partition_fingerprints(pc_engine, spec)
            for spec in reconcile_specs
        }
        if verify_derived:
            _verify_pc_derived_data_current_read_only(pc_engine, tickers)
            pc_after_derived_check = {
                spec.table_name: _partition_fingerprints(pc_engine, spec)
                for spec in reconcile_specs
            }
            changed_during_verification = [
                spec.table_name
                for spec in reconcile_specs
                if _mismatched_partitions(
                    pc_before[spec.table_name],
                    pc_after_derived_check[spec.table_name],
                )
            ]
            if changed_during_verification:
                raise RuntimeError(
                    "PC database changed during derived-data verification: "
                    + ", ".join(changed_during_verification)
                )

        local_before = {
            spec.table_name: _partition_fingerprints(
                local_engine, spec, connection=local_conn
            )
            for spec in reconcile_specs
        }
        pending_written: Dict[str, int] = {}
        for spec in reconcile_specs:
            mismatched = _mismatched_partitions(
                pc_before[spec.table_name], local_before[spec.table_name]
            )
            pending_written[spec.table_name] = (
                _copy_pc_partitions_to_local_exactly(
                    pc_engine,
                    local_engine,
                    spec,
                    mismatched,
                    pc_before[spec.table_name],
                    local_before[spec.table_name],
                    local_connection=local_conn,
                    preserve_local_raw=False,
                )
            )

        local_after = {
            spec.table_name: _partition_fingerprints(
                local_engine, spec, connection=local_conn
            )
            for spec in reconcile_specs
        }
        pc_after = {
            spec.table_name: _partition_fingerprints(pc_engine, spec)
            for spec in reconcile_specs
        }
        unstable = [
            spec.table_name
            for spec in reconcile_specs
            if _mismatched_partitions(
                pc_before[spec.table_name], local_after[spec.table_name]
            )
            or _mismatched_partitions(
                pc_before[spec.table_name], pc_after[spec.table_name]
            )
        ]
        if unstable:
            raise RuntimeError(
                "database changed during atomic mirror sync: "
                + ", ".join(unstable)
            )

        _mark_local_mirror_handoff_clean(local_conn)
        local_conn.commit()
        return pending_written
    except LocalMirrorNeedsReconciliationError:
        if local_conn is not None:
            local_conn.rollback()
        raise
    except Exception as exc:
        if local_conn is not None:
            try:
                local_conn.rollback()
            except Exception:
                pass
        raise RuntimeError(f"Exact PC -> local mirror sync failed: {exc}") from exc
    finally:
        if local_conn is not None:
            local_conn.close()


def sync_local_mirror_from_pc_atomic(
    pc_engine: Engine,
    local_engine: Engine,
    tables: Tuple[Tuple[str, str], ...] = MIRRORED_TABLES,
    *,
    tickers: Optional[List[str]] = None,
    verify_derived: bool = True,
) -> Dict[str, int]:
    """Publish an exact PC generation only when the local mirror is clean.

    All keys and values are compared, causal derived caches are verified, and
    every local change shares one SQLite transaction. The PC is read-only in
    this active-dashboard path; tracked local writes raise a typed exception
    so the UI can stage the normal guarded reconciliation. Readers see the old
    generation or the new one—never a partially copied collection of tables.

    Full content comparison is deletion-aware and deliberately avoids a
    timestamp cursor, so out-of-order commits and old corrections cannot fall
    through a replication hole.
    """
    return _sync_clean_local_mirror_from_pc_exactly(
        pc_engine,
        local_engine,
        tables,
        tickers=tickers,
        verify_derived=verify_derived,
    )

def _reconciliation_universe(
    pc_engine: Engine,
    tickers: Optional[List[str]],
    affected_daily_symbols: Set[str],
) -> Tuple[List[str], List[str]]:
    """Return scanner and chart symbol sets without doing a network refresh."""
    scanner_symbols = _clean_symbols(tickers or [])
    if not scanner_symbols:
        try:
            from src.utils.data_loader import get_default_universe

            scanner_symbols = _clean_symbols(
                get_default_universe(max_symbols=None, refresh=False)
            )
        except Exception:
            scanner_symbols = []
    if not scanner_symbols:
        price_history = _ensure_price_history_table(pc_engine)
        with pc_engine.connect() as conn:
            scanner_symbols = _clean_symbols(
                list(
                    conn.execute(
                        select(price_history.c.symbol)
                        .where(price_history.c.interval == "1d")
                        .distinct()
                    ).scalars()
                )
            )

    scanner_symbols = list(
        dict.fromkeys([REFERENCE_SYMBOL, *scanner_symbols])
    )
    chart_symbols = list(
        dict.fromkeys(
            [REFERENCE_SYMBOL, *scanner_symbols, *sorted(affected_daily_symbols)]
        )
    )
    return scanner_symbols, chart_symbols


def _rebuild_and_verify_pc_derived_data(
    pc_engine: Engine,
    tickers: Optional[List[str]],
    affected_daily_symbols: Set[str],
) -> None:
    """Make PC chart/scanner caches causal to the reconciled daily history."""
    scanner_symbols, chart_symbols = _reconciliation_universe(
        pc_engine, tickers, affected_daily_symbols
    )
    chart_inputs = _clean_symbols(chart_symbols)
    history_watermarks = get_price_history_watermarks(
        pc_engine, chart_inputs, interval="1d", strict=True
    )

    chart_plan = get_chart_indicator_refresh_plan(
        pc_engine,
        chart_inputs,
        reference_symbol=REFERENCE_SYMBOL,
        history_watermarks=history_watermarks,
    )
    if chart_plan:
        refresh_chart_indicators_to_db(
            chart_inputs,
            pc_engine,
            reference_symbol=REFERENCE_SYMBOL,
            history_watermarks=history_watermarks,
            refresh_plan=chart_plan,
        )
        remaining = get_chart_indicator_refresh_plan(
            pc_engine,
            list(chart_plan),
            reference_symbol=REFERENCE_SYMBOL,
            history_watermarks=history_watermarks,
        )
        if remaining:
            raise RuntimeError(
                f"PC chart indicators remain incomplete for {len(remaining)} symbol(s)."
            )

    metric_symbols = [
        symbol for symbol in scanner_symbols if symbol != REFERENCE_SYMBOL
    ]
    if not metric_symbols:
        return
    metric_inputs = list(dict.fromkeys([REFERENCE_SYMBOL, *metric_symbols]))
    metric_watermarks = {
        symbol: history_watermarks.get(symbol)
        for symbol in metric_inputs
    }
    if any(value is None for value in metric_watermarks.values()):
        metric_watermarks = get_price_history_watermarks(
            pc_engine, metric_inputs, interval="1d", strict=True
        )
    if not is_scanner_metrics_snapshot_current(
        pc_engine,
        metric_symbols,
        history_watermarks=metric_watermarks,
        strict=True,
    ):
        refresh_scanner_metrics_to_db(
            metric_symbols,
            pc_engine,
            history_watermarks=metric_watermarks,
        )
    if not is_scanner_metrics_snapshot_current(
        pc_engine,
        metric_symbols,
        history_watermarks=metric_watermarks,
        strict=True,
    ):
        raise RuntimeError("PC scanner metrics remain incomplete after reconciliation.")


def reconcile_local_mirror_with_pc(
    pc_engine: Engine,
    local_engine: Engine,
    *,
    tickers: Optional[List[str]] = None,
    verify_derived: bool = True,
    tables: Tuple[Tuple[str, str], ...] = MIRRORED_TABLES,
) -> LocalMirrorReconciliationResult:
    """Converge market data before routing the dashboard from local to PC.

    The merge is intentionally asymmetric:

    * only missing, completed, validated raw daily/hourly bars may travel from
      local SQLite to PC MySQL;
    * those writes are insert-only, so an existing PC primary key always wins;
    * derived/operational tables never travel local -> PC;
    * PC-derived caches are rebuilt/verified, then the PC is mirrored back to
      the laptop and the raw per-symbol watermarks are verified both ways.

    Any error returns ``success=False``.  Callers must keep using the local
    engine and retry later rather than switching on a partial result.
    """
    requested_names = {table_name for table_name, _watermark in tables}
    reconcile_specs = [
        spec
        for spec in _RECONCILE_TABLE_SPECS
        if spec.table_name in requested_names
    ]
    unsupported = requested_names.difference(
        spec.table_name for spec in reconcile_specs
    )
    if unsupported:
        return LocalMirrorReconciliationResult(
            False,
            {},
            {},
            errors=(
                "No exact reconciliation policy for: "
                + ", ".join(sorted(unsupported)),
            ),
        )

    local_to_pc: Dict[str, int] = {}
    pc_to_local: Dict[str, int] = {}
    errors: List[str] = []
    affected_daily: Set[str] = set()
    affected_hourly: Set[str] = set()
    local_handoff_ready = False

    try:
        with pc_engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        with local_engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        _ensure_symbol_refresh_failures_table(pc_engine)
        _ensure_symbol_refresh_failures_table(local_engine)
        _ensure_local_mirror_handoff_tracking(local_engine)
        # Never let two empty/equal legacy tables bypass safety checks merely
        # because there are no mismatched partitions for a copy helper to
        # inspect.  Every requested table must have the exact key shape before
        # any local row is eligible to reach the PC or the UI can switch green.
        for spec in reconcile_specs:
            pc_table = Table(
                spec.table_name, MetaData(), autoload_with=pc_engine
            )
            local_table = Table(
                spec.table_name, MetaData(), autoload_with=local_engine
            )
            _validate_reconcile_table_schema(pc_table, local_table, spec)
    except Exception as exc:
        return LocalMirrorReconciliationResult(
            False, {}, {}, errors=(f"Database readiness check failed: {exc}",)
        )

    # First find exact raw-partition differences and promote only composite
    # primary keys that do not exist on the PC.  Existing PC rows are never
    # updated, regardless of local timestamps or values.
    for spec in (item for item in reconcile_specs if item.reverse_raw):
        try:
            pc_fingerprints = _partition_fingerprints(pc_engine, spec)
            local_fingerprints = _partition_fingerprints(local_engine, spec)
            mismatched = _mismatched_partitions(
                pc_fingerprints, local_fingerprints
            )
            count, symbols = _insert_missing_local_raw_partitions(
                pc_engine,
                local_engine,
                spec,
                mismatched,
                pc_fingerprints,
                local_fingerprints,
            )
            local_to_pc[spec.table_name] = count
            if spec.table_name == "price_history":
                affected_daily.update(symbols)
            else:
                affected_hourly.update(symbols)
        except Exception as exc:
            errors.append(f"{spec.table_name} local -> PC failed: {exc}")

    # Always verify causal derived caches before a successful adoption.  Raw
    # inserts commit in retry-safe chunks; if a later table failed during a
    # previous pass, the next pass may see zero newly affected symbols even
    # though the PC's chart/scanner artifacts still need rebuilding.
    raw_pc_before_derived: Dict[
        str, Dict[Tuple[object, ...], _PartitionFingerprint]
    ] = {}
    if not errors and verify_derived:
        try:
            # This is the first side of a write fence.  If the PC refresh
            # changes raw history while derived caches are being rebuilt, the
            # canonical snapshot below will differ and this attempt must not
            # activate PC routing.
            for spec in (item for item in reconcile_specs if item.reverse_raw):
                raw_pc_before_derived[spec.table_name] = _partition_fingerprints(
                    pc_engine, spec
                )
            _rebuild_and_verify_pc_derived_data(
                pc_engine, tickers, affected_daily
            )
        except Exception as exc:
            errors.append(f"PC derived-data verification failed: {exc}")

    # Canonicalize every changed partition from PC -> local.  This applies PC
    # values to conflicts, fills interior gaps, and removes local-only derived
    # or operational rows.  It is deliberately deferred until reverse raw
    # writes and any necessary derived rebuild have succeeded.  One SQLite
    # transaction publishes all tables together and reserves the local writer
    # lock through the final equality barrier.
    canonical_pc_before: Dict[
        str, Dict[Tuple[object, ...], _PartitionFingerprint]
    ] = {}
    if not errors:
        local_conn = None
        try:
            local_conn = local_engine.connect()
            if local_engine.dialect.name == "sqlite":
                local_conn.exec_driver_sql("BEGIN IMMEDIATE")
            else:
                local_conn.begin()

            # Capture every PC table before any canonical copy starts.  A
            # second full snapshot after the copy is the other side of the
            # fence; any concurrent PC writer makes this pass fail safely.
            for spec in reconcile_specs:
                canonical_pc_before[spec.table_name] = _partition_fingerprints(
                    pc_engine, spec
                )
            if verify_derived:
                for spec in (item for item in reconcile_specs if item.reverse_raw):
                    if _mismatched_partitions(
                        raw_pc_before_derived.get(spec.table_name, {}),
                        canonical_pc_before[spec.table_name],
                    ):
                        raise RuntimeError(
                            f"PC {spec.table_name} changed while derived data "
                            "was being verified"
                        )

            canonical_local_before = {
                spec.table_name: _partition_fingerprints(
                    local_engine, spec, connection=local_conn
                )
                for spec in reconcile_specs
            }
            pending_pc_to_local: Dict[str, int] = {}
            for spec in reconcile_specs:
                pc_fingerprints = canonical_pc_before[spec.table_name]
                local_fingerprints = canonical_local_before[spec.table_name]
                mismatched = _mismatched_partitions(
                    pc_fingerprints, local_fingerprints
                )
                try:
                    pending_pc_to_local[spec.table_name] = (
                        _copy_pc_partitions_to_local_exactly(
                            pc_engine,
                            local_engine,
                            spec,
                            mismatched,
                            pc_fingerprints,
                            local_fingerprints,
                            local_connection=local_conn,
                        )
                    )
                except Exception as exc:
                    raise RuntimeError(
                        f"{spec.table_name} PC -> local failed: {exc}"
                    ) from exc

            # Full all-table barrier: PC-before == local-after == PC-after.
            # This catches new partitions, edits to a formerly equal
            # partition, and a local refresh that started after the initial
            # guard.  A moving database is retried while the UI stays local.
            canonical_local_after = {
                spec.table_name: _partition_fingerprints(
                    local_engine, spec, connection=local_conn
                )
                for spec in reconcile_specs
            }
            canonical_pc_after = {
                spec.table_name: _partition_fingerprints(pc_engine, spec)
                for spec in reconcile_specs
            }
            unstable = []
            for spec in reconcile_specs:
                before = canonical_pc_before[spec.table_name]
                if _mismatched_partitions(
                    before, canonical_local_after[spec.table_name]
                ) or _mismatched_partitions(
                    before, canonical_pc_after[spec.table_name]
                ):
                    unstable.append(spec.table_name)
            if unstable:
                raise RuntimeError(
                    "database changed during reconciliation: "
                    + ", ".join(unstable)
                )

            if local_engine.dialect.name == "sqlite":
                _mark_local_mirror_handoff_clean(local_conn)
            local_conn.commit()
            pc_to_local = pending_pc_to_local
            local_handoff_ready = local_engine.dialect.name == "sqlite"
        except Exception as exc:
            if local_conn is not None:
                try:
                    local_conn.rollback()
                except Exception:
                    pass
            errors.append(f"Atomic PC/local equality barrier failed: {exc}")
        finally:
            if local_conn is not None:
                local_conn.close()

    return LocalMirrorReconciliationResult(
        success=not errors,
        local_to_pc_rows=local_to_pc,
        pc_to_local_rows=pc_to_local,
        affected_daily_symbols=tuple(sorted(affected_daily)),
        affected_hourly_symbols=tuple(sorted(affected_hourly)),
        errors=tuple(errors),
        local_handoff_ready=local_handoff_ready,
    )


def local_mirror_is_stale(
    engine: Engine,
    expected_date: dt.date,
    tickers: Optional[List[str]] = None,
) -> bool:
    """True when any actionable symbol's daily history is missing or behind.

    A table-wide ``MAX(date)`` is not sufficient: one current symbol could
    hide an interrupted mirror sync that omitted the same date for thousands
    of later symbols.  Match the daily-refresh gate by ignoring symbols that
    have already crossed the chronic-failure threshold, while SPY remains an
    always-actionable provider canary.

    ``tickers``, when given, restricts the check to that set (plus the
    reference symbol) -- normally the current scanner/refresh universe. A
    symbol dropped from the tracked universe (delisted, ticker change, no
    longer in the S&P 500/KIS list) stops being refreshed by
    ``historical.py`` and therefore never accumulates chronic-failure
    attempts either; without this filter its old, permanently-lagging
    ``price_history`` rows would flag the entire mirror stale forever even
    though every symbol still being maintained is current. Omitting
    ``tickers`` preserves the old table-wide behavior.
    """
    if isinstance(expected_date, dt.datetime):
        expected_date = expected_date.date()
    expected_timestamp = dt.datetime.combine(expected_date, dt.time.min)
    try:
        table = _ensure_price_history_table(engine)
        chronic = get_chronically_failing_symbols(engine, interval="1d")
        stmt = (
            select(
                table.c.symbol,
                func.max(table.c.date).label("latest_date"),
            )
            .where(table.c.interval == "1d")
            .group_by(table.c.symbol)
        )
        with engine.connect() as conn:
            rows = conn.execute(stmt).all()
    except (SQLAlchemyError, ValueError, TypeError):
        return True
    if not rows:
        return True

    latest_by_symbol = {
        str(row.symbol).upper(): row.latest_date
        for row in rows
        if row.latest_date is not None
    }
    reference_latest = latest_by_symbol.get(REFERENCE_SYMBOL)
    if reference_latest is None or reference_latest < expected_timestamp:
        return True

    if tickers is not None:
        tracked = {
            str(symbol).strip().upper()
            for symbol in tickers
            if symbol is not None and str(symbol).strip()
        }
        tracked.discard(REFERENCE_SYMBOL)
        return any(
            symbol not in chronic
            and (
                symbol not in latest_by_symbol
                or latest_by_symbol[symbol] < expected_timestamp
            )
            for symbol in tracked
        )

    return any(
        latest < expected_timestamp and symbol not in chronic
        for symbol, latest in latest_by_symbol.items()
        if symbol != REFERENCE_SYMBOL
    )


def _get_price_history_table(metadata: MetaData) -> Table:
    return Table(
        "price_history",
        metadata,
        Column("symbol", String(20), primary_key=True),
        Column("date", DateTime, primary_key=True),
        Column("interval", String(10), primary_key=True, default="1d"),
        Column("open", Float),
        Column("high", Float),
        Column("low", Float),
        Column("close", Float),
        Column("adj_close", Float),
        Column("volume", Float),
        Column("updated_at", DateTime, default=_utcnow_naive, nullable=False),
    )


def _ensure_price_history_table(engine: Engine) -> Table:
    engine_key = id(engine)
    metadata = MetaData()
    price_history = _get_price_history_table(metadata)
    if engine_key not in _ensured_engines:
        metadata.create_all(engine)
        _ensure_price_history_interval_column(engine)
        _ensured_engines.add(engine_key)
    return price_history


def _ensure_price_history_interval_column(engine: Engine) -> None:
    """Migrate older daily-only price_history tables to interval-aware storage."""
    try:
        inspector = inspect(engine)
        if not inspector.has_table("price_history"):
            return
        columns = {column["name"] for column in inspector.get_columns("price_history")}
        if "interval" in columns:
            return
        if engine.dialect.name == "mysql":
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE price_history ADD COLUMN `interval` VARCHAR(10) NOT NULL DEFAULT '1d' AFTER `date`"))
                conn.execute(text("ALTER TABLE price_history DROP PRIMARY KEY, ADD PRIMARY KEY (`symbol`, `date`, `interval`)"))
        else:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE price_history ADD COLUMN interval VARCHAR(10) NOT NULL DEFAULT '1d'"))
    except SQLAlchemyError:
        return


def _get_hourly_price_history_table(metadata: MetaData) -> Table:
    return Table(
        "hourly_price_history",
        metadata,
        Column("symbol", String(20), primary_key=True),
        Column("timestamp", DateTime, primary_key=True),
        Column("source", String(20), primary_key=True, default="yfinance"),
        Column("open", Float),
        Column("high", Float),
        Column("low", Float),
        Column("close", Float),
        Column("adj_close", Float),
        Column("volume", Float),
        Column("updated_at", DateTime, default=_utcnow_naive, nullable=False),
    )


def _ensure_hourly_price_history_table(engine: Engine) -> Table:
    metadata = MetaData()
    hourly_history = _get_hourly_price_history_table(metadata)
    metadata.create_all(engine)
    return hourly_history


def _get_symbol_refresh_failures_table(metadata: MetaData) -> Table:
    return Table(
        "symbol_refresh_failures",
        metadata,
        Column("symbol", String(20), primary_key=True),
        Column("interval", String(10), primary_key=True),
        Column("consecutive_failures", Integer, nullable=False, default=0),
        Column("last_attempt_at", DateTime, nullable=False, default=_utcnow_naive),
    )


def _ensure_symbol_refresh_failures_table(engine: Engine) -> Table:
    metadata = MetaData()
    table = _get_symbol_refresh_failures_table(metadata)
    metadata.create_all(engine)
    return table


# Consecutive refresh runs that leave a symbol stale before it stops gating
# scripts/run_daily_refresh.py's staleness check. A handful of tickers
# (delisted symbols, preferred-share classes yfinance doesn't carry, etc.)
# can remain stale forever. Without this, one such symbol makes the daily
# "is anything stale?" check return
# true on every PC restart, re-running the full 5000+-symbol refresh each
# time even though every fetchable symbol is already current.
# Existing installations may already have three failures recorded from the
# old nonempty-is-success fetch logic. Four gives those symbols one recovery
# run through the freshness-aware chart fallback before they are quarantined.
CHRONIC_FAILURE_THRESHOLD = 4


def record_symbol_refresh_outcomes(
    engine: Engine,
    interval: str,
    succeeded: List[str],
    failed: List[str],
) -> None:
    """Persist whether each symbol's cache was current after a refresh run.

    A current cache resets the streak to 0 (so a symbol that starts working
    again stops being excluded). A stale cache increments it. See
    get_chronically_failing_symbols for how the streak is consumed.
    """
    succeeded_symbols = _clean_symbols(succeeded)
    succeeded_set = set(succeeded_symbols)
    failed_symbols = [s for s in _clean_symbols(failed) if s not in succeeded_set]
    if not succeeded_symbols and not failed_symbols:
        return

    interval = interval.strip().lower()
    now = _utcnow_naive()

    try:
        table = _ensure_symbol_refresh_failures_table(engine)
        dialect_name = engine.dialect.name
        with engine.begin() as conn:
            if succeeded_symbols:
                reset_records = [
                    {"symbol": symbol, "interval": interval, "consecutive_failures": 0, "last_attempt_at": now}
                    for symbol in succeeded_symbols
                ]
                _execute_bulk_upsert(conn, table, reset_records, ("symbol", "interval"), dialect_name)
            if failed_symbols:
                fail_records = [
                    {"symbol": symbol, "interval": interval, "consecutive_failures": 1, "last_attempt_at": now}
                    for symbol in failed_symbols
                ]
                if dialect_name == "mysql":
                    stmt = mysql_insert(table).values(fail_records)
                    conn.execute(
                        stmt.on_duplicate_key_update(
                            consecutive_failures=table.c.consecutive_failures + 1,
                            last_attempt_at=stmt.inserted.last_attempt_at,
                        )
                    )
                elif dialect_name == "sqlite":
                    stmt = sqlite_insert(table).values(fail_records)
                    conn.execute(
                        stmt.on_conflict_do_update(
                            index_elements=["symbol", "interval"],
                            set_={
                                "consecutive_failures": table.c.consecutive_failures + 1,
                                "last_attempt_at": stmt.excluded.last_attempt_at,
                            },
                        )
                    )
                else:
                    conn.execute(insert(table), fail_records)
    except SQLAlchemyError:
        # Best-effort bookkeeping only -- never let this fail the refresh run.
        return


def get_chronically_failing_symbols(
    engine: Engine, interval: str, threshold: int = CHRONIC_FAILURE_THRESHOLD
) -> Set[str]:
    """Symbols left stale by `threshold`+ consecutive refresh runs."""
    interval = interval.strip().lower()
    try:
        table = _ensure_symbol_refresh_failures_table(engine)
        stmt = select(table.c.symbol).where(
            table.c.interval == interval, table.c.consecutive_failures >= threshold
        )
        with engine.connect() as conn:
            rows = conn.execute(stmt).all()
    except SQLAlchemyError:
        return set()
    return {str(row.symbol).upper() for row in rows}


def _get_chart_indicators_table(metadata: MetaData) -> Table:
    return Table(
        "chart_indicators",
        metadata,
        Column("symbol", String(20), primary_key=True),
        Column("date", DateTime, primary_key=True),
        Column("relative_strength", Float),
        Column("rs_sma_50", Float),
        Column("rs_score_current", Float),
        Column("rs_score_yesterday", Float),
        Column("rs_score_week", Float),
        Column("rs_score_month", Float),
        Column("pct_change_today", Float),
        Column("avg_7", Float),
        Column("avg_65", Float),
        Column("ti65", Float),
        Column("is_ti65_bullish", Boolean),
        Column("is_ti65_bearish", Boolean),
        Column("is_9m_volume", Boolean),
        Column("is_plus_4pct_change", Boolean),
        Column("is_minus_4pct_change", Boolean),
        Column("is_rs_cross_up", Boolean),
        Column("updated_at", DateTime, default=_utcnow_naive, nullable=False),
    )


def _ensure_chart_indicators_table(engine: Engine) -> Table:
    metadata = MetaData()
    chart_indicators = _get_chart_indicators_table(metadata)
    metadata.create_all(engine)
    return chart_indicators


def _get_chart_indicator_manifests_table(metadata: MetaData) -> Table:
    return Table(
        "chart_indicator_manifests",
        metadata,
        Column("symbol", String(20), primary_key=True),
        Column("reference_symbol", String(20), nullable=False),
        Column("source_latest_date", DateTime, nullable=False),
        Column("source_row_count", Integer, nullable=False),
        Column("reference_latest_date", DateTime, nullable=False),
        Column("reference_row_count", Integer, nullable=False),
        Column("cache_version", Integer, nullable=False),
        Column("completed_at", DateTime, default=_utcnow_naive, nullable=False),
    )


def _ensure_chart_indicator_manifests_table(engine: Engine) -> Table:
    metadata = MetaData()
    table = _get_chart_indicator_manifests_table(metadata)
    metadata.create_all(engine)
    return table


def _get_intraday_price_history_table(metadata: MetaData) -> Table:
    return Table(
        "intraday_price_history",
        metadata,
        Column("symbol", String(20), primary_key=True),
        Column("timestamp", DateTime, primary_key=True),
        Column("interval", String(10), primary_key=True),
        Column("source", String(20), primary_key=True),
        Column("open", Float),
        Column("high", Float),
        Column("low", Float),
        Column("close", Float),
        Column("volume", Float),
        Column("updated_at", DateTime, default=_utcnow_naive, nullable=False),
    )


def _ensure_intraday_price_history_table(engine: Engine) -> Table:
    metadata = MetaData()
    table = _get_intraday_price_history_table(metadata)
    metadata.create_all(engine)
    return table


def _get_scanner_metrics_table(metadata: MetaData) -> Table:
    return Table(
        "scanner_metrics",
        metadata,
        Column("symbol", String(20), primary_key=True),
        Column("date", DateTime, primary_key=True),
        Column("price", Float),
        Column("volume", Float),
        Column("avg_volume_20d", Float),
        Column("dollar_volume", Float),
        Column("avg_dollar_volume_20d", Float),
        Column("price_history_days", Integer),
        Column("adr", Float),
        Column("adr_20", Float),
        Column("atr_14_pct", Float),
        Column("range_today_pct", Float),
        Column("return_1w", Float),
        Column("return_1m", Float),
        Column("return_3m", Float),
        Column("return_6m", Float),
        Column("growth_rank", Float),
        Column("growth_rank_1m", Float),
        Column("growth_rank_3m", Float),
        Column("sma_20", Float),
        Column("ema_50", Float),
        Column("sma_200", Float),
        Column("above_sma_20", Boolean),
        Column("above_ema_50", Boolean),
        Column("ma_alignment", Boolean),
        Column("distance_from_20ma_pct", Float),
        Column("distance_from_50ema_pct", Float),
        Column("trend_intensity", Float),
        Column("trend_score", Float),
        Column("relative_volume", Float),
        Column("volume_expansion", Float),
        Column("volume_dryup_ratio", Float),
        Column("high_20d", Float),
        Column("high_50d", Float),
        Column("high_252d", Float),
        Column("close_to_52w_high_pct", Float),
        Column("distance_to_20d_high_pct", Float),
        Column("breakout_20d", Boolean),
        Column("breakout_50d", Boolean),
        Column("consolidation_range_10d_pct", Float),
        Column("consolidation_tightness", Float),
        Column("pullback_depth_pct", Float),
        Column("extension_10ma_pct", Float),
        Column("extension_20ma_pct", Float),
        Column("extension_50ema_pct", Float),
        Column("return_3d", Float),
        Column("return_5d", Float),
        Column("consecutive_up_days", Integer),
        Column("parabolic_flag", Boolean),
        Column("rs_score_252", Float),
        Column("rs_above_sma_50", Boolean),
        Column("rs_slope_20d", Float),
        Column("score", Float),
        Column("updated_at", DateTime, default=_utcnow_naive, nullable=False),
    )


def _ensure_scanner_metrics_table(engine: Engine) -> Table:
    metadata = MetaData()
    table = _get_scanner_metrics_table(metadata)
    metadata.create_all(engine)
    return table


def _get_scanner_metric_snapshots_table(metadata: MetaData) -> Table:
    return Table(
        "scanner_metric_snapshots",
        metadata,
        Column("snapshot_date", DateTime, primary_key=True),
        Column("input_fingerprint", String(64), nullable=False),
        Column("metric_count", Integer, nullable=False),
        Column("completed_at", DateTime, nullable=False, default=_utcnow_naive),
    )


def _ensure_scanner_metric_snapshots_table(engine: Engine) -> Table:
    metadata = MetaData()
    table = _get_scanner_metric_snapshots_table(metadata)
    metadata.create_all(engine)
    return table


def _normalize_timestamp(ts: pd.Timestamp) -> dt.datetime:
    if ts.tzinfo is not None:
        ts = ts.tz_convert("UTC")
    return ts.tz_localize(None).to_pydatetime()


def _clean_symbols(symbols: List[str]) -> List[str]:
    cleaned = [str(symbol).strip().upper() for symbol in symbols if str(symbol).strip()]
    return list(dict.fromkeys(cleaned))


def _float_or_none(value) -> Optional[float]:
    if pd.isna(value):
        return None
    return float(value)


def _record_chunks(records: List[dict], chunk_size: int) -> List[List[dict]]:
    size = max(1, int(chunk_size or 1))
    return [records[index:index + size] for index in range(0, len(records), size)]


def _execute_bulk_upsert(
    conn,
    table: Table,
    records: List[dict],
    key_columns: Tuple[str, ...],
    dialect_name: str,
) -> int:
    if not records:
        return 0

    chunk_size = 5000 if dialect_name == "mysql" else 500
    rows_written = 0
    for chunk in _record_chunks(records, chunk_size):
        if dialect_name == "mysql":
            stmt = mysql_insert(table).values(chunk)
            update_cols = {
                col.name: stmt.inserted[col.name]
                for col in table.columns
                if col.name not in key_columns
            }
            conn.execute(stmt.on_duplicate_key_update(**update_cols))
        elif dialect_name == "sqlite":
            stmt = sqlite_insert(table).values(chunk)
            update_cols = {
                col.name: getattr(stmt.excluded, col.name)
                for col in table.columns
                if col.name not in key_columns
            }
            conn.execute(stmt.on_conflict_do_update(index_elements=list(key_columns), set_=update_cols))
        else:
            conn.execute(insert(table), chunk)
        rows_written += len(chunk)
    return rows_written


def _price_history_records_from_batch(
    batch_history: pd.DataFrame,
    symbols: List[str],
    interval: str = "1d",
) -> Tuple[List[dict], Dict[str, int]]:
    if batch_history.empty:
        return [], {}

    records = []
    counts: Dict[str, int] = {}
    updated_at = _utcnow_naive()
    normalized_interval = interval.strip().lower() or "1d"

    for symbol in _clean_symbols(symbols):
        symbol_df = _extract_symbol_history(batch_history, symbol)
        if symbol_df is None or symbol_df.empty:
            continue

        symbol_count = 0
        for timestamp, row in symbol_df.iterrows():
            try:
                record = {
                    "symbol": symbol,
                    "date": _normalize_timestamp(pd.Timestamp(timestamp)),
                    "interval": normalized_interval,
                    "open": _float_or_none(row.get("Open")),
                    "high": _float_or_none(row.get("High")),
                    "low": _float_or_none(row.get("Low")),
                    "close": _float_or_none(row.get("Close")),
                    "adj_close": _float_or_none(row.get("Adj Close", row.get("Close"))),
                    "volume": _float_or_none(row.get("Volume")),
                    "updated_at": updated_at,
                }
            except (TypeError, ValueError):
                continue

            if record["date"] is None or record["close"] is None:
                continue
            records.append(record)
            symbol_count += 1

        if symbol_count:
            counts[symbol] = symbol_count

    return records, counts


def _hourly_history_records_from_batch(
    batch_history: pd.DataFrame,
    symbols: List[str],
    source: str = "yfinance",
) -> Tuple[List[dict], Dict[str, int]]:
    if batch_history.empty:
        return [], {}

    records = []
    counts: Dict[str, int] = {}
    updated_at = _utcnow_naive()

    for symbol in _clean_symbols(symbols):
        symbol_df = _extract_symbol_history(batch_history, symbol)
        if symbol_df is None or symbol_df.empty:
            continue

        symbol_count = 0
        for timestamp, row in symbol_df.iterrows():
            try:
                record = {
                    "symbol": symbol,
                    "timestamp": _normalize_timestamp(pd.Timestamp(timestamp)),
                    "source": source,
                    "open": _float_or_none(row.get("Open")),
                    "high": _float_or_none(row.get("High")),
                    "low": _float_or_none(row.get("Low")),
                    "close": _float_or_none(row.get("Close")),
                    "adj_close": _float_or_none(row.get("Adj Close", row.get("Close"))),
                    "volume": _float_or_none(row.get("Volume")),
                    "updated_at": updated_at,
                }
            except (TypeError, ValueError):
                continue

            if record["timestamp"] is None or record["close"] is None:
                continue
            records.append(record)
            symbol_count += 1

        if symbol_count:
            counts[symbol] = symbol_count

    return records, counts


def save_symbol_history_to_db(symbol: str, history: pd.DataFrame, engine: Engine, interval: str = "1d") -> bool:
    if history.empty:
        return False

    records, _counts = _price_history_records_from_batch(history, [symbol], interval=interval)
    if not records:
        return False

    metadata = MetaData()
    price_history = _get_price_history_table(metadata)
    _ensure_price_history_table(engine)

    try:
        with engine.begin() as conn:
            _execute_bulk_upsert(
                conn,
                price_history,
                records,
                ("symbol", "date", "interval"),
                engine.dialect.name,
            )
        return True
    except SQLAlchemyError:
        return False


def save_universe_history_batch_to_db(
    batch_history: pd.DataFrame,
    symbols: List[str],
    engine: Engine,
    interval: str = "1d",
) -> int:
    """Bulk upsert a yfinance batch dataframe into price_history.

    Returns the number of OHLCV rows submitted for insert/update.
    """
    records, _counts = _price_history_records_from_batch(batch_history, symbols, interval=interval)
    if not records:
        return 0

    metadata = MetaData()
    price_history = _get_price_history_table(metadata)
    _ensure_price_history_table(engine)

    try:
        with engine.begin() as conn:
            return _execute_bulk_upsert(
                conn,
                price_history,
                records,
                ("symbol", "date", "interval"),
                engine.dialect.name,
            )
    except SQLAlchemyError:
        return 0


def save_hourly_history_to_db(
    symbol: str,
    history: pd.DataFrame,
    engine: Engine,
    source: str = "yfinance",
) -> bool:
    if history.empty:
        return False

    records, _counts = _hourly_history_records_from_batch(history, [symbol], source=source)
    if not records:
        return False

    metadata = MetaData()
    hourly_history = _get_hourly_price_history_table(metadata)
    _ensure_hourly_price_history_table(engine)

    try:
        with engine.begin() as conn:
            _execute_bulk_upsert(
                conn,
                hourly_history,
                records,
                ("symbol", "timestamp", "source"),
                engine.dialect.name,
            )
        return True
    except SQLAlchemyError:
        return False


def save_universe_hourly_history_batch_to_db(
    batch_history: pd.DataFrame,
    symbols: List[str],
    engine: Engine,
    source: str = "yfinance",
) -> int:
    """Bulk upsert yfinance 1-hour batch data into hourly_price_history."""
    records, _counts = _hourly_history_records_from_batch(batch_history, symbols, source=source)
    if not records:
        return 0

    metadata = MetaData()
    hourly_history = _get_hourly_price_history_table(metadata)
    _ensure_hourly_price_history_table(engine)

    try:
        with engine.begin() as conn:
            return _execute_bulk_upsert(
                conn,
                hourly_history,
                records,
                ("symbol", "timestamp", "source"),
                engine.dialect.name,
            )
    except SQLAlchemyError:
        return 0


def load_hourly_history_from_db(
    symbol: str,
    engine: Engine,
    start: Optional[dt.datetime] = None,
    end: Optional[dt.datetime] = None,
    source: Optional[str] = None,
) -> pd.DataFrame:
    metadata = MetaData()
    hourly_history = _get_hourly_price_history_table(metadata)
    stmt = select(hourly_history).where(hourly_history.c.symbol == symbol.strip().upper())
    if source:
        stmt = stmt.where(hourly_history.c.source == source)
    if start is not None:
        stmt = stmt.where(hourly_history.c.timestamp >= start)
    if end is not None:
        stmt = stmt.where(hourly_history.c.timestamp <= end)
    stmt = stmt.order_by(hourly_history.c.timestamp)

    try:
        _ensure_hourly_price_history_table(engine)
        with engine.connect() as conn:
            rows = conn.execute(stmt).all()
    except SQLAlchemyError:
        return pd.DataFrame()

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows, columns=rows[0]._mapping.keys())
    df["timestamp"] = pd.to_datetime(df["timestamp"]).dt.tz_localize("UTC")
    df = df.set_index("timestamp").sort_index()
    df = df[["open", "high", "low", "close", "adj_close", "volume"]]
    df.columns = ["Open", "High", "Low", "Close", "Adj Close", "Volume"]
    return df


def get_latest_hourly_price_history_timestamp(
    engine: Engine,
    symbol: Optional[str] = None,
    source: Optional[str] = None,
) -> Optional[dt.datetime]:
    metadata = MetaData()
    hourly_history = _get_hourly_price_history_table(metadata)
    stmt = select(func.max(hourly_history.c.timestamp))
    if symbol:
        stmt = stmt.where(hourly_history.c.symbol == symbol.strip().upper())
    if source:
        stmt = stmt.where(hourly_history.c.source == source)

    try:
        _ensure_hourly_price_history_table(engine)
        with engine.connect() as conn:
            latest_timestamp = conn.execute(stmt).scalar_one_or_none()
    except SQLAlchemyError:
        return None

    return latest_timestamp


def save_intraday_history_to_db(
    symbol: str,
    history: pd.DataFrame,
    engine: Engine,
    interval: str = "1m",
    source: str = "yfinance",
) -> bool:
    if history.empty:
        return False

    metadata = MetaData()
    intraday_history = _get_intraday_price_history_table(metadata)
    _ensure_intraday_price_history_table(engine)

    records = []
    for timestamp, row in history.iterrows():
        ts = pd.Timestamp(timestamp)
        records.append({
            "symbol": symbol.upper(),
            "timestamp": _normalize_timestamp(ts),
            "interval": interval,
            "source": source,
            "open": float(row.get("Open", row.get("Close", 0.0)) or 0.0),
            "high": float(row.get("High", row.get("Close", 0.0)) or 0.0),
            "low": float(row.get("Low", row.get("Close", 0.0)) or 0.0),
            "close": float(row.get("Close", 0.0) or 0.0),
            "volume": float(row.get("Volume", 0.0) or 0.0),
            "updated_at": _utcnow_naive(),
        })

    if not records:
        return False

    with engine.begin() as conn:
        if engine.dialect.name == "mysql":
            stmt = mysql_insert(intraday_history).values(records)
            update_cols = {
                col.name: stmt.inserted[col.name]
                for col in intraday_history.columns
                if col.name not in ("symbol", "timestamp", "interval", "source")
            }
            conn.execute(stmt.on_duplicate_key_update(**update_cols))
        else:
            stmt = sqlite_insert(intraday_history).values(records)
            update_cols = {
                col.name: stmt.excluded[col.name]
                for col in intraday_history.columns
                if col.name not in ("symbol", "timestamp", "interval", "source")
            }
            conn.execute(stmt.on_conflict_do_update(
                index_elements=["symbol", "timestamp", "interval", "source"],
                set_=update_cols,
            ))
    return True


def load_intraday_history_from_db(
    symbol: str,
    engine: Engine,
    interval: str = "1m",
    source: Optional[str] = None,
    since: Optional[dt.datetime] = None,
) -> pd.DataFrame:
    metadata = MetaData()
    intraday_history = _get_intraday_price_history_table(metadata)

    stmt = select(intraday_history).where(
        intraday_history.c.symbol == symbol.upper(),
        intraday_history.c.interval == interval,
    )
    if source:
        stmt = stmt.where(intraday_history.c.source == source)
    if since is not None:
        stmt = stmt.where(intraday_history.c.timestamp >= since)
    stmt = stmt.order_by(intraday_history.c.timestamp)

    try:
        _ensure_intraday_price_history_table(engine)
        with engine.connect() as conn:
            df = pd.read_sql(stmt, conn)
    except SQLAlchemyError:
        return pd.DataFrame()

    if df.empty:
        return pd.DataFrame()

    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df = df.set_index("timestamp")
    return df.rename(
        columns={
            "open": "Open",
            "high": "High",
            "low": "Low",
            "close": "Close",
            "volume": "Volume",
        }
    )[["Open", "High", "Low", "Close", "Volume"]]


def prune_intraday_history(engine: Engine, keep_days: int = 7) -> int:
    metadata = MetaData()
    intraday_history = _get_intraday_price_history_table(metadata)
    _ensure_intraday_price_history_table(engine)
    cutoff = _utcnow_naive() - dt.timedelta(days=keep_days)
    stmt = delete(intraday_history).where(intraday_history.c.timestamp < cutoff)
    with engine.begin() as conn:
        result = conn.execute(stmt)
    return int(result.rowcount or 0)


def delete_intraday_history_for_symbol(engine: Engine, symbol: str) -> int:
    metadata = MetaData()
    intraday_history = _get_intraday_price_history_table(metadata)
    _ensure_intraday_price_history_table(engine)
    stmt = delete(intraday_history).where(intraday_history.c.symbol == symbol.upper())
    with engine.begin() as conn:
        result = conn.execute(stmt)
    return int(result.rowcount or 0)


def load_symbol_history_from_db(
    symbol: str,
    engine: Engine,
    start: Optional[dt.datetime] = None,
    end: Optional[dt.datetime] = None,
    interval: str = "1d",
) -> pd.DataFrame:
    metadata = MetaData()
    price_history = _get_price_history_table(metadata)
    stmt = select(price_history).where(
        price_history.c.symbol == symbol.strip().upper(),
        price_history.c.interval == interval.strip().lower(),
    )
    if start is not None:
        stmt = stmt.where(price_history.c.date >= start)
    if end is not None:
        stmt = stmt.where(price_history.c.date <= end)
    stmt = stmt.order_by(price_history.c.date)

    try:
        _ensure_price_history_table(engine)
        with engine.connect() as conn:
            rows = conn.execute(stmt).all()
    except SQLAlchemyError:
        return pd.DataFrame()

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows, columns=rows[0]._mapping.keys())
    df["date"] = pd.to_datetime(df["date"]).dt.tz_localize("UTC")
    df = df.set_index("date").sort_index()
    df = df[["open", "high", "low", "close", "adj_close", "volume"]]
    df.columns = ["Open", "High", "Low", "Close", "Adj Close", "Volume"]
    return df


def load_universe_history_from_db(
    tickers: List[str],
    engine: Engine,
    start: Optional[dt.datetime] = None,
    end: Optional[dt.datetime] = None,
    interval: str = "1d",
) -> dict[str, pd.DataFrame]:
    metadata = MetaData()
    price_history = _get_price_history_table(metadata)
    symbols = _clean_symbols(tickers)
    if not symbols:
        return {}

    rows = []
    try:
        _ensure_price_history_table(engine)
        with engine.connect() as conn:
            for chunk in _record_chunks(symbols, CACHE_QUERY_SYMBOL_CHUNK_SIZE):
                stmt = select(price_history).where(
                    price_history.c.symbol.in_(chunk),
                    price_history.c.interval == interval.strip().lower(),
                )
                if start is not None:
                    stmt = stmt.where(price_history.c.date >= start)
                if end is not None:
                    stmt = stmt.where(price_history.c.date <= end)
                stmt = stmt.order_by(price_history.c.symbol, price_history.c.date)
                rows.extend(conn.execute(stmt).all())
    except SQLAlchemyError:
        return {}

    if not rows:
        return {}

    df = pd.DataFrame(rows, columns=rows[0]._mapping.keys())
    df["date"] = pd.to_datetime(df["date"]).dt.tz_localize("UTC")
    df = df.set_index("date").sort_index()
    df = df[["symbol", "open", "high", "low", "close", "adj_close", "volume"]]

    result: dict[str, pd.DataFrame] = {}
    for symbol, group in df.groupby("symbol"):
        symbol_df = group.drop(columns=["symbol"]).copy()
        symbol_df.columns = ["Open", "High", "Low", "Close", "Adj Close", "Volume"]
        result[symbol] = symbol_df

    return result


def _rolling_percent_rank(series: pd.Series, lookback: int) -> pd.Series:
    def rank_last(window) -> float:
        values = pd.Series(window).dropna()
        if values.empty:
            return float("nan")
        return float(values.rank(pct=True, method="max").iloc[-1] * 100)

    return series.rolling(lookback, min_periods=1).apply(rank_last, raw=False)


def calculate_chart_indicators(
    symbol: str,
    history: pd.DataFrame,
    spy_history: pd.DataFrame,
    rs_sma_period: int = 50,
    rs_score_lookback: int = 252,
) -> pd.DataFrame:
    """Calculate RS vs SPY, RS score, TI65, and marker fields for chart rendering."""
    if history.empty or spy_history.empty:
        return pd.DataFrame()

    symbol = symbol.strip().upper()
    symbol_history = history.copy()
    spy = spy_history.copy()
    symbol_history.index = pd.to_datetime(symbol_history.index).tz_localize(None)
    spy.index = pd.to_datetime(spy.index).tz_localize(None)

    df = symbol_history[["Close", "Volume"]].rename(columns={"Close": "close", "Volume": "volume"})
    df["spy_close"] = spy["Close"].astype(float)
    df = df.dropna(subset=["close", "spy_close"]).sort_index()
    if df.empty:
        return pd.DataFrame()

    close = df["close"].astype(float)
    volume = df["volume"].fillna(0).astype(float)
    relative_strength = close / df["spy_close"].replace(0, pd.NA).astype(float)
    rs_sma_50 = relative_strength.rolling(rs_sma_period, min_periods=1).mean()
    rs_score_current = _rolling_percent_rank(relative_strength, rs_score_lookback)
    pct_change_today = close.pct_change() * 100.0
    avg_7 = close.rolling(7, min_periods=1).mean()
    avg_65 = close.rolling(65, min_periods=1).mean()
    ti65 = avg_7 / avg_65.replace(0, pd.NA)

    indicators = pd.DataFrame(
        {
            "symbol": symbol,
            "date": df.index,
            "relative_strength": relative_strength,
            "rs_sma_50": rs_sma_50,
            "rs_score_current": rs_score_current,
            "rs_score_yesterday": rs_score_current.shift(1),
            "rs_score_week": rs_score_current.shift(5),
            "rs_score_month": rs_score_current.shift(21),
            "pct_change_today": pct_change_today,
            "avg_7": avg_7,
            "avg_65": avg_65,
            "ti65": ti65,
            "is_ti65_bullish": ti65 >= 1.05,
            "is_ti65_bearish": ti65 <= 0.95,
            "is_9m_volume": volume >= 9000000,
            "is_plus_4pct_change": pct_change_today >= 4.0,
            "is_minus_4pct_change": pct_change_today <= -4.0,
            "is_rs_cross_up": (relative_strength > rs_sma_50) & (relative_strength.shift(1) <= rs_sma_50.shift(1)),
        }
    )
    indicators["updated_at"] = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    return indicators


def calculate_chart_indicators_since(
    symbol: str,
    history: pd.DataFrame,
    spy_history: pd.DataFrame,
    start_date: dt.datetime,
    rs_sma_period: int = 50,
    rs_score_lookback: int = 252,
) -> pd.DataFrame:
    """Calculate only indicator rows at or after ``start_date``.

    Rolling outputs before the requested date are dependencies, not outputs.
    Computing the handful of required windows directly avoids repeating the
    expensive rolling-percent-rank calculation for every already-persisted
    row on each new market session.
    """
    if history.empty or spy_history.empty:
        return pd.DataFrame()

    symbol_history = history.copy()
    spy = spy_history.copy()
    symbol_history.index = pd.to_datetime(symbol_history.index).tz_localize(None)
    spy.index = pd.to_datetime(spy.index).tz_localize(None)
    df = symbol_history[["Close", "Volume"]].rename(
        columns={"Close": "close", "Volume": "volume"}
    )
    df["spy_close"] = spy["Close"].astype(float)
    df = df.dropna(subset=["close", "spy_close"]).sort_index()
    if df.empty:
        return pd.DataFrame()

    first_output = pd.Timestamp(start_date)
    if first_output.tzinfo is not None:
        first_output = first_output.tz_localize(None)
    target_positions = np.flatnonzero(df.index >= first_output)
    if len(target_positions) == 0:
        return pd.DataFrame()

    close = df["close"].astype(float).to_numpy()
    volume = df["volume"].fillna(0).astype(float).to_numpy()
    spy_close = df["spy_close"].astype(float).to_numpy()
    with np.errstate(divide="ignore", invalid="ignore"):
        relative_strength = np.where(spy_close != 0, close / spy_close, np.nan)

    def window_mean(values: np.ndarray, position: int, size: int) -> float:
        window = values[max(0, position - size + 1):position + 1]
        valid = window[~np.isnan(window)]
        return float(valid.mean()) if len(valid) else float("nan")

    def rs_score(position: int) -> float:
        if position < 0 or np.isnan(relative_strength[position]):
            return float("nan")
        window = relative_strength[
            max(0, position - rs_score_lookback + 1):position + 1
        ]
        valid = window[~np.isnan(window)]
        if len(valid) == 0:
            return float("nan")
        return float(np.count_nonzero(valid <= relative_strength[position]) / len(valid) * 100.0)

    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    rows = []
    for position in target_positions:
        position = int(position)
        current_rs = float(relative_strength[position])
        rs_sma_50 = window_mean(relative_strength, position, rs_sma_period)
        avg_7 = window_mean(close, position, 7)
        avg_65 = window_mean(close, position, 65)
        ti65 = avg_7 / avg_65 if avg_65 != 0 else float("nan")
        if position > 0:
            with np.errstate(divide="ignore", invalid="ignore"):
                pct_change_today = float((close[position] / close[position - 1] - 1.0) * 100.0)
            previous_rs = float(relative_strength[position - 1])
            previous_rs_sma = window_mean(relative_strength, position - 1, rs_sma_period)
        else:
            pct_change_today = float("nan")
            previous_rs = float("nan")
            previous_rs_sma = float("nan")

        rows.append(
            {
                "symbol": symbol.strip().upper(),
                "date": df.index[position],
                "relative_strength": current_rs,
                "rs_sma_50": rs_sma_50,
                "rs_score_current": rs_score(position),
                "rs_score_yesterday": rs_score(position - 1),
                "rs_score_week": rs_score(position - 5),
                "rs_score_month": rs_score(position - 21),
                "pct_change_today": pct_change_today,
                "avg_7": avg_7,
                "avg_65": avg_65,
                "ti65": ti65,
                "is_ti65_bullish": bool(ti65 >= 1.05),
                "is_ti65_bearish": bool(ti65 <= 0.95),
                "is_9m_volume": bool(volume[position] >= 9000000),
                "is_plus_4pct_change": bool(pct_change_today >= 4.0),
                "is_minus_4pct_change": bool(pct_change_today <= -4.0),
                "is_rs_cross_up": bool(
                    current_rs > rs_sma_50 and previous_rs <= previous_rs_sma
                ),
                "updated_at": now,
            }
        )

    return pd.DataFrame.from_records(rows)


def save_chart_indicators_to_db(symbol: str, indicators: pd.DataFrame, engine: Engine) -> bool:
    if indicators.empty:
        return False

    metadata = MetaData()
    chart_indicators = _get_chart_indicators_table(metadata)
    manifest_table = _get_chart_indicator_manifests_table(metadata)
    _ensure_chart_indicators_table(engine)
    _ensure_chart_indicator_manifests_table(engine)
    records = _chart_indicator_records(indicators, chart_indicators)
    if not records:
        return False

    try:
        with engine.begin() as conn:
            _invalidate_chart_indicator_manifests(
                conn, manifest_table, [symbol]
            )
            if engine.dialect.name not in ("mysql", "sqlite"):
                conn.execute(delete(chart_indicators).where(chart_indicators.c.symbol == symbol.strip().upper()))
            _execute_bulk_upsert(
                conn,
                chart_indicators,
                records,
                ("symbol", "date"),
                engine.dialect.name,
            )
        return True
    except SQLAlchemyError:
        return False


def _chart_indicator_records(indicators: pd.DataFrame, chart_indicators: Table) -> List[dict]:
    records = []
    value_columns = [column.name for column in chart_indicators.columns]
    for _, row in indicators.iterrows():
        record = {}
        for column in value_columns:
            value = row.get(column)
            if pd.isna(value):
                record[column] = None
            elif column == "date":
                record[column] = _normalize_timestamp(pd.Timestamp(value))
            elif column.startswith("is_"):
                record[column] = bool(value)
            else:
                record[column] = value
        records.append(record)
    return records


def save_chart_indicators_batch_to_db(
    records: List[dict],
    engine: Engine,
    replace_symbols: Optional[List[str]] = None,
) -> int:
    if not records:
        return 0

    metadata = MetaData()
    chart_indicators = _get_chart_indicators_table(metadata)
    manifest_table = _get_chart_indicator_manifests_table(metadata)
    _ensure_chart_indicators_table(engine)
    _ensure_chart_indicator_manifests_table(engine)
    replacement_symbols = _clean_symbols(replace_symbols or [])
    try:
        with engine.begin() as conn:
            _invalidate_chart_indicator_manifests(
                conn,
                manifest_table,
                [record["symbol"] for record in records if record.get("symbol")],
            )
            for chunk in _record_chunks(
                replacement_symbols, CACHE_QUERY_SYMBOL_CHUNK_SIZE
            ):
                conn.execute(
                    delete(chart_indicators).where(
                        chart_indicators.c.symbol.in_(chunk)
                    )
                )
            return _execute_bulk_upsert(
                conn,
                chart_indicators,
                records,
                ("symbol", "date"),
                engine.dialect.name,
            )
    except SQLAlchemyError:
        return 0


def get_latest_chart_indicator_dates(
    engine: Engine, symbols: List[str]
) -> Dict[str, dt.datetime]:
    """Return the latest persisted chart-indicator date for each symbol."""
    cleaned_symbols = _clean_symbols(symbols)
    if not cleaned_symbols:
        return {}
    rows = []
    try:
        table = _ensure_chart_indicators_table(engine)
        with engine.connect() as conn:
            for chunk in _record_chunks(
                cleaned_symbols, CACHE_QUERY_SYMBOL_CHUNK_SIZE
            ):
                stmt = (
                    select(table.c.symbol, func.max(table.c.date).label("latest_date"))
                    .where(table.c.symbol.in_(chunk))
                    .group_by(table.c.symbol)
                )
                rows.extend(conn.execute(stmt).all())
    except SQLAlchemyError:
        return {}
    return {
        str(row.symbol).upper(): row.latest_date
        for row in rows
        if row.latest_date is not None
    }


def _history_watermark_values(
    watermark: object,
) -> Optional[Tuple[dt.datetime, int]]:
    if isinstance(watermark, dict):
        latest = watermark.get("latest_date")
        row_count = int(watermark.get("row_count") or 0)
    elif isinstance(watermark, (tuple, list)) and len(watermark) >= 2:
        latest = watermark[0]
        row_count = int(watermark[1] or 0)
    else:
        return None
    if latest is None or pd.isna(latest) or row_count <= 0:
        return None
    return _normalize_timestamp(pd.Timestamp(latest)), row_count


def _get_chart_indicator_manifests(
    engine: Engine, symbols: List[str]
) -> Dict[str, Dict[str, object]]:
    cleaned_symbols = _clean_symbols(symbols)
    if not cleaned_symbols:
        return {}
    rows = []
    try:
        table = _ensure_chart_indicator_manifests_table(engine)
        with engine.connect() as conn:
            for chunk in _record_chunks(
                cleaned_symbols, CACHE_QUERY_SYMBOL_CHUNK_SIZE
            ):
                stmt = select(table).where(table.c.symbol.in_(chunk))
                rows.extend(conn.execute(stmt).all())
    except SQLAlchemyError as exc:
        raise RuntimeError("Unable to verify chart-indicator manifests") from exc
    return {
        str(row.symbol).upper(): {
            "reference_symbol": str(row.reference_symbol).upper(),
            "source_latest_date": row.source_latest_date,
            "source_row_count": int(row.source_row_count or 0),
            "reference_latest_date": row.reference_latest_date,
            "reference_row_count": int(row.reference_row_count or 0),
            "cache_version": int(row.cache_version or 0),
        }
        for row in rows
    }


def _save_chart_indicator_manifests(
    engine: Engine,
    symbols: List[str],
    history_watermarks: Dict[str, object],
    reference_symbol: str,
) -> None:
    reference_values = _history_watermark_values(
        history_watermarks.get(reference_symbol)
    )
    if reference_values is None:
        return
    reference_latest, reference_count = reference_values
    now = _utcnow_naive()
    records = []
    for symbol in _clean_symbols(symbols):
        source_values = _history_watermark_values(history_watermarks.get(symbol))
        if source_values is None:
            continue
        source_latest, source_count = source_values
        records.append(
            {
                "symbol": symbol,
                "reference_symbol": reference_symbol,
                "source_latest_date": source_latest,
                "source_row_count": source_count,
                "reference_latest_date": reference_latest,
                "reference_row_count": reference_count,
                "cache_version": CHART_INDICATOR_CACHE_VERSION,
                "completed_at": now,
            }
        )
    if not records:
        return
    table = _ensure_chart_indicator_manifests_table(engine)
    try:
        with engine.begin() as conn:
            for chunk in _record_chunks(
                records, CACHE_QUERY_SYMBOL_CHUNK_SIZE
            ):
                _execute_bulk_upsert(
                    conn,
                    table,
                    chunk,
                    ("symbol",),
                    engine.dialect.name,
                )
    except SQLAlchemyError as exc:
        raise RuntimeError("Unable to save chart-indicator manifests") from exc


def _chart_indicator_manifest_matches(
    manifest: Optional[Dict[str, object]],
    source_values: Tuple[dt.datetime, int],
    reference_values: Tuple[dt.datetime, int],
    reference_symbol: str,
) -> bool:
    if manifest is None:
        return False
    source_latest, source_count = source_values
    reference_latest, reference_count = reference_values
    return (
        manifest["reference_symbol"] == reference_symbol
        and manifest["cache_version"] == CHART_INDICATOR_CACHE_VERSION
        and manifest["source_latest_date"] == source_latest
        and manifest["source_row_count"] == source_count
        and manifest["reference_latest_date"] == reference_latest
        and manifest["reference_row_count"] == reference_count
    )


def _invalidate_chart_indicator_manifests(
    conn, table: Table, symbols: List[str]
) -> None:
    """Invalidate completion metadata in the same transaction as cache DML.

    All application writes to ``chart_indicators`` must use the save helpers
    above so a matching manifest remains proof that no app write was
    interrupted or partially committed.
    """
    for chunk in _record_chunks(
        _clean_symbols(symbols), CACHE_QUERY_SYMBOL_CHUNK_SIZE
    ):
        conn.execute(delete(table).where(table.c.symbol.in_(chunk)))


def _clear_chart_indicator_cache(engine: Engine, symbols: List[str]) -> None:
    cleaned_symbols = _clean_symbols(symbols)
    if not cleaned_symbols:
        return
    metadata = MetaData()
    indicators = _get_chart_indicators_table(metadata)
    manifests = _get_chart_indicator_manifests_table(metadata)
    _ensure_chart_indicators_table(engine)
    _ensure_chart_indicator_manifests_table(engine)
    try:
        with engine.begin() as conn:
            _invalidate_chart_indicator_manifests(
                conn, manifests, cleaned_symbols
            )
            for chunk in _record_chunks(
                cleaned_symbols, CACHE_QUERY_SYMBOL_CHUNK_SIZE
            ):
                conn.execute(
                    delete(indicators).where(indicators.c.symbol.in_(chunk))
                )
    except SQLAlchemyError as exc:
        raise RuntimeError("Unable to clear stale chart-indicator cache") from exc


def get_latest_chart_indicator_source_dates(
    engine: Engine,
    symbols: List[str],
    reference_symbol: str = "SPY",
) -> Dict[str, dt.datetime]:
    """Return each symbol's latest daily date that is also present for SPY."""
    reference_symbol = reference_symbol.strip().upper()
    cleaned_symbols = [symbol for symbol in _clean_symbols(symbols) if symbol != reference_symbol]
    if not cleaned_symbols:
        return {}

    metadata = MetaData()
    prices = _get_price_history_table(metadata)
    reference_prices = prices.alias("reference_prices")
    join_condition = (
        (prices.c.date == reference_prices.c.date)
        & (reference_prices.c.symbol == reference_symbol)
        & (reference_prices.c.interval == "1d")
    )
    rows = []
    try:
        _ensure_price_history_table(engine)
        with engine.connect() as conn:
            for chunk in _record_chunks(cleaned_symbols, CACHE_QUERY_SYMBOL_CHUNK_SIZE):
                stmt = (
                    select(
                        prices.c.symbol,
                        func.max(prices.c.date).label("latest_date"),
                    )
                    .select_from(prices.join(reference_prices, join_condition))
                    .where(
                        prices.c.symbol.in_(chunk), prices.c.interval == "1d"
                    )
                    .group_by(prices.c.symbol)
                )
                rows.extend(conn.execute(stmt).all())
    except SQLAlchemyError as exc:
        raise RuntimeError("Unable to verify chart-indicator source dates") from exc
    return {str(row.symbol).upper(): row.latest_date for row in rows if row.latest_date is not None}


def _exact_chart_indicator_refresh_dates(
    engine: Engine,
    symbols: List[str],
    reference_symbol: str,
    force: bool,
) -> Dict[str, dt.datetime]:
    """Run exact source/indicator coverage checks in bounded symbol chunks."""
    if not symbols:
        return {}
    metadata = MetaData()
    prices = _get_price_history_table(metadata)
    reference_prices = prices.alias("chart_reference_prices")
    indicators = _get_chart_indicators_table(metadata)
    source = prices.join(
        reference_prices,
        (prices.c.date == reference_prices.c.date)
        & (reference_prices.c.symbol == reference_symbol)
        & (reference_prices.c.interval == "1d"),
    )
    _ensure_price_history_table(engine)
    _ensure_chart_indicators_table(engine)
    rows = []
    try:
        with engine.connect() as conn:
            for chunk in _record_chunks(symbols, CACHE_QUERY_SYMBOL_CHUNK_SIZE):
                if force:
                    stmt = (
                        select(
                            prices.c.symbol,
                            func.min(prices.c.date).label("first_missing"),
                        )
                        .select_from(source)
                        .where(
                            prices.c.symbol.in_(chunk), prices.c.interval == "1d"
                        )
                        .group_by(prices.c.symbol)
                    )
                else:
                    source_with_indicators = source.outerjoin(
                        indicators,
                        (indicators.c.symbol == prices.c.symbol)
                        & (indicators.c.date == prices.c.date),
                    )
                    stmt = (
                        select(
                            prices.c.symbol,
                            func.min(prices.c.date).label("first_missing"),
                        )
                        .select_from(source_with_indicators)
                        .where(
                            prices.c.symbol.in_(chunk),
                            prices.c.interval == "1d",
                            indicators.c.date.is_(None),
                        )
                        .group_by(prices.c.symbol)
                    )
                rows.extend(conn.execute(stmt).all())
    except SQLAlchemyError as exc:
        raise RuntimeError("Unable to verify chart-indicator cache coverage") from exc
    return {
        str(row.symbol).upper(): row.first_missing
        for row in rows
        if row.first_missing is not None
    }


def get_chart_indicator_refresh_plan(
    engine: Engine,
    tickers: List[str],
    reference_symbol: str = "SPY",
    force: bool = False,
    history_watermarks: Optional[Dict[str, object]] = None,
) -> Dict[str, dt.datetime]:
    """Map symbols to their earliest missing indicator source date.

    A persisted completion manifest makes the normal restart path a small
    metadata lookup. Symbols without a matching manifest use the exact
    price/SPY/indicator anti-join in bounded chunks. Exact cache hits backfill
    the manifest, so upgrading an existing database requires the audit only
    once and never recalculates already-complete indicators.
    """
    reference_symbol = reference_symbol.strip().upper()
    symbols = [symbol for symbol in _clean_symbols(tickers) if symbol != reference_symbol]
    if not symbols:
        return {}
    if history_watermarks is None:
        history_watermarks = get_price_history_watermarks(
            engine, [reference_symbol, *symbols], interval="1d", strict=True
        )
    if force:
        refresh_dates = _exact_chart_indicator_refresh_dates(
            engine,
            symbols,
            reference_symbol=reference_symbol,
            force=True,
        )
        return refresh_dates
    else:
        reference_values = _history_watermark_values(
            history_watermarks.get(reference_symbol)
        )
        if reference_values is None:
            return {}
        manifests = _get_chart_indicator_manifests(engine, symbols)
        semantic_stale = []
        coverage_unknown = []
        for symbol in symbols:
            source_values = _history_watermark_values(
                history_watermarks.get(symbol)
            )
            if source_values is None:
                continue
            manifest = manifests.get(symbol)
            if manifest is not None and (
                manifest["reference_symbol"] != reference_symbol
                or manifest["cache_version"] != CHART_INDICATOR_CACHE_VERSION
            ):
                semantic_stale.append(symbol)
            elif not _chart_indicator_manifest_matches(
                manifest,
                source_values,
                reference_values,
                reference_symbol,
            ):
                coverage_unknown.append(symbol)

    refresh_dates = _exact_chart_indicator_refresh_dates(
        engine,
        coverage_unknown,
        reference_symbol=reference_symbol,
        force=False,
    )
    semantic_refresh_dates = _exact_chart_indicator_refresh_dates(
        engine,
        semantic_stale,
        reference_symbol=reference_symbol,
        force=True,
    )
    refresh_dates.update(semantic_refresh_dates)
    semantic_empty = [
        symbol
        for symbol in semantic_stale
        if symbol not in semantic_refresh_dates
    ]
    _clear_chart_indicator_cache(engine, semantic_empty)
    complete_symbols = [
        symbol
        for symbol in coverage_unknown
        if symbol not in refresh_dates
    ]
    complete_symbols.extend(semantic_empty)
    _save_chart_indicator_manifests(
        engine,
        complete_symbols,
        history_watermarks,
        reference_symbol,
    )
    return refresh_dates


def refresh_chart_indicators_for_symbol(symbol: str, engine: Engine, reference_symbol: str = "SPY") -> bool:
    history = load_symbol_history_from_db(symbol, engine)
    spy_history = load_symbol_history_from_db(reference_symbol, engine)
    indicators = calculate_chart_indicators(symbol, history, spy_history)
    return save_chart_indicators_to_db(symbol, indicators, engine)


def refresh_chart_indicators_to_db(
    tickers: List[str],
    engine: Engine,
    reference_symbol: str = "SPY",
    log_callback: Optional[Callable[[str], None]] = None,
    force: bool = False,
    history_watermarks: Optional[Dict[str, object]] = None,
    refresh_plan: Optional[Dict[str, dt.datetime]] = None,
) -> List[str]:
    updated = []
    reference_symbol = reference_symbol.strip().upper()
    all_symbols = [symbol for symbol in _clean_symbols(tickers) if symbol != reference_symbol]
    if refresh_plan is None:
        refresh_plan = get_chart_indicator_refresh_plan(
            engine,
            all_symbols,
            reference_symbol=reference_symbol,
            force=force,
            history_watermarks=history_watermarks,
        )
    else:
        allowed_symbols = set(all_symbols)
        refresh_plan = {
            symbol: start_date
            for raw_symbol, start_date in refresh_plan.items()
            if (symbol := str(raw_symbol).strip().upper()) in allowed_symbols
        }
    symbols = list(refresh_plan)
    cached_count = len(all_symbols) - len(symbols)
    if not symbols:
        if log_callback:
            log_callback(f"Chart indicators already current for {len(all_symbols)} symbols -- skipping.")
        return []

    replacement_symbols = set(symbols if force else [])
    if not force:
        manifests = _get_chart_indicator_manifests(engine, symbols)
        replacement_symbols.update(
            symbol
            for symbol in symbols
            if (
                (manifest := manifests.get(symbol)) is not None
                and (
                    manifest["reference_symbol"] != reference_symbol
                    or manifest["cache_version"]
                    != CHART_INDICATOR_CACHE_VERSION
                )
            )
        )

    total = len(symbols)
    start_ts = time.time()
    progress_every = max(1, min(100, total // 20 or 1))
    if log_callback:
        log_callback(
            f"Calculating chart indicators: 0/{total} (0%) - "
            f"cached={cached_count}, ETA calculating..."
        )

    histories = load_universe_history_from_db(list(dict.fromkeys([reference_symbol, *symbols])), engine)
    if not histories:
        if log_callback:
            log_callback("  Failed to load cached daily histories for chart indicators.")
        return []

    spy_history = histories.get(reference_symbol)
    if spy_history is None or spy_history.empty:
        try:
            spy_history = load_symbol_history_from_db(reference_symbol, engine, interval="1d")
        except Exception:
            spy_history = None
    if spy_history is None or spy_history.empty:
        if log_callback:
            log_callback(f"  {reference_symbol}: reference history unavailable for chart indicators.")
        return []

    metadata = MetaData()
    chart_indicators = _get_chart_indicators_table(metadata)
    _ensure_chart_indicators_table(engine)
    pending_records: List[dict] = []
    pending_symbols: List[str] = []
    rows_saved = 0
    save_threshold = 25000

    def flush_pending() -> None:
        nonlocal pending_records, pending_symbols, rows_saved, updated
        if not pending_records:
            return
        pending_replacements = [
            symbol
            for symbol in pending_symbols
            if symbol in replacement_symbols
        ]
        if pending_replacements:
            saved_count = save_chart_indicators_batch_to_db(
                pending_records,
                engine,
                replace_symbols=pending_replacements,
            )
        else:
            saved_count = save_chart_indicators_batch_to_db(
                pending_records, engine
            )
        if saved_count:
            rows_saved += saved_count
            updated.extend(pending_symbols)
        elif log_callback:
            log_callback(f"  Failed to bulk save {len(pending_records)} chart indicator rows.")
        pending_records = []
        pending_symbols = []

    for index, symbol in enumerate(symbols, start=1):
        history = histories.get(symbol)
        if history is None or history.empty:
            if log_callback:
                log_callback(f"  {symbol}: unable to calculate chart indicators")
        else:
            indicators = calculate_chart_indicators_since(
                symbol,
                history,
                spy_history,
                start_date=refresh_plan[symbol],
            )
            records = _chart_indicator_records(indicators, chart_indicators)
            if records:
                pending_records.extend(records)
                pending_symbols.append(symbol)
            elif log_callback:
                log_callback(f"  {symbol}: unable to calculate chart indicators")

        if len(pending_records) >= save_threshold:
            flush_pending()

        if log_callback and (index == total or index % progress_every == 0):
            flush_pending()
            elapsed = time.time() - start_ts
            avg_seconds = elapsed / max(1, index)
            eta_text = _format_eta(int(avg_seconds * max(0, total - index)))
            percent = int((index / total) * 100) if total else 100
            log_callback(
                f"Chart indicators progress: {index}/{total} ({percent}%) - "
                f"symbols_saved={len(set(updated))}, rows_saved={rows_saved}, ETA {eta_text}"
            )

    flush_pending()
    return list(dict.fromkeys(updated))


def load_chart_indicators_from_db(symbol: str, engine: Engine) -> pd.DataFrame:
    metadata = MetaData()
    chart_indicators = _get_chart_indicators_table(metadata)
    stmt = (
        select(chart_indicators)
        .where(chart_indicators.c.symbol == symbol.strip().upper())
        .order_by(chart_indicators.c.date)
    )

    try:
        with engine.connect() as conn:
            rows = conn.execute(stmt).all()
    except SQLAlchemyError:
        return pd.DataFrame()

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows, columns=rows[0]._mapping.keys())
    df["date"] = pd.to_datetime(df["date"])
    return df.set_index("date").sort_index()


def get_latest_price_history_date(engine: Engine, interval: str = "1d") -> Optional[dt.datetime]:
    """Return the most recent market data date stored in price_history."""
    metadata = MetaData()
    price_history = _get_price_history_table(metadata)
    stmt = select(func.max(price_history.c.date)).where(price_history.c.interval == interval.strip().lower())

    try:
        _ensure_price_history_table(engine)
        with engine.connect() as conn:
            latest_date = conn.execute(stmt).scalar_one_or_none()
    except SQLAlchemyError:
        return None

    return latest_date


def get_latest_price_history_dates(
    engine: Engine,
    symbols: List[str],
    interval: str = "1d",
    strict: bool = False,
) -> Dict[str, dt.datetime]:
    """Return latest cached daily/intraday price_history date per symbol."""
    cleaned_symbols = _clean_symbols(symbols)
    if not cleaned_symbols:
        return {}

    metadata = MetaData()
    price_history = _get_price_history_table(metadata)
    rows = []
    try:
        _ensure_price_history_table(engine)
        with engine.connect() as conn:
            for chunk in _record_chunks(cleaned_symbols, CACHE_QUERY_SYMBOL_CHUNK_SIZE):
                stmt = (
                    select(
                        price_history.c.symbol,
                        func.max(price_history.c.date).label("latest_date"),
                    )
                    .where(
                        price_history.c.symbol.in_(chunk),
                        price_history.c.interval == interval.strip().lower(),
                    )
                    .group_by(price_history.c.symbol)
                )
                rows.extend(conn.execute(stmt).all())
    except SQLAlchemyError as exc:
        if strict:
            raise RuntimeError("Unable to verify latest price-history dates") from exc
        return {}

    return {str(row.symbol).upper(): row.latest_date for row in rows if row.latest_date is not None}


def get_price_history_watermarks(
    engine: Engine,
    symbols: List[str],
    interval: str = "1d",
    strict: bool = False,
) -> Dict[str, Tuple[Optional[dt.datetime], int]]:
    """Return causal cache watermarks (latest date and row count) by symbol."""
    cleaned_symbols = _clean_symbols(symbols)
    if not cleaned_symbols:
        return {}

    metadata = MetaData()
    price_history = _get_price_history_table(metadata)
    rows = []
    try:
        _ensure_price_history_table(engine)
        with engine.connect() as conn:
            for chunk in _record_chunks(cleaned_symbols, CACHE_QUERY_SYMBOL_CHUNK_SIZE):
                stmt = (
                    select(
                        price_history.c.symbol,
                        func.max(price_history.c.date).label("latest_date"),
                        func.count().label("row_count"),
                    )
                    .where(
                        price_history.c.symbol.in_(chunk),
                        price_history.c.interval == interval.strip().lower(),
                    )
                    .group_by(price_history.c.symbol)
                )
                rows.extend(conn.execute(stmt).all())
    except SQLAlchemyError as exc:
        if strict:
            raise RuntimeError("Unable to verify price-history watermarks") from exc
        return {}
    return {
        str(row.symbol).upper(): (row.latest_date, int(row.row_count or 0))
        for row in rows
    }


def get_latest_hourly_price_history_timestamps(
    engine: Engine,
    symbols: List[str],
    source: Optional[str] = None,
    strict: bool = False,
) -> Dict[str, dt.datetime]:
    """Return latest cached 1-hour timestamp per symbol."""
    cleaned_symbols = _clean_symbols(symbols)
    if not cleaned_symbols:
        return {}

    metadata = MetaData()
    hourly_history = _get_hourly_price_history_table(metadata)
    rows = []
    try:
        _ensure_hourly_price_history_table(engine)
        with engine.connect() as conn:
            for chunk in _record_chunks(
                cleaned_symbols, HOURLY_CACHE_QUERY_SYMBOL_CHUNK_SIZE
            ):
                stmt = select(
                    hourly_history.c.symbol,
                    func.max(hourly_history.c.timestamp).label("latest_timestamp"),
                ).where(hourly_history.c.symbol.in_(chunk))
                if source:
                    stmt = stmt.where(hourly_history.c.source == source)
                stmt = stmt.group_by(hourly_history.c.symbol)
                rows.extend(conn.execute(stmt).all())
    except SQLAlchemyError as exc:
        if strict:
            raise RuntimeError("Unable to verify latest hourly-history timestamps") from exc
        return {}

    return {str(row.symbol).upper(): row.latest_timestamp for row in rows if row.latest_timestamp is not None}


def get_earliest_hourly_price_history_timestamps(
    engine: Engine,
    symbols: List[str],
    source: Optional[str] = None,
    strict: bool = False,
) -> Dict[str, dt.datetime]:
    """Return earliest cached 1-hour timestamp per symbol.

    Used alongside the latest timestamp to detect "shallow" hourly history --
    a symbol that refreshes daily can look perfectly current by recency alone
    while still only holding a couple of weeks of depth (see
    ``_period_for_hourly_refresh``).
    """
    cleaned_symbols = _clean_symbols(symbols)
    if not cleaned_symbols:
        return {}

    metadata = MetaData()
    hourly_history = _get_hourly_price_history_table(metadata)
    rows = []
    try:
        _ensure_hourly_price_history_table(engine)
        with engine.connect() as conn:
            for chunk in _record_chunks(
                cleaned_symbols, HOURLY_CACHE_QUERY_SYMBOL_CHUNK_SIZE
            ):
                stmt = select(
                    hourly_history.c.symbol,
                    func.min(hourly_history.c.timestamp).label("earliest_timestamp"),
                ).where(hourly_history.c.symbol.in_(chunk))
                if source:
                    stmt = stmt.where(hourly_history.c.source == source)
                stmt = stmt.group_by(hourly_history.c.symbol)
                rows.extend(conn.execute(stmt).all())
    except SQLAlchemyError as exc:
        if strict:
            raise RuntimeError("Unable to verify earliest hourly-history timestamps") from exc
        return {}

    return {str(row.symbol).upper(): row.earliest_timestamp for row in rows if row.earliest_timestamp is not None}


def _format_eta(seconds: int) -> str:
    if seconds < 0:
        return "00:00"
    minutes = seconds // 60
    secs = seconds % 60
    return f"{int(minutes):02d}:{int(secs):02d}"


def _format_elapsed(seconds: float) -> str:
    return _format_eta(int(max(0, seconds)))


def _chunk_symbols(symbols: List[str], chunk_size: int) -> List[List[str]]:
    size = max(1, int(chunk_size or 1))
    return [symbols[index:index + size] for index in range(0, len(symbols), size)]


def _symbols_with_history(
    history: pd.DataFrame,
    symbols: List[str],
    required_latest_date: Optional[dt.date] = None,
) -> List[str]:
    if history.empty:
        return []
    available = []
    for symbol in symbols:
        symbol_history = _extract_symbol_history(history, symbol)
        if symbol_history is None or symbol_history.empty:
            continue
        if required_latest_date is not None:
            try:
                latest = pd.Timestamp(symbol_history.index.max())
            except (TypeError, ValueError):
                continue
            if pd.isna(latest) or latest.date() < required_latest_date:
                continue
        available.append(symbol)
    return available


def _partition_symbols_by_freshness(
    latest_by_symbol: Dict[str, object],
    symbols: List[str],
    expected_date: dt.date,
) -> Tuple[List[str], List[str]]:
    """Split symbols by whether their cached data reaches the expected session."""
    current: List[str] = []
    stale: List[str] = []
    for symbol in _clean_symbols(symbols):
        latest = latest_by_symbol.get(symbol)
        if latest is None or pd.isna(latest):
            stale.append(symbol)
            continue
        try:
            latest_date = pd.Timestamp(latest).date()
        except (TypeError, ValueError):
            stale.append(symbol)
            continue
        (current if latest_date >= expected_date else stale).append(symbol)
    return current, stale


def _record_history_refresh_outcomes(
    engine: Engine,
    interval: str,
    current_symbols: List[str],
    stale_symbols: List[str],
) -> None:
    """Record symbol failures without mistaking a provider outage for bad tickers.

    If SPY itself could not reach the expected session, failures for the rest
    of that payload are not evidence that each individual symbol is unusable.
    Only advance the canary's streak; current symbols still reset normally.
    """
    failures = (
        [REFERENCE_SYMBOL]
        if REFERENCE_SYMBOL in set(stale_symbols)
        else stale_symbols
    )
    record_symbol_refresh_outcomes(
        engine, interval, current_symbols, failures
    )


def _period_for_daily_refresh(
    latest_date: Optional[dt.datetime],
    full_period: str = "1y",
    incremental_period: str = "1mo",
    recent_days: int = 45,
) -> str:
    if latest_date is None:
        return full_period
    latest = pd.Timestamp(latest_date).tz_localize(None).to_pydatetime()
    age_days = max(0, (_utcnow_naive() - latest).days)
    if age_days <= max(1, int(recent_days)):
        return incremental_period
    return full_period


def _period_groups_for_symbols(period_by_symbol: Dict[str, str], symbols: List[str]) -> Dict[str, List[str]]:
    groups: Dict[str, List[str]] = {}
    for symbol in symbols:
        groups.setdefault(period_by_symbol[symbol], []).append(symbol)
    return groups


def _emit_batch_progress(
    progress_callback: Optional[Callable[[str, int, int, int, str], None]],
    symbols: List[str],
    processed: int,
    total: int,
    start_ts: float,
) -> None:
    if not progress_callback or total <= 0:
        return
    elapsed = time.time() - start_ts
    avg_per_symbol = elapsed / max(1, processed)
    eta_text = _format_eta(int(avg_per_symbol * max(0, total - processed)))
    percent = int((processed / total) * 100)
    progress_callback(symbols[-1] if symbols else "", processed, total, percent, eta_text)


def _sleep_between_batches(batch_sleep: float) -> None:
    if batch_sleep <= 0:
        return
    time.sleep(batch_sleep + random.uniform(0.0, min(1.0, batch_sleep)))


def refresh_universe_history_to_db(
    tickers: List[str],
    engine: Engine,
    period: str = "3mo",
    interval: str = "1d",
    chunk_size: int = 200,
    threads: int = 8,
    batch_sleep: float = 1.5,
    retry_attempts: int = 0,
    full_backfill: bool = False,
    incremental_period: str = "1mo",
    recent_days: int = 45,
    progress_callback: Optional[Callable[[str, int, int, int, str], None]] = None,
    log_callback: Optional[Callable[[str], None]] = None,
) -> List[str]:
    symbols = _clean_symbols(tickers)
    if not symbols:
        return []

    interval = interval.strip().lower() or "1d"
    total = len(symbols)
    start_ts = time.time()
    updated: List[str] = []
    failed: List[str] = []
    current_from_download: Set[str] = set()
    expected_date = expected_latest_market_data_date()

    latest_by_symbol = {} if full_backfill else get_latest_price_history_dates(engine, symbols, interval=interval)
    period_by_symbol = {
        symbol: period if full_backfill else _period_for_daily_refresh(
            latest_by_symbol.get(symbol),
            full_period=period,
            incremental_period=incremental_period,
            recent_days=recent_days,
        )
        for symbol in symbols
    }
    period_groups = _period_groups_for_symbols(period_by_symbol, symbols)

    if log_callback:
        mode = "full backfill" if full_backfill else "incremental"
        log_callback(
            f"Starting {interval} yfinance {mode} refresh: {total} symbols, "
            f"chunk_size={chunk_size}, threads={threads}, sleep={batch_sleep:.1f}s"
        )

    processed = 0
    batch_number = 0
    total_batches = sum(len(_chunk_symbols(group, chunk_size)) for group in period_groups.values())

    for fetch_period, group_symbols in period_groups.items():
        for batch in _chunk_symbols(group_symbols, chunk_size):
            batch_number += 1
            if log_callback:
                log_callback(
                    f"Batch {batch_number}/{total_batches} {interval}: period={fetch_period}, "
                    f"symbols={', '.join(batch)}"
                )

            history = download_price_history(
                batch,
                period=fetch_period,
                interval=interval,
                max_symbols=len(batch),
                chunk_size=len(batch),
                threads=threads,
                batch_sleep=0,
                max_retries=0,
                fallback_to_single=False,
                chart_fallback=True,
                required_latest_date=expected_date,
            )
            available = _symbols_with_history(history, batch)
            current = _symbols_with_history(
                history, batch, required_latest_date=expected_date
            )
            current_from_download.update(current)
            not_current = [symbol for symbol in batch if symbol not in current]
            rows_saved = save_universe_history_batch_to_db(history, available, engine, interval=interval)

            if rows_saved:
                updated.extend(available)
            failed.extend(not_current)
            processed += len(batch)

            if log_callback:
                log_callback(
                    f"Batch {batch_number}/{total_batches} {interval}: rows_saved={rows_saved}, "
                    f"not_current={', '.join(not_current) if not_current else 'none'}"
                )
            _emit_batch_progress(progress_callback, batch, processed, total, start_ts)

            if batch_number < total_batches:
                _sleep_between_batches(batch_sleep)

    retry_symbols = [
        symbol
        for symbol in dict.fromkeys(failed)
        if symbol not in current_from_download
    ]
    for retry_index in range(1, max(0, int(retry_attempts)) + 1):
        if not retry_symbols:
            break

        retry_chunk_size = max(1, min(50, chunk_size // (2 ** retry_index)))
        if log_callback:
            log_callback(
                f"Retry {retry_index}/{retry_attempts} for {len(retry_symbols)} {interval} symbols "
                f"with chunk_size={retry_chunk_size}"
            )
        time.sleep(min(30.0, max(1.0, batch_sleep) * (2 ** retry_index)) + random.uniform(0.2, 1.0))

        next_retry: List[str] = []
        retry_groups = _period_groups_for_symbols(period_by_symbol, retry_symbols)
        for fetch_period, group_symbols in retry_groups.items():
            for batch in _chunk_symbols(group_symbols, retry_chunk_size):
                if log_callback:
                    log_callback(f"Retry {retry_index} {interval}: period={fetch_period}, symbols={', '.join(batch)}")
                history = download_price_history(
                    batch,
                    period=fetch_period,
                    interval=interval,
                    max_symbols=len(batch),
                    chunk_size=len(batch),
                    threads=threads,
                    batch_sleep=0,
                    max_retries=0,
                    fallback_to_single=False,
                    chart_fallback=True,
                    required_latest_date=expected_date,
                )
                available = _symbols_with_history(history, batch)
                current = _symbols_with_history(
                    history, batch, required_latest_date=expected_date
                )
                current_from_download.update(current)
                not_current = [symbol for symbol in batch if symbol not in current]
                rows_saved = save_universe_history_batch_to_db(history, available, engine, interval=interval)
                if rows_saved:
                    updated.extend(available)
                next_retry.extend(not_current)
                if log_callback:
                    log_callback(
                        f"Retry {retry_index} {interval}: rows_saved={rows_saved}, "
                        f"not_current={', '.join(not_current) if not_current else 'none'}"
                    )
                _sleep_between_batches(batch_sleep)
        retry_symbols = [
            symbol
            for symbol in dict.fromkeys(next_retry)
            if symbol not in current_from_download
        ]

    if retry_symbols and log_callback:
        log_callback(
            f"{len(retry_symbols)} {interval} symbol(s) remained unavailable or stale "
            f"through {expected_date} after batch and concurrent chart fallback; "
            "skipping redundant serial retries."
        )

    deduped_updated = list(dict.fromkeys(updated))
    latest_after_refresh = get_latest_price_history_dates(engine, symbols, interval=interval)
    current_symbols, stale_symbols = _partition_symbols_by_freshness(
        latest_after_refresh, symbols, expected_date
    )
    if log_callback:
        log_callback(
            f"Completed {interval} yfinance refresh: "
            f"received_rows_for={len(deduped_updated)}, "
            f"current={len(current_symbols)}, still_stale={len(stale_symbols)}, "
            f"elapsed={_format_elapsed(time.time() - start_ts)}"
        )
        if stale_symbols:
            log_callback(f"Still-stale {interval} symbols: {', '.join(stale_symbols)}")

    _record_history_refresh_outcomes(
        engine, interval, current_symbols, stale_symbols
    )

    return deduped_updated


# Depth a symbol's hourly history must reach back to before it's treated as
# "complete" rather than a thin, recency-only cache. 200 calendar days covers
# the TradingView 1H chart's ~120-trading-day default view (~840 hourly bars)
# with headroom for panning back further.
MIN_HOURLY_HISTORY_DAYS = 200


def _period_for_hourly_refresh(
    latest_timestamp: Optional[dt.datetime],
    earliest_timestamp: Optional[dt.datetime] = None,
    full_period: str = "730d",
    incremental_period: str = "10d",
    recent_days: int = 10,
    min_history_days: int = MIN_HOURLY_HISTORY_DAYS,
    backfill: bool = False,
) -> str:
    """Mirror ``_period_for_daily_refresh``, plus a depth check daily doesn't
    need (daily's very first fetch always used the full period, so recency
    alone implies depth; hourly's did not until this fixed, so existing
    "recent but shallow" caches must self-heal too).

    A symbol gets a full re-pull, with no manual ``--backfill`` flag
    required, when any of:
    - it has no cached hourly history at all,
    - its latest cached bar is older than ``recent_days`` (an incremental
      pull would leave a permanent gap behind it), or
    - its earliest cached bar doesn't reach back ``min_history_days`` (the
      cache is recent but shallow).
    ``backfill`` remains as an explicit override to force a full re-pull for
    every symbol regardless of freshness or depth.
    """
    if backfill:
        return full_period
    if latest_timestamp is None:
        return full_period
    latest = pd.Timestamp(latest_timestamp).tz_localize(None).to_pydatetime()
    age_days = max(0, (_utcnow_naive() - latest).days)
    if age_days > max(1, int(recent_days)):
        return full_period
    if earliest_timestamp is not None:
        earliest = pd.Timestamp(earliest_timestamp).tz_localize(None).to_pydatetime()
        depth_days = max(0, (_utcnow_naive() - earliest).days)
        if depth_days < max(1, int(min_history_days)):
            return full_period
    return incremental_period


def refresh_universe_hourly_history_to_db(
    tickers: List[str],
    engine: Engine,
    full_period: str = "730d",
    source: str = "yfinance",
    chunk_size: int = 100,
    threads: int = 8,
    batch_sleep: float = 1.5,
    retry_attempts: int = 0,
    backfill: bool = False,
    incremental_period: str = "10d",
    recent_days: int = 10,
    min_history_days: int = MIN_HOURLY_HISTORY_DAYS,
    progress_callback: Optional[Callable[[str, int, int, int, str], None]] = None,
    log_callback: Optional[Callable[[str], None]] = None,
) -> List[str]:
    symbols = _clean_symbols(tickers)
    if not symbols:
        return []

    total = len(symbols)
    start_ts = time.time()
    updated: List[str] = []
    failed: List[str] = []
    current_from_download: Set[str] = set()
    expected_date = expected_latest_market_data_date()

    latest_by_symbol = get_latest_hourly_price_history_timestamps(engine, symbols, source=source)
    earliest_by_symbol = (
        {}
        if backfill
        else get_earliest_hourly_price_history_timestamps(engine, symbols, source=source)
    )
    period_by_symbol = {
        symbol: _period_for_hourly_refresh(
            latest_by_symbol.get(symbol),
            earliest_timestamp=earliest_by_symbol.get(symbol),
            full_period=full_period,
            incremental_period=incremental_period,
            recent_days=recent_days,
            min_history_days=min_history_days,
            backfill=backfill,
        )
        for symbol in symbols
    }
    period_groups = _period_groups_for_symbols(period_by_symbol, symbols)

    if log_callback:
        if backfill:
            mode = "forced full backfill"
        else:
            full_count = sum(1 for period in period_by_symbol.values() if period == full_period)
            mode = (
                f"auto (self-healing: {full_count} full / {total - full_count} incremental)"
                if full_count
                else "incremental"
            )
        log_callback(
            f"Starting 1h yfinance {mode} refresh: {total} symbols, "
            f"chunk_size={chunk_size}, threads={threads}, sleep={batch_sleep:.1f}s"
        )

    processed = 0
    batch_number = 0
    total_batches = sum(len(_chunk_symbols(group, chunk_size)) for group in period_groups.values())

    for fetch_period, group_symbols in period_groups.items():
        for batch in _chunk_symbols(group_symbols, chunk_size):
            batch_number += 1
            if log_callback:
                log_callback(
                    f"Batch {batch_number}/{total_batches} 1h: period={fetch_period}, "
                    f"symbols={', '.join(batch)}"
                )

            history = download_price_history(
                batch,
                period=fetch_period,
                interval="1h",
                max_symbols=len(batch),
                chunk_size=len(batch),
                threads=threads,
                batch_sleep=0,
                max_retries=0,
                fallback_to_single=False,
                chart_fallback=True,
                required_latest_date=expected_date,
            )
            available = _symbols_with_history(history, batch)
            current = _symbols_with_history(
                history, batch, required_latest_date=expected_date
            )
            current_from_download.update(current)
            not_current = [symbol for symbol in batch if symbol not in current]
            rows_saved = save_universe_hourly_history_batch_to_db(history, available, engine, source=source)

            if rows_saved:
                updated.extend(available)
            failed.extend(not_current)
            processed += len(batch)

            if log_callback:
                log_callback(
                    f"Batch {batch_number}/{total_batches} 1h: rows_saved={rows_saved}, "
                    f"not_current={', '.join(not_current) if not_current else 'none'}"
                )
            _emit_batch_progress(progress_callback, batch, processed, total, start_ts)

            if batch_number < total_batches:
                _sleep_between_batches(batch_sleep)

    retry_symbols = [
        symbol
        for symbol in dict.fromkeys(failed)
        if symbol not in current_from_download
    ]
    for retry_index in range(1, max(0, int(retry_attempts)) + 1):
        if not retry_symbols:
            break

        retry_chunk_size = max(1, min(25, chunk_size // (2 ** retry_index)))
        if log_callback:
            log_callback(
                f"Retry {retry_index}/{retry_attempts} for {len(retry_symbols)} 1h symbols "
                f"with chunk_size={retry_chunk_size}"
            )
        time.sleep(min(30.0, max(1.0, batch_sleep) * (2 ** retry_index)) + random.uniform(0.2, 1.0))

        next_retry: List[str] = []
        retry_groups = _period_groups_for_symbols(period_by_symbol, retry_symbols)
        for fetch_period, group_symbols in retry_groups.items():
            for batch in _chunk_symbols(group_symbols, retry_chunk_size):
                if log_callback:
                    log_callback(f"Retry {retry_index} 1h: period={fetch_period}, symbols={', '.join(batch)}")
                history = download_price_history(
                    batch,
                    period=fetch_period,
                    interval="1h",
                    max_symbols=len(batch),
                    chunk_size=len(batch),
                    threads=threads,
                    batch_sleep=0,
                    max_retries=0,
                    fallback_to_single=False,
                    chart_fallback=True,
                    required_latest_date=expected_date,
                )
                available = _symbols_with_history(history, batch)
                current = _symbols_with_history(
                    history, batch, required_latest_date=expected_date
                )
                current_from_download.update(current)
                not_current = [symbol for symbol in batch if symbol not in current]
                rows_saved = save_universe_hourly_history_batch_to_db(history, available, engine, source=source)
                if rows_saved:
                    updated.extend(available)
                next_retry.extend(not_current)
                if log_callback:
                    log_callback(
                        f"Retry {retry_index} 1h: rows_saved={rows_saved}, "
                        f"not_current={', '.join(not_current) if not_current else 'none'}"
                    )
                _sleep_between_batches(batch_sleep)
        retry_symbols = [
            symbol
            for symbol in dict.fromkeys(next_retry)
            if symbol not in current_from_download
        ]

    if retry_symbols and log_callback:
        log_callback(
            f"{len(retry_symbols)} 1h symbol(s) remained unavailable or stale through "
            f"{expected_date} after batch and concurrent chart fallback; "
            "skipping redundant serial retries."
        )

    deduped_updated = list(dict.fromkeys(updated))
    # Match the gate: current data from any hourly source satisfies freshness.
    latest_after_refresh = get_latest_hourly_price_history_timestamps(engine, symbols)
    current_symbols, stale_symbols = _partition_symbols_by_freshness(
        latest_after_refresh, symbols, expected_date
    )
    if log_callback:
        log_callback(
            f"Completed 1h yfinance refresh: "
            f"received_rows_for={len(deduped_updated)}, "
            f"current={len(current_symbols)}, still_stale={len(stale_symbols)}, "
            f"elapsed={_format_elapsed(time.time() - start_ts)}"
        )
        if stale_symbols:
            log_callback(f"Still-stale 1h symbols: {', '.join(stale_symbols)}")

    _record_history_refresh_outcomes(
        engine, "1h", current_symbols, stale_symbols
    )

    return deduped_updated


def scanner_metrics_snapshot_date(expected_date: Optional[dt.date] = None) -> dt.datetime:
    """Database key for metrics derived from the latest completed market session."""
    market_date = expected_date or expected_latest_market_data_date()
    if isinstance(market_date, dt.datetime):
        market_date = market_date.date()
    return dt.datetime.combine(market_date, dt.time.min)


def _scanner_metric_and_input_symbols(tickers: List[str]) -> Tuple[List[str], List[str]]:
    metric_symbols = [
        symbol for symbol in _clean_symbols(tickers) if symbol != REFERENCE_SYMBOL
    ]
    return metric_symbols, [REFERENCE_SYMBOL, *metric_symbols]


def scanner_metrics_input_fingerprint(
    tickers: List[str], history_watermarks: Dict[str, object]
) -> str:
    """Hash the calculation version, universe, and causal daily inputs."""
    entries = [f"version={SCANNER_METRICS_CACHE_VERSION}"]
    for symbol in sorted(_clean_symbols(tickers)):
        watermark = history_watermarks.get(symbol)
        if isinstance(watermark, dict):
            latest = watermark.get("latest_date")
            row_count = int(watermark.get("row_count") or 0)
        elif isinstance(watermark, (tuple, list)) and len(watermark) >= 2:
            latest, row_count = watermark[0], int(watermark[1] or 0)
        else:
            # Retain a tolerant public helper for callers/tests supplying the
            # former latest-date-only shape. New cache decisions always pass
            # the stronger (latest date, row count) watermark.
            latest, row_count = watermark, 0
        if latest is None or pd.isna(latest):
            latest_text = ""
        else:
            latest_text = pd.Timestamp(latest).date().isoformat()
        entries.append(f"{symbol}|{latest_text}|{row_count}")
    return hashlib.sha256("\n".join(entries).encode("utf-8")).hexdigest()


def is_scanner_metrics_snapshot_current(
    engine: Engine,
    tickers: List[str],
    history_watermarks: Optional[Dict[str, object]] = None,
    snapshot_date: Optional[dt.datetime] = None,
    strict: bool = False,
) -> bool:
    """Whether a complete scanner snapshot matches the current daily inputs."""
    metric_symbols, input_symbols = _scanner_metric_and_input_symbols(tickers)
    if not metric_symbols:
        return False
    if history_watermarks is None:
        history_watermarks = get_price_history_watermarks(
            engine, input_symbols, interval="1d", strict=strict
        )
    fingerprint = scanner_metrics_input_fingerprint(input_symbols, history_watermarks)
    snapshot_date = snapshot_date or scanner_metrics_snapshot_date()

    try:
        metrics_table = _ensure_scanner_metrics_table(engine)
        snapshots_table = _ensure_scanner_metric_snapshots_table(engine)
        snapshot_stmt = select(
            snapshots_table.c.input_fingerprint,
            snapshots_table.c.metric_count,
        ).where(snapshots_table.c.snapshot_date == snapshot_date)
        with engine.connect() as conn:
            snapshot = conn.execute(snapshot_stmt).one_or_none()
            if snapshot is None or snapshot.input_fingerprint != fingerprint:
                return False
            actual_count = 0
            for chunk in _record_chunks(
                metric_symbols, SCANNER_QUERY_SYMBOL_CHUNK_SIZE
            ):
                count_stmt = select(func.count()).select_from(metrics_table).where(
                    metrics_table.c.symbol.in_(chunk),
                    metrics_table.c.date == snapshot_date,
                )
                actual_count += int(conn.execute(count_stmt).scalar_one())
    except SQLAlchemyError as exc:
        if strict:
            raise RuntimeError("Unable to verify scanner-metrics snapshot") from exc
        return False
    return int(snapshot.metric_count) > 0 and actual_count == int(snapshot.metric_count)


def get_universe_stock_metrics_from_db(
    tickers: List[str],
    engine: Engine,
    min_history_days: int = 1,
    lookback_days: int = 380,
) -> List[dict]:
    metric_symbols, input_symbols = _scanner_metric_and_input_symbols(tickers)
    if not metric_symbols:
        return []
    snapshot_date = scanner_metrics_snapshot_date()
    history_watermarks = get_price_history_watermarks(
        engine, input_symbols, interval="1d", strict=True
    )
    if is_scanner_metrics_snapshot_current(
        engine,
        metric_symbols,
        history_watermarks=history_watermarks,
        snapshot_date=snapshot_date,
        strict=True,
    ):
        return load_scanner_metrics_from_db(metric_symbols, engine, snapshot_date)

    from src.utils.data_loader import compute_stock_metrics

    metrics = []
    start_date = _utcnow_naive() - dt.timedelta(days=lookback_days)
    histories = load_universe_history_from_db(input_symbols, engine, start=start_date)
    if not histories:
        return []

    spy_history = histories.get("SPY")
    if spy_history is None:
        try:
            spy_history = load_symbol_history_from_db("SPY", engine, interval="1d")
        except Exception:
            spy_history = None

    for symbol in metric_symbols:
        history = histories.get(symbol)
        if history is None or history.empty:
            continue
        result = compute_stock_metrics(symbol, history, min_history_days=min_history_days, spy_history=spy_history)
        if result is not None:
            metrics.append(result)

    if not metrics:
        return []

    # Rank 1-month growth (populated in 'growth_rank' and 'growth_rank_1m')
    growth_values_1m = [item.get("return_1m", 0.0) for item in metrics]
    ranks_1m = pd.Series(growth_values_1m).rank(pct=True, method="max") * 100
    for idx, item in enumerate(metrics):
        item["growth_rank"] = float(ranks_1m.iloc[idx])
        item["growth_rank_1m"] = float(ranks_1m.iloc[idx])

    # Rank 3-month growth
    growth_values_3m = [item.get("return_3m", 0.0) for item in metrics]
    ranks_3m = pd.Series(growth_values_3m).rank(pct=True, method="max") * 100
    for idx, item in enumerate(metrics):
        item["growth_rank_3m"] = float(ranks_3m.iloc[idx])

    return metrics


def save_scanner_metrics_to_db(symbol: str, metrics: dict, date: dt.datetime, engine: Engine) -> bool:
    """Save computed scanner metrics for a symbol to the database."""
    metadata = MetaData()
    table = _get_scanner_metrics_table(metadata)
    row_data = _scanner_metric_record(symbol, metrics, date, table)
            
    try:
        with engine.begin() as conn:
            _execute_bulk_upsert(
                conn,
                table,
                [row_data],
                ("symbol", "date"),
                engine.dialect.name,
            )
        return True
    except SQLAlchemyError:
        return False


def _scanner_metric_record(symbol: str, metrics: dict, date: dt.datetime, table: Table) -> dict:
    row_data = {
        "symbol": symbol.strip().upper(),
        "date": date,
        "updated_at": _utcnow_naive(),
    }
    for key, value in metrics.items():
        if key not in table.columns:
            continue
        if pd.isna(value):
            value = None
        elif isinstance(value, (np.int64, np.int32)):
            value = int(value)
        elif isinstance(value, (np.float64, np.float32)):
            value = float(value)
        elif isinstance(value, (np.bool_)):
            value = bool(value)
        row_data[key] = value
    return row_data


def save_scanner_metrics_batch_to_db(metrics_list: List[dict], date: dt.datetime, engine: Engine) -> List[str]:
    if not metrics_list:
        return []

    metadata = MetaData()
    table = _get_scanner_metrics_table(metadata)
    _ensure_scanner_metrics_table(engine)
    records = [
        _scanner_metric_record(item["symbol"], item, date, table)
        for item in metrics_list
        if item.get("symbol")
    ]
    if not records:
        return []

    try:
        with engine.begin() as conn:
            for chunk in _record_chunks(
                records, SCANNER_METRIC_WRITE_CHUNK_SIZE
            ):
                _execute_bulk_upsert(
                    conn,
                    table,
                    chunk,
                    ("symbol", "date"),
                    engine.dialect.name,
                )
        return [record["symbol"] for record in records]
    except SQLAlchemyError:
        return []


def save_scanner_metrics_snapshot_to_db(
    metrics_list: List[dict],
    snapshot_date: dt.datetime,
    input_fingerprint: str,
    engine: Engine,
    snapshot_symbols: Optional[List[str]] = None,
    strict: bool = False,
) -> List[str]:
    """Atomically replace a complete scanner snapshot in bounded statements."""
    if not metrics_list:
        return []
    try:
        metrics_table = _ensure_scanner_metrics_table(engine)
        snapshots_table = _ensure_scanner_metric_snapshots_table(engine)
        records = [
            _scanner_metric_record(item["symbol"], item, snapshot_date, metrics_table)
            for item in metrics_list
            if item.get("symbol")
        ]
        if not records:
            return []
        manifest = {
            "snapshot_date": snapshot_date,
            "input_fingerprint": input_fingerprint,
            "metric_count": len(records),
            "completed_at": _utcnow_naive(),
        }
        delete_symbols = _clean_symbols(
            snapshot_symbols
            or [record["symbol"] for record in records]
        )
        dialect_name = engine.dialect.name
        with engine.begin() as conn:
            # The primary key starts with symbol, so a date-only delete can
            # scan the entire historical metrics table and hit the socket
            # timeout. Keep every statement indexed and bounded while the
            # surrounding transaction preserves all-or-nothing replacement.
            for chunk in _record_chunks(
                delete_symbols, SCANNER_QUERY_SYMBOL_CHUNK_SIZE
            ):
                conn.execute(
                    delete(metrics_table).where(
                        metrics_table.c.symbol.in_(chunk),
                        metrics_table.c.date == snapshot_date,
                    )
                )
            for chunk in _record_chunks(
                records, SCANNER_METRIC_WRITE_CHUNK_SIZE
            ):
                _execute_bulk_upsert(
                    conn,
                    metrics_table,
                    chunk,
                    ("symbol", "date"),
                    dialect_name,
                )
            _execute_bulk_upsert(
                conn,
                snapshots_table,
                [manifest],
                ("snapshot_date",),
                dialect_name,
            )
    except SQLAlchemyError as exc:
        if strict:
            driver_error = getattr(exc, "orig", None)
            detail = f": {driver_error}" if driver_error is not None else ""
            raise RuntimeError(
                f"Unable to save scanner metrics snapshot{detail}"
            ) from exc
        return []
    return [record["symbol"] for record in records]


def load_scanner_metrics_from_db(tickers: List[str], engine: Engine, date: Optional[dt.datetime] = None) -> List[dict]:
    """Load cached scanner metrics from the active data engine."""
    if date is None:
        date = scanner_metrics_snapshot_date()
    symbols = _clean_symbols(tickers)
    if not symbols:
        return []

    metadata = MetaData()
    table = _get_scanner_metrics_table(metadata)

    try:
        with engine.connect() as conn:
            rows = []
            for chunk in _record_chunks(
                symbols, SCANNER_QUERY_SYMBOL_CHUNK_SIZE
            ):
                stmt = select(table).where(
                    table.c.symbol.in_(chunk), table.c.date == date
                )
                rows.extend(conn.execute(stmt).fetchall())

            results = []
            for row in rows:
                row_dict = {}
                for idx, col in enumerate(table.columns):
                    val = row[idx]
                    if isinstance(col.type, Boolean) and val is not None:
                        val = bool(val)
                    row_dict[col.name] = val
                results.append(row_dict)
            return results
    except SQLAlchemyError:
        return []


def refresh_scanner_metrics_to_db(
    tickers: List[str],
    engine: Engine,
    log_callback: Optional[Callable[[str], None]] = None,
    force: bool = False,
    history_watermarks: Optional[Dict[str, object]] = None,
) -> List[str]:
    """Calculate and store scanner metrics for the universe."""
    symbols, input_symbols = _scanner_metric_and_input_symbols(tickers)
    if not symbols:
        return []
    snapshot_date = scanner_metrics_snapshot_date()
    if history_watermarks is None:
        history_watermarks = get_price_history_watermarks(
            engine, input_symbols, interval="1d", strict=True
        )
    input_fingerprint = scanner_metrics_input_fingerprint(
        input_symbols, history_watermarks
    )
    if not force and is_scanner_metrics_snapshot_current(
        engine,
        symbols,
        history_watermarks=history_watermarks,
        snapshot_date=snapshot_date,
        strict=True,
    ):
        if log_callback:
            log_callback(
                f"Scanner metrics already current for {snapshot_date.date()} -- skipping."
            )
        return []

    if log_callback:
        log_callback("Pre-calculating and saving scanner metrics...")

    try:
        # Compute directly so a stale snapshot is never read during generation.
        metrics_list = []
        start_date = _utcnow_naive() - dt.timedelta(days=380)
        histories = load_universe_history_from_db(input_symbols, engine, start=start_date)
        if not histories:
            if log_callback:
                log_callback("  Failed to load history for calculation.")
            return []

        spy_history = histories.get("SPY")
        if spy_history is None:
            try:
                spy_history = load_symbol_history_from_db("SPY", engine, interval="1d")
            except Exception:
                spy_history = None

        from src.utils.data_loader import compute_stock_metrics
        total_symbols = len(symbols)
        metric_start_ts = time.time()
        metric_progress_every = max(1, min(100, total_symbols // 20 or 1))
        if log_callback:
            log_callback(f"Calculating scanner metrics: 0/{total_symbols} (0%) - ETA calculating...")

        for index, symbol in enumerate(symbols, start=1):
            history = histories.get(symbol)
            if history is not None and not history.empty:
                result = compute_stock_metrics(symbol, history, spy_history=spy_history)
                if result is not None:
                    metrics_list.append(result)

            if log_callback and (index == total_symbols or index % metric_progress_every == 0):
                elapsed = time.time() - metric_start_ts
                avg_seconds = elapsed / max(1, index)
                eta_text = _format_eta(int(avg_seconds * max(0, total_symbols - index)))
                percent = int((index / total_symbols) * 100) if total_symbols else 100
                log_callback(
                    f"Scanner metrics progress: {index}/{total_symbols} ({percent}%) - "
                    f"calculated={len(metrics_list)}, ETA {eta_text}"
                )

        if not metrics_list:
            if log_callback:
                log_callback("  No metrics calculated.")
            return []

        # Ranks
        growth_values_1m = [item.get("return_1m", 0.0) for item in metrics_list]
        ranks_1m = pd.Series(growth_values_1m).rank(pct=True, method="max") * 100
        for idx, item in enumerate(metrics_list):
            item["growth_rank"] = float(ranks_1m.iloc[idx])
            item["growth_rank_1m"] = float(ranks_1m.iloc[idx])

        growth_values_3m = [item.get("return_3m", 0.0) for item in metrics_list]
        ranks_3m = pd.Series(growth_values_3m).rank(pct=True, method="max") * 100
        for idx, item in enumerate(metrics_list):
            item["growth_rank_3m"] = float(ranks_3m.iloc[idx])

        save_total = len(metrics_list)
        save_start_ts = time.time()
        if log_callback:
            log_callback(f"Saving scanner metrics: 0/{save_total} (0%) - ETA calculating...")

        saved = save_scanner_metrics_snapshot_to_db(
            metrics_list,
            snapshot_date,
            input_fingerprint,
            engine,
            snapshot_symbols=symbols,
            strict=True,
        )
        snapshot_complete = (
            len(saved) == len(metrics_list)
            and is_scanner_metrics_snapshot_current(
                engine,
                symbols,
                history_watermarks=history_watermarks,
                snapshot_date=snapshot_date,
                strict=True,
            )
        )
        if log_callback:
            elapsed = time.time() - save_start_ts
            log_callback(
                f"Scanner metrics save progress: {save_total}/{save_total} (100%) - "
                f"saved={len(saved)}, ETA 00:00, elapsed={_format_elapsed(elapsed)}"
            )
                
        if log_callback:
            if snapshot_complete:
                log_callback(
                    f"  Successfully saved scanner metrics for {len(saved)} symbols."
                )
            else:
                log_callback("  Failed to commit a complete scanner metrics snapshot.")
        return saved if snapshot_complete else []
    except Exception as exc:
        if log_callback:
            log_callback(f"  Failed to save scanner metrics: {exc}")
        return []
