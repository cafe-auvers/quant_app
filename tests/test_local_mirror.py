import datetime as dt
from types import SimpleNamespace

import pytest
from sqlalchemy import (
    Column,
    DateTime,
    MetaData,
    String,
    Table,
    create_engine,
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
