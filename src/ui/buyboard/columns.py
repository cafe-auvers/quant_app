"""The 8 Kanban columns (``buydashboard_to_kanban.md`` section 17-31) and the
draggable ``QListWidget`` each one uses.
"""
from __future__ import annotations

import json
from typing import Callable, Dict, List

from PyQt5.QtCore import QMimeData, Qt
from PyQt5.QtGui import QDrag
from PyQt5.QtWidgets import QAbstractItemView, QListWidget, QListWidgetItem

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
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.board_status = board_status
        self._on_card_dropped = on_card_dropped
        self.setDragDropMode(QAbstractItemView.DragDrop)
        self.setSelectionMode(QAbstractItemView.SingleSelection)
        self.setSpacing(6)
        self.setAcceptDrops(board_status not in SYSTEM_ONLY_TARGET_COLUMNS)
        self.setDragEnabled(True)
        self.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.setStyleSheet("QListWidget { background: palette(alternate-base); }")

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

    def set_cards(self, cards: List[TradeCardState]) -> None:
        self.clear()
        for card in cards:
            item = QListWidgetItem(self)
            item.setData(Qt.UserRole, card_drag_payload(card))
            widget = TradeCardWidget(card)
            item.setSizeHint(widget.sizeHint())
            self.setItemWidget(item, widget)
