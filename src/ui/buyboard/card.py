"""Draggable Kanban card widget for one TradeCardState."""
from __future__ import annotations

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QFrame, QLabel, QVBoxLayout

from src.core.trade_card_state import BoardStatus, PositionRuntimeStatus, TradeCardState

# Colors follow the existing Buy Dashboard convention (view.py row coloring):
# green for a profitable open position, red for a loss, blue-grey for a
# neutral/pending card. Kept as a flat palette rather than pulling in the
# dataviz skill's palette module -- this widget renders inside a native Qt
# desktop app, not a themed web page.
_BOARD_STATUS_ACCENT = {
    BoardStatus.WATCHLIST: "#607d8b",
    BoardStatus.BUYLIST: "#546e7a",
    BoardStatus.BUY_TODAY: "#1565c0",
    BoardStatus.ENTRY_PENDING: "#ef6c00",
    BoardStatus.OPEN_POSITION: "#2e7d32",
    BoardStatus.PARTIAL_SELL: "#8e24aa",
    BoardStatus.SELL_ALL: "#c62828",
    BoardStatus.CLOSED: "#455a64",
}


def _fmt_price(value) -> str:
    return f"{value:,.2f}" if value else "--"


def _pnl_percent(card: TradeCardState) -> float:
    if not card.average_entry_price or card.average_entry_price <= 0:
        return 0.0
    current = card.entry_trigger or card.average_entry_price
    return (current - card.average_entry_price) / card.average_entry_price * 100.0


class TradeCardWidget(QFrame):
    """Read-only rendering of one card. All mutation happens through
    ``src.ui.buyboard.controller.apply_board_command`` -- this widget never
    touches ``TradeCardState`` itself.
    """

    def __init__(self, card: TradeCardState, parent=None) -> None:
        super().__init__(parent)
        self._build(card)

    def _build(self, card: TradeCardState) -> None:
        accent = _BOARD_STATUS_ACCENT.get(card.board_status, "#607d8b")
        self.setFrameShape(QFrame.StyledPanel)
        self.setStyleSheet(
            f"QFrame {{ border-left: 4px solid {accent}; border-radius: 4px; "
            f"background-color: palette(base); padding: 2px; }}"
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(2)

        header = QLabel(f"<b>{card.symbol}</b>  <span style='color:#888;'>{card.name}</span>")
        layout.addWidget(header)

        badge_text = card.entry_runtime_status.value if card.entry_runtime_status else ""
        if card.position_runtime_status != PositionRuntimeStatus.NONE:
            badge_text = (badge_text + " " if badge_text else "") + card.position_runtime_status.value
        if badge_text:
            badge = QLabel(badge_text)
            badge.setStyleSheet("color: #555; font-size: 11px;")
            layout.addWidget(badge)

        if card.board_status == BoardStatus.OPEN_POSITION or card.broker_quantity:
            pnl = _pnl_percent(card)
            pnl_color = "#2e7d32" if pnl >= 0 else "#c62828"
            info = QLabel(
                f"{card.broker_quantity} sh @ {_fmt_price(card.average_entry_price)}"
                f"  <span style='color:{pnl_color};'>{pnl:+.2f}%</span>"
            )
        elif card.breakout_price:
            info = QLabel(f"Breakout {_fmt_price(card.breakout_price)}")
        else:
            info = QLabel("&nbsp;")
        layout.addWidget(info)

        if card.stop_type is not None and card.active_stop_price:
            stop_lbl = QLabel(
                f"Stop ({card.stop_type.value}): {_fmt_price(card.active_stop_price)}"
            )
            stop_lbl.setStyleSheet("color: #b71c1c; font-size: 11px;")
            layout.addWidget(stop_lbl)

        if card.exit_all_required:
            alert = QLabel("EXIT ALL REQUIRED")
            alert.setStyleSheet("color: white; background-color: #c62828; font-weight: bold;")
            layout.addWidget(alert)

        if card.entry_block_reason:
            block = QLabel(card.entry_block_reason)
            block.setStyleSheet("color: #ef6c00; font-size: 11px;")
            block.setWordWrap(True)
            layout.addWidget(block)

    def sizeHint(self):  # noqa: D102 - Qt override
        base = super().sizeHint()
        base.setHeight(max(base.height(), 78))
        return base


def card_drag_payload(card: TradeCardState) -> dict:
    """The minimal identity+version payload carried by a drag/drop event."""
    return {
        "environment": card.environment,
        "account_no": card.account_no,
        "symbol": card.symbol,
        "version": card.version,
    }
