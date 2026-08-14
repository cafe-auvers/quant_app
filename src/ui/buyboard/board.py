"""Assembles the Buy Board tab: 8 columns + refresh bar.

Widget assembly and the drag-drop -> command translation live here;
``apply_board_command`` (validation + persistence) lives in
:mod:`src.ui.buyboard.controller` and has no Qt dependency.
"""
from __future__ import annotations

import logging
from typing import Dict, List

from PyQt5.QtWidgets import (
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from src.core.execution_config import is_buyboard_engine_enabled
from src.core.trade_card_state import BoardStatus, TradeCardState

from . import dialogs
from .columns import BOARD_COLUMN_ORDER, BOARD_COLUMN_TITLES, BoardColumnList
from .drag_commands import (
    ActivateForToday,
    CancelQueuedSellAll,
    MoveToBuylist,
    MoveToWatchlist,
    RequestPartialSell,
    RequestSellAll,
)

logger = logging.getLogger(__name__)

# Commands whose real effect is a broker action (cancel/submit an order,
# change a live stop). While the engine cutover flag is off, these are
# refused with an explanatory message instead of silently moving the card to
# a column that implies something is happening at the broker when nothing
# is (see execution_config.is_buyboard_engine_enabled's docstring).
_ENGINE_GATED_TARGET_COLUMNS = {
    BoardStatus.PARTIAL_SELL,
    BoardStatus.SELL_ALL,
}


def build_buyboard_widget(main_window) -> None:
    root_layout = QVBoxLayout(main_window.buyboard_widget)
    root_layout.setContentsMargins(6, 4, 6, 4)

    header = QHBoxLayout()
    title = QLabel("<b>Buy Board</b>")
    header.addWidget(title)
    header.addStretch()
    engine_status = QLabel(
        "Engine: ENABLED" if is_buyboard_engine_enabled() else "Engine: OFF (board is read-only for order actions)"
    )
    engine_status.setStyleSheet(
        "color: #2e7d32; font-weight: bold;"
        if is_buyboard_engine_enabled()
        else "color: #888;"
    )
    header.addWidget(engine_status)
    refresh_btn = QPushButton("Refresh")
    refresh_btn.clicked.connect(main_window.refresh_buyboard)
    header.addWidget(refresh_btn)
    root_layout.addLayout(header)

    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    columns_widget = QWidget()
    columns_layout = QHBoxLayout(columns_widget)
    columns_layout.setSpacing(6)

    lists: Dict[BoardStatus, BoardColumnList] = {}
    for board_status in BOARD_COLUMN_ORDER:
        group = QGroupBox(BOARD_COLUMN_TITLES[board_status])
        group_layout = QVBoxLayout(group)
        column_list = BoardColumnList(
            board_status,
            lambda payload, target, mw=main_window: _handle_card_dropped(mw, payload, target),
        )
        group_layout.addWidget(column_list)
        columns_layout.addWidget(group, 1)
        lists[board_status] = column_list

    scroll.setWidget(columns_widget)
    root_layout.addWidget(scroll, 1)

    main_window.buyboard_columns = lists
    main_window._buyboard_engine_status_label = engine_status


def populate_buyboard_columns(main_window, cards: List[TradeCardState]) -> None:
    grouped: Dict[BoardStatus, List[TradeCardState]] = {
        status: [] for status in BOARD_COLUMN_ORDER
    }
    for card in cards:
        grouped.setdefault(card.board_status, []).append(card)
    for status, column_list in main_window.buyboard_columns.items():
        column_list.set_cards(grouped.get(status, []))
    if hasattr(main_window, "_buyboard_engine_status_label"):
        enabled = is_buyboard_engine_enabled()
        main_window._buyboard_engine_status_label.setText(
            "Engine: ENABLED" if enabled else "Engine: OFF (board is read-only for order actions)"
        )
        main_window._buyboard_engine_status_label.setStyleSheet(
            "color: #2e7d32; font-weight: bold;" if enabled else "color: #888;"
        )


def _handle_card_dropped(main_window, payload: dict, target_status: BoardStatus) -> None:
    environment = str(payload.get("environment", ""))
    account_no = str(payload.get("account_no", ""))
    symbol = str(payload.get("symbol", ""))
    version = int(payload.get("version", 0))

    if target_status in _ENGINE_GATED_TARGET_COLUMNS and not is_buyboard_engine_enabled():
        QMessageBox.information(
            main_window,
            "Buy Board",
            "The new execution engine is not enabled yet. Use the existing "
            "Buy Dashboard tab to submit real orders until this is turned on.",
        )
        return

    card = None
    try:
        from src.services import trade_card_repository as repo

        card = repo.get_trade_card(main_window._buyboard_engine(), environment, account_no, symbol)
    except Exception:  # pragma: no cover - defensive, repository already logs
        logger.exception("Failed to look up dropped card %s", symbol)
    if card is None:
        QMessageBox.warning(main_window, "Buy Board", f"Could not find a card for {symbol}.")
        main_window.refresh_buyboard()
        return

    if target_status == BoardStatus.WATCHLIST:
        command = MoveToWatchlist(
            environment=environment, account_no=account_no, symbol=symbol, expected_card_version=version
        )
    elif target_status == BoardStatus.BUYLIST:
        command = MoveToBuylist(
            environment=environment, account_no=account_no, symbol=symbol, expected_card_version=version
        )
    elif target_status == BoardStatus.BUY_TODAY:
        command = ActivateForToday(
            environment=environment, account_no=account_no, symbol=symbol, expected_card_version=version
        )
    elif target_status == BoardStatus.OPEN_POSITION:
        # The only manual drag that legally targets Open Positions is
        # cancelling a still-queued (not yet market-hours) Sell All.
        command = CancelQueuedSellAll(
            environment=environment, account_no=account_no, symbol=symbol, expected_card_version=version
        )
    elif target_status == BoardStatus.PARTIAL_SELL:
        quantity = dialogs.prompt_partial_sell_quantity(main_window, card)
        if quantity is None:
            return
        command = RequestPartialSell(
            environment=environment,
            account_no=account_no,
            symbol=symbol,
            expected_card_version=version,
            quantity=quantity,
        )
    elif target_status == BoardStatus.SELL_ALL:
        if not dialogs.confirm_sell_all(main_window, card):
            return
        command = RequestSellAll(
            environment=environment, account_no=account_no, symbol=symbol, expected_card_version=version
        )
    else:
        QMessageBox.information(
            main_window,
            "Buy Board",
            f"{BOARD_COLUMN_TITLES[target_status]} is reached automatically once "
            "the broker confirms the corresponding event -- it cannot be set by "
            "dragging a card there.",
        )
        return

    main_window._buyboard_dispatch_command(command)
