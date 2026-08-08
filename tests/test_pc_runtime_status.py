import datetime as dt

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


class _WidgetStub:
    def __init__(self):
        self.text = ""
        self.enabled = None
        self.tooltip = ""

    def setText(self, value):
        self.text = value

    def setEnabled(self, value):
        self.enabled = value

    def setStyleSheet(self, value):
        pass

    def setToolTip(self, value):
        self.tooltip = value


def test_ui_keeps_pc_on_when_database_works_but_listener_is_off():
    window = MainWindow.__new__(MainWindow)
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
    assert window.pc_services_label.text == "DB: On | Listener: Off | main.py: On"
    assert window.pc_status_button.text == "Remote Control Offline"
    assert window.pc_status_button.enabled is False
    assert window._pc_is_on is True
