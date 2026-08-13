import datetime as dt

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import create_engine, delete, event, select
from sqlalchemy.exc import OperationalError, SQLAlchemyError

import src.utils.db_loader as db_loader
from src.infrastructure.database import schema as schema_module
from src.infrastructure.database.repositories import (chart_indicators,
                                                      market_watermarks,
                                                      scanner)


@pytest.fixture
def engine():
    value = create_engine("sqlite:///:memory:", future=True)
    schema_module._ensured_engines.discard(value)
    try:
        yield value
    finally:
        schema_module._ensured_engines.discard(value)
        value.dispose()


def _history(dates, start_price=100.0):
    size = len(dates)
    close = np.arange(start_price, start_price + size, dtype=float)
    return pd.DataFrame(
        {
            "Open": close - 0.5,
            "High": close + 1.0,
            "Low": close - 1.0,
            "Close": close,
            "Adj Close": close,
            "Volume": np.arange(1_000_000, 1_000_000 + size, dtype=float),
        },
        index=pd.DatetimeIndex(dates),
    )


def _seed_daily_history(engine, dates):
    assert db_loader.save_symbol_history_to_db("SPY", _history(dates, 200), engine)
    assert db_loader.save_symbol_history_to_db("AAPL", _history(dates, 100), engine)


def _capture_selects(engine):
    captured = []

    def record(_conn, _cursor, statement, parameters, _context, _executemany):
        normalized = " ".join(statement.lower().split())
        if normalized.startswith("select"):
            captured.append((normalized, parameters))

    event.listen(engine, "before_cursor_execute", record)
    return captured, record


def test_price_history_watermarks_use_bounded_symbol_batches(engine, monkeypatch):
    symbols = ["SPY", "AAA", "BBB", "CCC", "DDD"]
    dates = pd.bdate_range("2026-01-05", periods=2)
    for offset, symbol in enumerate(symbols):
        assert db_loader.save_symbol_history_to_db(
            symbol, _history(dates, 100 + offset), engine
        )

    monkeypatch.setattr(market_watermarks, "CACHE_QUERY_SYMBOL_CHUNK_SIZE", 2)
    captured, listener = _capture_selects(engine)
    try:
        watermarks = db_loader.get_price_history_watermarks(
            engine, symbols, strict=True
        )
    finally:
        event.remove(engine, "before_cursor_execute", listener)

    queries = [
        item
        for item in captured
        if " from price_history " in item[0] and " group by " in item[0]
    ]
    assert set(watermarks) == set(symbols)
    assert len(queries) == 3
    assert all(len(parameters) <= 3 for _, parameters in queries)


def test_complete_chart_cache_uses_manifest_without_indicator_scan(
    engine, monkeypatch
):
    symbols = ["AAA", "BBB", "CCC", "DDD", "EEE"]
    dates = pd.bdate_range("2026-01-05", periods=3)
    spy_history = _history(dates, 200)
    assert db_loader.save_symbol_history_to_db("SPY", spy_history, engine)
    for offset, symbol in enumerate(symbols):
        history = _history(dates, 100 + offset)
        assert db_loader.save_symbol_history_to_db(symbol, history, engine)
        indicators = db_loader.calculate_chart_indicators(symbol, history, spy_history)
        assert db_loader.save_chart_indicators_to_db(symbol, indicators, engine)
    watermarks = db_loader.get_price_history_watermarks(engine, ["SPY", *symbols])

    monkeypatch.setattr(chart_indicators, "CACHE_QUERY_SYMBOL_CHUNK_SIZE", 2)
    assert db_loader.get_chart_indicator_refresh_plan(
        engine,
        ["SPY", *symbols],
        history_watermarks=watermarks,
    ) == {}

    captured, listener = _capture_selects(engine)
    try:
        plan = db_loader.get_chart_indicator_refresh_plan(
            engine,
            ["SPY", *symbols],
            history_watermarks=watermarks,
        )
    finally:
        event.remove(engine, "before_cursor_execute", listener)

    manifest_queries = [
        item for item in captured if " from chart_indicator_manifests " in item[0]
    ]
    exact_queries = [
        item for item in captured if "chart_reference_prices" in item[0]
    ]
    indicator_queries = [
        item for item in captured if " from chart_indicators " in item[0]
    ]
    assert plan == {}
    assert len(manifest_queries) == 3
    assert all(len(parameters) <= 2 for _, parameters in manifest_queries)
    assert exact_queries == []
    assert indicator_queries == []


def test_ambiguous_chart_gap_checks_use_bounded_symbol_batches(engine, monkeypatch):
    symbols = ["AAA", "BBB", "CCC", "DDD", "EEE"]
    dates = pd.bdate_range("2026-01-05", periods=2)
    assert db_loader.save_symbol_history_to_db("SPY", _history(dates, 200), engine)
    for offset, symbol in enumerate(symbols):
        assert db_loader.save_symbol_history_to_db(
            symbol, _history(dates, 100 + offset), engine
        )
    watermarks = db_loader.get_price_history_watermarks(engine, ["SPY", *symbols])

    monkeypatch.setattr(chart_indicators, "CACHE_QUERY_SYMBOL_CHUNK_SIZE", 2)
    captured, listener = _capture_selects(engine)
    try:
        plan = db_loader.get_chart_indicator_refresh_plan(
            engine,
            ["SPY", *symbols],
            history_watermarks=watermarks,
        )
    finally:
        event.remove(engine, "before_cursor_execute", listener)

    exact_queries = [
        item for item in captured if "chart_reference_prices" in item[0]
    ]
    assert set(plan) == set(symbols)
    assert len(exact_queries) == 3
    assert all(len(parameters) <= 5 for _, parameters in exact_queries)


def test_chart_plan_finds_middle_gap_and_refresh_repairs_it(engine, monkeypatch):
    dates = pd.bdate_range("2026-01-05", periods=8)
    _seed_daily_history(engine, dates)
    full = db_loader.calculate_chart_indicators(
        "AAPL", _history(dates, 100), _history(dates, 200)
    )
    missing_date = pd.Timestamp(dates[3]).to_pydatetime()
    partial = full.loc[pd.to_datetime(full["date"]) != pd.Timestamp(missing_date)]
    assert db_loader.save_chart_indicators_to_db("AAPL", partial, engine)

    plan = db_loader.get_chart_indicator_refresh_plan(engine, ["SPY", "AAPL"])

    assert plan == {"AAPL": missing_date}
    assert db_loader.refresh_chart_indicators_to_db(["SPY", "AAPL"], engine) == ["AAPL"]
    assert db_loader.get_chart_indicator_refresh_plan(engine, ["SPY", "AAPL"]) == {}

    monkeypatch.setattr(
        chart_indicators,
        "load_universe_history_from_db",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("current indicators must not load or calculate history")
        ),
    )
    assert db_loader.refresh_chart_indicators_to_db(["SPY", "AAPL"], engine) == []


def test_chart_plan_does_not_accept_a_latest_only_row_as_complete(engine):
    dates = pd.bdate_range("2026-01-05", periods=5)
    _seed_daily_history(engine, dates)
    indicators = db_loader.calculate_chart_indicators(
        "AAPL", _history(dates, 100), _history(dates, 200)
    )
    assert db_loader.save_chart_indicators_to_db("AAPL", indicators.tail(1), engine)

    plan = db_loader.get_chart_indicator_refresh_plan(engine, ["SPY", "AAPL"])

    assert plan == {"AAPL": pd.Timestamp(dates[0]).to_pydatetime()}


def test_chart_plan_does_not_let_orphan_row_hide_middle_gap(engine):
    dates = pd.bdate_range("2026-01-05", periods=3)
    _seed_daily_history(engine, dates)
    indicators = db_loader.calculate_chart_indicators(
        "AAPL", _history(dates, 100), _history(dates, 200)
    )
    missing_date = pd.Timestamp(dates[1]).to_pydatetime()
    orphan = indicators.tail(1).copy()
    orphan["date"] = pd.Timestamp("2026-01-08")
    partial = pd.concat(
        [
            indicators.loc[
                pd.to_datetime(indicators["date"]) != pd.Timestamp(missing_date)
            ],
            orphan,
        ],
        ignore_index=True,
    )
    assert db_loader.save_chart_indicators_to_db("AAPL", partial, engine)

    plan = db_loader.get_chart_indicator_refresh_plan(engine, ["SPY", "AAPL"])

    assert plan == {"AAPL": missing_date}


def test_chart_manifest_version_change_forces_recalculation(engine, monkeypatch):
    dates = pd.bdate_range("2026-01-05", periods=3)
    _seed_daily_history(engine, dates)
    indicators = db_loader.calculate_chart_indicators(
        "AAPL", _history(dates, 100), _history(dates, 200)
    )
    assert db_loader.save_chart_indicators_to_db("AAPL", indicators, engine)
    watermarks = db_loader.get_price_history_watermarks(
        engine, ["SPY", "AAPL"]
    )
    assert db_loader.get_chart_indicator_refresh_plan(
        engine,
        ["SPY", "AAPL"],
        history_watermarks=watermarks,
    ) == {}

    monkeypatch.setattr(
        chart_indicators,
        "CHART_INDICATOR_CACHE_VERSION",
        db_loader.CHART_INDICATOR_CACHE_VERSION + 1,
    )

    plan = db_loader.get_chart_indicator_refresh_plan(
        engine,
        ["SPY", "AAPL"],
        history_watermarks=watermarks,
    )

    assert plan == {"AAPL": pd.Timestamp(dates[0]).to_pydatetime()}


def test_chart_indicator_write_invalidates_completion_manifest(engine):
    dates = pd.bdate_range("2026-01-05", periods=3)
    _seed_daily_history(engine, dates)
    indicators = db_loader.calculate_chart_indicators(
        "AAPL", _history(dates, 100), _history(dates, 200)
    )
    assert db_loader.save_chart_indicators_to_db("AAPL", indicators, engine)
    watermarks = db_loader.get_price_history_watermarks(
        engine, ["SPY", "AAPL"]
    )
    assert db_loader.get_chart_indicator_refresh_plan(
        engine,
        ["SPY", "AAPL"],
        history_watermarks=watermarks,
    ) == {}
    assert set(db_loader._get_chart_indicator_manifests(engine, ["AAPL"])) == {
        "AAPL"
    }

    assert db_loader.save_chart_indicators_to_db(
        "AAPL", indicators.tail(1), engine
    )

    assert db_loader._get_chart_indicator_manifests(engine, ["AAPL"]) == {}


def test_chart_reference_change_replaces_old_reference_rows(engine):
    dates = pd.bdate_range("2026-01-05", periods=3)
    _seed_daily_history(engine, dates)
    indicators = db_loader.calculate_chart_indicators(
        "AAPL", _history(dates, 100), _history(dates, 200)
    )
    assert db_loader.save_chart_indicators_to_db("AAPL", indicators, engine)
    spy_watermarks = db_loader.get_price_history_watermarks(
        engine, ["SPY", "AAPL"]
    )
    assert db_loader.get_chart_indicator_refresh_plan(
        engine,
        ["SPY", "AAPL"],
        history_watermarks=spy_watermarks,
    ) == {}

    qqq_dates = dates.delete(1)
    assert db_loader.save_symbol_history_to_db(
        "QQQ", _history(qqq_dates, 300), engine
    )
    qqq_watermarks = db_loader.get_price_history_watermarks(
        engine, ["QQQ", "AAPL"]
    )
    plan = db_loader.get_chart_indicator_refresh_plan(
        engine,
        ["QQQ", "AAPL"],
        reference_symbol="QQQ",
        history_watermarks=qqq_watermarks,
    )

    assert plan == {"AAPL": pd.Timestamp(dates[0]).to_pydatetime()}
    assert db_loader.refresh_chart_indicators_to_db(
        ["QQQ", "AAPL"],
        engine,
        reference_symbol="QQQ",
        history_watermarks=qqq_watermarks,
        refresh_plan=plan,
    ) == ["AAPL"]
    saved = db_loader.load_chart_indicators_from_db("AAPL", engine)
    assert list(saved.index) == list(pd.DatetimeIndex(qqq_dates))
    assert db_loader.get_chart_indicator_refresh_plan(
        engine,
        ["QQQ", "AAPL"],
        reference_symbol="QQQ",
        history_watermarks=qqq_watermarks,
    ) == {}


def test_chart_reference_change_clears_rows_when_calendars_do_not_overlap(engine):
    dates = pd.bdate_range("2026-01-05", periods=2)
    _seed_daily_history(engine, dates)
    indicators = db_loader.calculate_chart_indicators(
        "AAPL", _history(dates, 100), _history(dates, 200)
    )
    assert db_loader.save_chart_indicators_to_db("AAPL", indicators, engine)
    spy_watermarks = db_loader.get_price_history_watermarks(
        engine, ["SPY", "AAPL"]
    )
    assert db_loader.get_chart_indicator_refresh_plan(
        engine,
        ["SPY", "AAPL"],
        history_watermarks=spy_watermarks,
    ) == {}

    qqq_dates = pd.bdate_range("2027-01-05", periods=2)
    assert db_loader.save_symbol_history_to_db(
        "QQQ", _history(qqq_dates, 300), engine
    )
    qqq_watermarks = db_loader.get_price_history_watermarks(
        engine, ["QQQ", "AAPL"]
    )

    assert db_loader.get_chart_indicator_refresh_plan(
        engine,
        ["QQQ", "AAPL"],
        reference_symbol="QQQ",
        history_watermarks=qqq_watermarks,
    ) == {}
    assert db_loader.load_chart_indicators_from_db("AAPL", engine).empty


def test_incremental_chart_calculation_matches_full_calculation():
    dates = pd.bdate_range("2024-01-02", periods=360)
    symbol_history = _history(dates, 80)
    spy_dates = dates.delete(np.arange(0, len(dates), 17))
    spy_history = _history(spy_dates, 180)
    full = db_loader.calculate_chart_indicators(
        "AAPL", symbol_history, spy_history
    )
    start_date = pd.Timestamp(full["date"].iloc[-45]).to_pydatetime()
    expected = full.loc[pd.to_datetime(full["date"]) >= pd.Timestamp(start_date)].copy()
    actual = db_loader.calculate_chart_indicators_since(
        "AAPL", symbol_history, spy_history, start_date=start_date
    )
    columns = [column for column in expected.columns if column != "updated_at"]

    pd.testing.assert_frame_equal(
        expected[columns].reset_index(drop=True),
        actual[columns].reset_index(drop=True),
        check_dtype=False,
        check_exact=False,
        rtol=1e-12,
        atol=1e-12,
    )


def test_scanner_snapshot_is_authoritative_and_detects_partial_rows(engine, monkeypatch):
    dates = pd.bdate_range("2026-01-05", periods=5)
    _seed_daily_history(engine, dates)
    assert db_loader.save_symbol_history_to_db("MSFT", _history(dates, 140), engine)
    symbols = ["SPY", "AAPL", "MSFT"]
    watermarks = db_loader.get_price_history_watermarks(engine, symbols)
    snapshot_date = db_loader.scanner_metrics_snapshot_date(dt.date(2026, 1, 9))
    fingerprint = db_loader.scanner_metrics_input_fingerprint(symbols, watermarks)
    saved = db_loader.save_scanner_metrics_snapshot_to_db(
        [
            {"symbol": "AAPL", "return_1m": 1.0},
            {"symbol": "MSFT", "return_1m": 2.0},
        ],
        snapshot_date,
        fingerprint,
        engine,
    )

    assert saved == ["AAPL", "MSFT"]
    assert db_loader.is_scanner_metrics_snapshot_current(
        engine,
        ["AAPL", "MSFT"],
        history_watermarks=watermarks,
        snapshot_date=snapshot_date,
    )

    table = db_loader._ensure_scanner_metrics_table(engine)
    with engine.begin() as conn:
        conn.execute(
            delete(table).where(
                table.c.symbol == "MSFT", table.c.date == snapshot_date
            )
        )

    assert not db_loader.is_scanner_metrics_snapshot_current(
        engine,
        ["AAPL", "MSFT"],
        history_watermarks=watermarks,
        snapshot_date=snapshot_date,
    )


def test_scanner_snapshot_uses_bounded_atomic_statements(engine, monkeypatch):
    symbols = ["AAA", "BBB", "CCC", "DDD", "EEE"]
    snapshot_date = dt.datetime(2026, 1, 9)
    metrics = [
        {"symbol": symbol, "return_1m": float(index)}
        for index, symbol in enumerate(symbols)
    ]
    monkeypatch.setattr(scanner, "SCANNER_QUERY_SYMBOL_CHUNK_SIZE", 2)
    monkeypatch.setattr(scanner, "SCANNER_METRIC_WRITE_CHUNK_SIZE", 2)
    original_upsert = scanner._execute_bulk_upsert
    metric_write_sizes = []

    def recording_upsert(conn, table, records, key_columns, dialect_name):
        if table.name == "scanner_metrics":
            metric_write_sizes.append(len(records))
        return original_upsert(conn, table, records, key_columns, dialect_name)

    statements = []

    def record_statement(_conn, _cursor, statement, parameters, _context, _many):
        statements.append((" ".join(statement.lower().split()), parameters))

    monkeypatch.setattr(scanner, "_execute_bulk_upsert", recording_upsert)
    event.listen(engine, "before_cursor_execute", record_statement)
    try:
        saved = db_loader.save_scanner_metrics_snapshot_to_db(
            metrics,
            snapshot_date,
            "fingerprint",
            engine,
            snapshot_symbols=symbols,
            strict=True,
        )
    finally:
        event.remove(engine, "before_cursor_execute", record_statement)

    delete_queries = [
        item
        for item in statements
        if item[0].startswith("delete from scanner_metrics ")
    ]
    assert saved == symbols
    assert metric_write_sizes == [2, 2, 1]
    assert len(delete_queries) == 3
    assert all(len(parameters) <= 3 for _, parameters in delete_queries)


def test_scanner_snapshot_chunk_failure_rolls_back_replacement(engine, monkeypatch):
    snapshot_date = dt.datetime(2026, 1, 9)
    original_metrics = [
        {"symbol": "AAPL", "return_1m": 1.0},
        {"symbol": "MSFT", "return_1m": 2.0},
    ]
    assert db_loader.save_scanner_metrics_snapshot_to_db(
        original_metrics,
        snapshot_date,
        "old-fingerprint",
        engine,
    ) == ["AAPL", "MSFT"]

    monkeypatch.setattr(scanner, "SCANNER_METRIC_WRITE_CHUNK_SIZE", 1)
    original_upsert = scanner._execute_bulk_upsert
    metric_writes = 0

    def fail_second_metric_chunk(conn, table, records, key_columns, dialect_name):
        nonlocal metric_writes
        if table.name == "scanner_metrics":
            metric_writes += 1
            if metric_writes == 2:
                raise SQLAlchemyError("simulated scanner write failure")
        return original_upsert(conn, table, records, key_columns, dialect_name)

    monkeypatch.setattr(
        scanner, "_execute_bulk_upsert", fail_second_metric_chunk
    )
    saved = db_loader.save_scanner_metrics_snapshot_to_db(
        [
            {"symbol": "AAPL", "return_1m": 10.0},
            {"symbol": "GOOG", "return_1m": 20.0},
        ],
        snapshot_date,
        "new-fingerprint",
        engine,
        snapshot_symbols=["AAPL", "MSFT", "GOOG"],
    )

    assert saved == []
    loaded = db_loader.load_scanner_metrics_from_db(
        ["AAPL", "MSFT", "GOOG"], engine, snapshot_date
    )
    assert {row["symbol"]: row["return_1m"] for row in loaded} == {
        "AAPL": 1.0,
        "MSFT": 2.0,
    }
    snapshots = db_loader._ensure_scanner_metric_snapshots_table(engine)
    with engine.connect() as conn:
        fingerprint = conn.execute(
            select(snapshots.c.input_fingerprint).where(
                snapshots.c.snapshot_date == snapshot_date
            )
        ).scalar_one()
    assert fingerprint == "old-fingerprint"


def test_scanner_snapshot_strict_error_keeps_driver_message(engine, monkeypatch):
    monkeypatch.setattr(
        scanner,
        "_execute_bulk_upsert",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            OperationalError(
                "INSERT",
                {},
                RuntimeError("driver write timed out"),
            )
        ),
    )

    with pytest.raises(RuntimeError, match="driver write timed out") as error:
        db_loader.save_scanner_metrics_snapshot_to_db(
            [{"symbol": "AAPL", "return_1m": 1.0}],
            dt.datetime(2026, 1, 9),
            "fingerprint",
            engine,
            strict=True,
        )

    assert isinstance(error.value.__cause__, OperationalError)


def test_scanner_cache_hit_skips_history_loading_and_calculation(engine, monkeypatch):
    dates = pd.bdate_range("2026-01-05", periods=5)
    _seed_daily_history(engine, dates)
    symbols = ["SPY", "AAPL"]
    watermarks = db_loader.get_price_history_watermarks(engine, symbols)
    snapshot_date = db_loader.scanner_metrics_snapshot_date()
    fingerprint = db_loader.scanner_metrics_input_fingerprint(symbols, watermarks)
    assert db_loader.save_scanner_metrics_snapshot_to_db(
        [{"symbol": "AAPL", "return_1m": 1.0}],
        snapshot_date,
        fingerprint,
        engine,
    ) == ["AAPL"]

    monkeypatch.setattr(
        scanner,
        "load_universe_history_from_db",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("current scanner metrics must not load history")
        ),
    )

    assert db_loader.refresh_scanner_metrics_to_db(["SPY", "AAPL"], engine) == []
    cached = db_loader.get_universe_stock_metrics_from_db(["AAPL"], engine)
    assert [item["symbol"] for item in cached] == ["AAPL"]


def test_scanner_reader_rejects_old_80_percent_style_partial_cache(engine, monkeypatch):
    dates = pd.bdate_range("2026-01-05", periods=5)
    _seed_daily_history(engine, dates)
    assert db_loader.save_symbol_history_to_db("MSFT", _history(dates, 140), engine)
    snapshot_date = db_loader.scanner_metrics_snapshot_date()
    old_inputs = ["SPY", "AAPL"]
    old_watermarks = db_loader.get_price_history_watermarks(engine, old_inputs)
    old_fingerprint = db_loader.scanner_metrics_input_fingerprint(
        old_inputs, old_watermarks
    )
    assert db_loader.save_scanner_metrics_snapshot_to_db(
        [{"symbol": "AAPL", "return_1m": 1.0}],
        snapshot_date,
        old_fingerprint,
        engine,
    ) == ["AAPL"]
    history_loads = []
    monkeypatch.setattr(
        scanner,
        "load_universe_history_from_db",
        lambda symbols, *args, **kwargs: history_loads.append(list(symbols)) or {},
    )

    assert db_loader.get_universe_stock_metrics_from_db(["AAPL", "MSFT"], engine) == []
    assert history_loads == [["SPY", "AAPL", "MSFT"]]


def test_scanner_fingerprint_tracks_spy_and_cache_version(engine, monkeypatch):
    dates = pd.bdate_range("2026-01-05", periods=3)
    _seed_daily_history(engine, dates)
    symbols = ["SPY", "AAPL"]
    original = db_loader.get_price_history_watermarks(engine, symbols)
    first = db_loader.scanner_metrics_input_fingerprint(symbols, original)

    later_spy = _history([pd.Timestamp("2026-01-08")], 250)
    assert db_loader.save_symbol_history_to_db("SPY", later_spy, engine)
    changed = db_loader.get_price_history_watermarks(engine, symbols)
    second = db_loader.scanner_metrics_input_fingerprint(symbols, changed)
    monkeypatch.setattr(
        scanner,
        "SCANNER_METRICS_CACHE_VERSION",
        db_loader.SCANNER_METRICS_CACHE_VERSION + 1,
    )
    third = db_loader.scanner_metrics_input_fingerprint(symbols, changed)

    assert first != second
    assert second != third
