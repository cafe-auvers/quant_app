"""Day charts for the chat-written journal; the user's comments stay in chat."""
from __future__ import annotations

import datetime as dt
import html
import logging

from PyQt5.QtCore import QDate, QThread, QTimer, QUrl, pyqtSignal
from PyQt5.QtGui import QDesktopServices
from PyQt5.QtWidgets import QDateEdit, QHBoxLayout, QLabel, QPushButton, QTextBrowser, QVBoxLayout

from src.core.trade_journal import NY_ZONE
from src.services.daily_journal import daily_journal_review_state, load_daily_journal, prepare_daily_journal_packet
from src.services.trade_journal import TradeJournalStore, sync_trade_journal
from src.services.journal_context import journal_context_html, load_journal_context
from src.utils.config import get_env_value

logger = logging.getLogger(__name__)


class TradeJournalWorker(QThread):
    completed = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, engine, market_engine=None, *, force_charts=False, context_session=None):
        super().__init__()
        self.engine, self.market_engine = engine, market_engine
        self.force_charts = force_charts
        self.context_session = context_session

    def run(self):
        try:
            self.completed.emit(sync_trade_journal(
                self.engine, market_engine=self.market_engine,
                force_charts=self.force_charts, context_session=self.context_session,
                interrupted=self.isInterruptionRequested,
            ))
        except Exception as exc:
            logger.exception("Trade chart capture failed")
            self.failed.emit(str(exc))


class TradeJournalMixin:
    def _build_trade_journal_tab(self):
        self._journal_trades = []
        self._journal_date_initialized = False
        layout = QVBoxLayout(self.journal_widget)
        title = QLabel("Trading day journal")
        title.setStyleSheet("font-size:20px;font-weight:600")
        layout.addWidget(title)
        description = QLabel(
            "Share your thoughts about the day in chat. I write a plain journal entry "
            "and add the daily and 5-minute charts for the stocks you bought. "
            "You review the draft and confirm before it is posted."
        )
        description.setWordWrap(True)
        layout.addWidget(description)
        controls = QHBoxLayout()
        controls.addWidget(QLabel("Trading day (New York):"))
        self.journal_date = QDateEdit()
        self.journal_date.setCalendarPopup(True)
        self.journal_date.setDisplayFormat("yyyy-MM-dd")
        today = dt.datetime.now(NY_ZONE).date()
        self.journal_date.setDate(QDate(today.year, today.month, today.day))
        self.journal_date.dateChanged.connect(self._on_journal_date_changed)
        controls.addWidget(self.journal_date)
        for label, callback in (
            ("Refresh day data", lambda: self._refresh_trade_journal(force_charts=True)),
            ("Prepare day for chat", self._prepare_journal_day),
            ("Open saved journal", self._open_daily_journal),
        ):
            button = QPushButton(label)
            button.clicked.connect(callback)
            controls.addWidget(button)
        controls.addStretch()
        layout.addLayout(controls)
        self.journal_status = QLabel("Trade facts and charts are collected in the background. Journals are written from your chat comments.")
        self.journal_status.setWordWrap(True)
        layout.addWidget(self.journal_status)
        self.journal_day_view = QTextBrowser()
        self.journal_day_view.setOpenExternalLinks(True)
        layout.addWidget(self.journal_day_view, 1)
        self.journal_capture_timer = QTimer(self)
        try:
            seconds = max(15, int(get_env_value("TRADE_JOURNAL_CAPTURE_SECONDS", "60")))
        except (TypeError, ValueError):
            seconds = 60
        self.journal_capture_timer.setInterval(seconds * 1000)
        self.journal_capture_timer.timeout.connect(self._refresh_trade_journal)
        if str(get_env_value("TRADE_JOURNAL_ENABLED", "true")).lower() in {"true", "1", "yes"}:
            self.journal_capture_timer.start()
            QTimer.singleShot(5500, self._refresh_trade_journal)

    def _selected_journal_day(self):
        return self.journal_date.date().toString("yyyy-MM-dd")

    def _on_journal_date_changed(self, *_args):
        self._journal_date_initialized = True
        self._show_journal_day()

    def _refresh_trade_journal(self, *_args, force_charts=False):
        if self.__dict__.get("_database_shutting_down", False):
            return
        worker = self.__dict__.get("_trade_journal_worker")
        if worker is not None and worker.isRunning():
            return
        engine = self._execution_state_engine()
        network_enabled = str(get_env_value("TRADE_JOURNAL_CHART_NETWORK_ENABLED", "true")).lower() in {"true", "1", "yes"}
        if engine is None and not network_enabled:
            try:
                self._on_trade_journal_synced({"trades": TradeJournalStore().list_trades(), "errors": []})
                self.journal_status.setText("Trading database offline. Showing saved charts and journals; new trades will be collected when it reconnects.")
            except Exception as exc:
                self._on_trade_journal_failed(str(exc))
            return
        self.journal_status.setText("Collecting trade facts and charts...")
        worker = TradeJournalWorker(engine, self.__dict__.get("db_engine"), force_charts=force_charts,
                                    context_session=self._selected_journal_day())
        self._trade_journal_worker = worker
        worker.completed.connect(self._on_trade_journal_synced)
        worker.failed.connect(self._on_trade_journal_failed)
        self._track_worker("_trade_journal_worker", worker)
        worker.start()

    def _on_trade_journal_synced(self, result):
        self._journal_trades = result["trades"]
        if not self._journal_date_initialized and self._journal_trades:
            latest = max(row["session_date"] for row in self._journal_trades)
            self.journal_date.setDate(QDate.fromString(latest, "yyyy-MM-dd"))
            self._journal_date_initialized = True
        self._show_journal_day()
        errors = result.get("errors", [])
        self.journal_status.setText(
            f"Capture needs attention: {'; '.join(errors[:2])}" if errors else
            "Charts ready for your chat journal. Share simple comments about how the day went; posting waits for your confirmation."
        )

    def _on_trade_journal_failed(self, message):
        self.journal_status.setText(f"Chart capture failed; will retry: {message}")

    def _show_journal_day(self):
        session = self._selected_journal_day()
        store = TradeJournalStore()
        entry = load_daily_journal(store, session)
        body = [f"<h2>{session}</h2>"]
        if entry:
            statuses = {
                "AWAITING_CONFIRMATION": "Awaiting your confirmation in chat",
                "APPROVED": "Approved; posting remains pending",
                "DRAFT_NEEDS_UPDATE": "Trade facts or charts changed. Update and review the draft before posting.",
            }
            status = statuses[daily_journal_review_state(store, session)]
            body.append(f"<p><b>{status}</b></p><p>{html.escape(entry['draft']).replace(chr(10), '<br>')}</p>")
        else:
            body.append("<p>No journal draft yet. Tell me how the day went in chat, in your own words.</p>")
        body.append(journal_context_html(load_journal_context(store, session)))
        buys = [row for row in self._journal_trades if row["session_date"] == session and row["side"] == "BUY"]
        body.append("<h3>Stocks bought</h3>")
        if not buys:
            body.append("<p>No recorded purchases for this day. Your day can still have a journal entry.</p>")
        seen = set()
        for trade in reversed(buys):
            fields = trade["entry_plan"].get("fields", {})
            body.append(f"<p><b>{html.escape(trade['symbol'])}</b>: {trade['quantity']} shares at average ${trade['average_fill_price']:.4f}; ORB {html.escape(str(fields.get('entry_orb_window') or fields.get('selected_orb_window') or 'not recorded'))}.</p>")
            key = (trade["environment"], trade["symbol"])
            if key in seen:
                continue
            seen.add(key)
            directory = store.materialize(trade)
            snapshots = store.snapshots(trade["journal_id"])
            for interval, label in (("1d", "Daily chart"), ("5m", "5-minute chart")):
                available = [row for row in snapshots if row["interval"] == interval]
                if not available:
                    body.append(f"<p>{label}: unavailable; capture will retry.</p>")
                    continue
                chart = next((row for row in available if row["stage"] == "end_of_day"), available[0])
                stem = f"{chart['stage']}-{interval}"
                image_path = directory / f"{stem}.png"
                if not image_path.exists():
                    image_path = directory / f"{stem}.svg"
                url = html.escape(QUrl.fromLocalFile(str(image_path)).toString(), quote=True)
                body.append(f'<p><a href="{url}">{label}</a> · {html.escape(chart["capture_basis"])}</p><p><a href="{url}"><img src="{url}" width="620"></a></p>')
        body.append("<p>Times use the recorded fill observation; exact broker execution timestamps are unavailable.</p>")
        self.journal_day_view.setHtml("".join(body))

    def _prepare_journal_day(self, *_args):
        try:
            path = prepare_daily_journal_packet(TradeJournalStore(), self._selected_journal_day())
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
            self.journal_status.setText(f"Day facts and purchase charts prepared for chat: {path}. Share your comments here in chat.")
        except Exception as exc:
            self.journal_status.setText(f"Could not prepare the day's charts: {exc}")

    def _open_daily_journal(self, *_args):
        store = TradeJournalStore()
        entry = load_daily_journal(store, self._selected_journal_day())
        if not entry:
            self.journal_status.setText("No journal saved for this day yet. Share your comments in chat to start the draft.")
            return
        path = store.root / "days" / entry["session_date"] / "journal.html"
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
