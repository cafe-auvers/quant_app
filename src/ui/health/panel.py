"""Separate production-health tab: system checks, event journal, P&L dashboard."""

from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import replace
from typing import List, Optional

from PyQt5.QtCore import QThread, Qt, pyqtSignal
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

try:
    from PyQt5.QtWebEngineWidgets import QWebEngineView
except ImportError:  # pragma: no cover - PyQtWebEngine is a hard requirement,
    QWebEngineView = None  # but every other chart tab guards this the same way.

from src.services.event_journal import load_recent_events
from src.services.health import (
    HealthContext,
    HealthLevel,
    HealthSnapshot,
    collect_health_snapshot,
)
from src.services.order_ledger import load_order_ledger
from src.services.pnl_history import (
    PnlDailySnapshot,
    compute_unrealized_pnl_usd,
    record_daily_pnl_snapshot,
)
from src.ui.health.pnl_chart import (
    SERIES_COMBINED,
    SERIES_REALIZED,
    SERIES_UNREALIZED,
    UNIT_KRW,
    UNIT_PCT,
    UNIT_USD,
    generate_pnl_chart_html,
    pnl_chart_points,
)

logger = logging.getLogger(__name__)


class HealthProbeWorker(QThread):
    completed = pyqtSignal(object, list, list)
    failed = pyqtSignal(str)

    def __init__(
        self,
        context: HealthContext,
        journal_symbol: str = "",
        *,
        unrealized_usd_today: float = 0.0,
        fx_rate_today: Optional[float] = None,
        capital_base_usd_today: Optional[float] = None,
    ) -> None:
        super().__init__()
        self.context = context
        self.journal_symbol = journal_symbol
        self.unrealized_usd_today = unrealized_usd_today
        self.fx_rate_today = fx_rate_today
        self.capital_base_usd_today = capital_base_usd_today

    def run(self) -> None:
        try:
            try:
                orders = tuple(load_order_ledger())
                context = replace(self.context, orders=orders, order_ledger_error="")
            except Exception as exc:
                orders = self.context.orders
                context = replace(self.context, order_ledger_error=str(exc))
            snapshot = collect_health_snapshot(context)
            events = load_recent_events(limit=300, symbol=self.journal_symbol)
            pnl_snapshots: List[PnlDailySnapshot] = []
            try:
                pnl_snapshots = record_daily_pnl_snapshot(
                    orders,
                    unrealized_usd_today=self.unrealized_usd_today,
                    fx_rate_today=self.fx_rate_today,
                    capital_base_usd_today=self.capital_base_usd_today,
                )
            except Exception:
                logger.exception("Failed to build daily P&L snapshot history")
            self.completed.emit(snapshot, events, pnl_snapshots)
        except Exception as exc:
            self.failed.emit(str(exc))


class HealthPanelMixin:
    """Build and refresh a read-only system-health view."""

    _HEALTH_COLORS = {
        HealthLevel.HEALTHY: QColor("#d1fae5"),
        HealthLevel.WARNING: QColor("#fef3c7"),
        HealthLevel.CRITICAL: QColor("#fee2e2"),
        HealthLevel.UNKNOWN: QColor("#e5e7eb"),
    }

    _PNL_SERIES_ITEMS = [
        ("Combined (Realized + Unrealized)", SERIES_COMBINED),
        ("Realized only", SERIES_REALIZED),
        ("Unrealized only", SERIES_UNREALIZED),
    ]
    _PNL_UNIT_ITEMS = [
        ("USD ($)", UNIT_USD),
        ("KRW (₩)", UNIT_KRW),
        ("Percent (%)", UNIT_PCT),
    ]

    def _build_health_tab(self) -> None:
        layout = QVBoxLayout(self.health_widget)

        controls = QHBoxLayout()
        title = QLabel("Production Health")
        title.setStyleSheet("font-size: 18px; font-weight: 600;")
        controls.addWidget(title)
        controls.addStretch()
        self.health_checked_at_label = QLabel("Not checked yet")
        controls.addWidget(self.health_checked_at_label)
        self.health_refresh_button = QPushButton("Refresh Health")
        self.health_refresh_button.clicked.connect(self.refresh_health_panel)
        controls.addWidget(self.health_refresh_button)
        layout.addLayout(controls)

        self.health_summary_label = QLabel(
            "Open this tab or click Refresh Health to run read-only local checks."
        )
        self.health_summary_label.setWordWrap(True)
        layout.addWidget(self.health_summary_label)

        health_group = QGroupBox("System checks")
        health_layout = QVBoxLayout(health_group)
        self.health_checks_table = QTableWidget(0, 4)
        self.health_checks_table.setHorizontalHeaderLabels(
            ["Component", "Status", "Summary", "Detail"]
        )
        self.health_checks_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.health_checks_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.health_checks_table.setAlternatingRowColors(True)
        header = self.health_checks_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        header.setSectionResizeMode(3, QHeaderView.Stretch)
        health_layout.addWidget(self.health_checks_table)
        layout.addWidget(health_group, 2)

        self.health_tabs = QTabWidget()
        self.health_tabs.addTab(self._build_journal_tab(), "Event Journal")
        self.health_tabs.addTab(self._build_pnl_dashboard_tab(), "P&L Dashboard")
        layout.addWidget(self.health_tabs, 3)

    def _build_journal_tab(self) -> QWidget:
        journal_tab = QWidget()
        journal_layout = QVBoxLayout(journal_tab)
        journal_controls = QHBoxLayout()
        journal_controls.addWidget(QLabel("Symbol:"))
        self.health_journal_symbol_input = QLineEdit()
        self.health_journal_symbol_input.setPlaceholderText("All symbols")
        self.health_journal_symbol_input.setMaximumWidth(180)
        self.health_journal_symbol_input.returnPressed.connect(
            self.refresh_health_panel
        )
        journal_controls.addWidget(self.health_journal_symbol_input)
        journal_controls.addStretch()
        journal_controls.addWidget(
            QLabel("Newest first; accounts are masked and secrets are redacted.")
        )
        journal_layout.addLayout(journal_controls)

        self.health_journal_table = QTableWidget(0, 7)
        self.health_journal_table.setHorizontalHeaderLabels(
            [
                "Time",
                "Event",
                "Symbol",
                "Strategy",
                "Account",
                "Qty / Price",
                "Reason / Payload",
            ]
        )
        self.health_journal_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.health_journal_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.health_journal_table.setAlternatingRowColors(True)
        journal_header = self.health_journal_table.horizontalHeader()
        for column in range(6):
            journal_header.setSectionResizeMode(column, QHeaderView.ResizeToContents)
        journal_header.setSectionResizeMode(6, QHeaderView.Stretch)
        journal_layout.addWidget(self.health_journal_table)
        return journal_tab

    def _build_pnl_dashboard_tab(self) -> QWidget:
        pnl_tab = QWidget()
        pnl_layout = QVBoxLayout(pnl_tab)

        controls = QHBoxLayout()
        controls.addWidget(QLabel("Series:"))
        self.health_pnl_series_combo = QComboBox()
        for label, _value in self._PNL_SERIES_ITEMS:
            self.health_pnl_series_combo.addItem(label)
        self.health_pnl_series_combo.currentIndexChanged.connect(
            self._refresh_pnl_dashboard_view
        )
        controls.addWidget(self.health_pnl_series_combo)

        controls.addWidget(QLabel("Unit:"))
        self.health_pnl_unit_combo = QComboBox()
        for label, _value in self._PNL_UNIT_ITEMS:
            self.health_pnl_unit_combo.addItem(label)
        self.health_pnl_unit_combo.currentIndexChanged.connect(
            self._refresh_pnl_dashboard_view
        )
        controls.addWidget(self.health_pnl_unit_combo)

        controls.addStretch()
        self.health_pnl_table_toggle = QPushButton("Show Table")
        self.health_pnl_table_toggle.setCheckable(True)
        self.health_pnl_table_toggle.toggled.connect(self._on_pnl_table_toggle)
        controls.addWidget(self.health_pnl_table_toggle)
        pnl_layout.addLayout(controls)

        self.health_pnl_status_label = QLabel(
            "No P&L history yet. It starts building the next time this tab refreshes."
        )
        self.health_pnl_status_label.setWordWrap(True)
        pnl_layout.addWidget(self.health_pnl_status_label)

        self.health_pnl_view_stack = QStackedWidget()

        if QWebEngineView is not None:
            self.health_pnl_chart_view = QWebEngineView()
        else:  # pragma: no cover - PyQtWebEngine is a pinned hard dependency
            self.health_pnl_chart_view = QTextEdit()
            self.health_pnl_chart_view.setReadOnly(True)
        self.health_pnl_view_stack.addWidget(self.health_pnl_chart_view)

        self.health_pnl_table = QTableWidget(0, 4)
        self.health_pnl_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.health_pnl_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.health_pnl_table.setAlternatingRowColors(True)
        pnl_table_header = self.health_pnl_table.horizontalHeader()
        for column in range(4):
            pnl_table_header.setSectionResizeMode(column, QHeaderView.Stretch)
        self.health_pnl_view_stack.addWidget(self.health_pnl_table)

        pnl_layout.addWidget(self.health_pnl_view_stack)

        self._pnl_snapshots: List[PnlDailySnapshot] = []
        self._render_pnl_dashboard_placeholder()
        return pnl_tab

    def _on_pnl_table_toggle(self, checked: bool) -> None:
        self.health_pnl_table_toggle.setText("Show Chart" if checked else "Show Table")
        self.health_pnl_view_stack.setCurrentIndex(1 if checked else 0)

    def _selected_pnl_series(self) -> str:
        index = self.health_pnl_series_combo.currentIndex()
        if 0 <= index < len(self._PNL_SERIES_ITEMS):
            return self._PNL_SERIES_ITEMS[index][1]
        return SERIES_COMBINED

    def _selected_pnl_unit(self) -> str:
        index = self.health_pnl_unit_combo.currentIndex()
        if 0 <= index < len(self._PNL_UNIT_ITEMS):
            return self._PNL_UNIT_ITEMS[index][1]
        return UNIT_USD

    def _render_pnl_dashboard_placeholder(self) -> None:
        if isinstance(self.health_pnl_chart_view, QTextEdit):
            self.health_pnl_chart_view.setPlainText(
                "No P&L history yet. It starts building the next time this tab refreshes."
            )
        elif QWebEngineView is not None:
            self.health_pnl_chart_view.setHtml(generate_pnl_chart_html([]))
        self.health_pnl_table.setRowCount(0)

    def _refresh_pnl_dashboard_view(self, *_args) -> None:
        snapshots = self.__dict__.get("_pnl_snapshots") or []
        series = self._selected_pnl_series()
        unit = self._selected_pnl_unit()

        if not snapshots:
            self._render_pnl_dashboard_placeholder()
            return

        if isinstance(self.health_pnl_chart_view, QTextEdit):
            points = pnl_chart_points(snapshots, series=series, unit=unit)
            lines = [f"{point['time']}: {point['value']:,.2f}" for point in points]
            self.health_pnl_chart_view.setPlainText(
                "\n".join(lines) or "No data yet for this unit."
            )
        elif QWebEngineView is not None:
            html_content = generate_pnl_chart_html(snapshots, series=series, unit=unit)
            self.health_pnl_chart_view.setHtml(html_content)

        self._populate_pnl_table(snapshots, unit=unit)

        latest = snapshots[-1]
        fx_note = (
            f"FX {latest.fx_rate:,.2f} KRW/USD" if latest.fx_rate else "FX not available yet"
        )
        capital_note = (
            f"capital base ${latest.capital_base_usd:,.0f}"
            if latest.capital_base_usd
            else "capital base not available yet (needed for %)"
        )
        self.health_pnl_status_label.setText(
            f"As of {latest.date}: realized ${latest.realized_usd:,.2f}, "
            f"unrealized ${latest.unrealized_usd:,.2f}, total ${latest.total_usd:,.2f} "
            f"({fx_note}, {capital_note}). Unrealized history only accumulates from the "
            f"day this dashboard started tracking; realized P&L is backfilled from your "
            f"full order history."
        )

    def _populate_pnl_table(self, snapshots: List[PnlDailySnapshot], *, unit: str) -> None:
        unit_suffix = {UNIT_USD: "$", UNIT_KRW: "₩", UNIT_PCT: "%"}.get(unit, "")
        self.health_pnl_table.setHorizontalHeaderLabels(
            [
                "Date",
                f"Realized ({unit_suffix})",
                f"Unrealized ({unit_suffix})",
                f"Total ({unit_suffix})",
            ]
        )
        ordered = list(reversed(snapshots))
        self.health_pnl_table.setRowCount(len(ordered))
        for row, snapshot in enumerate(ordered):
            realized_pt = pnl_chart_points([snapshot], series=SERIES_REALIZED, unit=unit)
            unrealized_pt = pnl_chart_points(
                [snapshot], series=SERIES_UNREALIZED, unit=unit
            )
            total_pt = pnl_chart_points([snapshot], series=SERIES_COMBINED, unit=unit)
            values = [
                snapshot.date,
                f"{realized_pt[0]['value']:,.2f}" if realized_pt else "—",
                f"{unrealized_pt[0]['value']:,.2f}" if unrealized_pt else "—",
                f"{total_pt[0]['value']:,.2f}" if total_pt else "—",
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setTextAlignment(Qt.AlignCenter if column else Qt.AlignLeft)
                self.health_pnl_table.setItem(row, column, item)

    def _pnl_inputs(self) -> tuple:
        positions = []
        for item in getattr(self.buylist_manager, "items", []) or []:
            if getattr(item, "shares_held", 0) > 0 and getattr(item, "avg_cost", 0) > 0:
                positions.append(
                    {
                        "symbol": item.symbol,
                        "shares_held": item.shares_held,
                        "avg_cost": item.avg_cost,
                    }
                )
        prices = dict(self.__dict__.get("latest_intraday_prices", {}) or {})
        unrealized_usd_today = compute_unrealized_pnl_usd(positions, prices)

        fx_rate = 0.0
        if hasattr(self, "usd_krw_rate_input"):
            fx_rate = self._parse_float(self.usd_krw_rate_input, 0.0)
        capital_base = 0.0
        if hasattr(self, "account_size_input"):
            capital_base = self._parse_float(self.account_size_input, 0.0)

        return (
            unrealized_usd_today,
            fx_rate if fx_rate > 0 else None,
            capital_base if capital_base > 0 else None,
        )

    def _health_context(self) -> HealthContext:
        reconciliation_worker = self.__dict__.get("order_reconciliation_worker")
        pc_database_engine = self.__dict__.get("pc_db_engine")
        if pc_database_engine is None:
            pc_database_engine = self.__dict__.get("_pc_probe_engine")
        kis_workers = (
            self.__dict__.get("kis_account_worker"),
            self.__dict__.get("kis_startup_worker"),
        )
        mirror_tickers = list(self.__dict__.get("universe_tickers") or []) or None

        state_sync_role = self.__dict__.get("state_sync_role")
        is_main_device = bool(state_sync_role and state_sync_role.is_main)
        last_reconcile = self.__dict__.get("_last_successful_reconcile_at")
        lease_age_seconds = None
        if is_main_device and last_reconcile is not None:
            lease_age_seconds = (
                dt.datetime.now(dt.timezone.utc) - last_reconcile
            ).total_seconds()
        handoff_worker = self.__dict__.get("handoff_reconciliation_worker")
        return HealthContext(
            db_source=str(self.__dict__.get("db_engine_source", "none")),
            db_initializing=bool(self.__dict__.get("db_initializing", False)),
            pc_database_ready=bool(self.__dict__.get("_pc_database_ready", False)),
            pc_database_engine=pc_database_engine,
            mirror_engine=self.__dict__.get("_local_mirror_engine"),
            mirror_tickers=mirror_tickers,
            kis_snapshot_count=len(self.__dict__.get("kis_account_snapshots", {})),
            kis_request_running=any(
                worker is not None and worker.isRunning() for worker in kis_workers
            ),
            kis_last_success_at=str(self.__dict__.get("_kis_api_last_success_at", "")),
            kis_last_error=str(self.__dict__.get("_kis_api_last_error", "")),
            orders=tuple(self.__dict__.get("order_ledger", ())),
            reconciliation_running=bool(
                reconciliation_worker is not None and reconciliation_worker.isRunning()
            ),
            reconciliation_last_success_at=str(
                self.__dict__.get("_last_order_reconciliation_at", "")
            ),
            reconciliation_last_error=str(
                self.__dict__.get("_last_order_reconciliation_error", "")
            ),
            is_main_device=is_main_device,
            main_device_hostname=str(
                self.__dict__.get("_last_main_device_hostname", "")
            ),
            lease_age_seconds=lease_age_seconds,
            auto_claim_enabled=bool(self.__dict__.get("_auto_claim_main_enabled", False)),
            handoff_reconciliation_running=bool(
                handoff_worker is not None and handoff_worker.isRunning()
            ),
            handoff_blocked_symbols=tuple(
                self.__dict__.get("_last_handoff_blocked_symbols", ())
            ),
        )

    def refresh_health_panel(self, *args) -> None:
        worker = self.__dict__.get("_health_probe_worker")
        if worker is not None and worker.isRunning():
            return
        self.health_refresh_button.setEnabled(False)
        self.health_checked_at_label.setText("Checking...")
        symbol = self.health_journal_symbol_input.text().strip().upper()
        unrealized_usd_today, fx_rate_today, capital_base_usd_today = self._pnl_inputs()
        worker = HealthProbeWorker(
            self._health_context(),
            symbol,
            unrealized_usd_today=unrealized_usd_today,
            fx_rate_today=fx_rate_today,
            capital_base_usd_today=capital_base_usd_today,
        )
        self._health_probe_worker = worker
        worker.completed.connect(self._on_health_probe_completed)
        worker.failed.connect(self._on_health_probe_failed)
        self._track_worker("_health_probe_worker", worker)
        worker.start()

    def _on_health_probe_completed(
        self,
        snapshot: HealthSnapshot,
        events: List[dict],
        pnl_snapshots: List[PnlDailySnapshot],
    ) -> None:
        self.health_refresh_button.setEnabled(True)
        self.health_checked_at_label.setText(f"Checked {snapshot.checked_at}")
        counts = {
            level: sum(check.level == level for check in snapshot.checks)
            for level in HealthLevel
        }
        self.health_summary_label.setText(
            f"Overall: {snapshot.overall_level.value} - "
            f"{counts[HealthLevel.HEALTHY]} healthy, "
            f"{counts[HealthLevel.WARNING]} warning, "
            f"{counts[HealthLevel.CRITICAL]} critical, "
            f"{counts[HealthLevel.UNKNOWN]} unknown."
        )
        self.health_checks_table.setRowCount(len(snapshot.checks))
        for row, check in enumerate(snapshot.checks):
            values = [
                check.component,
                check.level.value,
                check.summary,
                check.detail,
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value or ""))
                item.setToolTip(str(value or ""))
                if column == 1:
                    item.setBackground(self._HEALTH_COLORS[check.level])
                    item.setTextAlignment(Qt.AlignCenter)
                self.health_checks_table.setItem(row, column, item)
        self._populate_health_journal(events)
        self._pnl_snapshots = pnl_snapshots
        self._refresh_pnl_dashboard_view()

    def _on_health_probe_failed(self, message: str) -> None:
        self.health_refresh_button.setEnabled(True)
        self.health_checked_at_label.setText("Health check failed")
        self.health_summary_label.setText(
            f"The read-only health probe failed: {message}"
        )

    def _populate_health_journal(self, events: List[dict]) -> None:
        self.health_journal_table.setRowCount(len(events))
        for row, event in enumerate(events):
            quantity = event.get("quantity")
            price = event.get("price")
            qty_price = " / ".join(
                part
                for part in (
                    f"{quantity} sh" if quantity is not None else "",
                    f"{price}" if price is not None else "",
                )
                if part
            )
            detail = str(event.get("reason") or "")
            payload = event.get("payload")
            if payload:
                payload_text = json.dumps(
                    payload, ensure_ascii=False, sort_keys=True, separators=(", ", ": ")
                )
                detail = f"{detail} | {payload_text}" if detail else payload_text
            values = [
                event.get("timestamp", ""),
                event.get("event_type", ""),
                event.get("symbol", ""),
                event.get("strategy_id", ""),
                event.get("account", ""),
                qty_price,
                detail,
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value or ""))
                item.setToolTip(str(value or ""))
                self.health_journal_table.setItem(row, column, item)
