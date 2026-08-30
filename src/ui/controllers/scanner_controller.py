from __future__ import annotations

from src.ui.controllers.base import WindowController


class ScannerController(WindowController):
    """Own high-level scanner run workflows."""

    def run_all_scanners(
        self, checked: bool = False, show_warnings: bool = True
    ) -> None:
        """Run all configured scanner setups against the MySQL cache."""
        window = self.window
        if not window._prepare_scanner_run(show_warnings=show_warnings):
            return

        window.running_scanner_setup_name = "__ALL__"
        window.running_scanner_show_warnings = show_warnings
        window.append_log(
            f"Starting database scanner run for {len(window.scanner_setups)} setups."
        )
        window._start_scanner_worker()

    def run_scanner(self, checked: bool = False, show_warnings: bool = True) -> None:
        """Start the selected database-backed scanner asynchronously."""
        window = self.window
        if not window._prepare_scanner_run(show_warnings=show_warnings):
            return

        setup_name = (
            window.scanner_setup_combo.currentText()
            if hasattr(window, "scanner_setup_combo")
            else "current filters"
        )
        window.running_scanner_setup_name = setup_name
        window.running_scanner_show_warnings = show_warnings
        window.append_log(f"Starting database scanner run with setup: {setup_name}.")
        window._start_scanner_worker()
