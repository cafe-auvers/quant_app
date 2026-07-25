import json
from types import SimpleNamespace


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


def test_account_and_order_reconciliation_workers_keep_separate_state(
    monkeypatch,
):
    import src.ui.order_workers as order_workers
    import src.ui.workers as workers

    monkeypatch.setattr(
        workers,
        "fetch_account_snapshot",
        lambda *args, **kwargs: {"kind": "account"},
    )
    account_results = []
    account_errors = []
    account_worker = workers.KisAccountWorker("PROD", True, True)
    account_worker.finished_snapshot.connect(account_results.append)
    account_worker.error_occurred.connect(account_errors.append)
    account_worker.run()

    open_order = SimpleNamespace(
        client_order_id="client-1",
        execution_policy="REGULAR_LIMIT",
    )
    monkeypatch.setattr(
        order_workers,
        "fetch_account_snapshot",
        lambda *args, **kwargs: {"kind": "reconciliation"},
    )
    monkeypatch.setattr(
        order_workers,
        "reconcile_orders_with_snapshot",
        lambda orders, snapshot, previous_snapshot=None: list(orders),
    )
    reconciliation_results = []
    reconciliation_errors = []
    reconciliation_worker = order_workers.OrderReconciliationWorker(
        "PROD",
        "12345678-01",
        [open_order],
    )
    reconciliation_worker.finished_reconciliation.connect(
        lambda orders, snapshot: reconciliation_results.append((orders, snapshot))
    )
    reconciliation_worker.error_occurred.connect(reconciliation_errors.append)
    reconciliation_worker.run()

    assert account_results == [{"kind": "account"}]
    assert account_errors == []
    assert reconciliation_results == [
        ([open_order], {"kind": "reconciliation"})
    ]
    assert reconciliation_errors == []


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
