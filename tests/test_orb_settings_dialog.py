import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtWidgets import QApplication, QDialog

from src.risk.orb_position import (
    DEFAULT_ORB_SETTINGS,
    OrbSettings,
    configure_orb_settings,
    get_orb_settings,
)
from src.ui import main_window as main_window_module
from src.ui.orb_settings_dialog import OrbSettingsDialog


_APP = None


def _app():
    global _APP
    _APP = QApplication.instance() or QApplication([])
    return _APP


def test_orb_settings_dialog_shows_defaults_and_restores_them():
    _app()
    custom = OrbSettings(
        capital_min_percent=12.0,
        capital_ideal_percent=20.0,
        capital_max_percent=35.0,
        stop_adr_min_percent=25.0,
        stop_adr_ideal_percent=55.0,
        stop_adr_max_percent=80.0,
    )
    dialog = OrbSettingsDialog(custom)

    assert dialog.values() == custom.to_dict()
    dialog.reset_defaults()
    assert dialog.values() == DEFAULT_ORB_SETTINGS.to_dict()


def test_orb_settings_dialog_rejects_ideal_outside_bounds(monkeypatch):
    _app()
    dialog = OrbSettingsDialog()
    warnings = []
    monkeypatch.setattr(
        "src.ui.orb_settings_dialog.QMessageBox.warning",
        lambda *args: warnings.append(args),
    )
    dialog.spins["capital_min_percent"].setValue(20.0)
    dialog.spins["capital_ideal_percent"].setValue(10.0)

    dialog._accept_if_valid()

    assert dialog.result() != QDialog.Accepted
    assert warnings


def test_main_window_saves_and_applies_accepted_orb_settings(monkeypatch):
    custom = OrbSettings(
        capital_min_percent=12.0,
        capital_ideal_percent=22.0,
        capital_max_percent=36.0,
        stop_adr_min_percent=20.0,
        stop_adr_ideal_percent=50.0,
        stop_adr_max_percent=75.0,
    )
    saved = {}
    log_messages = []
    refresh_calls = []

    class AcceptedDialog:
        Accepted = QDialog.Accepted

        def __init__(self, values, parent):
            assert values == DEFAULT_ORB_SETTINGS.to_dict()
            assert parent is window

        def exec_(self):
            return QDialog.Accepted

        def orb_settings(self):
            return custom

    class FakeWindow:
        settings = {"orb_settings": DEFAULT_ORB_SETTINGS.to_dict()}

        def append_log(self, message):
            log_messages.append(message)

        def refresh_execution_queue(self, env, show_log=True):
            refresh_calls.append((env, show_log))

        def refresh_buyboard(self):
            refresh_calls.append("board")

    window = FakeWindow()
    original = get_orb_settings()
    monkeypatch.setattr(main_window_module, "OrbSettingsDialog", AcceptedDialog)
    monkeypatch.setattr(
        main_window_module,
        "save_json",
        lambda path, values: saved.update({"path": path, "values": values}),
    )
    try:
        main_window_module.MainWindow.show_orb_settings_dialog(window)
        assert window.settings["orb_settings"] == custom.to_dict()
        assert saved["path"] == main_window_module.SETTINGS_FILE
        assert saved["values"]["orb_settings"] == custom.to_dict()
        assert get_orb_settings() == custom
        assert refresh_calls == [("PROD", False), "board"]
        assert log_messages
    finally:
        configure_orb_settings(original)
