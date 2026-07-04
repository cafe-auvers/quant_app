import json


def test_refactored_ui_modules_importable():
    from src.ui.chart_bridge import ChartBridge
    from src.ui.controllers import (
        AccountController,
        BuylistExecutionController,
        ChartDataController,
        ScannerController,
        WatchlistController,
    )
    from src.ui.filter_catalog import DEFAULT_SCANNER_SETUPS, DEFAULT_TAB_OPTIONS
    from src.ui.main_window import MainWindow, _extract_latest_opening_bar
    from src.ui.workers import ScannerWorker, WatchlistAiWorker

    assert AccountController is not None
    assert BuylistExecutionController is not None
    assert ChartBridge is not None
    assert ChartDataController is not None
    assert MainWindow is not None
    assert ScannerController is not None
    assert WatchlistController is not None
    assert _extract_latest_opening_bar is not None
    assert ScannerWorker is not None
    assert WatchlistAiWorker is not None
    assert set(DEFAULT_SCANNER_SETUPS) == {"Setup 1", "Setup 2"}
    assert DEFAULT_TAB_OPTIONS["tradingview"] is True


def test_app_state_save_preserves_json_shapes(tmp_path, monkeypatch):
    import src.services.app_state as app_state

    monkeypatch.setattr(app_state, "WATCHLIST_FILE", tmp_path / "watchlist.json")
    monkeypatch.setattr(app_state, "BUYLIST_FILE", tmp_path / "buylist.json")
    monkeypatch.setattr(app_state, "TRADE_PLANS_FILE", tmp_path / "trade_plans.json")
    monkeypatch.setattr(app_state, "SCANNER_SETUPS_FILE", tmp_path / "scanner_setups.json")
    monkeypatch.setattr(app_state, "CHART_DRAWINGS_FILE", tmp_path / "chart_drawings.json")
    monkeypatch.setattr(app_state, "TAB_OPTIONS_FILE", tmp_path / "tab_options.json")
    monkeypatch.setattr(app_state, "STATE_METADATA_FILE", tmp_path / "state_metadata.json")

    scanner_setups = {"Setup 1": {"rules": []}}
    chart_drawings = {"AAPL": []}
    tab_options = {"dashboard": True, "scanner": False}

    thread = app_state.save_app_state(
        {"name": "Default", "items": []},
        {"items": []},
        {"plans": []},
        scanner_setups,
        chart_drawings,
        tab_options,
    )
    assert thread.daemon is False
    thread.join(timeout=2)

    assert json.loads((tmp_path / "watchlist.json").read_text()) == {"name": "Default", "items": []}
    assert json.loads((tmp_path / "buylist.json").read_text()) == {"items": []}
    assert json.loads((tmp_path / "trade_plans.json").read_text()) == {"plans": []}
    assert json.loads((tmp_path / "scanner_setups.json").read_text()) == {"setups": scanner_setups}
    assert json.loads((tmp_path / "chart_drawings.json").read_text()) == chart_drawings
    assert json.loads((tmp_path / "tab_options.json").read_text()) == {"tabs": tab_options}
