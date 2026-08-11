import datetime as dt
from types import SimpleNamespace

from sqlalchemy import create_engine, text

from src.services.pc_remote_control import PcServiceStatus, PcStatus
from src.services.runtime_status import (
    get_runtime_process_status,
    mark_runtime_process_stopped,
    record_runtime_heartbeat,
)
from src.ui.main_window import MainWindow
from src.ui.workers import PcRemoteStatusWorker


def test_runtime_heartbeat_reports_active_and_explicitly_stopped_process():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)

    record_runtime_heartbeat(engine, hostname="DATA-PC", pid=123)
    active = get_runtime_process_status(engine, "data-pc")

    assert active.observed is True
    assert active.active is True
    assert active.pid == 123

    mark_runtime_process_stopped(engine, hostname="DATA-PC", pid=123)
    stopped = get_runtime_process_status(engine, "data-pc")

    assert stopped.observed is True
    assert stopped.active is False


def test_runtime_heartbeat_becomes_inactive_when_stale():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    record_runtime_heartbeat(engine, hostname="data-pc", pid=123)
    stale_time = (
        dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
        - dt.timedelta(minutes=5)
    )
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE app_runtime_status "
                "SET heartbeat_at = :heartbeat_at "
                "WHERE hostname = 'data-pc' AND process_name = 'main.py'"
            ),
            {"heartbeat_at": stale_time},
        )

    status = get_runtime_process_status(
        engine,
        "data-pc",
        max_age_seconds=60,
    )

    assert status.observed is True
    assert status.active is False
    assert status.age_seconds is not None
    assert status.age_seconds >= 240


def test_pc_status_worker_checks_database_when_listener_is_off(monkeypatch):
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    monkeypatch.setattr(
        "src.services.pc_remote_control.check_pc_status",
        lambda: PcStatus.OFF,
    )
    results = []
    worker = PcRemoteStatusWorker(engine)
    worker.finished_status.connect(results.append)

    worker.run()

    assert len(results) == 1
    assert results[0].database_ready is True
    assert results[0].listener_status == PcStatus.OFF
    assert results[0].main_app_active is True


def test_runtime_monitoring_failure_does_not_hide_database_connectivity(monkeypatch):
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    monkeypatch.setattr(
        "src.services.pc_remote_control.check_pc_status",
        lambda: PcStatus.OFF,
    )
    monkeypatch.setattr(
        "src.services.runtime_status.record_runtime_heartbeat",
        lambda _engine: (_ for _ in ()).throw(RuntimeError("heartbeat unavailable")),
    )
    results = []
    worker = PcRemoteStatusWorker(engine)
    worker.finished_status.connect(results.append)

    worker.run()

    assert results[0].database_ready is True
    assert results[0].main_app_active is None


def test_pc_status_worker_silences_expected_mysql_probe_failures(monkeypatch):
    monkeypatch.setattr(
        "src.services.pc_remote_control.check_pc_status",
        lambda: PcStatus.OFF,
    )
    probe_calls = []

    def unavailable_engine(*, log_unavailable=True, ensure_schema=True):
        probe_calls.append((log_unavailable, ensure_schema))
        return None

    monkeypatch.setattr(
        "src.utils.db_loader.init_mysql_engine",
        unavailable_engine,
    )
    results = []
    worker = PcRemoteStatusWorker()
    worker.finished_status.connect(results.append)

    worker.run()

    assert probe_calls == [(False, False)]
    assert results[0].database_ready is False


class _SignalStub:
    def __init__(self):
        self.callbacks = []

    def connect(self, callback):
        self.callbacks.append(callback)


class _StatusWorkerStub:
    instances = []

    def __init__(self, engine=None, parent=None):
        self.engine = engine
        self.parent = parent
        self.finished_status = _SignalStub()
        self.finished = _SignalStub()
        self.started = False
        self.deleted = False
        self.__class__.instances.append(self)

    def start(self):
        self.started = True

    def deleteLater(self):
        self.deleted = True


def test_pc_status_poll_waits_for_database_initialization(monkeypatch):
    import src.ui.main_window as main_window

    _StatusWorkerStub.instances = []
    monkeypatch.setattr(main_window, "PcRemoteStatusWorker", _StatusWorkerStub)
    window = MainWindow.__new__(MainWindow)
    window.db_initializing = True
    window.pc_db_engine = None
    window._pc_status_worker = None

    window._poll_pc_status()

    assert _StatusWorkerStub.instances == []


def test_pc_status_poll_retains_worker_until_finished(monkeypatch):
    import src.ui.main_window as main_window

    _StatusWorkerStub.instances = []
    monkeypatch.setattr(main_window, "PcRemoteStatusWorker", _StatusWorkerStub)
    window = MainWindow.__new__(MainWindow)
    window.db_initializing = False
    window.pc_db_engine = object()
    window._pc_probe_engine = window.pc_db_engine
    window._pc_status_worker = None

    window._poll_pc_status()
    worker = _StatusWorkerStub.instances[0]

    assert worker.parent is window
    assert worker.started is True
    window._poll_pc_status()
    assert _StatusWorkerStub.instances == [worker]

    window._on_pc_status_worker_finished(worker)

    assert window._pc_status_worker is None
    assert worker.deleted is True


class _WidgetStub:
    def __init__(self):
        self.text = ""
        self.enabled = None
        self.tooltip = ""
        self.stylesheet = ""

    def setText(self, value):
        self.text = value

    def setEnabled(self, value):
        self.enabled = value

    def setStyleSheet(self, value):
        self.stylesheet = value

    def setToolTip(self, value):
        self.tooltip = value


class _ProgressBarStub:
    def __init__(self):
        self.minimum = 0
        self.maximum = 100
        self.value = 0

    def setRange(self, minimum, maximum):
        self.minimum = minimum
        self.maximum = maximum

    def setValue(self, value):
        self.value = value


class _ProgressLabelStub:
    def __init__(self):
        self.value = ""
        self.tooltip = ""

    def setText(self, value):
        self.value = value

    def text(self):
        return self.value

    def setToolTip(self, value):
        self.tooltip = value


class _StateSaveManagerStub:
    def __init__(self):
        self.engine_bindings = []

    def set_engine(self, engine, *, device_id="", is_main_device=False):
        self.engine_bindings.append((engine, device_id, is_main_device))


class _RecoveryWorkerStub:
    instances = []

    def __init__(self, generation, **kwargs):
        self.generation = generation
        self.kwargs = kwargs
        self.recovered = _SignalStub()
        self.finished = _SignalStub()
        self.started = False
        self.__class__.instances.append(self)

    def start(self):
        self.started = True


class _CompletedMirrorWorkerStub:
    def __init__(self, pc_engine, local_engine):
        self.pc_engine = pc_engine
        self.local_engine = local_engine
        self.deleted = False

    def deleteLater(self):
        self.deleted = True


def _runtime_transition_window(pc_engine, *, local_engine=None):
    window = MainWindow.__new__(MainWindow)
    window.pc_status_dot = _WidgetStub()
    window.pc_status_label = _WidgetStub()
    window.pc_services_label = _WidgetStub()
    window.pc_status_button = _WidgetStub()
    window.database_source_dot = _WidgetStub()
    window.database_source_label = _WidgetStub()
    window.main_device_button = _WidgetStub()
    window.db_engine = pc_engine
    window.pc_db_engine = pc_engine
    window._pc_probe_engine = pc_engine
    window._local_mirror_engine = local_engine
    window.db_engine_source = "pc"
    window.db_enabled = True
    window.db_initializing = False
    window.database_recovery_worker = None
    window._pc_database_ready = True
    window._last_pc_database_probe_ready = True
    window._database_transition_generation = 0
    window._database_shutting_down = False
    window._database_reconciliation_in_progress = False
    window._last_database_reconciliation_notice = ""
    window._initial_state_sync_complete = True
    window._cached_market_data_status = object()
    window.universe_tickers = []
    window.state_sync_role = SimpleNamespace(
        device_id="laptop-id",
        is_main=True,
    )
    window.state_sync_worker = None
    manager = _StateSaveManagerStub()
    window.state_save_manager = manager
    logs = []
    summary_updates = []
    window.append_log = logs.append
    window.update_dashboard_summary = lambda: summary_updates.append(True)
    return window, manager, logs, summary_updates


def _offline_pc_status():
    return PcServiceStatus(
        listener_status=PcStatus.OFF,
        database_ready=False,
    )


def _online_pc_status():
    return PcServiceStatus(
        listener_status=PcStatus.OFF,
        database_ready=True,
        database_hostname="data-pc",
    )


def test_runtime_pc_database_loss_switches_to_local_mirror_and_detaches_state_sync(
    monkeypatch,
):
    import src.utils.db_loader as db_loader

    pc_engine = object()
    local_engine = object()
    window, manager, logs, summary_updates = _runtime_transition_window(pc_engine)
    mirror_opens = []
    monkeypatch.setattr(
        db_loader,
        "init_local_mirror_engine",
        lambda: mirror_opens.append(True) or local_engine,
    )

    window._on_pc_status_result(_offline_pc_status())

    assert mirror_opens == [True]
    assert window.db_engine is local_engine
    assert window.db_engine_source == "local_mirror"
    assert window.db_enabled is True
    assert window.database_source_label.text == "DB: Local"
    assert "#ffb300" in window.database_source_dot.stylesheet
    assert window.pc_db_engine is None
    assert window._pc_probe_engine is pc_engine
    assert window._pc_database_ready is False
    assert window._initial_state_sync_complete is False
    assert manager.engine_bindings == [(None, "laptop-id", False)]
    assert summary_updates == [True]
    assert any("switched automatically to the local data mirror" in line for line in logs)


def test_repeated_runtime_offline_status_does_not_repeat_failover(monkeypatch):
    import src.utils.db_loader as db_loader

    pc_engine = object()
    local_engine = object()
    window, manager, logs, summary_updates = _runtime_transition_window(pc_engine)
    mirror_opens = []
    monkeypatch.setattr(
        db_loader,
        "init_local_mirror_engine",
        lambda: mirror_opens.append(True) or local_engine,
    )
    status = _offline_pc_status()

    window._on_pc_status_result(status)
    window._on_pc_status_result(status)

    assert mirror_opens == [True]
    assert manager.engine_bindings == [(None, "laptop-id", False)]
    assert window._database_transition_generation == 1
    assert summary_updates == [True]
    assert sum("switched automatically" in line for line in logs) == 1


def test_runtime_database_loss_without_local_mirror_shows_offline_red(monkeypatch):
    import src.utils.db_loader as db_loader

    window, _manager, _logs, _summary_updates = _runtime_transition_window(
        object()
    )
    monkeypatch.setattr(db_loader, "init_local_mirror_engine", lambda: None)

    window._on_pc_status_result(_offline_pc_status())

    assert window.db_engine is None
    assert window.db_engine_source == "none"
    assert window.db_enabled is False
    assert window.database_source_label.text == "DB: Offline"
    assert "#f23645" in window.database_source_dot.stylesheet


def test_periodic_state_sync_is_gated_after_runtime_failover(monkeypatch):
    import src.ui.main_window as main_window

    pc_engine = object()
    local_engine = object()
    window, _manager, _logs, _summary_updates = _runtime_transition_window(
        pc_engine,
        local_engine=local_engine,
    )
    worker_creations = []
    monkeypatch.setattr(
        main_window,
        "StateSyncWorker",
        lambda *args, **kwargs: worker_creations.append((args, kwargs)),
    )

    window._on_pc_status_result(_offline_pc_status())
    window._start_state_sync()

    assert window.pc_db_engine is None
    assert window._pc_database_ready is False
    assert worker_creations == []


def test_repeated_online_probe_starts_only_one_database_recovery(monkeypatch):
    import src.ui.main_window as main_window

    pc_engine = object()
    local_engine = object()
    window, _manager, _logs, _summary_updates = _runtime_transition_window(
        pc_engine,
        local_engine=local_engine,
    )
    window._on_pc_status_result(_offline_pc_status())
    window._pc_probe_engine = None
    _RecoveryWorkerStub.instances = []
    monkeypatch.setattr(main_window, "DatabaseRecoveryWorker", _RecoveryWorkerStub)

    status = _online_pc_status()
    window._on_pc_status_result(status)
    window._on_pc_status_result(status)

    assert len(_RecoveryWorkerStub.instances) == 1
    worker = _RecoveryWorkerStub.instances[0]
    assert worker.generation == 1
    assert worker.kwargs == {"pc_engine": None}
    assert worker.started is True
    assert window.database_recovery_worker is worker
    assert window.db_engine is local_engine
    assert window.db_engine_source == "local_mirror"
    assert window.database_source_label.text == "DB: Local"


def test_local_mirror_to_pc_recovery_switches_immediately_and_starts_backup(
    monkeypatch,
):
    import src.ui.main_window as main_window

    pc_engine = object()
    local_engine = object()
    window, manager, logs, summary_updates = _runtime_transition_window(
        pc_engine,
        local_engine=local_engine,
    )
    state_sync_starts = []
    mirror_sync_starts = []
    window._start_state_sync = lambda: state_sync_starts.append(True)
    window._start_background_local_mirror_sync = mirror_sync_starts.append
    _RecoveryWorkerStub.instances = []
    monkeypatch.setattr(main_window, "DatabaseRecoveryWorker", _RecoveryWorkerStub)

    window._on_pc_status_result(_offline_pc_status())
    status = _online_pc_status()
    window._on_pc_status_result(status)
    window._on_pc_status_result(status)

    assert window.db_engine is pc_engine
    assert window.pc_db_engine is pc_engine
    assert window.db_engine_source == "pc"
    assert window.db_enabled is True
    assert window.database_source_label.text == "DB: PC"
    assert "#26a69a" in window.database_source_dot.stylesheet
    assert window._pc_database_ready is True
    assert window._database_transition_generation == 2
    assert manager.engine_bindings == [
        (None, "laptop-id", False),
        (pc_engine, "laptop-id", False),
    ]
    assert state_sync_starts == [True]
    assert mirror_sync_starts == [pc_engine]
    assert _RecoveryWorkerStub.instances == []
    assert summary_updates == [True, True]
    assert sum("back online" in line for line in logs) == 1
    assert not any("synchronized before switching" in line for line in logs)


def test_background_backup_error_never_changes_active_pc_routing(
    monkeypatch,
):
    import src.ui.main_window as main_window

    pc_engine = object()
    local_engine = object()
    window, manager, logs, summary_updates = _runtime_transition_window(
        pc_engine,
        local_engine=local_engine,
    )
    mirror_worker = _CompletedMirrorWorkerStub(pc_engine, local_engine)
    window._local_mirror_sync_worker = mirror_worker
    _RecoveryWorkerStub.instances = []
    monkeypatch.setattr(main_window, "DatabaseRecoveryWorker", _RecoveryWorkerStub)

    window._on_local_mirror_sync_completed(
        {},
        "Local mirror contains laptop-side writes.",
        True,
    )

    assert window.db_engine is pc_engine
    assert window.db_engine_source == "pc"
    assert window.db_enabled is True
    assert window.pc_db_engine is pc_engine
    assert window._pc_probe_engine is pc_engine
    assert window._pc_database_ready is True
    assert window._database_transition_generation == 0
    assert manager.engine_bindings == []
    assert summary_updates == []
    assert _RecoveryWorkerStub.instances == []
    assert any("mirror sync incomplete" in line for line in logs)

    window._clear_worker_reference("_local_mirror_sync_worker", mirror_worker)

    assert mirror_worker.deleted is True
    assert window._local_mirror_sync_worker is None
    assert _RecoveryWorkerStub.instances == []


def test_stale_active_mirror_result_cannot_change_current_database_routing():
    pc_engine = object()
    local_engine = object()
    window, manager, logs, summary_updates = _runtime_transition_window(
        pc_engine,
        local_engine=local_engine,
    )
    window._database_transition_generation = 4

    window._on_local_mirror_sync_completed(
        {},
        "Local mirror is dirty.",
        True,
        3,
    )

    assert window.db_engine is pc_engine
    assert window.pc_db_engine is pc_engine
    assert window.db_engine_source == "pc"
    assert window._pc_database_ready is True
    assert manager.engine_bindings == []
    assert logs == []
    assert summary_updates == []


def test_local_backup_changes_cannot_block_pc_recovery(monkeypatch):
    import src.ui.main_window as main_window
    import src.utils.db_loader as db_loader

    pc_engine = object()
    local_engine = object()
    window, manager, logs, _summary_updates = _runtime_transition_window(
        pc_engine,
        local_engine=local_engine,
    )
    _RecoveryWorkerStub.instances = []
    monkeypatch.setattr(main_window, "DatabaseRecoveryWorker", _RecoveryWorkerStub)
    state_sync_starts = []
    backup_starts = []
    window._start_state_sync = lambda: state_sync_starts.append(True)
    window._start_background_local_mirror_sync = backup_starts.append
    monkeypatch.setattr(
        db_loader,
        "acquire_local_mirror_handoff_guard",
        lambda _engine: (_ for _ in ()).throw(
            RuntimeError("Local mirror changed after reconciliation.")
        ),
    )

    window._on_pc_status_result(_offline_pc_status())
    window._on_pc_status_result(_online_pc_status())

    assert window.db_engine is pc_engine
    assert window.db_engine_source == "pc"
    assert window.pc_db_engine is pc_engine
    assert manager.engine_bindings == [
        (None, "laptop-id", False),
        (pc_engine, "laptop-id", False),
    ]
    assert state_sync_starts == [True]
    assert backup_starts == [pc_engine]
    assert not any("handoff" in line for line in logs)


def test_failed_pc_connection_check_stays_local_and_retries(monkeypatch):
    import src.ui.main_window as main_window

    pc_engine = object()
    local_engine = object()
    window, manager, logs, summary_updates = _runtime_transition_window(
        pc_engine,
        local_engine=local_engine,
    )
    state_sync_starts = []
    window._start_state_sync = lambda: state_sync_starts.append(True)
    _RecoveryWorkerStub.instances = []
    monkeypatch.setattr(main_window, "DatabaseRecoveryWorker", _RecoveryWorkerStub)

    window._on_pc_status_result(_offline_pc_status())
    window._pc_probe_engine = None
    window._on_pc_status_result(_online_pc_status())
    outcome = main_window.DatabaseRecoveryOutcome(
        None,
        False,
        error="PC MySQL is no longer reachable.",
    )

    window._on_database_recovery_finished(outcome, generation=1)

    assert window.db_engine is local_engine
    assert window.db_engine_source == "local_mirror"
    assert window.pc_db_engine is None
    assert window.database_source_label.text == "DB: Local"
    assert "#ffb300" in window.database_source_dot.stylesheet
    assert manager.engine_bindings == [(None, "laptop-id", False)]
    assert state_sync_starts == []
    assert summary_updates == [True]
    assert any("connection is unavailable" in line for line in logs)

    first_worker = window.database_recovery_worker
    window._clear_worker_reference("database_recovery_worker", first_worker)
    window._on_pc_status_result(_online_pc_status())
    assert len(_RecoveryWorkerStub.instances) == 2


def test_existing_backup_writer_does_not_delay_pc_recovery(monkeypatch):
    import src.ui.main_window as main_window

    pc_engine = object()
    local_engine = object()
    window, _manager, _logs, _summary_updates = _runtime_transition_window(
        pc_engine,
        local_engine=local_engine,
    )
    _RecoveryWorkerStub.instances = []
    monkeypatch.setattr(main_window, "DatabaseRecoveryWorker", _RecoveryWorkerStub)
    state_sync_starts = []
    window._start_state_sync = lambda: state_sync_starts.append(True)
    window._on_pc_status_result(_offline_pc_status())
    previous_mirror_worker = object()
    window._local_mirror_sync_worker = previous_mirror_worker

    window._on_pc_status_result(_online_pc_status())

    assert _RecoveryWorkerStub.instances == []
    assert window.db_engine is pc_engine
    assert window.db_engine_source == "pc"
    assert state_sync_starts == [True]


def test_local_startup_uses_normal_offline_prompt_path():
    pc_engine = object()
    local_engine = object()
    window, _manager, logs, _summary_updates = _runtime_transition_window(
        pc_engine,
        local_engine=local_engine,
    )
    window.database_init_worker = SimpleNamespace(
        local_engine=local_engine,
        pc_candidate_engine=None,
        reconciliation_result=None,
    )
    prompt_calls = []
    scanner_calls = []
    poll_calls = []
    window._handle_local_mirror_startup = lambda _engine: prompt_calls.append(True)
    window.run_all_scanners = lambda **kwargs: scanner_calls.append(kwargs)
    window._poll_pc_status = lambda: poll_calls.append(True)

    window._on_optional_database_initialized(
        local_engine,
        "local_mirror",
        None,
        "daily history mismatch",
    )

    assert prompt_calls == [True]
    assert scanner_calls == []
    assert poll_calls == [True]
    assert window.db_engine is local_engine
    assert window.db_engine_source == "local_mirror"
    assert window._pc_probe_engine is None


def test_pc_startup_routes_immediately_then_starts_backup_in_background():
    pc_engine = object()
    window, manager, logs, summary_updates = _runtime_transition_window(
        pc_engine
    )
    window.database_init_worker = SimpleNamespace(
        local_engine=None,
        pc_candidate_engine=None,
        reconciliation_result=None,
    )
    state_sync_starts = []
    scanner_starts = []
    backup_starts = []
    poll_starts = []
    window._start_state_sync = lambda: state_sync_starts.append(True)
    window.run_all_scanners = lambda **kwargs: scanner_starts.append(kwargs)
    window._start_background_local_mirror_sync = backup_starts.append
    window._poll_pc_status = lambda: poll_starts.append(True)

    window._on_optional_database_initialized(
        pc_engine,
        "pc",
        pc_engine,
        "",
    )

    assert window.db_engine is pc_engine
    assert window.db_engine_source == "pc"
    assert window._local_mirror_engine is None
    assert manager.engine_bindings == [(pc_engine, "laptop-id", False)]
    assert state_sync_starts == [True]
    assert scanner_starts == [{"show_warnings": False}]
    assert backup_starts == [pc_engine]
    assert poll_starts == [True]
    assert summary_updates == [True]
    assert any("using it immediately" in line for line in logs)


def test_database_recovery_worker_only_checks_pc_connectivity(monkeypatch):
    import src.ui.main_window as main_window
    import src.utils.db_loader as db_loader

    pc_engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    calls = []
    monkeypatch.setattr(
        db_loader,
        "reconcile_local_mirror_with_pc",
        lambda pc, local, **kwargs: calls.append((pc, local, kwargs)),
    )
    emitted = []
    worker = main_window.DatabaseRecoveryWorker(
        9,
        pc_engine=pc_engine,
    )
    worker.recovered.connect(lambda outcome, generation: emitted.append((outcome, generation)))

    worker.run()

    assert calls == []
    outcome, generation = emitted[0]
    assert generation == 9
    assert outcome.success is True
    assert outcome.error == ""


def test_relevant_hourly_symbols_include_scan_watchlist_and_buylist():
    window = SimpleNamespace(
        watchlist=SimpleNamespace(
            items=[SimpleNamespace(symbol="msft"), SimpleNamespace(symbol="AAPL")]
        ),
        buylist_manager=SimpleNamespace(
            items=[SimpleNamespace(symbol="NVDA"), SimpleNamespace(symbol="MSFT")]
        ),
        scanner_results=[{"symbol": "tsla"}],
        scanner_results_by_setup={
            "Setup 1": [{"symbol": "AMZN"}, {"symbol": "aapl"}]
        },
    )

    assert MainWindow._relevant_hourly_symbols(window) == [
        "SPY",
        "AAPL",
        "AMZN",
        "MSFT",
        "NVDA",
        "TSLA",
    ]


def test_laptop_backup_progress_is_shown_in_dashboard_progress_widgets():
    window = MainWindow.__new__(MainWindow)
    window._database_shutting_down = False
    window._database_transition_generation = 4
    window.progress_bar = _ProgressBarStub()
    window.progress_label = _ProgressLabelStub()

    window._on_local_mirror_sync_progress(
        "Reading PC data: hourly_price_history",
        8,
        20,
        4,
    )

    assert window.progress_bar.minimum == 0
    assert window.progress_bar.maximum == 20
    assert window.progress_bar.value == 8
    assert window.progress_label.value == (
        "Laptop backup — Checking PC 1-hour prices | "
        "8 / 20 records (40%) | ETA calculating"
    )
    assert "already using PC MySQL" in window.progress_label.tooltip


def test_laptop_backup_progress_estimates_eta_from_record_rate(monkeypatch):
    import src.ui.main_window as main_window

    window = MainWindow.__new__(MainWindow)
    window._database_shutting_down = False
    window._database_transition_generation = 4
    window.progress_bar = _ProgressBarStub()
    window.progress_label = _ProgressLabelStub()
    clock = iter((100.0, 110.0))
    monkeypatch.setattr(main_window.time, "monotonic", lambda: next(clock))

    window._on_local_mirror_sync_progress(
        "Reading PC data: price_history",
        100,
        1000,
        4,
    )
    window._on_local_mirror_sync_progress(
        "Reading PC data: price_history",
        300,
        1000,
        4,
    )

    assert "300 / 1,000 records (30%)" in window.progress_label.value
    assert "ETA less than 1 min" in window.progress_label.value


def test_completed_checkpoint_check_reports_already_up_to_date(monkeypatch):
    import src.ui.main_window as main_window

    window = MainWindow.__new__(MainWindow)
    window._database_shutting_down = False
    window._database_transition_generation = 4
    window._local_mirror_sync_worker = SimpleNamespace(local_engine=None)
    window._local_mirror_sync_log_completion = True
    window.progress_bar = _ProgressBarStub()
    window.progress_label = _ProgressLabelStub()
    window.append_log = lambda _message: None
    monkeypatch.setattr(main_window.QTimer, "singleShot", lambda *_args: None)

    window._on_local_mirror_sync_completed({}, "", False, 4)

    assert window.progress_bar.value == 100
    assert window.progress_label.value == "Laptop backup already up to date."


def test_shutdown_message_identifies_laptop_backup_and_current_phase():
    mirror_worker = object()
    window = SimpleNamespace(
        _local_mirror_sync_worker=mirror_worker,
        _local_mirror_progress_phase="Verifying laptop backup: price_history",
    )

    title, message = MainWindow._shutdown_wait_message(
        window,
        [mirror_worker],
    )

    assert title == "Laptop backup finishing"
    assert "PC-to-laptop safety backup" in message
    assert "Verifying laptop backup: price_history" in message
    assert "not downloading market data for trading" in message


def test_backup_worker_initializes_local_mirror_and_uses_pc_as_authority(monkeypatch):
    import src.ui.main_window as main_window
    import src.utils.db_loader as db_loader

    local_engine = object()
    calls = []
    monkeypatch.setattr(
        db_loader,
        "init_local_mirror_engine",
        lambda: local_engine,
    )
    monkeypatch.setattr(
        db_loader,
        "sync_local_mirror_from_pc_checkpointed",
        lambda pc, local, **kwargs: calls.append((pc, local, kwargs)) or {},
    )
    emitted = []
    progress_events = []
    pc_engine = object()
    worker = main_window.LocalMirrorSyncWorker(
        pc_engine,
        None,
        hourly_symbols=["SPY", "AAPL"],
        generation=12,
    )
    worker.completed.connect(
        lambda written, error, needs_reconciliation, generation: emitted.append(
            (written, error, needs_reconciliation, generation)
        )
    )
    worker.progress.connect(
        lambda phase, current, total, generation: progress_events.append(
            (phase, current, total, generation)
        )
    )

    worker.run()

    assert len(calls) == 1
    called_pc, called_local, kwargs = calls[0]
    assert called_pc is pc_engine
    assert called_local is local_engine
    progress_callback = kwargs.pop("progress_callback")
    cancellation_callback = kwargs.pop("cancellation_callback")
    assert kwargs == {
        "hourly_symbols": ["SPY", "AAPL"],
    }
    assert cancellation_callback() is False
    progress_callback("Reading PC data: hourly_price_history", 2, 10)
    assert progress_events == [
        ("Reading PC data: hourly_price_history", 2, 10, 12)
    ]
    assert worker.local_engine is local_engine
    assert emitted == [({}, "", False, 12)]


def test_database_recovery_worker_contains_connection_failure(monkeypatch):
    import src.ui.main_window as main_window
    import src.utils.db_loader as db_loader

    monkeypatch.setattr(
        db_loader,
        "init_mysql_engine",
        lambda **_kwargs: None,
    )
    emitted = []
    worker = main_window.DatabaseRecoveryWorker(
        3,
    )
    worker.recovered.connect(lambda outcome, generation: emitted.append((outcome, generation)))

    worker.run()

    outcome, generation = emitted[0]
    assert generation == 3
    assert outcome.success is False
    assert outcome.error == "PC MySQL is no longer reachable."


def test_backup_worker_reports_error_without_requesting_reroute(monkeypatch):
    import src.ui.main_window as main_window
    import src.utils.db_loader as db_loader

    def require_staged_reconciliation(*_args, **_kwargs):
        raise db_loader.LocalMirrorNeedsReconciliationError(
            "Local mirror contains laptop-side writes."
        )

    monkeypatch.setattr(
        db_loader,
        "sync_local_mirror_from_pc_checkpointed",
        require_staged_reconciliation,
    )
    emitted = []
    worker = main_window.LocalMirrorSyncWorker(
        object(),
        object(),
        generation=7,
    )
    worker.completed.connect(
        lambda written, error, needs_reconciliation, generation: emitted.append(
            (written, error, needs_reconciliation, generation)
        )
    )

    worker.run()

    assert emitted == [
        ({}, "Local mirror contains laptop-side writes.", False, 7)
    ]


def test_active_mirror_worker_forwards_relevant_hourly_symbols(monkeypatch):
    import src.ui.main_window as main_window
    import src.utils.db_loader as db_loader

    calls = []
    monkeypatch.setattr(
        db_loader,
        "sync_local_mirror_from_pc_checkpointed",
        lambda pc, local, **kwargs: calls.append((pc, local, kwargs)) or {},
    )
    emitted = []
    pc_engine = object()
    local_engine = object()
    worker = main_window.LocalMirrorSyncWorker(
        pc_engine,
        local_engine,
        hourly_symbols=["SPY", "AAPL"],
        generation=8,
    )
    worker.completed.connect(
        lambda written, error, needs_reconciliation, generation: emitted.append(
            (written, error, needs_reconciliation, generation)
        )
    )

    worker.run()

    assert len(calls) == 1
    called_pc, called_local, kwargs = calls[0]
    assert called_pc is pc_engine
    assert called_local is local_engine
    assert callable(kwargs.pop("progress_callback"))
    assert callable(kwargs.pop("cancellation_callback"))
    assert kwargs == {
        "hourly_symbols": ["SPY", "AAPL"],
    }
    assert emitted == [({}, "", False, 8)]


def test_database_recovery_result_is_disposed_while_window_is_closing():
    import src.ui.main_window as main_window

    class CandidateEngine:
        def __init__(self):
            self.dispose_calls = 0

        def dispose(self):
            self.dispose_calls += 1

    pc_engine = object()
    window, _manager, _logs, _summary_updates = _runtime_transition_window(
        pc_engine
    )
    window._database_shutting_down = True
    candidate = CandidateEngine()

    outcome = main_window.DatabaseRecoveryOutcome(
        candidate,
        True,
    )
    window._on_database_recovery_finished(outcome, generation=0)

    assert candidate.dispose_calls == 1
    assert window.pc_db_engine is pc_engine


def test_transition_error_is_contained_inside_pc_status_slot():
    pc_engine = object()
    window, _manager, logs, _summary_updates = _runtime_transition_window(pc_engine)

    def fail_transition():
        raise RuntimeError("transition failed")

    window._switch_to_runtime_local_mirror = fail_transition

    window._on_pc_status_result(_offline_pc_status())

    assert window.pc_status_label.text == "PC: Unreachable"
    assert any("keep retrying" in line for line in logs)


def test_database_source_indicator_maps_pc_local_and_offline_colors():
    window = MainWindow.__new__(MainWindow)
    window.database_source_dot = _WidgetStub()
    window.database_source_label = _WidgetStub()
    window.db_initializing = False

    cases = [
        ("pc", object(), True, "DB: PC", "#26a69a"),
        ("local_mirror", object(), True, "DB: Local", "#ffb300"),
        ("none", None, False, "DB: Offline", "#f23645"),
    ]
    for source, engine, enabled, expected_text, expected_color in cases:
        window.db_engine_source = source
        window.db_engine = engine
        window.db_enabled = enabled

        window._update_database_source_indicator()

        assert window.database_source_label.text == expected_text
        assert expected_color in window.database_source_dot.stylesheet


def test_ui_keeps_pc_on_when_database_works_but_listener_is_off():
    window = MainWindow.__new__(MainWindow)
    engine = object()
    window.db_engine_source = "pc"
    window.pc_db_engine = engine
    window._pc_probe_engine = engine
    window._pc_database_ready = True
    window._last_pc_main_app_active = True
    window.pc_status_dot = _WidgetStub()
    window.pc_status_label = _WidgetStub()
    window.pc_services_label = _WidgetStub()
    window.pc_status_button = _WidgetStub()
    status = PcServiceStatus(
        listener_status=PcStatus.OFF,
        database_ready=True,
        database_hostname="data-pc",
        main_app_active=True,
    )

    window._on_pc_status_result(status)

    assert window.pc_status_label.text == "PC: On"
    assert window.pc_services_label.text == "PC DB: On | Listener: Off | main.py: On"
    assert window.pc_status_button.text == "Remote Control Offline"
    assert window.pc_status_button.enabled is False
    assert window._pc_is_on is True
