import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QTableWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from src.core.trade_reviewer import TradeReviewer
from src.core.watchlist import TradePlanManager
from src.ui.mixins.watchlist_mixin import WatchlistMixin


@pytest.fixture(scope="module", autouse=True)
def qapp():
    return QApplication.instance() or QApplication([])


class _TradePlanWindow(WatchlistMixin):
    """Small form host for exercising the mixin without MainWindow startup."""

    def __init__(self, rulebook_dir):
        self.symbol_input = QLineEdit("NVDA")
        self.entry_price_input = QLineEdit("100")
        self.stop_loss_input = QLineEdit("95")
        self.take_profit_input = QLineEdit()
        self.position_size_input = QLineEdit()
        self.account_size_input = QLineEdit("10000")
        self.risk_percent_input = QLineEdit("1")
        self.reason_input = QTextEdit("Breakout with volume confirmation")
        self.trade_review_output = QLabel()
        self.trade_plan_table = QTableWidget(0, 5)
        self.trade_manager = TradePlanManager()
        self.reviewer = TradeReviewer(rulebook_dir=rulebook_dir)
        self.save_count = 0

    def _save_state(self):
        self.save_count += 1


@pytest.fixture
def trade_plan_window(tmp_path):
    return _TradePlanWindow(tmp_path)


def test_trade_plan_sizes_and_reviews_valid_long_setup(trade_plan_window):
    assert trade_plan_window.calculate_position_size(show_warnings=False)
    assert trade_plan_window.position_size_input.text() == "20"

    assert trade_plan_window.review_trade(show_warnings=False)
    feedback = trade_plan_window.trade_review_output.text()
    assert "Approved" in feedback
    assert "rule-based exits" in feedback


def test_trade_plan_refuses_stop_at_or_above_entry(trade_plan_window):
    trade_plan_window.stop_loss_input.setText("100")

    assert not trade_plan_window.calculate_position_size(show_warnings=False)
    assert trade_plan_window.position_size_input.text() == "0"
    assert "below the entry" in trade_plan_window.trade_review_output.text()


def test_trade_plan_save_upserts_populates_and_loads_form(trade_plan_window):
    trade_plan_window.take_profit_input.setText("115")
    trade_plan_window.save_trade_plan()

    active_plans = trade_plan_window.trade_manager.get_active_plans()
    assert len(active_plans) == 1
    assert active_plans[0].symbol == "NVDA"
    assert active_plans[0].position_size == 20
    assert active_plans[0].risk_percent == pytest.approx(0.01)
    assert trade_plan_window.save_count == 1
    assert trade_plan_window.trade_plan_table.rowCount() == 1
    assert trade_plan_window.trade_plan_table.item(0, 0).data(Qt.UserRole) == "NVDA"
    assert "115.00" in trade_plan_window.trade_plan_table.item(0, 3).text()

    trade_plan_window.entry_price_input.setText("105")
    trade_plan_window.stop_loss_input.setText("100")
    trade_plan_window.save_trade_plan()

    active_plans = trade_plan_window.trade_manager.get_active_plans()
    assert len(active_plans) == 1
    assert active_plans[0].entry_price == pytest.approx(105.0)
    assert trade_plan_window.save_count == 2

    trade_plan_window.symbol_input.clear()
    trade_plan_window.entry_price_input.clear()
    trade_plan_window.stop_loss_input.clear()
    trade_plan_window.reason_input.clear()
    trade_plan_window.load_saved_trade_plan(0, 0)

    assert trade_plan_window.symbol_input.text() == "NVDA"
    assert trade_plan_window.entry_price_input.text() == "105.00"
    assert trade_plan_window.stop_loss_input.text() == "100.00"
    assert trade_plan_window.take_profit_input.text() == "115.00"
    assert trade_plan_window.reason_input.toPlainText() == "Breakout with volume confirmation"


def test_saved_plan_table_is_attached_to_existing_trade_plan_layout(
    trade_plan_window,
):
    trade_plan_window.trade_plan_widget = QWidget()
    outer_layout = QHBoxLayout(trade_plan_window.trade_plan_widget)
    outer_layout.addLayout(QVBoxLayout())
    right_layout = QVBoxLayout()
    trade_plan_window.orb_trade_plan_table = QTableWidget(0, 1)
    right_layout.addWidget(trade_plan_window.orb_trade_plan_table)
    outer_layout.addLayout(right_layout)

    trade_plan_window.populate_trade_plan_table()

    assert right_layout.indexOf(trade_plan_window.trade_plan_table) >= 0
    assert trade_plan_window.trade_plan_table.parent() is trade_plan_window.trade_plan_widget
