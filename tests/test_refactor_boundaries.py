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


def test_scanner_worker_loads_cold_universe_off_the_calling_ui_path(monkeypatch):
    import src.ui.workers as workers
    import src.utils.data_loader as data_loader
    import src.utils.db_loader as db_loader

    monkeypatch.setattr(
        data_loader, "get_default_universe", lambda max_symbols=None: ["AAPL", "MSFT"]
    )
    monkeypatch.setattr(
        db_loader,
        "get_universe_stock_metrics_from_db",
        lambda tickers, engine: [{"symbol": ticker} for ticker in tickers],
    )
    loaded_universes = []
    results = []
    errors = []
    worker = workers.ScannerWorker(
        tickers=None,
        engine=object(),
        min_volume=0,
        min_dollar_volume=0,
        min_adr=0,
        min_growth_rank=0,
        min_trend_intensity=0,
    )
    worker.universe_loaded.connect(loaded_universes.append)
    worker.finished_scan.connect(lambda metrics, _unused: results.append(metrics))
    worker.error_occurred.connect(errors.append)

    worker.run()

    assert loaded_universes == [["AAPL", "MSFT"]]
    assert results == [[{"symbol": "AAPL"}, {"symbol": "MSFT"}]]
    assert errors == []


def test_database_init_worker_never_raises_connection_errors_into_the_ui(monkeypatch):
    import src.ui.main_window as main_window

    monkeypatch.setattr(
        main_window,
        "resolve_data_engine",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("offline")),
    )
    results = []
    worker = main_window.DatabaseInitWorker()
    worker.initialized.connect(
        lambda engine, source, pc_engine, error: results.append((engine, source, pc_engine, error))
    )

    worker.run()

    assert results == [(None, "none", None, "offline")]


def test_database_init_worker_routes_to_pc_without_opening_local_mirror(monkeypatch):
    import src.ui.main_window as main_window
    import src.utils.db_loader as db_loader

    pc_engine = object()
    local_engine = object()
    report = SimpleNamespace(success=True, errors=())
    resolve_calls = []
    monkeypatch.setattr(
        main_window,
        "resolve_data_engine",
        lambda **kwargs: resolve_calls.append(kwargs) or SimpleNamespace(
            engine=pc_engine,
            source="pc",
            pc_engine=pc_engine,
        ),
    )
    monkeypatch.setattr(db_loader, "init_local_mirror_engine", lambda: local_engine)
    calls = []
    monkeypatch.setattr(
        db_loader,
        "reconcile_local_mirror_with_pc",
        lambda pc, local, **kwargs: calls.append((pc, local, kwargs)) or report,
    )
    results = []
    worker = main_window.DatabaseInitWorker()
    worker.initialized.connect(
        lambda engine, source, pc, error: results.append(
            (engine, source, pc, error)
        )
    )

    worker.run()

    assert resolve_calls == [{"ensure_pc_schema": False}]
    assert calls == []
    assert results == [(pc_engine, "pc", pc_engine, "")]
    assert not hasattr(worker, "local_engine")


def test_database_init_worker_respects_disabled_local_mirror_on_pc(monkeypatch):
    import src.ui.main_window as main_window
    import src.utils.db_loader as db_loader

    pc_engine = object()
    monkeypatch.setattr(
        main_window,
        "resolve_data_engine",
        lambda **_kwargs: SimpleNamespace(
            engine=pc_engine,
            source="pc",
            pc_engine=pc_engine,
        ),
    )
    monkeypatch.setattr(db_loader, "_local_mirror_enabled", lambda: False)
    monkeypatch.setattr(
        db_loader,
        "init_local_mirror_engine",
        lambda: (_ for _ in ()).throw(
            AssertionError("disabled mirror must not be opened")
        ),
    )
    monkeypatch.setattr(
        db_loader,
        "reconcile_local_mirror_with_pc",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("disabled mirror must not be reconciled")
        ),
    )
    results = []
    worker = main_window.DatabaseInitWorker()
    worker.initialized.connect(
        lambda engine, source, pc, error: results.append(
            (engine, source, pc, error)
        )
    )

    worker.run()

    assert results == [(pc_engine, "pc", pc_engine, "")]
    assert not hasattr(worker, "local_engine")


def test_database_init_worker_does_not_wait_for_backup_reconciliation(
    monkeypatch,
):
    import src.ui.main_window as main_window
    import src.utils.db_loader as db_loader

    pc_engine = object()
    local_engine = object()
    report = SimpleNamespace(
        success=False,
        errors=("daily history mismatch",),
    )
    monkeypatch.setattr(
        main_window,
        "resolve_data_engine",
        lambda **_kwargs: SimpleNamespace(
            engine=pc_engine,
            source="pc",
            pc_engine=pc_engine,
        ),
    )
    monkeypatch.setattr(db_loader, "init_local_mirror_engine", lambda: local_engine)
    monkeypatch.setattr(
        db_loader,
        "reconcile_local_mirror_with_pc",
        lambda *_args, **_kwargs: report,
    )
    results = []
    worker = main_window.DatabaseInitWorker()
    worker.initialized.connect(
        lambda engine, source, pc, error: results.append(
            (engine, source, pc, error)
        )
    )

    worker.run()

    assert results == [(pc_engine, "pc", pc_engine, "")]
    assert not hasattr(worker, "pc_candidate_engine")


def test_database_init_worker_never_starts_backup_reconciliation(monkeypatch):
    import src.ui.main_window as main_window
    import src.utils.db_loader as db_loader

    pc_engine = object()
    local_engine = object()
    monkeypatch.setattr(
        main_window,
        "resolve_data_engine",
        lambda **_kwargs: SimpleNamespace(
            engine=pc_engine,
            source="pc",
            pc_engine=pc_engine,
        ),
    )
    monkeypatch.setattr(db_loader, "init_local_mirror_engine", lambda: local_engine)
    monkeypatch.setattr(
        db_loader,
        "reconcile_local_mirror_with_pc",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("sync exploded")
        ),
    )
    results = []
    worker = main_window.DatabaseInitWorker()
    worker.initialized.connect(
        lambda engine, source, pc, error: results.append(
            (engine, source, pc, error)
        )
    )

    worker.run()

    assert results == [(pc_engine, "pc", pc_engine, "")]


def test_completed_daily_fallback_refresh_starts_queued_hourly_refresh(monkeypatch):
    import src.ui.mixins.scanner_mixin as scanner_mixin

    class Window(scanner_mixin.ScannerMixin):
        def __init__(self):
            self._pending_local_mirror_hourly_refresh = True
            self._run_scanners_after_local_mirror_refresh = True
            self.logs = []
            self.hourly_starts = 0
            self.scanner_starts = 0

        def show_refresh_complete(self, updated_count):
            self.updated_count = updated_count

        def update_dashboard_summary(self, **kwargs):
            self.dashboard_updated = kwargs

        def append_log(self, message):
            self.logs.append(message)

        def refresh_hourly_data_to_db(self):
            self.hourly_starts += 1
            return True

        def run_all_scanners(self, **kwargs):
            self.scanner_starts += 1

    monkeypatch.setattr(
        scanner_mixin, "is_refresh_running", lambda mode: (False, {})
    )
    window = Window()

    window._handle_refresh_terminal_status(
        scanner_mixin.MODE_1D,
        {"status": "completed", "result": {"updated_count": 12}},
    )

    assert window.updated_count == 12
    assert window._pending_local_mirror_hourly_refresh is False
    assert window.hourly_starts == 1
    assert window.scanner_starts == 0
    assert "queued 1H" in window.logs[-1]

    window._handle_refresh_terminal_status(
        scanner_mixin.MODE_1H,
        {"status": "completed", "result": {"updated_count": 4}},
    )

    assert window._run_scanners_after_local_mirror_refresh is False
    assert window.scanner_starts == 1


def test_laptop_prompts_and_starts_d10_hourly_refresh_when_only_hourly_is_stale(
    monkeypatch,
):
    import src.ui.main_window as main_window

    class Window:
        def __init__(self):
            self.logs = []
            self.daily_starts = 0
            self.hourly_starts = 0
            self.scanner_starts = 0

        def append_log(self, message):
            self.logs.append(message)

        def refresh_data_to_db(self):
            self.daily_starts += 1
            return True

        def refresh_hourly_data_to_db(self):
            self.hourly_starts += 1
            return True

        def run_all_scanners(self, **_kwargs):
            self.scanner_starts += 1

    questions = []
    monkeypatch.setattr(main_window, "get_default_universe", lambda: ["AAPL"])
    monkeypatch.setattr(
        main_window, "expected_latest_market_data_date", lambda: "2026-08-07"
    )
    monkeypatch.setattr(main_window, "local_mirror_is_stale", lambda *a, **k: False)
    monkeypatch.setattr(
        main_window, "local_mirror_hourly_is_stale", lambda *a, **k: True
    )
    monkeypatch.setattr(main_window, "is_refresh_running", lambda _mode: (False, {}))
    monkeypatch.setattr(
        main_window.QMessageBox,
        "question",
        lambda *args: questions.append(args) or main_window.QMessageBox.Yes,
    )

    window = Window()
    main_window.MainWindow._handle_local_mirror_startup_inner(window, object())

    assert len(questions) == 1
    assert "D-10" in questions[0][2]
    assert window.daily_starts == 0
    assert window.hourly_starts == 1
    assert window._run_scanners_after_local_mirror_refresh is True


def test_manual_refresh_cannot_start_during_database_reconciliation(monkeypatch):
    import src.ui.mixins.scanner_mixin as scanner_mixin

    class Window(scanner_mixin.ScannerMixin):
        _database_reconciliation_in_progress = True
        db_enabled = True

    messages = []
    launches = []
    monkeypatch.setattr(
        scanner_mixin.QMessageBox,
        "information",
        lambda *args: messages.append(args),
    )
    monkeypatch.setattr(
        scanner_mixin,
        "launch_refresh",
        lambda *args, **kwargs: launches.append((args, kwargs)),
    )
    window = Window()

    assert window.refresh_data_to_db() is False
    assert window.refresh_hourly_data_to_db() is False
    assert launches == []
    assert len(messages) == 2


def test_completed_pc_refresh_immediately_tops_up_local_mirror():
    import src.ui.mixins.scanner_mixin as scanner_mixin

    class Window(scanner_mixin.ScannerMixin):
        db_engine_source = "pc"
        _pending_local_mirror_hourly_refresh = False
        _run_scanners_after_local_mirror_refresh = False

        def __init__(self):
            self.mirror_syncs = 0

        def show_refresh_complete(self, _updated_count):
            pass

        def update_dashboard_summary(self, **_kwargs):
            pass

        def _sync_active_pc_to_local_mirror(self):
            self.mirror_syncs += 1

    window = Window()

    window._handle_refresh_terminal_status(
        scanner_mixin.MODE_1D,
        {"status": "completed", "result": {"updated_count": 12}},
    )

    assert window.mirror_syncs == 1


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
