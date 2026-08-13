"""Market-data watermark and freshness queries."""

import datetime as dt
from typing import Dict, List, Optional, Tuple

from sqlalchemy import MetaData, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from ..schema import (_ensure_hourly_price_history_table,
                      _ensure_price_history_table,
                      _get_hourly_price_history_table,
                      _get_price_history_table)
from ..settings import (CACHE_QUERY_SYMBOL_CHUNK_SIZE,
                        HOURLY_CACHE_QUERY_SYMBOL_CHUNK_SIZE)
from ..sql_helpers import _clean_symbols, _record_chunks


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
