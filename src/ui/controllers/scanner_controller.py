from __future__ import annotations

from src.ui.controllers.base import WindowController


class ScannerController(WindowController):
    """Own high-level scanner run workflows."""

    def run_all_scanners(self, checked: bool = False, show_warnings: bool = True) -> None:
        """Run all configured scanner setups against the MySQL cache."""
        if not self._prepare_scanner_run(show_warnings=show_warnings):
            return

        self.running_scanner_setup_name = "__ALL__"
        self.running_scanner_show_warnings = show_warnings
        self.append_log(f"Starting database scanner run for {len(self.scanner_setups)} setups.")
        self._start_scanner_worker()

    def run_scanner(self, checked: bool = False, show_warnings: bool = True) -> None:
        """Start the selected database-backed scanner asynchronously."""
        if not self._prepare_scanner_run(show_warnings=show_warnings):
            return

        setup_name = self.scanner_setup_combo.currentText() if hasattr(self, "scanner_setup_combo") else "current filters"
        self.running_scanner_setup_name = setup_name
        self.running_scanner_show_warnings = show_warnings
        self.append_log(f"Starting database scanner run with setup: {setup_name}.")
        self._start_scanner_worker()
