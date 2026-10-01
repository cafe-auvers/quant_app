from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QKeySequence
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import (
    QApplication,
    QAbstractItemView,
    QComboBox,
    QCompleter,
    QShortcut,
    QWidget,
)

from src.ui.charts.controller_layout import (
    ShortcutSafeSymbolComboBox,
    configure_symbol_entry_combo,
)
from src.ui.charts.controller_navigation import ChartsNavigationMixin


_APP = None


class _SymbolEntryHarness(ChartsNavigationMixin):
    def __init__(self, combo: QComboBox) -> None:
        self.tradingview_symbol_combo = combo


def test_manual_symbol_entry_keeps_focus_beyond_three_characters():
    global _APP
    _APP = QApplication.instance() or QApplication([])
    combo = QComboBox()
    configure_symbol_entry_combo(combo)
    combo.addItems(["AAPL", "ETHA", "GOOG", "MSFT"])
    harness = _SymbolEntryHarness(combo)
    combo.lineEdit().textEdited.connect(harness.filter_tradingview_symbol_combo)
    combo.show()
    combo.lineEdit().clear()
    combo.lineEdit().setFocus()
    _APP.processEvents()

    for character in "etha":
        focused = _APP.focusWidget()
        assert focused is not None
        assert not isinstance(focused, QAbstractItemView)
        QTest.keyClick(focused, character)
        _APP.processEvents()

    assert combo.currentText() == "ETHA"
    assert combo.count() == 4

    completer = combo.completer()
    assert completer.caseSensitivity() == Qt.CaseInsensitive
    assert completer.filterMode() == Qt.MatchStartsWith
    assert completer.completionMode() == QCompleter.PopupCompletion

    combo.close()


def test_noneditable_chart_symbol_combo_ignores_shortcut_letters():
    global _APP
    _APP = QApplication.instance() or QApplication([])
    combo = ShortcutSafeSymbolComboBox()
    combo.addItems(["AAPL", "WEX", "TSLA"])
    combo.setCurrentText("AAPL")
    combo.show()
    combo.setFocus()
    _APP.processEvents()

    for key in (Qt.Key_W, Qt.Key_T, Qt.Key_D, Qt.Key_B, Qt.Key_A):
        QTest.keyClick(combo, key)
        _APP.processEvents()
        assert combo.currentText() == "AAPL"

    combo.close()


def test_chart_scoped_shortcut_does_not_consume_manual_symbol_text():
    global _APP
    _APP = QApplication.instance() or QApplication([])
    container = QWidget()
    combo = QComboBox(container)
    configure_symbol_entry_combo(combo)
    chart = QWidget(container)
    shortcut = QShortcut(QKeySequence("W"), chart)
    shortcut.setContext(Qt.WidgetWithChildrenShortcut)
    activations = []
    shortcut.activated.connect(lambda: activations.append(True))
    container.show()

    combo.lineEdit().clear()
    combo.lineEdit().setFocus()
    _APP.processEvents()
    QTest.keyClick(combo.lineEdit(), Qt.Key_W)
    _APP.processEvents()

    assert combo.currentText() == "w"
    assert activations == []

    chart.setFocus()
    _APP.processEvents()
    QTest.keyClick(chart, Qt.Key_W)
    _APP.processEvents()
    assert activations == [True]

    container.close()
