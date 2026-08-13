"""History refresh orchestration."""

import datetime as dt
import random
import time
from typing import Callable, Dict, List, Optional, Set, Tuple

import pandas as pd
from sqlalchemy.engine import Engine

from src.utils.data_loader import (_extract_symbol_history,
                                   download_price_history)
from src.utils.market_calendar import expected_latest_market_data_date

from .formatting import _format_elapsed, _format_eta
from .repositories.market_bars import (
    save_universe_history_batch_to_db,
    save_universe_hourly_history_batch_to_db)
from .repositories.market_watermarks import (
    get_latest_hourly_price_history_timestamps, get_latest_price_history_dates)
from .schema import record_symbol_refresh_outcomes
from .settings import REFERENCE_SYMBOL
from .sql_helpers import _clean_symbols
from .time_utils import _utcnow_naive


def _chunk_symbols(symbols: List[str], chunk_size: int) -> List[List[str]]:
    size = max(1, int(chunk_size or 1))
    return [symbols[index:index + size] for index in range(0, len(symbols), size)]


def _symbols_with_history(
    history: pd.DataFrame,
    symbols: List[str],
    required_latest_date: Optional[dt.date] = None,
) -> List[str]:
    if history.empty:
        return []
    available = []
    for symbol in symbols:
        symbol_history = _extract_symbol_history(history, symbol)
        if symbol_history is None or symbol_history.empty:
            continue
        if required_latest_date is not None:
            try:
                latest = pd.Timestamp(symbol_history.index.max())
            except (TypeError, ValueError):
                continue
            if pd.isna(latest) or latest.date() < required_latest_date:
                continue
        available.append(symbol)
    return available


def _partition_symbols_by_freshness(
    latest_by_symbol: Dict[str, object],
    symbols: List[str],
    expected_date: dt.date,
) -> Tuple[List[str], List[str]]:
    """Split symbols by whether their cached data reaches the expected session."""
    current: List[str] = []
    stale: List[str] = []
    for symbol in _clean_symbols(symbols):
        latest = latest_by_symbol.get(symbol)
        if latest is None or pd.isna(latest):
            stale.append(symbol)
            continue
        try:
            latest_date = pd.Timestamp(latest).date()
        except (TypeError, ValueError):
            stale.append(symbol)
            continue
        (current if latest_date >= expected_date else stale).append(symbol)
    return current, stale


def _record_history_refresh_outcomes(
    engine: Engine,
    interval: str,
    current_symbols: List[str],
    stale_symbols: List[str],
) -> None:
    """Record symbol failures without mistaking a provider outage for bad tickers.

    If SPY itself could not reach the expected session, failures for the rest
    of that payload are not evidence that each individual symbol is unusable.
    Only advance the canary's streak; current symbols still reset normally.
    """
    failures = (
        [REFERENCE_SYMBOL]
        if REFERENCE_SYMBOL in set(stale_symbols)
        else stale_symbols
    )
    record_symbol_refresh_outcomes(
        engine, interval, current_symbols, failures
    )


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
    retry_attempts: int = 0,
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
    current_from_download: Set[str] = set()
    expected_date = expected_latest_market_data_date()

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
                chart_fallback=True,
                required_latest_date=expected_date,
            )
            available = _symbols_with_history(history, batch)
            current = _symbols_with_history(
                history, batch, required_latest_date=expected_date
            )
            current_from_download.update(current)
            not_current = [symbol for symbol in batch if symbol not in current]
            rows_saved = save_universe_history_batch_to_db(history, available, engine, interval=interval)

            if rows_saved:
                updated.extend(available)
            failed.extend(not_current)
            processed += len(batch)

            if log_callback:
                log_callback(
                    f"Batch {batch_number}/{total_batches} {interval}: rows_saved={rows_saved}, "
                    f"not_current={', '.join(not_current) if not_current else 'none'}"
                )
            _emit_batch_progress(progress_callback, batch, processed, total, start_ts)

            if batch_number < total_batches:
                _sleep_between_batches(batch_sleep)

    retry_symbols = [
        symbol
        for symbol in dict.fromkeys(failed)
        if symbol not in current_from_download
    ]
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
                    chart_fallback=True,
                    required_latest_date=expected_date,
                )
                available = _symbols_with_history(history, batch)
                current = _symbols_with_history(
                    history, batch, required_latest_date=expected_date
                )
                current_from_download.update(current)
                not_current = [symbol for symbol in batch if symbol not in current]
                rows_saved = save_universe_history_batch_to_db(history, available, engine, interval=interval)
                if rows_saved:
                    updated.extend(available)
                next_retry.extend(not_current)
                if log_callback:
                    log_callback(
                        f"Retry {retry_index} {interval}: rows_saved={rows_saved}, "
                        f"not_current={', '.join(not_current) if not_current else 'none'}"
                    )
                _sleep_between_batches(batch_sleep)
        retry_symbols = [
            symbol
            for symbol in dict.fromkeys(next_retry)
            if symbol not in current_from_download
        ]

    if retry_symbols and log_callback:
        log_callback(
            f"{len(retry_symbols)} {interval} symbol(s) remained unavailable or stale "
            f"through {expected_date} after batch and concurrent chart fallback; "
            "skipping redundant serial retries."
        )

    deduped_updated = list(dict.fromkeys(updated))
    latest_after_refresh = get_latest_price_history_dates(engine, symbols, interval=interval)
    current_symbols, stale_symbols = _partition_symbols_by_freshness(
        latest_after_refresh, symbols, expected_date
    )
    if log_callback:
        log_callback(
            f"Completed {interval} yfinance refresh: "
            f"received_rows_for={len(deduped_updated)}, "
            f"current={len(current_symbols)}, still_stale={len(stale_symbols)}, "
            f"elapsed={_format_elapsed(time.time() - start_ts)}"
        )
        if stale_symbols:
            log_callback(f"Still-stale {interval} symbols: {', '.join(stale_symbols)}")

    _record_history_refresh_outcomes(
        engine, interval, current_symbols, stale_symbols
    )

    return deduped_updated


HOURLY_INCREMENTAL_REFRESH_DAYS = 10
HOURLY_INCREMENTAL_REFRESH_PERIOD = f"{HOURLY_INCREMENTAL_REFRESH_DAYS}d"
HOURLY_BACKFILL_DAYS = 200
HOURLY_BACKFILL_PERIOD = f"{HOURLY_BACKFILL_DAYS}d"


def _period_for_hourly_refresh(
    *,
    full_period: str = HOURLY_BACKFILL_PERIOD,
    incremental_period: str = HOURLY_INCREMENTAL_REFRESH_PERIOD,
    backfill: bool = False,
) -> str:
    """Keep routine D-10 refreshes separate from explicit D-200 repair runs."""
    return full_period if backfill else incremental_period


def refresh_universe_hourly_history_to_db(
    tickers: List[str],
    engine: Engine,
    full_period: str = HOURLY_BACKFILL_PERIOD,
    source: str = "yfinance",
    chunk_size: int = 100,
    threads: int = 8,
    batch_sleep: float = 1.5,
    retry_attempts: int = 0,
    backfill: bool = False,
    incremental_period: str = HOURLY_INCREMENTAL_REFRESH_PERIOD,
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
    current_from_download: Set[str] = set()
    expected_date = expected_latest_market_data_date()

    fetch_period = _period_for_hourly_refresh(
        full_period=full_period,
        incremental_period=incremental_period,
        backfill=backfill,
    )
    period_by_symbol = {symbol: fetch_period for symbol in symbols}
    period_groups = _period_groups_for_symbols(period_by_symbol, symbols)

    if log_callback:
        mode = "forced D-200 backfill" if backfill else "routine D-10"
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
                chart_fallback=True,
                required_latest_date=expected_date,
            )
            available = _symbols_with_history(history, batch)
            current = _symbols_with_history(
                history, batch, required_latest_date=expected_date
            )
            current_from_download.update(current)
            not_current = [symbol for symbol in batch if symbol not in current]
            rows_saved = save_universe_hourly_history_batch_to_db(history, available, engine, source=source)

            if rows_saved:
                updated.extend(available)
            failed.extend(not_current)
            processed += len(batch)

            if log_callback:
                log_callback(
                    f"Batch {batch_number}/{total_batches} 1h: rows_saved={rows_saved}, "
                    f"not_current={', '.join(not_current) if not_current else 'none'}"
                )
            _emit_batch_progress(progress_callback, batch, processed, total, start_ts)

            if batch_number < total_batches:
                _sleep_between_batches(batch_sleep)

    retry_symbols = [
        symbol
        for symbol in dict.fromkeys(failed)
        if symbol not in current_from_download
    ]
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
                    chart_fallback=True,
                    required_latest_date=expected_date,
                )
                available = _symbols_with_history(history, batch)
                current = _symbols_with_history(
                    history, batch, required_latest_date=expected_date
                )
                current_from_download.update(current)
                not_current = [symbol for symbol in batch if symbol not in current]
                rows_saved = save_universe_hourly_history_batch_to_db(history, available, engine, source=source)
                if rows_saved:
                    updated.extend(available)
                next_retry.extend(not_current)
                if log_callback:
                    log_callback(
                        f"Retry {retry_index} 1h: rows_saved={rows_saved}, "
                        f"not_current={', '.join(not_current) if not_current else 'none'}"
                    )
                _sleep_between_batches(batch_sleep)
        retry_symbols = [
            symbol
            for symbol in dict.fromkeys(next_retry)
            if symbol not in current_from_download
        ]

    if retry_symbols and log_callback:
        log_callback(
            f"{len(retry_symbols)} 1h symbol(s) remained unavailable or stale through "
            f"{expected_date} after batch and concurrent chart fallback; "
            "skipping redundant serial retries."
        )

    deduped_updated = list(dict.fromkeys(updated))
    # Match the gate: current data from any hourly source satisfies freshness.
    latest_after_refresh = get_latest_hourly_price_history_timestamps(engine, symbols)
    current_symbols, stale_symbols = _partition_symbols_by_freshness(
        latest_after_refresh, symbols, expected_date
    )
    if log_callback:
        log_callback(
            f"Completed 1h yfinance refresh: "
            f"received_rows_for={len(deduped_updated)}, "
            f"current={len(current_symbols)}, still_stale={len(stale_symbols)}, "
            f"elapsed={_format_elapsed(time.time() - start_ts)}"
        )
        if stale_symbols:
            log_callback(f"Still-stale 1h symbols: {', '.join(stale_symbols)}")

    _record_history_refresh_outcomes(
        engine, "1h", current_symbols, stale_symbols
    )

    return deduped_updated
