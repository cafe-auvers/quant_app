"""
PyQt5 Stock Dashboard - Main Application Entry Point
"""
import logging
import sys
import traceback
from PyQt5.QtCore import qInstallMessageHandler
from PyQt5.QtWidgets import QApplication

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
    configure_logging()
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
