import os
import datetime as dt
import time
import random
from typing import Callable, Dict, List, Optional, Tuple

import pandas as pd
import numpy as np
from sqlalchemy import (
    create_engine,
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
)
from sqlalchemy.engine import Engine, URL
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from src.utils.config import get_mysql_config
from src.utils.data_loader import download_price_history, _extract_symbol_history, compute_stock_metrics

_ensured_engines: set = set()

def _utcnow_naive() -> dt.datetime:
    """Return a naive UTC timestamp for existing DB columns and comparisons."""
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def get_mysql_connection_url(db_name: Optional[str] = None) -> URL:
    config = get_mysql_config()
    if db_name is None:
        db_name = config["database"]

    host = config["host"]
    port = int(config["port"])
    user = config["user"]
    password = config["password"]

    return URL.create(
        drivername="mysql+pymysql",
        username=user or None,
        password=password or None,
        host=host,
        port=port,
        database=db_name,
        query={"charset": "utf8mb4"},
    )


def init_mysql_engine(db_name: str = "quant_app") -> Optional[Engine]:
    try:
        base_url = get_mysql_connection_url(db_name="mysql")
        base_engine = create_engine(base_url, future=True)
        with base_engine.connect() as conn:
            conn.execute(text(f"CREATE DATABASE IF NOT EXISTS `{db_name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"))
            conn.commit()

        engine = create_engine(get_mysql_connection_url(db_name=db_name), future=True)
        _ensure_price_history_table(engine)
        _ensure_hourly_price_history_table(engine)
        _ensure_chart_indicators_table(engine)
        _ensure_intraday_price_history_table(engine)
        _ensure_scanner_metrics_table(engine)
        return engine
    except SQLAlchemyError:
        return None


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
    _ensure_hourly_price_history_table(engine)
    stmt = select(hourly_history).where(hourly_history.c.symbol == symbol.strip().upper())
    if source:
        stmt = stmt.where(hourly_history.c.source == source)
    if start is not None:
        stmt = stmt.where(hourly_history.c.timestamp >= start)
    if end is not None:
        stmt = stmt.where(hourly_history.c.timestamp <= end)
    stmt = stmt.order_by(hourly_history.c.timestamp)

    try:
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
    _ensure_hourly_price_history_table(engine)
    stmt = select(func.max(hourly_history.c.timestamp))
    if symbol:
        stmt = stmt.where(hourly_history.c.symbol == symbol.strip().upper())
    if source:
        stmt = stmt.where(hourly_history.c.source == source)

    try:
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
    _ensure_intraday_price_history_table(engine)

    stmt = select(intraday_history).where(
        intraday_history.c.symbol == symbol.upper(),
        intraday_history.c.interval == interval,
    )
    if source:
        stmt = stmt.where(intraday_history.c.source == source)
    if since is not None:
        stmt = stmt.where(intraday_history.c.timestamp >= since)
    stmt = stmt.order_by(intraday_history.c.timestamp)

    with engine.connect() as conn:
        df = pd.read_sql(stmt, conn)

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
    _ensure_price_history_table(engine)
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
    _ensure_price_history_table(engine)
    symbols = [ticker.strip().upper() for ticker in tickers if ticker.strip()]
    stmt = select(price_history).where(
        price_history.c.symbol.in_(symbols),
        price_history.c.interval == interval.strip().lower(),
    )
    if start is not None:
        stmt = stmt.where(price_history.c.date >= start)
    if end is not None:
        stmt = stmt.where(price_history.c.date <= end)
    stmt = stmt.order_by(price_history.c.symbol, price_history.c.date)

    try:
        with engine.connect() as conn:
            rows = conn.execute(stmt).all()
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


def save_chart_indicators_to_db(symbol: str, indicators: pd.DataFrame, engine: Engine) -> bool:
    if indicators.empty:
        return False

    metadata = MetaData()
    chart_indicators = _get_chart_indicators_table(metadata)
    records = _chart_indicator_records(indicators, chart_indicators)
    if not records:
        return False

    try:
        with engine.begin() as conn:
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


def save_chart_indicators_batch_to_db(records: List[dict], engine: Engine) -> int:
    if not records:
        return 0

    metadata = MetaData()
    chart_indicators = _get_chart_indicators_table(metadata)
    _ensure_chart_indicators_table(engine)
    try:
        with engine.begin() as conn:
            return _execute_bulk_upsert(
                conn,
                chart_indicators,
                records,
                ("symbol", "date"),
                engine.dialect.name,
            )
    except SQLAlchemyError:
        return 0


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
) -> List[str]:
    updated = []
    symbols = [
        symbol.strip().upper()
        for symbol in tickers
        if symbol.strip() and symbol.strip().upper() != reference_symbol
    ]
    total = len(symbols)
    start_ts = time.time()
    progress_every = max(1, min(100, total // 20 or 1))
    if log_callback:
        log_callback(f"Calculating chart indicators: 0/{total} (0%) - ETA calculating...")

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
        saved_count = save_chart_indicators_batch_to_db(pending_records, engine)
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
            indicators = calculate_chart_indicators(symbol, history, spy_history)
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
    _ensure_price_history_table(engine)
    stmt = select(func.max(price_history.c.date)).where(price_history.c.interval == interval.strip().lower())

    try:
        with engine.connect() as conn:
            latest_date = conn.execute(stmt).scalar_one_or_none()
    except SQLAlchemyError:
        return None

    return latest_date


def get_latest_price_history_dates(
    engine: Engine,
    symbols: List[str],
    interval: str = "1d",
) -> Dict[str, dt.datetime]:
    """Return latest cached daily/intraday price_history date per symbol."""
    cleaned_symbols = _clean_symbols(symbols)
    if not cleaned_symbols:
        return {}

    metadata = MetaData()
    price_history = _get_price_history_table(metadata)
    _ensure_price_history_table(engine)
    stmt = (
        select(price_history.c.symbol, func.max(price_history.c.date).label("latest_date"))
        .where(
            price_history.c.symbol.in_(cleaned_symbols),
            price_history.c.interval == interval.strip().lower(),
        )
        .group_by(price_history.c.symbol)
    )

    try:
        with engine.connect() as conn:
            rows = conn.execute(stmt).all()
    except SQLAlchemyError:
        return {}

    return {str(row.symbol).upper(): row.latest_date for row in rows if row.latest_date is not None}


def get_latest_hourly_price_history_timestamps(
    engine: Engine,
    symbols: List[str],
    source: Optional[str] = None,
) -> Dict[str, dt.datetime]:
    """Return latest cached 1-hour timestamp per symbol."""
    cleaned_symbols = _clean_symbols(symbols)
    if not cleaned_symbols:
        return {}

    metadata = MetaData()
    hourly_history = _get_hourly_price_history_table(metadata)
    _ensure_hourly_price_history_table(engine)
    stmt = select(hourly_history.c.symbol, func.max(hourly_history.c.timestamp).label("latest_timestamp")).where(
        hourly_history.c.symbol.in_(cleaned_symbols)
    )
    if source:
        stmt = stmt.where(hourly_history.c.source == source)
    stmt = stmt.group_by(hourly_history.c.symbol)

    try:
        with engine.connect() as conn:
            rows = conn.execute(stmt).all()
    except SQLAlchemyError:
        return {}

    return {str(row.symbol).upper(): row.latest_timestamp for row in rows if row.latest_timestamp is not None}


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


def _symbols_with_history(history: pd.DataFrame, symbols: List[str]) -> List[str]:
    if history.empty:
        return []
    available = []
    for symbol in symbols:
        symbol_history = _extract_symbol_history(history, symbol)
        if symbol_history is not None and not symbol_history.empty:
            available.append(symbol)
    return available


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
    retry_attempts: int = 1,
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
                chart_fallback=False,
            )
            available = _symbols_with_history(history, batch)
            missing = [symbol for symbol in batch if symbol not in available]
            rows_saved = save_universe_history_batch_to_db(history, available, engine, interval=interval)

            if rows_saved:
                updated.extend(available)
            failed.extend(missing)
            processed += len(batch)

            if log_callback:
                log_callback(
                    f"Batch {batch_number}/{total_batches} {interval}: rows_saved={rows_saved}, "
                    f"failed={', '.join(missing) if missing else 'none'}"
                )
            _emit_batch_progress(progress_callback, batch, processed, total, start_ts)

            if batch_number < total_batches:
                _sleep_between_batches(batch_sleep)

    retry_symbols = [symbol for symbol in dict.fromkeys(failed) if symbol not in set(updated)]
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
                )
                available = _symbols_with_history(history, batch)
                missing = [symbol for symbol in batch if symbol not in available]
                rows_saved = save_universe_history_batch_to_db(history, available, engine, interval=interval)
                if rows_saved:
                    updated.extend(available)
                next_retry.extend(missing)
                if log_callback:
                    log_callback(
                        f"Retry {retry_index} {interval}: rows_saved={rows_saved}, "
                        f"failed={', '.join(missing) if missing else 'none'}"
                    )
                _sleep_between_batches(batch_sleep)
        retry_symbols = [symbol for symbol in dict.fromkeys(next_retry) if symbol not in set(updated)]

    if retry_symbols and log_callback:
        log_callback(f"Single-symbol fallback for {len(retry_symbols)} {interval} symbols...")

    for index, symbol in enumerate(retry_symbols, start=1):
        fetch_period = period_by_symbol[symbol]
        if log_callback:
            log_callback(f"Fallback {index}/{len(retry_symbols)} {interval}: {symbol}, period={fetch_period}")
        history = download_price_history(
            [symbol],
            period=fetch_period,
            interval=interval,
            max_symbols=1,
            chunk_size=1,
            threads=1,
            batch_sleep=0,
            max_retries=1,
            fallback_to_single=True,
        )
        symbol_df = _extract_symbol_history(history, symbol)
        if symbol_df is not None and not symbol_df.empty and save_symbol_history_to_db(symbol, symbol_df, engine, interval=interval):
            updated.append(symbol)
        elif log_callback:
            log_callback(f"Fallback {interval}: {symbol} failed")

    deduped_updated = list(dict.fromkeys(updated))
    if log_callback:
        unresolved = [symbol for symbol in retry_symbols if symbol not in set(deduped_updated)]
        log_callback(
            f"Completed {interval} yfinance refresh: updated={len(deduped_updated)}, "
            f"failed={len(unresolved)}, elapsed={_format_elapsed(time.time() - start_ts)}"
        )
        if unresolved:
            log_callback(f"Failed {interval} symbols: {', '.join(unresolved)}")

    return deduped_updated


def _period_for_hourly_refresh(
    latest_timestamp: Optional[dt.datetime],
    full_period: str = "730d",
    incremental_period: str = "10d",
    backfill: bool = False,
) -> str:
    if backfill:
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
    retry_attempts: int = 1,
    backfill: bool = False,
    incremental_period: str = "10d",
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

    latest_by_symbol = get_latest_hourly_price_history_timestamps(engine, symbols, source=source)
    period_by_symbol = {
        symbol: _period_for_hourly_refresh(
            latest_by_symbol.get(symbol),
            full_period=full_period,
            incremental_period=incremental_period,
            backfill=backfill,
        )
        for symbol in symbols
    }
    period_groups = _period_groups_for_symbols(period_by_symbol, symbols)

    if log_callback:
        mode = "full backfill" if backfill else "incremental"
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
                chart_fallback=False,
            )
            available = _symbols_with_history(history, batch)
            missing = [symbol for symbol in batch if symbol not in available]
            rows_saved = save_universe_hourly_history_batch_to_db(history, available, engine, source=source)

            if rows_saved:
                updated.extend(available)
            failed.extend(missing)
            processed += len(batch)

            if log_callback:
                log_callback(
                    f"Batch {batch_number}/{total_batches} 1h: rows_saved={rows_saved}, "
                    f"failed={', '.join(missing) if missing else 'none'}"
                )
            _emit_batch_progress(progress_callback, batch, processed, total, start_ts)

            if batch_number < total_batches:
                _sleep_between_batches(batch_sleep)

    retry_symbols = [symbol for symbol in dict.fromkeys(failed) if symbol not in set(updated)]
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
                    chart_fallback=False,
                )
                available = _symbols_with_history(history, batch)
                missing = [symbol for symbol in batch if symbol not in available]
                rows_saved = save_universe_hourly_history_batch_to_db(history, available, engine, source=source)
                if rows_saved:
                    updated.extend(available)
                next_retry.extend(missing)
                if log_callback:
                    log_callback(
                        f"Retry {retry_index} 1h: rows_saved={rows_saved}, "
                        f"failed={', '.join(missing) if missing else 'none'}"
                    )
                _sleep_between_batches(batch_sleep)
        retry_symbols = [symbol for symbol in dict.fromkeys(next_retry) if symbol not in set(updated)]

    if retry_symbols and log_callback:
        log_callback(f"Single-symbol fallback for {len(retry_symbols)} 1h symbols...")

    for index, symbol in enumerate(retry_symbols, start=1):
        fetch_period = period_by_symbol[symbol]
        if log_callback:
            log_callback(f"Fallback {index}/{len(retry_symbols)} 1h: {symbol}, period={fetch_period}")
        history = download_price_history(
            [symbol],
            period=fetch_period,
            interval="1h",
            max_symbols=1,
            chunk_size=1,
            threads=1,
            batch_sleep=0,
            max_retries=1,
            fallback_to_single=True,
        )
        symbol_df = _extract_symbol_history(history, symbol)
        if symbol_df is not None and not symbol_df.empty and save_hourly_history_to_db(symbol, symbol_df, engine, source=source):
            updated.append(symbol)
        elif log_callback:
            log_callback(f"Fallback 1h: {symbol} failed")

    deduped_updated = list(dict.fromkeys(updated))
    if log_callback:
        unresolved = [symbol for symbol in retry_symbols if symbol not in set(deduped_updated)]
        log_callback(
            f"Completed 1h yfinance refresh: updated={len(deduped_updated)}, "
            f"failed={len(unresolved)}, elapsed={_format_elapsed(time.time() - start_ts)}"
        )
        if unresolved:
            log_callback(f"Failed 1h symbols: {', '.join(unresolved)}")

    return deduped_updated


def get_universe_stock_metrics_from_db(
    tickers: List[str],
    engine: Engine,
    min_history_days: int = 1,
    lookback_days: int = 380,
) -> List[dict]:
    # Check if we have pre-calculated metrics in the database first
    today = _utcnow_naive().replace(hour=0, minute=0, second=0, microsecond=0)
    cached = load_scanner_metrics_from_db(tickers, engine, today)
    if len(cached) >= max(1, int(len(tickers) * 0.8)):
        return cached

    from src.utils.data_loader import compute_stock_metrics

    metrics = []
    start_date = _utcnow_naive() - dt.timedelta(days=lookback_days)
    db_tickers = list(dict.fromkeys(["SPY", *tickers]))
    histories = load_universe_history_from_db(db_tickers, engine, start=start_date)
    if not histories:
        return []

    spy_history = histories.get("SPY")
    if spy_history is None:
        try:
            spy_history = load_symbol_history_from_db("SPY", engine, interval="1d")
        except Exception:
            spy_history = None

    for symbol in tickers:
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
            _execute_bulk_upsert(
                conn,
                table,
                records,
                ("symbol", "date"),
                engine.dialect.name,
            )
        return [record["symbol"] for record in records]
    except SQLAlchemyError:
        return []


def load_scanner_metrics_from_db(tickers: List[str], engine: Engine, date: Optional[dt.datetime] = None) -> List[dict]:
    """Load cached scanner metrics from MySQL."""
    if date is None:
        date = _utcnow_naive().replace(hour=0, minute=0, second=0, microsecond=0)

    metadata = MetaData()
    table = _get_scanner_metrics_table(metadata)

    try:
        with engine.connect() as conn:
            stmt = select(table).where(
                (table.c.symbol.in_(tickers)) & (table.c.date == date)
            )
            rows = conn.execute(stmt).fetchall()

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
) -> List[str]:
    """Calculate and store scanner metrics for the universe in MySQL."""
    if log_callback:
        log_callback("Pre-calculating and saving scanner metrics to MySQL...")
        
    try:
        # Force a bypass of the cache lookup during generation by querying history
        # We need to temporarily mock/disable cache check or compute directly
        # Easiest way: call get_universe_stock_metrics_from_db with lookback_days
        # But wait, to prevent infinite recursion, we check if cached is loaded.
        # Since we haven't written to DB yet, cached should be empty anyway!
        # Just to be 100% sure we don't load stale cache, we can query on-the-fly:
        metrics_list = []
        start_date = _utcnow_naive() - dt.timedelta(days=380)
        db_tickers = list(dict.fromkeys(["SPY", *tickers]))
        histories = load_universe_history_from_db(db_tickers, engine, start=start_date)
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
        symbols = _clean_symbols(tickers)
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

        today = _utcnow_naive().replace(hour=0, minute=0, second=0, microsecond=0)
        save_total = len(metrics_list)
        save_start_ts = time.time()
        if log_callback:
            log_callback(f"Saving scanner metrics: 0/{save_total} (0%) - ETA calculating...")

        saved = save_scanner_metrics_batch_to_db(metrics_list, today, engine)
        if log_callback:
            elapsed = time.time() - save_start_ts
            log_callback(
                f"Scanner metrics save progress: {save_total}/{save_total} (100%) - "
                f"saved={len(saved)}, ETA 00:00, elapsed={_format_elapsed(elapsed)}"
            )
                
        if log_callback:
            log_callback(f"  Successfully saved scanner metrics for {len(saved)} symbols to MySQL.")
        return saved
    except Exception as exc:
        if log_callback:
            log_callback(f"  Failed to save scanner metrics: {exc}")
        return []
