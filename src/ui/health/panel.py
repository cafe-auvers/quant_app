"""Production health, daily trading summary, and P&L dashboard."""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import replace
from typing import List, Optional

from PyQt5.QtCore import QDate, QThread, Qt, QUrl, pyqtSignal
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDateEdit,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
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

from src.core.exit_policy import market_session_date
from src.services.daily_trading_summary import (
    DailyTradingSummary,
    build_daily_trading_summary,
)
from src.services.health import (
    HealthContext,
    HealthLevel,
    HealthSnapshot,
    collect_health_snapshot,
)
from src.services.order_ledger import load_order_ledger
from src.services.pnl_history import (
    BrokerRealizedPnlSeries,
    PnlDailySnapshot,
    compute_unrealized_pnl_usd,
    load_pnl_history,
    record_daily_pnl_snapshot,
)
from src.services.repository_sync import inspect_repository
from src.ui.charts.render_assets import lightweight_charts_base_path
from src.ui.health.pnl_chart import (
    SERIES_COMBINED,
    SERIES_REALIZED,
    SERIES_UNREALIZED,
    UNIT_KRW,
    UNIT_PCT,
    UNIT_USD,
    VIEW_CUMULATIVE,
    VIEW_DAILY,
    generate_pnl_chart_html,
    pnl_chart_points,
)
from src.utils.config import ROOT_DIR

logger = logging.getLogger(__name__)


class HealthProbeWorker(QThread):
    completed = pyqtSignal(object, list, list)
    failed = pyqtSignal(str)
    repository_checked = pyqtSignal(object)

    def __init__(
        self,
        context: HealthContext,
        *,
        unrealized_usd_today: float = 0.0,
        fx_rate_today: Optional[float] = None,
        capital_base_usd_today: Optional[float] = None,
        broker_realized_series: Optional[List[BrokerRealizedPnlSeries]] = None,
    ) -> None:
        super().__init__()
        self.context = context
        self.unrealized_usd_today = unrealized_usd_today
        self.fx_rate_today = fx_rate_today
        self.capital_base_usd_today = capital_base_usd_today
        self.broker_realized_series = list(broker_realized_series or [])

    def run(self) -> None:
        try:
            try:
                orders = tuple(load_order_ledger())
                context = replace(self.context, orders=orders, order_ledger_error="")
            except Exception as exc:
                orders = self.context.orders
                context = replace(self.context, order_ledger_error=str(exc))
            repository_status = inspect_repository(ROOT_DIR, fetch=True)
            self.repository_checked.emit(repository_status)
            context = replace(context, repository_status=repository_status)
            snapshot = collect_health_snapshot(context)
            pnl_snapshots: List[PnlDailySnapshot] = []
            try:
                pnl_snapshots = record_daily_pnl_snapshot(
                    orders,
                    unrealized_usd_today=self.unrealized_usd_today,
                    fx_rate_today=self.fx_rate_today,
                    capital_base_usd_today=self.capital_base_usd_today,
                    broker_realized_series=self.broker_realized_series,
                )
            except Exception:
                logger.exception("Failed to build daily P&L snapshot history")
            self.completed.emit(snapshot, [], pnl_snapshots)
        except Exception as exc:
            self.failed.emit(str(exc))


class DailySummaryWorker(QThread):
    completed = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, engine, session_date: dt.date) -> None:
        super().__init__()
        self.engine = engine
        self.session_date = session_date

    def run(self) -> None:
        try:
            self.completed.emit(
                build_daily_trading_summary(self.engine, self.session_date)
            )
        except Exception as exc:
            logger.exception("Failed to build daily trading summary")
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
    _PNL_VIEW_ITEMS = [
        ("Cumulative", VIEW_CUMULATIVE),
        ("Daily change", VIEW_DAILY),
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
            "Open this tab or click Refresh Health to run non-destructive system checks."
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
        self.health_tabs.addTab(self._build_daily_summary_tab(), "Daily Summary")
        self.health_tabs.addTab(self._build_pnl_dashboard_tab(), "P&L Dashboard")
        layout.addWidget(self.health_tabs, 3)

    def _build_daily_summary_tab(self) -> QWidget:
        summary_tab = QWidget()
        layout = QVBoxLayout(summary_tab)

        controls = QHBoxLayout()
        controls.addWidget(QLabel("Trading date:"))
        self.health_daily_date_edit = QDateEdit()
        self.health_daily_date_edit.setCalendarPopup(True)
        self.health_daily_date_edit.setDisplayFormat("yyyy-MM-dd")
        today = market_session_date()
        self.health_daily_date_edit.setDate(QDate(today.year, today.month, today.day))
        self.health_daily_date_edit.dateChanged.connect(self._refresh_daily_summary)
        controls.addWidget(self.health_daily_date_edit)
        self.health_daily_today_button = QPushButton("Today")
        self.health_daily_today_button.clicked.connect(self._select_daily_summary_today)
        controls.addWidget(self.health_daily_today_button)
        controls.addStretch()
        self.health_daily_status_label = QLabel("Loading daily trading summary...")
        self.health_daily_status_label.setWordWrap(True)
        controls.addWidget(self.health_daily_status_label, 1)
        layout.addLayout(controls)

        self.health_daily_tabs = QTabWidget()

        self.health_daily_plan_table = QTableWidget(0, 6)
        self.health_daily_plan_table.setHorizontalHeaderLabels(
            ["Plan source", "Symbol", "Breakout", "Planned qty", "Outcome", "Why / detail"]
        )
        self._configure_daily_table(self.health_daily_plan_table, stretch_column=5)
        self.health_daily_tabs.addTab(self.health_daily_plan_table, "Buy Today plan")

        self.health_daily_positions_table = QTableWidget(0, 4)
        self.health_daily_positions_table.setHorizontalHeaderLabels(
            ["Symbol", "Quantity", "Average entry", "Status"]
        )
        self._configure_daily_table(self.health_daily_positions_table, stretch_column=3)
        self.health_daily_tabs.addTab(self.health_daily_positions_table, "Open positions")

        self.health_daily_activity_table = QTableWidget(0, 6)
        self.health_daily_activity_table.setHorizontalHeaderLabels(
            ["Time", "Symbol", "Activity", "Quantity", "Price", "Status / reason"]
        )
        self._configure_daily_table(self.health_daily_activity_table, stretch_column=5)
        self.health_daily_tabs.addTab(self.health_daily_activity_table, "Orders & sells")

        self.health_daily_orb_rejections_table = QTableWidget(0, 14)
        self.health_daily_orb_rejections_table.setHorizontalHeaderLabels(
            [
                "Symbol", "Risk %", "Window", "Result", "ORB status",
                "ORB high", "Breakout", "Buffered breakout", "Entry trigger",
                "Stop / ORB low", "Shares", "Capital %", "Stop / ADR", "Why rejected",
            ]
        )
        self._configure_daily_table(
            self.health_daily_orb_rejections_table, stretch_column=13
        )
        self.health_daily_tabs.addTab(
            self.health_daily_orb_rejections_table, "Rejected ORB combinations"
        )
        layout.addWidget(self.health_daily_tabs)
        return summary_tab

    @staticmethod
    def _configure_daily_table(table: QTableWidget, *, stretch_column: int) -> None:
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.setAlternatingRowColors(True)
        header = table.horizontalHeader()
        for column in range(table.columnCount()):
            header.setSectionResizeMode(column, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(stretch_column, QHeaderView.Stretch)

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

        controls.addWidget(QLabel("View:"))
        self.health_pnl_view_combo = QComboBox()
        for label, _value in self._PNL_VIEW_ITEMS:
            self.health_pnl_view_combo.addItem(label)
        self.health_pnl_view_combo.currentIndexChanged.connect(
            self._refresh_pnl_dashboard_view
        )
        controls.addWidget(self.health_pnl_view_combo)

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
            self.health_pnl_chart_view.setMinimumHeight(320)
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

        # Show durable history immediately. The asynchronous health refresh
        # will then update it with the latest KIS/account values.
        self._pnl_snapshots = load_pnl_history()
        if self._pnl_snapshots:
            self._refresh_pnl_dashboard_view()
        else:
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

    def _selected_pnl_view(self) -> str:
        index = self.health_pnl_view_combo.currentIndex()
        if 0 <= index < len(self._PNL_VIEW_ITEMS):
            return self._PNL_VIEW_ITEMS[index][1]
        return VIEW_CUMULATIVE

    def _set_pnl_chart_html(self, html_content: str) -> None:
        asset_base = str(lightweight_charts_base_path().resolve()) + "/"
        self.health_pnl_chart_view.setHtml(
            html_content, QUrl.fromLocalFile(asset_base)
        )

    def _render_pnl_dashboard_placeholder(self) -> None:
        if isinstance(self.health_pnl_chart_view, QTextEdit):
            self.health_pnl_chart_view.setPlainText(
                "No P&L history yet. It starts building the next time this tab refreshes."
            )
        elif QWebEngineView is not None:
            self._set_pnl_chart_html(generate_pnl_chart_html([]))
        self.health_pnl_table.setRowCount(0)

    def _refresh_pnl_dashboard_view(self, *_args) -> None:
        snapshots = self.__dict__.get("_pnl_snapshots") or []
        series = self._selected_pnl_series()
        unit = self._selected_pnl_unit()
        view = self._selected_pnl_view()

        if not snapshots:
            self._render_pnl_dashboard_placeholder()
            return

        if isinstance(self.health_pnl_chart_view, QTextEdit):
            points = pnl_chart_points(
                snapshots, series=series, unit=unit, view=view
            )
            lines = [f"{point['time']}: {point['value']:,.2f}" for point in points]
            self.health_pnl_chart_view.setPlainText(
                "\n".join(lines) or "No data yet for this unit."
            )
        elif QWebEngineView is not None:
            html_content = generate_pnl_chart_html(
                snapshots, series=series, unit=unit, view=view
            )
            self._set_pnl_chart_html(html_content)

        self._populate_pnl_table(snapshots, unit=unit, view=view)

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
            f"day this dashboard started tracking. Realized source: "
            f"{latest.realized_source}."
        )

    def _populate_pnl_table(
        self,
        snapshots: List[PnlDailySnapshot],
        *,
        unit: str,
        view: str,
    ) -> None:
        unit_suffix = {UNIT_USD: "$", UNIT_KRW: "₩", UNIT_PCT: "%"}.get(unit, "")
        view_label = "Cumulative" if view == VIEW_CUMULATIVE else "Daily change"
        self.health_pnl_table.setHorizontalHeaderLabels(
            [
                "Date",
                f"Realized ({view_label}, {unit_suffix})",
                f"Unrealized ({view_label}, {unit_suffix})",
                f"Total ({view_label}, {unit_suffix})",
            ]
        )
        realized_points = {
            point["time"]: point["value"]
            for point in pnl_chart_points(
                snapshots, series=SERIES_REALIZED, unit=unit, view=view
            )
        }
        unrealized_points = {
            point["time"]: point["value"]
            for point in pnl_chart_points(
                snapshots, series=SERIES_UNREALIZED, unit=unit, view=view
            )
        }
        total_points = {
            point["time"]: point["value"]
            for point in pnl_chart_points(
                snapshots, series=SERIES_COMBINED, unit=unit, view=view
            )
        }
        ordered = list(reversed(snapshots))
        self.health_pnl_table.setRowCount(len(ordered))
        for row, snapshot in enumerate(ordered):
            realized = realized_points.get(snapshot.date)
            unrealized = unrealized_points.get(snapshot.date)
            total = total_points.get(snapshot.date)
            values = [
                snapshot.date,
                f"{realized:,.2f}" if realized is not None else "—",
                f"{unrealized:,.2f}" if unrealized is not None else "—",
                f"{total:,.2f}" if total is not None else "—",
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
        fx_rate = 0.0
        if hasattr(self, "usd_krw_rate_input"):
            fx_rate = self._parse_float(self.usd_krw_rate_input, 0.0)
        capital_base = 0.0
        if hasattr(self, "account_size_input"):
            capital_base = self._parse_float(self.account_size_input, 0.0)

        prices = dict(self.__dict__.get("latest_intraday_prices", {}) or {})
        unrealized_usd_today = compute_unrealized_pnl_usd(positions, prices)
        broker_unrealized_usd = 0.0
        broker_unrealized_available = False
        broker_realized_series: List[BrokerRealizedPnlSeries] = []
        for key, snapshot in (
            self.__dict__.get("kis_account_snapshots", {}) or {}
        ).items():
            if not isinstance(key, tuple) or len(key) != 2:
                continue
            environment, account_no = key
            if str(environment).upper() != "PROD" or not isinstance(snapshot, dict):
                continue
            overseas = snapshot.get("overseas")
            if isinstance(overseas, dict):
                broker_unrealized_available = True
                for holding in overseas.get("holdings") or []:
                    try:
                        broker_unrealized_usd += float(
                            holding.get("profit_loss", 0.0) or 0.0
                        )
                    except (AttributeError, TypeError, ValueError):
                        continue
                actual = overseas.get("realized_pnl") or {}
                if (
                    isinstance(actual, dict)
                    and actual.get("complete") is True
                    and str(actual.get("currency") or "").upper() == "USD"
                    and isinstance(actual.get("daily_usd"), dict)
                ):
                    broker_realized_series.append(
                        BrokerRealizedPnlSeries(
                            account_no=str(account_no),
                            start_date=str(actual.get("start_date") or ""),
                            end_date=str(actual.get("end_date") or ""),
                            daily_usd=actual["daily_usd"],
                        )
                    )
            domestic = snapshot.get("domestic")
            if isinstance(domestic, dict) and fx_rate > 0:
                broker_unrealized_available = True
                try:
                    broker_unrealized_usd += float(
                        domestic.get("summary", {}).get(
                            "evaluation_profit_loss_krw", 0.0
                        )
                        or 0.0
                    ) / fx_rate
                except (AttributeError, TypeError, ValueError):
                    pass
        if broker_unrealized_available:
            unrealized_usd_today = broker_unrealized_usd

        return (
            unrealized_usd_today,
            fx_rate if fx_rate > 0 else None,
            capital_base if capital_base > 0 else None,
            broker_realized_series,
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
        hourly_scope_loader = getattr(self, "_relevant_hourly_symbols", None)
        mirror_hourly_tickers = (
            hourly_scope_loader() if callable(hourly_scope_loader) else None
        )

        state_sync_role = self.__dict__.get("state_sync_role")
        is_main_device = bool(state_sync_role and state_sync_role.is_main)
        last_reconcile = self.__dict__.get("_last_successful_reconcile_at")
        lease_age_seconds = None
        if is_main_device and last_reconcile is not None:
            lease_age_seconds = (
                dt.datetime.now(dt.timezone.utc) - last_reconcile
            ).total_seconds()
        handoff_worker = self.__dict__.get("handoff_reconciliation_worker")
        market_data_metrics = None
        buyboard_worker = self.__dict__.get("_buyboard_runtime_worker")
        runtime = getattr(buyboard_worker, "runtime", None)
        market_data = getattr(runtime, "market_data", None)
        metrics_reader = getattr(market_data, "health_metrics", None)
        if callable(metrics_reader):
            try:
                market_data_metrics = metrics_reader()
            except Exception:
                logger.exception("Failed to read KIS market-data health metrics")
        request_scheduler_metrics = None
        scheduler = getattr(buyboard_worker, "request_scheduler", None)
        scheduler_metrics_reader = getattr(scheduler, "metrics", None)
        if callable(scheduler_metrics_reader):
            try:
                request_scheduler_metrics = scheduler_metrics_reader()
            except Exception:
                logger.exception("Failed to read KIS request-scheduler metrics")
        return HealthContext(
            db_source=str(self.__dict__.get("db_engine_source", "none")),
            db_initializing=bool(self.__dict__.get("db_initializing", False)),
            pc_database_ready=bool(self.__dict__.get("_pc_database_ready", False)),
            pc_database_engine=pc_database_engine,
            mirror_engine=self.__dict__.get("_local_mirror_engine"),
            mirror_tickers=mirror_tickers,
            mirror_hourly_tickers=mirror_hourly_tickers,
            operational_store_configured=(
                "operational_db_engine" in self.__dict__
            ),
            operational_store_engine=self.__dict__.get(
                "operational_db_engine"
            ),
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
            handoff_reconciliation_required=bool(
                self.__dict__.get("_handoff_reconciliation_required", False)
            ),
            handoff_blocked_symbols=tuple(
                self.__dict__.get("_last_handoff_blocked_symbols", ())
            ),
            market_data_metrics=market_data_metrics,
            request_scheduler_metrics=request_scheduler_metrics,
        )

    def refresh_health_panel(self, *args) -> None:
        worker = self.__dict__.get("_health_probe_worker")
        if worker is not None and worker.isRunning():
            return
        self.health_refresh_button.setEnabled(False)
        self.health_checked_at_label.setText("Checking...")
        (
            unrealized_usd_today,
            fx_rate_today,
            capital_base_usd_today,
            broker_realized_series,
        ) = self._pnl_inputs()
        worker = HealthProbeWorker(
            self._health_context(),
            unrealized_usd_today=unrealized_usd_today,
            fx_rate_today=fx_rate_today,
            capital_base_usd_today=capital_base_usd_today,
            broker_realized_series=broker_realized_series,
        )
        self._health_probe_worker = worker
        worker.completed.connect(self._on_health_probe_completed)
        worker.failed.connect(self._on_health_probe_failed)
        apply_repository_status = getattr(self, "_apply_repository_status", None)
        if callable(apply_repository_status):
            worker.repository_checked.connect(apply_repository_status)
        self._track_worker("_health_probe_worker", worker)
        worker.start()
        self._refresh_daily_summary()

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
        self._pnl_snapshots = pnl_snapshots
        self._refresh_pnl_dashboard_view()
        self._refresh_daily_summary()

    def _on_health_probe_failed(self, message: str) -> None:
        self.health_refresh_button.setEnabled(True)
        self.health_checked_at_label.setText("Health check failed")
        self.health_summary_label.setText(
            f"The read-only health probe failed: {message}"
        )

    def _selected_daily_summary_date(self) -> dt.date:
        selected = self.health_daily_date_edit.date()
        return dt.date(selected.year(), selected.month(), selected.day())

    def _select_daily_summary_today(self) -> None:
        today = market_session_date()
        self.health_daily_date_edit.setDate(QDate(today.year, today.month, today.day))
        self._refresh_daily_summary()

    def _refresh_daily_summary(self, *_args) -> None:
        if not hasattr(self, "health_daily_date_edit"):
            return
        worker = self.__dict__.get("_daily_summary_worker")
        if worker is not None and worker.isRunning():
            self._daily_summary_refresh_pending = True
            return
        engine_reader = getattr(self, "_execution_state_engine", None)
        engine = engine_reader() if callable(engine_reader) else self.__dict__.get(
            "operational_db_engine"
        )
        if engine is None:
            self.health_daily_status_label.setText(
                "Daily summary unavailable: the shared Kanban database is offline."
            )
            self._clear_daily_summary_tables()
            return
        selected_date = self._selected_daily_summary_date()
        self.health_daily_status_label.setText(
            f"Loading {selected_date.isoformat()} from the shared trading ledger..."
        )
        worker = DailySummaryWorker(engine, selected_date)
        self._daily_summary_worker = worker
        self._daily_summary_refresh_pending = False
        worker.completed.connect(self._on_daily_summary_completed)
        worker.failed.connect(self._on_daily_summary_failed)
        worker.finished.connect(self._on_daily_summary_worker_finished)
        self._track_worker("_daily_summary_worker", worker)
        worker.start()

    def _on_daily_summary_completed(self, summary: DailyTradingSummary) -> None:
        if summary.session_date != self._selected_daily_summary_date():
            self._daily_summary_refresh_pending = True
            return
        self.health_daily_status_label.setText(summary.note)
        self._populate_daily_plan(summary)
        self._populate_daily_positions(summary)
        self._populate_daily_activity(summary)
        self._populate_daily_orb_rejections(summary)
        self.health_daily_tabs.setTabText(
            0, f"Buy Today plan ({len(summary.plan_items)})"
        )
        self.health_daily_tabs.setTabText(
            1, f"Open positions ({len(summary.positions)})"
        )
        sell_count = sum(
            "SELL" in activity.activity for activity in summary.activities
        )
        self.health_daily_tabs.setTabText(
            2, f"Orders & sells ({sell_count} sells)"
        )
        self.health_daily_tabs.setTabText(
            3,
            "Rejected ORB combinations "
            f"({len(summary.rejected_orb_combinations)})",
        )

    def _on_daily_summary_failed(self, message: str) -> None:
        self.health_daily_status_label.setText(f"Daily summary failed: {message}")
        self._clear_daily_summary_tables()

    def _on_daily_summary_worker_finished(self) -> None:
        if self.__dict__.pop("_daily_summary_refresh_pending", False):
            self._refresh_daily_summary()

    def _clear_daily_summary_tables(self) -> None:
        self.health_daily_plan_table.setRowCount(0)
        self.health_daily_positions_table.setRowCount(0)
        self.health_daily_activity_table.setRowCount(0)
        self.health_daily_orb_rejections_table.setRowCount(0)

    @staticmethod
    def _set_daily_cell(table: QTableWidget, row: int, column: int, value) -> None:
        text = str(value if value not in (None, "") else "—")
        item = QTableWidgetItem(text)
        item.setToolTip(text)
        table.setItem(row, column, item)

    def _populate_daily_plan(self, summary: DailyTradingSummary) -> None:
        table = self.health_daily_plan_table
        table.setRowCount(len(summary.plan_items))
        for row, item in enumerate(summary.plan_items):
            why = " — ".join(
                part for part in (item.reason_category, item.reason) if part
            )
            values = (
                item.source,
                item.symbol,
                f"${item.breakout_price:,.2f}" if item.breakout_price else "—",
                item.planned_quantity or "—",
                item.outcome,
                why or "—",
            )
            for column, value in enumerate(values):
                self._set_daily_cell(table, row, column, value)

    def _populate_daily_positions(self, summary: DailyTradingSummary) -> None:
        table = self.health_daily_positions_table
        table.setRowCount(len(summary.positions))
        for row, item in enumerate(summary.positions):
            values = (
                item.symbol,
                item.quantity,
                f"${item.average_price:,.2f}" if item.average_price else "—",
                item.status,
            )
            for column, value in enumerate(values):
                self._set_daily_cell(table, row, column, value)

    def _populate_daily_activity(self, summary: DailyTradingSummary) -> None:
        table = self.health_daily_activity_table
        table.setRowCount(len(summary.activities))
        for row, item in enumerate(summary.activities):
            values = (
                item.occurred_at,
                item.symbol,
                item.activity,
                item.quantity,
                f"${item.price:,.2f}" if item.price else "—",
                " — ".join(part for part in (item.status, item.reason) if part),
            )
            for column, value in enumerate(values):
                self._set_daily_cell(table, row, column, value)

    def _populate_daily_orb_rejections(
        self, summary: DailyTradingSummary
    ) -> None:
        table = self.health_daily_orb_rejections_table
        rows = summary.rejected_orb_combinations
        table.setRowCount(len(rows))

        def price(value) -> str:
            return f"${value:,.2f}" if value is not None else "—"

        def percent(value) -> str:
            return f"{value:.2f}%" if value is not None else "—"

        for row, item in enumerate(rows):
            values = (
                item.symbol,
                f"{item.risk_percent * 100.0:.2f}%",
                item.window,
                item.classification,
                item.status.replace("_", " "),
                price(item.orb_high),
                price(item.breakout_price),
                price(item.breakout_trigger),
                price(item.entry_trigger),
                price(item.stop_price),
                item.shares or "—",
                percent(item.capital_percent),
                percent(item.stop_adr),
                item.reason,
            )
            for column, value in enumerate(values):
                self._set_daily_cell(table, row, column, value)
