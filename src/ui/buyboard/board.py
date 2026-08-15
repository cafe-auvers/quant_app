"""Assembles the Buy Board tab: 8 columns + refresh bar.

Widget assembly and the drag-drop -> command translation live here;
``apply_board_command`` (validation + persistence) lives in
:mod:`src.ui.buyboard.controller` and has no Qt dependency.
"""
from __future__ import annotations

import logging
from typing import Callable, Dict, List, Optional

from PyQt5.QtWidgets import (
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMenu,
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
    ReorderCard,
    RequestPartialSell,
    RequestSellAll,
    SetBreakevenStop,
    SetManualStop,
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

    # P1-10: uniqueness is (environment, account_no, symbol), not symbol
    # alone -- without a filter, two accounts holding the same symbol
    # render as two visually near-identical cards with no way to isolate
    # one account's view.
    header.addWidget(QLabel("Account:"))
    account_filter_combo = QComboBox()
    account_filter_combo.addItem("All Accounts", None)
    account_filter_combo.currentIndexChanged.connect(main_window.refresh_buyboard)
    header.addWidget(account_filter_combo)

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
            lambda payload, global_pos, mw=main_window: _handle_card_context_menu(mw, payload, global_pos),
        )
        group_layout.addWidget(column_list)
        columns_layout.addWidget(group, 1)
        lists[board_status] = column_list

    scroll.setWidget(columns_widget)
    root_layout.addWidget(scroll, 1)

    main_window.buyboard_columns = lists
    main_window._buyboard_engine_status_label = engine_status
    main_window._buyboard_account_filter_combo = account_filter_combo


def _quote_lookup_for(main_window) -> Optional[Callable[[str], Optional[float]]]:
    """Live last-price lookup for card P&L (P1-7), sourced from the running
    engine's market-data service if one is attached to the window (see
    :mod:`src.services.buyboard_runtime`). Returns ``None`` -- meaning "no
    live price available" -- until the engine is actually running; cards
    then show plain position facts instead of a fabricated P&L.
    """
    runtime = getattr(main_window, "buyboard_runtime", None)
    market_data = getattr(runtime, "market_data", None) if runtime is not None else None
    if market_data is None:
        return None

    def lookup(symbol: str) -> Optional[float]:
        quote = market_data.latest_quote(symbol)
        return quote.last_price if quote is not None else None

    return lookup


def _sync_account_filter_options(main_window, cards: List[TradeCardState]) -> Optional[str]:
    """Refreshes the account-filter combo's entries from the live card set
    and returns the currently selected account_no (``None`` = All Accounts)."""
    combo = getattr(main_window, "_buyboard_account_filter_combo", None)
    if combo is None:
        return None
    accounts = sorted({card.account_no for card in cards if card.account_no})
    previous_selection = combo.currentData()
    combo.blockSignals(True)
    combo.clear()
    combo.addItem("All Accounts", None)
    for account_no in accounts:
        combo.addItem(account_no, account_no)
    restored_index = combo.findData(previous_selection) if previous_selection else 0
    combo.setCurrentIndex(restored_index if restored_index >= 0 else 0)
    combo.blockSignals(False)
    return combo.currentData()


def populate_buyboard_columns(main_window, cards: List[TradeCardState]) -> None:
    selected_account = _sync_account_filter_options(main_window, cards)
    if selected_account:
        cards = [card for card in cards if card.account_no == selected_account]
    grouped: Dict[BoardStatus, List[TradeCardState]] = {
        status: [] for status in BOARD_COLUMN_ORDER
    }
    for card in cards:
        grouped.setdefault(card.board_status, []).append(card)
    quote_lookup = _quote_lookup_for(main_window)
    for status, column_list in main_window.buyboard_columns.items():
        column_list.set_cards(grouped.get(status, []), quote_lookup)
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


def _column_cards_sorted(main_window, board_status: BoardStatus) -> List[TradeCardState]:
    from src.services import trade_card_repository as repo

    all_cards = repo.list_trade_cards(main_window._buyboard_engine(), environment="PROD")
    return sorted(
        (c for c in all_cards if c.board_status == board_status),
        key=lambda c: -c.kanban_priority,
    )


def _handle_card_context_menu(main_window, payload: dict, global_pos) -> None:
    """Right-click actions: stop management (review finding P1-9) and
    Kanban-priority reordering (review finding P1-8). Commands and dialogs
    for breakeven/manual stops already existed
    (:mod:`src.ui.buyboard.drag_commands`, :mod:`src.ui.buyboard.dialogs`)
    but nothing in the board UI ever exposed them; ``kanban_priority`` was
    carried on every card from Phase 1 on but nothing ever let the user
    change it.
    """
    environment = str(payload.get("environment", ""))
    account_no = str(payload.get("account_no", ""))
    symbol = str(payload.get("symbol", ""))
    version = int(payload.get("version", 0))

    from src.services import trade_card_repository as repo

    card = repo.get_trade_card(main_window._buyboard_engine(), environment, account_no, symbol)
    if card is None:
        return

    menu = QMenu(main_window)
    breakeven_action = manual_stop_action = None
    if card.board_status in (BoardStatus.OPEN_POSITION, BoardStatus.PARTIAL_SELL):
        breakeven_action = menu.addAction("Move Stop to Breakeven")
        manual_stop_action = menu.addAction("Set Manual Stop…")
        menu.addSeparator()

    siblings = _column_cards_sorted(main_window, card.board_status)
    index = next((i for i, sibling in enumerate(siblings) if sibling.card_key == card.card_key), None)
    move_up_action = menu.addAction("Move Up Priority")
    move_up_action.setEnabled(index is not None and index > 0)
    move_down_action = menu.addAction("Move Down Priority")
    move_down_action.setEnabled(index is not None and index < len(siblings) - 1)

    chosen = menu.exec_(global_pos)
    if chosen is None:
        return

    if chosen is breakeven_action:
        if not is_buyboard_engine_enabled():
            QMessageBox.information(
                main_window, "Buy Board", "The new execution engine is not enabled yet."
            )
            return
        command = SetBreakevenStop(
            environment=environment, account_no=account_no, symbol=symbol, expected_card_version=version
        )
        main_window._buyboard_dispatch_command(command)
    elif chosen is manual_stop_action:
        if not is_buyboard_engine_enabled():
            QMessageBox.information(
                main_window, "Buy Board", "The new execution engine is not enabled yet."
            )
            return
        price = dialogs.prompt_manual_stop_price(main_window, card)
        if price is None:
            return
        command = SetManualStop(
            environment=environment,
            account_no=account_no,
            symbol=symbol,
            expected_card_version=version,
            price=price,
        )
        main_window._buyboard_dispatch_command(command)
    elif chosen is move_up_action and index is not None and index > 0:
        neighbor = siblings[index - 1]
        command = ReorderCard(
            environment=environment,
            account_no=account_no,
            symbol=symbol,
            expected_card_version=version,
            target_priority=neighbor.kanban_priority + 1,
        )
        main_window._buyboard_dispatch_command(command)
    elif chosen is move_down_action and index is not None and index < len(siblings) - 1:
        neighbor = siblings[index + 1]
        command = ReorderCard(
            environment=environment,
            account_no=account_no,
            symbol=symbol,
            expected_card_version=version,
            target_priority=neighbor.kanban_priority - 1,
        )
        main_window._buyboard_dispatch_command(command)
