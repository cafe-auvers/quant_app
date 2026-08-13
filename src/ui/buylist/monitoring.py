"""P2 buylist monitoring submixin."""
from src.ui.buylist._shared import *  # noqa: F401,F403

class BuylistMonitoringMixin:
    def _toggle_buylist_monitor(self, env: str) -> None:
        """Toggle the production monitor timer."""
        active_attr = f"_buylist_{env.lower()}_monitor_active"
        timer_attr = f"buylist_{env.lower()}_monitor_timer"
        lbl_attr = f"buylist_{env.lower()}_monitor_status_label"
        btn_name = f"buylistMonitorToggle_{env}"

        if not hasattr(self, timer_attr):
            return

        timer: QTimer = getattr(self, timer_attr)
        is_active: bool = getattr(self, active_attr, False)
        lbl: QLabel = getattr(self, lbl_attr, None)
        btn = self.findChild(QPushButton, btn_name)

        if is_active:
            timer.stop()
            setattr(self, active_attr, False)
            if lbl:
                lbl.setText("Monitor: OFF")
                lbl.setStyleSheet("color: #888;")
            if btn:
                btn.setText("Start Monitor")
            self.append_log(f"[Buylist/{env}] Monitor stopped.")
        else:
            timer.start(60000)
            setattr(self, active_attr, True)
            if lbl:
                lbl.setText("Monitor: ON (60s)")
                lbl.setStyleSheet("color: #4CAF50; font-weight: bold;")
            if btn:
                btn.setText("Stop Monitor")
            self.append_log(
                f"[Buylist/{env}] Monitor started — checking every 60 seconds."
            )
            self._run_buylist_monitor_cycle(env)  # run immediately

    def _ensure_buylist_monitor_running(self, env: str) -> bool:
        """Start one environment's buylist monitor if it is currently off."""
        active_attr = f"_buylist_{env.lower()}_monitor_active"
        if getattr(self, active_attr, False):
            return False
        if not hasattr(self, "_toggle_buylist_monitor"):
            return False
        self._toggle_buylist_monitor(env)
        return bool(getattr(self, active_attr, False))

    def _auto_submit_execute_ready_queue_items(self, env: str) -> None:
        """Auto-submit EXECUTE_READY execution-queue items when the monitor is active for env."""
        active_attr = f"_buylist_{env.lower()}_monitor_active"
        if not getattr(self, active_attr, False):
            return
        if not hasattr(self, "buylist_manager"):
            return
        items = [it for it in self.buylist_manager.items if it.environment == env]
        for it in items:
            if it.monitoring_status != "EXECUTE_READY":
                continue
            if not self._is_pre_entry_execution_queue_buylist_item(it):
                continue
            if not getattr(it, "orb_monitor_enabled", False):
                continue
            if getattr(it, "_buy_order_pending", False):
                continue
            auto_order_blocked = self._buylist_auto_order_blocked(it)
            if auto_order_blocked:
                if not getattr(it, "_auto_order_block_notice_logged", False):
                    it._auto_order_block_notice_logged = True
                    self.append_log(
                        f"[Buylist/{env}] {it.symbol} EXECUTE_READY but auto order is blocked: "
                        f"{getattr(it, 'auto_order_block_reason', '')}"
                    )
                continue
            queue_item = self._queue_item_for_buylist_item(it)
            candidate = (
                getattr(queue_item, "selected_candidate", None)
                if queue_item is not None
                else None
            )
            if candidate is None:
                self.append_log(
                    f"[Buylist/{env}] {it.symbol} EXECUTE_READY but no selected candidate — skipping auto-buy."
                )
                continue
            entry_trigger = float(getattr(candidate, "entry_trigger", 0.0) or 0.0)
            shares = int(getattr(candidate, "shares", 0) or 0)
            if shares < 1 or entry_trigger <= 0:
                self.append_log(
                    f"[Buylist/{env}] {it.symbol} EXECUTE_READY but invalid order "
                    f"(shares={shares}, trigger={entry_trigger:.2f}) — skipping auto-buy."
                )
                continue
            current_price = self.latest_intraday_prices.get(it.symbol, 0.0)
            mgr = self._ensure_execution_queue_manager()
            if mgr is not None:
                mgr.mark_order_submitted(
                    it.symbol, order_status="PENDING", environment=env
                )
            queue_status = (
                self._execution_queue_status_for_buylist_item(it) or "ORDER_PENDING"
            )
            it.monitoring_status = queue_status
            it.status = queue_status
            it._buy_order_pending = True
            self._save_buylist_state()
            self._save_execution_queue_state()
            self.append_log(
                f"[Buylist/{env}] {it.symbol} EXECUTE_READY — auto-submitting BUY "
                f"{shares} shares @ ${entry_trigger:.2f} (current ${current_price:.2f})"
            )
            self._submit_kis_buy_order(it, quantity=shares, order_price=entry_trigger)

    def _deactivate_pre_entry_orb_monitoring(self) -> None:
        """Reset all pre-entry ORB/queue monitoring to WATCHING at market close.

        BOUGHT positions (and any order mid-flight) are left untouched — they still need
        stop-loss monitoring next session. Today's ORB ranges (1m/5m/30m) are meaningless
        tomorrow since a new session opens a new range, so pre-entry items are fully reset
        rather than just paused, avoiding a stale window/candidate carried into the open.
        """
        if not hasattr(self, "buylist_manager"):
            return

        from src.core.execution_queue import ExecutionQueueStatus

        manager = self.__dict__.get("execution_queue_manager")
        pre_entry_watching = {
            ExecutionQueueStatus.ORB_FORMING.value,
            ExecutionQueueStatus.WAITING_BREAKOUT.value,
            ExecutionQueueStatus.ARMED.value,
            ExecutionQueueStatus.EXECUTE_READY.value,
        }
        submitted_entry_statuses = {
            ExecutionQueueStatus.ORDER_PENDING.value,
            ExecutionQueueStatus.ORDER_SUBMITTED.value,
            ExecutionQueueStatus.UNKNOWN_SUBMISSION_STATE.value,
        }
        reset_symbols: List[Tuple[str, str]] = []
        cancel_pending_symbols: List[Tuple[str, str]] = []

        for item in self.buylist_manager.items:
            status = str(getattr(item, "monitoring_status", "") or "").upper()
            is_queue = self._is_execution_queue_buylist_item(item)

            if is_queue:
                if status in submitted_entry_statuses:
                    symbol = str(getattr(item, "symbol", "") or "").upper()
                    environment = str(
                        getattr(item, "environment", "") or "PROD"
                    ).upper()
                    if status == ExecutionQueueStatus.UNKNOWN_SUBMISSION_STATE.value:
                        self.append_log(
                            f"[Buylist/{environment}] Market close: {symbol} has UNKNOWN_SUBMISSION_STATE; "
                            "left unresolved for broker reconciliation."
                        )
                        cancel_pending_symbols.append((symbol, environment))
                        continue
                    self._request_eod_entry_buy_cancellation(item, environment)
                    if self._open_broker_orders_for_buylist_item(
                        item,
                        environment,
                        side=OrderSide.BUY,
                        intent=OrderIntent.ENTRY,
                    ):
                        cancel_pending_symbols.append((symbol, environment))
                        continue
                    status = str(getattr(item, "monitoring_status", "") or "").upper()
                if status not in pre_entry_watching and status not in {
                    "ACTIVE",
                    ExecutionQueueStatus.REJECTED.value,
                    ExecutionQueueStatus.EXPIRED.value,
                }:
                    continue
            elif status != "ACTIVE":
                continue

            symbol = str(getattr(item, "symbol", "") or "").upper()
            environment = str(getattr(item, "environment", "") or "PROD").upper()

            if is_queue:
                item.orb_monitor_enabled = False
                item._buy_order_pending = False
                item._auto_order_block_notice_logged = False
                item._orb_queue_required_notice_logged = False
                self._clear_buylist_auto_order_block(item)

                queue_item = (
                    manager.get_item(symbol, environment)
                    if manager is not None
                    else None
                )
                if queue_item is not None:
                    queue_item.locked = False
                    queue_item.locked_reason = None
                    queue_item.manual_window_lock = False
                    queue_item.candidates = {}
                    queue_item.selected_window = None
                    queue_item.selected_candidate = None
                    queue_item.order_status = None
                    queue_item.order_id = None
                    queue_item.warnings = []
                    queue_item.status = ExecutionQueueStatus.WATCHING

            item.monitoring_status = "WATCHING"
            item.status = "WATCHING"
            reset_symbols.append((symbol, environment))

        if cancel_pending_symbols:
            symbols_text = ", ".join(
                f"{sym}/{env}" for sym, env in cancel_pending_symbols
            )
            self.append_log(
                f"[Buylist] Market close: entry BUY cancellation/reconciliation still pending for: {symbols_text}"
            )

        if not reset_symbols:
            return

        self._save_buylist_state()
        if manager is not None:
            self._save_execution_queue_state()
        if hasattr(self, "populate_buylist_dashboard"):
            self.populate_buylist_dashboard()
        symbols_text = ", ".join(f"{sym}/{env}" for sym, env in reset_symbols)
        self.append_log(
            f"[Buylist] Market closed — deactivated pre-entry ORB monitoring for: {symbols_text}"
        )

    def _run_buylist_monitor_cycle(self, env: str) -> None:
        """Check ACTIVE/BOUGHT items for one environment and fire orders as needed."""
        if not hasattr(self, "buylist_manager"):
            return

        items = [it for it in self.buylist_manager.items if it.environment == env]
        self._restore_monitorable_buylist_error_positions(items, env)
        active_items = [
            it for it in items if it.monitoring_status in ("ACTIVE", "BOUGHT")
        ]
        stop_reprice_items = [
            it
            for it in items
            if str(getattr(it, "monitoring_status", "") or "").upper()
            == "SELL_SUBMITTED"
        ]

        # Execution queue items whose trigger hasn't fired yet
        _skip_statuses = {
            "BOUGHT",
            "BUY_SUBMITTED",
            "BUY_PARTIAL",
            "SELL_SUBMITTED",
            "PARTIAL_EXIT_SUBMITTED",
            "SELL_RESERVED",
            "PARTIAL_EXIT_RESERVED",
            "SOLD",
            "ORDER_SUBMITTED",
            "ORDER_PENDING",
        }
        queue_watching_items = [
            it
            for it in items
            if self._is_pre_entry_execution_queue_buylist_item(it)
            and it.monitoring_status not in _skip_statuses
            and not getattr(it, "_buy_order_pending", False)
            and getattr(it, "orb_monitor_enabled", False)
        ]

        if not active_items and not queue_watching_items and not stop_reprice_items:
            return

        bought_count = sum(1 for it in items if it.monitoring_status == "BOUGHT")

        # Trigger an async KIS-first intraday refresh for execution-queue items.
        # When the worker finishes, _on_intraday_bulk_finished calls refresh_execution_queue
        # and then _auto_submit_execute_ready_queue_items, which fires any EXECUTE_READY orders.
        if queue_watching_items and hasattr(self, "refresh_watchlist_intraday_cache"):
            worker = getattr(self, "intraday_bulk_worker", None)
            if worker is None or not worker.isRunning():
                self.refresh_watchlist_intraday_cache(
                    show_messages=False,
                    triggered_by_live=True,
                    source="buylist monitor",
                )

        for item in stop_reprice_items:
            self._buylist_refresh_item_data(item)
            current_price = self.latest_intraday_prices.get(item.symbol, 0.0)
            if current_price > 0:
                self._maybe_reprice_stop_loss_sell(item, env, current_price)

        for item in active_items:
            self._buylist_refresh_item_data(item)
            try:
                current_price = float(self.latest_intraday_prices.get(item.symbol, 0.0))
            except (TypeError, ValueError):
                current_price = 0.0
            if not math.isfinite(current_price) or current_price <= 0:
                continue

            if item.monitoring_status == "ACTIVE":
                if self._is_orb_buylist_item(item):
                    if not getattr(item, "_orb_queue_required_notice_logged", False):
                        item._orb_queue_required_notice_logged = True
                        self.append_log(
                            f"[Buylist/{env}] {item.symbol} is an ORB entry; skipping legacy ACTIVE auto-buy. "
                            "Use the execution queue Review Order and Submit Buy flow."
                        )
                    continue

                # Compute entry_trigger: max(ORB high, breakout_price * (1+buffer))
                try:
                    bp = float(getattr(item, "breakout_price", None) or 0.0)
                    buf = float(getattr(item, "buffer_pct", 0.001) or 0.0)
                    item_entry_price = float(getattr(item, "entry_price", 0.0) or 0.0)
                except (TypeError, ValueError):
                    bp = buf = item_entry_price = 0.0
                if not math.isfinite(bp) or bp < 0:
                    bp = 0.0
                if not math.isfinite(buf) or buf < 0:
                    buf = 0.0
                if not math.isfinite(item_entry_price) or item_entry_price <= 0:
                    if not getattr(item, "_invalid_entry_notice_logged", False):
                        item._invalid_entry_notice_logged = True
                        self.append_log(
                            f"[Buylist/{env}] {item.symbol} has an invalid entry trigger; "
                            "monitoring will not submit an order until the plan is corrected."
                        )
                    continue
                breakout_trigger = bp * (1 + buf) if bp > 0 else 0.0
                entry_trigger = (
                    max(item_entry_price, breakout_trigger)
                    if breakout_trigger > 0
                    else item_entry_price
                )
                auto_order_blocked = self._buylist_auto_order_blocked(item)

                # Chase guard: if price has already run â‰¥2% above the trigger the setup is
                # stale (entered from a previous session or the breakout already happened).
                # Do not auto-buy; log once per cycle.
                MAX_CHASE_PCT = 0.02
                if current_price > entry_trigger * (1 + MAX_CHASE_PCT):
                    overshoot_pct = (current_price / entry_trigger - 1) * 100
                    self.append_log(
                        f"[Buylist/{env}] {item.symbol} price ${current_price:.2f} is "
                        f"+{overshoot_pct:.1f}% above trigger ${entry_trigger:.2f} — "
                        f"setup stale, skipping auto-buy."
                    )
                elif (
                    bought_count < 30
                    and current_price >= entry_trigger
                    and not auto_order_blocked
                    and not getattr(item, "_buy_order_pending", False)
                ):
                    item._buy_order_pending = True
                    if bp > 0:
                        self.append_log(
                            f"[Buylist/{env}] {item.symbol} confirmed breakout — "
                            f"trigger ${entry_trigger:.2f} (ORB ${item.entry_price:.2f}, "
                            f"daily breakout ${bp:.2f}, current ${current_price:.2f}) — submitting BUY."
                        )
                    else:
                        self.append_log(
                            f"[Buylist/{env}] {item.symbol} hit ORB entry ${entry_trigger:.2f} "
                            f"(current ${current_price:.2f}) — submitting BUY order."
                        )
                    # Order at current_price so avg_cost reflects what we'd actually pay;
                    # entry_trigger is the condition, not necessarily the fill price.
                    self._submit_kis_buy_order(item, order_price=current_price)
                    bought_count += 1
                elif (
                    current_price >= entry_trigger
                    and auto_order_blocked
                    and not getattr(item, "_auto_order_block_notice_logged", False)
                ):
                    item._auto_order_block_notice_logged = True
                    self.append_log(
                        f"[Buylist/{env}] Trigger met for {item.symbol}, but auto KIS order is blocked: "
                        f"{getattr(item, 'auto_order_block_reason', '')}"
                    )
                elif (
                    bp > 0
                    and current_price > item.entry_price
                    and current_price < breakout_trigger
                ):
                    self.append_log(
                        f"[Buylist/{env}] {item.symbol} above ORB ${item.entry_price:.2f} "
                        f"but below breakout trigger ${breakout_trigger:.2f} (${current_price:.2f}) — waiting."
                    )

            elif item.monitoring_status == "BOUGHT":
                auto_order_blocked = self._buylist_auto_order_blocked(item)
                exit_order_pending = getattr(item, "_exit_order_pending", False)
                if (
                    item.stop_loss > 0
                    and current_price <= item.stop_loss
                    and not auto_order_blocked
                    and not getattr(item, "_stop_order_pending", False)
                ):
                    item._stop_order_pending = True
                    self.append_log(
                        f"[Buylist/{env}] STOP HIT — {item.symbol} ${current_price:.2f} "
                        f"<= stop ${item.stop_loss:.2f}. Submitting SELL ALL."
                    )
                    self._submit_kis_sell_order(
                        item,
                        item.shares_held,
                        reason="stop-loss",
                        order_price=self._stop_loss_sell_limit_price(current_price),
                    )
                    continue
                elif (
                    item.stop_loss > 0
                    and current_price <= item.stop_loss
                    and auto_order_blocked
                    and not getattr(item, "_auto_order_block_notice_logged", False)
                ):
                    item._auto_order_block_notice_logged = True
                    self.append_log(
                        f"[Buylist/{env}] STOP still hit for {item.symbol}, but auto KIS order is blocked: "
                        f"{getattr(item, 'auto_order_block_reason', '')}"
                    )
                    continue

                if auto_order_blocked or exit_order_pending:
                    continue

                days_held = self._buylist_days_held(item)
                alert_changed = self._update_manual_exit_alerts(item, env, days_held)
                if alert_changed:
                    self._save_buylist_state()
        self._populate_buylist_env_table(env)

    def _restore_monitorable_buylist_error_positions(self, items, env: str) -> None:
        """Keep held positions monitorable after a rejected exit order."""
        changed = False
        for item in items:
            if getattr(item, "monitoring_status", "") != "ERROR":
                continue
            try:
                shares_held = int(getattr(item, "shares_held", 0) or 0)
            except (TypeError, ValueError):
                shares_held = 0
            if shares_held <= 0:
                continue

            item.monitoring_status = "BOUGHT"
            item._stop_order_pending = False
            changed = True
            self.append_log(
                f"[Buylist/{env}] {item.symbol} restored from ERROR to BOUGHT "
                f"because {shares_held} shares are still marked held."
            )
        if changed:
            self._save_buylist_state()

    @staticmethod
    def _buylist_auto_order_blocked(item) -> bool:
        from src.ui.buylist.controller import BuylistController

        return BuylistController.auto_order_blocked(item)

    def _set_buylist_auto_order_block(self, item, reason: str) -> None:
        item.auto_order_block_reason = str(reason or "").strip()
        item._buy_order_pending = False
        item._stop_order_pending = False
        item._exit_order_pending = False
        item._auto_order_block_notice_logged = False

    def _clear_buylist_auto_order_block(self, item) -> None:
        if hasattr(item, "auto_order_block_reason"):
            item.auto_order_block_reason = ""
        item._auto_order_block_notice_logged = False

    @staticmethod
    def _set_attr_if_changed(item, attr: str, value) -> bool:
        from src.ui.buylist.controller import BuylistController

        return BuylistController.set_attr_if_changed(item, attr, value)

    def _update_manual_exit_alerts(self, item, env: str, days_held: int) -> bool:
        changed = False
        suggested_action = "Review manually; no automatic sell submitted."

        try:
            shares_held = int(getattr(item, "shares_held", 0) or 0)
        except (TypeError, ValueError):
            shares_held = 0

        partial_reason = ""
        if (
            shares_held > 0
            and 3 <= days_held <= 5
            and not getattr(item, "sell_half_done", False)
        ):
            partial_reason = (
                "Position is within 3-5 trading day partial-exit review window."
            )

        changed |= self._set_attr_if_changed(
            item, "partial_exit_review_alert", bool(partial_reason)
        )
        changed |= self._set_attr_if_changed(
            item, "partial_exit_review_reason", partial_reason
        )

        ema_reason = ""
        if shares_held > 0 and getattr(item, "sell_half_done", False):
            momentum_signal = self._momentum_exit_signal(item)
            if momentum_signal:
                ema_reason = f"Close below {momentum_signal} - review manual exit next market open."

        changed |= self._set_attr_if_changed(
            item, "ema_trailing_stop_alert", bool(ema_reason)
        )
        changed |= self._set_attr_if_changed(
            item, "ema_trailing_stop_reason", ema_reason
        )

        if partial_reason or ema_reason:
            changed |= self._set_attr_if_changed(
                item, "suggested_action", suggested_action
            )
        elif str(getattr(item, "suggested_action", "") or "") == suggested_action:
            changed |= self._set_attr_if_changed(item, "suggested_action", "")

        partial_notice_key = "_partial_exit_review_notice_reason"
        if partial_reason and getattr(item, partial_notice_key, "") != partial_reason:
            setattr(item, partial_notice_key, partial_reason)
            self.append_log(
                f"[Buylist/{env}] {item.symbol}: {partial_reason} {suggested_action}"
            )
        elif not partial_reason and getattr(item, partial_notice_key, ""):
            setattr(item, partial_notice_key, "")

        ema_notice_key = "_ema_trailing_stop_notice_reason"
        if ema_reason and getattr(item, ema_notice_key, "") != ema_reason:
            setattr(item, ema_notice_key, ema_reason)
            self.append_log(
                f"[Buylist/{env}] {item.symbol}: {ema_reason} {suggested_action}"
            )
        elif not ema_reason and getattr(item, ema_notice_key, ""):
            setattr(item, ema_notice_key, "")

        return changed

    def _buylist_days_held(self, item) -> int:
        buy_date = getattr(item, "buy_date", None)
        if not buy_date:
            return 0
        buy_day = self._buylist_market_session_date_from_value(buy_date)
        session_today = self._us_market_session_date()
        if buy_day is None or buy_day >= session_today:
            return 0
        if hasattr(self, "_nyse_holidays"):
            holidays: set = set()
            for year in range(buy_day.year, session_today.year + 1):
                holidays |= self._nyse_holidays(year)
            days = 0
            cursor = buy_day
            while cursor < session_today:
                if cursor.weekday() < 5 and cursor not in holidays:
                    days += 1
                cursor += dt.timedelta(days=1)
            return days
        return (session_today - buy_day).days

    @staticmethod
    def _us_market_session_date(now: Optional[dt.datetime] = None) -> dt.date:
        from src.ui.buylist.controller import BuylistController

        return BuylistController.market_session_date(now)

    @staticmethod
    def _buylist_market_session_date_from_value(value) -> Optional[dt.date]:
        from src.ui.buylist.controller import BuylistController

        return BuylistController.market_session_date_from_value(value)

    @staticmethod
    def _partial_exit_quantity(shares_held: int) -> int:
        from src.ui.buylist.controller import BuylistController

        return BuylistController.partial_exit_quantity(shares_held)

    @staticmethod
    def _momentum_exit_signal(item) -> str:
        from src.ui.buylist.controller import BuylistController

        return BuylistController.momentum_exit_signal(item)

    @staticmethod
    def _completed_daily_close_rows(
        daily_rows,
        now: Optional[dt.datetime] = None,
    ) -> List[Tuple[dt.date, float]]:
        from src.ui.buylist.controller import BuylistController

        return BuylistController.completed_daily_close_rows(daily_rows, now)

    def _buylist_refresh_item_data(self, item) -> None:
        """Fetch latest 30d daily closes and compute 10/20 EMA for a buylist item.

        Calls Yahoo Finance v8 chart API directly with a browser User-Agent — yfinance's
        internal requests get blocked by Yahoo when the default bot UA is used.
        Falls back to yfinance.Ticker.history() if the direct call fails.
        """
        import requests as _req

        symbol = item.symbol
        closes = None
        daily_rows = None

        # Primary: direct Yahoo Finance v8 chart API (browser UA bypasses Yahoo's bot block)
        try:
            session = getattr(self, "_yf_session", None)
            if session is None:
                session = _req.Session()
                session.headers["User-Agent"] = (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                )
                self._yf_session = session

            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
            r = session.get(
                url,
                params={"interval": "1d", "range": "30d", "events": "div,splits"},
                timeout=15,
            )
            r.raise_for_status()
            payload = r.json()
            result = payload["chart"]["result"][0]
            raw_closes = result["indicators"]["quote"][0]["close"]
            timestamps = result.get("timestamp", []) or []
            daily_rows = []
            for ts_value, close_value in zip(timestamps, raw_closes):
                if close_value is None:
                    continue
                session_date = (
                    dt.datetime.fromtimestamp(float(ts_value), dt.timezone.utc)
                    .astimezone(US_MARKET_ZONE)
                    .date()
                )
                daily_rows.append((session_date, float(close_value)))
            closes = (
                [close for _session_date, close in daily_rows]
                if daily_rows
                else [float(c) for c in raw_closes if c is not None]
            )
        except Exception as exc:
            self.append_log(
                f"[Buylist] Direct fetch failed for {symbol}: {exc} — trying yfinance fallback."
            )

        # Fallback: yfinance Ticker.history() with stderr suppressed
        if not closes:
            import io, sys, yfinance as yf

            _stderr = sys.stderr
            try:
                sys.stderr = io.StringIO()
                hist = yf.Ticker(symbol).history(period="30d", interval="1d")
                if not hist.empty:
                    daily_rows = []
                    for index_value, close_value in hist["Close"].dropna().items():
                        if hasattr(index_value, "to_pydatetime"):
                            timestamp = index_value.to_pydatetime()
                        else:
                            timestamp = index_value
                        if isinstance(timestamp, dt.datetime):
                            if timestamp.tzinfo is None:
                                timestamp = timestamp.replace(tzinfo=US_MARKET_ZONE)
                            session_date = timestamp.astimezone(US_MARKET_ZONE).date()
                        elif isinstance(timestamp, dt.date):
                            session_date = timestamp
                        else:
                            continue
                        daily_rows.append((session_date, float(close_value)))
                    closes = [close for _session_date, close in daily_rows]
            except Exception:
                pass
            finally:
                sys.stderr = _stderr

        if not closes:
            self.append_log(
                f"[Buylist] No price data for {symbol} — skipping this cycle."
            )
            return

        completed_rows = self._completed_daily_close_rows(daily_rows)
        if completed_rows:
            closes = [close for _session_date, close in completed_rows]

        latest_close = closes[-1]
        try:
            existing_live_price = float(
                self.latest_intraday_prices.get(symbol, 0.0) or 0.0
            )
        except (TypeError, ValueError):
            existing_live_price = 0.0
        if existing_live_price <= 0:
            self.latest_intraday_prices[symbol] = latest_close
        item._latest_daily_close = latest_close
        item._ema10 = self._compute_ema(closes, 10)
        item._ema20 = self._compute_ema(closes, 20)

    @staticmethod
    def _compute_ema(prices: list, period: int) -> float:
        """Compute exponential moving average."""
        from src.ui.buylist.controller import BuylistController

        return BuylistController.compute_ema(prices, period)

    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # Buylist Dashboard — KIS order submission
    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
