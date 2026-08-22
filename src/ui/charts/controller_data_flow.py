"""TradingView and intraday data-flow orchestration."""

from __future__ import annotations

import datetime as dt
from typing import List, Optional
from zoneinfo import ZoneInfo

import pandas as pd
from PyQt5.QtCore import Qt, QTimer, QUrl
from PyQt5.QtWidgets import QMessageBox

try:
    from PyQt5.QtWebEngineWidgets import QWebEngineView
except ImportError:
    QWebEngineView = None
try:
    from PyQt5.QtWebChannel import QWebChannel
except ImportError:
    QWebChannel = None

from src.core.orb import resample_intraday_bars
from src.core.chart_fundamentals import (
    ChartFundamentalContext, canonical_symbol)
from src.infrastructure.database.repositories.chart_indicators import (
    calculate_chart_indicators, load_chart_indicators_from_db,
    refresh_chart_indicators_for_symbol)
from src.infrastructure.database.repositories.market_bars import \
    delete_intraday_history_for_symbol
from src.services.intraday_data_service import (format_intraday_source_label,
                                                load_best_intraday_history)
from src.services.chart_fundamentals import ChartFundamentalService
from src.ui.fundamental_worker import ChartFundamentalRefreshWorker
from src.ui.workers import IntradayBulkFetchWorker, IntradayFetchWorker
from src.utils.intraday_helpers import intraday_cache_needs_backfill
from src.utils.intraday_helpers import utcnow_naive as _utcnow_naive

from .render_assets import lightweight_charts_base_path

REFERENCE_SYMBOL = "SPY"
KST_ZONE = ZoneInfo("Asia/Seoul")
US_MARKET_ZONE = ZoneInfo("America/New_York")
MARKET_DATA_READY_TIME_KST = dt.time(7, 0)
LIVE_INTRADAY_REFRESH_INTERVAL_MS = 5 * 60 * 1000
TRADINGVIEW_REFRESH_INTERVAL_SECONDS = 5 * 60
KIS_DAILY_CHART_FAILURE_COOLDOWN_SECONDS = 30 * 60
CHART_FUNDAMENTAL_CONTEXT_CACHE_SECONDS = 30
CHART_FUNDAMENTAL_FRESHNESS_CACHE_SECONDS = 5 * 60
CHART_FUNDAMENTAL_CACHE_MAX_SYMBOLS = 128
MAX_CONCURRENT_CHART_FUNDAMENTAL_WORKERS = 2
CHART_REFERENCE_HISTORY_CACHE_SECONDS = 30
US_MARKET_OPEN_TIME = dt.time(9, 30)
US_MARKET_CLOSE_TIME = dt.time(16, 0)


class ChartsDataFlowMixin:
    def load_tradingview_chart(
        self,
        show_empty_message: bool = True,
        force: bool = False,
        fetch_live: bool = False,
        skip_split_view: bool = False,
    ) -> None:
        if not hasattr(self, "tradingview_symbol_combo"):
            return
        symbol = self.tradingview_symbol_combo.currentText().strip().upper()
        if not symbol:
            message = "Enter or select a symbol first."
            self.current_tradingview_symbol = ""
            if hasattr(self, "tradingview_status_label"):
                self.tradingview_status_label.setText(message)
            if show_empty_message:
                self._set_html_or_text(
                    self.tradingview_chart_view,
                    self._generate_message_html("No watchlist symbols", message),
                    message,
                )
                if hasattr(self, "tradingview_split_chart_view"):
                    self._set_html_or_text(
                        self.tradingview_split_chart_view,
                        self._generate_message_html("No watchlist symbols", message),
                        message,
                    )
            return

        tradingview_symbol = self._to_tradingview_symbol(symbol)
        base_options = {
            "show_volume": (
                self.tradingview_show_volume_checkbox.isChecked()
                if hasattr(self, "tradingview_show_volume_checkbox")
                else True
            ),
            "show_ema": (
                self.tradingview_show_ema_checkbox.isChecked()
                if hasattr(self, "tradingview_show_ema_checkbox")
                else True
            ),
            "show_rs": (
                self.tradingview_show_rs_checkbox.isChecked()
                if hasattr(self, "tradingview_show_rs_checkbox")
                else True
            ),
            "show_adr": (
                self.tradingview_show_adr_checkbox.isChecked()
                if hasattr(self, "tradingview_show_adr_checkbox")
                else True
            ),
            "show_growth_1m": (
                self.tradingview_show_growth_1m_checkbox.isChecked()
                if hasattr(self, "tradingview_show_growth_1m_checkbox")
                else True
            ),
            "show_growth_3m": (
                self.tradingview_show_growth_3m_checkbox.isChecked()
                if hasattr(self, "tradingview_show_growth_3m_checkbox")
                else True
            ),
            "show_growth_6m": (
                self.tradingview_show_growth_6m_checkbox.isChecked()
                if hasattr(self, "tradingview_show_growth_6m_checkbox")
                else False
            ),
            "show_earnings_events": (
                self.tradingview_show_earnings_checkbox.isChecked()
                if hasattr(self, "tradingview_show_earnings_checkbox")
                else True
            ),
            "show_earnings_line": (
                self.tradingview_show_earnings_checkbox.isChecked()
                if hasattr(self, "tradingview_show_earnings_checkbox")
                else True
            ),
            "show_stock_profile_watermark": True,
            "earnings_horizon_days": 14,
            "window_days": self._get_tradingview_window_days(),
        }
        now = dt.datetime.now(dt.timezone.utc)
        fundamental_generation = self._chart_fundamental_generation(symbol)
        fundamental_context = self._load_cached_chart_fundamental_context(
            symbol,
            now=now,
            horizon_days=int(base_options["earnings_horizon_days"]),
        )

        split_enabled = (
            hasattr(self, "tradingview_split_screen_checkbox")
            and self.tradingview_split_screen_checkbox.isChecked()
        )
        if split_enabled:
            self.tradingview_split_chart_view.setVisible(True)
            primary_status = self._render_tradingview_chart_view(
                self.tradingview_chart_view,
                symbol=symbol,
                tradingview_symbol=tradingview_symbol,
                timeframe="1D",
                base_options=base_options,
                now=now,
                force=force,
                fetch_live=fetch_live,
                view_key="left",
                fundamental_context=fundamental_context,
            )
            if not skip_split_view:
                split_status = self._render_tradingview_chart_view(
                    self.tradingview_split_chart_view,
                    symbol=symbol,
                    tradingview_symbol=tradingview_symbol,
                    timeframe="1H",
                    base_options=base_options,
                    now=now,
                    force=force,
                    fetch_live=fetch_live,
                    view_key="right",
                    fundamental_context=fundamental_context,
                )
            else:
                split_status = "1H skipped (drawing sync)"
            self.current_tradingview_symbol = (
                f"{tradingview_symbol}|split|volume={int(base_options['show_volume'])}|"
                f"ema={int(base_options['show_ema'])}|rs={int(base_options.get('show_rs', True))}"
            )
            self.tradingview_status_label.setText(f"{primary_status} | {split_status}")
            self._schedule_chart_fundamental_refresh(
                symbol, fundamental_generation, now=now
            )
            return

        if hasattr(self, "tradingview_split_chart_view"):
            self.tradingview_split_chart_view.setVisible(False)
        timeframe = (
            self.tradingview_timeframe_combo.currentText().strip().upper()
            if hasattr(self, "tradingview_timeframe_combo")
            else "1D"
        )
        status = self._render_tradingview_chart_view(
            self.tradingview_chart_view,
            symbol=symbol,
            tradingview_symbol=tradingview_symbol,
            timeframe=timeframe,
            base_options=base_options,
            now=now,
            force=force,
            fetch_live=fetch_live,
            view_key="single",
            fundamental_context=fundamental_context,
        )
        self.current_tradingview_symbol = (
            f"{tradingview_symbol}|{timeframe}|volume={int(base_options['show_volume'])}|"
            f"ema={int(base_options['show_ema'])}|rs={int(base_options.get('show_rs', True))}"
        )
        self.tradingview_status_label.setText(status)
        self._schedule_chart_fundamental_refresh(
            symbol, fundamental_generation, now=now
        )

    def _render_tradingview_chart_view(
        self,
        target_view,
        symbol: str,
        tradingview_symbol: str,
        timeframe: str,
        base_options: dict,
        now: dt.datetime,
        force: bool,
        view_key: str,
        fetch_live: bool = False,
        fundamental_context: Optional[ChartFundamentalContext] = None,
    ) -> str:
        options = {
            "show_volume": bool(base_options.get("show_volume", True)),
            "show_ema": bool(base_options.get("show_ema", True)),
            "show_rs": bool(base_options.get("show_rs", True)),
            "show_adr": bool(base_options.get("show_adr", True)),
            "show_growth_1m": bool(base_options.get("show_growth_1m", True)),
            "show_growth_3m": bool(base_options.get("show_growth_3m", True)),
            "show_growth_6m": bool(base_options.get("show_growth_6m", False)),
            "show_earnings_events": bool(
                base_options.get("show_earnings_events", True)
            ),
            "show_earnings_line": bool(base_options.get("show_earnings_line", True)),
            "show_stock_profile_watermark": bool(
                base_options.get("show_stock_profile_watermark", True)
            ),
            "earnings_horizon_days": int(
                base_options.get("earnings_horizon_days", 14) or 14
            ),
            "window_days": int(base_options.get("window_days", 7) or 7),
            "timeframe": timeframe,
            "view_key": view_key,
            "sync_crosshair": view_key in {"left", "right"},
        }
        options["max_history_bars"] = self._tradingview_max_history_bars(
            timeframe, options["window_days"]
        )
        options["history_is_normalized"] = True
        if timeframe.strip().upper() != "1D":
            options.update(
                {
                    "show_adr": False,
                    "show_growth_1m": False,
                    "show_growth_3m": False,
                    "show_growth_6m": False,
                }
            )
        base_refresh_key = (
            f"{view_key}|{tradingview_symbol}|{timeframe}|"
            f"volume={int(options['show_volume'])}|ema={int(options['show_ema'])}|"
            f"rs={int(options.get('show_rs', True))}|adr={int(options.get('show_adr', False))}|"
            f"g1={int(options.get('show_growth_1m', False))}|g3={int(options.get('show_growth_3m', False))}|"
            f"g6={int(options.get('show_growth_6m', False))}|window={options.get('window_days', 7)}"
        )
        refresh_key = base_refresh_key
        if fundamental_context is not None:
            refresh_key += (
                f"|earnings={int(options.get('show_earnings_events', True))}"
                f"|earnings_line={int(options.get('show_earnings_line', True))}"
                f"|profile={int(options.get('show_stock_profile_watermark', True))}"
                f"|fundamentals={fundamental_context.revision_token}"
            )
        last_refresh = self.tradingview_refresh_timestamps.get(refresh_key)
        if not force and not self._tradingview_refresh_due(last_refresh, now=now):
            next_refresh = last_refresh + dt.timedelta(
                seconds=TRADINGVIEW_REFRESH_INTERVAL_SECONDS
            )
            seconds_left = max(1, int((next_refresh - now).total_seconds()))
            return f"{timeframe} skipped; next auto refresh in {seconds_left // 60}m {seconds_left % 60}s"

        try:
            history = self._load_chart_history_for_timeframe(
                symbol,
                timeframe,
                use_live_fallback=fetch_live,
                window_days=int(options.get("window_days", 7) or 7),
                force_refresh=fetch_live,
            )
        except TypeError:
            history = self._load_chart_history_for_timeframe(
                symbol,
                timeframe,
                use_live_fallback=fetch_live,
                window_days=int(options.get("window_days", 7) or 7),
            )
        chart_history = self._normalize_chart_history(
            history,
            symbol,
            max_rows=options["max_history_bars"],
        )
        if chart_history.empty:
            message = f"No {timeframe} chart data found for {symbol}."
            self._set_html_or_text(
                target_view,
                self._generate_message_html(symbol, message),
                message,
            )
            self.tradingview_refresh_timestamps[refresh_key] = now
            return message

        latest_text = self._format_chart_latest_text(chart_history, timeframe)
        options["data_latest_text"] = latest_text

        drawings = self._build_combined_drawings(symbol, timeframe)
        watchlist = self.__dict__.get("watchlist")
        watchlist_item = watchlist.get(symbol) if watchlist is not None else None
        target_price = (
            watchlist_item.breakout_price if watchlist_item is not None else None
        )
        buylist_manager = self.__dict__.get("buylist_manager")
        buylist_item = (
            buylist_manager.get(symbol) if buylist_manager is not None else None
        )
        buy_price: Optional[float] = None
        buy_stop_loss: Optional[float] = None
        try:
            owned_shares = (
                max(0, int(getattr(buylist_item, "shares_held", 0) or 0))
                if buylist_item is not None
                else 0
            )
        except (TypeError, ValueError):
            owned_shares = 0
        if buylist_item is not None and owned_shares > 0:
            # Entry/stop fields also hold the ORB plan before a fill. Chart
            # position lines must represent executions, so never fall back to
            # the planned entry price when there is no broker-confirmed cost.
            raw_buy = buylist_item.avg_cost
            buy_price = float(raw_buy) if raw_buy and float(raw_buy) > 0 else None
            raw_stop = buylist_item.stop_loss
            buy_stop_loss = (
                float(raw_stop) if raw_stop and float(raw_stop) > 0 else None
            )
        indicators = (
            self._load_tradingview_indicator_history(symbol, timeframe, chart_history)
            if options.get("show_rs", True)
            else pd.DataFrame()
        )
        view_context = fundamental_context
        if view_context is not None and timeframe.strip().upper() == "1D":
            try:
                view_context = ChartFundamentalService.align_context_to_chart(
                    view_context, chart_history.index
                )
            except (TypeError, ValueError):
                # Supplemental data must never suppress an otherwise valid
                # price chart. Invalid/mixed EPS series is omitted and logged
                # by the provider/service path on its next refresh.
                view_context = view_context.__class__(
                    symbol=view_context.symbol,
                    stock_profile=view_context.stock_profile,
                    earnings_events=view_context.earnings_events,
                    next_earnings=view_context.next_earnings,
                    revision_token=view_context.revision_token,
                )
        html_content = self._generate_tradingview_lightweight_chart_html(
            tradingview_symbol,
            chart_history,
            options=options,
            drawings=drawings,
            storage_symbol=symbol,
            indicators=indicators,
            target_price=target_price,
            buy_price=buy_price,
            stop_loss=buy_stop_loss,
            interaction_settings=self.__dict__.get("settings", {}),
            stock_profile=(
                view_context.stock_profile if view_context is not None else None
            ),
            earnings_events=(
                view_context.earnings_events
                if view_context is not None and timeframe.strip().upper() == "1D"
                else ()
            ),
            earnings_line=(
                view_context.earnings_line
                if view_context is not None and timeframe.strip().upper() == "1D"
                else ()
            ),
            upcoming_earnings=(
                view_context.next_earnings if view_context is not None else None
            ),
        )
        if QWebEngineView is not None and isinstance(target_view, QWebEngineView):
            asset_base = str(lightweight_charts_base_path().resolve()) + "/"
            target_view.setHtml(html_content, QUrl.fromLocalFile(asset_base))
        else:
            target_view.setPlainText(
                f"TradingView Lightweight Chart for {tradingview_symbol} requires PyQtWebEngine."
            )
        self.tradingview_refresh_timestamps[refresh_key] = now
        return f"Loaded {timeframe} chart for {tradingview_symbol}"

    def _chart_fundamental_generation(self, symbol: str) -> int:
        canonical = canonical_symbol(symbol)
        previous = self.__dict__.get("_chart_fundamental_request_symbol", "")
        generation = int(
            self.__dict__.get("_chart_fundamental_request_generation", 0) or 0
        )
        if canonical != previous:
            generation += 1
            self._chart_fundamental_request_symbol = canonical
            self._chart_fundamental_request_generation = generation
        return generation

    def _load_cached_chart_fundamental_context(
        self,
        symbol: str,
        *,
        now: dt.datetime,
        horizon_days: int,
    ) -> ChartFundamentalContext:
        canonical = canonical_symbol(symbol)
        engine = self.__dict__.get("db_engine")
        if engine is None or not self.__dict__.get("db_enabled", False):
            return ChartFundamentalContext(symbol=canonical)
        cache_key = (id(engine), canonical, int(horizon_days))
        cache = self.__dict__.setdefault("_chart_fundamental_context_cache", {})
        cached = cache.get(cache_key)
        if cached is not None:
            cached_at, context = cached
            age_seconds = (now - cached_at).total_seconds()
            if 0 <= age_seconds < CHART_FUNDAMENTAL_CONTEXT_CACHE_SECONDS:
                return context
        try:
            context = ChartFundamentalService(engine).load_chart_fundamental_context(
                canonical,
                as_of=now,
                horizon_days=horizon_days,
            )
            cache[cache_key] = (now, context)
            while len(cache) > CHART_FUNDAMENTAL_CACHE_MAX_SYMBOLS:
                cache.pop(next(iter(cache)))
            return context
        except Exception as exc:
            append_log = getattr(self, "append_log", None)
            if callable(append_log):
                append_log(
                    f"Chart supplemental cache unavailable for {canonical}: {exc}"
                )
            return ChartFundamentalContext(symbol=canonical)

    def _schedule_chart_fundamental_refresh(
        self, symbol: str, generation: int, *, now: dt.datetime
    ) -> bool:
        if self.__dict__.get("db_engine_source") != "pc":
            return False
        engine = self.__dict__.get("db_engine")
        if engine is None or self.__dict__.get("_database_shutting_down", False):
            return False
        canonical = canonical_symbol(symbol)
        workers = self.__dict__.setdefault("_fundamental_refresh_workers", [])
        if any(
            getattr(worker, "symbol", "") == canonical
            and getattr(worker, "isRunning", lambda: False)()
            for worker in workers
        ):
            return False
        freshness_key = (id(engine), canonical)
        freshness_cache = self.__dict__.setdefault(
            "_chart_fundamental_freshness_cache", {}
        )
        last_fresh_check = freshness_cache.get(freshness_key)
        if last_fresh_check is not None:
            age_seconds = (now - last_fresh_check).total_seconds()
            if 0 <= age_seconds < CHART_FUNDAMENTAL_FRESHNESS_CACHE_SECONDS:
                return False
        running_workers = [
            worker
            for worker in workers
            if getattr(worker, "isRunning", lambda: False)()
        ]
        if len(running_workers) >= MAX_CONCURRENT_CHART_FUNDAMENTAL_WORKERS:
            self._pending_chart_fundamental_refresh = (
                canonical,
                int(generation),
                now,
            )
            return False
        worker = ChartFundamentalRefreshWorker(engine, canonical, generation)
        worker.completed.connect(self._on_chart_fundamental_refresh_completed)
        worker.not_required.connect(self._on_chart_fundamental_refresh_not_required)
        worker.failed.connect(self._on_chart_fundamental_refresh_failed)
        worker.finished.connect(self._drain_pending_chart_fundamental_refresh)
        workers.append(worker)
        self._fundamental_refresh_worker = worker
        track_worker = getattr(self, "_track_worker", None)
        if callable(track_worker):
            track_worker(
                "_fundamental_refresh_worker",
                worker,
                collection_name="_fundamental_refresh_workers",
            )
        worker.start()
        return True

    def _on_chart_fundamental_refresh_not_required(
        self, symbol: str, _generation: int
    ) -> None:
        engine = self.__dict__.get("db_engine")
        if engine is None:
            return
        freshness_cache = self.__dict__.setdefault(
            "_chart_fundamental_freshness_cache", {}
        )
        freshness_cache[(id(engine), canonical_symbol(symbol))] = (
            dt.datetime.now(dt.timezone.utc)
        )
        while len(freshness_cache) > CHART_FUNDAMENTAL_CACHE_MAX_SYMBOLS:
            freshness_cache.pop(next(iter(freshness_cache)))

    def _drain_pending_chart_fundamental_refresh(self) -> None:
        pending = self.__dict__.pop("_pending_chart_fundamental_refresh", None)
        if pending is None or self.__dict__.get("_database_shutting_down", False):
            return
        symbol, generation, requested_at = pending
        QTimer.singleShot(
            0,
            lambda: self._schedule_chart_fundamental_refresh(
                symbol,
                generation,
                now=requested_at,
            ),
        )

    def _on_chart_fundamental_refresh_completed(
        self, context: ChartFundamentalContext, generation: int
    ) -> None:
        # The chart has already rendered the database-cached context together
        # with price, ADR and RS/TI65.  A provider refresh must only warm the
        # cache for the next load; force-reloading the active QWebEngine page
        # makes earnings appear to arrive as a separate chart update.
        del generation
        engine = self.__dict__.get("db_engine")
        now = dt.datetime.now(dt.timezone.utc)
        if engine is not None:
            context_cache = self.__dict__.setdefault(
                "_chart_fundamental_context_cache", {}
            )
            context_cache[(id(engine), context.symbol, 14)] = (now, context)
            while len(context_cache) > CHART_FUNDAMENTAL_CACHE_MAX_SYMBOLS:
                context_cache.pop(next(iter(context_cache)))
            freshness_cache = self.__dict__.setdefault(
                "_chart_fundamental_freshness_cache", {}
            )
            freshness_cache[(id(engine), context.symbol)] = now
            while len(freshness_cache) > CHART_FUNDAMENTAL_CACHE_MAX_SYMBOLS:
                freshness_cache.pop(next(iter(freshness_cache)))
        append_log = getattr(self, "append_log", None)
        if callable(append_log):
            append_log(
                f"Refreshed chart profile/earnings cache for {context.symbol} "
                f"({len(context.earnings_events)} earnings events)."
            )

    def _on_chart_fundamental_refresh_failed(
        self, symbol: str, message: str, generation: int
    ) -> None:
        append_log = getattr(self, "append_log", None)
        if callable(append_log):
            append_log(f"Chart profile/earnings refresh failed for {symbol}: {message}")

    @staticmethod
    def _format_chart_latest_text(history: pd.DataFrame, timeframe: str) -> str:
        if history.empty:
            return "latest: unavailable"
        latest = pd.Timestamp(history.index[-1])
        timeframe = timeframe.strip().upper()
        if timeframe in {"1H", "5M"}:
            kst = (
                latest.tz_localize("UTC") if latest.tzinfo is None else latest
            ).tz_convert(KST_ZONE)
            return f"latest: {kst.strftime('%Y-%m-%d %H:%M')} KST"
        return f"latest: {latest.strftime('%Y-%m-%d')}"

    def _load_tradingview_indicator_history(
        self, symbol: str, timeframe: str, chart_history: pd.DataFrame
    ) -> pd.DataFrame:
        if chart_history.empty:
            return pd.DataFrame()
        symbol = symbol.strip().upper()
        timeframe = timeframe.strip().upper()
        if timeframe == "1D" and self.db_enabled and self.db_engine is not None:
            indicators = load_chart_indicators_from_db(
                symbol, self.db_engine, max_rows=len(chart_history)
            )
            if indicators.empty and refresh_chart_indicators_for_symbol(
                symbol, self.db_engine, reference_symbol=REFERENCE_SYMBOL
            ):
                indicators = load_chart_indicators_from_db(
                    symbol, self.db_engine, max_rows=len(chart_history)
                )
            if not indicators.empty:
                return self._align_chart_indicators(chart_history, indicators)

        reference_cache = self.__dict__.setdefault(
            "_chart_reference_history_cache", {}
        )
        reference_key = (id(self.__dict__.get("db_engine")), timeframe)
        now = dt.datetime.now(dt.timezone.utc)
        cached_reference = reference_cache.get(reference_key)
        reference_history = pd.DataFrame()
        if cached_reference is not None:
            cached_at, cached_history = cached_reference
            age_seconds = (now - cached_at).total_seconds()
            if 0 <= age_seconds < CHART_REFERENCE_HISTORY_CACHE_SECONDS:
                reference_history = cached_history
        if reference_history.empty:
            reference_history = self._load_chart_history_for_timeframe(
                REFERENCE_SYMBOL, timeframe, use_live_fallback=False
            )
            # The displayed symbol history already bounds the output. Keeping
            # the complete loaded reference avoids shortening RS/TI65.
            reference_history = self._normalize_chart_history(
                reference_history, REFERENCE_SYMBOL, max_rows=None
            )
            if not reference_history.empty:
                reference_cache[reference_key] = (now, reference_history)
        if reference_history.empty or "Close" not in reference_history.columns:
            return pd.DataFrame()
        indicators = calculate_chart_indicators(
            symbol, chart_history, reference_history
        )
        return self._align_chart_indicators(chart_history, indicators)

    def step_intraday_watchlist_symbol(self, direction: int) -> None:
        if not hasattr(self, "intraday_symbol_combo"):
            return
        count = self.intraday_symbol_combo.count()
        if count <= 0:
            self.intraday_status_label.setText("Add symbols to the watchlist first.")
            return

        current_index = self.intraday_symbol_combo.currentIndex()
        if current_index < 0:
            current_index = 0
        next_index = (current_index + direction) % count
        self.intraday_symbol_combo.setCurrentIndex(next_index)
        symbol = self.intraday_symbol_combo.currentText().strip().upper()

        sidebar_stock_list = self.__dict__.get("sidebar_stock_list")
        if sidebar_stock_list is not None:
            for row in range(sidebar_stock_list.count()):
                item = sidebar_stock_list.item(row)
                data = item.data(Qt.UserRole) or {}
                if data.get("symbol") == symbol:
                    sidebar_stock_list.setCurrentRow(row)
                    break

        self.plot_intraday_watchlist_symbol()

    def plot_intraday_watchlist_symbol(self, allow_fetch: bool = True) -> None:
        if not hasattr(self, "intraday_symbol_combo"):
            return
        symbol = self.intraday_symbol_combo.currentText().strip().upper()
        if not symbol:
            self.intraday_status_label.setText("Add symbols to the watchlist first.")
            return

        if not self.db_enabled or self.db_engine is None:
            message = (
                "Intraday charts and ORB monitoring require the MySQL cache. "
                "Configure MySQL, then refresh intraday data."
            )
            self._set_html_or_text(
                self.intraday_chart_view,
                self._generate_message_html("Intraday cache unavailable", message),
                message,
            )
            self.intraday_status_label.setText(message)
            return

        interval = self.intraday_interval_combo.currentText()
        window_days = self._get_intraday_window_days()
        symbol_history, cache_source = self._load_cached_intraday_5m_with_source(
            symbol, window_days=window_days
        )
        needs_backfill = self._intraday_cache_needs_backfill(
            symbol_history if symbol_history is not None else pd.DataFrame(),
            _utcnow_naive() - dt.timedelta(days=window_days),
        )
        if (
            allow_fetch
            and needs_backfill
            and self._can_start_intraday_fetch(symbol, window_days)
        ):
            self.start_intraday_fetch(symbol, window_days=window_days)
        if symbol_history is None or symbol_history.empty:
            self._set_html_or_text(
                self.intraday_chart_view,
                self._generate_message_html(
                    "Loading intraday data",
                    f"Fetching {window_days} days of 5-minute data for {symbol} in the background.",
                ),
                f"Fetching {window_days} days of 5-minute data for {symbol} in the background.",
            )
            self.intraday_status_label.setText(
                f"Fetching {symbol} intraday data in the background..."
            )
            return

        chart_history = resample_intraday_bars(symbol_history, interval)
        if chart_history.empty:
            self.intraday_status_label.setText(
                f"No {interval} intraday bars available for {symbol}."
            )
            return

        latest_price = float(chart_history["Close"].iloc[-1])
        self.update_trade_prices_from_latest(symbol, latest_price)
        watchlist_item = self.watchlist.get(symbol)
        target_price = (
            watchlist_item.breakout_price if watchlist_item is not None else None
        )
        intraday_options = self._get_intraday_chart_options()
        drawings = self._build_combined_drawings(
            symbol, intraday_options.get("timeframe", "1H")
        )
        self._set_html_or_text(
            self.intraday_chart_view,
            self._generate_local_chart_html(
                symbol,
                chart_history,
                compact=False,
                options=intraday_options,
                target_price=target_price,
                drawings=drawings,
                interaction_settings=self.__dict__.get("settings", {}),
            ),
            f"{symbol} intraday {interval} chart loaded. Latest price: {latest_price:.2f}",
        )
        latest_time = pd.Timestamp(chart_history.index[-1]).strftime("%Y-%m-%d %H:%M")
        source_note = f"{cache_source or 'legacy'} cache"
        if needs_backfill:
            source_note += "; background refresh running"
        if hasattr(self, "live_data_source_label") and cache_source:
            self.live_data_source_label.setText(
                format_intraday_source_label(cache_source)
            )
        self.intraday_status_label.setText(
            f"{symbol} {interval} {window_days}D intraday chart loaded from {source_note}. "
            f"Latest {latest_price:.2f} at {latest_time}."
        )

    def _get_intraday_window_days(self) -> int:
        if not hasattr(self, "intraday_window_combo"):
            return 7
        text = self.intraday_window_combo.currentText().strip().upper().replace("D", "")
        try:
            return max(1, min(7, int(text)))
        except ValueError:
            return 7

    def _get_tradingview_window_days(self) -> int:
        if not hasattr(self, "tradingview_window_combo"):
            return 7
        text = (
            self.tradingview_window_combo.currentText().strip().upper().replace("D", "")
        )
        try:
            return max(1, min(7, int(text)))
        except ValueError:
            return 7

    @staticmethod
    def _tradingview_max_history_bars(
        timeframe: str, window_days: int = 7
    ) -> Optional[int]:
        timeframe = timeframe.strip().upper()
        if timeframe == "5M":
            return max(100, min(2000, max(1, int(window_days or 7)) * 120))
        if timeframe == "1H":
            return 1000
        return 260

    def _get_intraday_chart_options(self) -> dict:
        interval = (
            self.intraday_interval_combo.currentText().strip().upper()
            if hasattr(self, "intraday_interval_combo")
            else "1H"
        )
        interval_timeframe = self._normalize_drawing_timeframe(interval)
        if not interval_timeframe:
            interval_timeframe = "1H"
        return {
            "timeframe": interval_timeframe,
            "show_volume": self.intraday_show_volume_checkbox.isChecked(),
            "show_rs": False,
            "show_ema": self.intraday_show_ema_checkbox.isChecked(),
            "show_adr": False,
            "show_growth_1m": False,
            "show_growth_3m": False,
            "show_growth_6m": False,
            "max_history_bars": 2000,
            "visible_bars": 2000,
            "intraday_chart": True,
        }

    def _intraday_fetch_key(self, symbol: str, window_days: int) -> str:
        return f"{symbol.strip().upper()}:{max(1, min(7, int(window_days or 7)))}"

    def _can_start_intraday_fetch(
        self, symbol: str, window_days: int, cooldown_seconds: int = 300
    ) -> bool:
        key = self._intraday_fetch_key(symbol, window_days)
        last_attempt = self.intraday_fetch_attempts.get(key)
        if last_attempt is None:
            return True
        return (_utcnow_naive() - last_attempt).total_seconds() >= cooldown_seconds

    def _load_cached_intraday_5m(
        self, symbol: str, window_days: int = 7
    ) -> Optional[pd.DataFrame]:
        bars, _source = self._load_cached_intraday_5m_with_source(
            symbol, window_days=window_days
        )
        return bars

    def _load_cached_intraday_5m_with_source(
        self, symbol: str, window_days: int = 7
    ) -> tuple[pd.DataFrame, str]:
        window_days = max(1, min(7, int(window_days or 7)))
        since = _utcnow_naive() - dt.timedelta(days=window_days)
        if self.db_enabled and self.db_engine is not None:
            try:
                bars, source = load_best_intraday_history(
                    symbol, self.db_engine, interval="5m", since=since
                )
                self.latest_intraday_sources[(symbol.strip().upper(), "5m")] = source
                return bars, source
            except Exception:
                return pd.DataFrame(), "none"
        return pd.DataFrame(), "none"

    def start_intraday_fetch(self, symbol: str, window_days: int = 7) -> bool:
        symbol = symbol.strip().upper()
        if not symbol:
            return False
        if not self.db_enabled or self.db_engine is None:
            if hasattr(self, "intraday_status_label"):
                self.intraday_status_label.setText(
                    "Intraday cache requires MySQL; no background fetch was started."
                )
            return False
        if (
            self.intraday_fetch_worker is not None
            and self.intraday_fetch_worker.isRunning()
        ):
            self.append_log(f"Intraday fetch for {symbol} already running, skipping.")
            return False
        engine = self.db_engine
        profile = self._selected_dashboard_kis_profile() or {}
        self.intraday_fetch_attempts[self._intraday_fetch_key(symbol, window_days)] = (
            _utcnow_naive()
        )
        self.append_log(
            f"Starting intraday fetch for {symbol} ({window_days}d window)..."
        )
        self.intraday_fetch_worker = IntradayFetchWorker(
            symbol,
            engine,
            window_days=window_days,
            fetch_days=None,
            environment=profile.get("environment", "PROD"),
            account_no=profile.get("account_no", ""),
            exchange="NASD",
            allow_fallback=True,
        )
        self.intraday_fetch_worker.finished_fetch.connect(
            self._on_intraday_fetch_finished
        )
        self.intraday_fetch_worker.provider_warning.connect(
            self._log_intraday_provider_warning
        )
        self.intraday_fetch_worker.error_occurred.connect(self._on_intraday_fetch_error)
        self._track_worker("intraday_fetch_worker", self.intraday_fetch_worker)
        self.intraday_fetch_worker.start()
        return True

    def _on_intraday_fetch_finished(
        self, symbol: str, fetched, window_days: int, source: str
    ) -> None:
        source_text = "yfinance fallback" if source == "yfinance" else source
        self.latest_intraday_sources[(symbol.strip().upper(), "5m")] = source
        latest_ts = ""
        try:
            if hasattr(fetched, "index") and not fetched.empty:
                latest_ts = f" | latest bar: {pd.Timestamp(fetched.index.max())}"
        except Exception:
            pass
        self.append_log(
            f"Updated intraday cache for {symbol} from {source_text}.{latest_ts}"
        )
        if hasattr(self, "live_data_source_label"):
            self.live_data_source_label.setText(format_intraday_source_label(source))
        if self.intraday_symbol_combo.currentText().strip().upper() == symbol:
            self.plot_intraday_watchlist_symbol(allow_fetch=False)
        if (
            hasattr(self, "symbol_input")
            and self.symbol_input.text().strip().upper() == symbol
        ):
            self.refresh_orb_trade_plan_table()
        if hasattr(self, "refresh_execution_queue"):
            env = (
                self.watchlist_env_combo.currentText()
                if hasattr(self, "watchlist_env_combo")
                else "PROD"
            )
            # Only this symbol's cached intraday data actually changed, so only
            # recheck this one execution-queue row (if it's even queued) instead
            # of every queued symbol. refresh_execution_queue(env, symbols=None)
            # walks the WHOLE queue -- per item that's two DB reads plus a full
            # order-ledger reload from disk -- and this handler fires on every
            # single-symbol intraday fetch (watchlist add, Activate, chart
            # refresh), so that used to run synchronously on the UI thread far
            # more often than the data actually warranted.
            self.refresh_execution_queue(env, show_log=False, symbols=[symbol])
        if (
            hasattr(self, "tradingview_timeframe_combo")
            and hasattr(self, "tradingview_widget")
            and hasattr(self, "tabs")
            and self.tabs.currentWidget() is self.tradingview_widget
        ):
            timeframe = self.tradingview_timeframe_combo.currentText().strip().upper()
            if timeframe in ("5M", "1H"):
                active = (
                    self.tradingview_symbol_combo.currentText().strip().upper()
                    if hasattr(self, "tradingview_symbol_combo")
                    else ""
                )
                if active == symbol.strip().upper():
                    self.load_tradingview_chart(force=True)

    def _on_intraday_fetch_error(self, symbol: str, message: str) -> None:
        self.append_log(f"Intraday fetch failed for {symbol}: {message}")
        if hasattr(self, "intraday_status_label"):
            self.intraday_status_label.setText(
                f"Intraday fetch failed for {symbol}: {message}"
            )

    def refresh_watchlist_intraday_cache(
        self,
        checked: bool = False,
        show_messages: bool = True,
        triggered_by_live: bool = False,
        source: str = "",
        symbols: Optional[List[str]] = None,
        purpose: str = "watchlist",
    ) -> None:
        if symbols is None:
            symbols = [item.symbol for item in self.watchlist.items]
        symbols = list(
            dict.fromkeys(
                str(symbol or "").strip().upper()
                for symbol in symbols
                if str(symbol or "").strip()
            )
        )
        if not symbols:
            if show_messages:
                QMessageBox.information(
                    self, "No watchlist", "Add symbols to the watchlist first."
                )
            if triggered_by_live and hasattr(self, "live_data_status_label"):
                self.live_data_status_label.setText("Live data: no watchlist symbols")
            return
        if not self.db_enabled or self.db_engine is None:
            message = (
                "Intraday watchlist refresh requires the MySQL cache; no data was fetched."
            )
            if not triggered_by_live or not getattr(
                self, "_intraday_cache_unavailable_notice_logged", False
            ):
                self.append_log(message)
                self._intraday_cache_unavailable_notice_logged = True
            if show_messages:
                QMessageBox.warning(self, "Intraday cache unavailable", message)
            if triggered_by_live and hasattr(self, "live_data_status_label"):
                self.live_data_status_label.setText("Live data: MySQL cache unavailable")
            return
        if (
            self.intraday_bulk_worker is not None
            and self.intraday_bulk_worker.isRunning()
        ):
            if show_messages:
                QMessageBox.information(
                    self,
                    "Intraday refresh running",
                    "Watchlist intraday refresh is already running.",
                )
            if triggered_by_live and hasattr(self, "live_data_status_label"):
                self.live_data_status_label.setText(
                    "Live data: refresh already running"
                )
            return

        engine = self.db_engine
        self.intraday_bulk_purpose = str(purpose or "watchlist")
        if self.intraday_bulk_purpose == "buyboard_orb":
            self._buyboard_orb_refresh_symbols = tuple(symbols)
        if hasattr(self, "refresh_watchlist_orb_button"):
            self.refresh_watchlist_orb_button.setEnabled(False)
        self.refresh_intraday_button.setEnabled(False)
        log_source = source or (
            "live auto refresh" if triggered_by_live else "manual refresh"
        )
        self.append_log(
            f"Starting 5-minute intraday {log_source} for {len(symbols)} watchlist symbols."
        )
        profile = self._selected_dashboard_kis_profile() or {}
        self.intraday_bulk_worker = IntradayBulkFetchWorker(
            symbols,
            engine,
            window_days=7,
            environment=profile.get("environment", "PROD"),
            account_no=profile.get("account_no", ""),
            exchange="NASD",
            # A chart/watchlist may use a clearly labelled fallback. An
            # executable Kanban ORB must be built from KIS data only.
            allow_fallback=self.intraday_bulk_purpose != "buyboard_orb",
        )
        self.intraday_bulk_worker.progress.connect(self._on_intraday_bulk_progress)
        self.intraday_bulk_worker.provider_warning.connect(
            self._log_intraday_provider_warning
        )
        self.intraday_bulk_worker.finished_bulk.connect(self._on_intraday_bulk_finished)
        self._track_worker("intraday_bulk_worker", self.intraday_bulk_worker)
        self.intraday_bulk_worker.start()

    def _on_intraday_bulk_progress(self, symbol: str, index: int, total: int) -> None:
        self.progress_label.setText(f"Intraday {index}/{total}: {symbol}")

    def _on_intraday_bulk_finished(self, updated: list, failed: list) -> None:
        purpose = str(getattr(self, "intraday_bulk_purpose", "watchlist") or "watchlist")
        if purpose == "scanner_orb":
            self.intraday_bulk_purpose = "watchlist"
            self.append_log(
                f"Scanner ORB phase intraday fetch complete: {len(updated)} updated, {len(failed)} failed."
            )
            if failed:
                self.append_log("Scanner ORB fetch failures: " + "; ".join(failed[:5]))
            self._score_scanner_results_by_orb()
            selected_source = self.pending_scanner_orb_source
            self.pending_scanner_orb_source = None
            self._finish_scanner_after_orb_phase(selected_source)
            return

        self.refresh_intraday_button.setEnabled(True)
        if hasattr(self, "refresh_watchlist_orb_button"):
            self.refresh_watchlist_orb_button.setEnabled(True)
        self.progress_label.setText("Intraday refresh complete.")
        self.append_log(
            f"Intraday refresh complete: {len(updated)} updated, {len(failed)} failed."
        )
        if failed:
            self.append_log("Intraday failures: " + "; ".join(failed[:5]))
        if getattr(self, "_refresh_orb_after_intraday_bulk", False):
            self._refresh_orb_after_intraday_bulk = False
            self.refresh_all_watchlist_orb_statuses()
        if hasattr(self, "refresh_execution_queue"):
            if purpose == "buyboard_orb":
                env = "PROD"
                requested_symbols = list(
                    self.__dict__.pop("_buyboard_orb_refresh_symbols", ()) or ()
                )
                refreshed_symbols = {
                    str(symbol or "").strip().upper() for symbol in updated
                }
                # Never stamp an old cached plan as freshly observed when its
                # KIS fetch failed. Leaving that queue row untouched lets the
                # ORB bridge surface DATA UNAVAILABLE instead of reusing it.
                queue_symbols = [
                    symbol
                    for symbol in requested_symbols
                    if symbol in refreshed_symbols
                ]
                # Position cards may not have an execution-queue row.  Cache
                # their freshly fetched KIS close directly so Current/P&L and
                # stop-distance display remain live in read-only recovery mode.
                loader = getattr(self, "_load_cached_intraday_interval", None)
                latest_prices = self.__dict__.setdefault(
                    "latest_intraday_prices", {}
                )
                if callable(loader):
                    for symbol in queue_symbols:
                        current_price = 0.0
                        for interval in ("1m", "5m"):
                            try:
                                frame = loader(symbol, interval, window_days=7)
                                if frame is None or frame.empty or "Close" not in frame:
                                    continue
                                session_filter = getattr(
                                    self, "_latest_intraday_session", None
                                )
                                if callable(session_filter):
                                    frame = session_filter(frame)
                                if frame is not None and not frame.empty:
                                    current_price = float(
                                        frame.sort_index()["Close"].dropna().iloc[-1]
                                    )
                            except Exception:
                                continue
                            if current_price > 0:
                                break
                        if current_price > 0:
                            latest_prices[symbol] = current_price
            else:
                env = (
                    self.watchlist_env_combo.currentText()
                    if hasattr(self, "watchlist_env_combo")
                    else "PROD"
                )
                queue_symbols = None
            self.refresh_execution_queue(
                env,
                show_log=False,
                symbols=queue_symbols,
                create_missing=False,
            )
            if hasattr(self, "_auto_replace_working_entry_queue_items"):
                self._auto_replace_working_entry_queue_items(env)
            if hasattr(self, "_auto_submit_execute_ready_queue_items"):
                self._auto_submit_execute_ready_queue_items(env)
            if purpose == "buyboard_orb" and hasattr(
                self, "_refresh_buyboard_live_metrics"
            ):
                self._refresh_buyboard_live_metrics()
        self.intraday_bulk_purpose = "watchlist"
        if hasattr(self, "live_data_checkbox") and self.live_data_checkbox.isChecked():
            status = f"Live data: updated {len(updated)}, failed {len(failed)}"
            if not self._is_us_regular_market_open():
                status += "; waiting for U.S. market hours"
            self.live_data_status_label.setText(status)
        if (
            hasattr(self, "intraday_symbol_combo")
            and self.tabs.currentWidget() is self.intraday_charts_widget
        ):
            self.plot_intraday_watchlist_symbol()
        if (
            hasattr(self, "tradingview_timeframe_combo")
            and hasattr(self, "tradingview_widget")
            and self.tabs.currentWidget() is self.tradingview_widget
        ):
            timeframe = self.tradingview_timeframe_combo.currentText().strip().upper()
            if timeframe in ("5M", "1H"):
                self.load_tradingview_chart(force=True)

    @staticmethod
    def _intraday_cache_needs_backfill(
        cached: pd.DataFrame, since: dt.datetime
    ) -> bool:
        return intraday_cache_needs_backfill(cached, since)

    def prefetch_intraday_cache_for_symbol(self, symbol: str) -> None:
        symbol = symbol.strip().upper()
        if not symbol:
            return
        try:
            if self.start_intraday_fetch(symbol, window_days=7):
                self.append_log(f"Queued 7-day intraday cache refresh for {symbol}.")
        except Exception as exc:
            self.append_log(f"Intraday prefetch failed for {symbol}: {exc}")

    def delete_intraday_cache_for_symbol(self, symbol: str) -> None:
        if not self.db_enabled or self.db_engine is None:
            return
        try:
            deleted = delete_intraday_history_for_symbol(self.db_engine, symbol)
            if deleted:
                self.append_log(f"Removed {deleted} intraday cache rows for {symbol}.")
        except Exception as exc:
            self.append_log(f"Intraday cache delete failed for {symbol}: {exc}")

    @staticmethod
    def _filter_symbols_by_prefix(symbols, prefix: str) -> List[str]:
        prefix = prefix.strip().upper()
        return [
            symbol
            for symbol in sorted(
                {str(item).strip().upper() for item in symbols if str(item).strip()}
            )
            if symbol.startswith(prefix)
        ]
