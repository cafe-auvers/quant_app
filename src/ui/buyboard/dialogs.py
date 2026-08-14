"""Modal dialogs for Kanban commands that need a user-entered value.

Modeled on the existing modal patterns in ``src/ui/buylist/view.py``'s ORB
review dialog and ``src/ui/buylist/actions.py::_buylist_sell_half_selected``.
The old partial-sell dialog hardcodes a 1/3-1/2-of-position slider range;
this one implements the spec's actual validation (section 571-579):
``0 < requested_quantity <= broker_orderable_quantity``, with quantities at
or above the ceiling explained to the user as becoming a Sell All.
"""
from __future__ import annotations

from typing import Optional

from PyQt5.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QSlider,
    QSpinBox,
    QVBoxLayout,
)
from PyQt5.QtCore import Qt

from src.core.trade_card_state import TradeCardState


def prompt_partial_sell_quantity(parent, card: TradeCardState) -> Optional[int]:
    """Returns the requested quantity, or ``None`` if the user cancelled.

    A quantity equal to the full orderable size is allowed through here --
    the controller (``apply_board_command``) is what actually converts it to
    a Sell All, so the message shown here is informational only.
    """
    orderable = card.orderable_quantity or card.broker_quantity
    if orderable <= 0:
        QMessageBox.warning(parent, "Partial Sell", "No orderable quantity to sell.")
        return None

    dialog = QDialog(parent)
    dialog.setWindowTitle(f"Partial Sell — {card.symbol}")
    layout = QVBoxLayout(dialog)

    layout.addWidget(QLabel(f"Current quantity: {orderable}"))

    row = QHBoxLayout()
    slider = QSlider(Qt.Horizontal)
    slider.setRange(1, orderable)
    slider.setValue(max(1, orderable // 2))
    spin = QSpinBox()
    spin.setRange(1, orderable)
    spin.setValue(slider.value())
    slider.valueChanged.connect(spin.setValue)
    spin.valueChanged.connect(slider.setValue)
    row.addWidget(slider, 1)
    row.addWidget(spin)
    layout.addLayout(row)

    hint = QLabel("")
    hint.setStyleSheet("color: #888; font-size: 11px;")
    layout.addWidget(hint)

    def _update_hint(value: int) -> None:
        pct = value / orderable * 100.0
        if value >= orderable:
            hint.setText(f"{pct:.0f}% of position — this will submit a Sell All.")
        else:
            hint.setText(f"{pct:.0f}% of position")

    spin.valueChanged.connect(_update_hint)
    _update_hint(spin.value())

    buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    layout.addWidget(buttons)

    if dialog.exec_() != QDialog.Accepted:
        return None
    return int(spin.value())


def confirm_sell_all(parent, card: TradeCardState) -> bool:
    quantity = card.orderable_quantity or card.broker_quantity
    reply = QMessageBox.question(
        parent,
        "Sell All",
        f"Submit a full liquidation for {card.symbol} ({quantity} shares)?",
        QMessageBox.Yes | QMessageBox.No,
        QMessageBox.No,
    )
    return reply == QMessageBox.Yes


def prompt_manual_stop_price(parent, card: TradeCardState) -> Optional[float]:
    minimum = card.active_stop_price or 0.0
    dialog = QDialog(parent)
    dialog.setWindowTitle(f"Manual Stop — {card.symbol}")
    layout = QVBoxLayout(dialog)
    layout.addWidget(
        QLabel(
            f"Current active stop: {minimum:,.2f}\n"
            "A manual stop can only tighten risk (must be >= the current stop)."
        )
    )
    spin = QDoubleSpinBox()
    spin.setDecimals(2)
    spin.setRange(minimum, 1_000_000.0)
    spin.setValue(minimum)
    layout.addWidget(spin)

    buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    layout.addWidget(buttons)

    if dialog.exec_() != QDialog.Accepted:
        return None
    return float(spin.value())
