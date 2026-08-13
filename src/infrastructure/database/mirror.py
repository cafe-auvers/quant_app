"""P2 mirror extraction from the legacy database loader."""
from ._shared import *  # noqa: F401,F403

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
    symbol_filter: Optional[List[str]] = None,
    cancellation_callback: Optional[Callable[[], bool]] = None,
    row_progress_callback: Optional[Callable[[int], None]] = None,
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
    # Scan in the table's exact primary-key order.  Each logical partition's
    # rows still have a deterministic relative order even when multiple
    # partitions are interleaved, and ``states`` below already tracks them
    # independently.  Putting partition columns first forced MySQL to filesort
    # the complete daily/hourly tables because their PKs are respectively
    # (symbol, date, interval) and (symbol, timestamp, source).  On a remote PC
    # that sort could produce no socket data before PyMySQL's read timeout.
    order_names = list(spec.primary_key)
    base_stmt = select(*selected_columns)
    if raw_partition_keys is not None:
        if not raw_partition_keys:
            return {}
        base_stmt = base_stmt.where(
            _partition_filter(
                table, spec.partition_columns, raw_partition_keys
            )
        )
    symbol_chunks: List[Optional[List[str]]] = [None]
    if symbol_filter is not None:
        if "symbol" not in table.columns:
            raise ValueError(
                f"{spec.table_name} cannot be reconciled by symbol."
            )
        cleaned_symbols = list(
            dict.fromkeys(
                str(symbol).strip().upper()
                for symbol in symbol_filter
                if symbol is not None and str(symbol).strip()
            )
        )
        if not cleaned_symbols:
            return {}
        symbol_chunks = list(
            _record_chunks(cleaned_symbols, HOURLY_CACHE_QUERY_SYMBOL_CHUNK_SIZE)
        )
    statements = []
    for symbol_chunk in symbol_chunks:
        stmt = base_stmt
        if symbol_chunk is not None:
            stmt = stmt.where(table.c.symbol.in_(symbol_chunk))
        statements.append(
            stmt.order_by(*(table.c[name] for name in order_names))
        )

    states: Dict[
        Tuple[object, ...], Tuple[Tuple[object, ...], int, object]
    ] = {}
    boolean_indexes = [
        value_indexes[name]
        for name in ordered_names
        if name in _BOOLEAN_RECONCILE_COLUMNS
    ]
    rows_processed = 0

    def consume(conn) -> None:
        nonlocal rows_processed
        streaming_conn = conn.execution_options(stream_results=True)
        for stmt in statements:
            if cancellation_callback is not None and cancellation_callback():
                raise InterruptedError("Local mirror synchronization was cancelled.")
            result = streaming_conn.execute(stmt)
            for row_index, row in enumerate(result):
                if (
                    row_index % 1000 == 0
                    and cancellation_callback is not None
                    and cancellation_callback()
                ):
                    result.close()
                    raise InterruptedError(
                        "Local mirror synchronization was cancelled."
                    )
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
                rows_processed += 1
                if row_progress_callback is not None and rows_processed % 5000 == 0:
                    row_progress_callback(rows_processed)

    if connection is not None:
        consume(connection)
    else:
        with engine.connect() as conn:
            consume(conn)

    if row_progress_callback is not None:
        row_progress_callback(rows_processed)

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


def _scoped_partition_fingerprints(
    engine: Engine,
    spec: _ReconcileTableSpec,
    *,
    hourly_symbols: Optional[List[str]] = None,
    cancellation_callback: Optional[Callable[[], bool]] = None,
    row_progress_callback: Optional[Callable[[int], None]] = None,
    connection=None,
) -> Dict[Tuple[object, ...], _PartitionFingerprint]:
    """Limit expensive hourly comparisons while leaving every other table exact."""
    return _partition_fingerprints(
        engine,
        spec,
        symbol_filter=(
            hourly_symbols
            if spec.table_name == "hourly_price_history"
            else None
        ),
        cancellation_callback=cancellation_callback,
        row_progress_callback=row_progress_callback,
        connection=connection,
    )


def _scoped_reconcile_row_count(
    engine: Engine,
    spec: _ReconcileTableSpec,
    *,
    hourly_symbols: Optional[List[str]] = None,
    cancellation_callback: Optional[Callable[[], bool]] = None,
    connection=None,
) -> int:
    """Count the rows that an exact mirror pass will inspect."""
    bind = connection if connection is not None else engine
    if not inspect(bind).has_table(spec.table_name):
        return 0
    table = Table(spec.table_name, MetaData(), autoload_with=bind)
    symbol_chunks: List[Optional[List[str]]] = [None]
    if spec.table_name == "hourly_price_history" and hourly_symbols is not None:
        cleaned_symbols = list(
            dict.fromkeys(
                str(symbol).strip().upper()
                for symbol in hourly_symbols
                if symbol is not None and str(symbol).strip()
            )
        )
        if not cleaned_symbols:
            return 0
        symbol_chunks = list(
            _record_chunks(cleaned_symbols, HOURLY_CACHE_QUERY_SYMBOL_CHUNK_SIZE)
        )

    def count_rows(conn) -> int:
        total = 0
        for symbol_chunk in symbol_chunks:
            if cancellation_callback is not None and cancellation_callback():
                raise InterruptedError("Local mirror synchronization was cancelled.")
            stmt = select(func.count()).select_from(table)
            if symbol_chunk is not None:
                stmt = stmt.where(table.c.symbol.in_(symbol_chunk))
            total += int(conn.execute(stmt).scalar_one() or 0)
        return total

    if connection is not None:
        return count_rows(connection)
    with engine.connect() as conn:
        return count_rows(conn)


def _mirror_scope_hash(
    spec: _ReconcileTableSpec,
    hourly_symbols: Optional[List[str]],
) -> str:
    if spec.table_name != "hourly_price_history" or hourly_symbols is None:
        return "all"
    symbols = sorted(
        {
            str(symbol).strip().upper()
            for symbol in hourly_symbols
            if symbol is not None and str(symbol).strip()
        }
    )
    return hashlib.sha256("\n".join(symbols).encode("utf-8")).hexdigest()


def _scoped_symbol_chunks(
    spec: _ReconcileTableSpec,
    hourly_symbols: Optional[List[str]],
) -> List[Optional[List[str]]]:
    if spec.table_name != "hourly_price_history" or hourly_symbols is None:
        return [None]
    symbols = list(
        dict.fromkeys(
            str(symbol).strip().upper()
            for symbol in hourly_symbols
            if symbol is not None and str(symbol).strip()
        )
    )
    if not symbols:
        return []
    return list(_record_chunks(symbols, HOURLY_CACHE_QUERY_SYMBOL_CHUNK_SIZE))


def _scoped_table_signature(
    engine: Engine,
    spec: _ReconcileTableSpec,
    *,
    hourly_symbols: Optional[List[str]] = None,
    cancellation_callback: Optional[Callable[[], bool]] = None,
    connection=None,
) -> _MirrorTableSignature:
    """Return a cheap revision/count signature for one mirrored scope."""
    bind = connection if connection is not None else engine
    if not inspect(bind).has_table(spec.table_name):
        return _MirrorTableSignature(0, None)
    table = Table(spec.table_name, MetaData(), autoload_with=bind)
    symbol_chunks = _scoped_symbol_chunks(spec, hourly_symbols)
    if not symbol_chunks:
        return _MirrorTableSignature(0, None)

    def read_signature(conn) -> _MirrorTableSignature:
        row_count = 0
        max_revision: Optional[dt.datetime] = None
        for symbol_chunk in symbol_chunks:
            if cancellation_callback is not None and cancellation_callback():
                raise InterruptedError("Local mirror synchronization was cancelled.")
            stmt = select(
                func.count().label("row_count"),
                func.max(table.c[spec.revision_column]).label("max_revision"),
            ).select_from(table)
            if symbol_chunk is not None:
                stmt = stmt.where(table.c.symbol.in_(symbol_chunk))
            row = conn.execute(stmt).one()
            row_count += int(row.row_count or 0)
            revision = _normalized_watermark(row.max_revision)
            if revision is not None and (
                max_revision is None or revision > max_revision
            ):
                max_revision = revision
        return _MirrorTableSignature(row_count, max_revision)

    if connection is not None:
        return read_signature(connection)
    with engine.connect() as conn:
        return read_signature(conn)


def _load_mirror_sync_checkpoint(
    conn,
    spec: _ReconcileTableSpec,
    scope_hash: str,
) -> Optional[_MirrorSyncCheckpoint]:
    table = _local_mirror_sync_state_table(MetaData())
    row = conn.execute(
        select(table).where(
            table.c.table_name == spec.table_name,
            table.c.scope_hash == scope_hash,
        )
    ).first()
    if row is None:
        return None
    return _MirrorSyncCheckpoint(
        table_name=spec.table_name,
        scope_hash=scope_hash,
        signature=_MirrorTableSignature(
            int(row.pc_row_count or 0),
            _normalized_watermark(row.pc_max_revision),
        ),
        synced_at=_normalized_watermark(row.synced_at) or _utcnow_naive(),
    )


def _save_mirror_sync_checkpoint(
    conn,
    spec: _ReconcileTableSpec,
    scope_hash: str,
    signature: _MirrorTableSignature,
) -> None:
    table = _local_mirror_sync_state_table(MetaData())
    stmt = sqlite_insert(table).values(
        table_name=spec.table_name,
        scope_hash=scope_hash,
        pc_row_count=signature.row_count,
        pc_max_revision=signature.max_revision,
        synced_at=_utcnow_naive(),
    )
    conn.execute(
        stmt.on_conflict_do_update(
            index_elements=["table_name", "scope_hash"],
            set_={
                "pc_row_count": stmt.excluded.pc_row_count,
                "pc_max_revision": stmt.excluded.pc_max_revision,
                "synced_at": stmt.excluded.synced_at,
            },
        )
    )


def _scoped_changed_row_count(
    engine: Engine,
    spec: _ReconcileTableSpec,
    *,
    since_revision: Optional[dt.datetime],
    hourly_symbols: Optional[List[str]],
    cancellation_callback: Optional[Callable[[], bool]],
) -> int:
    table = Table(spec.table_name, MetaData(), autoload_with=engine)
    symbol_chunks = _scoped_symbol_chunks(spec, hourly_symbols)
    total = 0
    with engine.connect() as conn:
        for symbol_chunk in symbol_chunks:
            if cancellation_callback is not None and cancellation_callback():
                raise InterruptedError("Local mirror synchronization was cancelled.")
            stmt = select(func.count()).select_from(table)
            if since_revision is not None:
                stmt = stmt.where(
                    table.c[spec.revision_column] >= since_revision
                )
            if symbol_chunk is not None:
                stmt = stmt.where(table.c.symbol.in_(symbol_chunk))
            total += int(conn.execute(stmt).scalar_one() or 0)
    return total


def _copy_scoped_changed_rows_to_local(
    pc_engine: Engine,
    local_engine: Engine,
    local_conn,
    spec: _ReconcileTableSpec,
    *,
    since_revision: Optional[dt.datetime],
    hourly_symbols: Optional[List[str]],
    cancellation_callback: Optional[Callable[[], bool]],
    row_progress_callback: Optional[Callable[[int], None]],
) -> int:
    pc_table = Table(spec.table_name, MetaData(), autoload_with=pc_engine)
    local_table = Table(spec.table_name, MetaData(), autoload_with=local_conn)
    _validate_reconcile_table_schema(pc_table, local_table, spec)
    symbol_chunks = _scoped_symbol_chunks(spec, hourly_symbols)
    rows_written = 0
    with pc_engine.connect() as pc_conn:
        streaming_conn = pc_conn.execution_options(stream_results=True)
        for symbol_chunk in symbol_chunks:
            if cancellation_callback is not None and cancellation_callback():
                raise InterruptedError("Local mirror synchronization was cancelled.")
            stmt = select(pc_table)
            if since_revision is not None:
                stmt = stmt.where(
                    pc_table.c[spec.revision_column] >= since_revision
                )
            if symbol_chunk is not None:
                stmt = stmt.where(pc_table.c.symbol.in_(symbol_chunk))
            result = streaming_conn.execute(stmt)
            while True:
                if cancellation_callback is not None and cancellation_callback():
                    result.close()
                    raise InterruptedError(
                        "Local mirror synchronization was cancelled."
                    )
                batch = result.fetchmany(1000)
                if not batch:
                    break
                records = [
                    {
                        name: value
                        for name, value in dict(row._mapping).items()
                        if name in local_table.columns
                    }
                    for row in batch
                ]
                _execute_bulk_upsert(
                    local_conn,
                    local_table,
                    records,
                    spec.primary_key,
                    local_engine.dialect.name,
                )
                rows_written += len(records)
                if row_progress_callback is not None:
                    row_progress_callback(rows_written)
            result.close()
    if row_progress_callback is not None:
        row_progress_callback(rows_written)
    return rows_written


def _scoped_partition_revision_summaries(
    engine: Engine,
    spec: _ReconcileTableSpec,
    *,
    hourly_symbols: Optional[List[str]],
    cancellation_callback: Optional[Callable[[], bool]],
    connection=None,
) -> Dict[Tuple[object, ...], _PartitionFingerprint]:
    """Summarize partitions in SQL so only changed partitions need row reads."""
    bind = connection if connection is not None else engine
    table = Table(spec.table_name, MetaData(), autoload_with=bind)
    partition_columns = [table.c[name] for name in spec.partition_columns]
    symbol_chunks = _scoped_symbol_chunks(spec, hourly_symbols)

    def read_summaries(conn) -> Dict[Tuple[object, ...], _PartitionFingerprint]:
        summaries: Dict[Tuple[object, ...], _PartitionFingerprint] = {}
        for symbol_chunk in symbol_chunks:
            if cancellation_callback is not None and cancellation_callback():
                raise InterruptedError("Local mirror synchronization was cancelled.")
            stmt = select(
                *partition_columns,
                func.count().label("row_count"),
                func.max(table.c[spec.revision_column]).label("max_revision"),
            ).group_by(*partition_columns)
            if symbol_chunk is not None:
                stmt = stmt.where(table.c.symbol.in_(symbol_chunk))
            for row in conn.execute(stmt):
                raw_key = tuple(row[index] for index in range(len(partition_columns)))
                normalized_key = tuple(
                    (
                        _normalized_watermark(value).isoformat(timespec="microseconds")
                        if isinstance(value, (dt.datetime, pd.Timestamp))
                        else value
                    )
                    for value in raw_key
                )
                revision = _normalized_watermark(row.max_revision)
                digest = revision.isoformat(timespec="microseconds") if revision else ""
                summaries[normalized_key] = _PartitionFingerprint(
                    raw_key,
                    int(row.row_count or 0),
                    digest,
                )
        return summaries

    if connection is not None:
        return read_summaries(connection)
    with engine.connect() as conn:
        return read_summaries(conn)


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
    cancellation_callback: Optional[Callable[[], bool]] = None,
    row_progress_callback: Optional[Callable[[int], None]] = None,
) -> int:
    """Apply PC-wins rows and remove local-only keys for changed partitions."""
    if not partition_keys:
        return 0
    pc_table = Table(spec.table_name, MetaData(), autoload_with=pc_engine)
    local_bind = local_connection if local_connection is not None else local_engine
    local_table = Table(spec.table_name, MetaData(), autoload_with=local_bind)
    _validate_reconcile_table_schema(pc_table, local_table, spec)
    changed = 0
    rows_processed = 0

    for key_chunk in _record_chunks(partition_keys, partition_chunk_size):
        if cancellation_callback is not None and cancellation_callback():
            raise InterruptedError("Local mirror synchronization was cancelled.")
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
        rows_processed += len(pc_rows) + len(local_rows)
        if row_progress_callback is not None:
            row_progress_callback(rows_processed)
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
    if row_progress_callback is not None:
        row_progress_callback(rows_processed)
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
    hourly_symbols: Optional[List[str]],
    verify_derived: bool,
    pc_authoritative: bool,
    progress_callback: Optional[Callable[[str, int, int], None]],
    cancellation_callback: Optional[Callable[[], bool]],
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

    total_work = 0
    completed_work = 0

    def check_cancelled() -> None:
        if cancellation_callback is not None and cancellation_callback():
            raise InterruptedError("Local mirror synchronization was cancelled.")

    def start_phase(label: str) -> None:
        check_cancelled()
        if progress_callback is not None:
            progress_callback(label, completed_work, total_work)

    def report_phase_progress(label: str, current: int) -> None:
        check_cancelled()
        if progress_callback is not None:
            progress_callback(label, min(max(0, current), total_work), total_work)

    def scan_fingerprints(
        engine: Engine,
        spec: _ReconcileTableSpec,
        label: str,
        units: int,
        *,
        connection=None,
    ) -> Dict[Tuple[object, ...], _PartitionFingerprint]:
        nonlocal completed_work
        start = completed_work
        start_phase(label)
        result = _scoped_partition_fingerprints(
            engine,
            spec,
            hourly_symbols=hourly_symbols,
            cancellation_callback=cancellation_callback,
            row_progress_callback=lambda rows: report_phase_progress(
                label, start + min(max(0, int(rows)), units)
            ),
            connection=connection,
        )
        completed_work = start + units
        report_phase_progress(label, completed_work)
        return result

    local_conn = None
    try:
        start_phase("Preparing laptop safety backup")
        with pc_engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        _ensure_local_mirror_handoff_tracking(local_engine)
        local_conn = local_engine.connect()
        local_conn.exec_driver_sql("BEGIN IMMEDIATE")
        handoff = _local_mirror_handoff_table(MetaData())
        dirty = local_conn.execute(
            select(handoff.c.dirty).where(handoff.c.id == 1)
        ).scalar_one()
        if bool(dirty) and not pc_authoritative:
            raise LocalMirrorNeedsReconciliationError(
                "Local mirror contains changes that require staged reconciliation."
            )

        # Equal or empty legacy tables must not bypass schema validation.
        for spec in reconcile_specs:
            start_phase(f"Checking table layout: {spec.table_name}")
            pc_table = Table(
                spec.table_name, MetaData(), autoload_with=pc_engine
            )
            local_table = Table(
                spec.table_name, MetaData(), autoload_with=local_conn
            )
            _validate_reconcile_table_schema(pc_table, local_table, spec)

        pc_row_counts: Dict[str, int] = {}
        local_row_counts: Dict[str, int] = {}
        for spec in reconcile_specs:
            start_phase(f"Counting backup records: {spec.table_name}")
            pc_row_counts[spec.table_name] = _scoped_reconcile_row_count(
                pc_engine,
                spec,
                hourly_symbols=hourly_symbols,
                cancellation_callback=cancellation_callback,
            )
            local_row_counts[spec.table_name] = _scoped_reconcile_row_count(
                local_engine,
                spec,
                hourly_symbols=hourly_symbols,
                cancellation_callback=cancellation_callback,
                connection=local_conn,
            )

        # Each PC row is read initially, during the update/copy decision,
        # when verifying the laptop copy, and once more to prove the PC did
        # not change. Each existing laptop row is read initially and during
        # the update/copy decision. Derived verification adds one PC reread.
        pc_passes = 5 if verify_derived else 4
        total_work = (
            pc_passes * sum(pc_row_counts.values())
            + 2 * sum(local_row_counts.values())
        )
        start_phase("Starting record comparison")

        pc_before = {}
        for spec in reconcile_specs:
            pc_before[spec.table_name] = scan_fingerprints(
                pc_engine,
                spec,
                f"Reading PC data: {spec.table_name}",
                pc_row_counts[spec.table_name],
            )
        if verify_derived:
            start_phase("Checking PC derived-data freshness")
            _verify_pc_derived_data_current_read_only(pc_engine, tickers)
            pc_after_derived_check = {}
            for spec in reconcile_specs:
                pc_after_derived_check[spec.table_name] = (
                    scan_fingerprints(
                        pc_engine,
                        spec,
                        f"Rechecking PC data: {spec.table_name}",
                        pc_row_counts[spec.table_name],
                    )
                )
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

        local_before = {}
        for spec in reconcile_specs:
            local_before[spec.table_name] = scan_fingerprints(
                local_engine,
                spec,
                f"Reading laptop backup: {spec.table_name}",
                local_row_counts[spec.table_name],
                connection=local_conn,
            )
        pending_written: Dict[str, int] = {}
        for spec in reconcile_specs:
            label = f"Updating laptop backup: {spec.table_name}"
            start = completed_work
            phase_units = (
                pc_row_counts[spec.table_name]
                + local_row_counts[spec.table_name]
            )
            start_phase(label)
            mismatched = _mismatched_partitions(
                pc_before[spec.table_name], local_before[spec.table_name]
            )
            mismatched_units = sum(
                (
                    pc_before[spec.table_name].get(key).row_count
                    if pc_before[spec.table_name].get(key) is not None
                    else 0
                )
                + (
                    local_before[spec.table_name].get(key).row_count
                    if local_before[spec.table_name].get(key) is not None
                    else 0
                )
                for key in mismatched
            )
            mismatch_work = min(phase_units, mismatched_units)
            already_matched = phase_units - mismatch_work
            report_phase_progress(label, start + already_matched)
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
                    cancellation_callback=cancellation_callback,
                    row_progress_callback=lambda rows: report_phase_progress(
                        label,
                        start
                        + already_matched
                        + min(max(0, int(rows)), mismatch_work),
                    ),
                )
            )
            completed_work = start + phase_units
            report_phase_progress(label, completed_work)

        local_after = {}
        for spec in reconcile_specs:
            local_after[spec.table_name] = scan_fingerprints(
                local_engine,
                spec,
                f"Verifying laptop backup: {spec.table_name}",
                pc_row_counts[spec.table_name],
                connection=local_conn,
            )
        pc_after = {}
        for spec in reconcile_specs:
            pc_after[spec.table_name] = scan_fingerprints(
                pc_engine,
                spec,
                f"Confirming PC unchanged: {spec.table_name}",
                pc_row_counts[spec.table_name],
            )
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

        start_phase("Finalizing laptop safety backup")
        _mark_local_mirror_handoff_clean(local_conn)
        local_conn.commit()
        if progress_callback is not None:
            progress_callback("Laptop safety backup complete", total_work, total_work)
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
    hourly_symbols: Optional[List[str]] = None,
    verify_derived: bool = True,
    pc_authoritative: bool = False,
    progress_callback: Optional[Callable[[str, int, int], None]] = None,
    cancellation_callback: Optional[Callable[[], bool]] = None,
) -> Dict[str, int]:
    """Publish an exact PC generation to the local mirror atomically.

    All keys and values are compared and every local change shares one SQLite
    transaction. With ``pc_authoritative=True``, tracked laptop writes do not
    block the backup: PC partitions replace the scoped local cache and nothing
    is written back to MySQL. Readers see the old
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
        hourly_symbols=hourly_symbols,
        verify_derived=verify_derived,
        pc_authoritative=pc_authoritative,
        progress_callback=progress_callback,
        cancellation_callback=cancellation_callback,
    )


def sync_local_mirror_from_pc_checkpointed(
    pc_engine: Engine,
    local_engine: Engine,
    tables: Tuple[Tuple[str, str], ...] = MIRRORED_TABLES,
    *,
    hourly_symbols: Optional[List[str]] = None,
    progress_callback: Optional[Callable[[str, int, int], None]] = None,
    cancellation_callback: Optional[Callable[[], bool]] = None,
) -> Dict[str, int]:
    """Incrementally maintain the disposable laptop safety copy.

    A successful run stores a per-table PC revision/count checkpoint in the
    same SQLite transaction as the copied rows. Unchanged restarts therefore
    need only small aggregate queries. Changed rows are replayed by revision;
    count/revision mismatches use SQL partition summaries and exact-copy only
    the affected partitions, which also propagates deletions.
    """
    if local_engine is None or local_engine.dialect.name != "sqlite":
        raise RuntimeError("An SQLite local mirror is required for checkpointed sync.")

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
            "No incremental reconciliation policy for: "
            + ", ".join(sorted(unsupported))
        )

    def check_cancelled() -> None:
        if cancellation_callback is not None and cancellation_callback():
            raise InterruptedError("Local mirror synchronization was cancelled.")

    def report(label: str, current: int = 0, total: int = 0) -> None:
        check_cancelled()
        if progress_callback is not None:
            progress_callback(label, max(0, int(current)), max(0, int(total)))

    _ensure_local_mirror_handoff_tracking(local_engine)
    _ensure_local_mirror_sync_state(local_engine)
    local_conn = None
    try:
        report("Checking laptop backup checkpoint")
        with pc_engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        local_conn = local_engine.connect()
        local_conn.exec_driver_sql("BEGIN IMMEDIATE")
        handoff = _local_mirror_handoff_table(MetaData())
        local_dirty = bool(
            local_conn.execute(
                select(handoff.c.dirty).where(handoff.c.id == 1)
            ).scalar_one()
        )

        for spec in reconcile_specs:
            report(f"Checking table layout: {spec.table_name}")
            pc_table = Table(spec.table_name, MetaData(), autoload_with=pc_engine)
            local_table = Table(
                spec.table_name, MetaData(), autoload_with=local_conn
            )
            _validate_reconcile_table_schema(pc_table, local_table, spec)

        pc_before: Dict[str, _MirrorTableSignature] = {}
        checkpoints: Dict[str, Optional[_MirrorSyncCheckpoint]] = {}
        scope_hashes: Dict[str, str] = {}
        changed_specs: List[_ReconcileTableSpec] = []
        for spec in reconcile_specs:
            report(f"Checking PC changes: {spec.table_name}")
            scope_hash = _mirror_scope_hash(spec, hourly_symbols)
            scope_hashes[spec.table_name] = scope_hash
            signature = _scoped_table_signature(
                pc_engine,
                spec,
                hourly_symbols=hourly_symbols,
                cancellation_callback=cancellation_callback,
            )
            pc_before[spec.table_name] = signature
            checkpoint = _load_mirror_sync_checkpoint(
                local_conn, spec, scope_hash
            )
            checkpoints[spec.table_name] = checkpoint
            if (
                not local_dirty
                and checkpoint is not None
                and checkpoint.signature == signature
            ):
                continue

            # This also seeds a checkpoint after an older exact-sync version:
            # a clean handoff plus matching local/PC revision and count means
            # the existing safety copy can be adopted without another audit.
            report(f"Checking laptop changes: {spec.table_name}")
            local_signature = _scoped_table_signature(
                local_engine,
                spec,
                hourly_symbols=hourly_symbols,
                cancellation_callback=cancellation_callback,
                connection=local_conn,
            )
            if local_signature != signature:
                changed_specs.append(spec)

        written = {spec.table_name: 0 for spec in reconcile_specs}
        if not changed_specs:
            for spec in reconcile_specs:
                _save_mirror_sync_checkpoint(
                    local_conn,
                    spec,
                    scope_hashes[spec.table_name],
                    pc_before[spec.table_name],
                )
            _mark_local_mirror_handoff_clean(local_conn)
            local_conn.commit()
            if progress_callback is not None:
                progress_callback("Laptop safety backup already up to date", 1, 1)
            return written

        changed_row_counts: Dict[str, int] = {}
        since_revisions: Dict[str, Optional[dt.datetime]] = {}
        for spec in changed_specs:
            checkpoint = checkpoints[spec.table_name]
            since_revision = (
                checkpoint.signature.max_revision
                if checkpoint is not None
                else None
            )
            since_revisions[spec.table_name] = since_revision
            report(f"Counting changed PC rows: {spec.table_name}")
            changed_row_counts[spec.table_name] = _scoped_changed_row_count(
                pc_engine,
                spec,
                since_revision=since_revision,
                hourly_symbols=hourly_symbols,
                cancellation_callback=cancellation_callback,
            )

        total_work = sum(changed_row_counts.values())
        completed_work = 0
        for spec in changed_specs:
            label = f"Copying changed PC data: {spec.table_name}"
            phase_start = completed_work
            phase_total = changed_row_counts[spec.table_name]
            report(label, completed_work, total_work)
            copied = _copy_scoped_changed_rows_to_local(
                pc_engine,
                local_engine,
                local_conn,
                spec,
                since_revision=since_revisions[spec.table_name],
                hourly_symbols=hourly_symbols,
                cancellation_callback=cancellation_callback,
                row_progress_callback=lambda rows: report(
                    label,
                    phase_start + min(max(0, int(rows)), phase_total),
                    total_work,
                ),
            )
            written[spec.table_name] += copied
            completed_work = phase_start + phase_total
            report(label, completed_work, total_work)

            report(
                f"Verifying incremental backup: {spec.table_name}",
                completed_work,
                total_work,
            )
            pc_after_copy = _scoped_table_signature(
                pc_engine,
                spec,
                hourly_symbols=hourly_symbols,
                cancellation_callback=cancellation_callback,
            )
            if pc_after_copy != pc_before[spec.table_name]:
                raise RuntimeError(
                    f"PC {spec.table_name} changed during incremental backup."
                )
            local_after_copy = _scoped_table_signature(
                local_engine,
                spec,
                hourly_symbols=hourly_symbols,
                cancellation_callback=cancellation_callback,
                connection=local_conn,
            )
            if local_after_copy == pc_after_copy:
                continue

            report(
                f"Finding changed partitions: {spec.table_name}",
                completed_work,
                total_work,
            )
            pc_partitions = _scoped_partition_revision_summaries(
                pc_engine,
                spec,
                hourly_symbols=hourly_symbols,
                cancellation_callback=cancellation_callback,
            )
            local_partitions = _scoped_partition_revision_summaries(
                local_engine,
                spec,
                hourly_symbols=hourly_symbols,
                cancellation_callback=cancellation_callback,
                connection=local_conn,
            )
            mismatched = _mismatched_partitions(pc_partitions, local_partitions)
            partition_work = sum(
                (
                    pc_partitions.get(key).row_count
                    if pc_partitions.get(key) is not None
                    else 0
                )
                + (
                    local_partitions.get(key).row_count
                    if local_partitions.get(key) is not None
                    else 0
                )
                for key in mismatched
            )
            total_work += partition_work
            label = f"Reconciling changed partitions: {spec.table_name}"
            phase_start = completed_work
            report(label, completed_work, total_work)
            changed = _copy_pc_partitions_to_local_exactly(
                pc_engine,
                local_engine,
                spec,
                mismatched,
                pc_partitions,
                local_partitions,
                local_connection=local_conn,
                preserve_local_raw=False,
                cancellation_callback=cancellation_callback,
                row_progress_callback=lambda rows: report(
                    label,
                    phase_start + min(max(0, int(rows)), partition_work),
                    total_work,
                ),
            )
            written[spec.table_name] += changed
            completed_work += partition_work
            local_after_copy = _scoped_table_signature(
                local_engine,
                spec,
                hourly_symbols=hourly_symbols,
                cancellation_callback=cancellation_callback,
                connection=local_conn,
            )
            if local_after_copy != pc_after_copy:
                raise RuntimeError(
                    f"Laptop {spec.table_name} did not match the PC after "
                    "targeted reconciliation."
                )

        final_signatures: Dict[str, _MirrorTableSignature] = {}
        for spec in reconcile_specs:
            report(
                f"Confirming PC checkpoint: {spec.table_name}",
                completed_work,
                total_work,
            )
            final_signature = _scoped_table_signature(
                pc_engine,
                spec,
                hourly_symbols=hourly_symbols,
                cancellation_callback=cancellation_callback,
            )
            if final_signature != pc_before[spec.table_name]:
                raise RuntimeError(
                    f"PC {spec.table_name} changed before checkpoint commit."
                )
            final_signatures[spec.table_name] = final_signature

        for spec in reconcile_specs:
            _save_mirror_sync_checkpoint(
                local_conn,
                spec,
                scope_hashes[spec.table_name],
                final_signatures[spec.table_name],
            )
        _mark_local_mirror_handoff_clean(local_conn)
        local_conn.commit()
        final_total = max(total_work, completed_work, 1)
        if progress_callback is not None:
            progress_callback("Laptop safety backup complete", final_total, final_total)
        return written
    except Exception as exc:
        if local_conn is not None:
            try:
                local_conn.rollback()
            except Exception:
                pass
        raise RuntimeError(
            f"Checkpointed PC -> local mirror sync failed: {exc}"
        ) from exc
    finally:
        if local_conn is not None:
            local_conn.close()

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
    hourly_symbols: Optional[List[str]] = None,
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
            pc_fingerprints = _scoped_partition_fingerprints(
                pc_engine, spec, hourly_symbols=hourly_symbols
            )
            local_fingerprints = _scoped_partition_fingerprints(
                local_engine, spec, hourly_symbols=hourly_symbols
            )
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
                raw_pc_before_derived[spec.table_name] = (
                    _scoped_partition_fingerprints(
                        pc_engine, spec, hourly_symbols=hourly_symbols
                    )
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
                canonical_pc_before[spec.table_name] = (
                    _scoped_partition_fingerprints(
                        pc_engine, spec, hourly_symbols=hourly_symbols
                    )
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
                spec.table_name: _scoped_partition_fingerprints(
                    local_engine,
                    spec,
                    hourly_symbols=hourly_symbols,
                    connection=local_conn,
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
                spec.table_name: _scoped_partition_fingerprints(
                    local_engine,
                    spec,
                    hourly_symbols=hourly_symbols,
                    connection=local_conn,
                )
                for spec in reconcile_specs
            }
            canonical_pc_after = {
                spec.table_name: _scoped_partition_fingerprints(
                    pc_engine, spec, hourly_symbols=hourly_symbols
                )
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


def local_mirror_hourly_is_stale(
    engine: Engine,
    expected_date: dt.date,
    tickers: Optional[List[str]],
) -> bool:
    """True when actionable 1-hour history is missing or behind.

    The laptop startup prompt uses this alongside ``local_mirror_is_stale`` so
    current daily rows cannot hide a failed hourly refresh. As in the PC's
    scheduled gate, chronically unavailable symbols are ignored while SPY
    remains the provider canary.
    """
    if isinstance(expected_date, dt.datetime):
        expected_date = expected_date.date()
    if tickers is None:
        return True

    symbols = _clean_symbols([REFERENCE_SYMBOL, *tickers])
    try:
        latest_by_symbol = get_latest_hourly_price_history_timestamps(
            engine, symbols, strict=True
        )
        chronic = get_chronically_failing_symbols(engine, interval="1h")
    except (RuntimeError, SQLAlchemyError, ValueError, TypeError):
        return True

    reference_latest = latest_by_symbol.get(REFERENCE_SYMBOL)
    reference_date = (
        reference_latest.date()
        if hasattr(reference_latest, "date")
        else reference_latest
    )
    if reference_date is None or reference_date < expected_date:
        return True

    for symbol in symbols:
        if symbol == REFERENCE_SYMBOL or symbol in chronic:
            continue
        latest = latest_by_symbol.get(symbol)
        latest_date = latest.date() if hasattr(latest, "date") else latest
        if latest_date is None or latest_date < expected_date:
            return True
    return False
