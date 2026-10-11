"""Extend the existing daily refresh with exact missing study ranges."""
from __future__ import annotations

import datetime as dt
import time

import pandas as pd
from sqlalchemy import select

from src.core.breakout_replay import normalize_daily
from src.infrastructure.database.repositories.breakout_replay import ReplayRepository, now
from src.infrastructure.database.repositories.market_bars import save_universe_history_batch_to_db
from src.utils.data_loader import _extract_symbol_history, _download_yfinance_batch, normalize_yahoo_symbol
from src.utils.market_calendar import expected_latest_market_data_date


def missing_study_ranges(symbols, bounds, requests, start, end, asof):
    jobs = []
    for symbol in symbols:
        record = bounds.get(symbol)
        first = record["first"].date() if record else None
        last = record["last"].date() if record else None
        ranges = [(start, end)] if not record else []
        if first and first > start + dt.timedelta(days=4):
            ranges.append((start, min(end, first - dt.timedelta(days=1))))
        if last and last < end:
            ranges.append((max(start, last + dt.timedelta(days=1)), end))
        for begin, finish in ranges:
            if begin > finish:
                continue
            prior = [row for row in requests if row["symbol"] == symbol and row["start_day"] <= begin and row["end_day"] >= finish]
            if any(row["status"] == "complete" or
                   (row["status"] == "empty" and row["updated_at"] > asof - dt.timedelta(days=30)) or
                   (row["status"] == "failed" and row["updated_at"] > asof - dt.timedelta(hours=6)) for row in prior):
                continue
            jobs.append((symbol, begin, finish))
    return jobs


def refresh_study_history(tickers, engine, start, log_callback=None, progress_callback=None,
                          chunk_size=40, threads=8, batch_sleep=1.5):
    repo = ReplayRepository(engine)
    symbols = ["SPY", *sorted((set(tickers) | set(repo.symbols())) - {"SPY"})]
    end = expected_latest_market_data_date()
    jobs = missing_study_ranges(symbols, repo.symbol_bounds(), repo.rows(select(repo.daily_requests)), start, end, now())
    grouped = {}
    for symbol, begin, finish in jobs:
        grouped.setdefault((begin, finish), []).append(symbol)
    batches = [(symbols[offset:offset + chunk_size], begin, finish) for (begin, finish), symbols in grouped.items()
               for offset in range(0, len(symbols), chunk_size)]
    repo.prepared.save_job("history", {"status": "running", "processed": 0, "total": len(jobs),
        "history_start": str(start), "updated_at": now().isoformat()})
    updated = set()
    processed = 0
    for batch, begin, finish in batches:
        if log_callback:
            log_callback(f"Daily study extension: {processed}/{len(jobs)} missing ranges; {begin} through {finish}, {len(batch)} symbols")
        error = False
        try:
            data = _download_yfinance_batch([normalize_yahoo_symbol(s) for s in batch], period=None, start=str(begin), end=str(finish + dt.timedelta(days=1)),
                interval="1d", threads=threads, timeout=20)
            histories = {}
            for symbol in batch:
                history = _extract_symbol_history(data, symbol) if not data.empty else None
                if history is not None:
                    history = normalize_daily(history).loc[pd.Timestamp(begin):pd.Timestamp(finish)]
                    if not history.empty:
                        histories[symbol] = history
            if histories:
                saved = save_universe_history_batch_to_db(pd.concat(histories, axis=1), list(histories), engine, interval="1d")
                if not saved:
                    raise RuntimeError("Could not save study daily extension")
                updated.update(histories)
        except Exception:
            histories, error = {}, True
        repo.upsert(repo.daily_requests, [{"symbol": symbol, "start_day": begin, "end_day": finish,
            "status": "failed" if error else "complete" if symbol in histories else "empty",
            "row_count": len(histories.get(symbol, [])), "updated_at": now()} for symbol in batch], ("symbol", "start_day", "end_day"))
        processed += len(batch)
        repo.prepared.save_job("history", {"status": "running", "processed": processed, "total": len(jobs),
            "history_start": str(start), "updated_at": now().isoformat()})
        if progress_callback:
            progress_callback(batch[-1], processed, len(jobs), int(100 * processed / len(jobs)), "background")
        if batch_sleep and processed < len(jobs):
            time.sleep(batch_sleep)
    if log_callback:
        log_callback(f"Daily study extension complete: {len(updated)} symbols added; stored dates were not downloaded again.")
    repo.prepared.save_job("history", {"status": "complete", "processed": processed, "total": len(jobs),
        "history_start": str(start), "updated_at": now().isoformat()})
    return sorted(updated)
