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

    def unavailable_engine(*, log_unavailable=True):
        probe_calls.append(log_unavailable)
        return None

    monkeypatch.setattr(
        "src.utils.db_loader.init_mysql_engine",
        unavailable_engine,
    )
    results = []
    worker = PcRemoteStatusWorker()
    worker.finished_status.connect(results.append)

    worker.run()

    assert probe_calls == [False]
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
    assert worker.kwargs["local_engine"] is local_engine
    assert worker.started is True
    assert window.database_recovery_worker is worker
    assert window.db_engine is local_engine
    assert window.db_engine_source == "local_mirror"
    assert window.database_source_label.text == "DB: Local (Syncing...)"


def test_local_mirror_to_pc_recovery_is_staged_and_idempotent(monkeypatch):
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

    assert window.db_engine is local_engine
    assert window.pc_db_engine is None
    assert window.db_engine_source == "local_mirror"
    assert window.database_source_label.text == "DB: Local (Syncing...)"
    assert len(_RecoveryWorkerStub.instances) == 1
    assert manager.engine_bindings == [(None, "laptop-id", False)]
    assert state_sync_starts == []

    reconciliation = SimpleNamespace(
        total_local_to_pc_rows=3,
        total_pc_to_local_rows=8,
    )
    outcome = main_window.DatabaseRecoveryOutcome(
        pc_engine,
        True,
        reconciled_local_mirror=True,
        reconciliation_result=reconciliation,
    )
    window._on_database_recovery_finished(outcome, generation=1)
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
    assert mirror_sync_starts == []
    assert summary_updates == [True, True]
    assert sum("back online" in line for line in logs) == 1
    assert sum("market data synchronized" in line for line in logs) == 1


def test_active_pc_dirty_mirror_stages_local_until_normal_recovery_starts(
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

    assert window.db_engine is local_engine
    assert window.db_engine_source == "local_mirror"
    assert window.db_enabled is True
    assert window.database_source_label.text == "DB: Local (Syncing...)"
    assert "#ffb300" in window.database_source_dot.stylesheet
    assert window.pc_db_engine is None
    assert window._pc_probe_engine is pc_engine
    assert window._pc_database_ready is False
    assert window._database_transition_generation == 1
    assert manager.engine_bindings == [(None, "laptop-id", False)]
    assert summary_updates == [True]
    assert _RecoveryWorkerStub.instances == []
    assert any("before switching back to PC" in line for line in logs)

    window._clear_worker_reference("_local_mirror_sync_worker", mirror_worker)

    assert mirror_worker.deleted is True
    assert window._local_mirror_sync_worker is None
    assert len(_RecoveryWorkerStub.instances) == 1
    recovery_worker = _RecoveryWorkerStub.instances[0]
    assert recovery_worker.kwargs["pc_engine"] is pc_engine
    assert recovery_worker.kwargs["local_engine"] is local_engine
    assert recovery_worker.generation == 1
    assert recovery_worker.started is True


def test_active_pc_dirty_mirror_then_verified_recovery_switches_green(monkeypatch):
    import src.ui.main_window as main_window

    pc_engine = object()
    local_engine = object()
    window, manager, _logs, _summary_updates = _runtime_transition_window(
        pc_engine,
        local_engine=local_engine,
    )
    state_sync_starts = []
    window._start_state_sync = lambda: state_sync_starts.append(True)
    mirror_worker = _CompletedMirrorWorkerStub(pc_engine, local_engine)
    window._local_mirror_sync_worker = mirror_worker
    _RecoveryWorkerStub.instances = []
    monkeypatch.setattr(main_window, "DatabaseRecoveryWorker", _RecoveryWorkerStub)

    window._on_local_mirror_sync_completed({}, "Local mirror is dirty.", True)
    window._clear_worker_reference("_local_mirror_sync_worker", mirror_worker)
    reconciliation = SimpleNamespace(
        total_local_to_pc_rows=2,
        total_pc_to_local_rows=5,
    )
    outcome = main_window.DatabaseRecoveryOutcome(
        pc_engine,
        True,
        reconciled_local_mirror=True,
        reconciliation_result=reconciliation,
    )
    window._on_database_recovery_finished(outcome, generation=1)

    assert window.db_engine is pc_engine
    assert window.db_engine_source == "pc"
    assert window.database_source_label.text == "DB: PC"
    assert "#26a69a" in window.database_source_dot.stylesheet
    assert window._database_transition_generation == 2
    assert manager.engine_bindings == [
        (None, "laptop-id", False),
        (pc_engine, "laptop-id", False),
    ]
    assert state_sync_starts == [True]


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


def test_local_write_after_reconciliation_cancels_pc_handoff(monkeypatch):
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
    monkeypatch.setattr(
        db_loader,
        "acquire_local_mirror_handoff_guard",
        lambda _engine: (_ for _ in ()).throw(
            RuntimeError("Local mirror changed after reconciliation.")
        ),
    )

    window._on_pc_status_result(_offline_pc_status())
    window._on_pc_status_result(_online_pc_status())
    reconciliation = SimpleNamespace(
        total_local_to_pc_rows=1,
        total_pc_to_local_rows=2,
        local_handoff_ready=True,
    )
    outcome = main_window.DatabaseRecoveryOutcome(
        pc_engine,
        True,
        reconciled_local_mirror=True,
        reconciliation_result=reconciliation,
    )

    window._on_database_recovery_finished(outcome, generation=1)

    assert window.db_engine is local_engine
    assert window.db_engine_source == "local_mirror"
    assert window.pc_db_engine is None
    assert manager.engine_bindings == [(None, "laptop-id", False)]
    assert any("handoff changed" in line for line in logs)


def test_failed_reconciliation_stays_on_local_and_keeps_state_detached(monkeypatch):
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
    window._on_pc_status_result(_online_pc_status())
    outcome = main_window.DatabaseRecoveryOutcome(
        pc_engine,
        False,
        error="price_history did not converge",
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
    assert any("staying on the local database" in line for line in logs)

    first_worker = window.database_recovery_worker
    window._clear_worker_reference("database_recovery_worker", first_worker)
    window._on_pc_status_result(_online_pc_status())
    assert len(_RecoveryWorkerStub.instances) == 2


def test_recovery_waits_for_existing_pc_to_local_mirror_writer(monkeypatch):
    import src.ui.main_window as main_window

    pc_engine = object()
    local_engine = object()
    window, _manager, _logs, _summary_updates = _runtime_transition_window(
        pc_engine,
        local_engine=local_engine,
    )
    _RecoveryWorkerStub.instances = []
    monkeypatch.setattr(main_window, "DatabaseRecoveryWorker", _RecoveryWorkerStub)
    window._on_pc_status_result(_offline_pc_status())
    previous_mirror_worker = object()
    window._local_mirror_sync_worker = previous_mirror_worker

    window._on_pc_status_result(_online_pc_status())

    assert _RecoveryWorkerStub.instances == []
    assert window.db_engine is local_engine
    assert window.db_engine_source == "local_mirror"

    window._local_mirror_sync_worker = None
    window._on_pc_status_result(_online_pc_status())
    assert len(_RecoveryWorkerStub.instances) == 1


def test_failed_startup_reconciliation_skips_pc_unreachable_local_prompt():
    pc_engine = object()
    local_engine = object()
    window, _manager, logs, _summary_updates = _runtime_transition_window(
        pc_engine,
        local_engine=local_engine,
    )
    window.database_init_worker = SimpleNamespace(
        local_engine=local_engine,
        pc_candidate_engine=pc_engine,
        reconciliation_result=SimpleNamespace(success=False),
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

    assert prompt_calls == []
    assert scanner_calls == [{"show_warnings": False}]
    assert poll_calls == [True]
    assert window.db_engine is local_engine
    assert window.db_engine_source == "local_mirror"
    assert window._pc_probe_engine is pc_engine
    assert any("retrying automatically" in line for line in logs)


def test_database_recovery_worker_emits_verified_reconciliation(monkeypatch):
    import src.ui.main_window as main_window
    import src.utils.db_loader as db_loader

    pc_engine = object()
    local_engine = object()
    reconciliation = SimpleNamespace(success=True, errors=())
    calls = []
    monkeypatch.setattr(
        db_loader,
        "reconcile_local_mirror_with_pc",
        lambda pc, local, *, tickers=None: calls.append((pc, local, tickers))
        or reconciliation,
    )
    emitted = []
    worker = main_window.DatabaseRecoveryWorker(
        9,
        pc_engine=pc_engine,
        local_engine=local_engine,
        tickers=["AAPL"],
    )
    worker.recovered.connect(lambda outcome, generation: emitted.append((outcome, generation)))

    worker.run()

    assert calls == [(pc_engine, local_engine, ["AAPL"])]
    outcome, generation = emitted[0]
    assert generation == 9
    assert outcome.success is True
    assert outcome.reconciled_local_mirror is True
    assert outcome.reconciliation_result is reconciliation


def test_database_recovery_worker_contains_reconciliation_failure(monkeypatch):
    import src.ui.main_window as main_window
    import src.utils.db_loader as db_loader

    reconciliation = SimpleNamespace(
        success=False,
        errors=("price_history mismatch",),
    )
    monkeypatch.setattr(
        db_loader,
        "reconcile_local_mirror_with_pc",
        lambda *_args, **_kwargs: reconciliation,
    )
    emitted = []
    worker = main_window.DatabaseRecoveryWorker(
        3,
        pc_engine=object(),
        local_engine=object(),
    )
    worker.recovered.connect(lambda outcome, generation: emitted.append((outcome, generation)))

    worker.run()

    outcome, generation = emitted[0]
    assert generation == 3
    assert outcome.success is False
    assert outcome.error == "price_history mismatch"


def test_active_mirror_worker_emits_typed_staged_reconciliation(monkeypatch):
    import src.ui.main_window as main_window
    import src.utils.db_loader as db_loader

    def require_staged_reconciliation(*_args, **_kwargs):
        raise db_loader.LocalMirrorNeedsReconciliationError(
            "Local mirror contains laptop-side writes."
        )

    monkeypatch.setattr(
        db_loader,
        "sync_local_mirror_from_pc_atomic",
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
        ({}, "Local mirror contains laptop-side writes.", True, 7)
    ]


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
        reconciled_local_mirror=True,
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
