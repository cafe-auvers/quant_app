"""Modal dialogs for Kanban commands that need a user-entered value.

Modeled on the existing modal patterns in ``src/ui/buylist/view.py``'s ORB
review dialog and ``src/ui/buylist/actions.py::_buylist_sell_half_selected``.
The old partial-sell dialog hardcodes a 1/3-1/2-of-position slider range;
this one implements the spec's actual validation (section 571-579):
``0 < requested_quantity <= broker_orderable_quantity``, with quantities at
or above the ceiling explained to the user as becoming a Sell All.
"""
from __future__ import annotations

from typing import Callable, Iterable, Optional

from PyQt5.QtGui import QColor, QFont
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QSlider,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QPushButton,
    QVBoxLayout,
)
from PyQt5.QtCore import Qt

from src.core.execution_queue import OrbCandidateStatus, SUPPORTED_ORB_WINDOWS
from src.core.orb_combinations import OrbPositionCombination
from src.core.trade_card_state import TradeCardState
from src.core.position_pnl import estimate_position_pnl


_RISK_VALID_ORB_STATUSES = {
    OrbCandidateStatus.WAITING_BREAKOUT,
    OrbCandidateStatus.VALID,
    OrbCandidateStatus.EXECUTE_READY,
}


def prompt_buy_today_profile(parent, symbol: str) -> Optional[bool]:
    """Return the chosen stop/ADR profile, or None when publication is cancelled."""
    from src.risk.orb_position import get_orb_settings

    settings = get_orb_settings()
    normal = " / ".join(f"{value:g}%" for value in settings.stop_adr_bounds())
    ep = " / ".join(f"{value:g}%" for value in settings.stop_adr_bounds(True))
    dialog = QDialog(parent)
    dialog.setWindowTitle("Operator action")
    dialog.setMinimumWidth(540)
    layout = QVBoxLayout(dialog)
    title = QLabel(f"Publish {symbol} to Buy Today?")
    title.setStyleSheet("font-weight: bold; font-size: 16px;")
    layout.addWidget(title)
    layout.addWidget(QLabel(f"Stop/ADR lower / ideal / upper\nNormal: {normal}\nEP: {ep}"))
    actions = QHBoxLayout()
    choice = [None]

    def publish(is_ep: bool) -> None:
        choice[0] = is_ep
        dialog.accept()

    cancel = QPushButton("Cancel")
    cancel.setDefault(True)
    cancel.clicked.connect(dialog.reject)
    normal_button = QPushButton("Publish to Buy Today")
    normal_button.clicked.connect(lambda: publish(False))
    ep_button = QPushButton("Publish as EP")
    ep_button.clicked.connect(lambda: publish(True))
    for button in (cancel, normal_button, ep_button):
        actions.addWidget(button)
    layout.addLayout(actions)
    dialog.exec_()
    return choice[0]


def _orb_plan_classification(candidate, *, stale: bool = False) -> str:
    """Return the trader-facing validity of one computed ORB plan."""

    if stale:
        return "INVALID"
    if candidate.status in _RISK_VALID_ORB_STATUSES:
        return "VALID"
    if candidate.status in {
        OrbCandidateStatus.FORMING,
        OrbCandidateStatus.NOT_AVAILABLE,
    }:
        return "FORMING"
    return "INVALID"


def show_orb_plan_dialog(
    parent,
    queue_item,
    *,
    lock_window: Callable[[str], None],
    unlock_auto: Callable[[], None],
    stale_windows: Iterable[str] = (),
    read_only: bool = False,
    read_only_reason: str = "",
) -> None:
    """Show every ORB plan and allow a durable manual/automatic selection.

    ``queue_item`` remains the existing execution queue's authoritative ORB
    calculation. The board only renders and selects those plans; it never
    introduces a second ORB calculation path.
    """

    candidates = dict(getattr(queue_item, "candidates", {}) or {})
    stale_window_set = {
        str(window or "").strip() for window in stale_windows
    }
    if not candidates:
        QMessageBox.warning(
            parent,
            "ORB Plans",
            f"{queue_item.symbol} has no ORB plans yet. Refresh the plans and try again.",
        )
        return

    dialog = QDialog(parent)
    dialog.setWindowTitle(f"ORB Plans - {queue_item.symbol} [PROD]")
    dialog.setMinimumWidth(880)
    dialog.setMinimumHeight(360)
    layout = QVBoxLayout(dialog)
    layout.setSpacing(8)

    plan_summary = QLabel()
    plan_summary.setStyleSheet(
        "font-weight: bold; padding: 4px 8px; background-color: #e8f5e9; "
        "color: #1b5e20; border-radius: 4px;"
    )
    layout.addWidget(plan_summary)

    lock_label = QLabel()
    layout.addWidget(lock_label)

    if read_only:
        read_only_label = QLabel(
            str(read_only_reason or "ORB selection is read-only.")
        )
        read_only_label.setWordWrap(True)
        read_only_label.setStyleSheet(
            "font-weight: bold; padding: 4px 8px; background-color: #fff3e0; "
            "color: #e65100; border-radius: 4px;"
        )
        layout.addWidget(read_only_label)
    else:
        publish_label = QLabel(
            "A window change is saved on this device only. After locking or "
            "returning to automatic selection, click Publish Today's Plan "
            "to hand the change to another Execution Owner."
        )
        publish_label.setWordWrap(True)
        publish_label.setStyleSheet(
            "padding: 4px 8px; background-color: #e3f2fd; color: #0d47a1; "
            "border-radius: 4px;"
        )
        layout.addWidget(publish_label)

    columns = [
        "Window",
        "Plan",
        "Status",
        "ORB High",
        "ORB Low",
        "Entry",
        "Stop",
        "Shares",
        "Capital%",
        "Risk%",
        "Score",
        "Stop/ADR",
        "Reason / Warnings",
    ]
    table = QTableWidget(0, len(columns))
    table.setHorizontalHeaderLabels(columns)
    table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
    table.horizontalHeader().setStretchLastSection(True)
    table.setSelectionBehavior(QAbstractItemView.SelectRows)
    table.setSelectionMode(QAbstractItemView.SingleSelection)
    table.setEditTriggers(QAbstractItemView.NoEditTriggers)
    table.setAlternatingRowColors(True)
    table.verticalHeader().setVisible(False)
    for column, width in enumerate(
        [60, 68, 112, 74, 74, 74, 72, 58, 72, 62, 58, 72, 180]
    ):
        table.setColumnWidth(column, width)
    layout.addWidget(table, 1)

    def _price(value) -> str:
        try:
            return f"${float(value):.2f}" if value is not None else "-"
        except (TypeError, ValueError):
            return "-"

    def _percent(value, *, fraction: bool = False) -> str:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return "-"
        if fraction:
            number *= 100.0
        return f"{number:.2f}%"

    status_colors = {
        "VALID": ("#1b5e20", "#c8e6c9"),
        "FORMING": ("#4a148c", "#e1bee7"),
        "INVALID": ("#b71c1c", "#ffcdd2"),
    }

    def _selected_window() -> str:
        selected = str(getattr(queue_item, "selected_window", "") or "")
        if selected:
            return selected
        candidate = getattr(queue_item, "selected_candidate", None)
        return str(getattr(candidate, "window", "") or "")

    def _populate() -> None:
        table.setRowCount(0)
        selected_window = _selected_window()
        selected_row = -1
        valid_windows = []
        forming_windows = []
        for window in SUPPORTED_ORB_WINDOWS:
            candidate = candidates.get(window)
            if candidate is None:
                continue
            is_stale = window in stale_window_set
            classification = _orb_plan_classification(candidate, stale=is_stale)
            if classification == "VALID":
                valid_windows.append(window)
            elif classification == "FORMING":
                forming_windows.append(window)

            row = table.rowCount()
            table.insertRow(row)
            is_selected = window == selected_window
            if is_selected:
                selected_row = row
            status_text = (
                OrbCandidateStatus.NOT_AVAILABLE.value
                if is_stale
                else str(getattr(candidate.status, "value", candidate.status))
            )
            details = "; ".join(getattr(candidate, "warnings", ()) or ())
            if not details:
                details = str(getattr(candidate, "reason", "") or "OK")
            if is_stale:
                details = "Current-session ORB minute bars are unavailable"
            values = [
                f"> {window}" if is_selected else window,
                classification,
                status_text,
                _price(candidate.orb_high),
                _price(candidate.orb_low),
                _price(candidate.entry_trigger),
                _price(candidate.stop_loss),
                f"{int(candidate.shares):,}" if candidate.shares else "-",
                _percent(candidate.capital_percent),
                _percent(candidate.risk_percent, fraction=True),
                f"{float(candidate.score or 0.0):.1f}",
                _percent(candidate.stop_adr),
                details,
            ]
            foreground, background = status_colors[classification]
            for column, value in enumerate(values):
                cell = QTableWidgetItem(value)
                cell.setData(Qt.UserRole, window)
                cell.setTextAlignment(Qt.AlignCenter)
                cell.setForeground(QColor(foreground))
                cell.setBackground(QColor(background))
                if is_selected:
                    bold = QFont(cell.font())
                    bold.setBold(True)
                    cell.setFont(bold)
                    cell.setForeground(QColor("#000000"))
                    cell.setBackground(QColor("#fff176"))
                table.setItem(row, column, cell)

        valid_text = ", ".join(valid_windows) if valid_windows else "none yet"
        forming_text = ", ".join(forming_windows) if forming_windows else "none"
        plan_summary.setText(
            f"Valid ORB plans: {valid_text}    |    Still forming: {forming_text}"
        )
        if selected_row >= 0:
            table.selectRow(selected_row)
        _update_action_buttons()

    def _update_lock_label() -> None:
        selected = _selected_window() or "none"
        if getattr(queue_item, "manual_window_lock", False):
            lock_label.setText(
                f"Selected plan: {selected} ORB (manual lock; refresh will keep it)"
            )
            lock_label.setStyleSheet(
                "font-weight: bold; background-color: #e65100; color: white; "
                "padding: 4px 8px; border-radius: 4px;"
            )
        elif getattr(queue_item, "locked", False):
            lock_label.setText(f"Selected plan: {selected} ORB (order locked)")
            lock_label.setStyleSheet(
                "font-weight: bold; background-color: #6d4c41; color: white; "
                "padding: 4px 8px; border-radius: 4px;"
            )
        else:
            lock_label.setText(
                f"Selected plan: {selected} ORB | Auto-selects the best valid plan"
            )
            lock_label.setStyleSheet(
                "font-weight: bold; background-color: #1565c0; color: white; "
                "padding: 4px 8px; border-radius: 4px;"
            )

    button_row = QHBoxLayout()
    lock_button = QPushButton("Lock Selected Plan")
    lock_button.setStyleSheet(
        "background-color: #e65100; color: white; font-weight: bold;"
    )
    auto_button = QPushButton("Use Automatic Best Plan")
    auto_button.setStyleSheet(
        "background-color: #1565c0; color: white; font-weight: bold;"
    )
    close_button = QPushButton("Close")
    button_row.addWidget(lock_button)
    button_row.addWidget(auto_button)
    button_row.addStretch()
    button_row.addWidget(close_button)
    layout.addLayout(button_row)

    def _selected_action_window() -> str:
        row = table.currentRow()
        if row < 0 or table.item(row, 0) is None:
            return ""
        return str(table.item(row, 0).data(Qt.UserRole) or "")

    def _update_action_buttons() -> None:
        window = _selected_action_window()
        lock_button.setEnabled(not read_only and bool(window))
        auto_button.setEnabled(not read_only)
        tooltip = str(read_only_reason or "ORB selection is read-only.")
        lock_button.setToolTip(tooltip if read_only else "")
        auto_button.setToolTip(tooltip if read_only else "")

    def _lock_selected() -> None:
        if read_only:
            return
        window = _selected_action_window()
        if not window:
            QMessageBox.warning(
                dialog, "ORB Plans", "Select an ORB plan row first."
            )
            return
        lock_window(window)
        _populate()
        _update_lock_label()

    def _unlock_auto() -> None:
        if read_only:
            return
        unlock_auto()
        _populate()
        _update_lock_label()

    lock_button.clicked.connect(_lock_selected)
    auto_button.clicked.connect(_unlock_auto)
    table.itemSelectionChanged.connect(_update_action_buttons)
    close_button.clicked.connect(dialog.accept)
    _populate()
    _update_lock_label()
    dialog.exec_()


def _orb_combination_classification(
    combination: OrbPositionCombination,
) -> str:
    if combination.valid:
        return "VALID"
    if combination.status in {
        OrbCandidateStatus.FORMING,
        OrbCandidateStatus.NOT_AVAILABLE,
    }:
        return "FORMING"
    return "INVALID"


def show_orb_combinations_dialog(
    parent,
    queue_item,
    combinations: list[OrbPositionCombination],
    *,
    buffer_pct: float,
    snapshot_label: str = "",
) -> None:
    """Show every risk/window sizing option without changing plan selection."""

    dialog = QDialog(parent)
    title_prefix = "Rejected ORB Combinations" if snapshot_label else "ORB Combinations"
    dialog.setWindowTitle(f"{title_prefix} - {queue_item.symbol} [PROD]")
    dialog.setMinimumWidth(1200)
    dialog.setMinimumHeight(520)
    layout = QVBoxLayout(dialog)
    layout.setSpacing(8)

    valid_count = sum(item.valid for item in combinations)
    sizing_equity = (
        float(combinations[0].account_equity) if combinations else 0.0
    )
    summary = QLabel(
        f"{len(combinations)} combinations | {valid_count} valid | "
        f"Legacy buffer metadata: {float(buffer_pct) * 100.0:.4g}% | "
        f"Queue sizing equity: ${sizing_equity:,.2f}"
    )
    summary.setStyleSheet(
        "font-weight: bold; padding: 4px 8px; background-color: #e3f2fd; "
        "color: #0d47a1; border-radius: 4px;"
    )
    layout.addWidget(summary)

    explanation = QLabel(
        (
            f"Frozen rejection snapshot: {snapshot_label}. These are the exact "
            "0.25%-2.00% risk cases evaluated before the card returned to Buylist."
            if snapshot_label
            else "Read-only comparison of all 0.25%-2.00% risk cases across 1m, "
            "5m, and 30m ORBs. Use the separate ORB Plans dialog to select or "
            "lock the optimized plan."
        )
    )
    explanation.setWordWrap(True)
    layout.addWidget(explanation)

    valid_only = QCheckBox("Valid combinations only")
    valid_only.setChecked(False)
    layout.addWidget(valid_only)

    metrics = [
        ("Classification", lambda item: _orb_combination_classification(item)),
        ("ORB Status", lambda item: str(item.status.value)),
        ("Risk %", lambda item: f"{item.risk_percent * 100.0:.2f}%"),
        ("Window", lambda item: item.window),
        ("Recommendation", lambda item: f"{item.score:.1f}"),
        ("ORB High", lambda item: _format_optional_price(item.orb_high)),
        ("ORB Low", lambda item: _format_optional_price(item.orb_low)),
        (
            "Breakout Price",
            lambda item: _format_optional_price(item.breakout_price),
        ),
        (
            "Legacy Buffer %",
            lambda item: (
                f"{item.buffer_pct * 100.0:.4g}%"
                if item.buffer_pct is not None
                else "-"
            ),
        ),
        (
            "Confirmation Trigger",
            lambda item: _format_optional_price(item.breakout_trigger),
        ),
        ("Entry Floor", lambda item: _format_optional_price(item.entry_floor)),
        ("Execution Price", lambda item: _format_optional_price(item.execution_price)),
        ("Stop Price", lambda item: _format_optional_price(item.stop_price)),
        ("Risk / Share", lambda item: f"${item.risk_per_share:.2f}"),
        ("Shares", lambda item: f"{item.shares:,}"),
        ("Investment", lambda item: f"${item.investment:,.2f}"),
        ("Capital %", lambda item: f"{item.capital_percent:.2f}%"),
        (
            "ADR %",
            lambda item: (
                f"{item.adr_percent:.2f}%"
                if item.adr_percent is not None
                else "-"
            ),
        ),
        ("Stop Loss %", lambda item: f"{item.stop_loss_percent:.2f}%"),
        (
            "SL / ADR",
            lambda item: (
                f"{item.stop_adr:.2f}%" if item.stop_adr is not None else "-"
            ),
        ),
        ("Reason / Current State", lambda item: item.reason),
    ]
    table = QTableWidget(len(metrics), 1)
    table.setHorizontalHeaderLabels([""])
    table.verticalHeader().setVisible(False)
    table.setEditTriggers(QAbstractItemView.NoEditTriggers)
    table.setSelectionMode(QAbstractItemView.NoSelection)
    table.setAlternatingRowColors(False)
    layout.addWidget(table, 1)

    colors = {
        "VALID": (QColor("#1b5e20"), QColor("#c8e6c9")),
        "FORMING": (QColor("#4a148c"), QColor("#e1bee7")),
        "INVALID": (QColor("#b71c1c"), QColor("#ffcdd2")),
    }

    def populate() -> None:
        visible = [
            item
            for item in combinations
            if not valid_only.isChecked() or item.valid
        ]
        table.setRowCount(len(metrics))
        table.setColumnCount(1 + len(visible))
        table.setHorizontalHeaderLabels(
            ["Metric"]
            + [
                f"{item.risk_percent * 100.0:.2f}% {item.window}"
                for item in visible
            ]
        )
        for row, (label, _formatter) in enumerate(metrics):
            metric = QTableWidgetItem(label)
            bold = QFont(metric.font())
            bold.setBold(True)
            metric.setFont(bold)
            table.setItem(row, 0, metric)
        for column, item in enumerate(visible, start=1):
            classification = _orb_combination_classification(item)
            foreground, background = colors[classification]
            for row, (_label, formatter) in enumerate(metrics):
                cell = QTableWidgetItem(formatter(item))
                cell.setTextAlignment(Qt.AlignCenter)
                cell.setForeground(foreground)
                cell.setBackground(background)
                table.setItem(row, column, cell)
            table.setColumnWidth(column, 112)
        table.setColumnWidth(0, 150)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        table.horizontalHeader().setStretchLastSection(False)

    close_button = QPushButton("Close")
    close_button.clicked.connect(dialog.accept)
    button_row = QHBoxLayout()
    button_row.addStretch()
    button_row.addWidget(close_button)
    layout.addLayout(button_row)

    valid_only.stateChanged.connect(populate)
    populate()
    dialog.exec_()


def _format_optional_price(value) -> str:
    try:
        return f"${float(value):.2f}" if value is not None else "-"
    except (TypeError, ValueError, OverflowError):
        return "-"


def prompt_partial_sell(parent, card: TradeCardState) -> Optional[tuple[int, Optional[float]]]:
    """Return quantity and an optional SELL limit, or None on cancellation."""
    orderable = card.orderable_quantity or card.broker_quantity
    if orderable <= 0:
        QMessageBox.warning(parent, "Partial Sell", "No orderable quantity to sell.")
        return None

    dialog = QDialog(parent)
    dialog.setWindowTitle(f"Partial Sell — {card.symbol}")
    dialog.setMinimumWidth(440)
    layout = QVBoxLayout(dialog)

    layout.addWidget(QLabel(f"Current quantity: {orderable}"))

    row = QHBoxLayout()
    slider = QSlider(Qt.Horizontal)
    slider.setRange(1, orderable)
    slider.setValue(max(1, orderable // 2))
    spin = QSpinBox()
    spin.setObjectName("partial_sell_quantity")
    spin.setRange(1, orderable)
    spin.setValue(slider.value())
    slider.valueChanged.connect(spin.setValue)
    spin.valueChanged.connect(slider.setValue)
    row.addWidget(slider, 1)
    row.addWidget(spin)
    layout.addLayout(row)

    use_limit = QCheckBox("Sell at a limit price")
    use_limit.setObjectName("partial_sell_use_limit")
    layout.addWidget(use_limit)
    price = QDoubleSpinBox()
    price.setObjectName("partial_sell_limit_price")
    price.setPrefix("$ ")
    price.setDecimals(4)
    price.setRange(0.0001, 1_000_000.0)
    price.setValue(card.market_data_last_trusted_price or card.average_entry_price or 1.0)
    price.setEnabled(False)
    use_limit.toggled.connect(price.setEnabled)
    layout.addWidget(price)
    explanation = QLabel(
        "A limit sells at your price or higher and can fill immediately if the market "
        "is already there. It waits until filled, cancelled or expired by KIS. "
        "Outside the regular session, submission waits for the open. Stops take priority."
    )
    explanation.setWordWrap(True)
    layout.addWidget(explanation)

    hint = QLabel("")
    hint.setStyleSheet("color: #888; font-size: 11px;")
    layout.addWidget(hint)
    pnl_preview = QLabel()
    pnl_preview.setObjectName("partial_sell_pnl_preview")
    pnl_preview.setWordWrap(True)
    pnl_preview.setStyleSheet("padding: 10px; background: palette(alternate-base); font-size: 13px;")
    layout.addWidget(pnl_preview)

    def _update_hint(value: int) -> None:
        pct = value / orderable * 100.0
        if value >= orderable and use_limit.isChecked():
            hint.setText(f"{pct:.0f}% of position at the limit price")
        elif value >= orderable:
            hint.setText(f"{pct:.0f}% of position — this will submit a Sell All.")
        else:
            hint.setText(f"{pct:.0f}% of position")
        mark = price.value() if use_limit.isChecked() else card.market_data_last_trusted_price
        pnl = estimate_position_pnl(card.average_entry_price, mark, value)
        if pnl is None:
            pnl_preview.setText("Estimated P&L unavailable until a price and average cost are reported.")
        else:
            sign = "-" if pnl.dollars < 0 else "+"
            basis = "limit floor" if use_limit.isChecked() else "reported quote; may be stale"
            pnl_preview.setText(
                f"Estimated P&L: {sign}${abs(pnl.dollars):,.2f} ({pnl.percent:+.2f}%)\n"
                f"{value:,} shares @ ${mark:,.4f} ({basis})\n"
                f"Estimated proceeds: ${pnl.proceeds:,.2f}. Gross USD before fees."
            )

    spin.valueChanged.connect(_update_hint)
    use_limit.toggled.connect(lambda _checked: _update_hint(spin.value()))
    price.valueChanged.connect(lambda _price: _update_hint(spin.value()))
    _update_hint(spin.value())

    buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)

    def accept() -> None:
        if use_limit.isChecked():
            from src.core.exit_execution_command import validate_user_exit_limit_price

            try:
                validate_user_exit_limit_price(price.value())
            except ValueError as exc:
                QMessageBox.warning(dialog, "Partial Sell", str(exc))
                return
        dialog.accept()

    buttons.accepted.connect(accept)
    buttons.rejected.connect(dialog.reject)
    layout.addWidget(buttons)

    if dialog.exec_() != QDialog.Accepted:
        return None
    return int(spin.value()), float(price.value()) if use_limit.isChecked() else None


def confirm_sell_all(parent, card: TradeCardState) -> bool:
    quantity = card.orderable_quantity or card.broker_quantity
    pnl = estimate_position_pnl(
        card.average_entry_price, card.market_data_last_trusted_price, card.broker_quantity
    )
    estimate = "\nEstimated P&L unavailable until a price and average cost are reported."
    if pnl is not None:
        sign = "-" if pnl.dollars < 0 else "+"
        estimate = (
            f"\nEstimated P&L on all {card.broker_quantity:,} held shares: "
            f"{sign}${abs(pnl.dollars):,.2f} ({pnl.percent:+.2f}%).\n"
            "Based on the reported quote, which may be stale. Gross USD before fees; fill price can differ."
        )
    reply = QMessageBox.question(
        parent,
        "Sell All",
        f"Submit a full liquidation for {card.symbol} ({quantity} shares)?" + estimate,
        QMessageBox.Yes | QMessageBox.No,
        QMessageBox.No,
    )
    return reply == QMessageBox.Yes


def prompt_manual_stop_price(parent, card: TradeCardState) -> Optional[float]:
    from src.services.position_manager import minimum_manual_stop_price

    minimum = minimum_manual_stop_price(card)
    current = card.active_stop_price or minimum
    dialog = QDialog(parent)
    dialog.setWindowTitle(f"Manual Stop — {card.symbol}")
    layout = QVBoxLayout(dialog)
    layout.addWidget(
        QLabel(
            f"Current active stop: {current:,.4f}\n"
            f"Minimum allowed stop: {minimum:,.4f}"
        )
    )
    spin = QDoubleSpinBox()
    spin.setDecimals(4)
    spin.setRange(minimum, 1_000_000.0)
    spin.setValue(current)
    layout.addWidget(spin)

    buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    layout.addWidget(buttons)

    if dialog.exec_() != QDialog.Accepted:
        return None
    return float(spin.value())
