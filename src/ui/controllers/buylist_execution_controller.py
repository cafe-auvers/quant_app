from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

from PyQt5.QtWidgets import QMessageBox

from src.core.order_state import OrderIntent, OrderSide
from src.core.execution_queue import build_queue_display_state
from src.core.watchlist import BuylistItem
from src.ui.controllers.base import WindowController


@dataclass
class ExecutionQueueRefreshRequest:
    env: str
    manager: Optional[Any]
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

    @staticmethod
    def _latest_close(frame: Any) -> float:
        try:
            if frame is None or frame.empty or "Close" not in frame.columns:
                return 0.0
            return float(frame.sort_index()["Close"].iloc[-1])
        except Exception:
            return 0.0

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
        if request.manager is None:
            result.failures.append("Execution queue manager is unavailable.")
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
                current_price = (
                    self._latest_close(one_minute)
                    or self._latest_close(five_minute)
                    or request.signal_price_for_symbol(symbol)
                )
                if current_price > 0:
                    request.set_latest_intraday_price(symbol, current_price)
                queue_has_working_order = request.manager.has_pending_or_submitted_order(
                    symbol,
                    environment=request.env,
                )
                broker_has_open_order = request.has_duplicate_open_order(
                    request.env,
                    request.account_no,
                    symbol,
                    OrderSide.BUY,
                    OrderIntent.ENTRY,
                )
                duplicate_order = bool(broker_has_open_order and not queue_has_working_order)
                queue_item = request.manager.build_or_update_from_watchlist_item(
                    watch_item,
                    {"1m": one_minute, "5m": five_minute, "30m": five_minute},
                    current_price=current_price,
                    account_size=request.account_size,
                    risk_percent=request.risk_percent,
                    environment=request.env,
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

        status_text = self._status_text(queue_item.status)
        candidate = queue_item.selected_candidate
        display = build_queue_display_state(queue_item, existing or watch_item)
        entry_price = display.entry_price
        stop_loss = display.stop_loss
        planned_shares = display.planned_shares
        capital_percent = display.capital_percent
        stop_adr = float(display.stop_adr or 0.0)
        risk_percent = display.risk_percent
        selected_window = display.selected_window
        warnings = display.warnings
        score = float(getattr(candidate, "score", 0.0) or 0.0) if candidate else 0.0
        summary = (
            f"Execution queue {status_text}"
            + (f"; selected ORB {selected_window}" if selected_window else "")
            + (f"; entry {entry_price:.2f}" if entry_price > 0 else "")
        )
        trade_plan = display.trade_plan or status_text

        if existing is None:
            # Compatibility mirrors: queue state remains authoritative for display/order flow.
            existing = BuylistItem(
                symbol=symbol,
                name=str(getattr(watch_item, "name", "") or symbol),
                entry_price=entry_price,
                target_price=0.0,
                stop_loss=stop_loss,
                total_score=score,
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
            existing.status = status_text
            existing.ai_summary = summary
            existing.notes = str(getattr(watch_item, "notes", "") or existing.notes or "")
            existing.monitoring_status = status_text
            existing.environment = env
            existing.breakout_price = getattr(watch_item, "breakout_price", None)
            existing.breakout_method = f"execution_queue:{selected_window}" if selected_window else "execution_queue"
            existing.buffer_pct = buffer_pct
            # Preserve existing compatibility mirrors unless they are missing.
            if float(getattr(existing, "entry_price", 0.0) or 0.0) <= 0:
                existing.entry_price = entry_price
            if float(getattr(existing, "stop_loss", 0.0) or 0.0) <= 0:
                existing.stop_loss = stop_loss
            if float(getattr(existing, "total_score", 0.0) or 0.0) <= 0:
                existing.total_score = score
            if float(getattr(existing, "stop_adr", 0.0) or 0.0) <= 0:
                existing.stop_adr = stop_adr
            # Always update sizing fields — auto-selected risk% changes each refresh
            existing.position_percent = capital_percent
            existing.risk_percent = risk_percent
            if not str(getattr(existing, "trade_plan", "") or ""):
                existing.trade_plan = trade_plan
            if not list(getattr(existing, "warnings", []) or []):
                existing.warnings = warnings

        existing._planned_shares = planned_shares
        existing._selected_orb_window = selected_window
        existing._execution_queue_symbol = symbol
        existing._execution_entry_trigger = entry_price

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
        if status_text == "UNKNOWN_SUBMISSION_STATE":
            QMessageBox.warning(
                self.window,
                "Submission state unknown",
                f"{item.symbol} has an unknown broker submission result.\n\n"
                "Reconcile KIS account/order status before clearing this state or submitting again.",
            )
            return
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
        manager.mark_order_submitted(item.symbol, order_status="PENDING", environment=env)
        queue_status = self._execution_queue_status_for_buylist_item(item) or "ORDER_PENDING"
        item.monitoring_status = queue_status
        item.status = queue_status
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
