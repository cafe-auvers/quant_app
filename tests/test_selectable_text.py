"""Tests for application-wide selectable dashboard text."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtCore import QEvent, Qt
from PyQt5.QtWidgets import QApplication, QLabel, QMessageBox, QWidget

from src.ui.selectable_text import (
    install_selectable_text_support,
    make_label_text_selectable,
)


_APP = QApplication.instance() or QApplication([])


def _is_mouse_and_keyboard_selectable(label: QLabel) -> bool:
    flags = label.textInteractionFlags()
    return bool(flags & Qt.TextSelectableByMouse) and bool(
        flags & Qt.TextSelectableByKeyboard
    )


def test_make_label_text_selectable_preserves_link_interaction() -> None:
    label = QLabel('<a href="https://example.com">Details</a>')
    label.setTextInteractionFlags(
        Qt.LinksAccessibleByMouse | Qt.LinksAccessibleByKeyboard
    )

    make_label_text_selectable(label)

    flags = label.textInteractionFlags()
    assert _is_mouse_and_keyboard_selectable(label)
    assert flags & Qt.LinksAccessibleByMouse
    assert flags & Qt.LinksAccessibleByKeyboard


def test_filter_makes_dynamically_shown_labels_selectable() -> None:
    install_selectable_text_support(_APP)
    parent = QWidget()
    label = QLabel("Scanner error details", parent)

    parent.show()
    _APP.processEvents()

    assert _is_mouse_and_keyboard_selectable(label)
    parent.close()


def test_filter_makes_message_box_text_selectable() -> None:
    text_filter = install_selectable_text_support(_APP)
    message_box = QMessageBox()
    message_box.setWindowTitle("Scanner error")
    message_box.setText("Unable to query scanner metrics")

    message_labels = [
        label for label in message_box.findChildren(QLabel) if label.text()
    ]
    for label in message_labels:
        text_filter.eventFilter(label, QEvent(QEvent.Show))

    assert message_labels
    assert all(_is_mouse_and_keyboard_selectable(label) for label in message_labels)
