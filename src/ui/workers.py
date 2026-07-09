"""Background QThread workers used by the dashboard UI."""
from __future__ import annotations

import datetime as dt
import json
import math
import time
from typing import List, Optional

import pandas as pd
from PyQt5.QtCore import QThread, pyqtSignal

from src.api.kis_account_snapshot_dual import fetch_account_snapshot
from src.core.order_state import BrokerOrder, OrderIntent, OrderSide
from src.core.orb import calculate_orb_range
from src.core.scoring import calculate_deterministic_scores, run_ai_review
from src.services.intraday_data_service import fetch_intraday_with_fallback, load_best_intraday_history
from src.services.intraday_provider import IntradayInterval, IntradayRequest
from src.services.order_reconciliation import reconcile_orders_with_snapshot
from src.utils.data_loader import download_price_history, _extract_symbol_history
from src.utils.db_loader import (
    load_symbol_history_from_db,
    prune_intraday_history,
    save_intraday_history_to_db,
)
from src.utils.intraday_helpers import (
    intraday_cache_needs_backfill,
    utcnow_naive as _utcnow_naive,
)


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
                self.finished_rate.emit(kis_rate, "KIS account snapshot", dt.datetime.now().isoformat(timespec="seconds"))
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
            history = download_price_history(["KRW=X"], period=period, interval=interval, max_symbols=1)
            fx_history = _extract_symbol_history(history, "KRW=X")
            if fx_history is not None and not fx_history.empty:
                return fx_history
        return None

    @classmethod
    def _extract_usd_krw_from_snapshot(cls, snapshot: dict) -> Optional[float]:
        for key, value in cls._walk_snapshot_values(snapshot):
            key_text = str(key).lower()
            if not any(token in key_text for token in ("exrt", "exchange", "rate", "fx", "환율")):
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


class KisOrderWorker(QThread):
    """Places a KIS overseas equity order in a background thread."""
    finished_order = pyqtSignal(object)
    error_occurred = pyqtSignal(str)

    def __init__(
        self,
        environment: str,
        symbol: str,
        quantity: int,
        price: float,
        side: str,
        exchange: str = "NASD",
        order_type: str = "limit",
        account_no: Optional[str] = None,
        intent: OrderIntent | str = OrderIntent.UNKNOWN,
        buylist_symbol_key: str = "",
    ) -> None:
        super().__init__()
        self.environment = environment
        self.symbol = symbol
        self.quantity = quantity
        self.price = price
        self.side = side
        self.exchange = exchange
        self.order_type = order_type
        self.account_no = account_no
        self.intent = intent
        self.buylist_symbol_key = buylist_symbol_key

    def run(self) -> None:
        try:
            from src.services.order_execution_service import submit_guarded_overseas_order

            order = submit_guarded_overseas_order(
                environment=self.environment,
                account_no=self.account_no or "",
                symbol=self.symbol,
                side=OrderSide(str(self.side).upper()),
                intent=self.intent if isinstance(self.intent, OrderIntent) else OrderIntent(str(self.intent).upper()),
                quantity=self.quantity,
                limit_price=self.price,
                exchange=self.exchange,
            )
            self.finished_order.emit(order)
        except Exception as exc:
            self.error_occurred.emit(str(exc))


class OrderReconciliationWorker(QThread):
    finished_reconciliation = pyqtSignal(list, dict)
    error_occurred = pyqtSignal(str)

    def __init__(
        self,
        environment: str,
        account_no: str,
        open_orders: List[BrokerOrder],
        previous_snapshot: Optional[dict] = None,
    ) -> None:
        super().__init__()
        self.environment = environment
        self.account_no = account_no
        self.open_orders = list(open_orders)
        self.previous_snapshot = previous_snapshot

    def run(self) -> None:
        try:
            snapshot = fetch_account_snapshot(
                self.environment,
                include_domestic=True,
                include_overseas=True,
                account_no=self.account_no,
            )
            updated_orders = reconcile_orders_with_snapshot(
                self.open_orders,
                snapshot,
                previous_snapshot=self.previous_snapshot,
            )
            self.finished_reconciliation.emit(updated_orders, snapshot)
        except Exception as exc:
            self.error_occurred.emit(str(exc))


class KisOrderQueryWorker(QThread):
    finished_query = pyqtSignal(list)
    error_occurred = pyqtSignal(str)

    def __init__(
        self,
        environment: Optional[str] = None,
        account_no: Optional[str] = None,
        symbol: Optional[str] = None,
        broker_order_id: Optional[str] = None,
        client_order_id: Optional[str] = None,
    ) -> None:
        super().__init__()
        self.environment = environment
        self.account_no = account_no
        self.symbol = symbol
        self.broker_order_id = broker_order_id
        self.client_order_id = client_order_id

    def run(self) -> None:
        try:
            from src.services.order_reconciliation import query_and_reconcile_unresolved_orders

            updated = query_and_reconcile_unresolved_orders(
                environment=self.environment,
                account_no=self.account_no,
                symbol=self.symbol,
            )
            if self.client_order_id:
                updated = [
                    order for order in updated
                    if order.client_order_id == self.client_order_id
                    or (self.broker_order_id and order.broker_order_id == self.broker_order_id)
                ]
            self.finished_query.emit(updated)
        except Exception as exc:
            self.error_occurred.emit(str(exc))


class KisOrderCancelWorker(QThread):
    finished_cancel = pyqtSignal(object)
    error_occurred = pyqtSignal(str)

    def __init__(self, client_order_id: str) -> None:
        super().__init__()
        self.client_order_id = client_order_id

    def run(self) -> None:
        try:
            from src.services.order_reconciliation import cancel_and_reconcile_order

            order = cancel_and_reconcile_order(self.client_order_id)
            self.finished_cancel.emit(order)
        except Exception as exc:
            self.error_occurred.emit(str(exc))


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
        environment: str = "SIM",
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
                        cached, _source = load_best_intraday_history(self.symbol, self.engine, interval="5m", since=since)
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
                raise RuntimeError("; ".join(result.warnings) or "No 5-minute intraday rows returned.")

            if self.engine is not None:
                save_intraday_history_to_db(self.symbol, fetched, self.engine, interval="5m", source=result.source)
                opening_result = fetch_intraday_with_fallback(self._request(IntradayInterval.ONE_MINUTE, 1))
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
            self.finished_fetch.emit(self.symbol, fetched, self.window_days, result.source)
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
    def _download_with_retries(symbol: str, days: int, attempts: int = 3) -> pd.DataFrame:
        from src.services.yfinance_intraday_provider import _download_5m_with_retries

        return _download_5m_with_retries(symbol, days, attempts=attempts)

    @staticmethod
    def _download_opening_1m_bar(symbol: str) -> pd.DataFrame:
        from src.services.yfinance_intraday_provider import _download_opening_1m_bar

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
        environment: str = "SIM",
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
                result = fetch_intraday_with_fallback(self._request(symbol, IntradayInterval.FIVE_MINUTE, fetch_days))
                for warning in result.warnings:
                    self.provider_warning.emit(symbol, warning)
                fetched = result.bars
                if fetched.empty:
                    raise RuntimeError("; ".join(result.warnings) or "No 5-minute intraday rows returned.")
                if self.engine is not None:
                    save_intraday_history_to_db(symbol, fetched, self.engine, interval="5m", source=result.source)
                    opening_result = fetch_intraday_with_fallback(self._request(symbol, IntradayInterval.ONE_MINUTE, 1))
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

    def _request(self, symbol: str, interval: IntradayInterval, days: int) -> IntradayRequest:
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
            cached, _source = load_best_intraday_history(symbol, self.engine, interval="5m", since=since)
        except Exception:
            return self.window_days
        if intraday_cache_needs_backfill(cached, since):
            return self.window_days
        return 1


class ScannerWorker(QThread):
    finished_scan = pyqtSignal(list, object)
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
    ):
        super().__init__()
        self.tickers = tickers
        self.engine = engine
        self.min_volume = min_volume
        self.min_dollar_volume = min_dollar_volume
        self.min_adr = min_adr
        self.min_growth_rank = min_growth_rank
        self.min_trend_intensity = min_trend_intensity

    def run(self) -> None:
        try:
            from src.utils.db_loader import get_universe_stock_metrics_from_db

            if self.engine is None:
                raise RuntimeError("MySQL cache is unavailable. Configure MySQL, then refresh the cache before scanning.")

            self.log_message.emit("Running scanner using MySQL cache...")
            stock_metrics = get_universe_stock_metrics_from_db(
                self.tickers,
                engine=self.engine,
            )

            self.finished_scan.emit(stock_metrics, None)
        except Exception as exc:
            self.error_occurred.emit(str(exc))


class WatchlistAiWorker(QThread):
    finished_analysis = pyqtSignal(dict)
    finished_analysis_df = pyqtSignal(pd.DataFrame)
    progress_update = pyqtSignal(str)
    log_message = pyqtSignal(str)

    def __init__(self, watchlist_items, db_engine, account_size, risk_percent, active_plans=None, env="SIM"):
        super().__init__()
        self.watchlist_items = watchlist_items
        self.db_engine = db_engine
        self.account_size = account_size
        self.risk_percent = risk_percent
        self.active_plans = active_plans or {}
        self.env = env

    @staticmethod
    def _calculate_position_values(
        account_size: float,
        risk_percent: float,
        entry_price: float,
        stop_price: float,
        adr_percent: Optional[float] = None,
    ) -> dict:
        total_risk = account_size * risk_percent if account_size > 0 and risk_percent > 0 else 0.0
        risk_per_share = max(0.0, entry_price - stop_price)
        raw_shares = total_risk / risk_per_share if total_risk > 0 and risk_per_share > 0 else 0.0
        shares = float(math.ceil(raw_shares)) if raw_shares > 0 else 0.0
        investment = shares * entry_price
        capital_percent = (investment / account_size * 100.0) if account_size > 0 else 0.0
        stop_loss_percent = (risk_per_share / entry_price * 100.0) if entry_price > 0 else 0.0
        sl_adr = (stop_loss_percent / adr_percent * 100.0) if adr_percent and adr_percent > 0 else None
        return {
            "total_risk": total_risk,
            "risk_per_share": risk_per_share,
            "shares": shares,
            "investment": investment,
            "capital_percent": capital_percent,
            "stop_loss_percent": stop_loss_percent,
            "sl_adr": sl_adr,
        }

    @staticmethod
    def _is_plan_valid(sizing: dict, adr_percent: Optional[float]) -> bool:
        if sizing.get("shares", 0.0) < 1.0:
            return False
        capital_percent = sizing.get("capital_percent", 0.0)
        if capital_percent < 10.0 or capital_percent >= 30.0:
            return False
        stop_loss_percent = sizing.get("stop_loss_percent", 0.0)
        if adr_percent is not None and adr_percent > 0 and stop_loss_percent >= adr_percent:
            return False
        sl_adr = sizing.get("sl_adr")
        if sl_adr is not None and (sl_adr < 15.0 or sl_adr > 66.0):
            return False
        return True

    @staticmethod
    def _score_recommendation(sizing: dict, risk_percent: float) -> float:
        sl_adr = sizing.get("sl_adr")
        capital_percent = sizing.get("capital_percent", 0.0)
        if sl_adr is None:
            return 0.0
        sl_adr_score = max(0.0, 100.0 - abs(float(sl_adr) - 65.0) * 3.0)
        capital_score = max(0.0, 100.0 - abs(float(capital_percent) - 17.5) * 4.0)
        risk_score = max(0.0, 100.0 - float(risk_percent) * 100.0 * 25.0)
        return round((sl_adr_score * 0.45) + (capital_score * 0.40) + (risk_score * 0.15), 1)

    @staticmethod
    def _select_recommended_plan(df: pd.DataFrame, symbol: str) -> Optional[pd.Series]:
        symbol_df = df[df['symbol'] == symbol]
        if symbol_df.empty:
            return None
            
        # Case A: Saved trade plans have highest priority
        saved_df = symbol_df[symbol_df['case_type'] == 'A_SAVED']
        if not saved_df.empty:
            return saved_df.iloc[0]
            
        # Case B: Manual overrides
        manual_df = symbol_df[symbol_df['case_type'] == 'B_MANUAL']
        if not manual_df.empty:
            valid_manual = manual_df[manual_df['valid'] == True]
            if not valid_manual.empty:
                return valid_manual.sort_values(by=['score', 'risk_pct'], ascending=[False, True]).iloc[0]
            return manual_df.sort_values(by=['score', 'risk_pct'], ascending=[False, True]).iloc[0]
            
        # Case C & D: ORB and Daily plans
        orb_df = symbol_df[symbol_df['case_type'] == 'C_ORB']
        valid_orb = orb_df[orb_df['valid'] == True]
        if not valid_orb.empty:
            return valid_orb.sort_values(by=['score', 'risk_pct'], ascending=[False, True]).iloc[0]
            
        daily_df = symbol_df[symbol_df['case_type'] == 'D_DAILY']
        valid_daily = daily_df[daily_df['valid'] == True]
        if not valid_daily.empty:
            return valid_daily.sort_values(by=['score', 'risk_pct'], ascending=[False, True]).iloc[0]
            
        orb_and_daily = symbol_df[symbol_df['case_type'].isin(['C_ORB', 'D_DAILY'])]
        if not orb_and_daily.empty:
            invalid_orb = orb_and_daily[orb_and_daily['case_type'] == 'C_ORB']
            if not invalid_orb.empty:
                return invalid_orb.sort_values(by=['score', 'risk_pct'], ascending=[False, True]).iloc[0]
            return orb_and_daily.sort_values(by=['score', 'risk_pct'], ascending=[False, True]).iloc[0]
            
        return symbol_df.iloc[0]

    def run(self) -> None:
        import pandas as pd
        import math
        import datetime as dt
        from src.core.scoring import calculate_deterministic_scores, run_ai_review
        from src.utils.db_loader import load_symbol_history_from_db
        from src.utils.data_loader import download_price_history
        from src.core.orb import calculate_orb_range

        total_items = len(self.watchlist_items)
        self.log_message.emit(f"Starting AI Review & Scoreboard Analysis for {total_items} items.")
        
        # Phase 1: Perform Trade Plan for all watchlist items first and save as DataFrame
        all_candidates = []
        histories = {}
        adr_percents = {}
        prices = {}
        
        for idx, item in enumerate(self.watchlist_items):
            symbol = item.symbol.upper().strip()
            self.progress_update.emit(f"Generating Trade Plans for {symbol} ({idx+1}/{total_items})...")
            
            # Load daily price history
            history = pd.DataFrame()
            if self.db_engine is not None:
                history = load_symbol_history_from_db(symbol, self.db_engine, interval="1d")
            if history.empty:
                self.log_message.emit(f"Cache miss for {symbol}. Fetching daily history from yfinance...")
                history = download_price_history([symbol], period="6mo", interval="1d", max_symbols=1)
                
            histories[symbol] = history
            
            if history.empty:
                prices[symbol] = 0.0
                adr_percents[symbol] = 2.5
                continue
                
            latest_bar = history.iloc[-1]
            price = float(latest_bar["Close"])
            prices[symbol] = price
            
            # Calculate ADR
            prev_close = history["Close"].astype(float).shift(1)
            high_low_ratio = (history["High"].astype(float) - history["Low"].astype(float)) / prev_close
            adr_percent_series = high_low_ratio.rolling(20, min_periods=5).mean() * 100.0
            adr_percent = float(adr_percent_series.iloc[-1]) if not pd.isna(adr_percent_series.iloc[-1]) else 2.5
            adr_percents[symbol] = adr_percent
            
            # Load intraday history
            since_dt = _utcnow_naive() - dt.timedelta(days=7)
            one_minute = pd.DataFrame()
            five_minute = pd.DataFrame()
            if self.db_engine is not None:
                try:
                    one_minute, _one_source = load_best_intraday_history(
                        symbol, self.db_engine, interval="1m", since=since_dt
                    )
                except Exception:
                    pass
                try:
                    five_minute, _five_source = load_best_intraday_history(
                        symbol, self.db_engine, interval="5m", since=since_dt
                    )
                except Exception:
                    pass
                    
            def get_latest_session(df):
                if df.empty:
                    return df
                df_sorted = df.sort_index().copy()
                dates = pd.to_datetime(df_sorted.index).date
                return df_sorted[dates == dates[-1]]
            
            one_min_session = get_latest_session(one_minute)
            five_min_session = get_latest_session(five_minute)
            
            # Define risk cases to evaluate
            selected_risk = self.risk_percent
            risk_cases = [0.0025, 0.005, 0.0075, 0.01, 0.0125, 0.015, 0.0175, 0.02]
            if selected_risk > 0 and all(abs(selected_risk - case) > 0.00001 for case in risk_cases):
                risk_cases.append(selected_risk)
            risk_cases = sorted(risk_cases)
            
            # Check Case A: Saved trade plan
            trade_plan = self.active_plans.get(symbol)
            if trade_plan is not None:
                entry_price = trade_plan.entry_price
                stop_loss = trade_plan.stop_loss
                risk_pct = getattr(trade_plan, "risk_percent", self.risk_percent)
                if risk_pct is None or risk_pct <= 0:
                    risk_pct = self.risk_percent
                    
                sizing = self._calculate_position_values(self.account_size, risk_pct, entry_price, stop_loss, adr_percent)
                valid = self._is_plan_valid(sizing, adr_percent)
                score = self._score_recommendation(sizing, risk_pct)
                
                all_candidates.append({
                    "symbol": symbol,
                    "case_type": "A_SAVED",
                    "window": "saved",
                    "risk_pct": risk_pct,
                    "entry_price": entry_price,
                    "stop_loss": stop_loss,
                    "shares": sizing["shares"],
                    "capital_percent": sizing["capital_percent"],
                    "stop_loss_percent": sizing["stop_loss_percent"],
                    "sl_adr": sizing["sl_adr"],
                    "valid": valid,
                    "score": score,
                    "env": self.env
                })
                
            # Case B: Manual prices
            has_manual_prices = (item.entry_price is not None and item.entry_price > 0 and
                                 item.stop_loss is not None and item.stop_loss > 0)
            if has_manual_prices:
                for r_case in risk_cases:
                    entry_price = item.entry_price
                    stop_loss = item.stop_loss
                    
                    sizing = self._calculate_position_values(self.account_size, r_case, entry_price, stop_loss, adr_percent)
                    valid = self._is_plan_valid(sizing, adr_percent)
                    score = self._score_recommendation(sizing, r_case)
                    
                    all_candidates.append({
                        "symbol": symbol,
                        "case_type": "B_MANUAL",
                        "window": "manual",
                        "risk_pct": r_case,
                        "entry_price": entry_price,
                        "stop_loss": stop_loss,
                        "shares": sizing["shares"],
                        "capital_percent": sizing["capital_percent"],
                        "stop_loss_percent": sizing["stop_loss_percent"],
                        "sl_adr": sizing["sl_adr"],
                        "valid": valid,
                        "score": score,
                        "env": self.env
                    })
                    
            # Case C: Intraday ORB search
            orb_windows = [
                ("1m", one_min_session),
                ("5m", five_min_session),
                ("30m", five_min_session),
            ]
            for r_case in risk_cases:
                for window, history_df in orb_windows:
                    if history_df.empty:
                        continue
                    orb_range = calculate_orb_range(symbol, history_df, window)
                    if orb_range is None:
                        continue
                        
                    orb_high = float(orb_range.high)
                    breakout_price = float(getattr(item, "breakout_price", 0.0) or 0.0)
                    buffer_pct = 0.001
                    breakout_trigger = breakout_price * (1 + buffer_pct) if breakout_price > 0 else 0.0
                    entry_price = orb_high
                    stop_loss = float(orb_range.low)
                    
                    sizing = self._calculate_position_values(self.account_size, r_case, entry_price, stop_loss, adr_percent)
                    valid = breakout_price > 0 and orb_high > breakout_trigger and self._is_plan_valid(sizing, adr_percent)
                    score = self._score_recommendation(sizing, r_case)
                    
                    all_candidates.append({
                        "symbol": symbol,
                        "case_type": "C_ORB",
                        "window": window,
                        "risk_pct": r_case,
                        "entry_price": entry_price,
                        "stop_loss": stop_loss,
                        "shares": sizing["shares"],
                        "capital_percent": sizing["capital_percent"],
                        "stop_loss_percent": sizing["stop_loss_percent"],
                        "sl_adr": sizing["sl_adr"],
                        "valid": valid,
                        "score": score,
                        "env": self.env
                    })
                    
            # Case D: Daily Fallback
            for r_case in risk_cases:
                entry_price = item.entry_price if (item.entry_price and item.entry_price > 0) else price
                stop_loss = item.stop_loss if (item.stop_loss and item.stop_loss > 0) else entry_price * (1.0 - (0.75 * adr_percent / 100.0))
                
                sizing = self._calculate_position_values(self.account_size, r_case, entry_price, stop_loss, adr_percent)
                valid = self._is_plan_valid(sizing, adr_percent)
                score = self._score_recommendation(sizing, r_case)
                
                all_candidates.append({
                    "symbol": symbol,
                    "case_type": "D_DAILY",
                    "window": "daily",
                    "risk_pct": r_case,
                    "entry_price": entry_price,
                    "stop_loss": stop_loss,
                    "shares": sizing["shares"],
                    "capital_percent": sizing["capital_percent"],
                    "stop_loss_percent": sizing["stop_loss_percent"],
                    "sl_adr": sizing["sl_adr"],
                    "valid": valid,
                    "score": score,
                    "env": self.env
                })

        # Save it as a dataframe then use the data
        if all_candidates:
            candidates_df = pd.DataFrame(all_candidates)
        else:
            candidates_df = pd.DataFrame(columns=[
                "symbol", "case_type", "window", "risk_pct", "entry_price", "stop_loss",
                "shares", "capital_percent", "stop_loss_percent", "sl_adr", "valid", "score", "env"
            ])
            
        recommended_rows = []
        for item in self.watchlist_items:
            symbol = item.symbol.upper().strip()
            rec = self._select_recommended_plan(candidates_df, symbol)
            if rec is not None:
                recommended_rows.append(rec)
                
        if recommended_rows:
            recommended_df = pd.DataFrame(recommended_rows)
        else:
            recommended_df = pd.DataFrame(columns=candidates_df.columns)
            
        results_list = []
        
        # Phase 2: Compute Scoreboard logic using the recommended plans from the DataFrame
        for idx, item in enumerate(self.watchlist_items):
            symbol = item.symbol.upper().strip()
            self.progress_update.emit(f"Scoring {symbol} ({idx+1}/{total_items})...")
            self.log_message.emit(f"Scoring {symbol}...")
            
            history = histories.get(symbol, pd.DataFrame())
            
            try:
                # Find the selected recommended plan from recommended_df
                if not recommended_df.empty and symbol in recommended_df["symbol"].values:
                    rec_row = recommended_df[recommended_df["symbol"] == symbol].iloc[0]
                    entry_price = float(rec_row["entry_price"])
                    stop_loss = float(rec_row["stop_loss"])
                    risk_pct = float(rec_row["risk_pct"])
                else:
                    # Fallback if somehow not found
                    entry_price = item.entry_price or 0.0
                    stop_loss = item.stop_loss or 0.0
                    risk_pct = self.risk_percent
                    
                scores = calculate_deterministic_scores(
                    symbol=symbol,
                    history=history,
                    entry_price=entry_price,
                    breakout_price=getattr(item, "breakout_price", None),
                    stop_loss=stop_loss,
                    account_size=self.account_size,
                    risk_percent=risk_pct,
                )
                
                # Optional AI review
                today_str = dt.date.today().isoformat()
                cached = getattr(item, "ai_analysis", None)
                if (cached and isinstance(cached, dict) and 
                    cached.get("full_json") and 
                    cached.get("full_json", {}).get("as_of_date") == today_str):
                    self.log_message.emit(f"Using cached AI review for {symbol} (As-of: {today_str})")
                    ai_res = cached
                else:
                    self.log_message.emit(f"Cache miss/outdated for {symbol}. Fetching new AI review...")
                    ai_res = run_ai_review(symbol, scores, reasoning=item.notes)
                    item.ai_analysis = ai_res
                
                scores["ai_summary"] = ai_res.get("summary", "Bullish consolidation pattern setup.")
                scores["ai_catalyst"] = ai_res.get("catalyst", "")
                scores["news_score"] = ai_res.get("news_score", 80.0)
                
                # Total Score weighted calculation
                total_score = (
                    scores["technical_score"] * 0.25 +
                    scores["setup_score"] * 0.25 +
                    scores["risk_score"] * 0.20 +
                    scores["news_score"] * 0.15 +
                    scores["timing_score"] * 0.15
                )
                scores["total_score"] = round(total_score, 1)
                
                # Status determination based on eligibility
                has_hard_reject = len(scores.get("warnings", [])) > 0
                if has_hard_reject:
                    scores["status"] = "REJECTED"
                elif scores["total_score"] >= 85:
                    scores["status"] = "BUY_READY"
                else:
                    scores["status"] = "WATCHING"
                    
                scores["symbol"] = symbol
                scores["env"] = self.env
                results_list.append(scores)
                self.log_message.emit(f"{symbol} analysis complete. Status: {scores['status']}, Score: {scores['total_score']:.1f}")
                
            except Exception as e:
                self.log_message.emit(f"Error scoring {symbol}: {str(e)}")
                results_list.append({
                    "symbol": symbol,
                    "env": self.env,
                    "price": 0.0,
                    "total_score": 0.0,
                    "status": "ERROR",
                    "technical_score": 0.0,
                    "setup_score": 0.0,
                    "risk_score": 0.0,
                    "news_score": 0.0,
                    "timing_score": 0.0,
                    "rr": 0.0,
                    "stop_adr": 0.0,
                    "position_percent": 0.0,
                    "ai_summary": f"Failed: {str(e)}",
                    "ai_catalyst": "",
                    "warnings": [f"Analysis error: {str(e)}"],
                    "entry_price": item.entry_price or 0.0,
                    "breakout_price": getattr(item, "breakout_price", None),
                    "stop_loss": item.stop_loss or 0.0,
                })
                
        df = pd.DataFrame(results_list)
        results = df.set_index("symbol").to_dict(orient="index") if not df.empty else {}
        self.finished_analysis_df.emit(recommended_df)
        self.finished_analysis.emit(results)


class SingleStockAiWorker(QThread):
    finished_analysis = pyqtSignal(dict)

    def __init__(self, symbol: str, item, db_engine, parent):
        super().__init__()
        self.symbol = symbol.upper().strip()
        self.item = item
        self.db_engine = db_engine
        self.parent = parent
        self.env = parent.watchlist_env_combo.currentText() if hasattr(parent, "watchlist_env_combo") else "SIM"
        self.account_size = parent._get_account_balance_for_env(self.env)
        self.risk_percent = parent._parse_float(parent.risk_percent_input, 1.0) / 100.0

    def run(self) -> None:
        import pandas as pd
        import json
        import datetime as dt
        from src.core.scoring import run_ai_review, calculate_deterministic_scores, fetch_recent_news_headlines
        from src.utils.db_loader import load_symbol_history_from_db
        from src.utils.data_loader import download_price_history

        symbol = self.symbol
        item = self.item

        # Load daily price history
        history = pd.DataFrame()
        if self.db_engine is not None:
            history = load_symbol_history_from_db(symbol, self.db_engine, interval="1d")
        if history.empty:
            history = download_price_history([symbol], period="6mo", interval="1d", max_symbols=1)

        if history.empty:
            self.finished_analysis.emit({"error": f"Could not load daily history for {symbol}."})
            return

        latest_bar = history.iloc[-1]
        price = float(latest_bar["Close"])

        # Calculate ADR
        prev_close = history["Close"].astype(float).shift(1)
        high_low_ratio = (history["High"].astype(float) - history["Low"].astype(float)) / prev_close
        adr_percent_series = high_low_ratio.rolling(20, min_periods=5).mean() * 100.0
        adr_percent = float(adr_percent_series.iloc[-1]) if not pd.isna(adr_percent_series.iloc[-1]) else 2.5

        # Calculate EMAs
        ema_20_series = history["Close"].ewm(span=20, adjust=False).mean()
        ema_50_series = history["Close"].ewm(span=50, adjust=False).mean()
        ema_20 = float(ema_20_series.iloc[-1]) if not pd.isna(ema_20_series.iloc[-1]) else price
        ema_50 = float(ema_50_series.iloc[-1]) if not pd.isna(ema_50_series.iloc[-1]) else price

        # Generate baseline metrics for scoring
        entry_price = item.entry_price if (item.entry_price and item.entry_price > 0) else price
        stop_loss = item.stop_loss if (item.stop_loss and item.stop_loss > 0) else entry_price * (1.0 - (0.75 * adr_percent / 100.0))

        scores = calculate_deterministic_scores(
            symbol=symbol,
            history=history,
            entry_price=entry_price,
            breakout_price=getattr(item, "breakout_price", None),
            stop_loss=stop_loss,
            account_size=self.account_size,
            risk_percent=self.risk_percent,
        )
        # Re-attach details to scores dict
        scores["ema_20"] = ema_20
        scores["ema_50"] = ema_50
        scores["above_20_ema"] = bool(price > ema_20)
        scores["above_50_ema"] = bool(price > ema_50)

        # Assemble new inputs for prompt
        scanner_metrics_json = json.dumps({
            "volume_20d_avg": float(history["Volume"].tail(20).mean()),
            "adr_20_pct": adr_percent,
            "daily_volume": float(latest_bar["Volume"]),
            "daily_dollar_volume": float(latest_bar["Volume"] * price)
        }, indent=2)

        technical_indicators_json = json.dumps({
            "above_20_ema": bool(price > ema_20),
            "above_50_ema": bool(price > ema_50),
            "ema_20": ema_20,
            "ema_50": ema_50,
            "current_price": price
        }, indent=2)

        active_plans = self.parent.trade_manager.get_active_plans() if hasattr(self.parent, "trade_manager") else []
        plan = next((p for p in active_plans if p.symbol == symbol), None)
        trade_plan_json = ""
        if plan:
            trade_plan_json = json.dumps({
                "entry_price": plan.entry_price,
                "stop_loss": plan.stop_loss,
                "exit_model": "No fixed profit target; partial exit after 3-5 working days if the trade has worked, final exit below selected EMA.",
                "risk_percent": getattr(plan, "risk_percent", self.risk_percent)
            }, indent=2)

        account_risk_json = json.dumps({
            "account_size_usd": self.account_size,
            "risk_percent_of_account": self.risk_percent * 100.0
        }, indent=2)

        chart_notes = ""
        user_notes = item.notes or ""

        today_str = dt.date.today().isoformat()
        cached = getattr(item, "ai_analysis", None)
        if (cached and isinstance(cached, dict) and 
            cached.get("full_json") and 
            cached.get("full_json", {}).get("as_of_date") == today_str):
            ai_res = cached
        else:
            # Call rich run_ai_review
            ai_res = run_ai_review(
                symbol=symbol,
                metrics=scores,
                reasoning=user_notes,
                company_name=item.name or symbol,
                as_of_date=today_str,
                current_price=price,
                scanner_metrics_json=scanner_metrics_json,
                technical_indicators_json=technical_indicators_json,
                chart_notes=chart_notes,
                trade_plan_json=trade_plan_json,
                account_risk_json=account_risk_json,
                user_notes=user_notes
            )
            item.ai_analysis = ai_res

        self.finished_analysis.emit(ai_res)


