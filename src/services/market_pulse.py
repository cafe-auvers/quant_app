"""Market Pulse provider boundary, refresh orchestration, and local cache."""

from __future__ import annotations

import datetime as dt
import logging
import threading
import time
from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Mapping, Optional, Protocol, Sequence

import pandas as pd
from sqlalchemy.engine import Engine

from src.core.market_pulse import (
    MarketPulseInstrument,
    MarketPulseRow,
    MarketPulseSnapshot,
    calculate_market_pulse_metrics,
    latest_valid_session_date,
    load_market_pulse_instruments,
    rank_market_pulse_rows,
    snapshot_from_dict,
    snapshot_to_dict,
)
from src.infrastructure.database.repositories.market_bars import (
    load_universe_history_from_db,
    save_universe_history_batch_to_db,
)
from src.infrastructure.database.repositories.market_pulse import (
    MarketPulseSnapshotRepository,
)
from src.utils.config import DATA_DIR, ROOT_DIR
from src.utils.data_loader import _extract_symbol_history, download_price_history
from src.utils.market_calendar import expected_latest_market_data_date
from src.utils.storage import load_json, save_json


logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = ROOT_DIR / "config" / "market_pulse_instruments.json"
DEFAULT_CACHE_PATH = DATA_DIR / "market_pulse_snapshot.json"


class MarketPulseRefreshError(RuntimeError):
    pass


class MarketPulseRefreshInProgress(MarketPulseRefreshError):
    pass


@dataclass(frozen=True)
class MarketPulseHistoryBatch:
    histories: Mapping[str, pd.DataFrame]
    failures: Mapping[str, str]
    source: str
    raw_history: pd.DataFrame


class MarketPulseHistoryProvider(Protocol):
    def fetch(
        self,
        tickers: Sequence[str],
        *,
        latest_completed_session: dt.date,
    ) -> MarketPulseHistoryBatch:
        ...


class YFinanceMarketPulseProvider:
    """Narrow, replaceable EOD provider using the existing batch loader."""

    source = "yfinance_adjusted"

    def __init__(
        self,
        *,
        period: str = "2y",
        timeout_seconds: float = 15.0,
        max_retries: int = 2,
        chunk_size: int = 50,
    ) -> None:
        self.period = period
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.chunk_size = chunk_size

    def fetch(
        self,
        tickers: Sequence[str],
        *,
        latest_completed_session: dt.date,
    ) -> MarketPulseHistoryBatch:
        symbols = list(dict.fromkeys(str(value).strip().upper() for value in tickers))
        try:
            raw = download_price_history(
                symbols,
                period=self.period,
                interval="1d",
                max_symbols=None,
                chunk_size=self.chunk_size,
                threads=8,
                batch_sleep=0.5,
                max_retries=self.max_retries,
                fallback_to_single=False,
                chart_fallback=False,
                required_latest_date=latest_completed_session,
                timeout_seconds=self.timeout_seconds,
            )
        except Exception as exc:
            logger.exception("Market Pulse yfinance batch failed")
            reason = f"Provider request failed: {exc}"
            return MarketPulseHistoryBatch({}, {symbol: reason for symbol in symbols}, self.source, pd.DataFrame())

        if not raw.empty:
            try:
                dates = pd.DatetimeIndex(pd.to_datetime(raw.index))
                raw = raw.loc[
                    [value.date() <= latest_completed_session for value in dates]
                ]
            except (TypeError, ValueError):
                logger.warning(
                    "Market Pulse provider returned an invalid daily date index"
                )
                raw = pd.DataFrame()

        histories = {}
        failures = {}
        for symbol in symbols:
            frame = _extract_symbol_history(raw, symbol)
            if frame is None or frame.empty:
                failures[symbol] = "No usable daily history returned"
            else:
                histories[symbol] = frame
        return MarketPulseHistoryBatch(histories, failures, self.source, raw)


class MarketPulseService:
    """Coordinate one atomic, failure-safe refresh of all configured sections."""

    def __init__(
        self,
        *,
        provider: Optional[MarketPulseHistoryProvider] = None,
        engine: Optional[Engine] = None,
        config_path: Path = DEFAULT_CONFIG_PATH,
        cache_path: Path = DEFAULT_CACHE_PATH,
    ) -> None:
        self.provider = provider or YFinanceMarketPulseProvider()
        self.engine = engine
        self.config_path = Path(config_path)
        self.cache_path = Path(cache_path)
        self._refresh_lock = threading.Lock()

    @property
    def instruments(self) -> tuple[MarketPulseInstrument, ...]:
        return load_market_pulse_instruments(self.config_path)

    @property
    def is_refreshing(self) -> bool:
        return self._refresh_lock.locked()

    def set_engine(self, engine: Optional[Engine]) -> None:
        self.engine = engine

    def load_cached_snapshot(self) -> Optional[MarketPulseSnapshot]:
        """Load the non-blocking local display cache, then optional SQL cache."""

        local = self._project_snapshot_to_active_config(
            snapshot_from_dict(load_json(self.cache_path, {}))
        )
        if self.engine is None:
            return local
        try:
            database = self._project_snapshot_to_active_config(
                MarketPulseSnapshotRepository(self.engine).load_latest_snapshot()
            )
        except Exception:
            logger.warning("Market Pulse SQL cache load failed", exc_info=True)
            return local
        if database is None:
            return local
        if local is None or database.refreshed_at > local.refreshed_at:
            return database
        return local

    def _project_snapshot_to_active_config(
        self,
        snapshot: Optional[MarketPulseSnapshot],
    ) -> Optional[MarketPulseSnapshot]:
        if snapshot is None:
            return None
        active = {
            (item.section, item.ticker): item
            for item in self.instruments
            if item.is_active
        }
        rows = []
        for row in snapshot.rows:
            configured = active.get((row.section, row.ticker))
            if configured is None:
                continue
            rows.append(
                replace(
                    row,
                    display_name=configured.display_name,
                    display_order=configured.display_order,
                )
            )
        if not rows:
            return None
        active_tickers = {item.ticker for item in active.values()}
        return replace(
            snapshot,
            rows=rank_market_pulse_rows(rows),
            failures={
                ticker: reason
                for ticker, reason in snapshot.failures.items()
                if ticker in active_tickers
            },
        )

    @staticmethod
    def _merge_histories(
        cached: Mapping[str, pd.DataFrame],
        downloaded: Mapping[str, pd.DataFrame],
    ) -> Mapping[str, pd.DataFrame]:
        merged = dict(cached)
        for ticker, fresh in downloaded.items():
            prior = cached.get(ticker)
            merged[ticker] = (
                pd.concat([prior, fresh])
                if prior is not None and not prior.empty
                else fresh.copy()
            )
        return merged

    @staticmethod
    def _resolve_common_as_of_date(
        histories: Mapping[str, pd.DataFrame],
        tickers: Sequence[str],
        expected_date: dt.date,
    ) -> dt.date:
        latest_dates = [
            latest_valid_session_date(histories.get(ticker, pd.DataFrame()), expected_date)
            for ticker in tickers
        ]
        available = [value for value in latest_dates if value is not None]
        if not available:
            raise MarketPulseRefreshError("No completed daily sessions were available")
        counts = Counter(available)
        # Modal latest date tolerates a small number of unavailable/stale ETFs
        # without silently dragging the entire dashboard backward.
        return max(counts, key=lambda value: (counts[value], value))

    def refresh(
        self,
        *,
        now: Optional[dt.datetime] = None,
    ) -> MarketPulseSnapshot:
        if not self._refresh_lock.acquire(blocking=False):
            raise MarketPulseRefreshInProgress("A Market Pulse refresh is already running")
        started = time.monotonic()
        expected_date = expected_latest_market_data_date(now)
        try:
            instruments = tuple(item for item in self.instruments if item.is_active)
            tickers = list(dict.fromkeys(item.ticker for item in instruments))
            logger.info(
                "Market Pulse refresh started",
                extra={"ticker_count": len(tickers), "expected_date": expected_date.isoformat()},
            )

            cached_histories = {}
            if self.engine is not None:
                try:
                    cached_histories = load_universe_history_from_db(
                        tickers, self.engine, interval="1d"
                    )
                except Exception:
                    logger.warning("Market Pulse raw-history cache load failed", exc_info=True)

            batch = self.provider.fetch(
                tickers,
                latest_completed_session=expected_date,
            )
            if batch.failures:
                logger.warning(
                    "Market Pulse provider returned partial failures",
                    extra={"failures": dict(batch.failures)},
                )
            if not batch.histories:
                reasons = sorted(set(batch.failures.values()))
                detail = reasons[0] if reasons else "Provider returned an empty response"
                raise MarketPulseRefreshError(detail)

            if self.engine is not None and not batch.raw_history.empty:
                try:
                    rows_written = save_universe_history_batch_to_db(
                        batch.raw_history,
                        list(batch.histories),
                        self.engine,
                        interval="1d",
                    )
                    if rows_written == 0:
                        logger.warning(
                            "Market Pulse raw-history upsert wrote no rows"
                        )
                except Exception:
                    logger.warning("Market Pulse raw-history upsert failed", exc_info=True)

            histories = self._merge_histories(cached_histories, batch.histories)
            as_of_date = self._resolve_common_as_of_date(histories, tickers, expected_date)
            failures = dict(batch.failures)
            rows = []
            for item in instruments:
                history = histories.get(item.ticker, pd.DataFrame())
                session_date = latest_valid_session_date(history, expected_date)
                metrics = calculate_market_pulse_metrics(history, as_of_date)
                error = str(failures.get(item.ticker) or "")
                status = "available"
                if session_date is None:
                    status = "unavailable"
                    error = error or "No usable daily history"
                elif session_date < as_of_date:
                    status = "stale"
                    error = error or f"Latest data is {session_date.isoformat()}"
                    failures.setdefault(item.ticker, error)
                elif metrics.close is None:
                    status = "unavailable"
                    error = error or f"No close for {as_of_date.isoformat()}"
                    failures.setdefault(item.ticker, error)
                elif error:
                    # A current cached row remains usable, while its failed
                    # provider refresh stays visible to the operator.
                    status = "cached"

                rows.append(
                    MarketPulseRow(
                        section=item.section,
                        display_name=item.display_name,
                        ticker=item.ticker,
                        display_order=item.display_order,
                        rank=0,
                        close=metrics.close,
                        daily_return=metrics.daily_return,
                        weekly_return=metrics.weekly_return,
                        monthly_return=metrics.monthly_return,
                        pct_above_52w_low=metrics.pct_above_52w_low,
                        pct_below_52w_high=metrics.pct_below_52w_high,
                        status=status,
                        error=error,
                        source_session_date=session_date,
                    )
                )

            refreshed_at = dt.datetime.now(dt.timezone.utc)
            snapshot = MarketPulseSnapshot(
                as_of_date=as_of_date,
                refreshed_at=refreshed_at,
                source=batch.source,
                rows=rank_market_pulse_rows(rows),
                failures=failures,
                stale=as_of_date < expected_date,
            )

            if self.engine is not None:
                try:
                    MarketPulseSnapshotRepository(self.engine).upsert_snapshot(
                        snapshot, self.instruments
                    )
                except Exception:
                    logger.warning("Market Pulse SQL snapshot upsert failed", exc_info=True)
            # This tiny, atomic JSON projection makes next tab opening instant,
            # including when optional MySQL is offline.
            save_json(self.cache_path, snapshot_to_dict(snapshot))

            duration = time.monotonic() - started
            valid_rows = sum(row.status in {"available", "cached"} for row in snapshot.rows)
            logger.info(
                "Market Pulse refresh completed",
                extra={
                    "duration_seconds": round(duration, 3),
                    "row_count": len(snapshot.rows),
                    "valid_row_count": valid_rows,
                    "failure_count": len(failures),
                    "as_of_date": as_of_date.isoformat(),
                },
            )
            return snapshot
        except Exception:
            logger.exception(
                "Market Pulse refresh failed",
                extra={"duration_seconds": round(time.monotonic() - started, 3)},
            )
            raise
        finally:
            self._refresh_lock.release()
