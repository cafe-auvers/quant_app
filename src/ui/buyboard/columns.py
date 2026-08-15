"""The 8 Kanban columns (``buydashboard_to_kanban.md`` section 17-31) and the
draggable ``QListWidget`` each one uses.
"""
from __future__ import annotations

import json
from typing import Callable, Dict, List, Optional

from PyQt5.QtCore import QMimeData, Qt
from PyQt5.QtGui import QDrag
from PyQt5.QtWidgets import QAbstractItemView, QListWidget, QListWidgetItem, QMenu

from src.core.trade_card_state import BoardStatus, TradeCardState

from .card import TradeCardWidget, card_drag_payload

# Left-to-right column order exactly as the spec lists it (section 18-31).
BOARD_COLUMN_ORDER: List[BoardStatus] = [
    BoardStatus.WATCHLIST,
    BoardStatus.BUYLIST,
    BoardStatus.BUY_TODAY,
    BoardStatus.ENTRY_PENDING,
    BoardStatus.OPEN_POSITION,
    BoardStatus.PARTIAL_SELL,
    BoardStatus.SELL_ALL,
    BoardStatus.CLOSED,
]

BOARD_COLUMN_TITLES: Dict[BoardStatus, str] = {
    BoardStatus.WATCHLIST: "Watchlist",
    BoardStatus.BUYLIST: "Buylist",
    BoardStatus.BUY_TODAY: "Buy Today",
    BoardStatus.ENTRY_PENDING: "Entry Pending",
    BoardStatus.OPEN_POSITION: "Open Positions",
    BoardStatus.PARTIAL_SELL: "Partial Sell",
    BoardStatus.SELL_ALL: "Sell All",
    BoardStatus.CLOSED: "Closed",
}

# Columns a user may never drag a card *into* directly -- they are reached
# only as a side effect of broker confirmation (a fill, a broker-confirmed
# flat position), never by a manual move. Enforced again in
# board.py::_handle_card_dropped as a second guard, but the column itself
# refuses the drop so the cursor gives immediate feedback.
SYSTEM_ONLY_TARGET_COLUMNS = {BoardStatus.ENTRY_PENDING, BoardStatus.CLOSED}

_CARD_MIME_TYPE = "application/x-buyboard-card"


class BoardColumnList(QListWidget):
    """One Kanban column. Cards are rendered via ``setItemWidget`` for
    display; drag/drop is implemented on top of a custom MIME type instead
    of Qt's built-in item-model move so a drop always goes through
    ``on_card_dropped`` (which calls the backend command handler) rather
    than Qt silently relocating the row itself.
    """

    def __init__(
        self,
        board_status: BoardStatus,
        on_card_dropped: Callable[[dict, BoardStatus], None],
        on_card_context_menu: Optional[Callable[[dict, "object"], None]] = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.board_status = board_status
        self._on_card_dropped = on_card_dropped
        self._on_card_context_menu = on_card_context_menu
        self.setDragDropMode(QAbstractItemView.DragDrop)
        self.setSelectionMode(QAbstractItemView.SingleSelection)
        self.setSpacing(6)
        self.setAcceptDrops(board_status not in SYSTEM_ONLY_TARGET_COLUMNS)
        self.setDragEnabled(True)
        self.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.setStyleSheet("QListWidget { background: palette(alternate-base); }")
        if on_card_context_menu is not None:
            self.setContextMenuPolicy(Qt.CustomContextMenu)
            self.customContextMenuRequested.connect(self._show_card_context_menu)

    # -- right-click actions (P1-9: stop management) ---------------------
    def _show_card_context_menu(self, position) -> None:
        item = self.itemAt(position)
        if item is None or self._on_card_context_menu is None:
            return
        payload = item.data(Qt.UserRole)
        if not payload:
            return
        self._on_card_context_menu(payload, self.mapToGlobal(position))

    # -- drag source ---------------------------------------------------
    def startDrag(self, supportedActions) -> None:  # noqa: N802 - Qt override
        item = self.currentItem()
        if item is None:
            return
        payload = item.data(Qt.UserRole)
        if not payload:
            return
        drag = QDrag(self)
        mime = QMimeData()
        mime.setData(_CARD_MIME_TYPE, json.dumps(payload).encode("utf-8"))
        drag.setMimeData(mime)
        drag.exec_(Qt.MoveAction)

    # -- drop target -----------------------------------------------------
    def dragEnterEvent(self, event) -> None:  # noqa: N802 - Qt override
        if not self.acceptDrops():
            event.ignore()
            return
        if event.mimeData().hasFormat(_CARD_MIME_TYPE):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:  # noqa: N802 - Qt override
        if not self.acceptDrops():
            event.ignore()
            return
        if event.mimeData().hasFormat(_CARD_MIME_TYPE):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event) -> None:  # noqa: N802 - Qt override
        mime = event.mimeData()
        if not self.acceptDrops() or not mime.hasFormat(_CARD_MIME_TYPE):
            event.ignore()
            return
        try:
            payload = json.loads(bytes(mime.data(_CARD_MIME_TYPE)).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            event.ignore()
            return
        # Never let Qt perform its own row move -- the board always
        # rebuilds itself from the repository after a command is applied
        # (or rejected), so the visual state stays a pure function of
        # backend truth instead of drifting from an ad hoc item move.
        event.setDropAction(Qt.IgnoreAction)
        event.accept()
        self._on_card_dropped(payload, self.board_status)

    def set_cards(
        self,
        cards: List[TradeCardState],
        quote_lookup: Optional[Callable[[str], Optional[float]]] = None,
    ) -> None:
        """``quote_lookup``, when supplied, returns the live last price for
        a symbol (e.g. from a running ``RealtimeMarketDataService``) so
        cards can show real P&L instead of none at all (section P1-7).

        Sorted by ``kanban_priority`` descending (higher priority first) --
        section 379's "higher Kanban priority wins" is otherwise invisible
        (review finding P1-8): without this, ``ReorderCard`` could change
        the stored value but the column would render in the same order
        regardless.
        """
        self.clear()
        for card in sorted(cards, key=lambda c: -c.kanban_priority):
            item = QListWidgetItem(self)
            item.setData(Qt.UserRole, card_drag_payload(card))
            current_price = quote_lookup(card.symbol) if quote_lookup is not None else None
            widget = TradeCardWidget(card, current_price=current_price)
            item.setSizeHint(widget.sizeHint())
            self.setItemWidget(item, widget)
