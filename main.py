"""
PyQt5 Stock Dashboard - Main Application Entry Point
"""
import faulthandler
import logging
import os
import sys
import traceback


def _load_repository_environment() -> None:
    """Load gitignored ``.env`` values before application modules import.

    ``src.core.execution_config`` intentionally resolves fail-closed feature
    flags at import time.  The legacy KIS compatibility loader used to load
    ``.env`` later, which meant a normal ``python main.py`` launch could import
    the Kanban/WebSocket configuration first and permanently freeze every new
    live flag at its default value even though the operator had configured the
    repository-level ``.env`` correctly.  Environment variables supplied by
    the OS still win; the file only fills values that are not already present.
    """

    from src.utils.config import load_env_file

    for key, value in load_env_file().items():
        os.environ.setdefault(key, value)


def _configure_qt_rendering_environment(platform: str | None = None) -> None:
    """Use Qt's software renderer by default on Windows driver stacks."""
    if (platform or sys.platform) != "win32":
        return
    # These must be present before importing PyQt/QtWebEngine.  Explicit user
    # values still win, which keeps hardware acceleration opt-in for machines
    # with a current, stable graphics driver.
    os.environ.setdefault("QT_OPENGL", "software")
    os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu")


# Load operational configuration before importing any application module that
# snapshots environment-backed settings at module import time.
_load_repository_environment()
_configure_qt_rendering_environment()

from src.utils.logging_config import configure_logging

logger = logging.getLogger(__name__)


def _qt_message_handler(mode, context, message):
    """Suppress one known Qt MIME warning while preserving other Qt output."""
    if (
        "QMimeDatabase: Error loading internal MIME data" in message
        or "Premature end of document" in message
    ):
        return
    sys.stderr.write(f"{message}\n")


def _install_global_excepthook():
    """Log uncaught exceptions instead of letting PyQt5 abort the process silently.

    An unhandled Python exception raised inside a Qt slot -- especially one
    invoked across a QThread signal boundary, as several startup/background
    workers here are -- normally kills the whole app with no dialog and no
    traceback anywhere. This at least guarantees a record in quant_app.log so
    a "the app just closed" report is diagnosable afterward.
    """
    default_hook = sys.excepthook

    def _log_and_delegate(exc_type, exc_value, exc_tb):
        logger.critical(
            "Unhandled exception; the application may now terminate:\n%s",
            "".join(traceback.format_exception(exc_type, exc_value, exc_tb)),
        )
        default_hook(exc_type, exc_value, exc_tb)

    sys.excepthook = _log_and_delegate


def main():
    """Initialize and run the application."""
    from PyQt5.QtCore import qInstallMessageHandler
    from PyQt5.QtWidgets import QApplication

    configure_logging()
    try:
        faulthandler.enable(all_threads=True)
    except (OSError, RuntimeError):
        logger.warning("Native Python fault tracing could not be enabled.")
    _install_global_excepthook()
    qInstallMessageHandler(_qt_message_handler)
    from src.ui.main_window import MainWindow

    app = QApplication(sys.argv)

    # Set application metadata
    app.setApplicationName("Stock Dashboard")
    app.setApplicationVersion("0.1.0")

    # Create and show main window
    window = MainWindow()
    window.show()

    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
