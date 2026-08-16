"""Draggable Kanban card widget for one TradeCardState."""
from __future__ import annotations

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QFrame, QLabel, QPushButton, QVBoxLayout

from src.core.board_workflow import BoardCardProjection
from src.core.discovered_external_order import DiscoveredExternalOrder
from src.core.execution_order_record import ExecutionOrderRecord
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


def _pnl_percent(card: TradeCardState, current_price: float) -> float:
    if not card.average_entry_price or card.average_entry_price <= 0:
        return 0.0
    return (current_price - card.average_entry_price) / card.average_entry_price * 100.0


def _as_projection(value) -> tuple[TradeCardState, BoardCardProjection | None]:
    if isinstance(value, BoardCardProjection):
        return value.card, value
    return value, None


class TradeCardWidget(QFrame):
    """Read-only rendering of one card. All mutation happens through
    ``src.ui.buyboard.controller.apply_board_command`` -- this widget never
    touches ``TradeCardState`` itself.
    """

    def __init__(self, card: TradeCardState | BoardCardProjection, current_price: float | None = None, parent=None) -> None:
        """``current_price`` is a live quote the caller looked up (e.g. from
        :class:`~src.services.realtime_market_data.RealtimeMarketDataService`),
        not derived from the card itself. Code review finding P1-7: the
        original version computed P&L from ``entry_trigger`` (the ORB
        trigger price, not a live quote), which is not just stale but not
        even the right *kind* of number -- it silently showed a P&L that
        had nothing to do with the market. Without a real quote, this shows
        plain position/breakout facts and no P&L figure at all, rather than
        a misleading one.
        """
        super().__init__(parent)
        resolved_card, projection = _as_projection(card)
        self._build(resolved_card, current_price, projection)

    def _build(
        self,
        card: TradeCardState,
        current_price: float | None,
        projection: BoardCardProjection | None = None,
    ) -> None:
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

        # P1-10: the database's uniqueness scope is (environment, account_no,
        # symbol), not symbol alone -- two accounts can legitimately hold the
        # same symbol as two distinct cards. Without this, they'd render as
        # visually identical cards with no way to tell them apart.
        if card.account_no:
            account_lbl = QLabel(f"Account {card.account_no}")
            account_lbl.setStyleSheet("color: #888; font-size: 10px;")
            layout.addWidget(account_lbl)

        badge_text = card.entry_runtime_status.value if card.entry_runtime_status else ""
        if card.position_runtime_status != PositionRuntimeStatus.NONE:
            badge_text = (badge_text + " " if badge_text else "") + card.position_runtime_status.value
        if badge_text:
            badge = QLabel(badge_text)
            badge.setStyleSheet("color: #555; font-size: 11px;")
            layout.addWidget(badge)

        if card.board_status == BoardStatus.OPEN_POSITION or card.broker_quantity:
            if current_price:
                pnl = _pnl_percent(card, current_price)
                pnl_color = "#2e7d32" if pnl >= 0 else "#c62828"
                pnl_html = f"  <span style='color:{pnl_color};'>{pnl:+.2f}%</span>"
            else:
                # No live quote available -- show the position facts without
                # inventing a P&L figure (P1-7).
                pnl_html = "  <span style='color:#888;'>P&amp;L: no live quote</span>"
            info = QLabel(
                f"{card.broker_quantity} sh @ {_fmt_price(card.average_entry_price)}" + pnl_html
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

        if card.broker_quantity or card.orderable_quantity:
            quantity = QLabel(
                f"Held {card.broker_quantity} / Orderable {card.orderable_quantity}"
            )
            quantity.setStyleSheet("color: #555; font-size: 11px;")
            layout.addWidget(quantity)

        if card.pending_partial_sell_quantity:
            pending = QLabel(
                f"PARTIAL SELL REQUESTED: {card.pending_partial_sell_quantity}"
            )
            pending.setStyleSheet("color: #8e24aa; font-weight: bold;")
            layout.addWidget(pending)
        elif card.exit_all_required:
            pending = QLabel("SELL ALL / PROTECTIVE EXIT PENDING")
            pending.setStyleSheet("color: #c62828; font-weight: bold;")
            layout.addWidget(pending)

        if projection is not None and projection.working_order_count:
            statuses = ", ".join(status.value for status in projection.owned_order_statuses)
            order = QLabel(
                f"Owned broker order(s): {projection.working_order_count} [{statuses}]"
            )
            order.setStyleSheet("color: #ef6c00; font-size: 11px;")
            order.setWordWrap(True)
            layout.addWidget(order)
        if projection is not None and projection.ambiguous_order_count:
            ambiguous = QLabel("AMBIGUOUS ORDER — reconciliation required")
            ambiguous.setStyleSheet(
                "color: white; background-color: #b71c1c; font-weight: bold;"
            )
            ambiguous.setWordWrap(True)
            layout.addWidget(ambiguous)
        if projection is not None and projection.reconciliation_blocked:
            recon = QLabel("RECONCILIATION BLOCKED / STALE")
            recon.setStyleSheet("color: #c62828; font-weight: bold;")
            layout.addWidget(recon)
        if projection is not None and projection.engine_restrictions:
            restriction = QLabel("Restricted: " + " / ".join(projection.engine_restrictions))
            restriction.setStyleSheet("color: #ad6704; font-size: 11px;")
            restriction.setWordWrap(True)
            layout.addWidget(restriction)

        if card.exit_all_required:
            alert = QLabel("EXIT ALL REQUIRED")
            alert.setStyleSheet("color: white; background-color: #c62828; font-weight: bold;")
            layout.addWidget(alert)

        if card.entry_block_reason:
            block = QLabel(card.entry_block_reason)
            block.setStyleSheet("color: #ef6c00; font-size: 11px;")
            block.setWordWrap(True)
            layout.addWidget(block)

        if card.warnings:
            warnings_lbl = QLabel(" / ".join(card.warnings))
            warnings_lbl.setStyleSheet("color: #b71c1c; font-size: 11px; font-weight: bold;")
            warnings_lbl.setWordWrap(True)
            layout.addWidget(warnings_lbl)

    def sizeHint(self):  # noqa: D102 - Qt override
        base = super().sizeHint()
        base.setHeight(max(base.height(), 78))
        return base


def card_drag_payload(card: TradeCardState | BoardCardProjection) -> dict:
    """The minimal identity+version payload carried by a drag/drop event."""
    resolved, projection = _as_projection(card)
    return {
        "environment": resolved.environment,
        "account_no": resolved.account_no,
        "symbol": resolved.symbol,
        "version": resolved.version,
        "readiness_generation": (
            projection.readiness_generation if projection is not None else 0
        ),
        "ownership_version": (
            projection.ownership_version if projection is not None else 0
        ),
        "execution_owner": (
            projection.ownership_owner if projection is not None else ""
        ),
        "strategy_instance_id": (
            projection.strategy_instance_id if projection is not None else ""
        ),
    }


class ExternalOrderWidget(QFrame):
    """A deliberately separate, non-draggable unowned-broker-order row."""

    def __init__(self, order: DiscoveredExternalOrder, on_adopt=None, parent=None) -> None:
        super().__init__(parent)
        self.setFrameShape(QFrame.StyledPanel)
        self.setStyleSheet(
            "QFrame { border: 2px dashed #c62828; background-color: #fff3f3; padding: 4px; }"
        )
        layout = QVBoxLayout(self)
        title = QLabel(f"<b>UNOWNED BROKER ORDER</b> — {order.symbol}")
        title.setStyleSheet("color: #b71c1c;")
        layout.addWidget(title)
        layout.addWidget(
            QLabel(
                f"{order.side.value} {order.quantity_requested} | {order.broker_status.value} | "
                f"broker id {order.broker_order_id}"
            )
        )
        warning = QLabel("Not part of this card. Never cancelled or linked automatically.")
        warning.setWordWrap(True)
        layout.addWidget(warning)
        if on_adopt is not None:
            button = QPushButton("Adopt…")
            button.clicked.connect(lambda _=False: on_adopt(order))
            layout.addWidget(button)


class UnlinkedExecutionOrderWidget(QFrame):
    """Application/adopted order not correlated to the current card cycle."""

    def __init__(self, order: ExecutionOrderRecord, parent=None) -> None:
        super().__init__(parent)
        self.setFrameShape(QFrame.StyledPanel)
        self.setStyleSheet(
            "QFrame { border: 2px dotted #6a1b9a; background-color: #faf2ff; padding: 4px; }"
        )
        layout = QVBoxLayout(self)
        title = QLabel(f"<b>UNLINKED OWNED ORDER</b> — {order.symbol}")
        title.setStyleSheet("color: #6a1b9a;")
        layout.addWidget(title)
        layout.addWidget(
            QLabel(
                f"{order.side.value} {order.submitted_quantity} | {order.status.value} | "
                f"client id {order.client_order_id}"
            )
        )
        note = QLabel("Displayed separately; it is not projected into this card lifecycle.")
        note.setWordWrap(True)
        layout.addWidget(note)
