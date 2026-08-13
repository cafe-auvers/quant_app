import pytest

from src.services import trading_state


_qapp = None  # Module-level reference: PyQt5 crashes on widget creation if the
# QApplication singleton is garbage collected, which happens if
# `QApplication.instance() or QApplication([])` is used as a bare statement
# with nothing holding a reference to the result.


def _make_window_with_trading_button():
    global _qapp
    from PyQt5.QtWidgets import QApplication, QPushButton
    import src.ui.main_window as main_window_module
    from src.ui.main_window import MainWindow

    _qapp = QApplication.instance() or QApplication([])
    window = MainWindow.__new__(MainWindow)
    window.trading_enabled_button = QPushButton()
    window.trading_enabled_button.setCheckable(True)
    window.append_log = lambda _message: None
    return window, main_window_module


def test_trading_disabled_by_default():
    # The autouse fixture in conftest.py flips this on for most tests; verify
    # the real module-level default (what every fresh process actually starts
    # with) is disabled.
    trading_state.reset_trading_state_for_tests()
    assert trading_state.is_trading_enabled() is False


def test_set_trading_enabled_toggles_state():
    assert trading_state.set_trading_enabled(True) is True
    assert trading_state.is_trading_enabled() is True

    assert trading_state.set_trading_enabled(False) is False
    assert trading_state.is_trading_enabled() is False


def test_env_lock_forces_disabled_even_when_toggled_on(monkeypatch):
    monkeypatch.setattr(trading_state, "get_env_value", lambda key, default=None: "false")

    trading_state.set_trading_enabled(True)

    assert trading_state.is_trading_locked_disabled() is True
    assert trading_state.is_trading_enabled() is False


def test_env_lock_recognizes_common_falsy_spellings(monkeypatch):
    for spelling in ["0", "false", "False", "no", "NO", "off", " Off "]:
        monkeypatch.setattr(trading_state, "get_env_value", lambda key, default=None, v=spelling: v)
        assert trading_state.is_trading_locked_disabled() is True


@pytest.mark.parametrize("value", ["", " ", "flase", "enabled", "2"])
def test_env_lock_fails_closed_for_blank_or_unrecognized_values(
    monkeypatch, value
):
    monkeypatch.setattr(
        trading_state,
        "get_env_value",
        lambda key, default=None: value,
    )

    assert trading_state.is_trading_locked_disabled() is True
    assert trading_state.set_trading_enabled(True) is False


def test_env_lock_clears_hidden_armed_state(monkeypatch):
    configured_value = ["true"]
    monkeypatch.setattr(
        trading_state,
        "get_env_value",
        lambda key, default=None: configured_value[0],
    )
    assert trading_state.set_trading_enabled(True) is True

    configured_value[0] = "false"
    assert trading_state.is_trading_enabled() is False

    # Removing the lock must not resurrect the old armed state. A fresh user
    # confirmation is required after every administrative lock.
    configured_value[0] = "true"
    assert trading_state.is_trading_enabled() is False


def test_env_value_unset_or_truthy_does_not_lock_or_auto_enable(monkeypatch):
    monkeypatch.setattr(trading_state, "get_env_value", lambda key, default=None: None)
    assert trading_state.is_trading_locked_disabled() is False
    # Absence of the env var must never auto-enable trading on its own.
    trading_state.reset_trading_state_for_tests()
    assert trading_state.is_trading_enabled() is False

    monkeypatch.setattr(trading_state, "get_env_value", lambda key, default=None: "true")
    assert trading_state.is_trading_locked_disabled() is False


def test_toolbar_toggle_requires_confirmation_to_enable(monkeypatch):
    from PyQt5.QtWidgets import QMessageBox

    trading_state.reset_trading_state_for_tests()
    window, main_window_module = _make_window_with_trading_button()
    monkeypatch.setattr(main_window_module.QMessageBox, "question", lambda *a, **k: QMessageBox.No)

    window.trading_enabled_button.setChecked(True)
    main_window_module.MainWindow._on_trading_enabled_toggled(window, True)

    assert trading_state.is_trading_enabled() is False
    assert window.trading_enabled_button.isChecked() is False


def test_toolbar_toggle_enables_after_confirmation(monkeypatch):
    from PyQt5.QtWidgets import QMessageBox

    trading_state.reset_trading_state_for_tests()
    window, main_window_module = _make_window_with_trading_button()
    monkeypatch.setattr(main_window_module.QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)

    main_window_module.MainWindow._on_trading_enabled_toggled(window, True)

    assert trading_state.is_trading_enabled() is True
    assert window.trading_enabled_button.isChecked() is True
    assert "ON" in window.trading_enabled_button.text()


def test_toolbar_toggle_disables_without_confirmation(monkeypatch):
    from PyQt5.QtWidgets import QMessageBox

    trading_state.set_trading_enabled(True)
    window, main_window_module = _make_window_with_trading_button()
    monkeypatch.setattr(
        main_window_module.QMessageBox,
        "question",
        lambda *a, **k: pytest.fail("disabling must not prompt for confirmation"),
    )

    main_window_module.MainWindow._on_trading_enabled_toggled(window, False)

    assert trading_state.is_trading_enabled() is False
    assert "DISABLED" in window.trading_enabled_button.text()


def test_toolbar_reflects_env_lock_as_disabled_and_uneditable(monkeypatch):
    window, main_window_module = _make_window_with_trading_button()
    monkeypatch.setattr(trading_state, "get_env_value", lambda key, default=None: "false")

    main_window_module.MainWindow._refresh_trading_enabled_widget(window)

    assert window.trading_enabled_button.isChecked() is False
    assert window.trading_enabled_button.isEnabled() is False
    assert "LOCKED" in window.trading_enabled_button.text()
