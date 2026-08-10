import datetime as dt

import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError

import src.utils.db_loader as db_loader


@pytest.fixture
def sqlite_engine():
    engine = create_engine("sqlite:///:memory:", future=True)
    db_loader._ensured_engines.discard(id(engine))
    try:
        yield engine
    finally:
        db_loader._ensured_engines.discard(id(engine))
        engine.dispose()


def _single_symbol_history(timestamp: str) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "Open": [10.0],
            "High": [11.0],
            "Low": [9.0],
            "Close": [10.5],
            "Volume": [1000.0],
        },
        index=[pd.Timestamp(timestamp)],
    )


def test_daily_refresh_counts_old_nonempty_history_as_failure(monkeypatch, sqlite_engine):
    engine = sqlite_engine
    expected_date = dt.date(2026, 6, 23)
    old_history = _single_symbol_history("2026-06-20")

    monkeypatch.setattr(db_loader, "expected_latest_market_data_date", lambda: expected_date)
    monkeypatch.setattr(db_loader, "download_price_history", lambda *args, **kwargs: old_history)

    for _ in range(db_loader.CHRONIC_FAILURE_THRESHOLD):
        updated = db_loader.refresh_universe_history_to_db(
            ["OLD"], engine, chunk_size=1, batch_sleep=0, retry_attempts=0
        )
        assert updated == ["OLD"]

    assert db_loader.get_chronically_failing_symbols(engine, "1d") == {"OLD"}
    assert db_loader.get_chronically_failing_symbols(engine, "1h") == set()

    fresh_history = _single_symbol_history("2026-06-23")
    monkeypatch.setattr(db_loader, "download_price_history", lambda *args, **kwargs: fresh_history)
    db_loader.refresh_universe_history_to_db(
        ["OLD"], engine, chunk_size=1, batch_sleep=0, retry_attempts=0
    )

    assert db_loader.get_chronically_failing_symbols(engine, "1d") == set()


def test_daily_refresh_retries_nonempty_stale_history_and_accepts_fresh_retry(
    monkeypatch, sqlite_engine
):
    engine = sqlite_engine
    expected_date = dt.date(2026, 6, 23)
    responses = iter(
        [
            _single_symbol_history("2026-06-20"),
            _single_symbol_history("2026-06-23"),
        ]
    )
    calls = []
    logs = []

    def download(*args, **kwargs):
        calls.append((args, kwargs))
        return next(responses)

    monkeypatch.setattr(db_loader, "expected_latest_market_data_date", lambda: expected_date)
    monkeypatch.setattr(db_loader, "download_price_history", download)
    monkeypatch.setattr(db_loader.time, "sleep", lambda _seconds: None)

    for _ in range(db_loader.CHRONIC_FAILURE_THRESHOLD):
        db_loader.record_symbol_refresh_outcomes(engine, "1d", [], ["STALE"])
    assert db_loader.get_chronically_failing_symbols(engine, "1d") == {"STALE"}

    updated = db_loader.refresh_universe_history_to_db(
        ["STALE"],
        engine,
        chunk_size=1,
        batch_sleep=0,
        retry_attempts=1,
        log_callback=logs.append,
    )

    assert updated == ["STALE"]
    assert len(calls) == 2
    assert any("Retry 1/1 for 1 1d symbols" in message for message in logs)
    latest, row_count = db_loader.get_price_history_watermarks(engine, ["STALE"])["STALE"]
    assert latest.date() == expected_date
    assert row_count == 2
    assert db_loader.get_chronically_failing_symbols(engine, "1d") == set()


def test_hourly_refresh_counts_old_nonempty_history_as_failure(monkeypatch, sqlite_engine):
    engine = sqlite_engine
    expected_date = dt.date(2026, 6, 23)
    old_history = _single_symbol_history("2026-06-20 15:30:00")

    monkeypatch.setattr(db_loader, "expected_latest_market_data_date", lambda: expected_date)
    monkeypatch.setattr(db_loader, "download_price_history", lambda *args, **kwargs: old_history)

    for _ in range(db_loader.CHRONIC_FAILURE_THRESHOLD):
        updated = db_loader.refresh_universe_hourly_history_to_db(
            ["OLD"], engine, chunk_size=1, batch_sleep=0, retry_attempts=0
        )
        assert updated == ["OLD"]

    assert db_loader.get_chronically_failing_symbols(engine, "1h") == {"OLD"}

    fresh_history = _single_symbol_history("2026-06-23 15:30:00")
    monkeypatch.setattr(db_loader, "download_price_history", lambda *args, **kwargs: fresh_history)
    db_loader.refresh_universe_hourly_history_to_db(
        ["OLD"], engine, chunk_size=1, batch_sleep=0, retry_attempts=0
    )

    assert db_loader.get_chronically_failing_symbols(engine, "1h") == set()


def test_provider_outage_only_advances_reference_symbol_failure_streak(
    monkeypatch, sqlite_engine
):
    expected_date = dt.date(2026, 6, 23)
    monkeypatch.setattr(
        db_loader, "expected_latest_market_data_date", lambda: expected_date
    )
    monkeypatch.setattr(
        db_loader, "download_price_history", lambda *args, **kwargs: pd.DataFrame()
    )

    for _ in range(db_loader.CHRONIC_FAILURE_THRESHOLD):
        db_loader.refresh_universe_history_to_db(
            ["SPY", "AAPL"],
            sqlite_engine,
            chunk_size=2,
            batch_sleep=0,
            retry_attempts=0,
        )

    assert db_loader.get_chronically_failing_symbols(sqlite_engine, "1d") == {
        "SPY"
    }


@pytest.mark.parametrize(
    ("refresh", "extra_kwargs", "expected_log"),
    [
        (
            db_loader.refresh_universe_history_to_db,
            {"interval": "1d"},
            "1d symbol(s) remained unavailable",
        ),
        (
            db_loader.refresh_universe_hourly_history_to_db,
            {},
            "1h symbol(s) remained unavailable",
        ),
    ],
)
def test_refresh_does_not_immediately_repeat_failed_batch(
    monkeypatch,
    sqlite_engine,
    refresh,
    extra_kwargs,
    expected_log,
):
    calls = []
    logs = []
    monkeypatch.setattr(
        db_loader,
        "download_price_history",
        lambda *args, **kwargs: calls.append((args, kwargs)) or pd.DataFrame(),
    )

    updated = refresh(
        ["MISSING"],
        sqlite_engine,
        chunk_size=1,
        batch_sleep=0,
        log_callback=logs.append,
        **extra_kwargs,
    )

    assert updated == []
    assert len(calls) == 1
    assert any(expected_log in message for message in logs)


def test_daily_refresh_resets_streak_when_existing_cache_is_current(monkeypatch, sqlite_engine):
    engine = sqlite_engine
    expected_date = dt.date(2026, 6, 23)
    current_history = _single_symbol_history("2026-06-23")
    assert db_loader.save_symbol_history_to_db("CACHED", current_history, engine, interval="1d")

    for _ in range(db_loader.CHRONIC_FAILURE_THRESHOLD):
        db_loader.record_symbol_refresh_outcomes(engine, "1d", [], ["CACHED"])
    assert db_loader.get_chronically_failing_symbols(engine, "1d") == {"CACHED"}

    monkeypatch.setattr(db_loader, "expected_latest_market_data_date", lambda: expected_date)
    monkeypatch.setattr(
        db_loader, "download_price_history", lambda *args, **kwargs: pd.DataFrame()
    )

    updated = db_loader.refresh_universe_history_to_db(
        ["CACHED"], engine, chunk_size=1, batch_sleep=0, retry_attempts=0
    )

    assert updated == []
    assert db_loader.get_chronically_failing_symbols(engine, "1d") == set()


def test_refresh_failure_bookkeeping_ignores_table_creation_errors(monkeypatch, sqlite_engine):
    engine = sqlite_engine

    def fail_table_creation(_engine):
        raise OperationalError("CREATE TABLE", {}, RuntimeError("permission denied"))

    monkeypatch.setattr(db_loader, "_ensure_symbol_refresh_failures_table", fail_table_creation)

    db_loader.record_symbol_refresh_outcomes(engine, "1d", [], ["AAPL"])

    assert db_loader.get_chronically_failing_symbols(engine, "1d") == set()


@pytest.mark.parametrize(
    ("ensure_name", "load_history"),
    [
        (
            "_ensure_price_history_table",
            lambda engine: db_loader.load_symbol_history_from_db("AAPL", engine),
        ),
        (
            "_ensure_hourly_price_history_table",
            lambda engine: db_loader.load_hourly_history_from_db("AAPL", engine),
        ),
        (
            "_ensure_intraday_price_history_table",
            lambda engine: db_loader.load_intraday_history_from_db(
                "AAPL", engine, interval="5m"
            ),
        ),
    ],
    ids=["daily", "hourly", "intraday"],
)
def test_history_loaders_return_empty_when_table_creation_fails(
    monkeypatch, sqlite_engine, ensure_name, load_history
):
    def fail_table_creation(_engine):
        raise OperationalError(
            "CREATE TABLE", {}, RuntimeError("database went offline")
        )

    monkeypatch.setattr(db_loader, ensure_name, fail_table_creation)

    history = load_history(sqlite_engine)

    assert isinstance(history, pd.DataFrame)
    assert history.empty
