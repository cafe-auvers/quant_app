import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtWidgets import QApplication

from src.ui.chart_settings_dialog import ChartSettingsDialog


_APP = None


def test_stock_information_option_defaults_on_and_can_be_changed():
    global _APP
    _APP = QApplication.instance() or QApplication([])

    dialog = ChartSettingsDialog()
    option = dialog.checkboxes["tradingview_show_stock_profile_checkbox"]

    assert len(dialog.checkboxes) == 10
    assert all(name.startswith("tradingview_") for name in dialog.checkboxes)
    assert option.isChecked()

    option.setChecked(False)
    assert dialog.values()["tradingview_show_stock_profile_checkbox"] is False

    dialog.reset_defaults()
    assert option.isChecked()
    dialog.close()
