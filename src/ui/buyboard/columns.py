"""The operator-facing Kanban columns and their draggable ``QListWidget``.

Watchlist and Closed remain durable workflow states, but are intentionally not
rendered on the execution board. Watchlist maintenance happens in TradingView
and closed trades remain available through history/reporting views.
"""
from __future__ import annotations

import json
from typing import Callable, Dict, List, Optional

from PyQt5.QtCore import QMimeData, Qt
from PyQt5.QtGui import QDrag
from PyQt5.QtWidgets import QAbstractItemView, QListWidget, QListWidgetItem, QMenu

from src.core.board_workflow import (
    BoardCardProjection,
    BoardExecutionOrderProjection,
    BoardExternalOrderProjection,
)
from src.core.trade_card_state import BoardStatus, TradeCardState

from .card import (
    ExternalOrderWidget,
    TradeCardWidget,
    UnlinkedExecutionOrderWidget,
    board_interaction_fingerprint,
    card_drag_payload,
)

# Left-to-right operator workflow. Hidden lifecycle states remain valid
# ``BoardStatus`` values and are not deleted from canonical storage.
BOARD_COLUMN_ORDER: List[BoardStatus] = [
    BoardStatus.BUYLIST,
    BoardStatus.BUY_TODAY,
    BoardStatus.ENTRY_PENDING,
    BoardStatus.OPEN_POSITION,
    BoardStatus.PARTIAL_SELL,
    BoardStatus.SELL_ALL,
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
        on_external_order_adopt: Optional[Callable[[object], None]] = None,
        on_interaction_active: Optional[Callable[[bool], None]] = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.board_status = board_status
        self._on_card_dropped = on_card_dropped
        self._on_card_context_menu = on_card_context_menu
        self._on_external_order_adopt = on_external_order_adopt
        self._on_interaction_active = on_interaction_active
        self._render_signature = None
        self._pending_card_keys: set[str] = set()
        self.setDragDropMode(QAbstractItemView.DragDrop)
        self.setSelectionMode(QAbstractItemView.SingleSelection)
        self.setSpacing(6)
        self.setAcceptDrops(board_status not in SYSTEM_ONLY_TARGET_COLUMNS)
        self.setDragEnabled(True)
        self.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
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
        if self._payload_card_key(payload) in self._pending_card_keys:
            return
        self._set_interaction_active(True)
        try:
            self._on_card_context_menu(payload, self.mapToGlobal(position))
        finally:
            self._set_interaction_active(False)

    def _set_interaction_active(self, active: bool) -> None:
        if self._on_interaction_active is not None:
            self._on_interaction_active(bool(active))

    def _set_item_widget_size(self, item: QListWidgetItem, widget) -> None:
        size = widget.sizeHint()
        available_width = max(120, self.viewport().width() - 4)
        size.setWidth(available_width)
        item.setSizeHint(size)

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        for index in range(self.count()):
            item = self.item(index)
            widget = self.itemWidget(item)
            if widget is not None:
                self._set_item_widget_size(item, widget)

    # -- drag source ---------------------------------------------------
    def startDrag(self, supportedActions) -> None:  # noqa: N802 - Qt override
        item = self.currentItem()
        if item is None:
            return
        payload = item.data(Qt.UserRole)
        if not payload:
            return
        if bool(payload.get("recovery_snapshot", False)):
            return
        if self._payload_card_key(payload) in self._pending_card_keys:
            return
        self._set_interaction_active(True)
        try:
            drag = QDrag(self)
            mime = QMimeData()
            mime.setData(_CARD_MIME_TYPE, json.dumps(payload).encode("utf-8"))
            drag.setMimeData(mime)
            drag.exec_(Qt.MoveAction)
        finally:
            self._set_interaction_active(False)

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
        cards: List[
            TradeCardState
            | BoardCardProjection
            | BoardExternalOrderProjection
            | BoardExecutionOrderProjection
        ],
        quote_lookup: Optional[Callable[[str], Optional[float]]] = None,
        account_equity_lookup: Optional[
            Callable[[str, str], Optional[float]]
        ] = None,
    ) -> bool:
        """``quote_lookup``, when supplied, returns the live last price for
        a symbol (e.g. from a running ``RealtimeMarketDataService``) so
        cards can show real P&L instead of none at all (section P1-7).

        Sorted by ``kanban_priority`` descending (higher priority first) --
        section 379's "higher Kanban priority wins" is otherwise invisible
        (review finding P1-8): without this, ``ReorderCard`` could change
        the stored value but the column would render in the same order
        regardless.
        """

        def state(item):
            return item.card if isinstance(item, BoardCardProjection) else item

        def priority(item):
            if isinstance(
                item, (BoardExternalOrderProjection, BoardExecutionOrderProjection)
            ):
                return 0
            return state(item).kanban_priority

        ordered = sorted(cards, key=lambda c: -priority(c))

        def prepare_render(item):
            if isinstance(item, BoardCardProjection):
                fingerprint = board_interaction_fingerprint(item)
                return item, fingerprint, (
                    "CARD", item.card.card_key, fingerprint
                )
            if isinstance(item, TradeCardState):
                fingerprint = board_interaction_fingerprint(item)
                return item, fingerprint, ("CARD", item.card_key, fingerprint)
            return item, None, (type(item).__name__, repr(item))

        prepared = tuple(prepare_render(item) for item in ordered)
        signature = tuple(identity for _item, _fingerprint, identity in prepared)
        if signature == self._render_signature:
            existing = {}
            for index in range(self.count()):
                item = self.item(index)
                payload = item.data(Qt.UserRole)
                if not isinstance(payload, dict):
                    continue
                key = (
                    str(payload.get("environment", "")),
                    str(payload.get("account_no", "")),
                    str(payload.get("symbol", "")),
                )
                existing[key] = (item, self.itemWidget(item))
            for value, fingerprint, _identity in prepared:
                if isinstance(
                    value,
                    (BoardExternalOrderProjection, BoardExecutionOrderProjection),
                ):
                    continue
                card_state = state(value)
                key = (
                    card_state.environment,
                    card_state.account_no,
                    card_state.symbol,
                )
                row = existing.get(key)
                if row is None:
                    continue
                item, widget = row
                payload = card_drag_payload(
                    value,
                    state_fingerprint=fingerprint,
                )
                if item.data(Qt.UserRole) != payload:
                    item.setData(Qt.UserRole, payload)
                if isinstance(widget, TradeCardWidget):
                    current_price = (
                        quote_lookup(card_state.symbol)
                        if quote_lookup is not None
                        else None
                    )
                    account_equity = (
                        account_equity_lookup(
                            card_state.environment,
                            card_state.account_no,
                        )
                        if account_equity_lookup is not None
                        else None
                    )
                    widget.update_live_metrics(
                        card_state,
                        current_price,
                        account_equity,
                    )
            return False

        self.clear()
        for card, fingerprint, _identity in prepared:
            if isinstance(card, BoardExternalOrderProjection):
                external_item = QListWidgetItem(self)
                external_item.setFlags(Qt.ItemIsEnabled)
                external_widget = ExternalOrderWidget(
                    card.order,
                    on_adopt=self._on_external_order_adopt,
                )
                self._set_item_widget_size(external_item, external_widget)
                self.setItemWidget(external_item, external_widget)
                continue
            if isinstance(card, BoardExecutionOrderProjection):
                owned_item = QListWidgetItem(self)
                owned_item.setFlags(Qt.ItemIsEnabled)
                owned_widget = UnlinkedExecutionOrderWidget(card.order)
                self._set_item_widget_size(owned_item, owned_widget)
                self.setItemWidget(owned_item, owned_widget)
                continue
            card_state = state(card)
            item = QListWidgetItem(self)
            item.setData(
                Qt.UserRole,
                card_drag_payload(card, state_fingerprint=fingerprint),
            )
            current_price = (
                quote_lookup(card_state.symbol)
                if quote_lookup is not None
                else None
            )
            account_equity = (
                account_equity_lookup(
                    card_state.environment,
                    card_state.account_no,
                )
                if account_equity_lookup is not None
                else None
            )
            widget = TradeCardWidget(
                card,
                current_price=current_price,
                account_equity=account_equity,
            )
            widget.set_pending(card_state.card_key in self._pending_card_keys)
            self._set_item_widget_size(item, widget)
            self.setItemWidget(item, widget)
            if bool(item.data(Qt.UserRole).get("recovery_snapshot", False)):
                item.setFlags(item.flags() & ~Qt.ItemIsDragEnabled)
            if isinstance(card, BoardCardProjection):
                for owned_order in card.unlinked_owned_orders:
                    owned_item = QListWidgetItem(self)
                    owned_item.setFlags(Qt.ItemIsEnabled)
                    owned_widget = UnlinkedExecutionOrderWidget(owned_order)
                    self._set_item_widget_size(owned_item, owned_widget)
                    self.setItemWidget(owned_item, owned_widget)
                for external_order in card.external_orders:
                    external_item = QListWidgetItem(self)
                    # No card payload: an external order cannot be dragged as
                    # though it were application-owned state.
                    external_item.setFlags(Qt.ItemIsEnabled)
                    external_widget = ExternalOrderWidget(
                        external_order,
                        on_adopt=self._on_external_order_adopt,
                    )
                    self._set_item_widget_size(external_item, external_widget)
                    self.setItemWidget(external_item, external_widget)
        self._render_signature = signature
        return True

    @staticmethod
    def _payload_card_key(payload: dict) -> str:
        return (
            f"{str(payload.get('environment', '')).upper()}:"
            f"{str(payload.get('account_no', ''))}:"
            f"{str(payload.get('symbol', '')).upper()}"
        )

    def set_pending_card_keys(self, card_keys: set[str]) -> None:
        """Show non-blocking save state and disable repeat gestures per card."""

        self._pending_card_keys = {str(key) for key in card_keys}
        for index in range(self.count()):
            item = self.item(index)
            payload = item.data(Qt.UserRole)
            if not isinstance(payload, dict):
                continue
            pending = self._payload_card_key(payload) in self._pending_card_keys
            flags = item.flags()
            restricted = bool(payload.get("recovery_snapshot", False))
            if pending or restricted:
                item.setFlags(flags & ~Qt.ItemIsDragEnabled)
            else:
                item.setFlags(flags | Qt.ItemIsDragEnabled)
            widget = self.itemWidget(item)
            if isinstance(widget, TradeCardWidget):
                widget.set_pending(pending)
                self._set_item_widget_size(item, widget)

    def refresh_live_metrics(
        self,
        quote_lookup: Optional[Callable[[str], Optional[float]]] = None,
        account_equity_lookup: Optional[
            Callable[[str, str], Optional[float]]
        ] = None,
    ) -> int:
        """Repaint quote-derived card values without DB reads or row rebuilds."""

        updated = 0
        for index in range(self.count()):
            item = self.item(index)
            widget = self.itemWidget(item)
            if not isinstance(widget, TradeCardWidget):
                continue
            card = widget.card_state
            current_price = (
                quote_lookup(card.symbol) if quote_lookup is not None else None
            )
            account_equity = (
                account_equity_lookup(card.environment, card.account_no)
                if account_equity_lookup is not None
                else None
            )
            if widget.update_live_metrics(card, current_price, account_equity):
                updated += 1
        return updated
