from __future__ import annotations

from PyQt5.QtWidgets import QMessageBox

from src.ui.controllers.base import WindowController


class WatchlistController(WindowController):
    """Own watchlist workflows that compute and refresh derived state."""

    def refresh_orb_statuses_with_data(self) -> None:
        """Refresh intraday data, then evaluate ORB entry status for all watchlist rows."""
        symbols = [item.symbol for item in getattr(self.watchlist, "items", [])]
        if not symbols:
            QMessageBox.information(self.window, "No watchlist", "Add symbols to the watchlist first.")
            return

        self._refresh_orb_after_intraday_bulk = True
        worker = getattr(self, "intraday_bulk_worker", None)
        if worker is not None and worker.isRunning():
            self.append_log("Intraday refresh already running; ORB status will refresh from current cache now.")
            self.refresh_all_orb_statuses()
            return

        self.refresh_all_orb_statuses()
        self.append_log(f"Refreshing intraday data before ORB status check for {len(symbols)} watchlist symbols.")
        self.refresh_watchlist_intraday_cache(show_messages=False)

    def refresh_all_orb_statuses(self) -> None:
        """Evaluate aggregate ORB status for every watchlist symbol without changing selection."""
        symbols = [item.symbol.strip().upper() for item in getattr(self.watchlist, "items", []) if item.symbol]
        if not symbols:
            return

        if not hasattr(self, "watchlist_scores"):
            self.watchlist_scores = {}

        counts = {
            "BUY_READY": 0,
            "WATCHING": 0,
            "WAITING_ENTRY": 0,
            "NO_ENTRY": 0,
            "NO_INTRADAY": 0,
            "NO_VALID_ORB": 0,
            "BELOW_BREAKOUT": 0,
        }
        for symbol in symbols:
            records = self._calculate_watchlist_orb_records_for_symbol(symbol)
            orb_status = self._derive_watchlist_orb_status(records)
            self.watchlist_scores.setdefault(symbol, {})["orb_status"] = orb_status
            counts[orb_status] = counts.get(orb_status, 0) + 1

        self._force_watchlist_orb_status_eval = True
        try:
            self.populate_watchlist_table()
        finally:
            self._force_watchlist_orb_status_eval = False
        self._apply_cached_orb_statuses_to_watchlist_table()
        self._refresh_selected_watchlist_orb_panel()
        self.append_log(
            "ORB status refreshed for "
            f"{len(symbols)} watchlist symbols: "
            f"{counts.get('BUY_READY', 0)} ready, "
            f"{counts.get('WATCHING', 0)} watching, "
            f"{counts.get('WAITING_ENTRY', 0)} waiting entry, "
            f"{counts.get('NO_ENTRY', 0)} no entry, "
            f"{counts.get('NO_INTRADAY', 0)} no intraday, "
            f"{counts.get('NO_VALID_ORB', 0)} no valid ORB, "
            f"{counts.get('BELOW_BREAKOUT', 0)} below breakout."
        )
