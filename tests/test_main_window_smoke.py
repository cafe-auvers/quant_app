import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtCore import QTimer
from PyQt5.QtWidgets import QApplication

from src.core.watchlist import BuylistManager, TradePlanManager, Watchlist
from src.services.state_sync import LocalDeviceRole
from src.ui import main_window as main_window_module
from src.ui.charts import controller_layout

_APP = None


class _InMemoryStateSaveManager:
    def set_engine(self, *_args, **_kwargs):
        return None


def test_main_window_constructs_and_closes_offscreen_without_external_io(monkeypatch):
    global _APP
    _APP = QApplication.instance() or QApplication([])

    monkeypatch.setattr(controller_layout, "QWebEngineView", None)
    monkeypatch.setattr(QTimer, "start", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(QTimer, "singleShot", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(main_window_module, "load_order_ledger", lambda: [])
    monkeypatch.setattr(
        main_window_module,
        "load_local_device_role",
        lambda: LocalDeviceRole("test-device", "test-host", False),
    )
    monkeypatch.setattr(
        main_window_module,
        "get_state_save_manager",
        lambda: _InMemoryStateSaveManager(),
    )
    monkeypatch.setattr(main_window_module.MainWindow, "_load_watchlist", lambda _self: Watchlist())
    monkeypatch.setattr(main_window_module.MainWindow, "_load_buylist", lambda _self: BuylistManager())
    monkeypatch.setattr(
        main_window_module.MainWindow,
        "_load_trade_plans",
        lambda _self: TradePlanManager(),
    )
    monkeypatch.setattr(
        main_window_module.MainWindow,
        "_load_scanner_setups",
        lambda _self: {
            name: values.copy()
            for name, values in main_window_module.DEFAULT_SCANNER_SETUPS.items()
        },
    )
    monkeypatch.setattr(main_window_module.MainWindow, "_load_chart_drawings", lambda _self: {})
    monkeypatch.setattr(
        main_window_module.MainWindow,
        "_load_tab_options",
        lambda _self: dict(main_window_module.DEFAULT_TAB_OPTIONS),
    )
    monkeypatch.setattr(
        main_window_module,
        "load_json",
        lambda _path, default: dict(default),
    )
    monkeypatch.setattr(main_window_module, "reconcile_stale_status", lambda _mode: None)
    monkeypatch.setattr(main_window_module, "read_status", lambda _mode: {})
    monkeypatch.setattr(main_window_module.MainWindow, "_poll_refresh_status", lambda _self: None)
    monkeypatch.setattr(
        main_window_module.MainWindow,
        "_flush_state_saves_for_shutdown",
        lambda _self, timeout=5.0: SimpleNamespace(success=True, error=""),
    )

    from src.services import runtime_status

    monkeypatch.setattr(
        runtime_status, "safe_mark_runtime_process_stopped", lambda _engine: None
    )

    window = main_window_module.MainWindow()
    window.show()
    assert window.centralWidget() is not None
    assert window.tabs.count() > 0
    tab_labels = [window.tabs.tabText(index) for index in range(window.tabs.count())]
    tradingview_index = tab_labels.index("TradingView Chart")
    assert tab_labels[tradingview_index + 1] == "Health"

    assert window.close() is True
    assert window._database_shutting_down is True
