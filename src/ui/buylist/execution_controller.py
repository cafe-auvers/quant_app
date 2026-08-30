from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence

from PyQt5.QtWidgets import QMessageBox

from src.core.execution_queue import build_queue_display_state
from src.core.order_state import OrderIntent, OrderSide
from src.core.watchlist import BuylistItem
from src.ui.controllers.base import WindowController
from src.strategy.orb.entry_policy import passive_limit_submission_ready


@dataclass
class ExecutionQueueRefreshRequest:
    env: str
    manager: Optional[Any]
    buylist_manager: Any
    target_items: Sequence[Any]
    missing_symbols: List[str] = field(default_factory=list)
    requested_symbols: Optional[List[str]] = None
    # Retained in the request shape for compatibility with older callers.
    # Execution sizing requires account_size_for_account's exact account
    # equity and never falls back to this selected-account estimate.
    account_size: float = 0.0
    risk_percent: float = 0.01
    buffer_pct: float = 0.001
    buffer_pct_for_symbol: Callable[[str], Optional[float]] = lambda _symbol: None
    account_no: str = ""
    account_no_for_symbol: Callable[[str], str] = lambda _symbol: ""
    account_size_for_account: Callable[[str, str], Optional[float]] = (
        lambda _environment, _account_no: None
    )
    trade_card_engine: Optional[Any] = None
    window_days: int = 7
    latest_intraday_session: Callable[[Any], Any] = lambda frame: frame
    load_intraday_interval: Callable[[str, str, int], Any] = (
        lambda _symbol, _interval, _window_days: None
    )
    signal_price_for_symbol: Callable[[str], float] = lambda _symbol: 0.0
    set_latest_intraday_price: Callable[[str, float], None] = (
        lambda _symbol, _price: None
    )
    has_duplicate_open_order: Callable[
        [str, str, str, OrderSide, OrderIntent], bool
    ] = lambda *_args: False
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
    canonical_changed_keys: List[str] = field(default_factory=list)

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

    def refresh_execution_queue(
        self, request: ExecutionQueueRefreshRequest
    ) -> ExecutionQueueRefreshResult:
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

        for planning_item in request.target_items:
            symbol = str(getattr(planning_item, "symbol", "") or "").strip().upper()
            if not symbol:
                continue
            try:
                item_account_no = str(
                    request.account_no_for_symbol(symbol)
                    or getattr(planning_item, "kis_account_no", "")
                    or request.account_no
                    or ""
                ).strip()
                try:
                    resolved_account_size = request.account_size_for_account(
                        request.env,
                        item_account_no,
                    )
                    item_account_size = (
                        float(resolved_account_size)
                        if resolved_account_size is not None
                        else 0.0
                    )
                except (TypeError, ValueError, OverflowError):
                    item_account_size = 0.0
                if not math.isfinite(item_account_size):
                    item_account_size = 0.0
                if not math.isfinite(item_account_size) or item_account_size <= 0:
                    # Missing account truth is temporary, not an invalid ORB
                    # structure. Preserve any last-good queue snapshot rather
                    # than rebuilding every window as terminal RISK_INVALID;
                    # otherwise the runtime can incorrectly return a valid
                    # published Buy Today card to Buylist.
                    transient_status = "ACCOUNT_EQUITY_UNAVAILABLE"
                    result.status_counts[transient_status] = (
                        result.status_counts.get(transient_status, 0) + 1
                    )
                    continue
                try:
                    item_buffer_pct = float(request.buffer_pct_for_symbol(symbol))
                except (TypeError, ValueError, OverflowError):
                    item_buffer_pct = float(request.buffer_pct)
                if (
                    not math.isfinite(item_buffer_pct)
                    or item_buffer_pct < 0.0
                    or item_buffer_pct > 1.0
                ):
                    item_buffer_pct = 0.001
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
                queue_has_working_order = (
                    request.manager.has_pending_or_submitted_order(
                        symbol,
                        environment=request.env,
                    )
                )
                broker_has_open_order = request.has_duplicate_open_order(
                    request.env,
                    item_account_no,
                    symbol,
                    OrderSide.BUY,
                    OrderIntent.ENTRY,
                )
                duplicate_order = bool(
                    broker_has_open_order and not queue_has_working_order
                )
                queue_item = request.manager.build_or_update_from_watchlist_item(
                    planning_item,
                    {"1m": one_minute, "5m": five_minute, "30m": five_minute},
                    current_price=current_price,
                    account_size=item_account_size,
                    risk_percent=request.risk_percent,
                    environment=request.env,
                    account_no=item_account_no,
                    adr_percent=request.adr_percent_for_symbol(symbol),
                    buffer_pct=item_buffer_pct,
                    duplicate_pending_order=duplicate_order,
                    # The UI request has already resolved the canonical,
                    # per-symbol buffer. Legacy imported plan data may keep
                    # its risk/window lock but cannot override this value.
                    force_buffer_pct=True,
                )
                sync = self.apply_execution_queue_item_to_buylist(
                    queue_item,
                    planning_item,
                    request.env,
                    item_buffer_pct,
                    buylist_manager=request.buylist_manager,
                    trade_card_engine=request.trade_card_engine,
                    default_account_no=item_account_no,
                )
                if sync is not None and sync.changed and sync.card_key:
                    result.canonical_changed_keys.append(sync.card_key)
                status_text = self._status_text(queue_item.status)
                result.status_counts[status_text] = (
                    result.status_counts.get(status_text, 0) + 1
                )
                result.refreshed += 1
            except Exception as exc:
                result.failures.append(f"{symbol}: {exc}")
        return result

    def apply_execution_queue_item_to_buylist(
        self,
        queue_item,
        planning_item,
        env: str,
        buffer_pct: float,
        buylist_manager: Optional[Any] = None,
        trade_card_engine: Optional[Any] = None,
        default_account_no: str = "",
    ) -> Any:
        symbol = str(queue_item.symbol or "").upper()
        if not symbol:
            return

        protected_statuses = {
            "BOUGHT",
            "BUY_SUBMITTED",
            "BUY_PARTIAL",
            "SELL_SUBMITTED",
            "PARTIAL_EXIT_SUBMITTED",
            "PARTIAL_EXIT_RESERVED",
            "SELL_RESERVED",
            "SOLD",
        }
        manager = (
            buylist_manager
            if buylist_manager is not None
            else self.window.buylist_manager
        )
        existing = manager.get(symbol, env)
        if existing is not None:
            existing_status = str(
                getattr(existing, "monitoring_status", "") or ""
            ).upper()
            if (
                existing_status == "FILLED"
                and int(getattr(existing, "shares_held", 0) or 0) > 0
            ):
                existing.monitoring_status = "BOUGHT"
                existing_status = "BOUGHT"
            if existing_status in protected_statuses:
                return

        status_text = self._status_text(queue_item.status)
        candidate = queue_item.selected_candidate
        display = build_queue_display_state(queue_item, existing or planning_item)
        entry_price = display.entry_price
        stop_loss = display.stop_loss
        capital_percent = display.capital_percent
        stop_adr = float(display.stop_adr or 0.0)
        risk_percent = display.risk_percent
        selected_window = display.selected_window
        warnings = display.warnings
        score = float(getattr(candidate, "score", 0.0) or 0.0) if candidate else 0.0
        # ``buffer_pct`` was resolved before candidate construction and is the
        # one authoritative value for this refresh. Legacy imported plan data
        # may retain a selected window/risk, but cannot replace a published
        # card's buffer after executor handoff.
        effective_buffer_pct = buffer_pct
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
                name=str(getattr(planning_item, "name", "") or symbol),
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
                notes=str(getattr(planning_item, "notes", "") or ""),
                risk_percent=risk_percent,
                trade_plan=trade_plan,
                monitoring_status=status_text,
                environment=env,
                breakout_price=getattr(planning_item, "breakout_price", None),
                breakout_method=f"execution_queue:{selected_window}"
                if selected_window
                else "execution_queue",
                buffer_pct=effective_buffer_pct,
                kis_account_no=str(default_account_no or "").strip(),
            )
            from src.services.buylist_membership_service import add_to_buylist

            return add_to_buylist(
                manager,
                existing,
                engine=trade_card_engine,
                default_account_no=default_account_no,
            )
        else:
            existing.name = str(
                getattr(planning_item, "name", "") or existing.name or symbol
            )
            existing.status = status_text
            existing.ai_summary = summary
            existing.notes = str(
                getattr(planning_item, "notes", "") or existing.notes or ""
            )
            existing.monitoring_status = status_text
            existing.environment = env
            existing.breakout_price = getattr(planning_item, "breakout_price", None)
            existing.breakout_method = (
                f"execution_queue:{selected_window}"
                if selected_window
                else "execution_queue"
            )
            existing.buffer_pct = effective_buffer_pct
            resolved_account_no = str(default_account_no or "").strip()
            if resolved_account_no:
                existing.kis_account_no = resolved_account_no
            # A manual queue-window choice is explicit, so refresh its
            # compatibility mirror. Auto-selected queue rows keep their
            # existing values unless missing.
            use_selected_plan = bool(getattr(queue_item, "manual_window_lock", False))
            if (
                use_selected_plan
                or float(getattr(existing, "entry_price", 0.0) or 0.0) <= 0
            ):
                existing.entry_price = entry_price
            if (
                use_selected_plan
                or float(getattr(existing, "stop_loss", 0.0) or 0.0) <= 0
            ):
                existing.stop_loss = stop_loss
            if (
                use_selected_plan
                or float(getattr(existing, "total_score", 0.0) or 0.0) <= 0
            ):
                existing.total_score = score
            if (
                use_selected_plan
                or float(getattr(existing, "stop_adr", 0.0) or 0.0) <= 0
            ):
                existing.stop_adr = stop_adr
            # Always update sizing fields — auto-selected risk% changes each refresh
            existing.position_percent = capital_percent
            existing.risk_percent = risk_percent
            if use_selected_plan or not str(getattr(existing, "trade_plan", "") or ""):
                existing.trade_plan = trade_plan
            if use_selected_plan or not list(getattr(existing, "warnings", []) or []):
                existing.warnings = warnings

        from src.services.buylist_membership_service import reconcile_buylist_item

        return reconcile_buylist_item(
            trade_card_engine,
            existing,
            default_account_no=default_account_no,
        )

    def submit_selected_queue_order(self, env: str) -> None:
        window = self.window
        submission_guard = getattr(window, "_state_sync_allows_order_submission", None)
        if callable(submission_guard) and not submission_guard():
            return
        item = window._buylist_selected_item(env)
        if not item:
            QMessageBox.warning(
                window, "No selection", "Select an execution queue row first."
            )
            return
        queue_item = window._queue_item_for_buylist_item(item)
        if queue_item is None:
            QMessageBox.warning(
                window,
                "No queue item",
                f"{item.symbol} is not in the execution queue. Click Refresh Queue first.",
            )
            return
        candidate = getattr(queue_item, "selected_candidate", None)
        status_text = self._status_text(getattr(queue_item, "status", ""))
        if status_text == "UNKNOWN_SUBMISSION_STATE":
            QMessageBox.warning(
                window,
                "Submission state unknown",
                f"{item.symbol} has an unknown broker submission result.\n\n"
                "Reconcile KIS account/order status before clearing this state or submitting again.",
            )
            return
        if candidate is None or status_text != "EXECUTE_READY":
            QMessageBox.warning(
                window,
                "Not ready",
                f"{item.symbol} is {status_text or 'not ready'}; submit is allowed only when status is EXECUTE_READY.",
            )
            return
        try:
            shares_value = float(candidate.shares)
            execution_price = float(candidate.execution_price)
        except (TypeError, ValueError, OverflowError):
            shares_value = 0.0
            execution_price = 0.0
        if (
            not math.isfinite(shares_value)
            or not shares_value.is_integer()
            or shares_value < 1
            or not math.isfinite(execution_price)
            or execution_price <= 0
            or not bool(getattr(candidate, "breakout_confirmed", False))
        ):
            QMessageBox.warning(
                window,
                "Invalid order",
                f"{item.symbol} lacks a confirmed passive execution plan.",
            )
            return
        worker = getattr(window, "_buyboard_runtime_worker", None)
        runtime = getattr(worker, "runtime", None)
        market_data = getattr(runtime, "market_data", None)
        now = datetime.now(timezone.utc)
        quote = (
            market_data.latest_quote(item.symbol) if market_data is not None else None
        )
        if (
            market_data is None
            or quote is None
            or not market_data.entry_quote_ready(item.symbol, now=now)
            or not quote.is_execution_fresh(now=now)
            or not passive_limit_submission_ready(
                last_trade=quote.last_price,
                best_ask=quote.ask,
                execution_price=execution_price,
            )
        ):
            QMessageBox.warning(
                window,
                "Passive entry not ready",
                "A fresh WebSocket last trade and best ask must both be strictly "
                "above the exact execution price.",
            )
            return
        if window._buylist_auto_order_blocked(item):
            QMessageBox.warning(
                window,
                "KIS order blocked",
                f"{item.symbol} cannot be submitted through the selected KIS account/API:\n"
                f"{getattr(item, 'auto_order_block_reason', '')}",
            )
            return

        window_values = getattr(window, "__dict__", {})
        selected_account_fn = window_values.get("_selected_order_account_for_item")
        if selected_account_fn is None:
            selected_account_fn = getattr(
                type(window), "_selected_order_account_for_item", None
            )
        account_no = (
            (
                selected_account_fn(item, env)
                if "_selected_order_account_for_item" in window_values
                else selected_account_fn(window, item, env)
            )
            if callable(selected_account_fn)
            else window._first_account_no_for_environment(env)
        )
        if not account_no:
            warn_account_fn = window_values.get("_warn_order_account_unavailable")
            if warn_account_fn is None:
                warn_account_fn = getattr(
                    type(window), "_warn_order_account_unavailable", None
                )
            if callable(warn_account_fn):
                if "_warn_order_account_unavailable" in window_values:
                    warn_account_fn(item, env)
                else:
                    warn_account_fn(window, item, env)
            else:
                QMessageBox.warning(
                    window,
                    "KIS account required",
                    "Select a configured KIS account before submitting an order.",
                )
            return
        if window._has_duplicate_open_order(
            env,
            account_no,
            item.symbol,
            OrderSide.BUY,
            OrderIntent.ENTRY,
        ):
            QMessageBox.warning(
                window,
                "Duplicate order",
                f"An open BUY ENTRY order already exists for {item.symbol}.",
            )
            return

        review = window._format_execution_queue_order_review(env, item, queue_item)
        title = f"Submit {env} BUY Order"
        body = review
        if env == "PROD":
            body = "This will submit a live PROD BUY order.\n\n" + review
        reply = QMessageBox.question(
            window,
            title,
            body + "\n\nSubmit this order?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        manager = window._ensure_execution_queue_manager()
        manager.mark_order_submitted(
            item.symbol, order_status="PENDING", environment=env
        )
        queue_status = (
            window._execution_queue_status_for_buylist_item(item) or "ORDER_PENDING"
        )
        item.monitoring_status = queue_status
        item.status = queue_status
        item._buy_order_pending = True
        window._save_buylist_state()
        window._save_execution_queue_state()
        window.populate_buylist_dashboard()
        window._submit_kis_buy_order(
            item,
            quantity=int(shares_value),
            order_price=execution_price,
        )
