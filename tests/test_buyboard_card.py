"""Tests for src.ui.buyboard.card (P1-7: real P&L, not entry_trigger)."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt5.QtWidgets import QApplication
from PyQt5.QtWidgets import QLabel

from src.core.board_workflow import BoardCardProjection
from src.core.trade_card_state import (
    BoardStatus,
    EntryRuntimeStatus,
    PositionRuntimeStatus,
    TradeCardState,
)
from src.ui.buyboard.card import (
    TradeCardWidget,
    _card_metric_rows,
    _card_status_text,
    _pnl_percent,
)

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


def _widget_text(widget):
    return "\n".join(label.text() for label in widget.findChildren(QLabel))


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


def test_watchlist_card_hides_internal_noise_and_only_shows_breakout():
    _ensure_app()
    card = TradeCardState(
        environment="PROD",
        account_no="SECRET-ACCOUNT",
        symbol="NVDA",
        name="NVIDIA",
        board_status=BoardStatus.WATCHLIST,
        breakout_price=183.5,
        active_stop_price=170.0,
        warnings=["migrated_from_buylist"],
    )
    projection = BoardCardProjection(
        card=card,
        engine_restrictions=("Device state is STANDBY",),
    )

    text = _widget_text(TradeCardWidget(projection, current_price=182.8))

    assert "Breakout" in text
    assert "$183.50" in text
    assert "Current" not in text
    assert "Account" not in text
    assert "SECRET-ACCOUNT" not in text
    assert "migrated" not in text
    assert "STANDBY" not in text
    assert "MANUAL" not in text


def test_buylist_card_shows_only_name_and_breakout_not_execution_or_orb_state():
    _ensure_app()
    card = TradeCardState(
        environment="PROD",
        account_no="SECRET-ACCOUNT",
        symbol="MAX",
        name="MediaAlpha",
        board_status=BoardStatus.BUYLIST,
        breakout_price=13.75,
        entry_runtime_status=EntryRuntimeStatus.DATA_UNAVAILABLE,
        entry_block_reason="Current-session ORB minute bars are unavailable",
        warnings=["DATA_STALE"],
    )
    projection = BoardCardProjection(
        card=card,
        reconciliation_blocked=True,
        engine_restrictions=(
            "Observation only: execution owner is LEGACY",
            "Execution engine disabled",
        ),
        ambiguous_order_count=1,
    )

    text = _widget_text(TradeCardWidget(projection, current_price=13.5))

    assert "MAX" in text
    assert "MediaAlpha" in text
    assert "Breakout" in text
    assert "$13.75" in text
    assert "Current" not in text
    assert "RESTRICTED" not in text
    assert "LEGACY" not in text
    assert "ORB" not in text
    assert "unavailable" not in text
    assert "RECONCILIATION" not in text
    assert "AMBIGUOUS" not in text
    assert "DATA STALE" not in text


def test_buylist_card_shows_buy_today_rejection_memo():
    _ensure_app()
    card = TradeCardState(
        environment="PROD",
        account_no="1",
        symbol="WEX",
        board_status=BoardStatus.BUYLIST,
        breakout_price=192.72,
        buy_today_note=(
            "Buy Today rejected - all ORB plans invalid. "
            "1m: stop is too wide; 5m: breakout not cleared; 30m: risk invalid"
        ),
    )

    text = _widget_text(TradeCardWidget(card))

    assert "Memo:" in text
    assert "all ORB plans invalid" in text
    assert "1m: stop is too wide" in text


def test_buy_today_card_shows_live_breakout_distance_and_planned_stop_result():
    card = TradeCardState(
        environment="PROD",
        account_no="1",
        symbol="NVDA",
        board_status=BoardStatus.BUY_TODAY,
        entry_runtime_status=EntryRuntimeStatus.WAITING_BREAKOUT,
        breakout_price=183.5,
        entry_trigger=183.5,
        entry_orb_low=179.5,
        planned_quantity=250,
    )

    rows = dict(_card_metric_rows(card, 183.25, 100_000.0))

    assert rows["Current"] == "$183.25"
    assert rows["Breakout"] == "$183.50"
    assert rows["To Breakout"] == "+0.14%"
    assert rows["Planned"] == "250 sh"
    assert "-1.00% acct" in rows["Stop P&L"]
    assert "-$1,000" in rows["Stop P&L"]


def test_buy_today_without_orb_shows_authoritative_risk_budget_only():
    card = TradeCardState(
        environment="PROD",
        account_no="1",
        symbol="NVDA",
        board_status=BoardStatus.BUY_TODAY,
        entry_runtime_status=EntryRuntimeStatus.ORB_FORMING,
        breakout_price=183.5,
        risk_percent=0.01,
    )

    rows = dict(_card_metric_rows(card, 182.8, 100_000.0))

    assert rows["Planned"] == "--"
    assert rows["Risk Budget"] == "1.00% acct&nbsp;&nbsp;$1,000"
    assert "Stop P&L" not in rows


def test_buy_today_metrics_fall_back_to_the_persisted_observed_price():
    card = TradeCardState(
        environment="PROD",
        account_no="1",
        symbol="NVDA",
        board_status=BoardStatus.BUY_TODAY,
        breakout_price=200.0,
        market_data_last_trusted_price=198.0,
    )

    rows = dict(_card_metric_rows(card, None))

    assert rows["Current"] == "$198.00"
    assert rows["Breakout"] == "$200.00"
    assert rows["To Breakout"] == "+1.01%"


def test_buy_today_status_names_the_selected_orb_plan():
    card = TradeCardState(
        environment="PROD",
        account_no="1",
        symbol="NVDA",
        board_status=BoardStatus.BUY_TODAY,
        entry_runtime_status=EntryRuntimeStatus.ORB_FORMING,
        selected_orb_window="30m",
    )

    assert _card_status_text(card) == "ORB FORMING - ORB 30m"


def test_entry_pending_card_prioritizes_cancel_and_fill_progress():
    _ensure_app()
    card = TradeCardState(
        environment="PROD",
        account_no="1",
        symbol="NVDA",
        board_status=BoardStatus.ENTRY_PENDING,
        breakout_price=183.5,
        target_position_quantity=250,
        broker_quantity=100,
        orderable_quantity=100,
        average_entry_price=183.61,
        active_stop_price=179.5,
        stop_quantity=100,
        entry_cancel_in_flight=True,
    )

    text = _widget_text(
        TradeCardWidget(card, current_price=183.72, account_equity=100_000.0)
    )

    assert "CANCELLING ENTRY" in text
    assert "Filled" in text and "100 / 250 sh" in text
    assert "Avg Fill" in text and "$183.61" in text
    assert "Owned broker order" not in text


def test_open_card_shows_live_dollar_pnl_and_account_stop_pnl():
    _ensure_app()
    card = _open_card(
        broker_quantity=250,
        orderable_quantity=250,
        average_entry_price=183.5,
        active_stop_price=184.9,
        stop_quantity=250,
    )

    text = _widget_text(
        TradeCardWidget(card, current_price=195.2, account_equity=100_000.0)
    )

    assert "Current" in text and "$195.20" in text
    assert "Avg Entry" in text and "$183.50" in text
    assert "Position" in text and "250 sh" in text
    assert "+6.38%" in text and "+$2,925" in text
    assert "+0.35% acct" in text and "+$350" in text
    assert "Stop (" not in text


def test_incomplete_stop_coverage_is_an_exception_not_a_pnl_claim():
    _ensure_app()
    card = _open_card(
        broker_quantity=100,
        orderable_quantity=100,
        active_stop_price=95.0,
        stop_quantity=60,
    )

    text = _widget_text(
        TradeCardWidget(card, current_price=105.0, account_equity=10_000.0)
    )

    assert "WARNING: 40 SH UNPROTECTED" in text
    assert "Stop P&amp;L" not in text


def test_closed_card_does_not_fabricate_final_pnl_or_current_market_data():
    _ensure_app()
    card = TradeCardState(
        environment="PROD",
        account_no="1",
        symbol="NVDA",
        board_status=BoardStatus.CLOSED,
        position_runtime_status=PositionRuntimeStatus.CLOSED,
    )

    text = _widget_text(TradeCardWidget(card, current_price=195.2))

    assert "CLOSED" in text
    assert "Current" not in text
    assert "Final P" not in text


def test_partial_sell_and_sell_all_show_trader_facing_execution_state():
    _ensure_app()
    partial = _open_card(
        board_status=BoardStatus.PARTIAL_SELL,
        broker_quantity=250,
        orderable_quantity=250,
        pending_partial_sell_quantity=100,
        reserved_sell_quantity=80,
        exit_client_order_id="internal-id",
        active_stop_price=101.0,
        stop_quantity=250,
    )
    sell_all = _open_card(
        board_status=BoardStatus.SELL_ALL,
        broker_quantity=150,
        orderable_quantity=150,
        sell_all_at_market_open=True,
        active_stop_price=101.0,
        stop_quantity=150,
    )

    partial_text = _widget_text(TradeCardWidget(partial, current_price=110.0))
    sell_all_text = _widget_text(TradeCardWidget(sell_all, current_price=110.0))

    assert "PARTIAL SELL - ORDER PENDING" in partial_text
    assert "Selling" in partial_text and "80 sh" in partial_text
    assert "internal-id" not in partial_text
    assert "SELL ALL - QUEUED FOR OPEN" in sell_all_text
    assert "Selling All" in sell_all_text and "150 sh" in sell_all_text


def test_actionable_restrictions_remain_visible_while_standby_is_silent():
    _ensure_app()
    card = _open_card(orderable_quantity=100)
    projection = BoardCardProjection(
        card=card,
        engine_restrictions=(
            "Device state is STANDBY",
            "Canonical database is not confirmed writable",
        ),
    )

    text = _widget_text(TradeCardWidget(projection))

    assert "STANDBY" not in text
    assert "Canonical database is not confirmed writable" in text


def test_standby_ready_is_not_presented_as_an_execution_error():
    _ensure_app()
    projection = BoardCardProjection(
        card=_open_card(orderable_quantity=100),
        engine_restrictions=("Device state is STANDBY_READY",),
    )

    text = _widget_text(TradeCardWidget(projection))

    assert "RESTRICTED" not in text
    assert "STANDBY_READY" not in text


def test_live_metric_refresh_reuses_widget_and_never_rebuilds_column():
    _ensure_app()
    from src.ui.buyboard.columns import BoardColumnList

    card = _open_card(
        orderable_quantity=100,
        active_stop_price=95.0,
        stop_quantity=100,
    )
    column = BoardColumnList(
        BoardStatus.OPEN_POSITION,
        on_card_dropped=lambda payload, target: None,
    )
    assert column.set_cards(
        [card],
        quote_lookup=lambda _symbol: 101.0,
        account_equity_lookup=lambda _environment, _account: 10_000.0,
    ) is True
    item = column.item(0)
    widget = column.itemWidget(item)

    assert column.refresh_live_metrics(
        quote_lookup=lambda _symbol: 110.0,
        account_equity_lookup=lambda _environment, _account: 10_000.0,
    ) == 1

    assert column.itemWidget(item) is widget
    assert "+10.00%" in _widget_text(widget)
    assert column.refresh_live_metrics(
        quote_lookup=lambda _symbol: 110.0,
        account_equity_lookup=lambda _environment, _account: 10_000.0,
    ) == 0


def test_live_metric_refresh_expands_buy_today_item_for_full_description():
    _ensure_app()
    from src.ui.buyboard.columns import BoardColumnList

    card = TradeCardState(
        environment="PROD",
        account_no="1",
        symbol="CDNA",
        name="CDNA",
        board_status=BoardStatus.BUY_TODAY,
        breakout_price=47.99,
        planned_quantity=44,
        risk_percent=0.005,
    )
    column = BoardColumnList(
        BoardStatus.BUY_TODAY,
        on_card_dropped=lambda payload, target: None,
    )
    column.resize(340, 500)
    column.set_cards(
        [card],
        quote_lookup=lambda _symbol: 48.44,
        account_equity_lookup=lambda _environment, _account: None,
    )
    item = column.item(0)
    widget = column.itemWidget(item)
    initial_height = item.sizeHint().height()

    assert column.refresh_live_metrics(
        quote_lookup=lambda _symbol: 48.44,
        account_equity_lookup=lambda _environment, _account: 100_000.0,
    ) == 1

    assert "Risk Budget" in _widget_text(widget)
    assert item.sizeHint().height() > initial_height
    assert item.sizeHint().height() >= widget.sizeHint().height()


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
