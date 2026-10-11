import datetime as dt
from types import SimpleNamespace

from PyQt5.QtCore import QDate
from PyQt5.QtWidgets import QApplication, QVBoxLayout, QWidget, QTextEdit

from src.core.order_state import OrderIntent, OrderSide
from src.core.execution_order_record import ExecutionOrderStatus
from src.core.trade_journal import fill_facts
from src.services.trade_journal import TradeJournalStore
from src.services.daily_journal import save_daily_journal_draft
from src.ui.trade_journal import TradeJournalMixin

_APP = None


class JournalWindow(TradeJournalMixin, QWidget):
    def __init__(self):
        super().__init__()
        self.journal_widget = QWidget()
        QVBoxLayout(self).addWidget(self.journal_widget)
        self._build_trade_journal_tab()

    def _execution_state_engine(self):
        return None


def _seed(store, identity, symbol, side=OrderSide.BUY):
    now = dt.datetime(2026, 10, 8, 14, 0, tzinfo=dt.timezone.utc)
    order = SimpleNamespace(
        environment="PROD", account_no="test-account", client_order_id=identity,
        symbol=symbol, side=side, intent=OrderIntent.ENTRY,
        status=ExecutionOrderStatus.FILLED, filled_quantity=10, average_fill_price=100,
        market_session_date="2026-10-08", submission_started_at=now.isoformat(),
        prepared_at=now.isoformat(), last_broker_seen_at=now.isoformat(), last_reconciled_at=None,
    )
    store.record_fill(fill_facts(order, now), {"provenance": "order-linked snapshot", "fields": {"entry_orb_window": "5m"}}, now)


def test_day_view_has_purchase_charts_and_no_writing_form(monkeypatch):
    global _APP
    _APP = QApplication.instance() or QApplication([])
    monkeypatch.setenv("TRADE_JOURNAL_ENABLED", "false")
    store = TradeJournalStore()
    _seed(store, "first", "FIRST")
    _seed(store, "sell", "SELL_ONLY", OrderSide.SELL)
    window = JournalWindow()
    window._on_trade_journal_synced({"trades": store.list_trades(), "errors": []})
    text = window.journal_day_view.toPlainText()
    assert window._selected_journal_day() == "2026-10-08"
    assert "FIRST" in text
    assert "SELL_ONLY" not in text
    assert "Daily chart" in text
    assert "5-minute chart" in text
    assert "Tell me how the day went in chat" in text
    assert all(edit.isReadOnly() for edit in window.findChildren(QTextEdit))
    window.journal_date.setDate(QDate(2026, 10, 7))
    window._on_trade_journal_synced({"trades": store.list_trades(), "errors": []})
    assert window._selected_journal_day() == "2026-10-07"
    assert "No recorded purchases" in window.journal_day_view.toPlainText()
    window.close()


def test_offline_archive_shows_chat_draft_awaiting_confirmation(monkeypatch):
    global _APP
    _APP = QApplication.instance() or QApplication([])
    monkeypatch.setenv("TRADE_JOURNAL_ENABLED", "false")
    store = TradeJournalStore()
    _seed(store, "first", "FIRST")
    save_daily_journal_draft(store, "2026-10-08", comments="I waited today", draft="I waited for the entry before buying.")
    window = JournalWindow()
    window._refresh_trade_journal()
    assert "offline" in window.journal_status.text()
    text = window.journal_day_view.toPlainText()
    assert "I waited for the entry before buying." in text
    assert "Awaiting your confirmation in chat" in text
    assert "exact broker execution" in text
    window.close()
