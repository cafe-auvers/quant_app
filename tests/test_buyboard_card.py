"""Tests for src.ui.buyboard.card (P1-7: real P&L, not entry_trigger)."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt5.QtWidgets import QApplication

from src.core.trade_card_state import BoardStatus, PositionRuntimeStatus, TradeCardState
from src.ui.buyboard.card import TradeCardWidget, _pnl_percent

_APP = None


def _ensure_app():
    global _APP
    _APP = QApplication.instance() or QApplication([])


def _open_card(**overrides):
    fields = dict(
        environment="PROD",
        account_no="1",
        symbol="AAPL",
        board_status=BoardStatus.OPEN_POSITION,
        position_runtime_status=PositionRuntimeStatus.OPEN,
        broker_quantity=100,
        average_entry_price=100.0,
    )
    fields.update(overrides)
    return TradeCardState(**fields)


def test_pnl_percent_uses_the_supplied_current_price():
    card = _open_card(average_entry_price=100.0)
    assert _pnl_percent(card, 110.0) == pytest.approx(10.0)
    assert _pnl_percent(card, 90.0) == pytest.approx(-10.0)


def test_pnl_percent_zero_when_no_average_entry_price():
    card = _open_card(average_entry_price=0.0)
    assert _pnl_percent(card, 150.0) == 0.0


def test_card_widget_renders_without_crashing_with_a_live_price():
    _ensure_app()
    card = _open_card()
    widget = TradeCardWidget(card, current_price=105.0)
    assert widget is not None


def test_card_widget_renders_without_crashing_without_a_live_price():
    """No live quote -- must not fabricate a P&L from entry_trigger (P1-7)."""
    _ensure_app()
    card = _open_card()
    widget = TradeCardWidget(card, current_price=None)
    assert widget is not None


def test_card_widget_shows_warnings():
    _ensure_app()
    card = _open_card(warnings=["STOP_REQUIRED", "DATA_STALE"])
    widget = TradeCardWidget(card)
    assert widget is not None


# --- P1-8: column rendering respects kanban_priority ------------------------


def test_board_column_list_sorts_by_priority_descending():
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt5.QtWidgets import QApplication

    from src.core.trade_card_state import BoardStatus
    from src.ui.buyboard.columns import BoardColumnList

    QApplication.instance() or QApplication([])

    low = TradeCardState(environment="PROD", account_no="1", symbol="LOW", kanban_priority=1)
    high = TradeCardState(environment="PROD", account_no="1", symbol="HIGH", kanban_priority=10)
    mid = TradeCardState(environment="PROD", account_no="1", symbol="MID", kanban_priority=5)

    column = BoardColumnList(BoardStatus.WATCHLIST, on_card_dropped=lambda payload, target: None)
    column.set_cards([low, high, mid])

    from PyQt5.QtCore import Qt

    rendered_symbols = [column.item(i).data(Qt.UserRole)["symbol"] for i in range(column.count())]
    assert rendered_symbols == ["HIGH", "MID", "LOW"]
