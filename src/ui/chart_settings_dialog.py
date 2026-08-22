"""TradingView chart display settings dialog."""

from __future__ import annotations

from typing import Mapping

from PyQt5.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QGridLayout,
    QGroupBox,
    QLabel,
    QVBoxLayout,
)


TRADINGVIEW_CHART_SETTINGS = (
    ("tradingview_split_screen_checkbox", "Split 1D / 1H", True),
    ("tradingview_show_volume_checkbox", "Volume", True),
    ("tradingview_show_ema_checkbox", "EMA 10/20/50", True),
    ("tradingview_show_rs_checkbox", "RS/TI65", True),
    ("tradingview_show_adr_checkbox", "ADR", True),
    ("tradingview_show_growth_1m_checkbox", "1M growth", True),
    ("tradingview_show_growth_3m_checkbox", "3M growth", True),
    ("tradingview_show_growth_6m_checkbox", "6M growth", False),
    ("tradingview_show_earnings_checkbox", "Earnings", True),
    (
        "tradingview_show_stock_profile_checkbox",
        "Stock information (symbol, long name, sector and detail)",
        True,
    ),
)

CHART_SETTING_GROUPS = (
    ("TradingView Chart tab", TRADINGVIEW_CHART_SETTINGS),
)

CHART_SETTING_DEFAULTS = {
    name: default
    for _title, definitions in CHART_SETTING_GROUPS
    for name, _label, default in definitions
}


class ChartSettingsDialog(QDialog):
    """Edit TradingView chart options without crowding its toolbar."""

    def __init__(
        self,
        values: Mapping[str, bool] | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Chart Settings")
        self.setMinimumWidth(640)
        current_values = dict(CHART_SETTING_DEFAULTS)
        current_values.update(values or {})
        self.checkboxes: dict[str, QCheckBox] = {}

        layout = QVBoxLayout(self)
        description = QLabel(
            "Choose the overlays and information shown on the TradingView chart."
        )
        description.setWordWrap(True)
        layout.addWidget(description)

        for title, definitions in CHART_SETTING_GROUPS:
            group = QGroupBox(title)
            group_layout = QGridLayout(group)
            for index, (name, label, _default) in enumerate(definitions):
                checkbox = QCheckBox(label)
                checkbox.setObjectName(name)
                checkbox.setChecked(bool(current_values[name]))
                group_layout.addWidget(checkbox, index // 2, index % 2)
                self.checkboxes[name] = checkbox
            layout.addWidget(group)

        buttons = QDialogButtonBox(
            QDialogButtonBox.RestoreDefaults
            | QDialogButtonBox.Ok
            | QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        buttons.button(QDialogButtonBox.RestoreDefaults).clicked.connect(
            self.reset_defaults
        )
        layout.addWidget(buttons)

    def values(self) -> dict[str, bool]:
        return {
            name: checkbox.isChecked()
            for name, checkbox in self.checkboxes.items()
        }

    def reset_defaults(self) -> None:
        for name, default in CHART_SETTING_DEFAULTS.items():
            self.checkboxes[name].setChecked(default)
