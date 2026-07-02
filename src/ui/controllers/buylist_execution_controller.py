from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from PyQt5.QtWidgets import QMessageBox

from src.core.order_state import OrderIntent, OrderSide
from src.core.watchlist import BuylistItem
from src.ui.controllers.base import WindowController


class BuylistExecutionController(WindowController):
    """Own execution-queue refresh and order submission workflows."""

    def execution_queue_target_items(
        self,
        env: str,
        symbols: Optional[List[str]] = None,
        *,
        create_missing: bool = False,
    ) -> Tuple[List[Any], List[str]]:
        watch_items = list(getattr(getattr(self, "watchlist", None), "items", []) or [])
        watch_by_symbol = {
            str(getattr(item, "symbol", "") or "").strip().upper(): item
            for item in watch_items
            if str(getattr(item, "symbol", "") or "").strip()
        }
        queued_symbols = [
            str(getattr(item, "symbol", "") or "").strip().upper()
            for item in list(getattr(getattr(self, "buylist_manager", None), "items", []) or [])
            if str(getattr(item, "environment", "") or "").upper() == env
            and self._is_execution_queue_buylist_item(item)
        ]

        if symbols is None:
            target_symbols = queued_symbols
        else:
            requested = []
            for raw_symbol in symbols:
                symbol = str(raw_symbol or "").strip().upper()
                if symbol and symbol not in requested:
                    requested.append(symbol)
            target_symbols = requested if create_missing else [symbol for symbol in requested if symbol in queued_symbols]

        targets: List[Any] = []
        missing: List[str] = []
        for symbol in target_symbols:
            item = watch_by_symbol.get(symbol)
            if item is None:
                existing = self.buylist_manager.get(symbol, env) if hasattr(self, "buylist_manager") else None
                if existing is not None and self._is_execution_queue_buylist_item(existing):
                    item = existing
            if item is None:
                missing.append(symbol)
                continue
            targets.append(item)
        return targets, missing

    def refresh_execution_queue(
        self,
        env: Optional[str] = None,
        show_log: bool = True,
        symbols: Optional[List[str]] = None,
        *,
        create_missing: bool = False,
    ) -> int:
        """Refresh existing queue rows, or intentionally queue selected symbols."""
        env = (env or (self.watchlist_env_combo.currentText() if hasattr(self, "watchlist_env_combo") else "SIM")).upper()
        target_items, missing_symbols = self.execution_queue_target_items(
            env,
            symbols,
            create_missing=create_missing,
        )
        if not target_items:
            if show_log:
                if symbols is None:
                    self.append_log(f"[Execution Queue/{env}] No queued buylist symbols to refresh.")
                else:
                    self.append_log(f"[Execution Queue/{env}] No selected watchlist symbols could be queued.")
                if missing_symbols:
                    self.append_log(f"[Execution Queue/{env}] Missing symbols: " + ", ".join(missing_symbols[:10]))
            return 0

        manager = self._ensure_execution_queue_manager()
        account_size = self._get_account_balance_for_env(env) if hasattr(self, "_get_account_balance_for_env") else 100000.0
        risk_percent = (
            self._parse_float(self.risk_percent_input, 1.0) / 100.0
            if hasattr(self, "risk_percent_input") else 0.01
        )
        if risk_percent <= 0:
            risk_percent = 0.01
        buffer_pct = self._watchlist_orb_buffer_pct() if hasattr(self, "_watchlist_orb_buffer_pct") else 0.001
        account_no = self._first_account_no_for_environment(env) or ""

        status_counts: Dict[str, int] = {}
        failed: List[str] = []
        refreshed = 0
        for watch_item in target_items:
            symbol = str(getattr(watch_item, "symbol", "") or "").strip().upper()
            if not symbol:
                continue
            try:
                one_minute = self._latest_intraday_session(
                    self._load_cached_intraday_interval(symbol, "1m", window_days=7)
                )
                five_minute = self._latest_intraday_session(
                    self._load_cached_intraday_interval(symbol, "5m", window_days=7)
                )
                current_price = self._watchlist_orb_signal_price(symbol) if hasattr(self, "_watchlist_orb_signal_price") else 0.0
                if current_price > 0:
                    self.latest_intraday_prices[symbol] = current_price
                duplicate_order = manager.has_pending_or_submitted_order(symbol) or self._has_duplicate_open_order(
                    env,
                    account_no,
                    symbol,
                    OrderSide.BUY,
                    OrderIntent.ENTRY,
                )
                queue_item = manager.build_or_update_from_watchlist_item(
                    watch_item,
                    {"1m": one_minute, "5m": five_minute, "30m": five_minute},
                    current_price=current_price,
                    account_size=account_size,
                    risk_percent=risk_percent,
                    adr_percent=self._calculate_adr_percent_for_symbol(symbol),
                    buffer_pct=buffer_pct,
                    duplicate_pending_order=duplicate_order,
                )
                self.apply_execution_queue_item_to_buylist(queue_item, watch_item, env, buffer_pct)
                status_text = self._execution_queue_value(queue_item.status)
                status_counts[status_text] = status_counts.get(status_text, 0) + 1
                refreshed += 1
            except Exception as exc:
                failed.append(f"{symbol}: {exc}")

        self.populate_buylist_dashboard()
        if hasattr(self, "update_dashboard_summary"):
            self.update_dashboard_summary()
        self._save_buylist_state()
        self._save_execution_queue_state()

        if show_log:
            counts_text = ", ".join(f"{key}={value}" for key, value in sorted(status_counts.items())) or "none"
            scope = "queued" if symbols is None else "selected"
            self.append_log(
                f"[Execution Queue/{env}] Refreshed {refreshed} {scope} symbol(s): {counts_text}."
            )
            if missing_symbols:
                self.append_log(f"[Execution Queue/{env}] Missing symbols: " + ", ".join(missing_symbols[:10]))
            if failed:
                self.append_log(f"[Execution Queue/{env}] Refresh failures: " + "; ".join(failed[:10]))
        return refreshed

    def apply_execution_queue_item_to_buylist(self, queue_item, watch_item, env: str, buffer_pct: float) -> None:
        symbol = str(queue_item.symbol or "").upper()
        if not symbol:
            return

        protected_statuses = {
            "BOUGHT", "BUY_SUBMITTED", "BUY_PARTIAL", "SELL_SUBMITTED",
            "PARTIAL_EXIT_SUBMITTED", "SOLD",
        }
        existing = self.buylist_manager.get(symbol, env)
        if existing is not None and str(getattr(existing, "monitoring_status", "")).upper() in protected_statuses:
            return

        candidate = queue_item.selected_candidate
        status_text = self._execution_queue_value(queue_item.status)
        entry_trigger = float(getattr(candidate, "entry_trigger", 0.0) or 0.0) if candidate else 0.0
        orb_high = float(getattr(candidate, "orb_high", 0.0) or 0.0) if candidate else 0.0
        stop_loss = float(getattr(candidate, "stop_loss", 0.0) or 0.0) if candidate else float(getattr(watch_item, "stop_loss", 0.0) or 0.0)
        planned_shares = int(getattr(candidate, "shares", 0) or 0) if candidate else 0
        capital_percent = float(getattr(candidate, "capital_percent", 0.0) or 0.0) if candidate else 0.0
        stop_adr = float(getattr(candidate, "stop_adr", 0.0) or 0.0) if candidate and getattr(candidate, "stop_adr", None) is not None else 0.0
        risk_percent = float(getattr(candidate, "risk_percent", 0.0) or 0.0) * 100.0 if candidate else 0.0
        selected_window = str(getattr(candidate, "window", "") or queue_item.selected_window or "")

        warnings = list(getattr(queue_item, "warnings", []) or [])
        if candidate:
            warnings.extend(list(getattr(candidate, "warnings", []) or []))
            if getattr(candidate, "reason", "") and not getattr(candidate, "valid", False):
                warnings.append(str(candidate.reason))
        elif queue_item.candidates:
            for window, cand in queue_item.candidates.items():
                reason = str(getattr(cand, "reason", "") or "")
                if reason:
                    warnings.append(f"{window}: {reason}")
        warnings = list(dict.fromkeys(warnings))

        entry_price = entry_trigger or orb_high or float(getattr(watch_item, "entry_price", 0.0) or 0.0)
        summary = (
            f"Execution queue {status_text}"
            + (f"; selected ORB {selected_window}" if selected_window else "")
            + (f"; entry {entry_trigger:.2f}" if entry_trigger > 0 else "")
        )
        trade_plan = (
            f"ORB {selected_window}: buy {planned_shares} @ {entry_trigger:.2f}"
            if selected_window and entry_trigger > 0 and planned_shares > 0
            else status_text
        )

        if existing is None:
            existing = BuylistItem(
                symbol=symbol,
                name=str(getattr(watch_item, "name", "") or symbol),
                entry_price=entry_price,
                target_price=0.0,
                stop_loss=stop_loss,
                total_score=float(getattr(candidate, "score", 0.0) or 0.0) if candidate else 0.0,
                status=status_text,
                technical_score=0.0,
                setup_score=0.0,
                risk_score=0.0,
                news_score=0.0,
                timing_score=0.0,
                rr=0.0,
                stop_adr=stop_adr,
                position_percent=capital_percent,
                ai_summary=summary,
                warnings=warnings,
                notes=str(getattr(watch_item, "notes", "") or ""),
                risk_percent=risk_percent,
                trade_plan=trade_plan,
                monitoring_status=status_text,
                environment=env,
                breakout_price=getattr(watch_item, "breakout_price", None),
                breakout_method=f"execution_queue:{selected_window}" if selected_window else "execution_queue",
                buffer_pct=buffer_pct,
            )
            self.buylist_manager.add(existing)
        else:
            existing.name = str(getattr(watch_item, "name", "") or existing.name or symbol)
            existing.entry_price = entry_price
            existing.stop_loss = stop_loss
            existing.total_score = float(getattr(candidate, "score", 0.0) or 0.0) if candidate else 0.0
            existing.status = status_text
            existing.stop_adr = stop_adr
            existing.position_percent = capital_percent
            existing.ai_summary = summary
            existing.warnings = warnings
            existing.notes = str(getattr(watch_item, "notes", "") or existing.notes or "")
            existing.risk_percent = risk_percent
            existing.trade_plan = trade_plan
            existing.monitoring_status = status_text
            existing.environment = env
            existing.breakout_price = getattr(watch_item, "breakout_price", None)
            existing.breakout_method = f"execution_queue:{selected_window}" if selected_window else "execution_queue"
            existing.buffer_pct = buffer_pct

        existing._planned_shares = planned_shares
        existing._selected_orb_window = selected_window
        existing._execution_queue_symbol = symbol
        existing._execution_entry_trigger = entry_trigger

    def submit_selected_queue_order(self, env: str) -> None:
        item = self._buylist_selected_item(env)
        if not item:
            QMessageBox.warning(self.window, "No selection", "Select an execution queue row first.")
            return
        queue_item = self._queue_item_for_buylist_item(item)
        if queue_item is None:
            QMessageBox.warning(self.window, "No queue item", f"{item.symbol} is not in the execution queue. Click Refresh Queue first.")
            return
        candidate = getattr(queue_item, "selected_candidate", None)
        status_text = self._execution_queue_value(getattr(queue_item, "status", ""))
        if candidate is None or status_text != "EXECUTE_READY":
            QMessageBox.warning(
                self.window,
                "Not ready",
                f"{item.symbol} is {status_text or 'not ready'}; submit is allowed only when status is EXECUTE_READY.",
            )
            return
        if int(candidate.shares or 0) < 1 or float(candidate.entry_trigger or 0.0) <= 0:
            QMessageBox.warning(self.window, "Invalid order", f"{item.symbol} has invalid quantity or entry trigger.")
            return
        if self._buylist_auto_order_blocked(item):
            QMessageBox.warning(
                self.window,
                "KIS order blocked",
                f"{item.symbol} cannot be submitted through the selected KIS account/API:\n"
                f"{getattr(item, 'auto_order_block_reason', '')}",
            )
            return

        account_no = self._first_account_no_for_environment(env) or ""
        if self._has_duplicate_open_order(env, account_no, item.symbol, OrderSide.BUY, OrderIntent.ENTRY):
            QMessageBox.warning(self.window, "Duplicate order", f"An open BUY ENTRY order already exists for {item.symbol}.")
            return

        review = self._format_execution_queue_order_review(env, item, queue_item)
        title = f"Submit {env} BUY Order"
        body = review
        if env == "PROD":
            body = "This will submit a live PROD BUY order.\n\n" + review
        reply = QMessageBox.question(
            self.window,
            title,
            body + "\n\nSubmit this order?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        manager = self._ensure_execution_queue_manager()
        manager.mark_order_submitted(item.symbol, order_status="PENDING")
        queue_status = self._execution_queue_status_for_buylist_item(item) or "ORDER_PENDING"
        item.monitoring_status = queue_status
        item.status = queue_status
        item.entry_price = float(candidate.entry_trigger or 0.0)
        item.stop_loss = float(candidate.stop_loss or 0.0)
        item._planned_shares = int(candidate.shares or 0)
        item._selected_orb_window = str(candidate.window or "")
        item._buy_order_pending = True
        self._save_buylist_state()
        self._save_execution_queue_state()
        self.populate_buylist_dashboard()
        self._submit_kis_buy_order(
            item,
            quantity=int(candidate.shares or 0),
            order_price=float(candidate.entry_trigger or 0.0),
        )
