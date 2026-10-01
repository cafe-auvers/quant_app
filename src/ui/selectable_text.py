"""Application-wide support for selecting and copying displayed text."""

from __future__ import annotations

from typing import Optional

from PyQt5.QtCore import QEvent, QObject, Qt
from PyQt5.QtWidgets import QApplication, QLabel, QWidget


_FILTER_ATTRIBUTE = "_dashboard_selectable_text_filter"
_SELECTABLE_FLAGS = Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard


def make_label_text_selectable(label: QLabel) -> None:
    """Allow drag selection and keyboard copying without disabling links."""

    current_flags = label.textInteractionFlags()
    desired_flags = current_flags | _SELECTABLE_FLAGS
    if desired_flags != current_flags:
        label.setTextInteractionFlags(desired_flags)


def make_widget_text_selectable(widget: QWidget) -> None:
    """Apply selectable-text behavior to every label below *widget*."""

    if isinstance(widget, QLabel):
        make_label_text_selectable(widget)
    for label in widget.findChildren(QLabel):
        make_label_text_selectable(label)


class SelectableTextEventFilter(QObject):
    """Keep labels selectable as dialogs and dashboard widgets are created."""

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        # Each QLabel receives its own Show event, including the internal
        # labels created by QMessageBox.  Updating only that label avoids
        # mutating an entire widget tree while Qt is midway through showing it.
        if event.type() == QEvent.Show and isinstance(watched, QLabel):
            make_label_text_selectable(watched)
        return False


def install_selectable_text_support(
    app: Optional[QApplication] = None,
) -> SelectableTextEventFilter:
    """Install the application filter once and update existing widgets."""

    application = app or QApplication.instance()
    if application is None:
        raise RuntimeError("A QApplication is required for selectable text support.")

    installed = getattr(application, _FILTER_ATTRIBUTE, None)
    if isinstance(installed, SelectableTextEventFilter):
        text_filter = installed
    else:
        text_filter = SelectableTextEventFilter(application)
        application.installEventFilter(text_filter)
        setattr(application, _FILTER_ATTRIBUTE, text_filter)

    for widget in application.topLevelWidgets():
        make_widget_text_selectable(widget)
    return text_filter
