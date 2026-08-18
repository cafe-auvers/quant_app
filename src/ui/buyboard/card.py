"""Draggable Kanban card widget for one TradeCardState."""
from __future__ import annotations

import hashlib
import html
import json
import math

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


def _fmt_money(value: float, *, signed: bool = False) -> str:
    """Compact dollar formatting without throwing away meaningful cents."""

    number = float(value or 0.0)
    if abs(number) < 0.005:
        number = 0.0
    amount = f"{abs(number):,.2f}".rstrip("0").rstrip(".")
    if number < 0:
        return f"-${amount}"
    if signed and number > 0:
        return f"+${amount}"
    return f"${amount}"


def _positive_float(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _pnl_percent(card: TradeCardState, current_price: float) -> float:
    if not card.average_entry_price or card.average_entry_price <= 0:
        return 0.0
    return (current_price - card.average_entry_price) / card.average_entry_price * 100.0


def _as_projection(value) -> tuple[TradeCardState, BoardCardProjection | None]:
    if isinstance(value, BoardCardProjection):
        return value.card, value
    return value, None


def board_interaction_fingerprint(
    value: TradeCardState | BoardCardProjection,
) -> str:
    """Hash actionable state, excluding storage and per-quote observations.

    Reconciliation can confirm identical lifecycle facts under a new row
    version, while each trusted quote can advance the audit-only last-price
    fields. Neither makes a user's drag unsafe. Position/order/stop state,
    outage warnings, ownership, and runtime fences remain in the hash.
    """

    card, projection = _as_projection(value)
    card_state = dict(card.to_dict())
    for observation_only_field in (
        "version",
        "updated_at",
        "market_data_last_trusted_price",
        "market_data_last_trusted_at",
    ):
        card_state.pop(observation_only_field, None)
    payload = {"card": card_state}
    if projection is not None:
        payload["projection"] = {
            "ownership_owner": projection.ownership_owner,
            "ownership_version": projection.ownership_version,
            "strategy_instance_id": projection.strategy_instance_id,
            "readiness_generation": projection.readiness_generation,
            "reconciliation_blocked": projection.reconciliation_blocked,
            "engine_restrictions": projection.engine_restrictions,
            "owned_order_statuses": tuple(
                status.value for status in projection.owned_order_statuses
            ),
            "working_order_count": projection.working_order_count,
            "ambiguous_order_count": projection.ambiguous_order_count,
            "unlinked_owned_orders": tuple(
                repr(order) for order in projection.unlinked_owned_orders
            ),
            "external_orders": tuple(
                repr(order) for order in projection.external_orders
            ),
        }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _humanize(value: object) -> str:
    raw = getattr(value, "value", value)
    return str(raw or "").strip().replace("_", " ")


def _card_status_text(
    card: TradeCardState,
    projection: BoardCardProjection | None = None,
) -> str:
    """Return a trader-facing lifecycle label, not raw runtime diagnostics."""

    if card.board_status == BoardStatus.BUY_TODAY:
        return _humanize(card.entry_runtime_status)
    if card.board_status == BoardStatus.ENTRY_PENDING:
        if card.entry_cancel_in_flight:
            return "CANCELLING ENTRY"
        return "ENTRY - ORDER PENDING" if (
            card.entry_client_order_id
            or (projection is not None and projection.working_order_count)
        ) else "ENTRY PENDING"
    if card.board_status == BoardStatus.OPEN_POSITION:
        if card.position_runtime_status == PositionRuntimeStatus.ENTRY_COMPLETING:
            return "ENTRY COMPLETING"
        return ""
    if card.board_status == BoardStatus.PARTIAL_SELL:
        if card.exit_cancel_in_flight:
            return "CANCELLING PARTIAL SELL"
        return "PARTIAL SELL - ORDER PENDING" if (
            card.exit_client_order_id
            or (projection is not None and projection.working_order_count)
        ) else "PARTIAL SELL"
    if card.board_status == BoardStatus.SELL_ALL:
        if card.sell_all_at_market_open or (
            card.position_runtime_status == PositionRuntimeStatus.QUEUED_FOR_OPEN
        ):
            return "SELL ALL - QUEUED FOR OPEN"
        if card.exit_cancel_in_flight:
            return "SELL ALL - CANCELLING / REPRICING"
        return "SELL ALL - ORDER PENDING" if (
            card.exit_client_order_id
            or (projection is not None and projection.working_order_count)
        ) else "SELL ALL"
    if card.board_status == BoardStatus.CLOSED:
        return "CLOSED"
    return ""


def _entry_target_quantity(card: TradeCardState) -> int:
    target = (
        int(card.target_position_quantity or 0)
        or int(card.planned_quantity or 0)
        or (
            int(card.broker_quantity or 0)
            + int(card.entry_remaining_target_quantity or 0)
        )
    )
    return max(0, int(card.broker_quantity or 0), target)


def _stop_pnl(
    card: TradeCardState,
    *,
    quantity: int,
    entry_price: float | None,
    require_active_protection: bool,
) -> float | None:
    if quantity <= 0:
        return None
    entry = _positive_float(entry_price)
    if require_active_protection:
        stop = _positive_float(card.active_stop_price)
        if stop is None or int(card.stop_quantity or 0) < quantity:
            return None
    else:
        stop = _positive_float(card.entry_orb_low or card.active_stop_price)
    if entry is None or stop is None:
        return None
    return (stop - entry) * quantity


def _account_result(value: float, account_equity: float | None) -> str:
    equity = _positive_float(account_equity)
    percent = (
        f"{value / equity * 100.0:+.2f}% acct" if equity is not None else "-- acct"
    )
    color = "#2e7d32" if value >= 0 else "#c62828"
    return (
        f"{percent}&nbsp;&nbsp;"
        f"<span style='color:{color};'>{_fmt_money(value, signed=True)}</span>"
    )


def _pnl_result(card: TradeCardState, current_price: float | None) -> str:
    current = _positive_float(current_price)
    entry = _positive_float(card.average_entry_price)
    quantity = max(0, int(card.broker_quantity or 0))
    if current is None or entry is None or quantity <= 0:
        return "--"
    percent = _pnl_percent(card, current)
    dollars = (current - entry) * quantity
    color = "#2e7d32" if dollars >= 0 else "#c62828"
    return (
        f"<span style='color:{color};'>{percent:+.2f}%&nbsp;&nbsp;"
        f"{_fmt_money(dollars, signed=True)}</span>"
    )


def _card_metric_rows(
    card: TradeCardState,
    current_price: float | None,
    account_equity: float | None = None,
) -> list[tuple[str, str]]:
    """Build state-specific facts exclusively from authoritative card/cache data."""

    status = card.board_status
    if status in (BoardStatus.WATCHLIST, BoardStatus.BUYLIST):
        return [("Breakout", f"${_fmt_price(card.breakout_price)}")]
    if status == BoardStatus.CLOSED:
        # Trade-cycle realized/final P&L is not yet a first-class durable
        # field. Keep CLOSED honest and simple instead of deriving history
        # from a now-flat card.
        return []

    rows: list[tuple[str, str]] = [("Current", f"${_fmt_price(current_price)}")]
    if status in (BoardStatus.BUY_TODAY, BoardStatus.ENTRY_PENDING):
        rows.append(("Breakout", f"${_fmt_price(card.breakout_price)}"))

    if status == BoardStatus.BUY_TODAY:
        current = _positive_float(current_price)
        breakout = _positive_float(card.breakout_price)
        distance = (
            f"{(breakout / current - 1.0) * 100.0:+.2f}%"
            if current is not None and breakout is not None
            else "--"
        )
        planned = max(0, int(card.planned_quantity or 0))
        rows.extend(
            [
                ("To Breakout", distance),
                ("Planned", f"{planned:,} sh" if planned else "--"),
            ]
        )
        planned_stop_pnl = _stop_pnl(
            card,
            quantity=planned,
            entry_price=card.entry_trigger or card.breakout_price,
            require_active_protection=False,
        )
        if planned_stop_pnl is not None:
            rows.append(("Stop P&L", _account_result(planned_stop_pnl, account_equity)))
        else:
            risk_fraction = _positive_float(card.risk_percent)
            equity = _positive_float(account_equity)
            if risk_fraction is not None and equity is not None:
                rows.append(
                    (
                        "Risk Budget",
                        f"{risk_fraction * 100.0:.2f}% acct&nbsp;&nbsp;"
                        f"{_fmt_money(equity * risk_fraction)}",
                    )
                )
        return rows

    if status == BoardStatus.ENTRY_PENDING:
        filled = max(0, int(card.broker_quantity or 0))
        target = _entry_target_quantity(card)
        fill_progress = (
            f"{filled:,} / {target:,} sh" if target else f"{filled:,} / -- sh"
        )
        rows.extend(
            [
                ("Filled", fill_progress),
                ("Avg Fill", f"${_fmt_price(card.average_entry_price)}"),
            ]
        )
        if filled:
            stop_pnl = _stop_pnl(
                card,
                quantity=filled,
                entry_price=card.average_entry_price,
                require_active_protection=True,
            )
        else:
            stop_pnl = _stop_pnl(
                card,
                quantity=target,
                entry_price=card.entry_trigger or card.breakout_price,
                require_active_protection=False,
            )
        if stop_pnl is not None:
            rows.append(("Stop P&L", _account_result(stop_pnl, account_equity)))
        return rows

    rows.extend(
        [
            ("Avg Entry", f"${_fmt_price(card.average_entry_price)}"),
            ("Position", f"{max(0, int(card.broker_quantity or 0)):,} sh"),
        ]
    )
    if status == BoardStatus.PARTIAL_SELL:
        selling = max(0, int(card.reserved_sell_quantity or 0)) or max(
            0, int(card.pending_partial_sell_quantity or 0)
        )
        rows.append(("Selling", f"{selling:,} sh" if selling else "--"))
    elif status == BoardStatus.SELL_ALL:
        selling = max(0, int(card.reserved_sell_quantity or 0)) or max(
            0,
            int(card.orderable_quantity or 0),
            int(card.broker_quantity or 0),
        )
        rows.append(("Selling All", f"{selling:,} sh" if selling else "--"))
    rows.append(("P&L", _pnl_result(card, current_price)))
    stop_pnl = _stop_pnl(
        card,
        quantity=max(0, int(card.broker_quantity or 0)),
        entry_price=card.average_entry_price,
        require_active_protection=True,
    )
    if stop_pnl is not None:
        rows.append(("Stop P&L", _account_result(stop_pnl, account_equity)))
    return rows


def _metric_rows_html(rows: list[tuple[str, str]]) -> str:
    if not rows:
        return ""
    body = "".join(
        "<tr>"
        f"<td style='color:#666; padding-right:10px;'>{html.escape(label)}</td>"
        f"<td align='right'>{value}</td>"
        "</tr>"
        for label, value in rows
    )
    return f"<table cellspacing='0' cellpadding='1'>{body}</table>"


def _visible_restrictions(projection: BoardCardProjection | None) -> tuple[str, ...]:
    if projection is None:
        return ()
    return tuple(
        reason
        for reason in projection.engine_restrictions
        if str(reason).strip().casefold() != "device state is standby"
    )


def _visible_warnings(card: TradeCardState) -> tuple[str, ...]:
    return tuple(
        warning
        for warning in card.warnings
        if str(warning).strip().casefold() != "migrated_from_buylist"
    )


class TradeCardWidget(QFrame):
    """Read-only trader-facing projection of one authoritative card."""

    def __init__(
        self,
        card: TradeCardState | BoardCardProjection,
        current_price: float | None = None,
        account_equity: float | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        resolved_card, projection = _as_projection(card)
        self._build(resolved_card, current_price, account_equity, projection)

    def _build(
        self,
        card: TradeCardState,
        current_price: float | None,
        account_equity: float | None,
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

        header = QLabel(
            f"<b>{html.escape(card.symbol)}</b>  "
            f"<span style='color:#888;'>{html.escape(card.name)}</span>"
        )
        layout.addWidget(header)

        self.card_key = card.card_key
        self.card_state = card
        self._projection = projection
        status_text = _card_status_text(card, projection)
        if status_text:
            status_label = QLabel(html.escape(status_text))
            status_label.setStyleSheet(
                "color: #555; font-size: 11px; font-weight: bold;"
            )
            layout.addWidget(status_label)

        self._metrics_html = _metric_rows_html(
            _card_metric_rows(card, current_price, account_equity)
        )
        self._metrics_label = QLabel(self._metrics_html)
        # Compatibility for extensions/tests that inspected the former
        # single position-summary label directly.
        self._info_label = self._metrics_label
        layout.addWidget(self._metrics_label)

        if card.pending_stop_command_id and card.pending_stop_price:
            pending_stop = QLabel(
                f"STOP CHANGE PENDING - ${_fmt_price(card.pending_stop_price)}"
            )
            pending_stop.setStyleSheet("color: #ef6c00; font-weight: bold;")
            layout.addWidget(pending_stop)

        if projection is not None and projection.ambiguous_order_count:
            ambiguous = QLabel("AMBIGUOUS ORDER - RECONCILIATION REQUIRED")
            ambiguous.setStyleSheet(
                "color: white; background-color: #b71c1c; font-weight: bold;"
            )
            ambiguous.setWordWrap(True)
            layout.addWidget(ambiguous)
        if projection is not None and projection.reconciliation_blocked:
            recon = QLabel("RECONCILIATION BLOCKED / STALE")
            recon.setStyleSheet("color: #c62828; font-weight: bold;")
            layout.addWidget(recon)
        restrictions = _visible_restrictions(projection)
        if restrictions:
            restriction = QLabel("RESTRICTED: " + " / ".join(restrictions))
            restriction.setStyleSheet("color: #ad6704; font-size: 11px;")
            restriction.setWordWrap(True)
            layout.addWidget(restriction)

        if card.exit_all_required and card.board_status != BoardStatus.SELL_ALL:
            alert = QLabel("EXIT ALL REQUIRED")
            alert.setStyleSheet(
                "color: white; background-color: #c62828; font-weight: bold;"
            )
            layout.addWidget(alert)

        unprotected_shares = 0
        if card.broker_quantity > 0 and (
            not card.active_stop_price or card.stop_quantity < card.broker_quantity
        ):
            unprotected_shares = max(
                0, card.broker_quantity - int(card.stop_quantity or 0)
            )
            protection = QLabel(
                f"WARNING: {unprotected_shares:,} SH UNPROTECTED"
            )
            protection.setStyleSheet(
                "color: white; background-color: #b71c1c; font-weight: bold;"
            )
            layout.addWidget(protection)
        if card.broker_quantity > card.orderable_quantity:
            unavailable = card.broker_quantity - max(0, int(card.orderable_quantity or 0))
            orderable = QLabel(f"WARNING: {unavailable:,} SH NOT ORDERABLE")
            orderable.setStyleSheet("color: #b71c1c; font-weight: bold;")
            layout.addWidget(orderable)
        if projection is not None and projection.external_orders:
            external = QLabel("EXTERNAL BROKER ORDER DETECTED")
            external.setStyleSheet(
                "color: white; background-color: #b71c1c; font-weight: bold;"
            )
            layout.addWidget(external)

        if card.entry_block_reason:
            block = QLabel(html.escape(_humanize(card.entry_block_reason)))
            block.setStyleSheet("color: #ef6c00; font-size: 11px;")
            block.setWordWrap(True)
            layout.addWidget(block)

        warnings = tuple(
            warning
            for warning in _visible_warnings(card)
            if not (
                unprotected_shares
                and str(warning).strip().casefold() == "stop_required"
            )
        )
        if warnings:
            warnings_lbl = QLabel(
                " / ".join(html.escape(_humanize(warning)) for warning in warnings)
            )
            warnings_lbl.setStyleSheet(
                "color: #b71c1c; font-size: 11px; font-weight: bold;"
            )
            warnings_lbl.setWordWrap(True)
            layout.addWidget(warnings_lbl)

    def sizeHint(self):  # noqa: D102 - Qt override
        base = super().sizeHint()
        base.setHeight(max(base.height(), 78))
        return base

    def update_live_metrics(
        self,
        card: TradeCardState,
        current_price: float | None,
        account_equity: float | None = None,
    ) -> bool:
        """Refresh quote/equity-derived text without rebuilding the card."""

        if card.card_key != self.card_key:
            return False
        self.card_state = card
        metrics_html = _metric_rows_html(
            _card_metric_rows(card, current_price, account_equity)
        )
        if metrics_html == self._metrics_html:
            return False
        self._metrics_html = metrics_html
        self._metrics_label.setText(metrics_html)
        return True

    def update_current_price(
        self, card: TradeCardState, current_price: float | None
    ) -> bool:
        """Backward-compatible alias for older callers/tests."""

        return self.update_live_metrics(card, current_price)


def card_drag_payload(
    card: TradeCardState | BoardCardProjection,
    *,
    state_fingerprint: str | None = None,
) -> dict:
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
        "state_fingerprint": (
            state_fingerprint
            if state_fingerprint is not None
            else board_interaction_fingerprint(card)
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
