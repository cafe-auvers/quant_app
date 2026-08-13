"""Read-only local mirror freshness checks."""

import datetime as dt
from typing import List, Optional

from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from .repositories.market_watermarks import \
    get_latest_hourly_price_history_timestamps
from .schema import (_ensure_price_history_table,
                     get_chronically_failing_symbols)
from .settings import REFERENCE_SYMBOL
from .sql_helpers import _clean_symbols


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
