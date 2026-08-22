"""Database table definitions and schema guards."""

import weakref
from typing import List, Set

from sqlalchemy import (Boolean, Column, Date, DateTime, Float, Index, Integer,
                        MetaData, String, Table, insert, inspect, select, text)
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from .sql_helpers import _clean_symbols, _execute_bulk_upsert
from .time_utils import _utcnow_naive

_ensured_engines: weakref.WeakSet[Engine] = weakref.WeakSet()

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
    metadata = MetaData()
    price_history = _get_price_history_table(metadata)
    if engine not in _ensured_engines:
        metadata.create_all(engine)
        _ensure_price_history_interval_column(engine)
        _ensured_engines.add(engine)
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


def _get_stock_profiles_table(metadata: MetaData) -> Table:
    """Company metadata keyed by the application's canonical symbol."""
    table = Table(
        "stock_profiles",
        metadata,
        Column("symbol", String(32), primary_key=True),
        Column("provider_symbol", String(32)),
        Column("company_name", String(255), nullable=False),
        Column("short_name", String(255)),
        Column("quote_type", String(40)),
        Column("exchange", String(40)),
        Column("market", String(80)),
        Column("currency", String(16)),
        Column("country", String(80)),
        Column("sector_name", String(160)),
        Column("sector_key", String(160)),
        Column("industry_name", String(200)),
        Column("industry_key", String(200)),
        Column("category", String(200)),
        Column("fund_family", String(200)),
        Column("profile_status", String(20), nullable=False),
        Column("source", String(40), nullable=False),
        Column("last_checked_at", DateTime, nullable=False),
        Column("last_successful_sync_at", DateTime),
        Column("created_at", DateTime, nullable=False, default=_utcnow_naive),
        Column("updated_at", DateTime, nullable=False, default=_utcnow_naive),
    )
    Index("ix_stock_profiles_sector_key", table.c.sector_key)
    Index("ix_stock_profiles_industry_key", table.c.industry_key)
    return table


def _ensure_stock_profiles_table(engine: Engine) -> Table:
    metadata = MetaData()
    table = _get_stock_profiles_table(metadata)
    metadata.create_all(engine)
    return table


def _get_earnings_events_table(metadata: MetaData) -> Table:
    """Quarterly reported and mutable expected earnings events."""
    table = Table(
        "earnings_events",
        metadata,
        Column("symbol", String(32), primary_key=True),
        Column("event_key", String(80), primary_key=True),
        Column("report_date", Date, nullable=False),
        Column("report_datetime_utc", DateTime),
        Column("fiscal_period_end", Date),
        Column("event_status", String(20), nullable=False),
        Column("report_timing", String(20), nullable=False),
        Column("is_date_estimated", Boolean, nullable=False, default=False),
        Column("reported_eps", Float),
        Column("estimated_eps", Float),
        Column("statement_diluted_eps", Float),
        Column("statement_basic_eps", Float),
        Column("eps_basis", String(20)),
        Column("eps_surprise", Float),
        Column("eps_surprise_pct", Float),
        Column("revenue", Float),
        Column("estimated_revenue", Float),
        Column("eps_yoy_growth_pct", Float),
        Column("previous_eps_yoy_growth_pct", Float),
        Column("eps_growth_status", String(40), nullable=False),
        Column("previous_eps_growth_status", String(40), nullable=False),
        Column("revenue_yoy_growth_pct", Float),
        Column("previous_revenue_yoy_growth_pct", Float),
        Column("ttm_eps", Float),
        Column("source", String(40), nullable=False),
        Column("source_updated_at", DateTime, nullable=False),
        Column("created_at", DateTime, nullable=False, default=_utcnow_naive),
        Column("updated_at", DateTime, nullable=False, default=_utcnow_naive),
    )
    Index("ix_earnings_events_symbol_report_date", table.c.symbol, table.c.report_date)
    return table


def _ensure_earnings_events_table(engine: Engine) -> Table:
    metadata = MetaData()
    table = _get_earnings_events_table(metadata)
    metadata.create_all(engine)
    return table


def _get_fundamental_sync_state_table(metadata: MetaData) -> Table:
    """Positive/negative cache state for provider datasets, including empty results."""
    return Table(
        "fundamental_sync_state",
        metadata,
        Column("symbol", String(32), primary_key=True),
        Column("dataset", String(24), primary_key=True),
        Column("status", String(20), nullable=False),
        Column("source", String(40), nullable=False),
        Column("payload_fingerprint", String(64)),
        Column("last_error", String(500)),
        Column("last_checked_at", DateTime, nullable=False),
        Column("last_successful_sync_at", DateTime),
        Column("created_at", DateTime, nullable=False, default=_utcnow_naive),
        Column("updated_at", DateTime, nullable=False, default=_utcnow_naive),
    )


def _ensure_fundamental_sync_state_table(engine: Engine) -> Table:
    metadata = MetaData()
    table = _get_fundamental_sync_state_table(metadata)
    metadata.create_all(engine)
    return table
