import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import QApplication

from src.core.trade_card_state import BoardStatus, TradeCardState
from src.core.watchlist import BuylistManager, TradePlanManager, Watchlist
from src.services.state_sync import LocalDeviceRole
from src.ui import main_window as main_window_module
from src.ui.charts import controller_layout
from src.ui.health import panel as health_panel_module

_APP = None


class _InMemoryStateSaveManager:
    def set_engine(self, *_args, **_kwargs):
        return None


def test_main_window_constructs_and_closes_offscreen_without_external_io(monkeypatch):
    global _APP
    _APP = QApplication.instance() or QApplication([])

    watchlist = Watchlist()
    watchlist.add("STIM", "Neuronetics")

    monkeypatch.setattr(controller_layout, "QWebEngineView", None)
    monkeypatch.setattr(health_panel_module, "QWebEngineView", None)
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
    monkeypatch.setattr(
        main_window_module.MainWindow, "_load_watchlist", lambda _self: watchlist
    )
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
        "refresh_health_panel",
        lambda _self: None,
    )
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
    assert "Buy Board" in tab_labels
    assert "Watchlist" not in tab_labels
    assert "Buy Dashboard" not in tab_labels
    assert not hasattr(window, "main_device_button")
    assert not hasattr(window, "buylist_widget")
    assert not hasattr(window, "watchlist_widget")
    assert not hasattr(window, "watchlist_table")
    assert not hasattr(window, "watchlist_buffer_pct_input")
    assert not hasattr(window, "analyze_stock_ai_button")
    assert not hasattr(window, "save_watchlist_snapshot_button")
    assert not hasattr(window, "live_data_checkbox")
    assert window.buyboard_orb_buffer_pct_input.text() == "0.1"
    header_layout = window.buyboard_widget.layout().itemAt(0).layout()
    assert header_layout.indexOf(window.buyboard_orb_buffer_pct_input) < (
        header_layout.indexOf(window._buyboard_engine_status_label)
    )
    assert window.tradingview_add_watchlist_button.text() in {
        "Add to Watchlist (W)",
        "In Watchlist (W)",
    }
    assert window.tradingview_watchlist_shortcut.isEnabled()
    assert hasattr(window, "add_current_tradingview_symbol_to_watchlist")
    assert hasattr(window, "_update_tradingview_watchlist_btn")
    assert window.intraday_symbol_combo.count() == 0
    assert not hasattr(window, "intraday_status_label")
    assert not hasattr(window, "intraday_chart_view")
    assert not hasattr(window, "_build_watchlist_tab")
    assert not hasattr(window, "run_watchlist_ai_review")
    assert not hasattr(window, "populate_watchlist_table")
    sidebar_sources = [
        window.sidebar_source_combo.itemText(index)
        for index in range(window.sidebar_source_combo.count())
    ]
    assert "Watchlist" in sidebar_sources
    assert "Buylist" in sidebar_sources
    assert "Buy Today" in sidebar_sources
    assert hasattr(window, "sidebar_move_buylist_button")
    assert hasattr(window, "sidebar_remove_watchlist_button")

    health_index = tab_labels.index("Health")
    window.tabs.setCurrentIndex(health_index)
    QApplication.processEvents()
    assert window.stock_sidebar.isVisible()

    window._buyboard_current_projections = (
        TradeCardState(
            environment="PROD",
            account_no="12345678",
            symbol="NVDA",
            name="NVIDIA",
            board_status=BoardStatus.BUY_TODAY,
            breakout_price=180.0,
        ),
        TradeCardState(
            environment="PROD",
            account_no="12345678",
            symbol="AAPL",
            board_status=BoardStatus.BUYLIST,
        ),
    )
    buy_today_index = sidebar_sources.index("Buy Today")
    window.sidebar_source_combo.setCurrentIndex(buy_today_index)
    QApplication.processEvents()
    assert window.sidebar_stock_list.count() == 1
    buy_today_data = window.sidebar_stock_list.item(0).data(Qt.UserRole)
    assert buy_today_data["symbol"] == "NVDA"
    assert buy_today_data["source"] == "buy_today"
    assert buy_today_data["account_no"] == "12345678"
    assert window.sidebar_add_watchlist_button.isEnabled() is False
    from src.ui.buyboard.columns import BOARD_COLUMN_ORDER

    assert set(window.buyboard_columns.keys()) == set(BOARD_COLUMN_ORDER)
    assert window.close() is True
    assert window._database_shutting_down is True
