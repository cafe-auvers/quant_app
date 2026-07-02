from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

from PyQt5.QtWidgets import QMessageBox

from src.core.order_state import OrderIntent, OrderSide
from src.core.watchlist import BuylistItem
from src.ui.controllers.base import WindowController


@dataclass
class ExecutionQueueRefreshRequest:
    env: str
    manager: Any
    buylist_manager: Any
    target_items: Sequence[Any]
    missing_symbols: List[str] = field(default_factory=list)
    requested_symbols: Optional[List[str]] = None
    account_size: float = 100000.0
    risk_percent: float = 0.01
    buffer_pct: float = 0.001
    account_no: str = ""
    window_days: int = 7
    latest_intraday_session: Callable[[Any], Any] = lambda frame: frame
    load_intraday_interval: Callable[[str, str, int], Any] = lambda _symbol, _interval, _window_days: None
    signal_price_for_symbol: Callable[[str], float] = lambda _symbol: 0.0
    set_latest_intraday_price: Callable[[str, float], None] = lambda _symbol, _price: None
    has_duplicate_open_order: Callable[[str, str, str, OrderSide, OrderIntent], bool] = lambda *_args: False
    adr_percent_for_symbol: Callable[[str], Optional[float]] = lambda _symbol: None

    @property
    def scope(self) -> str:
        return "queued" if self.requested_symbols is None else "selected"


@dataclass
class ExecutionQueueRefreshResult:
    env: str
    requested_symbols: Optional[List[str]] = None
    missing_symbols: List[str] = field(default_factory=list)
    status_counts: Dict[str, int] = field(default_factory=dict)
    failures: List[str] = field(default_factory=list)
    refreshed: int = 0
    target_count: int = 0

    @property
    def scope(self) -> str:
        return "queued" if self.requested_symbols is None else "selected"


class BuylistExecutionController(WindowController):
    """Own execution-queue refresh and order submission workflows."""

    @staticmethod
    def _status_text(value: Any) -> str:
        return str(getattr(value, "value", value) or "")

    def refresh_execution_queue(self, request: ExecutionQueueRefreshRequest) -> ExecutionQueueRefreshResult:
        """Refresh existing queue rows, or intentionally queue selected symbols."""
        result = ExecutionQueueRefreshResult(
            env=request.env,
            requested_symbols=request.requested_symbols,
            missing_symbols=list(request.missing_symbols),
            target_count=len(request.target_items),
        )
        if not request.target_items:
            return result

        for watch_item in request.target_items:
            symbol = str(getattr(watch_item, "symbol", "") or "").strip().upper()
            if not symbol:
                continue
            try:
                one_minute = request.latest_intraday_session(
                    request.load_intraday_interval(symbol, "1m", request.window_days)
                )
                five_minute = request.latest_intraday_session(
                    request.load_intraday_interval(symbol, "5m", request.window_days)
                )
                current_price = request.signal_price_for_symbol(symbol)
                if current_price > 0:
                    request.set_latest_intraday_price(symbol, current_price)
                duplicate_order = request.manager.has_pending_or_submitted_order(symbol) or request.has_duplicate_open_order(
                    request.env,
                    request.account_no,
                    symbol,
                    OrderSide.BUY,
                    OrderIntent.ENTRY,
                )
                queue_item = request.manager.build_or_update_from_watchlist_item(
                    watch_item,
                    {"1m": one_minute, "5m": five_minute, "30m": five_minute},
                    current_price=current_price,
                    account_size=request.account_size,
                    risk_percent=request.risk_percent,
                    adr_percent=request.adr_percent_for_symbol(symbol),
                    buffer_pct=request.buffer_pct,
                    duplicate_pending_order=duplicate_order,
                )
                self.apply_execution_queue_item_to_buylist(
                    queue_item,
                    watch_item,
                    request.env,
                    request.buffer_pct,
                    buylist_manager=request.buylist_manager,
                )
                status_text = self._status_text(queue_item.status)
                result.status_counts[status_text] = result.status_counts.get(status_text, 0) + 1
                result.refreshed += 1
            except Exception as exc:
                result.failures.append(f"{symbol}: {exc}")
        return result

    def apply_execution_queue_item_to_buylist(
        self,
        queue_item,
        watch_item,
        env: str,
        buffer_pct: float,
        buylist_manager: Optional[Any] = None,
    ) -> None:
        symbol = str(queue_item.symbol or "").upper()
        if not symbol:
            return

        protected_statuses = {
            "BOUGHT", "BUY_SUBMITTED", "BUY_PARTIAL", "SELL_SUBMITTED",
            "PARTIAL_EXIT_SUBMITTED", "SOLD",
        }
        manager = buylist_manager if buylist_manager is not None else self.buylist_manager
        existing = manager.get(symbol, env)
        if existing is not None and str(getattr(existing, "monitoring_status", "")).upper() in protected_statuses:
            return

        candidate = queue_item.selected_candidate
        status_text = self._status_text(queue_item.status)
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
            manager.add(existing)
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
        status_text = self._status_text(getattr(queue_item, "status", ""))
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
