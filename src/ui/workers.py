"""Background QThread workers used by the dashboard UI."""

from __future__ import annotations

import datetime as dt
import time
from typing import List, Optional

import pandas as pd
from PyQt5.QtCore import QThread, pyqtSignal

from src.api.kis_account_snapshot_dual import fetch_account_snapshot
from src.infrastructure.database.repositories.market_bars import (
    prune_intraday_history, save_intraday_history_to_db)
from src.services.intraday_data_service import (fetch_intraday_with_fallback,
                                                load_best_intraday_history)
from src.services.intraday_provider import IntradayInterval, IntradayRequest
from src.ui.order_workers import (HandoffReconciliationWorker,
                                  KisOrderCancelWorker, KisOrderQueryWorker,
                                  KisOrderWorker, OrderReconciliationWorker)
from src.utils.data_loader import _extract_symbol_history, download_price_history
from src.utils.intraday_helpers import intraday_cache_needs_backfill
from src.utils.intraday_helpers import utcnow_naive as _utcnow_naive


class KisAccountWorker(QThread):
    finished_snapshot = pyqtSignal(dict)
    error_occurred = pyqtSignal(str)

    def __init__(
        self,
        environment: str,
        include_domestic: bool,
        include_overseas: bool,
        force_token: bool = False,
        account_no: Optional[str] = None,
    ):
        super().__init__()
        self.environment = environment
        self.include_domestic = include_domestic
        self.include_overseas = include_overseas
        self.force_token = force_token
        self.account_no = account_no

    def run(self) -> None:
        try:
            snapshot = fetch_account_snapshot(
                self.environment,
                include_domestic=self.include_domestic,
                include_overseas=self.include_overseas,
                force_token=self.force_token,
                account_no=self.account_no,
            )
            self.finished_snapshot.emit(snapshot)
        except Exception as exc:
            self.error_occurred.emit(str(exc))


class KisStartupAccountsWorker(QThread):
    finished_profiles = pyqtSignal(dict, list)
    log_message = pyqtSignal(str)

    def __init__(self, profiles: List[dict]):
        super().__init__()
        self.profiles = list(profiles)

    def run(self) -> None:
        snapshots = {}
        errors = []
        for index, profile in enumerate(self.profiles):
            if self.isInterruptionRequested():
                break
            environment = profile.get("environment", "")
            account_no = profile.get("account_no", "")
            label = profile.get("label", f"{environment} {account_no}")
            try:
                self.log_message.emit(f"Startup KIS fetch: {label}")
                snapshot = fetch_account_snapshot(
                    environment,
                    include_domestic=True,
                    include_overseas=True,
                    account_no=account_no,
                )
                snapshots[(environment, account_no)] = snapshot
            except Exception as exc:
                errors.append(f"{label}: {exc}")
            if index < len(self.profiles) - 1:
                time.sleep(2.0)
        self.finished_profiles.emit(snapshots, errors)


class FxRateWorker(QThread):
    finished_rate = pyqtSignal(float, str, str)
    error_occurred = pyqtSignal(str)

    def __init__(self, snapshot: Optional[dict] = None):
        super().__init__()
        self.snapshot = snapshot or {}

    def run(self) -> None:
        try:
            kis_rate = self._extract_usd_krw_from_snapshot(self.snapshot)
            if kis_rate and kis_rate > 0:
                self.finished_rate.emit(
                    kis_rate,
                    "KIS account snapshot",
                    dt.datetime.now().isoformat(timespec="seconds"),
                )
                return

            fx_history = self._download_yfinance_usd_krw()
            if fx_history is None or fx_history.empty:
                raise RuntimeError("No USD/KRW rows returned from yfinance.")
            rate = float(fx_history["Close"].dropna().iloc[-1])
            if rate <= 0:
                raise RuntimeError("Invalid USD/KRW rate returned from yfinance.")
            timestamp = pd.Timestamp(fx_history.index[-1]).strftime("%Y-%m-%d %H:%M")
            self.finished_rate.emit(rate, "yfinance KRW=X", timestamp)
        except Exception as exc:
            self.error_occurred.emit(str(exc))

    @staticmethod
    def _download_yfinance_usd_krw() -> Optional[pd.DataFrame]:
        for period, interval in (("1d", "1m"), ("5d", "15m"), ("5d", "1d")):
            history = download_price_history(
                ["KRW=X"], period=period, interval=interval, max_symbols=1
            )
            fx_history = _extract_symbol_history(history, "KRW=X")
            if fx_history is not None and not fx_history.empty:
                return fx_history
        return None

    @classmethod
    def _extract_usd_krw_from_snapshot(cls, snapshot: dict) -> Optional[float]:
        for key, value in cls._walk_snapshot_values(snapshot):
            key_text = str(key).lower()
            if not any(
                token in key_text
                for token in ("exrt", "exchange", "rate", "fx", "환율")
            ):
                continue
            try:
                rate = float(str(value).replace(",", "").strip())
            except (TypeError, ValueError):
                continue
            if 900 <= rate <= 2000:
                return rate
        return None

    @classmethod
    def _walk_snapshot_values(cls, value, parent_key: str = ""):
        if isinstance(value, dict):
            for key, item in value.items():
                yield from cls._walk_snapshot_values(item, str(key))
        elif isinstance(value, list):
            for item in value:
                yield from cls._walk_snapshot_values(item, parent_key)
        else:
            yield parent_key, value


class IntradayFetchWorker(QThread):
    finished_fetch = pyqtSignal(str, object, int, str)
    error_occurred = pyqtSignal(str, str)
    provider_warning = pyqtSignal(str, str)

    def __init__(
        self,
        symbol: str,
        engine,
        window_days: int = 7,
        fetch_days: Optional[int] = None,
        environment: str = "PROD",
        account_no: str = "",
        exchange: str = "NASD",
        allow_fallback: bool = True,
    ):
        super().__init__()
        self.symbol = symbol.strip().upper()
        self.engine = engine
        self.window_days = max(1, min(7, int(window_days or 7)))
        self.fetch_days = fetch_days
        self.environment = environment
        self.account_no = account_no
        self.exchange = exchange
        self.allow_fallback = allow_fallback

    def run(self) -> None:
        try:
            days_to_fetch = self.fetch_days
            if days_to_fetch is None:
                days_to_fetch = self.window_days
                if self.engine is not None:
                    try:
                        since = _utcnow_naive() - dt.timedelta(days=self.window_days)
                        cached, _source = load_best_intraday_history(
                            self.symbol, self.engine, interval="5m", since=since
                        )
                        needs_backfill = True
                        if not cached.empty:
                            oldest = pd.Timestamp(cached.index.min()).tz_localize(None)
                            if oldest <= pd.Timestamp(since) + pd.Timedelta(hours=12):
                                needs_backfill = False
                        if not needs_backfill:
                            days_to_fetch = 2
                    except Exception:
                        pass  # keep self.window_days

            request = self._request(IntradayInterval.FIVE_MINUTE, days_to_fetch)
            result = fetch_intraday_with_fallback(request)
            for warning in result.warnings:
                self.provider_warning.emit(self.symbol, warning)
            fetched = result.bars
            if fetched.empty:
                raise RuntimeError(
                    "; ".join(result.warnings) or "No 5-minute intraday rows returned."
                )

            if self.engine is not None:
                save_intraday_history_to_db(
                    self.symbol,
                    fetched,
                    self.engine,
                    interval="5m",
                    source=result.source,
                )
                opening_result = fetch_intraday_with_fallback(
                    self._request(IntradayInterval.ONE_MINUTE, 1)
                )
                seen_warnings = set(result.warnings)
                for warning in opening_result.warnings:
                    if warning not in seen_warnings:
                        self.provider_warning.emit(self.symbol, warning)
                if not opening_result.bars.empty:
                    save_intraday_history_to_db(
                        self.symbol,
                        opening_result.bars,
                        self.engine,
                        interval="1m",
                        source=opening_result.source,
                    )
                prune_intraday_history(self.engine, keep_days=7)
            self.finished_fetch.emit(
                self.symbol, fetched, self.window_days, result.source
            )
        except Exception as exc:
            self.error_occurred.emit(self.symbol, str(exc))

    def _request(self, interval: IntradayInterval, days: int) -> IntradayRequest:
        return IntradayRequest(
            symbol=self.symbol,
            interval=interval,
            window_days=days,
            environment=self.environment,
            account_no=self.account_no,
            exchange=self.exchange,
            allow_fallback=self.allow_fallback,
        )

    @staticmethod
    def _download_with_retries(
        symbol: str, days: int, attempts: int = 3
    ) -> pd.DataFrame:
        from src.services.yfinance_intraday_provider import \
            _download_5m_with_retries

        return _download_5m_with_retries(symbol, days, attempts=attempts)

    @staticmethod
    def _download_opening_1m_bar(symbol: str) -> pd.DataFrame:
        from src.services.yfinance_intraday_provider import \
            _download_opening_1m_bar

        return _download_opening_1m_bar(symbol)


class IntradayBulkFetchWorker(QThread):
    progress = pyqtSignal(str, int, int)
    finished_bulk = pyqtSignal(list, list)
    error_occurred = pyqtSignal(str)
    provider_warning = pyqtSignal(str, str)

    def __init__(
        self,
        symbols: List[str],
        engine,
        window_days: int = 7,
        environment: str = "PROD",
        account_no: str = "",
        exchange: str = "NASD",
        allow_fallback: bool = True,
    ):
        super().__init__()
        self.symbols = [symbol.strip().upper() for symbol in symbols if symbol.strip()]
        self.engine = engine
        self.window_days = max(1, min(7, int(window_days or 7)))
        self.environment = environment
        self.account_no = account_no
        self.exchange = exchange
        self.allow_fallback = allow_fallback

    def run(self) -> None:
        updated = []
        failed = []
        total = len(self.symbols)
        for index, symbol in enumerate(self.symbols, start=1):
            if self.isInterruptionRequested():
                break
            self.progress.emit(symbol, index, total)
            try:
                fetch_days = self._fetch_days_for_symbol(symbol)
                result = fetch_intraday_with_fallback(
                    self._request(symbol, IntradayInterval.FIVE_MINUTE, fetch_days)
                )
                for warning in result.warnings:
                    self.provider_warning.emit(symbol, warning)
                fetched = result.bars
                if fetched.empty:
                    raise RuntimeError(
                        "; ".join(result.warnings)
                        or "No 5-minute intraday rows returned."
                    )
                if self.engine is not None:
                    save_intraday_history_to_db(
                        symbol,
                        fetched,
                        self.engine,
                        interval="5m",
                        source=result.source,
                    )
                    opening_result = fetch_intraday_with_fallback(
                        self._request(symbol, IntradayInterval.ONE_MINUTE, 1)
                    )
                    seen_warnings = set(result.warnings)
                    for warning in opening_result.warnings:
                        if warning not in seen_warnings:
                            self.provider_warning.emit(symbol, warning)
                    if not opening_result.bars.empty:
                        save_intraday_history_to_db(
                            symbol,
                            opening_result.bars,
                            self.engine,
                            interval="1m",
                            source=opening_result.source,
                        )
                updated.append(symbol)
            except Exception as exc:
                failed.append(f"{symbol}: {exc}")
        if self.engine is not None:
            try:
                prune_intraday_history(self.engine, keep_days=7)
            except Exception as exc:
                failed.append(f"prune: {exc}")
        self.finished_bulk.emit(updated, failed)

    def _request(
        self, symbol: str, interval: IntradayInterval, days: int
    ) -> IntradayRequest:
        return IntradayRequest(
            symbol=symbol,
            interval=interval,
            window_days=days,
            environment=self.environment,
            account_no=self.account_no,
            exchange=self.exchange,
            allow_fallback=self.allow_fallback,
        )

    def _fetch_days_for_symbol(self, symbol: str) -> int:
        if self.engine is None:
            return self.window_days
        since = _utcnow_naive() - dt.timedelta(days=self.window_days)
        try:
            cached, _source = load_best_intraday_history(
                symbol, self.engine, interval="5m", since=since
            )
        except Exception:
            return self.window_days
        if intraday_cache_needs_backfill(cached, since):
            return self.window_days
        return 1


class ScannerWorker(QThread):
    finished_scan = pyqtSignal(list, object)
    universe_loaded = pyqtSignal(list)
    log_message = pyqtSignal(str)
    error_occurred = pyqtSignal(str)

    def __init__(
        self,
        tickers,
        engine,
        min_volume: float,
        min_dollar_volume: float,
        min_adr: float,
        min_growth_rank: float,
        min_trend_intensity: float,
        universe_limit: Optional[int] = None,
        scanner_rules_by_setup: Optional[dict] = None,
    ):
        super().__init__()
        self.tickers = list(tickers or [])
        self.engine = engine
        self.min_volume = min_volume
        self.min_dollar_volume = min_dollar_volume
        self.min_adr = min_adr
        self.min_growth_rank = min_growth_rank
        self.min_trend_intensity = min_trend_intensity
        self.universe_limit = universe_limit
        self.scanner_rules_by_setup = scanner_rules_by_setup

    def run(self) -> None:
        try:
            from src.infrastructure.database.repositories.scanner import (
                get_universe_stock_metrics_from_db,
                query_scanner_metrics_with_funnel,
            )

            if self.engine is None:
                raise RuntimeError(
                    "MySQL cache is unavailable. Configure MySQL, then refresh the cache before scanning."
                )

            if not self.tickers:
                from src.utils.data_loader import get_default_universe

                self.log_message.emit("Loading scanner universe in the background...")
                self.tickers = get_default_universe(max_symbols=self.universe_limit)
                if not self.tickers:
                    raise RuntimeError("Scanner universe is empty.")
                self.universe_loaded.emit(list(self.tickers))
            if self.isInterruptionRequested():
                return

            if self.scanner_rules_by_setup is not None:
                self.log_message.emit(
                    "Querying scanner snapshot with database filters..."
                )
                results_by_setup = {}
                funnels_by_setup = {}
                for setup_name, rules in self.scanner_rules_by_setup.items():
                    if self.isInterruptionRequested():
                        return
                    results, funnel = query_scanner_metrics_with_funnel(
                        self.tickers,
                        self.engine,
                        list(rules or []),
                    )
                    results_by_setup[setup_name] = results
                    funnels_by_setup[setup_name] = funnel
                self.finished_scan.emit(
                    [],
                    {
                        "database_filtered": True,
                        "results_by_setup": results_by_setup,
                        "funnels_by_setup": funnels_by_setup,
                        "rules_by_setup": self.scanner_rules_by_setup,
                    },
                )
                return

            self.log_message.emit("Running scanner using MySQL cache...")
            stock_metrics = get_universe_stock_metrics_from_db(
                self.tickers,
                engine=self.engine,
            )

            self.finished_scan.emit(stock_metrics, None)
        except Exception as exc:
            self.error_occurred.emit(str(exc))


class PcRemoteStatusWorker(QThread):
    """Check database, remote-control, and remote main-app health separately."""

    finished_status = pyqtSignal(object)  # PcServiceStatus

    def __init__(self, engine=None, parent=None) -> None:
        super().__init__(parent)
        self.engine = engine

    def run(self) -> None:
        from concurrent.futures import ThreadPoolExecutor

        from sqlalchemy import text

        from src.infrastructure.database.engine import init_mysql_engine
        from src.services.pc_remote_control import (PcServiceStatus, PcStatus,
                                                    check_pc_status)
        from src.services.runtime_status import (database_server_hostname,
                                                 get_runtime_process_status,
                                                 record_runtime_heartbeat)

        def probe_listener():
            try:
                return check_pc_status()
            except Exception:
                return PcStatus.UNKNOWN

        def probe_database():
            engine = self.engine
            owns_engine = False
            db_ready = False
            try:
                if engine is None:
                    # This worker keeps probing while the PC is offline. Leave
                    # the user-facing failure log to the startup attempt.
                    engine = init_mysql_engine(
                        log_unavailable=False,
                        ensure_schema=False,
                    )
                    owns_engine = engine is not None
                if engine is not None:
                    with engine.connect() as conn:
                        conn.execute(text("SELECT 1"))
                    db_ready = True
            except Exception:
                db_ready = False
            return db_ready, engine, owns_engine

        # Both network paths use a three-second timeout. Running them in
        # parallel gives the UI a definitive answer in roughly three seconds
        # instead of making an offline user wait for two sequential timeouts.
        with ThreadPoolExecutor(max_workers=2) as executor:
            listener_future = executor.submit(probe_listener)
            database_future = executor.submit(probe_database)
            listener_status = listener_future.result()
            db_ready, engine, owns_engine = database_future.result()

        db_hostname = ""
        main_app_active = None
        main_app_last_seen_seconds = None

        if db_ready and engine is not None:
            try:
                record_runtime_heartbeat(engine)
                db_hostname = database_server_hostname(engine)
                runtime_status = get_runtime_process_status(engine, db_hostname)
                if runtime_status.observed:
                    main_app_active = runtime_status.active
                    main_app_last_seen_seconds = runtime_status.age_seconds
            except Exception:
                # Runtime monitoring must never turn a successful database
                # connectivity result into a false "DB: Off" report.
                db_hostname = ""
                main_app_active = None
                main_app_last_seen_seconds = None

        if owns_engine and engine is not None:
            try:
                engine.dispose()
            except Exception:
                pass

        self.finished_status.emit(
            PcServiceStatus(
                listener_status=listener_status,
                database_ready=db_ready,
                database_hostname=db_hostname,
                main_app_active=main_app_active,
                main_app_last_seen_seconds=main_app_last_seen_seconds,
            )
        )
