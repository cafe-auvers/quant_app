import datetime as dt
from types import SimpleNamespace

import pytest
from sqlalchemy import (
    Column,
    DateTime,
    Float,
    MetaData,
    String,
    Table,
    create_engine,
    delete,
    event,
    insert,
    select,
    update,
)

import src.utils.db_loader as db_loader


def _source_table(engine, table_name="bars"):
    metadata = MetaData()
    table = Table(
        table_name,
        metadata,
        Column("symbol", String(20), primary_key=True),
        Column("stamp", DateTime, primary_key=True),
        Column("value", String(20)),
    )
    metadata.create_all(engine)
    return table


def _bar_rows():
    return [
        {
            "symbol": symbol,
            "stamp": dt.datetime(2026, 1, day),
            "value": f"{symbol}{day}",
        }
        for symbol in ("A", "B", "C")
        for day in range(1, 4)
    ]


def _read_rows(engine, table_name="bars"):
    table = Table(table_name, MetaData(), autoload_with=engine)
    with engine.connect() as conn:
        return [
            tuple(row)
            for row in conn.execute(
                select(table).order_by(table.c.symbol, table.c.stamp)
            ).all()
        ]


def _daily_bar(symbol, day, close, *, interval="1d"):
    close = float(close)
    return {
        "symbol": symbol,
        "date": dt.datetime(2026, 1, day),
        "interval": interval,
        "open": close,
        "high": close + 1.0,
        "low": close - 1.0,
        "close": close,
        "adj_close": close,
        "volume": 1000.0,
        "updated_at": dt.datetime(2026, 1, day, 22),
    }


def _hourly_bar(symbol, hour, close, *, source="yfinance"):
    close = float(close)
    return {
        "symbol": symbol,
        "timestamp": dt.datetime(2026, 1, 2, hour),
        "source": source,
        "open": close,
        "high": close + 1.0,
        "low": close - 1.0,
        "close": close,
        "adj_close": close,
        "volume": 1000.0,
        "updated_at": dt.datetime(2026, 1, 2, hour, 5),
    }


def _raw_reconciliation_tables():
    return (
        ("price_history", "date"),
        ("hourly_price_history", "timestamp"),
    )


def _raw_engines():
    pc = create_engine("sqlite:///:memory:", future=True)
    local = create_engine("sqlite:///:memory:", future=True)
    return (
        pc,
        local,
        db_loader._ensure_price_history_table(pc),
        db_loader._ensure_price_history_table(local),
        db_loader._ensure_hourly_price_history_table(pc),
        db_loader._ensure_hourly_price_history_table(local),
    )


@pytest.mark.parametrize(
    ("table_name", "expected_order"),
    [
        (
            "price_history",
            "order by price_history.symbol, price_history.date, price_history.interval",
        ),
        (
            "hourly_price_history",
            "order by hourly_price_history.symbol, hourly_price_history.timestamp, hourly_price_history.source",
        ),
    ],
)
def test_full_fingerprint_scan_uses_primary_key_order(table_name, expected_order):
    engine = create_engine("sqlite:///:memory:", future=True)
    if table_name == "price_history":
        db_loader._ensure_price_history_table(engine)
    else:
        db_loader._ensure_hourly_price_history_table(engine)
    spec = next(
        item
        for item in db_loader._RECONCILE_TABLE_SPECS
        if item.table_name == table_name
    )
    statements = []

    def capture_statement(_conn, _cursor, statement, _parameters, _context, _many):
        normalized = statement.lower().replace('"', "").replace("`", "")
        if f"from {table_name}" in normalized and "order by" in normalized:
            statements.append(normalized)

    event.listen(engine, "before_cursor_execute", capture_statement)
    try:
        db_loader._partition_fingerprints(engine, spec)
    finally:
        event.remove(engine, "before_cursor_execute", capture_statement)
        engine.dispose()

    assert len(statements) == 1
    assert expected_order in statements[0]


def test_resolve_data_engine_prefers_pc_and_falls_back_to_local(monkeypatch):
    pc_engine = object()
    local_engine = object()

    monkeypatch.setattr(db_loader, "init_mysql_engine", lambda: pc_engine)
    monkeypatch.setattr(
        db_loader,
        "init_local_mirror_engine",
        lambda: (_ for _ in ()).throw(AssertionError("local should not open")),
    )
    resolution = db_loader.resolve_data_engine()
    assert resolution.engine is pc_engine
    assert resolution.pc_engine is pc_engine
    assert resolution.source == "pc"

    monkeypatch.setattr(db_loader, "init_mysql_engine", lambda: None)
    monkeypatch.setattr(db_loader, "init_local_mirror_engine", lambda: local_engine)
    resolution = db_loader.resolve_data_engine()
    assert resolution.engine is local_engine
    assert resolution.pc_engine is None
    assert resolution.source == "local_mirror"


def test_resolve_data_engine_can_disable_laptop_only_fallback(monkeypatch):
    monkeypatch.setenv(db_loader.LOCAL_MIRROR_ENABLED_ENV, "0")
    monkeypatch.setattr(db_loader, "init_mysql_engine", lambda: None)
    monkeypatch.setattr(
        db_loader,
        "init_local_mirror_engine",
        lambda: (_ for _ in ()).throw(AssertionError("local should not open")),
    )

    resolution = db_loader.resolve_data_engine()

    assert resolution.engine is None
    assert resolution.pc_engine is None
    assert resolution.source == "none"


def test_init_local_mirror_engine_accepts_string_path_and_configures_sqlite(tmp_path):
    engine = db_loader.init_local_mirror_engine(str(tmp_path / "mirror.db"))
    assert engine is not None
    try:
        with engine.connect() as conn:
            journal_mode = conn.exec_driver_sql("PRAGMA journal_mode").scalar_one()
            busy_timeout = conn.exec_driver_sql("PRAGMA busy_timeout").scalar_one()
        assert journal_mode.lower() == "wal"
        assert busy_timeout == 30_000
        assert "price_history" in set(db_loader.inspect(engine).get_table_names())
    finally:
        engine.dispose()


def test_mirror_sync_replays_latest_watermark_updates():
    source = create_engine("sqlite:///:memory:", future=True)
    local = create_engine("sqlite:///:memory:", future=True)
    table = _source_table(source)
    with source.begin() as conn:
        conn.execute(insert(table), _bar_rows())

    db_loader.sync_mirror_table(source, local, "bars", "stamp", chunk_size=2)
    with source.begin() as conn:
        conn.execute(
            update(table)
            .where(
                table.c.symbol == "C",
                table.c.stamp == dt.datetime(2026, 1, 3),
            )
            .values(value="corrected")
        )

    db_loader.sync_mirror_table(source, local, "bars", "stamp", chunk_size=2)

    assert _read_rows(local)[-1] == (
        "C",
        dt.datetime(2026, 1, 3),
        "corrected",
    )


def test_mirror_sync_resume_does_not_skip_rows_after_interruption(monkeypatch):
    source = create_engine("sqlite:///:memory:", future=True)
    local = create_engine("sqlite:///:memory:", future=True)
    table = _source_table(source)
    rows = _bar_rows()
    with source.begin() as conn:
        conn.execute(insert(table), rows)

    original_upsert = db_loader._execute_bulk_upsert
    call_count = 0

    def interrupt_second_batch(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise RuntimeError("interrupted")
        return original_upsert(*args, **kwargs)

    monkeypatch.setattr(db_loader, "_execute_bulk_upsert", interrupt_second_batch)
    with pytest.raises(RuntimeError, match="interrupted"):
        db_loader.sync_mirror_table(
            source, local, "bars", "stamp", chunk_size=2
        )

    monkeypatch.setattr(db_loader, "_execute_bulk_upsert", original_upsert)
    db_loader.sync_mirror_table(source, local, "bars", "stamp", chunk_size=2)

    assert len(_read_rows(local)) == len(rows)


def test_sync_continues_after_one_table_fails_and_reports_the_error():
    source = create_engine("sqlite:///:memory:", future=True)
    local = create_engine("sqlite:///:memory:", future=True)
    table = _source_table(source)
    with source.begin() as conn:
        conn.execute(insert(table), _bar_rows()[:1])
    errors = []

    written = db_loader.sync_local_mirror_from_pc(
        source,
        local,
        tables=(("missing", "stamp"), ("bars", "stamp")),
        error_callback=errors.append,
    )

    assert written == {"bars": 1}
    assert len(errors) == 1
    assert "missing" in errors[0]


def test_atomic_mirror_sync_copies_old_backfill_below_local_revision_max():
    pc, local, pc_daily, local_daily, _pc_hourly, _local_hourly = _raw_engines()
    latest_logical_row = _daily_bar("A", 10, 100)
    old_backfill_written_later = _daily_bar("B", 1, 200)
    old_backfill_written_later["updated_at"] = dt.datetime(2026, 1, 9, 22)
    with pc.begin() as conn:
        conn.execute(insert(pc_daily), latest_logical_row)
    with local.begin() as conn:
        conn.execute(insert(local_daily), latest_logical_row)
    clean = db_loader.reconcile_local_mirror_with_pc(
        pc,
        local,
        tables=(("price_history", "date"),),
        verify_derived=False,
    )
    assert clean.success is True
    with pc.begin() as conn:
        conn.execute(insert(pc_daily), old_backfill_written_later)

    written = db_loader.sync_local_mirror_from_pc_atomic(
        pc,
        local,
        tables=(("price_history", "date"),),
        verify_derived=False,
    )

    assert written == {"price_history": 1}
    with local.connect() as conn:
        assert conn.execute(
            select(local_daily.c.symbol).order_by(local_daily.c.symbol)
        ).scalars().all() == ["A", "B"]


def test_atomic_mirror_sync_applies_pc_deletions():
    pc = create_engine("sqlite:///:memory:", future=True)
    local = create_engine("sqlite:///:memory:", future=True)
    pc_table = db_loader._ensure_chart_indicators_table(pc)
    local_table = db_loader._ensure_chart_indicators_table(local)
    rows = [
        {
            "symbol": symbol,
            "date": dt.datetime(2026, 1, 2),
            "relative_strength": 1.0,
            "updated_at": dt.datetime(2026, 1, 2, 22),
        }
        for symbol in ("A", "B")
    ]
    with pc.begin() as conn:
        conn.execute(insert(pc_table), rows)
    with local.begin() as conn:
        conn.execute(insert(local_table), rows)
    clean = db_loader.reconcile_local_mirror_with_pc(
        pc,
        local,
        tables=(("chart_indicators", "date"),),
        verify_derived=False,
    )
    assert clean.success is True
    with pc.begin() as conn:
        conn.execute(delete(pc_table).where(pc_table.c.symbol == "B"))

    written = db_loader.sync_local_mirror_from_pc_atomic(
        pc,
        local,
        tables=(("chart_indicators", "date"),),
        verify_derived=False,
    )

    assert written == {"chart_indicators": 1}
    with local.connect() as conn:
        assert conn.execute(select(local_table.c.symbol)).scalars().all() == ["A"]


def test_atomic_mirror_sync_applies_clean_pc_raw_deletion():
    pc, local, pc_daily, local_daily, _pc_hourly, _local_hourly = _raw_engines()
    rows = [_daily_bar("A", 1, 100), _daily_bar("B", 2, 200)]
    for engine, table in ((pc, pc_daily), (local, local_daily)):
        with engine.begin() as conn:
            conn.execute(insert(table), rows)
    clean = db_loader.reconcile_local_mirror_with_pc(
        pc,
        local,
        tables=(("price_history", "date"),),
        verify_derived=False,
    )
    assert clean.success is True

    # Since local remained clean, B is local-only only because the PC deleted
    # it. The periodic PC-canonical copy must propagate that deletion.
    with pc.begin() as conn:
        conn.execute(delete(pc_daily).where(pc_daily.c.symbol == "B"))

    written = db_loader.sync_local_mirror_from_pc_atomic(
        pc,
        local,
        tables=(("price_history", "date"),),
        verify_derived=False,
    )

    assert written == {"price_history": 1}
    with local.connect() as conn:
        assert conn.execute(select(local_daily.c.symbol)).scalars().all() == ["A"]


def test_atomic_mirror_sync_verifier_failure_does_not_change_pc():
    pc, local, pc_daily, local_daily, _pc_hourly, _local_hourly = _raw_engines()
    shared = [_daily_bar("SPY", 1, 100), _daily_bar("A", 1, 50)]
    for engine, table in ((pc, pc_daily), (local, local_daily)):
        with engine.begin() as conn:
            conn.execute(insert(table), shared)
    clean = db_loader.reconcile_local_mirror_with_pc(
        pc,
        local,
        tables=(("price_history", "date"),),
        verify_derived=False,
    )
    assert clean.success is True
    # The empty manifest makes the strict active-PC verifier reject this
    # generation. Creating it before the SQL listener is intentional: schema
    # setup is not part of the periodic, source-read-only path.
    db_loader._ensure_chart_indicator_manifests_table(pc)
    with pc.begin() as conn:
        conn.execute(insert(pc_daily), _daily_bar("B", 2, 200))

    pc_mutations = []

    def capture_pc_mutations(
        _conn, _cursor, statement, _parameters, _context, _executemany
    ):
        verb = statement.lstrip().split(None, 1)[0].upper()
        if verb in {"INSERT", "UPDATE", "DELETE", "CREATE", "DROP", "ALTER"}:
            pc_mutations.append(statement)

    event.listen(pc, "before_cursor_execute", capture_pc_mutations)
    with pc.connect() as conn:
        before = conn.execute(
            select(pc_daily).order_by(pc_daily.c.symbol)
        ).all()

    with pytest.raises(RuntimeError, match="chart indicators are not current"):
        db_loader.sync_local_mirror_from_pc_atomic(
            pc,
            local,
            tables=(("price_history", "date"),),
            tickers=["A"],
            verify_derived=True,
        )

    with pc.connect() as conn:
        after = conn.execute(
            select(pc_daily).order_by(pc_daily.c.symbol)
        ).all()
    assert after == before
    assert pc_mutations == []
    with local.connect() as conn:
        assert set(conn.execute(select(local_daily.c.symbol)).scalars()) == {
            "A",
            "SPY",
        }


def test_atomic_mirror_sync_requests_reconciliation_for_unobserved_local_write():
    pc, local, pc_daily, local_daily, _pc_hourly, _local_hourly = _raw_engines()
    shared_row = _daily_bar("A", 1, 100)
    with pc.begin() as conn:
        conn.execute(insert(pc_daily), shared_row)
    with local.begin() as conn:
        conn.execute(insert(local_daily), shared_row)
    clean = db_loader.reconcile_local_mirror_with_pc(
        pc,
        local,
        verify_derived=False,
        tables=(("price_history", "date"),),
    )
    assert clean.success is True

    # Simulates a short PC outage between UI probes: an independent refresh
    # resolved SQLite and wrote a bar even though the window still says PC.
    with local.begin() as conn:
        conn.execute(insert(local_daily), _daily_bar("B", 2, 200))

    with pytest.raises(db_loader.LocalMirrorNeedsReconciliationError):
        db_loader.sync_local_mirror_from_pc_atomic(
            pc,
            local,
            tables=(("price_history", "date"),),
            verify_derived=False,
        )

    # The active-PC mirror is source-read-only and preserves the offline row.
    with pc.connect() as conn:
        assert conn.execute(
            select(pc_daily.c.symbol).order_by(pc_daily.c.symbol)
        ).scalars().all() == ["A"]
    with local.connect() as conn:
        assert conn.execute(
            select(local_daily.c.symbol).order_by(local_daily.c.symbol)
        ).scalars().all() == ["A", "B"]

    # The UI stages onto local and this normal handoff path performs the merge.
    reconciled = db_loader.reconcile_local_mirror_with_pc(
        pc,
        local,
        tables=(("price_history", "date"),),
        verify_derived=False,
    )
    assert reconciled.success is True
    with pc.connect() as conn:
        assert conn.execute(
            select(pc_daily.c.symbol).order_by(pc_daily.c.symbol)
        ).scalars().all() == ["A", "B"]


def test_atomic_mirror_sync_rolls_back_all_tables_on_later_failure(monkeypatch):
    pc, local, pc_daily, local_daily, pc_hourly, local_hourly = _raw_engines()
    with pc.begin() as conn:
        conn.execute(
            insert(pc_daily),
            _daily_bar("A", 1, 100),
        )
        conn.execute(
            insert(pc_hourly),
            _hourly_bar("A", 10, 100),
        )
    with local.begin() as conn:
        conn.execute(insert(local_daily), _daily_bar("A", 1, 100))
        conn.execute(insert(local_hourly), _hourly_bar("A", 10, 100))

    clean = db_loader.reconcile_local_mirror_with_pc(
        pc,
        local,
        tables=_raw_reconciliation_tables(),
        verify_derived=False,
    )
    assert clean.success is True
    with pc.begin() as conn:
        conn.execute(insert(pc_daily), _daily_bar("B", 2, 200))
        conn.execute(insert(pc_hourly), _hourly_bar("B", 11, 200))

    original_upsert = db_loader._execute_bulk_upsert

    def fail_hourly(conn, table, records, key_columns, dialect_name):
        if table.name == "hourly_price_history":
            raise RuntimeError("simulated later-table failure")
        return original_upsert(conn, table, records, key_columns, dialect_name)

    monkeypatch.setattr(db_loader, "_execute_bulk_upsert", fail_hourly)

    with pytest.raises(RuntimeError, match="later-table failure"):
        db_loader.sync_local_mirror_from_pc_atomic(
            pc,
            local,
            tables=_raw_reconciliation_tables(),
            verify_derived=False,
        )

    with local.connect() as conn:
        assert conn.execute(
            select(local_daily.c.symbol).order_by(local_daily.c.symbol)
        ).scalars().all() == ["A"]
        assert conn.execute(
            select(local_hourly.c.symbol).order_by(local_hourly.c.symbol)
        ).scalars().all() == ["A"]


def test_reconciliation_promotes_only_newer_missing_raw_bars():
    pc, local, pc_daily, local_daily, pc_hourly, local_hourly = _raw_engines()
    with pc.begin() as conn:
        conn.execute(insert(pc_daily), _daily_bar("A", 1, 100))
        conn.execute(insert(pc_hourly), _hourly_bar("A", 10, 100))
    with local.begin() as conn:
        # This same-key disagreement must never overwrite the PC row.
        conn.execute(insert(local_daily), _daily_bar("A", 1, 999))
        conn.execute(insert(local_daily), _daily_bar("A", 2, 110))
        conn.execute(insert(local_hourly), _hourly_bar("A", 10, 999))
        conn.execute(insert(local_hourly), _hourly_bar("A", 11, 111))

    result = db_loader.reconcile_local_mirror_with_pc(
        pc,
        local,
        verify_derived=False,
        tables=_raw_reconciliation_tables(),
    )

    assert result.success is True
    assert result.local_to_pc_rows == {
        "price_history": 1,
        "hourly_price_history": 1,
    }
    with pc.connect() as conn:
        daily_rows = conn.execute(
            select(pc_daily.c.date, pc_daily.c.close).order_by(pc_daily.c.date)
        ).all()
        hourly_rows = conn.execute(
            select(pc_hourly.c.timestamp, pc_hourly.c.close).order_by(
                pc_hourly.c.timestamp
            )
        ).all()
    assert daily_rows == [
        (dt.datetime(2026, 1, 1), 100.0),
        (dt.datetime(2026, 1, 2), 110.0),
    ]
    assert hourly_rows == [
        (dt.datetime(2026, 1, 2, 10), 100.0),
        (dt.datetime(2026, 1, 2, 11), 111.0),
    ]


def test_reconciliation_pc_wins_equal_primary_key_conflict():
    pc, local, pc_daily, local_daily, _pc_hourly, _local_hourly = _raw_engines()
    with pc.begin() as conn:
        conn.execute(insert(pc_daily), _daily_bar("A", 2, 100))
    with local.begin() as conn:
        conn.execute(insert(local_daily), _daily_bar("A", 2, 999))

    result = db_loader.reconcile_local_mirror_with_pc(
        pc,
        local,
        verify_derived=False,
        tables=_raw_reconciliation_tables(),
    )

    assert result.success is True
    assert result.local_to_pc_rows["price_history"] == 0
    with pc.connect() as conn:
        assert conn.execute(select(pc_daily.c.close)).scalar_one() == 100.0
    with local.connect() as conn:
        assert conn.execute(select(local_daily.c.close)).scalar_one() == 100.0


def test_reconciliation_uses_per_symbol_watermarks_in_both_directions():
    pc, local, pc_daily, local_daily, _pc_hourly, _local_hourly = _raw_engines()
    with pc.begin() as conn:
        conn.execute(
            insert(pc_daily),
            [_daily_bar("A", 1, 101), _daily_bar("B", 3, 203)],
        )
    with local.begin() as conn:
        conn.execute(
            insert(local_daily),
            [_daily_bar("A", 2, 102), _daily_bar("B", 1, 201)],
        )

    result = db_loader.reconcile_local_mirror_with_pc(
        pc,
        local,
        verify_derived=False,
        tables=_raw_reconciliation_tables(),
    )

    assert result.success is True
    pc_latest = db_loader._raw_group_watermarks(
        pc, db_loader._RAW_MIRROR_SPECS[0]
    )
    local_latest = db_loader._raw_group_watermarks(
        local, db_loader._RAW_MIRROR_SPECS[0]
    )
    assert pc_latest == local_latest
    assert pc_latest[("A", "1d")] == dt.datetime(2026, 1, 2)
    assert pc_latest[("B", "1d")] == dt.datetime(2026, 1, 3)


def test_reconciliation_fills_interior_pc_primary_key_gap():
    pc, local, pc_daily, local_daily, _pc_hourly, _local_hourly = _raw_engines()
    with pc.begin() as conn:
        conn.execute(
            insert(pc_daily),
            [_daily_bar("A", 1, 101), _daily_bar("A", 3, 103)],
        )
    with local.begin() as conn:
        conn.execute(
            insert(local_daily),
            [
                _daily_bar("A", 1, 101),
                _daily_bar("A", 2, 102),
                _daily_bar("A", 3, 103),
            ],
        )

    result = db_loader.reconcile_local_mirror_with_pc(
        pc,
        local,
        verify_derived=False,
        tables=_raw_reconciliation_tables(),
    )

    assert result.success is True
    assert result.local_to_pc_rows["price_history"] == 1
    with pc.connect() as conn:
        assert conn.execute(
            select(pc_daily.c.date).order_by(pc_daily.c.date)
        ).scalars().all() == [
            dt.datetime(2026, 1, 1),
            dt.datetime(2026, 1, 2),
            dt.datetime(2026, 1, 3),
        ]


def test_reconciliation_detects_different_keys_with_equal_count_and_maximum():
    pc, local, pc_daily, local_daily, _pc_hourly, _local_hourly = _raw_engines()
    # Both partitions have count=2, max(date)=Jan3, and max(updated_at)=Jan3,
    # but each side starts with a different interior/older primary key.
    with pc.begin() as conn:
        conn.execute(
            insert(pc_daily),
            [_daily_bar("A", 1, 101), _daily_bar("A", 3, 103)],
        )
    with local.begin() as conn:
        conn.execute(
            insert(local_daily),
            [_daily_bar("A", 2, 102), _daily_bar("A", 3, 103)],
        )

    result = db_loader.reconcile_local_mirror_with_pc(
        pc,
        local,
        verify_derived=False,
        tables=_raw_reconciliation_tables(),
    )

    assert result.success is True
    assert result.local_to_pc_rows["price_history"] == 1
    expected_dates = [
        dt.datetime(2026, 1, 1),
        dt.datetime(2026, 1, 2),
        dt.datetime(2026, 1, 3),
    ]
    with pc.connect() as conn:
        assert conn.execute(
            select(pc_daily.c.date).order_by(pc_daily.c.date)
        ).scalars().all() == expected_dates
    with local.connect() as conn:
        assert conn.execute(
            select(local_daily.c.date).order_by(local_daily.c.date)
        ).scalars().all() == expected_dates


def test_reconciliation_canonicalizes_older_conflict_below_latest_date():
    pc, local, pc_daily, local_daily, _pc_hourly, _local_hourly = _raw_engines()
    with pc.begin() as conn:
        conn.execute(
            insert(pc_daily),
            [_daily_bar("A", 1, 101), _daily_bar("A", 3, 103)],
        )
    with local.begin() as conn:
        conn.execute(
            insert(local_daily),
            [_daily_bar("A", 1, 999), _daily_bar("A", 3, 103)],
        )

    result = db_loader.reconcile_local_mirror_with_pc(
        pc,
        local,
        verify_derived=False,
        tables=_raw_reconciliation_tables(),
    )

    assert result.success is True
    with local.connect() as conn:
        values = conn.execute(
            select(local_daily.c.date, local_daily.c.close).order_by(
                local_daily.c.date
            )
        ).all()
    assert values == [
        (dt.datetime(2026, 1, 1), 101.0),
        (dt.datetime(2026, 1, 3), 103.0),
    ]


def test_hourly_reconciliation_ignores_symbols_outside_relevant_scope():
    pc, local, _pc_daily, _local_daily, pc_hourly, local_hourly = _raw_engines()
    with pc.begin() as conn:
        conn.execute(
            insert(pc_hourly),
            [_hourly_bar("A", 10, 100), _hourly_bar("B", 10, 200)],
        )
    with local.begin() as conn:
        conn.execute(
            insert(local_hourly),
            [_hourly_bar("A", 11, 101), _hourly_bar("B", 10, 999)],
        )

    result = db_loader.reconcile_local_mirror_with_pc(
        pc,
        local,
        hourly_symbols=["A"],
        verify_derived=False,
        tables=db_loader.HOURLY_MIRROR_TABLES,
    )

    assert result.success is True
    assert result.local_to_pc_rows["hourly_price_history"] == 1
    with pc.connect() as conn:
        pc_values = conn.execute(
            select(pc_hourly.c.symbol, pc_hourly.c.close).order_by(
                pc_hourly.c.symbol, pc_hourly.c.timestamp
            )
        ).all()
    with local.connect() as conn:
        local_values = conn.execute(
            select(local_hourly.c.symbol, local_hourly.c.close).order_by(
                local_hourly.c.symbol, local_hourly.c.timestamp
            )
        ).all()
    assert pc_values == [("A", 100.0), ("A", 101.0), ("B", 200.0)]
    assert local_values == [("A", 100.0), ("A", 101.0), ("B", 999.0)]


def test_reconciliation_removes_local_only_derived_rows():
    pc = create_engine("sqlite:///:memory:", future=True)
    local = create_engine("sqlite:///:memory:", future=True)
    pc_table = db_loader._ensure_chart_indicators_table(pc)
    local_table = db_loader._ensure_chart_indicators_table(local)
    pc_row = {
        "symbol": "A",
        "date": dt.datetime(2026, 1, 2),
        "relative_strength": 1.1,
        "updated_at": dt.datetime(2026, 1, 2, 22),
    }
    with pc.begin() as conn:
        conn.execute(insert(pc_table), pc_row)
    with local.begin() as conn:
        conn.execute(insert(local_table), pc_row)
        conn.execute(
            insert(local_table),
            {
                **pc_row,
                "symbol": "LOCAL_ONLY",
                "relative_strength": 9.9,
            },
        )

    result = db_loader.reconcile_local_mirror_with_pc(
        pc,
        local,
        verify_derived=False,
        tables=(("chart_indicators", "date"),),
    )

    assert result.success is True
    assert result.local_to_pc_rows == {}
    with pc.connect() as conn:
        assert conn.execute(select(pc_table.c.symbol)).scalars().all() == ["A"]
    with local.connect() as conn:
        assert conn.execute(select(local_table.c.symbol)).scalars().all() == ["A"]


def test_exact_reconciliation_is_idempotent_on_second_pass():
    pc, local, pc_daily, local_daily, _pc_hourly, _local_hourly = _raw_engines()
    with pc.begin() as conn:
        conn.execute(insert(pc_daily), _daily_bar("A", 1, 100))
    with local.begin() as conn:
        conn.execute(insert(local_daily), _daily_bar("A", 2, 101))

    first = db_loader.reconcile_local_mirror_with_pc(
        pc,
        local,
        verify_derived=False,
        tables=_raw_reconciliation_tables(),
    )
    second = db_loader.reconcile_local_mirror_with_pc(
        pc,
        local,
        verify_derived=False,
        tables=_raw_reconciliation_tables(),
    )

    assert first.success is True
    assert second.success is True
    assert second.total_local_to_pc_rows == 0
    assert second.total_pc_to_local_rows == 0


def test_reconciliation_blocks_legacy_local_primary_key_before_pc_write():
    pc = create_engine("sqlite:///:memory:", future=True)
    local = create_engine("sqlite:///:memory:", future=True)
    pc_daily = db_loader._ensure_price_history_table(pc)
    legacy_metadata = MetaData()
    legacy_daily = Table(
        "price_history",
        legacy_metadata,
        Column("symbol", String(20), primary_key=True),
        Column("date", DateTime, primary_key=True),
        Column("interval", String(10), nullable=False),
        Column("open", Float),
        Column("high", Float),
        Column("low", Float),
        Column("close", Float),
        Column("adj_close", Float),
        Column("volume", Float),
        Column("updated_at", DateTime, nullable=False),
    )
    legacy_metadata.create_all(local)
    with local.begin() as conn:
        conn.execute(insert(legacy_daily), _daily_bar("A", 2, 100))

    result = db_loader.reconcile_local_mirror_with_pc(
        pc,
        local,
        verify_derived=False,
        tables=(("price_history", "date"),),
    )

    assert result.success is False
    assert any("expected primary key" in error for error in result.errors)
    with pc.connect() as conn:
        assert conn.execute(select(pc_daily.c.symbol)).all() == []


def test_reconciliation_blocks_empty_legacy_primary_key_schema():
    pc = create_engine("sqlite:///:memory:", future=True)
    local = create_engine("sqlite:///:memory:", future=True)
    db_loader._ensure_price_history_table(pc)
    legacy_metadata = MetaData()
    Table(
        "price_history",
        legacy_metadata,
        Column("symbol", String(20), primary_key=True),
        Column("date", DateTime, primary_key=True),
        Column("interval", String(10), nullable=False),
        Column("open", Float),
        Column("high", Float),
        Column("low", Float),
        Column("close", Float),
        Column("adj_close", Float),
        Column("volume", Float),
        Column("updated_at", DateTime, nullable=False),
    )
    legacy_metadata.create_all(local)

    result = db_loader.reconcile_local_mirror_with_pc(
        pc,
        local,
        verify_derived=False,
        tables=(("price_history", "date"),),
    )

    assert result.success is False
    assert any("expected primary key" in error for error in result.errors)


def test_reconciliation_blocks_schema_missing_required_revision_column():
    pc = create_engine("sqlite:///:memory:", future=True)
    local = create_engine("sqlite:///:memory:", future=True)
    for engine in (pc, local):
        metadata = MetaData()
        Table(
            "price_history",
            metadata,
            Column("symbol", String(20), primary_key=True),
            Column("date", DateTime, primary_key=True),
            Column("interval", String(10), primary_key=True),
            Column("open", Float),
            Column("high", Float),
            Column("low", Float),
            Column("close", Float),
            Column("adj_close", Float),
            Column("volume", Float),
        )
        metadata.create_all(engine)

    result = db_loader.reconcile_local_mirror_with_pc(
        pc,
        local,
        verify_derived=False,
        tables=(("price_history", "date"),),
    )

    assert result.success is False
    assert any("missing required column" in error for error in result.errors)


def test_reconciliation_rejects_invalid_local_bar_and_does_not_switch_ready():
    pc, local, _pc_daily, local_daily, _pc_hourly, _local_hourly = _raw_engines()
    invalid = _daily_bar("A", 2, 100)
    invalid["high"] = 50.0
    with local.begin() as conn:
        conn.execute(insert(local_daily), invalid)

    result = db_loader.reconcile_local_mirror_with_pc(
        pc,
        local,
        verify_derived=False,
        tables=_raw_reconciliation_tables(),
    )

    assert result.success is False
    assert any("Invalid OHLC range" in error for error in result.errors)
    with pc.connect() as conn:
        assert conn.execute(
            select(db_loader._ensure_price_history_table(pc).c.symbol)
        ).all() == []


def test_reconciliation_rebuilds_pc_derived_data_after_daily_promotion(monkeypatch):
    pc, local, _pc_daily, local_daily, _pc_hourly, _local_hourly = _raw_engines()
    with local.begin() as conn:
        conn.execute(insert(local_daily), _daily_bar("A", 2, 100))
    calls = []
    monkeypatch.setattr(
        db_loader,
        "_rebuild_and_verify_pc_derived_data",
        lambda engine, tickers, affected: calls.append(
            (engine, tickers, set(affected))
        ),
    )

    result = db_loader.reconcile_local_mirror_with_pc(
        pc,
        local,
        tickers=["A"],
        tables=_raw_reconciliation_tables(),
    )

    assert result.success is True
    assert calls == [(pc, ["A"], {"A"})]


def test_reconciliation_retry_still_verifies_derived_after_committed_raw_insert(
    monkeypatch,
):
    pc, local, _pc_daily, local_daily, _pc_hourly, _local_hourly = _raw_engines()
    with local.begin() as conn:
        conn.execute(insert(local_daily), _daily_bar("A", 2, 100))

    original_fingerprints = db_loader._partition_fingerprints
    fail_hourly_once = {"value": True}

    def flaky_fingerprints(engine, spec, **kwargs):
        if spec.table_name == "hourly_price_history" and fail_hourly_once["value"]:
            fail_hourly_once["value"] = False
            raise RuntimeError("transient hourly failure")
        return original_fingerprints(engine, spec, **kwargs)

    monkeypatch.setattr(db_loader, "_partition_fingerprints", flaky_fingerprints)
    derived_calls = []
    monkeypatch.setattr(
        db_loader,
        "_rebuild_and_verify_pc_derived_data",
        lambda engine, tickers, affected: derived_calls.append(set(affected)),
    )

    first = db_loader.reconcile_local_mirror_with_pc(
        pc,
        local,
        tickers=["A"],
        tables=_raw_reconciliation_tables(),
    )
    second = db_loader.reconcile_local_mirror_with_pc(
        pc,
        local,
        tickers=["A"],
        tables=_raw_reconciliation_tables(),
    )

    assert first.success is False
    assert first.local_to_pc_rows["price_history"] == 1
    assert second.success is True
    assert second.local_to_pc_rows["price_history"] == 0
    assert derived_calls == [set()]


def test_reconciliation_write_fence_rejects_concurrent_pc_change(monkeypatch):
    pc, local, pc_daily, local_daily, _pc_hourly, _local_hourly = _raw_engines()
    shared_row = _daily_bar("A", 1, 100)
    with pc.begin() as conn:
        conn.execute(insert(pc_daily), shared_row)
    with local.begin() as conn:
        conn.execute(insert(local_daily), shared_row)

    original_fingerprints = db_loader._partition_fingerprints
    pc_price_calls = {"count": 0}

    def fingerprints_with_concurrent_pc_write(engine, spec, **kwargs):
        snapshot = original_fingerprints(engine, spec, **kwargs)
        if engine is pc and spec.table_name == "price_history":
            pc_price_calls["count"] += 1
            if pc_price_calls["count"] == 2:
                # Arrives immediately after the canonical PC-before snapshot.
                # The all-table PC-after barrier must detect it and refuse a
                # successful handoff rather than leaving the mirror behind.
                with pc.begin() as conn:
                    conn.execute(insert(pc_daily), _daily_bar("B", 2, 200))
        return snapshot

    monkeypatch.setattr(
        db_loader, "_partition_fingerprints", fingerprints_with_concurrent_pc_write
    )

    first = db_loader.reconcile_local_mirror_with_pc(
        pc,
        local,
        verify_derived=False,
        tables=(("price_history", "date"),),
    )
    second = db_loader.reconcile_local_mirror_with_pc(
        pc,
        local,
        verify_derived=False,
        tables=(("price_history", "date"),),
    )

    assert first.success is False
    assert any("equality barrier" in error for error in first.errors)
    assert second.success is True
    with local.connect() as conn:
        assert conn.execute(
            select(local_daily.c.symbol).order_by(local_daily.c.symbol)
        ).scalars().all() == ["A", "B"]


def test_reconciliation_preserves_concurrent_local_raw_insert_for_retry(monkeypatch):
    pc, local, pc_daily, local_daily, _pc_hourly, _local_hourly = _raw_engines()
    shared_row = _daily_bar("A", 1, 100)
    with pc.begin() as conn:
        conn.execute(insert(pc_daily), shared_row)
    with local.begin() as conn:
        conn.execute(insert(local_daily), shared_row)

    original_fingerprints = db_loader._partition_fingerprints
    pc_price_calls = {"count": 0}

    def fingerprints_with_concurrent_local_write(engine, spec, **kwargs):
        snapshot = original_fingerprints(engine, spec, **kwargs)
        if engine is pc and spec.table_name == "price_history":
            pc_price_calls["count"] += 1
            if pc_price_calls["count"] == 2:
                with local.begin() as conn:
                    conn.execute(insert(local_daily), _daily_bar("B", 2, 200))
        return snapshot

    monkeypatch.setattr(
        db_loader, "_partition_fingerprints", fingerprints_with_concurrent_local_write
    )

    first = db_loader.reconcile_local_mirror_with_pc(
        pc,
        local,
        verify_derived=False,
        tables=(("price_history", "date"),),
    )
    with local.connect() as conn:
        assert conn.execute(
            select(local_daily.c.symbol).order_by(local_daily.c.symbol)
        ).scalars().all() == ["A", "B"]

    second = db_loader.reconcile_local_mirror_with_pc(
        pc,
        local,
        verify_derived=False,
        tables=(("price_history", "date"),),
    )

    assert first.success is False
    assert second.success is True
    with pc.connect() as conn:
        assert conn.execute(
            select(pc_daily.c.symbol).order_by(pc_daily.c.symbol)
        ).scalars().all() == ["A", "B"]


def test_local_handoff_guard_rejects_write_after_reconciliation():
    pc, local, pc_daily, local_daily, _pc_hourly, _local_hourly = _raw_engines()
    shared_row = _daily_bar("A", 1, 100)
    with pc.begin() as conn:
        conn.execute(insert(pc_daily), shared_row)
    with local.begin() as conn:
        conn.execute(insert(local_daily), shared_row)

    first = db_loader.reconcile_local_mirror_with_pc(
        pc,
        local,
        verify_derived=False,
        tables=(("price_history", "date"),),
    )
    assert first.success is True
    assert first.local_handoff_ready is True
    guard = db_loader.acquire_local_mirror_handoff_guard(local)
    db_loader.release_local_mirror_handoff_guard(guard)

    with local.begin() as conn:
        conn.execute(insert(local_daily), _daily_bar("B", 2, 200))

    with pytest.raises(RuntimeError, match="changed after reconciliation"):
        db_loader.acquire_local_mirror_handoff_guard(local)

    second = db_loader.reconcile_local_mirror_with_pc(
        pc,
        local,
        verify_derived=False,
        tables=(("price_history", "date"),),
    )
    assert second.success is True
    guard = db_loader.acquire_local_mirror_handoff_guard(local)
    db_loader.release_local_mirror_handoff_guard(guard)


def test_local_mirror_staleness_uses_latest_daily_market_date():
    engine = create_engine("sqlite:///:memory:", future=True)
    assert db_loader.local_mirror_is_stale(engine, dt.date(2026, 1, 2))

    table = db_loader._ensure_price_history_table(engine)
    with engine.begin() as conn:
        conn.execute(
            insert(table),
            {
                "symbol": "SPY",
                "date": dt.datetime(2026, 1, 2),
                "interval": "1d",
                "close": 100.0,
                "updated_at": dt.datetime(2026, 1, 2),
            },
        )

    assert not db_loader.local_mirror_is_stale(engine, dt.date(2026, 1, 2))
    assert db_loader.local_mirror_is_stale(engine, dt.date(2026, 1, 3))

    with engine.begin() as conn:
        conn.execute(
            insert(table),
            {
                "symbol": "STALE",
                "date": dt.datetime(2026, 1, 1),
                "interval": "1d",
                "close": 50.0,
                "updated_at": dt.datetime(2026, 1, 1),
            },
        )
    assert db_loader.local_mirror_is_stale(engine, dt.date(2026, 1, 2))

    for _ in range(db_loader.CHRONIC_FAILURE_THRESHOLD):
        db_loader.record_symbol_refresh_outcomes(engine, "1d", [], ["STALE"])
    assert not db_loader.local_mirror_is_stale(engine, dt.date(2026, 1, 2))


def test_local_mirror_staleness_ignores_symbols_outside_the_tracked_universe():
    """A symbol dropped from the universe never accumulates chronic-failure
    attempts (historical.py stops trying it), so its old leftover rows must
    not be able to flag the whole mirror stale forever -- unlike CHRONIC
    above, this covers a symbol that was simply never (and will never be)
    retried, not one that failed repeatedly."""
    engine = create_engine("sqlite:///:memory:", future=True)
    table = db_loader._ensure_price_history_table(engine)
    with engine.begin() as conn:
        conn.execute(
            insert(table),
            [
                {
                    "symbol": "SPY",
                    "date": dt.datetime(2026, 1, 2),
                    "interval": "1d",
                    "close": 100.0,
                    "updated_at": dt.datetime(2026, 1, 2),
                },
                {
                    "symbol": "AAPL",
                    "date": dt.datetime(2026, 1, 2),
                    "interval": "1d",
                    "close": 200.0,
                    "updated_at": dt.datetime(2026, 1, 2),
                },
                {
                    "symbol": "DELISTED",
                    "date": dt.datetime(2025, 6, 1),
                    "interval": "1d",
                    "close": 10.0,
                    "updated_at": dt.datetime(2025, 6, 1),
                },
            ],
        )

    # Without a universe filter, the leftover DELISTED row still flags staleness.
    assert db_loader.local_mirror_is_stale(engine, dt.date(2026, 1, 2))
    # Restricting to the current universe (which no longer includes it) fixes that.
    assert not db_loader.local_mirror_is_stale(
        engine, dt.date(2026, 1, 2), tickers=["SPY", "AAPL"]
    )
    # A ticker still in the tracked universe is not exempted this way.
    assert db_loader.local_mirror_is_stale(
        engine, dt.date(2026, 1, 2), tickers=["SPY", "DELISTED"]
    )


def test_local_mirror_staleness_detects_missing_tracked_symbols():
    engine = create_engine("sqlite:///:memory:", future=True)
    table = db_loader._ensure_price_history_table(engine)
    with engine.begin() as conn:
        conn.execute(
            insert(table),
            {
                "symbol": "SPY",
                "date": dt.datetime(2026, 1, 2),
                "interval": "1d",
                "close": 100.0,
                "updated_at": dt.datetime(2026, 1, 2),
            },
        )

    assert db_loader.local_mirror_is_stale(
        engine, dt.date(2026, 1, 2), tickers=["SPY", "MISSING"]
    )

    for _ in range(db_loader.CHRONIC_FAILURE_THRESHOLD):
        db_loader.record_symbol_refresh_outcomes(engine, "1d", [], ["MISSING"])
    assert not db_loader.local_mirror_is_stale(
        engine, dt.date(2026, 1, 2), tickers=["SPY", "MISSING"]
    )


def test_sync_cli_reports_and_runs_all_phases(monkeypatch, capsys):
    import scripts.sync_local_mirror_from_pc as sync_cli

    pc_engine = SimpleNamespace(
        url=SimpleNamespace(host="pc", port=3306, database="quant_app"),
        dispose=lambda: None,
    )
    local_engine = SimpleNamespace(dispose=lambda: None)
    stats_calls = []
    monkeypatch.setattr(sync_cli, "init_mysql_engine", lambda: pc_engine)
    monkeypatch.setattr(sync_cli, "init_local_mirror_engine", lambda: local_engine)
    monkeypatch.setattr(
        sync_cli,
        "_print_stats_table",
        lambda title, engine: stats_calls.append((title, engine)),
    )
    monkeypatch.setattr(
        sync_cli,
        "sync_local_mirror_from_pc",
        lambda source, destination, **kwargs: {"price_history": 7},
    )

    assert sync_cli.main() == 0

    assert stats_calls == [
        ("PC (source)", pc_engine),
        ("Local mirror (before sync)", local_engine),
        ("Local mirror (after sync)", local_engine),
    ]
    assert "Done. 7 row(s) written" in capsys.readouterr().out


def test_sync_cli_fails_cleanly_when_pc_is_unreachable(monkeypatch, capsys):
    import scripts.sync_local_mirror_from_pc as sync_cli

    monkeypatch.setattr(sync_cli, "init_mysql_engine", lambda: None)

    assert sync_cli.main() == 1
    assert "Could not reach the PC's MySQL database" in capsys.readouterr().err


def test_sync_cli_returns_failure_for_partial_table_errors(monkeypatch, capsys):
    import scripts.sync_local_mirror_from_pc as sync_cli

    engine = SimpleNamespace(
        url=SimpleNamespace(host="pc", port=3306, database="quant_app"),
        dispose=lambda: None,
    )
    monkeypatch.setattr(sync_cli, "init_mysql_engine", lambda: engine)
    monkeypatch.setattr(sync_cli, "init_local_mirror_engine", lambda: engine)
    monkeypatch.setattr(sync_cli, "_print_stats_table", lambda *args: None)

    def partial_sync(source, destination, **kwargs):
        kwargs["error_callback"]("one table failed")
        return {"price_history": 3}

    monkeypatch.setattr(sync_cli, "sync_local_mirror_from_pc", partial_sync)

    assert sync_cli.main() == 1
    assert "completed with 1 table error" in capsys.readouterr().err
