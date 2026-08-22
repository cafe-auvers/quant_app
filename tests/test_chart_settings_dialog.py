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
    assert dialog.stock_profile_opacity_slider.value() == 70

    option.setChecked(False)
    dialog.stock_profile_opacity_slider.setValue(45)
    assert dialog.values()["tradingview_show_stock_profile_checkbox"] is False
    assert (
        dialog.values()["tradingview_stock_profile_opacity_slider"] == 45
    )

    dialog.reset_defaults()
    assert option.isChecked()
    assert dialog.stock_profile_opacity_slider.value() == 70
    dialog.close()
