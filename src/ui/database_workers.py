"""Background workers for database discovery, freshness, and mirror upkeep.

These workers are infrastructure adapters for the desktop application.  They
live outside ``main_window`` so the window only coordinates their lifecycle and
handles their results.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Optional

from PyQt5.QtCore import QThread, pyqtSignal

from src.infrastructure.database.coordination_engine import (
    coordination_database_configured,
    init_coordination_engine,
)
from src.infrastructure.database.mirror_engine import resolve_data_engine
from src.infrastructure.database.mirror_freshness import (
    local_mirror_hourly_is_stale,
    local_mirror_is_stale,
)
from src.infrastructure.database.settings import REFERENCE_SYMBOL
from src.services.runtime_status import record_runtime_heartbeat
from src.utils.data_loader import get_default_universe
from src.utils.market_calendar import expected_latest_market_data_date

logger = logging.getLogger(__name__)


class DatabaseInitWorker(QThread):
    """Use a fast PC connection check, falling back locally only if needed."""

    initialized = pyqtSignal(object, str, object, str)

    def run(self) -> None:
        try:
            # PC schema setup belongs to historical refresh/migration jobs.
            # Dashboard startup needs only a successful connection probe.
            resolution = resolve_data_engine(ensure_pc_schema=False)
            self.initialized.emit(
                resolution.engine, resolution.source, resolution.pc_engine, ""
            )
        except Exception as exc:
            # resolve_data_engine normally returns a "none" resolution on
            # optional-db failures, but the UI must also remain usable if an
            # unexpected driver error escapes it.
            self.initialized.emit(None, "none", None, str(exc))


class CoordinationDatabaseInitWorker(QThread):
    """Connect/provision the tiny Internet coordination store off the UI thread."""

    initialized = pyqtSignal(object, str)

    def run(self) -> None:
        if not coordination_database_configured():
            self.initialized.emit(None, "")
            return
        try:
            engine = init_coordination_engine(ensure_schema=True, raise_on_error=True)
            self.initialized.emit(engine, "")
        except Exception as exc:  # credentials/endpoints must never reach UI logs
            logger.debug(
                "Coordination database initialization failed: %s", type(exc).__name__
            )
            self.initialized.emit(
                None,
                "The configured shared coordination database could not be reached. "
                "Verify its SQL endpoint, TLS CA, username, password, and Internet connection.",
            )


class CoordinationRuntimeHeartbeatWorker(QThread):
    """Publish this local ``main.py`` process to shared coordination."""

    def __init__(self, engine, *, hostname: str, parent=None) -> None:
        super().__init__(parent)
        self.engine = engine
        self.hostname = str(hostname or "").strip()

    def run(self) -> None:
        try:
            record_runtime_heartbeat(self.engine, hostname=self.hostname)
        except Exception as exc:  # the independent DB probes own user notices
            logger.debug(
                "Coordination runtime heartbeat failed: %s", type(exc).__name__
            )


@dataclass(frozen=True)
class MarketDataStatusResult:
    """Slow market-cache freshness and watermarks resolved off the GUI thread."""

    engine: object
    latest_daily: object = None
    latest_hourly: object = None
    expected_date: object = None
    daily_is_stale: Optional[bool] = None
    hourly_is_stale: Optional[bool] = None
    error: str = ""


class MarketDataStatusWorker(QThread):
    """Read market-cache watermarks without freezing the dashboard."""

    completed = pyqtSignal(object)

    def __init__(
        self,
        engine,
        tickers: Optional[List[str]] = None,
        hourly_tickers: Optional[List[str]] = None,
        universe_limit: Optional[int] = None,
    ) -> None:
        super().__init__()
        self.engine = engine
        self.tickers = list(tickers or []) or None
        self.hourly_tickers = list(hourly_tickers or []) or None
        self.universe_limit = universe_limit

    def run(self) -> None:
        try:
            from src.infrastructure.database.repositories.market_bars import (
                get_latest_hourly_price_history_timestamp,
            )
            from src.infrastructure.database.repositories.market_watermarks import (
                get_latest_price_history_date,
            )

            tickers = self.tickers
            if tickers is None:
                tickers = get_default_universe(max_symbols=self.universe_limit)
            hourly_tickers = self.hourly_tickers or tickers
            expected_date = expected_latest_market_data_date()
            result = MarketDataStatusResult(
                engine=self.engine,
                latest_daily=get_latest_price_history_date(self.engine),
                # The local mirror intentionally contains a scoped hourly
                # subset. SPY is its always-present freshness canary and its
                # primary-key lookup avoids a multi-million-row global MAX.
                latest_hourly=get_latest_hourly_price_history_timestamp(
                    self.engine,
                    symbol=(
                        REFERENCE_SYMBOL if self.hourly_tickers is not None else None
                    ),
                ),
                expected_date=expected_date,
                daily_is_stale=local_mirror_is_stale(
                    self.engine, expected_date, tickers=tickers
                ),
                hourly_is_stale=local_mirror_hourly_is_stale(
                    self.engine, expected_date, tickers=hourly_tickers
                ),
            )
        except Exception as exc:
            result = MarketDataStatusResult(engine=self.engine, error=str(exc))
        self.completed.emit(result)


@dataclass(frozen=True)
class DatabaseRecoveryOutcome:
    engine: object
    success: bool
    error: str = ""


class DatabaseRecoveryWorker(QThread):
    """Verify PC MySQL connectivity without waiting for the local backup."""

    recovered = pyqtSignal(object, int)

    def __init__(
        self,
        generation: int,
        pc_engine=None,
    ) -> None:
        super().__init__()
        self.generation = int(generation)
        self.pc_engine = pc_engine

    def run(self) -> None:
        from sqlalchemy import text

        from src.infrastructure.database.engine import init_mysql_engine

        engine = self.pc_engine
        try:
            if engine is None:
                engine = init_mysql_engine(
                    log_unavailable=False,
                    ensure_schema=False,
                )
            if engine is None:
                outcome = DatabaseRecoveryOutcome(
                    None, False, error="PC MySQL is no longer reachable."
                )
            elif self.isInterruptionRequested():
                outcome = DatabaseRecoveryOutcome(
                    engine, False, error="Database connection check was interrupted."
                )
            else:
                with engine.connect() as conn:
                    conn.execute(text("SELECT 1"))
                outcome = DatabaseRecoveryOutcome(engine, True)
        except Exception as exc:
            logger.exception("Runtime MySQL recovery failed unexpectedly")
            outcome = DatabaseRecoveryOutcome(engine, False, error=str(exc))
        self.recovered.emit(outcome, self.generation)


class LocalMirrorSyncWorker(QThread):
    """Best-effort, silent PC -> laptop local-mirror top-up in the background."""

    completed = pyqtSignal(dict, str, bool, int)
    progress = pyqtSignal(str, int, int, int)

    def __init__(
        self,
        pc_engine,
        local_engine,
        hourly_symbols: Optional[List[str]] = None,
        *,
        generation: int = 0,
    ) -> None:
        super().__init__()
        self.pc_engine = pc_engine
        self.local_engine = local_engine
        self.hourly_symbols = None if hourly_symbols is None else list(hourly_symbols)
        self.generation = int(generation)

    def run(self) -> None:
        from src.infrastructure.database.mirror_copy import (
            sync_local_mirror_from_pc_checkpointed,
        )
        from src.infrastructure.database.mirror_engine import init_local_mirror_engine

        try:
            if self.local_engine is None:
                self.local_engine = init_local_mirror_engine()
            if self.local_engine is None:
                raise RuntimeError("The local data mirror is unavailable.")
            written = sync_local_mirror_from_pc_checkpointed(
                self.pc_engine,
                self.local_engine,
                hourly_symbols=self.hourly_symbols,
                progress_callback=lambda phase, current, total: self.progress.emit(
                    phase,
                    current,
                    total,
                    self.generation,
                ),
                cancellation_callback=self.isInterruptionRequested,
            )
            self.completed.emit(written, "", False, self.generation)
        except Exception as exc:
            self.completed.emit({}, str(exc), False, self.generation)


__all__ = [
    "CoordinationDatabaseInitWorker",
    "CoordinationRuntimeHeartbeatWorker",
    "DatabaseInitWorker",
    "DatabaseRecoveryOutcome",
    "DatabaseRecoveryWorker",
    "LocalMirrorSyncWorker",
    "MarketDataStatusResult",
    "MarketDataStatusWorker",
]
