"""
PyQt5 Stock Dashboard - Main Application Entry Point
"""
import sys
from PyQt5.QtCore import qInstallMessageHandler
from PyQt5.QtWidgets import QApplication


def _qt_message_handler(mode, context, message):
    """Suppress one known Qt MIME warning while preserving other Qt output."""
    if (
        "QMimeDatabase: Error loading internal MIME data" in message
        or "Premature end of document" in message
    ):
        return
    sys.stderr.write(f"{message}\n")


def main():
    """Initialize and run the application."""
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
