"""Read-only PyQt presentation for the Market Pulse dashboard."""

from __future__ import annotations

import datetime as dt
import math
from typing import Sequence
from zoneinfo import ZoneInfo

from PyQt5.QtCore import QAbstractTableModel, QModelIndex, QRectF, Qt, QThread, pyqtSignal
from PyQt5.QtGui import QColor, QFont, QPainter
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStyledItemDelegate,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from src.core.market_pulse import (
    SECTION_LABELS,
    SECTION_ORDER,
    MarketPulseRow,
    MarketPulseSnapshot,
)
from src.services.market_pulse import (
    MarketPulseRefreshInProgress,
    MarketPulseService,
)
from src.utils.market_calendar import expected_latest_market_data_date


KST_ZONE = ZoneInfo("Asia/Seoul")


class MarketPulseTableModel(QAbstractTableModel):
    """Sortable table model that never mutates the daily rank."""

    COLUMNS = (
        ("Rank", "rank"),
        ("Name", "display_name"),
        ("Ticker", "ticker"),
        ("Close", "close"),
        ("Intraday %", "intraday_return"),
        ("Daily %", "daily_return"),
        ("Weekly %", "weekly_return"),
        ("Monthly %", "monthly_return"),
        ("% Above 52W Low", "pct_above_52w_low"),
        ("% Below 52W High", "pct_below_52w_high"),
        ("stock1", "stock1"),
        ("stock2", "stock2"),
        ("stock3", "stock3"),
        ("stock4", "stock4"),
    )
    HEADER_TOOLTIPS = (
        "Daily performance rank. This rank stays fixed when another column is sorted.",
        "Configured market segment, sector, industry, or theme name.",
        "ETF proxy ticker.",
        "Latest completed-session closing price.",
        "Latest yfinance 5-minute close / latest completed-session close - 1.",
        "Reference price / prior session - 1.",
        "Reference price / five sessions earlier - 1.",
        "Reference price / 21 sessions earlier - 1.",
        "Reference price / lowest reference price in the last 252 sessions - 1.",
        "Reference price / highest reference price in the last 252 sessions - 1. Values are zero near the high and negative below it.",
        "Strongest eligible reported ETF holding by 63-session return relative to SPY. Click to open its TradingView chart.",
        "Second-strongest eligible reported ETF holding by 63-session return relative to SPY. Click to open its TradingView chart.",
        "Third-strongest eligible reported ETF holding by 63-session return relative to SPY. Click to open its TradingView chart.",
        "Fourth-strongest eligible reported ETF holding by 63-session return relative to SPY. Click to open its TradingView chart.",
    )

    def __init__(self, rows: Sequence[MarketPulseRow] = (), parent=None) -> None:
        super().__init__(parent)
        self._rows = list(rows)
        self.sort_column = 5
        self.sort_order = Qt.DescendingOrder
        self.sort(self.sort_column, self.sort_order)

    @property
    def rows(self) -> tuple[MarketPulseRow, ...]:
        return tuple(self._rows)

    def replace_rows(self, rows: Sequence[MarketPulseRow]) -> None:
        self.beginResetModel()
        self._rows = list(rows)
        self.endResetModel()
        self.sort(self.sort_column, self.sort_order)

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.COLUMNS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal and 0 <= section < len(self.COLUMNS):
            if role == Qt.DisplayRole:
                return self.COLUMNS[section][0]
            if role in (Qt.ToolTipRole, Qt.AccessibleDescriptionRole):
                return self.HEADER_TOOLTIPS[section]
            if role == Qt.TextAlignmentRole:
                return Qt.AlignCenter
        return super().headerData(section, orientation, role)

    @staticmethod
    def _display_value(column: int, value) -> str:
        if value is None or (isinstance(value, float) and not math.isfinite(value)):
            return "—"
        if column == 0:
            return str(int(value))
        if column == 3:
            return f"{float(value):,.2f}"
        if column in (4, 5, 6, 7):
            return f"{float(value):+.1%}"
        if column in (8, 9):
            return f"{float(value):+.2%}"
        return str(value)

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self._rows):
            return None
        row = self._rows[index.row()]
        field = self.COLUMNS[index.column()][1]
        value = getattr(row, field)
        if role == Qt.DisplayRole:
            return self._display_value(index.column(), value)
        if role == Qt.UserRole:
            return value
        if role == Qt.TextAlignmentRole:
            if index.column() == 1:
                return int(Qt.AlignLeft | Qt.AlignVCenter)
            return int(Qt.AlignRight | Qt.AlignVCenter) if index.column() >= 3 else int(Qt.AlignCenter)
        if role == Qt.ForegroundRole:
            if row.status not in {"available", "cached"}:
                return QColor("#8b91a1")
            if index.column() in (4, 5, 6, 7) and value is not None:
                number = float(value)
                if number > 0.0005:
                    return QColor("#137333")
                if number < -0.0005:
                    return QColor("#c62828")
        if role == Qt.FontRole and index.column() >= 2:
            font = QFont("Consolas")
            font.setStyleHint(QFont.Monospace)
            if index.column() >= 10 and value:
                font.setUnderline(True)
            return font
        if role == Qt.ForegroundRole and index.column() >= 10 and value:
            return QColor("#1565c0")
        if role in (Qt.ToolTipRole, Qt.AccessibleDescriptionRole):
            metric_help = self.HEADER_TOOLTIPS[index.column()]
            state = "Available"
            if row.status != "available":
                state = row.status.capitalize()
                if row.error:
                    state += f": {row.error}"
            session = (
                row.source_session_date.isoformat()
                if row.source_session_date is not None
                else "unavailable"
            )
            return f"{metric_help}\nRow status: {state}\nSource session: {session}"
        return None

    def sort(self, column: int, order=Qt.AscendingOrder) -> None:
        if not 0 <= column < len(self.COLUMNS):
            return
        self.sort_column = column
        self.sort_order = order
        field = self.COLUMNS[column][1]
        available = []
        missing = []
        for row in self._rows:
            value = getattr(row, field)
            if value is None or (isinstance(value, float) and not math.isfinite(value)):
                missing.append(row)
            else:
                available.append(row)

        numeric = column in (0, 3, 4, 5, 6, 7, 8, 9)
        if numeric:
            if order == Qt.DescendingOrder:
                available.sort(key=lambda row: (-float(getattr(row, field)), row.ticker))
            else:
                available.sort(key=lambda row: (float(getattr(row, field)), row.ticker))
        else:
            available.sort(
                key=lambda row: (str(getattr(row, field)).casefold(), row.ticker),
                reverse=order == Qt.DescendingOrder,
            )
        missing.sort(key=lambda row: row.ticker)
        self.layoutAboutToBeChanged.emit()
        self._rows = available + missing
        self.layoutChanged.emit()


class MarketPulseMetricDelegate(QStyledItemDelegate):
    """Paint rank gradients and safely clamped 52-week data bars."""

    def paint(self, painter: QPainter, option, index) -> None:
        value = index.data(Qt.UserRole)
        if index.column() == 0 and value is not None:
            total = max(1, index.model().rowCount() - 1)
            fraction = min(1.0, max(0.0, (float(value) - 1.0) / total))
            start = QColor("#d8f3dc")
            end = QColor("#f6d1d1")
            color = QColor(
                round(start.red() + (end.red() - start.red()) * fraction),
                round(start.green() + (end.green() - start.green()) * fraction),
                round(start.blue() + (end.blue() - start.blue()) * fraction),
            )
            painter.save()
            painter.fillRect(option.rect.adjusted(2, 2, -2, -2), color)
            painter.restore()
        elif index.column() in (8, 9) and value is not None:
            number = float(value)
            magnitude = max(0.0, number) if index.column() == 8 else abs(min(0.0, number))
            width = min(1.0, magnitude) * max(0, option.rect.width() - 8)
            if width > 0:
                bar_color = QColor(46, 160, 67, 58) if index.column() == 8 else QColor(214, 39, 40, 55)
                painter.save()
                painter.setPen(Qt.NoPen)
                painter.setBrush(bar_color)
                painter.drawRoundedRect(
                    QRectF(option.rect.left() + 4, option.rect.top() + 5, width, option.rect.height() - 10),
                    3,
                    3,
                )
                painter.restore()
        super().paint(painter, option, index)


class MarketPulseRefreshWorker(QThread):
    snapshot_ready = pyqtSignal(object)
    error_occurred = pyqtSignal(str)

    def __init__(self, service: MarketPulseService, parent=None) -> None:
        super().__init__(parent)
        self.service = service

    def run(self) -> None:
        try:
            self.snapshot_ready.emit(self.service.refresh())
        except MarketPulseRefreshInProgress as exc:
            self.error_occurred.emit(str(exc))
        except Exception as exc:
            self.error_occurred.emit(str(exc) or type(exc).__name__)


class MarketPulseIntradayRefreshWorker(QThread):
    snapshot_ready = pyqtSignal(object)
    error_occurred = pyqtSignal(str)

    def __init__(self, service: MarketPulseService, parent=None) -> None:
        super().__init__(parent)
        self.service = service

    def run(self) -> None:
        try:
            self.snapshot_ready.emit(self.service.refresh_intraday())
        except MarketPulseRefreshInProgress as exc:
            self.error_occurred.emit(str(exc))
        except Exception as exc:
            self.error_occurred.emit(str(exc) or type(exc).__name__)


class MarketPulseMixin:
    """Build and coordinate the isolated Market Pulse navigation page."""

    def _build_market_pulse_tab(self) -> None:
        if not hasattr(self, "market_pulse_service"):
            self.market_pulse_service = MarketPulseService()
        self.market_pulse_models = {}
        self.market_pulse_tables = {}
        root_layout = QVBoxLayout(self.market_pulse_widget)
        root_layout.setContentsMargins(16, 14, 16, 14)
        root_layout.setSpacing(10)

        header = QHBoxLayout()
        title_box = QVBoxLayout()
        heading = QLabel("Market Awareness")
        heading.setObjectName("marketPulseHeading")
        heading.setStyleSheet("font-size: 22px; font-weight: 700; color: #131722;")
        subtitle = QLabel(
            "Market, sector, and thematic leadership. Components are ordered by 63-session relative strength versus SPY."
        )
        subtitle.setWordWrap(True)
        subtitle.setStyleSheet("color: #5d606b;")
        title_box.addWidget(heading)
        title_box.addWidget(subtitle)
        header.addLayout(title_box, 1)

        status_box = QVBoxLayout()
        status_box.setAlignment(Qt.AlignRight | Qt.AlignTop)
        self.market_pulse_as_of_label = QLabel("As of: —")
        self.market_pulse_as_of_label.setObjectName("marketPulseAsOfLabel")
        self.market_pulse_updated_label = QLabel("Last updated: —")
        self.market_pulse_updated_label.setStyleSheet("color: #5d606b;")
        self.market_pulse_refresh_button = QPushButton("Refresh")
        self.market_pulse_refresh_button.setObjectName("marketPulseRefreshButton")
        self.market_pulse_refresh_button.setToolTip(
            "Refresh completed-session metrics, ETF holdings, relative-strength order, and intraday values."
        )
        self.market_pulse_refresh_button.clicked.connect(self.refresh_market_pulse)
        self.market_pulse_intraday_refresh_button = QPushButton("Refresh Intraday")
        self.market_pulse_intraday_refresh_button.setObjectName(
            "marketPulseIntradayRefreshButton"
        )
        self.market_pulse_intraday_refresh_button.setToolTip(
            "Refresh only Intraday % from yfinance; keep daily, weekly, monthly, and rank unchanged."
        )
        self.market_pulse_intraday_refresh_button.clicked.connect(
            self.refresh_market_pulse_intraday
        )
        status_box.addWidget(self.market_pulse_as_of_label, 0, Qt.AlignRight)
        status_box.addWidget(self.market_pulse_updated_label, 0, Qt.AlignRight)
        status_box.addWidget(self.market_pulse_refresh_button, 0, Qt.AlignRight)
        status_box.addWidget(
            self.market_pulse_intraday_refresh_button, 0, Qt.AlignRight
        )
        header.addLayout(status_box)
        root_layout.addLayout(header)

        self.market_pulse_message_label = QLabel()
        self.market_pulse_message_label.setObjectName("marketPulseMessageLabel")
        self.market_pulse_message_label.setWordWrap(True)
        self.market_pulse_message_label.setVisible(False)
        root_layout.addWidget(self.market_pulse_message_label)

        self.market_pulse_loading_bar = QProgressBar()
        self.market_pulse_loading_bar.setRange(0, 0)
        self.market_pulse_loading_bar.setFixedHeight(6)
        self.market_pulse_loading_bar.setTextVisible(False)
        self.market_pulse_loading_bar.setVisible(False)
        root_layout.addWidget(self.market_pulse_loading_bar)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(14)
        for section in SECTION_ORDER:
            section_label = QLabel(SECTION_LABELS[section])
            section_label.setObjectName(f"marketPulseSection_{section}")
            section_label.setStyleSheet(
                "font-size: 16px; font-weight: 700; color: #131722; padding-top: 4px;"
            )
            content_layout.addWidget(section_label)
            model = MarketPulseTableModel(parent=self.market_pulse_widget)
            table = self._create_market_pulse_table(section, model)
            self.market_pulse_models[section] = model
            self.market_pulse_tables[section] = table
            content_layout.addWidget(table)
        content_layout.addStretch(1)
        scroll.setWidget(content)
        root_layout.addWidget(scroll, 1)
        self.market_pulse_scroll_area = scroll

        try:
            cached = self.market_pulse_service.load_cached_snapshot()
        except Exception as exc:
            cached = None
            self._show_market_pulse_message(
                f"Cached Market Pulse data could not be loaded: {exc}", "error"
            )
        if cached is not None:
            self._render_market_pulse_snapshot(cached)
        else:
            self._show_market_pulse_message(
                "No cached snapshot yet. Select Refresh to download completed-session data.",
                "info",
            )

        self.tabs.currentChanged.connect(self._on_market_pulse_tab_changed)

    def _create_market_pulse_table(
        self, section: str, model: MarketPulseTableModel
    ) -> QTableView:
        table = QTableView()
        table.setObjectName(f"marketPulseTable_{section}")
        table.setAccessibleName(f"{SECTION_LABELS[section]} Market Pulse table")
        table.setModel(model)
        table.setItemDelegate(MarketPulseMetricDelegate(table))
        table.setSortingEnabled(True)
        table.horizontalHeader().setSortIndicator(5, Qt.DescendingOrder)
        table.horizontalHeader().setSortIndicatorShown(True)
        table.horizontalHeader().setSectionsClickable(True)
        table.verticalHeader().setVisible(False)
        table.verticalHeader().setDefaultSectionSize(28)
        table.setAlternatingRowColors(True)
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.setSelectionMode(QAbstractItemView.SingleSelection)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        table.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)
        table.setShowGrid(False)
        table.clicked.connect(self._open_market_pulse_component_chart)
        table.setStyleSheet(
            "QTableView { border: 1px solid #e0e3eb; border-radius: 5px; "
            "background: #ffffff; alternate-background-color: #f8f9fb; }"
            "QHeaderView::section { background: #f1f3f6; color: #3d4350; "
            "font-weight: 600; border: none; border-right: 1px solid #e0e3eb; "
            "border-bottom: 1px solid #d1d4dc; padding: 7px 5px; }"
        )
        widths = (56, 230, 78, 92, 92, 92, 92, 92, 175, 175, 76, 76, 76, 76)
        header = table.horizontalHeader()
        for column, width in enumerate(widths):
            header.setSectionResizeMode(column, QHeaderView.Fixed)
            table.setColumnWidth(column, width)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        table.setMinimumWidth(1240)
        self._resize_market_pulse_table(table, 0)
        return table

    def _open_market_pulse_component_chart(self, index: QModelIndex) -> None:
        if not index.isValid() or index.column() < 10:
            return
        symbol = str(index.data(Qt.UserRole) or "").strip().upper()
        if not symbol:
            return
        set_symbol = getattr(self, "_set_tradingview_symbol", None)
        tradingview_widget = self.__dict__.get("tradingview_widget")
        if not callable(set_symbol) or tradingview_widget is None:
            return
        select_unfiltered_symbol = getattr(
            self, "_select_sidebar_universe_symbol", None
        )
        if callable(select_unfiltered_symbol):
            select_unfiltered_symbol(symbol)
        set_symbol(symbol)
        self.tabs.setCurrentWidget(tradingview_widget)
        load_chart = getattr(self, "load_tradingview_chart", None)
        if callable(load_chart):
            load_chart(force=True)

    @staticmethod
    def _resize_market_pulse_table(table: QTableView, row_count: int) -> None:
        header_height = table.horizontalHeader().sizeHint().height()
        row_height = table.verticalHeader().defaultSectionSize()
        table.setFixedHeight(header_height + max(1, row_count) * row_height + 3)
        table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def _on_market_pulse_tab_changed(self, *_args) -> None:
        if self.tabs.currentWidget() is not self.market_pulse_widget:
            return
        snapshot = self.__dict__.get("market_pulse_snapshot")
        if snapshot is not None:
            self._update_market_pulse_freshness(snapshot)

    def refresh_market_pulse(self, *_args) -> None:
        worker = self.__dict__.get("market_pulse_worker")
        if worker is not None and worker.isRunning():
            self._show_market_pulse_message(
                "A Market Pulse refresh is already running.", "info"
            )
            return
        self.market_pulse_service.set_engine(self.__dict__.get("db_engine"))
        worker = MarketPulseRefreshWorker(self.market_pulse_service)
        self.market_pulse_worker = worker
        worker.snapshot_ready.connect(self._on_market_pulse_refresh_ready)
        worker.error_occurred.connect(self._on_market_pulse_refresh_error)
        self._set_market_pulse_loading(True)
        if hasattr(self, "append_log"):
            self.append_log(
                "Market Pulse refresh started (completed-session and intraday data)."
            )
        track_worker = getattr(self, "_track_worker", None)
        if callable(track_worker):
            track_worker("market_pulse_worker", worker)
        else:
            worker.finished.connect(lambda: setattr(self, "market_pulse_worker", None))
        worker.start()

    def refresh_market_pulse_intraday(self, *_args) -> None:
        worker = self.__dict__.get("market_pulse_worker")
        if worker is not None and worker.isRunning():
            self._show_market_pulse_message(
                "A Market Pulse refresh is already running.", "info"
            )
            return
        self.market_pulse_service.set_engine(self.__dict__.get("db_engine"))
        worker = MarketPulseIntradayRefreshWorker(self.market_pulse_service)
        self.market_pulse_worker = worker
        worker.snapshot_ready.connect(
            self._on_market_pulse_intraday_refresh_ready
        )
        worker.error_occurred.connect(
            self._on_market_pulse_intraday_refresh_error
        )
        self._set_market_pulse_loading(True, intraday_only=True)
        if hasattr(self, "append_log"):
            self.append_log("Market Pulse intraday-only refresh started.")
        track_worker = getattr(self, "_track_worker", None)
        if callable(track_worker):
            track_worker("market_pulse_worker", worker)
        else:
            worker.finished.connect(
                lambda: setattr(self, "market_pulse_worker", None)
            )
        worker.start()

    def _set_market_pulse_loading(
        self, loading: bool, *, intraday_only: bool = False
    ) -> None:
        self.market_pulse_loading_bar.setVisible(loading)
        self.market_pulse_refresh_button.setEnabled(not loading)
        self.market_pulse_intraday_refresh_button.setEnabled(not loading)
        self.market_pulse_refresh_button.setText(
            "Refreshing…" if loading and not intraday_only else "Refresh"
        )
        self.market_pulse_intraday_refresh_button.setText(
            "Refreshing…"
            if loading and intraday_only
            else "Refresh Intraday"
        )

    def _on_market_pulse_refresh_ready(self, snapshot: MarketPulseSnapshot) -> None:
        self._render_market_pulse_snapshot(snapshot)
        self._set_market_pulse_loading(False)
        valid = sum(row.status in {"available", "cached"} for row in snapshot.rows)
        if hasattr(self, "append_log"):
            self.append_log(
                f"Market Pulse refresh complete: {valid}/{len(snapshot.rows)} rows, "
                f"as of {snapshot.as_of_date.isoformat()}, {len(snapshot.failures)} issue(s)."
            )

    def _on_market_pulse_refresh_error(self, message: str) -> None:
        self._set_market_pulse_loading(False)
        self._show_market_pulse_message(
            f"Refresh failed; the last successful snapshot remains displayed. {message}",
            "error",
        )
        if hasattr(self, "append_log"):
            self.append_log(f"Market Pulse refresh failed: {message}")

    def _on_market_pulse_intraday_refresh_ready(
        self, snapshot: MarketPulseSnapshot
    ) -> None:
        self._render_market_pulse_snapshot(snapshot)
        self._set_market_pulse_loading(False)
        updated = sum(row.intraday_return is not None for row in snapshot.rows)
        if hasattr(self, "append_log"):
            self.append_log(
                f"Market Pulse intraday-only refresh complete: "
                f"{updated}/{len(snapshot.rows)} rows updated."
            )

    def _on_market_pulse_intraday_refresh_error(self, message: str) -> None:
        self._set_market_pulse_loading(False)
        self._show_market_pulse_message(
            "Intraday refresh failed; the last successful snapshot remains "
            f"displayed. {message}",
            "error",
        )
        if hasattr(self, "append_log"):
            self.append_log(f"Market Pulse intraday-only refresh failed: {message}")

    def _render_market_pulse_snapshot(self, snapshot: MarketPulseSnapshot) -> None:
        self.market_pulse_snapshot = snapshot
        for section in SECTION_ORDER:
            rows = [row for row in snapshot.rows if row.section == section]
            self.market_pulse_models[section].replace_rows(rows)
            self._resize_market_pulse_table(self.market_pulse_tables[section], len(rows))
        self.market_pulse_as_of_label.setText(
            f"As of: {snapshot.as_of_date.isoformat()}"
        )
        updated = snapshot.refreshed_at
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=dt.timezone.utc)
        self.market_pulse_updated_label.setText(
            "Last updated: " + updated.astimezone(KST_ZONE).strftime("%Y-%m-%d %H:%M KST")
        )
        self._update_market_pulse_freshness(snapshot)

    def _update_market_pulse_freshness(self, snapshot: MarketPulseSnapshot) -> None:
        expected = expected_latest_market_data_date()
        unavailable = sum(
            row.status not in {"available", "cached"} for row in snapshot.rows
        )
        if snapshot.as_of_date < expected:
            self._show_market_pulse_message(
                f"Stale snapshot: latest common session is {snapshot.as_of_date.isoformat()}; "
                f"{expected.isoformat()} is expected. Select Refresh.",
                "warning",
            )
        elif unavailable:
            self._show_market_pulse_message(
                f"Updated with {unavailable} unavailable or stale row(s). Hover a row for details.",
                "warning",
            )
        elif snapshot.failures:
            self._show_market_pulse_message(
                f"Updated from cache with {len(snapshot.failures)} provider warning(s).",
                "warning",
            )
        else:
            self.market_pulse_message_label.setVisible(False)

    def _show_market_pulse_message(self, text: str, level: str) -> None:
        colors = {
            "error": ("#fde8e8", "#9b1c1c", "#f5b5b5"),
            "warning": ("#fff8e1", "#8a5a00", "#f2d287"),
            "info": ("#eaf2ff", "#1f4f99", "#b8cff5"),
        }
        background, foreground, border = colors.get(level, colors["info"])
        self.market_pulse_message_label.setText(text)
        self.market_pulse_message_label.setStyleSheet(
            f"background: {background}; color: {foreground}; border: 1px solid {border}; "
            "border-radius: 4px; padding: 7px 10px;"
        )
        self.market_pulse_message_label.setVisible(True)
