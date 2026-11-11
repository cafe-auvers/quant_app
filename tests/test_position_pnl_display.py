import math

import pytest
from PyQt5.QtWidgets import QApplication, QLabel

from src.core.position_pnl import estimate_position_pnl
from src.core.trade_card_state import BoardStatus, PositionRuntimeStatus, TradeCardState
from src.ui.buyboard.card import _card_metric_rows


def card(**overrides):
    fields = dict(environment="PROD", account_no="1", symbol="TEST",
                  board_status=BoardStatus.OPEN_POSITION,
                  position_runtime_status=PositionRuntimeStatus.OPEN,
                  broker_quantity=100, orderable_quantity=100, average_entry_price=10)
    fields.update(overrides)
    return TradeCardState(**fields)


@pytest.mark.parametrize("price,quantity,dollars,percent,proceeds", [
    (11, 100, 100, 10, 1100), (12, 25, 50, 20, 300),
    (9, 100, -100, -10, 900), (10, 100, 0, 0, 1000),
])
def test_gross_estimates_use_the_exit_share_count(price, quantity, dollars, percent, proceeds):
    pnl = estimate_position_pnl(10, price, quantity)
    assert pnl.dollars == pytest.approx(dollars)
    assert pnl.percent == pytest.approx(percent)
    assert pnl.proceeds == pytest.approx(proceeds)


@pytest.mark.parametrize("cost,price,quantity", [
    (None, 10, 10), (0, 10, 10), (10, None, 10), (10, 0, 10),
    (10, math.nan, 10), (math.inf, 10, 10), (10, 10, 0),
    (10, 10, 1.5), (10, 10, -10), (1e-300, 1e300, 10),
])
def test_missing_invalid_or_overflowing_data_is_unavailable(cost, price, quantity):
    assert estimate_position_pnl(cost, price, quantity) is None


def test_partial_limit_estimate_is_separate_from_full_current_position_pnl():
    rows = dict(_card_metric_rows(card(board_status=BoardStatus.PARTIAL_SELL,
        pending_partial_sell_quantity=25, pending_partial_sell_price=12), 11))
    assert "+$100" in rows["P&L"]
    assert "+$50" in rows["Partial P&L est."]
    assert "25 sh @ $12.00 (limit floor)" in rows["Exit estimate"]


def test_partially_filled_exit_estimates_only_broker_reported_remaining_shares():
    rows = dict(_card_metric_rows(card(board_status=BoardStatus.PARTIAL_SELL,
        broker_quantity=90, pending_partial_sell_quantity=25, reserved_sell_quantity=15,
        pending_partial_sell_price=12, exit_client_order_id="sell"), 11))
    assert "+$30" in rows["Partial P&L est."]
    assert "15 sh" in rows["Exit estimate"]


@pytest.mark.parametrize("remaining", [0, 150])
def test_unknown_or_inconsistent_working_sell_quantity_does_not_invent_profit(remaining):
    rows = dict(_card_metric_rows(card(board_status=BoardStatus.PARTIAL_SELL,
        pending_partial_sell_quantity=25, reserved_sell_quantity=remaining,
        pending_partial_sell_price=12, exit_client_order_id="sell"), 11))
    assert rows["Partial P&L est."] == "--"
    assert rows["Exit estimate"] == "Sell quantity syncing"


def test_stop_liquidation_never_uses_the_old_partial_limit_estimate():
    rows = dict(_card_metric_rows(card(board_status=BoardStatus.PARTIAL_SELL,
        exit_all_required=True, pending_partial_sell_quantity=25,
        pending_partial_sell_price=12), 9))
    assert "Partial P&L est." not in rows
    assert "-$100" in rows["Sell All P&L est."]
    assert "100 sh @ $9.00" in rows["Exit estimate"]


def test_missing_quote_does_not_invent_zero_pnl_but_limit_estimate_still_works():
    rows = dict(_card_metric_rows(card(board_status=BoardStatus.PARTIAL_SELL,
        pending_partial_sell_quantity=25, pending_partial_sell_price=12), None))
    assert rows["P&L"] == "--"
    assert "+$50" in rows["Partial P&L est."]


def test_reported_quote_fallback_is_labeled():
    rows = dict(_card_metric_rows(card(market_data_last_trusted_price=11), None))
    assert "+$100" in rows["P&L"]
    assert "reported quote" in rows["P&L"]


def test_partial_dialog_preview_updates_without_creating_a_command(monkeypatch):
    from PyQt5.QtWidgets import QCheckBox, QDialog, QDoubleSpinBox, QSpinBox
    from src.ui.buyboard.dialogs import prompt_partial_sell

    app = QApplication.instance() or QApplication([])

    def inspect(dialog):
        preview = dialog.findChild(QLabel, "partial_sell_pnl_preview")
        assert "unavailable" in preview.text()
        dialog.findChild(QSpinBox, "partial_sell_quantity").setValue(25)
        dialog.findChild(QCheckBox, "partial_sell_use_limit").setChecked(True)
        dialog.findChild(QDoubleSpinBox, "partial_sell_limit_price").setValue(12)
        assert "+$50.00 (+20.00%)" in preview.text()
        assert "$300.00" in preview.text()
        return QDialog.Rejected

    monkeypatch.setattr(QDialog, "exec_", inspect)
    assert prompt_partial_sell(None, card()) is None


@pytest.mark.parametrize("width", [180, 240, 360])
def test_narrow_cards_resize_to_fit_wrapped_content(width):
    from src.ui.buyboard.columns import BoardColumnList
    app = QApplication.instance() or QApplication([])
    from pathlib import Path
    from PyQt5.QtGui import QFont, QFontDatabase
    font_path = Path("C:/Windows/Fonts/segoeui.ttf")
    if font_path.is_file():
        QFontDatabase.addApplicationFont(str(font_path))
    column = BoardColumnList(BoardStatus.OPEN_POSITION, lambda *_: None)
    column.setFont(QFont("Segoe UI", 10))
    column.resize(width, 500)
    position = card(active_stop_price=9.3, stop_quantity=100,
                    name="A long company name with lots of readable information")
    column.set_cards([position], quote_lookup=lambda _: 11)
    column.show()
    app.processEvents()
    from PyQt5.QtTest import QTest
    QTest.qWait(20)
    position.warnings = ["RECONCILIATION_REQUIRED_" * 8]
    column.set_cards([position], quote_lookup=lambda _: 11)
    QTest.qWait(20)
    item = column.item(0)
    widget = column.itemWidget(item)
    assert widget.heightForWidth(widget.width()) <= item.sizeHint().height()
    visible_labels = [label for label in widget.findChildren(QLabel) if label.isVisibleTo(widget)]
    assert widget._metrics_label in visible_labels
    assert "Stop loss" in widget._metrics_label.text() and "$9.30" in widget._metrics_label.text()
    assert "before fees" in widget._metrics_label.toolTip()
    assert not any("Gross USD estimates" in label.text() for label in visible_labels)
    for label in visible_labels:
        assert label.geometry().bottom() < widget.height()
    from PyQt5.QtGui import QTextDocument
    doc = QTextDocument()
    doc.setDocumentMargin(0)
    doc.setDefaultFont(widget._metrics_label.font())
    doc.setHtml(widget._metrics_label.text())
    doc.setTextWidth(widget._metrics_label.contentsRect().width())
    assert doc.idealWidth() <= widget._metrics_label.width() + 1
    assert doc.size().height() <= widget._metrics_label.height() + 1
    column.close()


@pytest.mark.parametrize("status", [BoardStatus.OPEN_POSITION, BoardStatus.PARTIAL_SELL, BoardStatus.SELL_ALL])
@pytest.mark.parametrize("stop,covered,pending,price,coverage", [
    (9.3, 100, 8.5, "$9.30", "100 sh"),
    (0.785, 25, 0.76, "$0.7850", "25 / 100 sh covered"),
    (None, 0, 9.5, "Not set", ""),
])
def test_position_cards_lead_with_actual_stop_price_and_coverage(status, stop, covered, pending, price, coverage):
    position = card(board_status=status, active_stop_price=stop, stop_quantity=covered,
                    pending_stop_price=pending, pending_stop_command_id="pending-stop")
    rows = _card_metric_rows(position, 11)
    assert rows[0][0] == "Stop loss"
    assert price in rows[0][1] and coverage in rows[0][1]
    assert f"{pending:.2f}" not in rows[0][1]
