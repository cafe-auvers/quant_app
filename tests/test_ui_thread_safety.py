import os
import threading
import time


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5 import sip
from PyQt5.QtCore import (
    QCoreApplication,
    QEvent,
    QObject,
    QThread,
    pyqtSignal,
    pyqtSlot,
)

from src.ui.main_window import MainWindow


def test_windows_qt_rendering_defaults_are_safe_and_overridable(monkeypatch):
    import main

    monkeypatch.delenv("QT_OPENGL", raising=False)
    monkeypatch.delenv("QTWEBENGINE_CHROMIUM_FLAGS", raising=False)
    main._configure_qt_rendering_environment("win32")

    assert os.environ["QT_OPENGL"] == "software"
    assert os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] == "--disable-gpu"

    monkeypatch.setenv("QT_OPENGL", "desktop")
    monkeypatch.setenv("QTWEBENGINE_CHROMIUM_FLAGS", "--use-angle=gl")
    main._configure_qt_rendering_environment("win32")

    assert os.environ["QT_OPENGL"] == "desktop"
    assert os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] == "--use-angle=gl"


class _RecordingScrollBar:
    def __init__(self):
        self.value = 0

    @staticmethod
    def maximum() -> int:
        return 0

    def setValue(self, value: int) -> None:
        self.value = value


class _RecordingLogOutput:
    def __init__(self):
        self.append_thread_ids: list[int] = []
        self.lines: list[str] = []
        self.scroll_bar = _RecordingScrollBar()

    def append(self, text: str) -> None:
        self.append_thread_ids.append(int(QThread.currentThreadId()))
        self.lines.append(text)

    def verticalScrollBar(self) -> _RecordingScrollBar:
        return self.scroll_bar


class _LogHarness(QObject):
    """Exercise MainWindow's log methods without constructing the dashboard."""

    log_message_requested = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.log_message_requested.connect(self._append_log_on_ui_thread)
        self.log_output = _RecordingLogOutput()

    def append_log(self, message: str) -> None:
        MainWindow.append_log(self, message)

    @pyqtSlot(str)
    def _append_log_on_ui_thread(self, message: str) -> None:
        MainWindow._append_log_on_ui_thread(self, message)


class _WorkerHarness(QObject):
    worker_cleanup_requested = pyqtSignal(object)

    def __init__(self):
        super().__init__()
        self._tracked_workers = {}
        self.worker = None
        self.worker_cleanup_requested.connect(self._on_tracked_worker_finished)

    def _track_worker(self, attribute_name, worker, *, collection_name=None):
        MainWindow._track_worker(
            self,
            attribute_name,
            worker,
            collection_name=collection_name,
        )

    @pyqtSlot(object)
    def _on_tracked_worker_finished(self, worker):
        MainWindow._on_tracked_worker_finished(self, worker)

    def _clear_worker_reference(self, attribute_name, worker):
        MainWindow._clear_worker_reference(self, attribute_name, worker)


def test_append_log_from_python_thread_is_dispatched_to_gui_thread():
    app = QCoreApplication.instance() or QCoreApplication([])
    window = _LogHarness()
    gui_thread_id = int(QThread.currentThreadId())
    caller_thread_ids: list[int] = []
    caller_errors: list[BaseException] = []

    def append_from_background_thread() -> None:
        caller_thread_ids.append(int(QThread.currentThreadId()))
        try:
            window.append_log("background save completed")
        except BaseException as exc:  # pragma: no cover - asserted below
            caller_errors.append(exc)

    thread = threading.Thread(target=append_from_background_thread)
    thread.start()
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert not caller_errors
    assert caller_thread_ids != [gui_thread_id]
    # The GUI event loop has not run yet, proving the background caller did
    # not invoke QTextEdit.append directly.
    assert window.log_output.append_thread_ids == []

    deadline = time.monotonic() + 1
    while not window.log_output.append_thread_ids and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.001)

    assert window.log_output.append_thread_ids == [gui_thread_id]
    assert "background save completed" in window.log_output.lines[0]


def test_tracked_qthread_is_parented_and_deleted_after_finished():
    app = QCoreApplication.instance() or QCoreApplication([])
    owner = _WorkerHarness()
    worker = QThread()
    owner.worker = worker

    owner._track_worker("worker", worker)

    assert worker.parent() is owner
    assert worker in owner._tracked_workers

    worker.start()
    worker.quit()
    assert worker.wait(1000)

    deadline = time.monotonic() + 1
    while owner.worker is not None and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.001)

    assert owner.worker is None
    assert worker not in owner._tracked_workers

    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    app.processEvents()
    assert sip.isdeleted(worker)
