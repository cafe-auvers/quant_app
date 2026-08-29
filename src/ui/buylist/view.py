"""Buylist widgets, table rendering, and execution-queue persistence."""

import datetime as dt
import math
from typing import Any, List, Optional, Tuple

import pandas as pd
from PyQt5.QtCore import Qt, QThread, QTimer
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import (QAbstractItemView, QDialog, QHBoxLayout,
                             QHeaderView, QLabel, QMessageBox, QPushButton,
                             QSplitter, QTableWidget, QTableWidgetItem,
                             QVBoxLayout, QWidget)

from src.services.app_state import (
    archive_non_production_execution_queue_state, quarantine_rejected_records)
from src.services import buying_power_cache
from src.infrastructure.database.repositories.market_bars import \
    load_symbol_history_from_db
from src.services.intraday_data_service import load_best_intraday_history
from src.utils.intraday_helpers import utcnow_naive as _utcnow_naive
from src.utils.storage import load_json, save_json

from .constants import EXECUTION_QUEUE_FILE


class BuylistViewMixin:
    def _build_buylist_env_panel(self, env: str) -> QWidget:
        """Build the production environment panel for the Buy Dashboard."""
        if env != "PROD":
            raise ValueError("Buy Dashboard supports the PROD environment only")
        accent = "#b71c1c"
        label_text = "PROD  —  Live Trading"

        panel = QWidget()
        layout = QVBoxLayout()
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(3)

        # â”€â”€ Header + summary bar â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        header_layout = QHBoxLayout()

        env_label = QLabel(f"  {label_text}  ")
        env_label.setWordWrap(False)
        env_label.setStyleSheet(
            f"background-color: {accent}; color: white; font-weight: bold; "
            f"border-radius: 4px; padding: 2px 8px; white-space: nowrap;"
        )
        header_layout.addWidget(env_label)
        header_layout.addSpacing(12)

        positions_lbl = QLabel("Positions: 0 / 5")
        positions_lbl.setStyleSheet("font-weight: bold; color: #4CAF50;")
        capital_lbl = QLabel("Capital: 0.0%")
        pnl_lbl = QLabel("P&L: —")
        monitor_lbl = QLabel("Monitor: OFF")
        monitor_lbl.setStyleSheet("color: #888;")

        header_layout.addWidget(positions_lbl)
        header_layout.addSpacing(14)
        header_layout.addWidget(capital_lbl)
        header_layout.addSpacing(14)
        header_layout.addWidget(pnl_lbl)
        header_layout.addStretch()
        header_layout.addWidget(monitor_lbl)

        monitor_btn = QPushButton("Start Monitor")
        monitor_btn.setObjectName(f"buylistMonitorToggle_{env}")
        monitor_btn.setFixedWidth(120)
        monitor_btn.clicked.connect(
            lambda _=False, e=env: self._toggle_buylist_monitor(e)
        )
        header_layout.addWidget(monitor_btn)
        layout.addLayout(header_layout)

        self.buylist_prod_positions_label = positions_lbl
        self.buylist_prod_capital_label = capital_lbl
        self.buylist_prod_pnl_label = pnl_lbl
        self.buylist_prod_monitor_status_label = monitor_lbl

        # â”€â”€ Table â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        # Columns: Symbol | Name | Status | Monitor | Entry(ORB) | Breakout | Stop |
        #          Current | P&L% | Shares | Capital% | Risk% | Days | Alerts
        table = QTableWidget(0, 14)
        table.setHorizontalHeaderLabels(
            [
                "Symbol",
                "Name",
                "Status",
                "Monitor",
                "Entry",
                "Breakout",
                "Stop",
                "Current",
                "P&L%",
                "Shares",
                "Capital%",
                "Risk%",
                "Days",
                "Alerts",
            ]
        )
        table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        table.horizontalHeader().setStretchLastSection(True)
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.setAlternatingRowColors(True)
        table.verticalHeader().setVisible(False)
        for col, width in enumerate(
            [65, 120, 80, 62, 70, 72, 70, 70, 60, 55, 65, 52, 48, 170]
        ):
            table.setColumnWidth(col, width)
        layout.addWidget(table, 1)
        table.cellDoubleClicked.connect(
            lambda row, column, source=table: self._buylist_show_tradingview_chart(
                source, row, column
            )
        )

        self.buylist_prod_table = table

        # ——— Action buttons ——————————————————————————————————————
        btn_layout = QHBoxLayout()
        # min_width keeps multi-word labels from breaking across lines
        btns = [
            (
                "Activate",
                80,
                None,
                lambda _=False, e=env: self._buylist_activate_selected(e),
            ),
            (
                "Refresh Queue",
                110,
                None,
                lambda _=False, e=env: self.refresh_execution_queue(e),
            ),
            (
                "Review Order",
                105,
                None,
                lambda _=False, e=env: self._buylist_review_selected_queue_order(e),
            ),
            (
                f"Submit {env}",
                105,
                "background-color: #4CAF50; color: white;",
                lambda _=False, e=env: self._buylist_submit_selected_queue_order(e),
            ),
            (
                "Check Order Status",
                145,
                None,
                lambda _=False, e=env: self._buylist_check_order_status(e),
            ),
            (
                "Cancel Order",
                110,
                "background-color: #b71c1c; color: white;",
                lambda _=False, e=env: self._buylist_cancel_selected_order(e),
            ),
            (
                "Deactivate",
                90,
                None,
                lambda _=False, e=env: self._buylist_deactivate_selected(e),
            ),
            (
                "Breakeven",
                100,
                "background-color: #2196F3; color: white;",
                lambda _=False, e=env: self._buylist_move_to_breakeven_selected(e),
            ),
            (
                "Sell 1/3–1/2",
                110,
                "background-color: #FF9800; color: white;",
                lambda _=False, e=env: self._buylist_sell_half_selected(e),
            ),
            (
                "Sell All",
                80,
                "background-color: #f44336; color: white;",
                lambda _=False, e=env: self._buylist_sell_all_selected(e),
            ),
            (
                "Remove",
                75,
                None,
                lambda _=False, e=env: self._buylist_remove_selected(e),
            ),
            (
                "Refresh",
                75,
                None,
                lambda _=False, e=env: self.populate_buylist_dashboard(),
            ),
        ]
        for label, min_w, style, slot in btns:
            btn = QPushButton(label)
            btn.setMinimumWidth(min_w)
            if style:
                btn.setStyleSheet(style)
            btn.clicked.connect(slot)
            btn_layout.addWidget(btn)
        btn_layout.addStretch()
        layout.addLayout(btn_layout)

        panel.setLayout(layout)
        return panel

    def _buylist_show_tradingview_chart(
        self, table: QTableWidget, row: int, column: int
    ) -> None:
        """Buy Dashboard double-click: jump to the TradingView tab for the selected symbol."""
        symbol_item = table.item(row, 0)
        if symbol_item is None:
            return
        symbol = symbol_item.text().strip().upper()
        if not symbol:
            return
        if hasattr(self, "tradingview_symbol_combo"):
            self._set_tradingview_symbol(symbol)
        self.tabs.setCurrentWidget(self.tradingview_widget)
        self.load_tradingview_chart(force=True)

    def _build_buylist_tab(self) -> None:
        """Build the production Buylist Dashboard tab."""
        layout = QVBoxLayout()
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)

        splitter = QSplitter(Qt.Vertical)
        splitter.addWidget(self._build_buylist_env_panel("PROD"))
        layout.addWidget(splitter, 1)

        self.buylist_widget.setLayout(layout)

        # The live monitor never auto-starts.
        self.buylist_prod_monitor_timer = QTimer()
        self.buylist_prod_monitor_timer.timeout.connect(
            lambda: self._run_buylist_monitor_cycle("PROD")
        )
        self._buylist_prod_monitor_active = False

        self._buylist_order_workers: List[QThread] = []
        self._buylist_aux_workers: List[QThread] = []
        self.kis_order_worker = None
        self.broker_order_query_worker = None
        self.broker_order_cancel_worker = None

        self.populate_buylist_dashboard()

    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # Buylist Dashboard — populate & refresh
    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    def populate_buylist_dashboard(self) -> None:
        """Refresh the production buylist table."""
        self._populate_buylist_env_table("PROD")
        if hasattr(self, "_update_tradingview_queue_btn"):
            self._update_tradingview_queue_btn()
        if hasattr(self, "_update_intraday_queue_btn"):
            self._update_intraday_queue_btn()
        if hasattr(self, "_update_tradingview_activate_btn"):
            self._update_tradingview_activate_btn()
        if hasattr(self, "_update_intraday_activate_btn"):
            self._update_intraday_activate_btn()

    def _populate_buylist_env_table(self, env: str) -> None:
        """Populate the table for one environment and update its summary bar."""
        table_attr = f"buylist_{env.lower()}_table"
        if not hasattr(self, table_attr):
            return
        table: QTableWidget = getattr(self, table_attr)

        items = [it for it in self.buylist_manager.items if it.environment == env]
        table.setRowCount(0)

        bought_count = sum(1 for it in items if it.monitoring_status == "BOUGHT")
        total_capital = 0.0
        total_pnl_usd = 0.0

        active_attr = f"_buylist_{env.lower()}_monitor_active"
        monitor_running = self.__dict__.get(active_attr, False)

        for item in items:
            row = table.rowCount()
            table.insertRow(row)
            queue_display = self._queue_display_state_for_buylist_item(item)
            display_status = (
                queue_display.display_status
                if queue_display
                else self._buylist_dashboard_status(item)
            )
            is_queue_item = self._is_execution_queue_buylist_item(item)

            current_price = (
                queue_display.current_price
                if queue_display and queue_display.current_price > 0
                else self.latest_intraday_prices.get(item.symbol, 0.0)
            )
            pnl_pct = pnl_usd = 0.0
            if (
                item.monitoring_status == "BOUGHT"
                and item.avg_cost > 0
                and current_price > 0
            ):
                pnl_pct = (current_price - item.avg_cost) / item.avg_cost * 100.0
                pnl_usd = (current_price - item.avg_cost) * item.shares_held

            days_held = self._buylist_days_held(item) if item.buy_date else 0

            # For BOUGHT positions use the frozen position_percent snapshotted at fill time —
            # account_size_input can change (e.g. KIS balance load) and would give nonsense %.
            if (
                item.monitoring_status == "BOUGHT"
                and item.shares_held > 0
                and item.avg_cost > 0
            ):
                capital_pct = item.position_percent
            elif queue_display:
                capital_pct = queue_display.capital_percent
            else:
                account_size = (
                    self._parse_float(self.account_size_input, 0.0)
                    if hasattr(self, "account_size_input")
                    else 0.0
                )
                capital_pct = (
                    item.shares_held * item.avg_cost / account_size * 100.0
                    if account_size > 0 and item.avg_cost > 0
                    else item.position_percent
                )
            alerts = self._buylist_compute_alerts(
                item, current_price, days_held, queue_display
            )

            def _cell(text: str) -> QTableWidgetItem:
                c = QTableWidgetItem(str(text))
                c.setTextAlignment(Qt.AlignCenter)
                return c

            entry_price = (
                queue_display.entry_price if queue_display else item.entry_price
            )
            stop_loss = queue_display.stop_loss if queue_display else item.stop_loss
            bp_val = (
                queue_display.breakout_price
                if queue_display and queue_display.breakout_price is not None
                else getattr(item, "breakout_price", None)
            ) or 0.0
            bp_display = f"{bp_val:.2f}" if bp_val > 0 else "—"

            table.setItem(
                row, 0, _cell(queue_display.symbol if queue_display else item.symbol)
            )
            display_name = queue_display.name if queue_display else item.name
            table.setItem(row, 1, _cell(display_name[:16] if display_name else ""))
            table.setItem(row, 2, _cell(display_status))
            if is_queue_item:
                status_text = str(getattr(item, "monitoring_status", "") or "").upper()
                monitor_on = (
                    monitor_running
                    and getattr(item, "orb_monitor_enabled", False)
                    and status_text
                    in {
                        "WATCHING",
                        "ORB_FORMING",
                        "WAITING_BREAKOUT",
                        "ARMED",
                        "EXECUTE_READY",
                    }
                )
            else:
                monitor_on = item.monitoring_status in ("ACTIVE", "BOUGHT")
            table.setItem(row, 3, _cell("ON" if monitor_on else "OFF"))
            table.setItem(row, 4, _cell(f"{entry_price:.2f}"))
            table.setItem(row, 5, _cell(bp_display))  # daily breakout level
            table.setItem(row, 6, _cell(f"{stop_loss:.2f}"))
            table.setItem(
                row, 7, _cell(f"{current_price:.2f}" if current_price > 0 else "-")
            )
            table.setItem(
                row,
                8,
                _cell(
                    f"{pnl_pct:+.1f}%" if item.monitoring_status == "BOUGHT" else "-"
                ),
            )
            planned_shares = (
                queue_display.planned_shares
                if queue_display
                else 0
            )
            display_shares = (
                item.shares_held
                if item.monitoring_status == "BOUGHT"
                else planned_shares
            )
            table.setItem(
                row, 9, _cell(str(display_shares) if display_shares > 0 else "-")
            )
            table.setItem(row, 10, _cell(f"{capital_pct:.1f}%"))
            risk_pct_val = (
                queue_display.risk_percent if queue_display else item.risk_percent
            )
            risk_pct_display = f"{risk_pct_val:.2f}%" if risk_pct_val > 0 else "-"
            table.setItem(row, 11, _cell(risk_pct_display))
            table.setItem(
                row,
                12,
                _cell(str(days_held) if item.monitoring_status == "BOUGHT" else "-"),
            )

            alert_cell = _cell(alerts if alerts else "OK")
            if "STOP" in alerts:
                alert_cell.setBackground(QColor("#e53935"))
                alert_cell.setForeground(QColor("white"))
            elif alerts and alerts != "OK":
                alert_cell.setBackground(QColor("#fb8c00"))
                alert_cell.setForeground(QColor("white"))
            table.setItem(row, 13, alert_cell)

            # Row color by status
            row_color = None
            if item.monitoring_status == "BOUGHT":
                row_color = (
                    QColor("#2e7d32") if pnl_pct >= 0 else QColor("#c62828")
                )  # medium green / red
            elif item.monitoring_status == "ACTIVE" and not is_queue_item:
                row_color = QColor("#1565c0")  # medium blue
            elif item.monitoring_status == "SOLD":
                row_color = QColor("#546e7a")  # blue-grey
            if row_color:
                for col in range(table.columnCount()):
                    cell = table.item(row, col)
                    if cell:
                        cell.setBackground(row_color)

            if item.monitoring_status == "BOUGHT":
                total_capital += capital_pct
                total_pnl_usd += pnl_usd

        # Update summary labels
        pos_lbl = getattr(self, f"buylist_{env.lower()}_positions_label", None)
        cap_lbl = getattr(self, f"buylist_{env.lower()}_capital_label", None)
        pnl_lbl = getattr(self, f"buylist_{env.lower()}_pnl_label", None)
        if pos_lbl:
            pos_lbl.setText(f"Positions: {bought_count} / 30")
            pos_lbl.setStyleSheet(
                f"font-weight: bold; color: {'#f44336' if bought_count >= 30 else '#4CAF50'};"
            )
        if cap_lbl:
            cap_lbl.setText(f"Capital: {total_capital:.1f}%")
        if pnl_lbl:
            sign = "+" if total_pnl_usd >= 0 else ""
            pnl_lbl.setText(f"P&L: {sign}${total_pnl_usd:,.0f}")
            pnl_lbl.setStyleSheet(
                f"color: {'#4CAF50' if total_pnl_usd >= 0 else '#f44336'}; font-weight: bold;"
            )

    def _buylist_compute_alerts(
        self, item, current_price: float, days_held: int, queue_display=None
    ) -> str:
        """Return a pipe-separated alert string for a buylist item."""
        alerts = []
        if queue_display is not None:
            if queue_display.display_status:
                alerts.append(queue_display.display_status)
                if queue_display.display_status == "UNKNOWN_SUBMISSION_STATE":
                    alerts.append("UNKNOWN SUBMISSION - RECONCILE BEFORE RETRY")
            if queue_display.selected_window:
                alerts.append(f"ORB {queue_display.selected_window}")
            if queue_display.planned_shares > 0:
                alerts.append(f"Qty {queue_display.planned_shares}")
            return " | ".join(dict.fromkeys(alerts))

        queue_status = self._execution_queue_status_for_buylist_item(item)
        if queue_status:
            alerts.append(queue_status)
            if queue_status == "UNKNOWN_SUBMISSION_STATE":
                alerts.append("UNKNOWN SUBMISSION - RECONCILE BEFORE RETRY")

        if item.monitoring_status == "BOUGHT":
            if (
                current_price > 0
                and item.stop_loss > 0
                and current_price <= item.stop_loss
            ):
                alerts.append("STOP HIT")
            if getattr(item, "auto_order_block_reason", ""):
                alerts.append("KIS ORDER BLOCKED")
            if 3 <= days_held <= 5 and not item.sell_half_done:
                alerts.append("PARTIAL EXIT REVIEW")
            if getattr(item, "partial_exit_review_alert", False):
                alerts.append(
                    str(
                        getattr(item, "partial_exit_review_reason", "")
                        or "Manual partial-exit review"
                    )
                )
            if getattr(item, "ema_trailing_stop_alert", False):
                alerts.append(
                    str(
                        getattr(item, "ema_trailing_stop_reason", "")
                        or "EMA trailing-stop review"
                    )
                )
        elif item.monitoring_status == "ACTIVE":
            alerts.append("LEGACY ENTRY RETIRED - QUEUE REQUIRED")
        else:
            queue_statuses = self._execution_queue_status_values()
            status_text = str(getattr(item, "monitoring_status", "") or "").upper()
            if status_text in queue_statuses:
                alerts.append(status_text)
        return " | ".join(dict.fromkeys(alerts))

    @staticmethod
    def _execution_queue_value(value) -> str:
        return str(getattr(value, "value", value) or "")

    @staticmethod
    def _execution_queue_status_values() -> set:
        from src.core.execution_queue import ExecutionQueueStatus

        return {status.value for status in ExecutionQueueStatus}

    def _execution_queue_status_for_buylist_item(self, item) -> Optional[str]:
        if item is None or not self._is_pre_entry_execution_queue_buylist_item(item):
            return None
        queue_item = self._execution_queue_item_for_buylist_item(item)
        if queue_item is None:
            return None
        return self._execution_queue_value(queue_item.status)

    def _queue_display_state_for_buylist_item(self, item):
        if item is None or not self._is_pre_entry_execution_queue_buylist_item(item):
            return None
        queue_item = self._execution_queue_item_for_buylist_item(item)
        if queue_item is None:
            return None
        from src.core.execution_queue import build_queue_display_state

        return build_queue_display_state(queue_item, item)

    def _execution_queue_item_for_buylist_item(self, item):
        if item is None:
            return None
        symbol = str(getattr(item, "symbol", "") or "").upper()
        if not symbol:
            return None
        environment = str(getattr(item, "environment", "") or "PROD").upper()
        manager = self.__dict__.get("execution_queue_manager")
        if manager is None:
            manager = self._ensure_execution_queue_manager()
        get_item = getattr(manager, "get_item", None)
        if callable(get_item):
            return get_item(symbol, environment)
        from src.core.execution_queue import queue_key

        return manager.items.get(queue_key(symbol, environment))

    def _buylist_dashboard_status(self, item) -> str:
        queue_status = self._execution_queue_status_for_buylist_item(item)
        if queue_status:
            return queue_status
        return str(getattr(item, "monitoring_status", "") or "")

    def _is_orb_buylist_item(self, item) -> bool:
        if self._is_execution_queue_buylist_item(item):
            return True
        method = str(getattr(item, "breakout_method", "") or "").lower()
        if "orb" in method:
            return True
        try:
            breakout_price = float(getattr(item, "breakout_price", 0.0) or 0.0)
        except (TypeError, ValueError):
            breakout_price = 0.0
        return breakout_price > 0

    def _ensure_execution_queue_manager(self):
        from src.core.execution_queue import ExecutionQueueManager

        manager = self.__dict__.get("execution_queue_manager")
        if manager is not None:
            return manager

        data = load_json(EXECUTION_QUEUE_FILE, {})
        archive_non_production_execution_queue_state(data)
        rejected_records = []

        def collect_rejected(index, raw_record, error):
            rejected_records.append(
                {
                    "index": index,
                    "identity": (
                        str(raw_record.get("key") or index)
                        if isinstance(raw_record, dict)
                        else str(index)
                    ),
                    "error_type": type(error).__name__,
                    "error": str(error),
                    "record": raw_record,
                }
            )

        try:
            manager = (
                ExecutionQueueManager.from_dict(data, on_rejected=collect_rejected)
                if data
                else ExecutionQueueManager()
            )
        except Exception as exc:
            self.append_log(
                f"Execution queue state could not be loaded; starting fresh: {exc}"
            )
            manager = ExecutionQueueManager()
        quarantine_rejected_records(EXECUTION_QUEUE_FILE, rejected_records)
        manager.upgrade_margin = 0.0
        self.execution_queue_manager = manager
        return manager

    def _save_execution_queue_state(self) -> None:
        manager = self.__dict__.get("execution_queue_manager")
        if manager is None:
            return
        try:
            save_json(EXECUTION_QUEUE_FILE, manager.to_dict())
        except Exception as exc:
            self.append_log(f"Execution queue state save failed: {exc}")

    @staticmethod
    def _format_queue_price(value) -> str:
        try:
            price = float(value or 0.0)
        except (TypeError, ValueError):
            price = 0.0
        return f"${price:.2f}" if price > 0 else "-"

    @staticmethod
    def _format_queue_percent(value) -> str:
        try:
            pct = float(value)
        except (TypeError, ValueError):
            return "-"
        return f"{pct:.1f}%"

    @staticmethod
    def _is_execution_queue_buylist_item(item) -> bool:
        method = str(getattr(item, "breakout_method", "") or "").lower()
        return method.startswith("execution_queue")

    @staticmethod
    def _is_pre_entry_execution_queue_buylist_item(item) -> bool:
        from src.core.execution_queue import is_pre_entry_execution_queue_item

        return is_pre_entry_execution_queue_item(item)

    def _buylist_load_cached_intraday_interval(
        self, symbol: str, interval: str, window_days: int = 7
    ) -> pd.DataFrame:
        """Load queue-planning bars without depending on WatchlistMixin."""

        symbol = str(symbol or "").strip().upper()
        if (
            not symbol
            or not bool(getattr(self, "db_enabled", False))
            or getattr(self, "db_engine", None) is None
        ):
            return pd.DataFrame()
        since = _utcnow_naive() - dt.timedelta(
            days=max(1, min(7, int(window_days or 7)))
        )
        try:
            bars, source = load_best_intraday_history(
                symbol,
                self.db_engine,
                interval=interval,
                since=since,
            )
            self.__dict__.setdefault("latest_intraday_sources", {})[
                (symbol, interval)
            ] = source
            return bars
        except Exception:
            return pd.DataFrame()

    @staticmethod
    def _buylist_latest_intraday_session(intraday: pd.DataFrame) -> pd.DataFrame:
        if intraday is None or intraday.empty:
            return pd.DataFrame()
        bars = intraday.sort_index().copy()
        session_dates = pd.to_datetime(bars.index).date
        latest_date = session_dates[-1]
        return bars[session_dates == latest_date]

    def _buylist_adr_percent_for_symbol(self, symbol: str) -> Optional[float]:
        """Calculate ADR for queue sizing without a Watchlist UI helper."""

        if (
            not symbol
            or not bool(getattr(self, "db_enabled", False))
            or getattr(self, "db_engine", None) is None
        ):
            return None
        history = load_symbol_history_from_db(
            str(symbol).strip().upper(),
            self.db_engine,
            interval="1d",
            # ADR(20) needs only the recent window plus the previous close.
            # Loading a symbol's entire retained history on the Qt thread made
            # an explicit queue refresh unnecessarily expensive as well.
            max_rows=30,
        )
        if history.empty or len(history) < 2:
            return None
        prev_close = history["Close"].astype(float).shift(1)
        adr = (
            (history["High"].astype(float) - history["Low"].astype(float))
            / prev_close
        ).replace([float("inf"), float("-inf")], pd.NA)
        value = adr.rolling(20, min_periods=5).mean().iloc[-1]
        if pd.isna(value):
            return None
        return float(value * 100.0)

    def _buylist_signal_price(self, symbol: str) -> float:
        """Return only the Buy Board's already-observed intraday price."""

        try:
            price = float(
                getattr(self, "latest_intraday_prices", {}).get(
                    str(symbol or "").strip().upper(), 0.0
                )
            )
        except (TypeError, ValueError, OverflowError):
            return 0.0
        return price if math.isfinite(price) and price > 0 else 0.0

    @staticmethod
    def _buylist_risk_fraction(input_widget) -> float:
        try:
            raw_percent = float(input_widget.text())
        except (AttributeError, TypeError, ValueError, OverflowError):
            return 0.01
        if not math.isfinite(raw_percent) or raw_percent <= 0 or raw_percent > 100:
            return 0.01
        return raw_percent / 100.0

    def _execution_queue_target_items(
        self,
        env: str,
        symbols: Optional[List[str]] = None,
        *,
        create_missing: bool = False,
    ) -> Tuple[List[Any], List[str]]:
        # Existing Buylist mirrors and canonical cards are active planning
        # sources. Legacy Watchlist rows are read only for a one-time,
        # explicitly requested import (``create_missing=True``); they never
        # override an existing queue row on periodic refresh.
        legacy_items = list(
            getattr(getattr(self, "watchlist", None), "items", []) or []
        )
        legacy_by_symbol = {
            str(getattr(item, "symbol", "") or "").strip().upper(): item
            for item in legacy_items
            if str(getattr(item, "symbol", "") or "").strip()
        }
        canonical_by_symbol = {}
        active_canonical_symbols: set[str] = set()
        for projection in tuple(
            self.__dict__.get("_buyboard_current_projections", ()) or ()
        ):
            card = getattr(projection, "card", projection)
            if str(getattr(card, "environment", "") or "").upper() != env:
                continue
            status = str(
                getattr(getattr(card, "board_status", ""), "value", None)
                or getattr(card, "board_status", "")
            ).upper()
            symbol = str(getattr(card, "symbol", "") or "").strip().upper()
            if symbol and status not in {"", "WATCHLIST", "CLOSED"}:
                canonical_by_symbol.setdefault(symbol, card)
            if symbol and status == "BUY_TODAY":
                active_canonical_symbols.add(symbol)
        queued_symbols = [
            str(getattr(item, "symbol", "") or "").strip().upper()
            for item in list(
                getattr(getattr(self, "buylist_manager", None), "items", []) or []
            )
            if str(getattr(item, "environment", "") or "").upper() == env
            and self._is_execution_queue_buylist_item(item)
        ]

        if symbols is None:
            target_symbols = queued_symbols
        else:
            requested = []
            for raw_symbol in symbols:
                symbol = str(raw_symbol or "").strip().upper()
                if symbol and symbol not in requested:
                    requested.append(symbol)
            target_symbols = (
                requested
                if create_missing
                else [
                    symbol
                    for symbol in requested
                    if symbol in queued_symbols
                    or symbol in active_canonical_symbols
                ]
            )

        targets: List[Any] = []
        missing: List[str] = []
        for symbol in target_symbols:
            existing = (
                self.buylist_manager.get(symbol, env)
                if hasattr(self, "buylist_manager")
                else None
            )
            # Canonical TradeCard state owns the target.  A local Buylist
            # mirror may lag after a chart Set/Clear command and must never
            # rebuild the queue with the superseded breakout level.
            item = canonical_by_symbol.get(symbol)
            if (
                item is None
                and existing is not None
                and self._is_execution_queue_buylist_item(existing)
            ):
                item = existing
            if item is None and create_missing:
                item = legacy_by_symbol.get(symbol)
            if item is None:
                missing.append(symbol)
                continue
            targets.append(item)
        return targets, missing

    def _build_execution_queue_refresh_request(
        self,
        env: Optional[str] = None,
        symbols: Optional[List[str]] = None,
        *,
        create_missing: bool = False,
    ):
        from src.ui.buylist.execution_controller import \
            ExecutionQueueRefreshRequest

        env = str(env or "PROD").upper()
        requested_symbols = None
        if symbols is not None:
            requested_symbols = []
            for raw_symbol in symbols:
                symbol = str(raw_symbol or "").strip().upper()
                if symbol and symbol not in requested_symbols:
                    requested_symbols.append(symbol)

        target_items, missing_symbols = self._execution_queue_target_items(
            env,
            requested_symbols,
            create_missing=create_missing,
        )
        manager = None
        account_size = 0.0
        risk_percent = 0.01
        buffer_pct = 0.001
        account_no = ""
        if target_items:
            manager = self._ensure_execution_queue_manager()
            risk_percent = self._buylist_risk_fraction(
                getattr(self, "risk_percent_input", None)
            )
            if hasattr(self, "_buyboard_orb_buffer_pct"):
                buffer_pct = self._buyboard_orb_buffer_pct()
            account_no = self._first_account_no_for_environment(env) or ""

        published_buffers: dict[str, set[float]] = {}
        active_accounts: dict[str, set[str]] = {}
        canonical_accounts: dict[str, set[str]] = {}
        for projection in tuple(
            self.__dict__.get("_buyboard_current_projections", ()) or ()
        ):
            card = getattr(projection, "card", projection)
            if str(getattr(card, "environment", "") or "").upper() != env:
                continue
            status = str(
                getattr(getattr(card, "board_status", ""), "value", None)
                or getattr(card, "board_status", "")
            ).upper()
            symbol = str(getattr(card, "symbol", "") or "").strip().upper()
            card_account = str(
                getattr(card, "account_no", "") or ""
            ).strip()
            if symbol and card_account and status not in {"", "WATCHLIST", "CLOSED"}:
                canonical_accounts.setdefault(symbol, set()).add(card_account)
            if symbol and card_account and status == "BUY_TODAY":
                active_accounts.setdefault(symbol, set()).add(card_account)
            if status in {"", "WATCHLIST", "BUYLIST", "CLOSED"}:
                continue
            try:
                value = float(getattr(card, "buffer_pct", None))
            except (TypeError, ValueError, OverflowError):
                continue
            if symbol and math.isfinite(value) and 0.0 <= value <= 1.0:
                published_buffers.setdefault(symbol, set()).add(value)

        target_accounts: dict[str, str] = {}
        queue_accounts: dict[str, str] = {}
        for item in target_items:
            symbol = str(
                getattr(item, "symbol", "") or ""
            ).strip().upper()
            if not symbol:
                continue
            mirror = self.buylist_manager.get(symbol, env)
            mirror_account = str(
                getattr(mirror, "kis_account_no", "") or ""
            ).strip()
            item_account = str(
                getattr(item, "kis_account_no", "") or ""
            ).strip()
            target_accounts[symbol] = mirror_account or item_account
            queue_item = (
                manager.get_item(symbol, env)
                if manager is not None
                else None
            )
            queue_accounts[symbol] = str(
                getattr(queue_item, "account_no", "") or ""
            ).strip()

        def account_no_for_symbol(symbol: str) -> str:
            active = active_accounts.get(symbol, set())
            if len(active) > 1:
                raise ValueError(
                    f"{symbol} is active in Buy Today for multiple accounts"
                )
            if active:
                return next(iter(active))

            canonical = canonical_accounts.get(symbol, set())
            if len(canonical) == 1:
                return next(iter(canonical))

            mirror_account = target_accounts.get(symbol, "")
            queue_account = queue_accounts.get(symbol, "")
            if len(canonical) > 1:
                for candidate_account in (mirror_account, queue_account):
                    if candidate_account in canonical:
                        return candidate_account
                raise ValueError(
                    f"{symbol} exists in Buylist for multiple accounts"
                )
            return mirror_account or queue_account or account_no

        def account_size_for_account(
            environment: str, resolved_account_no: str
        ) -> Optional[float]:
            snapshot = buying_power_cache.get_snapshot(
                environment,
                resolved_account_no,
            )
            try:
                equity = float(
                    getattr(snapshot, "total_equity_usd", 0.0) or 0.0
                )
            except (TypeError, ValueError, OverflowError):
                return None
            if math.isfinite(equity) and equity > 0:
                return equity
            return None

        def persisted_buffer_for_symbol(symbol: str) -> Optional[float]:
            """Freeze every existing queue row to its published buffer.

            The header is a default for a newly queued plan. Periodic ORB
            refreshes, executor handoff, and manual window locks must keep the
            buffer already written to that symbol's Buylist/card state.
            """

            canonical = published_buffers.get(symbol, set())
            if len(canonical) > 1:
                raise ValueError(
                    f"Conflicting published ORB buffers exist for {symbol}"
                )
            if canonical:
                return next(iter(canonical))
            existing = self.buylist_manager.get(symbol, env)
            if existing is not None:
                try:
                    value = float(getattr(existing, "buffer_pct", None))
                except (TypeError, ValueError, OverflowError):
                    value = None
                if (
                    value is not None
                    and math.isfinite(value)
                    and 0.0 <= value <= 1.0
                ):
                    return value
            return None

        return ExecutionQueueRefreshRequest(
            env=env,
            manager=manager,
            buylist_manager=self.buylist_manager,
            target_items=target_items,
            missing_symbols=missing_symbols,
            requested_symbols=requested_symbols,
            account_size=account_size,
            risk_percent=risk_percent,
            buffer_pct=buffer_pct,
            buffer_pct_for_symbol=persisted_buffer_for_symbol,
            account_no=account_no,
            account_no_for_symbol=account_no_for_symbol,
            account_size_for_account=account_size_for_account,
            trade_card_engine=(
                self._execution_state_engine()
                if callable(getattr(self, "_execution_state_engine", None))
                else self.__dict__.get("pc_db_engine")
            ),
            latest_intraday_session=self._buylist_latest_intraday_session,
            load_intraday_interval=lambda symbol, interval, window_days: self._buylist_load_cached_intraday_interval(
                symbol,
                interval,
                window_days=window_days,
            ),
            signal_price_for_symbol=self._buylist_signal_price,
            set_latest_intraday_price=lambda symbol, price: self.latest_intraday_prices.__setitem__(
                symbol, price
            ),
            has_duplicate_open_order=self._has_duplicate_open_order,
            adr_percent_for_symbol=self._buylist_adr_percent_for_symbol,
        )

    def _apply_execution_queue_refresh_result(
        self, result, show_log: bool = True
    ) -> None:
        if result.target_count > 0:
            self.populate_buylist_dashboard()
            if hasattr(self, "update_dashboard_summary"):
                self.update_dashboard_summary()
            self._save_buylist_state()
            self._save_execution_queue_state()
            if result.canonical_changed_keys and hasattr(self, "refresh_buyboard"):
                self.refresh_buyboard()

        if not show_log:
            return

        if result.target_count == 0:
            if result.requested_symbols is None:
                self.append_log(
                    f"[Execution Queue/{result.env}] No queued buylist symbols to refresh."
                )
            else:
                self.append_log(
                    f"[Execution Queue/{result.env}] No selected planning symbols could be queued."
                )
            if result.missing_symbols:
                self.append_log(
                    f"[Execution Queue/{result.env}] Missing symbols: "
                    + ", ".join(result.missing_symbols[:10])
                )
            return

        counts_text = (
            ", ".join(
                f"{key}={value}" for key, value in sorted(result.status_counts.items())
            )
            or "none"
        )
        self.append_log(
            f"[Execution Queue/{result.env}] Refreshed {result.refreshed} {result.scope} symbol(s): {counts_text}."
        )
        if result.missing_symbols:
            self.append_log(
                f"[Execution Queue/{result.env}] Missing symbols: "
                + ", ".join(result.missing_symbols[:10])
            )
        if result.failures:
            self.append_log(
                f"[Execution Queue/{result.env}] Refresh failures: "
                + "; ".join(result.failures[:10])
            )

    def refresh_execution_queue(
        self,
        env: Optional[str] = None,
        show_log: bool = True,
        symbols: Optional[List[str]] = None,
        *,
        create_missing: bool = False,
    ) -> int:
        """Refresh existing queue rows, or intentionally queue selected symbols."""
        from src.ui.buylist.execution_controller import \
            BuylistExecutionController
        from src.ui.controllers.base import get_controller

        controller = get_controller(
            self, "buylist_execution_controller", BuylistExecutionController
        )
        request = self._build_execution_queue_refresh_request(
            env,
            symbols=symbols,
            create_missing=create_missing,
        )
        result = controller.refresh_execution_queue(request)
        self._last_execution_queue_refresh_result = result
        self._apply_execution_queue_refresh_result(result, show_log=show_log)
        if result.target_count == 0 or (
            result.refreshed == result.target_count and not result.failures
        ):
            self._execution_queue_account_sizing_dirty = False
        return result.refreshed

    def _apply_execution_queue_item_to_buylist(
        self, queue_item, planning_item, env: str, buffer_pct: float
    ) -> None:
        from src.ui.buylist.execution_controller import \
            BuylistExecutionController
        from src.ui.controllers.base import get_controller

        controller = get_controller(
            self, "buylist_execution_controller", BuylistExecutionController
        )
        controller.apply_execution_queue_item_to_buylist(
            queue_item, planning_item, env, buffer_pct
        )

    def _queue_item_for_buylist_item(self, item):
        if item is None:
            return None
        return self._execution_queue_item_for_buylist_item(item)

    def _unlock_execution_queue_item_for_auto(self, queue_item) -> None:
        """Return the canonical execution-queue row to automatic selection."""
        from src.core.execution_queue import select_best_orb_candidate

        manager = self.__dict__.get("execution_queue_manager")
        upgrade_margin = getattr(manager, "upgrade_margin", 0.0) if manager else 0.0
        queue_item.locked = False
        queue_item.manual_window_lock = False
        queue_item.locked_reason = None
        best = select_best_orb_candidate(
            getattr(queue_item, "candidates", {}) or {},
            getattr(queue_item, "selected_window", None),
            False,
            upgrade_margin=upgrade_margin,
        )
        queue_item.selected_candidate = best
        queue_item.selected_window = best.window if best else None
        self._save_execution_queue_state()

    def _format_execution_queue_order_review(self, env: str, item, queue_item) -> str:
        from src.core.execution_queue import OrbCandidateStatus

        candidate = getattr(queue_item, "selected_candidate", None)
        pending_trigger = False

        if candidate is None:
            # ARMED: best candidate is WAITING_BREAKOUT — not yet EXECUTE_READY.
            # Show it as a preview so the user can verify the planned order.
            _priority = {
                OrbCandidateStatus.WAITING_BREAKOUT: 0,
                OrbCandidateStatus.RISK_INVALID: 1,
                OrbCandidateStatus.REJECTED: 2,
            }
            all_candidates = list(getattr(queue_item, "candidates", {}).values())
            displayable = [c for c in all_candidates if c.status in _priority]
            if displayable:
                candidate = min(
                    displayable,
                    key=lambda c: (_priority[c.status], -float(c.score or 0)),
                )
                pending_trigger = True

        if candidate is None:
            return (
                f"{item.symbol} has no ORB candidate computed yet.\n\n"
                "Refresh the Buy Today ORB plans and try again."
            )

        account_no = self._first_account_no_for_environment(env) or "<not selected>"
        entry_trigger = float(candidate.entry_trigger or 0.0)
        execution_price = float(candidate.execution_price or 0.0)
        shares = int(candidate.shares or 0)
        stop_loss = float(candidate.stop_loss or 0.0)
        estimated_amount = execution_price * shares
        risk_amount = max(0.0, execution_price - stop_loss) * shares
        warnings = list(getattr(candidate, "warnings", []) or []) + list(
            getattr(queue_item, "warnings", []) or []
        )
        warning_text = "; ".join(dict.fromkeys(warnings)) if warnings else "None"
        status_line = (
            "Status: ARMED — waiting for a post-range trade strictly above the confirmation trigger"
            if pending_trigger
            else (
                "Status: EXECUTE_READY — exact passive limit is eligible on the next "
                "monitor cycle only while fresh last/ask remain strictly above it"
            )
        )
        return "\n".join(
            [
                status_line,
                "",
                f"Environment: {env}",
                f"Account: {account_no}",
                f"Symbol: {item.symbol}",
                f"Selected ORB: {candidate.window}",
                "Side: BUY",
                f"Limit price: {self._format_queue_price(execution_price)}",
                f"Breakout confirmation: > {self._format_queue_price(entry_trigger)}",
                f"Quantity: {shares}",
                f"Estimated amount: {self._format_queue_price(estimated_amount)}",
                f"ORB high: {self._format_queue_price(candidate.orb_high)}",
                f"ORB low: {self._format_queue_price(candidate.orb_low)}",
                f"Breakout price: {self._format_queue_price(candidate.breakout_price)}",
                f"Breakout trigger: {self._format_queue_price(candidate.breakout_trigger)}",
                f"Stop loss: {self._format_queue_price(stop_loss)}",
                f"Risk amount: {self._format_queue_price(risk_amount)}",
                f"Capital allocation: {self._format_queue_percent(candidate.capital_percent)}",
                f"Stop/ADR: {self._format_queue_percent(candidate.stop_adr)}",
                f"Score: {float(candidate.score or 0.0):.1f}",
                f"Warnings: {warning_text}",
            ]
        )

    def _buylist_review_selected_queue_order(self, env: str) -> None:
        from src.core.execution_queue import (SUPPORTED_ORB_WINDOWS,
                                              OrbCandidateStatus)

        item = self._buylist_selected_item(env)
        if not item:
            QMessageBox.warning(
                self, "No selection", "Select an execution queue row first."
            )
            return
        queue_item = self._queue_item_for_buylist_item(item)
        if queue_item is None:
            QMessageBox.warning(
                self,
                "No queue item",
                f"{item.symbol} is not in the execution queue. Click Refresh Queue first.",
            )
            return

        candidates: dict = getattr(queue_item, "candidates", {}) or {}
        if not any(
            c
            for c in candidates.values()
            if c.status not in (OrbCandidateStatus.NOT_AVAILABLE,)
        ):
            QMessageBox.warning(
                self,
                "No data",
                f"{item.symbol} has no ORB candidates yet. Click Refresh Queue first.",
            )
            return

        # ── Dialog ────────────────────────────────────────────────────────────
        dlg = QDialog(self)
        dlg.setWindowTitle(f"ORB Plan — {item.symbol}  [{env}]")
        dlg.setMinimumWidth(780)
        dlg.setMinimumHeight(320)
        dlg_layout = QVBoxLayout(dlg)
        dlg_layout.setSpacing(8)

        # Lock status banner
        lock_lbl = QLabel()
        lock_lbl.setStyleSheet(
            "font-weight: bold; padding: 4px 8px; border-radius: 4px;"
        )
        dlg_layout.addWidget(lock_lbl)

        # ── Candidate table ────────────────────────────────────────────────────
        COLS = [
            "Window",
            "Status",
            "ORB High",
            "ORB Low",
            "Entry",
            "Stop",
            "Shares",
            "Capital%",
            "Risk%",
            "Score",
            "Stop/ADR",
            "Warnings",
        ]
        tbl = QTableWidget(0, len(COLS))
        tbl.setHorizontalHeaderLabels(COLS)
        tbl.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        tbl.horizontalHeader().setStretchLastSection(True)
        tbl.setSelectionBehavior(QAbstractItemView.SelectRows)
        tbl.setEditTriggers(QAbstractItemView.NoEditTriggers)
        tbl.setAlternatingRowColors(True)
        tbl.verticalHeader().setVisible(False)
        for col, w in enumerate([52, 105, 72, 72, 72, 68, 58, 72, 60, 56, 70, 150]):
            tbl.setColumnWidth(col, w)
        dlg_layout.addWidget(tbl, 1)

        _status_color = {
            OrbCandidateStatus.EXECUTE_READY: ("#1b5e20", "#a5d6a7"),
            OrbCandidateStatus.WAITING_BREAKOUT: ("#0d47a1", "#90caf9"),
            OrbCandidateStatus.RISK_INVALID: ("#b71c1c", "#ef9a9a"),
            OrbCandidateStatus.REJECTED: ("#37474f", "#b0bec5"),
            OrbCandidateStatus.FORMING: ("#4a148c", "#ce93d8"),
        }

        def _fmt_p(v) -> str:
            try:
                return f"${float(v):.2f}" if v is not None else "—"
            except (TypeError, ValueError):
                return "—"

        def _fmt_pct(v) -> str:
            try:
                return f"{float(v):.1f}%" if v is not None else "—"
            except (TypeError, ValueError):
                return "—"

        def _populate_table():
            tbl.setRowCount(0)
            current_window = getattr(queue_item, "selected_window", None)
            # If selected_window not set, derive from selected_candidate
            if not current_window:
                sc = getattr(queue_item, "selected_candidate", None)
                if sc:
                    current_window = getattr(sc, "window", None)
            for window in SUPPORTED_ORB_WINDOWS:
                cand = candidates.get(window)
                if cand is None:
                    continue
                is_selected = window == current_window
                row = tbl.rowCount()
                tbl.insertRow(row)
                status_str = (
                    cand.status.value
                    if hasattr(cand.status, "value")
                    else str(cand.status)
                )
                window_label = f"▶ {window}" if is_selected else window
                vals = [
                    window_label,
                    status_str,
                    _fmt_p(cand.orb_high),
                    _fmt_p(cand.orb_low),
                    _fmt_p(cand.entry_trigger),
                    _fmt_p(cand.stop_loss),
                    str(int(cand.shares or 0)) if cand.shares else "—",
                    _fmt_pct(cand.capital_percent),
                    (
                        _fmt_pct(float(cand.risk_percent or 0) * 100)
                        if cand.risk_percent
                        else "—"
                    ),
                    f"{float(cand.score or 0):.1f}",
                    _fmt_pct(cand.stop_adr),
                    "; ".join(cand.warnings) if cand.warnings else "OK",
                ]
                for col, val in enumerate(vals):
                    cell = QTableWidgetItem(val)
                    cell.setTextAlignment(Qt.AlignCenter)
                    tbl.setItem(row, col, cell)

                if is_selected:
                    # Yellow highlight with bold black text — clearly selected
                    from PyQt5.QtGui import QFont

                    bold = QFont()
                    bold.setBold(True)
                    for col in range(len(COLS)):
                        c = tbl.item(row, col)
                        if c:
                            c.setBackground(QColor("#fff176"))
                            c.setForeground(QColor("#000000"))
                            c.setFont(bold)
                else:
                    # Color by status
                    fg, bg = _status_color.get(cand.status, ("#212121", "#f5f5f5"))
                    for col in range(len(COLS)):
                        c = tbl.item(row, col)
                        if c:
                            c.setBackground(QColor(bg))
                            c.setForeground(QColor(fg))

        def _update_lock_label():
            if getattr(queue_item, "manual_window_lock", False):
                w = getattr(queue_item, "selected_window", "?")
                lock_lbl.setText(
                    f"Manual lock: {w} window. Queue refresh will not change the selected plan."
                )
                lock_lbl.setStyleSheet(
                    "font-weight: bold; background-color: #e65100; color: white; padding: 4px 8px; border-radius: 4px;"
                )
                return
            if getattr(queue_item, "locked", False):
                w = getattr(queue_item, "selected_window", "?")
                lock_lbl.setText(
                    f"Order lock: {w} window. Auto replacement is allowed only for a higher ORB score."
                )
                lock_lbl.setStyleSheet(
                    "font-weight: bold; background-color: #6d4c41; color: white; padding: 4px 8px; border-radius: 4px;"
                )
                return
            lock_lbl.setText(
                "Auto: best-scoring valid plan selected each queue refresh."
            )
            lock_lbl.setStyleSheet(
                "font-weight: bold; background-color: #1565c0; color: white; padding: 4px 8px; border-radius: 4px;"
            )
            return
            if getattr(queue_item, "locked", False):
                w = getattr(queue_item, "selected_window", "?")
                lock_lbl.setText(
                    f"🔒  LOCKED to {w} window — queue refresh will not change the selected plan"
                )
                lock_lbl.setStyleSheet(
                    "font-weight: bold; background-color: #e65100; color: white; padding: 4px 8px; border-radius: 4px;"
                )
            else:
                lock_lbl.setText(
                    "⚡  AUTO — best-scoring valid plan selected each queue refresh"
                )
                lock_lbl.setStyleSheet(
                    "font-weight: bold; background-color: #1565c0; color: white; padding: 4px 8px; border-radius: 4px;"
                )

        _populate_table()
        _update_lock_label()

        # ── Buttons ────────────────────────────────────────────────────────────
        btn_row = QHBoxLayout()

        lock_btn = QPushButton("🔒  Lock Selected Window")
        lock_btn.setMinimumWidth(170)
        lock_btn.setStyleSheet(
            "background-color: #e65100; color: white; font-weight: bold;"
        )

        unlock_btn = QPushButton("⚡  Unlock (Auto)")
        unlock_btn.setMinimumWidth(130)
        unlock_btn.setStyleSheet(
            "background-color: #1565c0; color: white; font-weight: bold;"
        )

        close_btn = QPushButton("Close")
        close_btn.setMinimumWidth(80)

        btn_row.addWidget(lock_btn)
        btn_row.addWidget(unlock_btn)
        btn_row.addStretch()
        btn_row.addWidget(close_btn)
        dlg_layout.addLayout(btn_row)

        def _lock_selected():
            sel = tbl.currentRow()
            if sel < 0:
                QMessageBox.warning(
                    dlg, "No row selected", "Click a window row first, then lock."
                )
                return
            window_cell = tbl.item(sel, 0)
            if window_cell is None:
                return
            raw_chosen = window_cell.text().strip()
            chosen = next(
                (
                    window
                    for window in SUPPORTED_ORB_WINDOWS
                    if window in raw_chosen.split()
                ),
                raw_chosen,
            )
            cand = candidates.get(chosen)
            if cand is None:
                return
            queue_item.locked = True
            queue_item.manual_window_lock = True
            queue_item.locked_reason = "Manual ORB window lock"
            queue_item.selected_window = chosen
            queue_item.selected_candidate = cand
            self._save_execution_queue_state()
            self.populate_buylist_dashboard()
            _populate_table()
            _update_lock_label()

        def _unlock():
            self._unlock_execution_queue_item_for_auto(queue_item)
            self.populate_buylist_dashboard()
            _populate_table()
            _update_lock_label()

        lock_btn.clicked.connect(_lock_selected)
        unlock_btn.clicked.connect(_unlock)
        close_btn.clicked.connect(dlg.accept)

        dlg.exec_()

    def _buylist_submit_selected_queue_order(self, env: str) -> None:
        from src.ui.buylist.execution_controller import \
            BuylistExecutionController
        from src.ui.controllers.base import get_controller

        controller = get_controller(
            self, "buylist_execution_controller", BuylistExecutionController
        )
        controller.submit_selected_queue_order(env)

    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # Buylist Dashboard — action button handlers
    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
