from __future__ import annotations

import datetime as dt
from typing import Any, Dict, Optional, Tuple
from zoneinfo import ZoneInfo

from PyQt5.QtWidgets import QMessageBox

from src.ui.controllers.base import WindowController
from src.ui.workers import KisAccountWorker

US_MARKET_ZONE = ZoneInfo("America/New_York")


class AccountController(WindowController):
    """Own KIS account refresh and position sync workflows."""

    @staticmethod
    def _profile_account_no(profile: Any, environment: str) -> str:
        """Return a selected profile's account only when it matches the environment."""
        if not isinstance(profile, dict):
            return ""
        profile_environment = (
            str(profile.get("environment") or environment or "").strip().upper()
        )
        if profile_environment != str(environment or "").strip().upper():
            return ""
        return str(profile.get("account_no") or "").strip()

    def _selected_trade_account_no(self, environment: str) -> str:
        """Read the account the user selected for trade sizing and order entry."""
        combo = getattr(self.window, "__dict__", {}).get("trade_kis_account_combo")
        if combo is None:
            return ""
        try:
            profile = combo.currentData()
        except RuntimeError:
            # A deleted Qt widget can briefly be observed while shutting down.
            return ""
        return self._profile_account_no(profile, environment)

    def _account_no_for_buylist_item(self, item: Any, environment: str) -> str:
        """Resolve durable item ownership, including pre-migration ledger records."""
        account_no = str(getattr(item, "kis_account_no", "") or "").strip()
        if account_no:
            return account_no

        order_id = str(getattr(item, "kis_order_id", "") or "").strip()
        if not order_id:
            return ""
        for order in getattr(self.window, "__dict__", {}).get("order_ledger", []) or []:
            if (
                str(getattr(order, "environment", "") or "").upper()
                != str(environment or "").upper()
                or str(getattr(order, "symbol", "") or "").upper()
                != str(getattr(item, "symbol", "") or "").upper()
            ):
                continue
            if order_id not in {
                str(getattr(order, "client_order_id", "") or ""),
                str(getattr(order, "broker_order_id", "") or ""),
            }:
                continue
            account_no = str(getattr(order, "account_no", "") or "").strip()
            if account_no:
                item.kis_account_no = account_no
                return account_no
        return ""

    def refresh_trade_account_size(self) -> None:
        window = self.window
        profile = (
            window.trade_kis_account_combo.currentData()
            if hasattr(window, "trade_kis_account_combo")
            else None
        )
        if not profile:
            QMessageBox.warning(
                self.window, "No KIS account", "Select a configured KIS account first."
            )
            return
        if (
            window.kis_startup_worker is not None
            and window.kis_startup_worker.isRunning()
        ):
            QMessageBox.information(
                self.window,
                "KIS preload running",
                "Startup KIS account preload is still running.",
            )
            return
        if (
            window.kis_account_worker is not None
            and window.kis_account_worker.isRunning()
        ):
            QMessageBox.information(
                self.window, "KIS refresh running", "A KIS refresh is already running."
            )
            return

        environment = window.trade_kis_environment_combo.currentText()
        window.append_log(
            f"Fetching {profile.get('label', environment)} account value..."
        )
        requested_profile = dict(profile)
        window.kis_account_worker = KisAccountWorker(
            environment=environment,
            include_domestic=True,
            include_overseas=True,
            account_no=requested_profile.get("account_no"),
        )
        window.kis_account_worker.finished_snapshot.connect(
            lambda snapshot,
            requested=requested_profile: window._on_trade_account_snapshot_finished(
                snapshot, requested
            )
        )
        window.kis_account_worker.error_occurred.connect(
            lambda error,
            requested=requested_profile: window._on_trade_account_snapshot_error(
                error, requested
            )
        )
        window._track_worker("kis_account_worker", window.kis_account_worker)
        window.kis_account_worker.start()

    def sync_positions_from_kis(
        self, snapshots: Optional[Dict[Any, dict]] = None
    ) -> int:
        """Sync held buylist positions to real KIS account holdings when snapshots are available."""
        window = self.window
        if not hasattr(window, "buylist_manager"):
            return 0

        snapshot_map = (
            snapshots
            if snapshots is not None
            else getattr(window, "kis_account_snapshots", {})
        )
        if not isinstance(snapshot_map, dict):
            return 0

        holdings_by_key: Dict[Tuple[str, str, str], Tuple[float, float]] = {}
        snapshot_accounts_by_environment: Dict[str, set[str]] = {}
        for key, snapshot in snapshot_map.items():
            if isinstance(key, tuple) and len(key) >= 2:
                environment = str(key[0] or "").upper()
                account_no = str(key[1] or "")
            else:
                environment = str((snapshot or {}).get("environment", "")).upper()
                account_no = ""
            if environment != "PROD":
                continue

            if account_no:
                snapshot_accounts_by_environment.setdefault(environment, set()).add(
                    account_no
                )

            for holding in window._buylist_snapshot_holdings(snapshot):
                symbol = str(holding.get("symbol", "")).strip().upper()
                quantity = window._buylist_to_float(holding.get("quantity"))
                if not symbol or quantity <= 0:
                    continue
                average_price = window._buylist_to_float(holding.get("average_price"))
                holdings_key = (environment, account_no, symbol)
                # KIS can repeat a symbol across exchange sections.  Keep the
                # largest quantity for this one account, never a value selected
                # from a different configured account.
                if quantity > holdings_by_key.get(holdings_key, (0.0, 0.0))[0]:
                    holdings_by_key[holdings_key] = (quantity, average_price)

        changed = 0
        for item in window.buylist_manager.items:
            symbol = str(getattr(item, "symbol", "")).strip().upper()
            environment = str(getattr(item, "environment", "") or "PROD").upper()
            account_no = self._account_no_for_buylist_item(item, environment)
            if not account_no:
                account_no = self._selected_trade_account_no(environment)
            if not account_no:
                # Old state without account attribution remains compatible when
                # exactly one account snapshot is present.  With several
                # accounts, ticker-only matching is unsafe, so leave the item
                # unchanged until the user selects/assigns an account.
                accounts = snapshot_accounts_by_environment.get(environment, set())
                if len(accounts) == 1:
                    account_no = next(iter(accounts))
                elif len(accounts) > 1:
                    window.append_log(
                        f"[Buylist/{environment}] Skipped KIS sync for {symbol}: "
                        "no account is assigned and multiple account snapshots are loaded."
                    )
                    continue

            holding = holdings_by_key.get((environment, account_no, symbol))
            if holding is None:
                account_was_fetched = (
                    account_no
                    in snapshot_accounts_by_environment.get(environment, set())
                )
                old_shares = max(0, int(getattr(item, "shares_held", 0) or 0))
                old_status = str(getattr(item, "monitoring_status", "") or "")
                position_statuses = {
                    "BOUGHT",
                    "BUY_PARTIAL",
                    "FILLED",
                    "SELL_SUBMITTED",
                    "PARTIAL_EXIT_SUBMITTED",
                    "PARTIAL_EXIT_RESERVED",
                    "SELL_RESERVED",
                }
                if account_was_fetched and (
                    old_shares > 0 or old_status.upper() in position_statuses
                ):
                    item.shares_held = 0
                    item.position_percent = 0.0
                    item.monitoring_status = "SOLD"
                    item._buy_order_pending = False
                    changed += 1
                    window.append_log(
                        f"[Buylist/{environment}] KIS confirms {symbol} is flat in "
                        f"account {account_no}; cleared stale {old_shares}-share position."
                    )
                continue

            account_quantity, average_price = holding
            shares_held = max(0, int(round(account_quantity)))
            if shares_held <= 0:
                continue

            old_shares = int(getattr(item, "shares_held", 0) or 0)
            old_avg = window._buylist_to_float(getattr(item, "avg_cost", 0.0))
            old_status = str(getattr(item, "monitoring_status", ""))
            old_account_no = str(getattr(item, "kis_account_no", "") or "").strip()

            item.shares_held = shares_held
            if average_price > 0:
                item.avg_cost = float(average_price)
            item.kis_account_no = account_no
            if not getattr(item, "buy_date", None):
                item.buy_date = dt.datetime.now(US_MARKET_ZONE)
            item._buy_order_pending = False
            if window._is_execution_queue_buylist_item(item):
                manager = window._ensure_execution_queue_manager()
                queue_item = (
                    manager.get_item(symbol, environment)
                    if hasattr(manager, "get_item")
                    else None
                )
                if queue_item is not None:
                    manager.mark_order_filled(
                        symbol, order_status="FILLED", environment=environment
                    )
                    item.status = (
                        window._execution_queue_status_for_buylist_item(item)
                        or item.status
                    )
            if item.monitoring_status in {
                "WATCHING",
                "ACTIVE",
                "ORDER_PENDING",
                "ORDER_SUBMITTED",
                "BUY_SUBMITTED",
                "BUY_PARTIAL",
                "FILLED",
                "ERROR",
                "UNKNOWN_SUBMISSION_STATE",
                "BOUGHT",
            }:
                item.monitoring_status = "BOUGHT"

            if (
                old_shares != item.shares_held
                or old_avg != item.avg_cost
                or old_status != item.monitoring_status
                or old_account_no != item.kis_account_no
            ):
                changed += 1
                window.append_log(
                    f"[Buylist/{environment}] Synced {symbol} from KIS account {account_no or '<unknown>'}: "
                    f"shares {old_shares} -> {item.shares_held}, avg ${old_avg:.2f} -> ${item.avg_cost:.2f}."
                )

        if changed:
            window._save_buylist_state()
            window.populate_buylist_dashboard()
        return changed
