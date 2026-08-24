import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import QApplication, QPushButton

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
    scanner_index = tab_labels.index("Scanner")
    assert tab_labels[scanner_index + 1] == "Market Pulse"
    tradingview_index = tab_labels.index("TradingView")
    assert tab_labels[tradingview_index : tradingview_index + 3] == [
        "TradingView",
        "Buy Board",
        "Health",
    ]
    assert "TradingView Chart" not in tab_labels
    assert "Charts" not in tab_labels
    assert "Watchlist" not in tab_labels
    assert "Buy Dashboard" not in tab_labels
    assert not hasattr(window, "main_device_button")
    assert not hasattr(window, "buylist_widget")
    assert not hasattr(window, "watchlist_widget")
    assert not hasattr(window, "watchlist_table")
    assert not hasattr(window, "watchlist_buffer_pct_input")
    assert not hasattr(window, "analyze_stock_ai_button")
    assert not hasattr(window, "dashboard_summary_label")
    assert window.findChild(QPushButton, "refreshSummaryButton") is None
    assert window.refresh_db_button.text() == "Checking Historical 1D Data..."
    assert window.refresh_db_button.isEnabled() is False
    assert window.refresh_hourly_button.text() == "Checking Historical 1H Data..."
    assert window.refresh_hourly_button.isEnabled() is False
    assert not hasattr(window, "save_watchlist_snapshot_button")
    assert not hasattr(window, "live_data_checkbox")
    assert not hasattr(window, "scanner_orb_score_checkbox")
    assert not hasattr(window, "scanner_table")
    assert window.scanner_universe_count_label.text() == "Universe: —"
    assert len(window.active_rule_count_labels) == len(window.active_rule_widgets)
    assert window._scanner_live_refresh_timer.isSingleShot()
    assert window._scanner_live_refresh_timer.interval() == 300
    assert window.buyboard_orb_buffer_pct_input.text() == "0.1"
    header_layout = window.buyboard_widget.layout().itemAt(0).layout()
    assert header_layout.indexOf(window.buyboard_orb_buffer_pct_input) < (
        header_layout.indexOf(window._buyboard_engine_status_label)
    )
    assert window.tradingview_add_watchlist_button.text() in {
        "Add to Watchlist (W)",
        "Remove from Watchlist (W)",
    }
    assert window.tradingview_watchlist_shortcut.isEnabled()
    assert hasattr(window, "add_current_tradingview_symbol_to_watchlist")
    assert hasattr(window, "_update_tradingview_watchlist_btn")
    tools_menu = next(
        action.menu()
        for action in window.menuBar().actions()
        if action.text() == "Tools"
    )
    chart_settings_action = next(
        action for action in tools_menu.actions() if action.text() == "Chart Settings"
    )
    assert chart_settings_action is window.chart_settings_action
    orb_settings_action = next(
        action for action in tools_menu.actions() if action.text() == "ORB Settings"
    )
    assert orb_settings_action is window.orb_settings_action
    assert window.tradingview_show_stock_profile_checkbox.isChecked()
    assert window.tradingview_stock_profile_opacity_slider.value() == 70
    assert window.tradingview_stock_profile_opacity_slider.isHidden()
    assert window.tradingview_show_volume_checkbox.isHidden()
    assert not hasattr(window, "charts_widget")
    assert not hasattr(window, "chart_view")
    assert not hasattr(window, "_build_charts_tab")
    assert "charts" not in window.tab_options
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
    assert sidebar_sources[0] == "Universe"
    assert window.sidebar_source_combo.currentData() == {"type": "universe"}
    assert "Watchlist" in sidebar_sources
    assert "Buylist" in sidebar_sources
    assert "Buy Today" in sidebar_sources
    assert hasattr(window, "sidebar_move_buylist_button")
    assert hasattr(window, "sidebar_remove_watchlist_button")

    window._on_scanner_universe_loaded(["ZZZ", "AAPL"])
    assert [
        window.sidebar_stock_list.item(row).data(Qt.UserRole)["symbol"]
        for row in range(window.sidebar_stock_list.count())
    ] == ["AAPL", "ZZZ"]
    assert window._select_sidebar_universe_symbol("OUTSIDE") is True
    assert window.sidebar_source_combo.currentData() == {"type": "universe"}
    assert window._get_sidebar_selected_symbol() == "OUTSIDE"

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
