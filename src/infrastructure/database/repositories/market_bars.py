"""Market-bar persistence and retrieval."""

import datetime as dt
from typing import Dict, List, Optional, Tuple

import pandas as pd
from sqlalchemy import MetaData, delete, func, select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from src.utils.data_loader import _extract_symbol_history

from ..schema import (_ensure_hourly_price_history_table,
                      _ensure_intraday_price_history_table,
                      _ensure_price_history_table,
                      _get_hourly_price_history_table,
                      _get_intraday_price_history_table,
                      _get_price_history_table)
from ..settings import CACHE_QUERY_SYMBOL_CHUNK_SIZE
from ..sql_helpers import _clean_symbols, _execute_bulk_upsert, _record_chunks
from ..time_utils import _utcnow_naive


def _normalize_timestamp(ts: pd.Timestamp) -> dt.datetime:
    if ts.tzinfo is not None:
        ts = ts.tz_convert("UTC")
    return ts.tz_localize(None).to_pydatetime()


def _float_or_none(value) -> Optional[float]:
    if pd.isna(value):
        return None
    return float(value)


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


