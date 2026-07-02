from __future__ import annotations

import datetime as dt
from typing import Any, Dict, Optional, Tuple

from PyQt5.QtWidgets import QMessageBox

from src.ui.controllers.base import WindowController
from src.ui.workers import KisAccountWorker


class AccountController(WindowController):
    """Own KIS account refresh and position sync workflows."""

    def refresh_trade_account_size(self) -> None:
        profile = self.trade_kis_account_combo.currentData() if hasattr(self, "trade_kis_account_combo") else None
        if not profile:
            QMessageBox.warning(self.window, "No KIS account", "Select a configured KIS account first.")
            return
        if self.kis_startup_worker is not None and self.kis_startup_worker.isRunning():
            QMessageBox.information(self.window, "KIS preload running", "Startup KIS account preload is still running.")
            return
        if self.kis_account_worker is not None and self.kis_account_worker.isRunning():
            QMessageBox.information(self.window, "KIS refresh running", "A KIS refresh is already running.")
            return

        environment = self.trade_kis_environment_combo.currentText()
        self.append_log(f"Fetching {profile.get('label', environment)} account value...")
        self.kis_account_worker = KisAccountWorker(
            environment=environment,
            include_domestic=True,
            include_overseas=True,
            account_no=profile.get("account_no"),
        )
        self.kis_account_worker.finished_snapshot.connect(self._on_trade_account_snapshot_finished)
        self.kis_account_worker.error_occurred.connect(self._on_trade_account_snapshot_error)
        self.kis_account_worker.finished.connect(
            lambda worker=self.kis_account_worker: self._clear_worker_reference("kis_account_worker", worker)
        )
        self.kis_account_worker.start()

    def sync_positions_from_kis(self, snapshots: Optional[Dict[Any, dict]] = None) -> int:
        """Sync held buylist positions to real KIS account holdings when snapshots are available."""
        if not hasattr(self, "buylist_manager"):
            return 0

        snapshot_map = snapshots if snapshots is not None else getattr(self, "kis_account_snapshots", {})
        if not isinstance(snapshot_map, dict):
            return 0

        holdings_by_key: Dict[Tuple[str, str], Tuple[float, float, str]] = {}
        for key, snapshot in snapshot_map.items():
            if isinstance(key, tuple) and len(key) >= 2:
                environment = str(key[0] or "").upper()
                account_no = str(key[1] or "")
            else:
                environment = str((snapshot or {}).get("environment", "")).upper()
                account_no = ""
            if not environment:
                continue

            for holding in self._buylist_snapshot_holdings(snapshot):
                symbol = str(holding.get("symbol", "")).strip().upper()
                quantity = self._buylist_to_float(holding.get("quantity"))
                if not symbol or quantity <= 0:
                    continue
                average_price = self._buylist_to_float(holding.get("average_price"))
                holdings_key = (environment, symbol)
                if quantity > holdings_by_key.get(holdings_key, (0.0, 0.0, ""))[0]:
                    holdings_by_key[holdings_key] = (quantity, average_price, account_no)

        changed = 0
        for item in self.buylist_manager.items:
            symbol = str(getattr(item, "symbol", "")).strip().upper()
            environment = str(getattr(item, "environment", "") or "SIM").upper()
            holding = holdings_by_key.get((environment, symbol))
            if holding is None:
                continue

            account_quantity, average_price, account_no = holding
            shares_held = max(0, int(round(account_quantity)))
            if shares_held <= 0:
                continue

            old_shares = int(getattr(item, "shares_held", 0) or 0)
            old_avg = float(getattr(item, "avg_cost", 0.0) or 0.0)
            old_status = str(getattr(item, "monitoring_status", ""))

            item.shares_held = shares_held
            if average_price > 0:
                item.avg_cost = float(average_price)
            if not getattr(item, "buy_date", None):
                item.buy_date = dt.datetime.now()
            item._buy_order_pending = False
            if self._is_execution_queue_buylist_item(item):
                manager = self._ensure_execution_queue_manager()
                queue_item = manager.get_item(symbol, environment) if hasattr(manager, "get_item") else None
                if queue_item is not None:
                    manager.mark_order_filled(symbol, order_status="FILLED", environment=environment)
                    item.status = self._execution_queue_status_for_buylist_item(item) or item.status
            if item.monitoring_status in {
                "WATCHING",
                "ACTIVE",
                "BUY_SUBMITTED",
                "BUY_PARTIAL",
                "ERROR",
                "UNKNOWN_SUBMISSION_STATE",
                "BOUGHT",
            }:
                item.monitoring_status = "BOUGHT"

            if old_shares != item.shares_held or old_avg != item.avg_cost or old_status != item.monitoring_status:
                changed += 1
                self.append_log(
                    f"[Buylist/{environment}] Synced {symbol} from KIS account {account_no or '<unknown>'}: "
                    f"shares {old_shares} -> {item.shares_held}, avg ${old_avg:.2f} -> ${item.avg_cost:.2f}."
                )

        if changed:
            self._save_buylist_state()
            self.populate_buylist_dashboard()
        return changed
