"""Buylist monitoring orchestration."""

import datetime as dt
import math
from typing import List, Optional, Tuple

from PyQt5.QtCore import QTimer
from PyQt5.QtWidgets import QLabel, QPushButton

from src.core.execution_config import is_buyboard_engine_enabled
from src.core.entry_monitoring_command import build_entry_monitoring_command
from src.core.execution_queue import (
    BROKER_UNCERTAIN_STATUSES,
    POSITION_HOLDING_STATUSES,
    ExecutionQueueStatus,
)
from src.core.order_state import OrderIntent, OrderSide

from .constants import US_MARKET_ZONE


class BuylistMonitoringMixin:
    def _legacy_auto_execution_blocked(
        self, env: str, symbol: str = "", *, account_no: str = "", is_protective_exit: bool = False
    ) -> bool:
        """Mutual-exclusion guard between the legacy 60-second monitor and
        the new Kanban trading engine (code review: "There must never be two
        separate automated monitors capable of submitting orders for the
        same account and symbol"). Once ``BUYBOARD_ENGINE_ENABLED=true``,
        ``src.services.trading_engine`` owns every automatic entry/exit
        decision; the legacy Buy Dashboard's monitor becomes
        display/manual-emergency-only. This blocks only *automatic*
        triggers (the 60s cycle, auto-replace, auto-reprice) -- a
        user-clicked button (Activate, Sell All, Sell 1/3-1/2, ...) is
        unaffected, matching "legacy dashboard becomes display/manual-
        emergency-only" rather than fully disabling the tab.

        ``is_protective_exit`` (review finding P0: "the fallback re-enables
        legacy entries as well as exits" -- a single symmetric on/off
        switch was too blunt): when the Buy Board engine's health is
        uncertain, a *new automatic entry* must stay blocked on both sides
        (starting a fresh position off an engine whose true state is
        unknown is never safe), but an existing position's *stop-loss
        protection* must fail open to the legacy monitor rather than leave
        the position with no automatic protection at all. Pass
        ``is_protective_exit=True`` only from a stop-loss/liquidation call
        site; entry call sites use the default.

        ``account_no`` (review finding P0: "partial account failure still
        creates cross-account dual execution" -- a global health check
        meant one account's startup-reconciliation failure suppressed
        legacy protection for *every* account, including ones the Buy
        Board engine had already fully confirmed and was actively
        managing, giving that healthy account two simultaneously-active
        exit engines) scopes the health check to the specific account this
        call concerns. Always pass the item/card's real account number
        from every call site.
        """
        if not is_buyboard_engine_enabled():
            return False
        # E1: an unhealthy Kanban engine never implicitly transfers a
        # KANBAN-owned symbol back to legacy.  The durable gateway enforces
        # this too; checking here prevents the legacy monitor from even
        # attempting the forbidden mutation and produces the intended alert.
        kanban_owned = False
        ownership_known = False
        engine_resolver = getattr(self, "_execution_state_engine", None)
        ownership_engine = (
            engine_resolver()
            if callable(engine_resolver)
            else self.__dict__.get("pc_db_engine")
        )
        if ownership_engine is not None and account_no and symbol:
            try:
                from src.core.execution_ownership import ExecutionOwner
                from src.services.execution_ownership_repository import get_ownership

                ownership = get_ownership(
                    ownership_engine,
                    environment=env,
                    account_no=account_no,
                    symbol=symbol,
                )
                ownership_known = True
                kanban_owned = ownership.owner == ExecutionOwner.KANBAN
            except Exception:
                # UNKNOWN is not LEGACY. The durable gateway is still the
                # final boundary, but the monitor must not even attempt to
                # fail open when ownership could not be proved.
                ownership_known = False
        # Review finding P0: flag-only suppression is unsafe -- if the new
        # engine has stopped or failed while the flag stays on, this would
        # leave *both* engines dark with nothing protecting open positions.
        # Only actually suppress once _buyboard_engine_healthy() confirms
        # the worker is running, has finished startup reconciliation, and
        # produced a recent heartbeat; a health-check method that does not
        # exist at all (a lightweight test/dummy window not modeling the
        # new engine) is treated as healthy for backward compatibility --
        # it is the *presence of an unhealthy worker* that must fail open,
        # not merely "nobody checked."
        health_check = getattr(self, "_buyboard_engine_healthy", None)
        # account_no is passed through as-given, deliberately NOT
        # collapsed via "account_no or None" -- review finding P0: "blank
        # kis_account_no values are also converted into a global health
        # check rather than failing safely." A blank/unresolved account
        # must fail its own (never-positively-confirmed) account-specific
        # check, not silently fall back to the coarser global one.
        if callable(health_check):
            try:
                engine_healthy = health_check(
                    account_no,
                    action=("PROTECTIVE_EXIT" if is_protective_exit else "NEW_ENTRY"),
                    symbol=symbol,
                )
            except TypeError as exc:
                # Compatibility with lightweight test/dummy windows whose
                # health seam predates action-specific reconciliation.
                if "unexpected keyword argument" not in str(exc):
                    raise
                engine_healthy = health_check(account_no)
        else:
            engine_healthy = True
        if not engine_healthy:
            logged = self.__dict__.setdefault("_buyboard_engine_unhealthy_notice_logged", set())
            if account_no not in logged:
                logged.add(account_no)
                if not ownership_known:
                    message = (
                        f"CRITICAL: execution ownership is UNKNOWN for {symbol} "
                        f"({account_no or env}). Legacy automatic entries and "
                        "protective exits remain BLOCKED until durable LEGACY "
                        "ownership is positively verified."
                    )
                elif kanban_owned:
                    message = (
                        f"CRITICAL: the Buy Board engine is unhealthy for KANBAN-owned "
                        f"{symbol} ({account_no or env}). Legacy may observe but all "
                        "legacy mutations remain BLOCKED; an explicit audited ownership "
                        "transfer is required before legacy can protect this symbol."
                    )
                else:
                    message = (
                        f"CRITICAL: BUYBOARD_ENGINE_ENABLED=true but the Buy Board "
                        f"engine is not confirmed healthy for account {account_no or env} "
                        "(not running, lease lost, startup reconciliation incomplete "
                        "for this account, or no recent heartbeat) -- legacy "
                        "stop-loss protection remains ACTIVE as a fail-safe, but new "
                        "automatic entries stay BLOCKED on both engines until health "
                        "is confirmed. Investigate the Buy Board engine immediately."
                    )
                # Review: a log-only alert "does not protect unattended
                # trading when the user is asleep / the app is minimized."
                # notify falls back to append_log for any caller (test
                # double, minimal window) that doesn't define the richer
                # notification method.
                notify = getattr(self, "_show_critical_notification", None)
                if callable(notify):
                    notify("Buy Board Engine Unhealthy", message)
                else:
                    self.append_log(message)
            # Fail open only for protective exits; a new entry must not
            # start on either engine while the authoritative one's state is
            # unknown.
            if not ownership_known or kanban_owned:
                return True
            return not is_protective_exit
        logged = self.__dict__.get("_buyboard_engine_unhealthy_notice_logged")
        if logged:
            logged.discard(account_no)
        # self.__dict__.get(...), not getattr(self, ...): on a QObject whose
        # __init__ never ran (some lightweight test doubles), PyQt5 raises
        # RuntimeError for an unset attribute instead of AttributeError, which
        # getattr's default does not catch -- src.ui.main_window already
        # works around the same issue this way (see _sync_buyboard_runtime_worker).
        if not self.__dict__.get("_legacy_auto_execution_notice_logged", False):
            self._legacy_auto_execution_notice_logged = True
            self.append_log(
                "[Buylist] Legacy automatic order submission is suppressed "
                "(BUYBOARD_ENGINE_ENABLED=true, Buy Board engine confirmed "
                "healthy) -- the Buy Board engine now owns automatic "
                "entry/exit execution and all operator actions."
            )
        return True

    def _toggle_buylist_monitor(self, env: str) -> None:
        """Toggle the production monitor timer."""
        active_attr = f"_buylist_{env.lower()}_monitor_active"
        timer_attr = f"buylist_{env.lower()}_monitor_timer"
        lbl_attr = f"buylist_{env.lower()}_monitor_status_label"
        btn_name = f"buylistMonitorToggle_{env}"

        if not hasattr(self, timer_attr):
            return

        timer: QTimer = getattr(self, timer_attr)
        is_active: bool = getattr(self, active_attr, False)
        lbl: QLabel = getattr(self, lbl_attr, None)
        btn = self.findChild(QPushButton, btn_name)

        if is_active:
            timer.stop()
            setattr(self, active_attr, False)
            if lbl:
                lbl.setText("Monitor: OFF")
                lbl.setStyleSheet("color: #888;")
            if btn:
                btn.setText("Start Monitor")
            self.append_log(f"[Buylist/{env}] Monitor stopped.")
        else:
            timer.start(60000)
            setattr(self, active_attr, True)
            if lbl:
                lbl.setText("Monitor: ON (60s)")
                lbl.setStyleSheet("color: #4CAF50; font-weight: bold;")
            if btn:
                btn.setText("Stop Monitor")
            self.append_log(
                f"[Buylist/{env}] Monitor started — checking every 60 seconds."
            )
            self._run_buylist_monitor_cycle(env)  # run immediately

    def _ensure_buylist_monitor_running(self, env: str) -> bool:
        """Start one environment's buylist monitor if it is currently off."""
        active_attr = f"_buylist_{env.lower()}_monitor_active"
        if getattr(self, active_attr, False):
            return False
        if not hasattr(self, "_toggle_buylist_monitor"):
            return False
        self._toggle_buylist_monitor(env)
        return bool(getattr(self, active_attr, False))

    def _auto_submit_execute_ready_queue_items(self, env: str) -> None:
        """Auto-submit EXECUTE_READY execution-queue items when the monitor is active for env."""
        active_attr = f"_buylist_{env.lower()}_monitor_active"
        if not getattr(self, active_attr, False):
            return
        # The legacy 60-second monitor has no execution-grade best-ask
        # stream, so it cannot prove the strict passive-pullback predicate
        # (fresh last > execution price AND fresh ask > execution price).
        # Automatic entries therefore belong exclusively to the guarded Buy
        # Board engine.  Keeping this path observation-only is fail-closed and
        # prevents the old entry-trigger price from becoming a live order.
        if not is_buyboard_engine_enabled():
            if not self.__dict__.get(
                "_legacy_passive_entry_block_notice_logged", False
            ):
                self._legacy_passive_entry_block_notice_logged = True
                self.append_log(
                    f"[Buylist/{env}] Legacy automatic BUY is disabled: "
                    "the passive pullback contract requires fresh WebSocket "
                    "last/ask data from the guarded Buy Board engine."
                )
            return
        if not hasattr(self, "buylist_manager"):
            return
        items = [it for it in self.buylist_manager.items if it.environment == env]
        for it in items:
            if it.monitoring_status != "EXECUTE_READY":
                continue
            if not self._is_pre_entry_execution_queue_buylist_item(it):
                continue
            if not getattr(it, "orb_monitor_enabled", False):
                continue
            if getattr(it, "_buy_order_pending", False):
                continue
            # Review finding P0: checked per-item (not once for the whole
            # env) so one account's Buy Board health does not block -- or
            # wrongly appear to clear -- another account's entries.
            if self._legacy_auto_execution_blocked(
                env, it.symbol, account_no=getattr(it, "kis_account_no", "") or ""
            ):
                continue
            auto_order_blocked = self._buylist_auto_order_blocked(it)
            if auto_order_blocked:
                if not getattr(it, "_auto_order_block_notice_logged", False):
                    it._auto_order_block_notice_logged = True
                    self.append_log(
                        f"[Buylist/{env}] {it.symbol} EXECUTE_READY but auto order is blocked: "
                        f"{getattr(it, 'auto_order_block_reason', '')}"
                    )
                continue
            queue_item = self._queue_item_for_buylist_item(it)
            candidate = (
                getattr(queue_item, "selected_candidate", None)
                if queue_item is not None
                else None
            )
            if candidate is None:
                self.append_log(
                    f"[Buylist/{env}] {it.symbol} EXECUTE_READY but no selected candidate — skipping auto-buy."
                )
                continue
            entry_trigger = float(getattr(candidate, "entry_trigger", 0.0) or 0.0)
            shares = int(getattr(candidate, "shares", 0) or 0)
            if shares < 1 or entry_trigger <= 0:
                self.append_log(
                    f"[Buylist/{env}] {it.symbol} EXECUTE_READY but invalid order "
                    f"(shares={shares}, trigger={entry_trigger:.2f}) — skipping auto-buy."
                )
                continue
            current_price = self.latest_intraday_prices.get(it.symbol, 0.0)
            mgr = self._ensure_execution_queue_manager()
            if mgr is not None:
                mgr.mark_order_submitted(
                    it.symbol, order_status="PENDING", environment=env
                )
            queue_status = (
                self._execution_queue_status_for_buylist_item(it) or "ORDER_PENDING"
            )
            it.monitoring_status = queue_status
            it.status = queue_status
            it._buy_order_pending = True
            self._save_buylist_state()
            self._save_execution_queue_state()
            self.append_log(
                f"[Buylist/{env}] {it.symbol} EXECUTE_READY — auto-submitting BUY "
                f"{shares} shares @ ${entry_trigger:.2f} (current ${current_price:.2f})"
            )
            self._submit_kis_buy_order(it, quantity=shares, order_price=entry_trigger)

    def _deactivate_pre_entry_orb_monitoring(self) -> None:
        """Reset all pre-entry ORB/queue monitoring to WATCHING at market close.

        BOUGHT positions (and any order mid-flight) are left untouched — they still need
        stop-loss monitoring next session. Today's ORB ranges (1m/5m/30m) are meaningless
        tomorrow since a new session opens a new range, so pre-entry items are fully reset
        rather than just paused, avoiding a stale window/candidate carried into the open.
        """
        if not hasattr(self, "buylist_manager"):
            return

        from src.core.execution_queue import (
            ExecutionQueueStatus,
            PRE_ENTRY_QUEUE_STATUSES,
        )

        manager = self.__dict__.get("execution_queue_manager")
        pre_entry_watching = PRE_ENTRY_QUEUE_STATUSES
        submitted_entry_statuses = {
            ExecutionQueueStatus.ORDER_PENDING.value,
            ExecutionQueueStatus.ORDER_SUBMITTED.value,
            ExecutionQueueStatus.UNKNOWN_SUBMISSION_STATE.value,
        }
        reset_symbols: List[Tuple[str, str]] = []
        cancel_pending_symbols: List[Tuple[str, str]] = []

        for item in self.buylist_manager.items:
            status = str(getattr(item, "monitoring_status", "") or "").upper()
            is_queue = self._is_execution_queue_buylist_item(item)

            if is_queue:
                if status in submitted_entry_statuses:
                    symbol = str(getattr(item, "symbol", "") or "").upper()
                    environment = str(
                        getattr(item, "environment", "") or "PROD"
                    ).upper()
                    if status == ExecutionQueueStatus.UNKNOWN_SUBMISSION_STATE.value:
                        self.append_log(
                            f"[Buylist/{environment}] Market close: {symbol} has UNKNOWN_SUBMISSION_STATE; "
                            "left unresolved for broker reconciliation."
                        )
                        cancel_pending_symbols.append((symbol, environment))
                        continue
                    self._request_eod_entry_buy_cancellation(item, environment)
                    if self._open_broker_orders_for_buylist_item(
                        item,
                        environment,
                        side=OrderSide.BUY,
                        intent=OrderIntent.ENTRY,
                    ):
                        cancel_pending_symbols.append((symbol, environment))
                        continue
                    status = str(getattr(item, "monitoring_status", "") or "").upper()
                if status not in pre_entry_watching and status not in {
                    "ACTIVE",
                    ExecutionQueueStatus.REJECTED.value,
                    ExecutionQueueStatus.EXPIRED.value,
                }:
                    continue
            elif status != "ACTIVE":
                continue

            symbol = str(getattr(item, "symbol", "") or "").upper()
            environment = str(getattr(item, "environment", "") or "PROD").upper()

            if is_queue:
                account_no = str(getattr(item, "kis_account_no", "") or "")
                if account_no:
                    monitoring_command = build_entry_monitoring_command(
                        environment=environment,
                        account_no=account_no,
                        symbol=symbol,
                        enabled=False,
                    )
                    item.orb_monitor_enabled = monitoring_command.enabled
                else:
                    item.orb_monitor_enabled = False
                item._buy_order_pending = False
                item._auto_order_block_notice_logged = False
                item._orb_queue_required_notice_logged = False
                self._clear_buylist_auto_order_block(item)

                queue_item = (
                    manager.get_item(symbol, environment)
                    if manager is not None
                    else None
                )
                if queue_item is not None:
                    queue_item.locked = False
                    queue_item.locked_reason = None
                    queue_item.manual_window_lock = False
                    queue_item.candidates = {}
                    queue_item.selected_window = None
                    queue_item.selected_candidate = None
                    queue_item.order_status = None
                    queue_item.order_id = None
                    queue_item.warnings = []
                    queue_item.status = ExecutionQueueStatus.WATCHING

            item.monitoring_status = "WATCHING"
            item.status = "WATCHING"
            reset_symbols.append((symbol, environment))

        if cancel_pending_symbols:
            symbols_text = ", ".join(
                f"{sym}/{env}" for sym, env in cancel_pending_symbols
            )
            self.append_log(
                f"[Buylist] Market close: entry BUY cancellation/reconciliation still pending for: {symbols_text}"
            )

        if not reset_symbols:
            return

        self._save_buylist_state()
        if manager is not None:
            self._save_execution_queue_state()
        if hasattr(self, "populate_buylist_dashboard"):
            self.populate_buylist_dashboard()
        symbols_text = ", ".join(f"{sym}/{env}" for sym, env in reset_symbols)
        self.append_log(
            f"[Buylist] Market closed — deactivated pre-entry ORB monitoring for: {symbols_text}"
        )

    def _run_buylist_monitor_cycle(self, env: str) -> None:
        """Check ACTIVE/BOUGHT items for one environment and fire orders as needed."""
        if not hasattr(self, "buylist_manager"):
            return

        items = [it for it in self.buylist_manager.items if it.environment == env]
        self._restore_monitorable_buylist_error_positions(items, env)
        active_items = [
            it for it in items if it.monitoring_status in ("ACTIVE", "BOUGHT")
        ]
        stop_reprice_items = [
            it
            for it in items
            if str(getattr(it, "monitoring_status", "") or "").upper()
            == "SELL_SUBMITTED"
        ]

        # Execution queue items whose trigger hasn't fired yet. Sourced from
        # the shared handoff-safety constants (src/core/execution_queue.py)
        # plus the terminal "SOLD" status, so this can never silently drift
        # from what handoff reconciliation treats as broker-uncertain/held.
        _skip_statuses = (
            BROKER_UNCERTAIN_STATUSES
            | POSITION_HOLDING_STATUSES
            | {
                "SOLD",
                ExecutionQueueStatus.REJECTED.value,
                ExecutionQueueStatus.EXPIRED.value,
            }
        )
        queue_watching_items = [
            it
            for it in items
            if self._is_pre_entry_execution_queue_buylist_item(it)
            and it.monitoring_status not in _skip_statuses
            and not getattr(it, "_buy_order_pending", False)
            and getattr(it, "orb_monitor_enabled", False)
        ]

        if not active_items and not queue_watching_items and not stop_reprice_items:
            return

        # Trigger an async KIS-first intraday refresh for execution-queue items.
        # When the worker finishes, _on_intraday_bulk_finished calls refresh_execution_queue
        # and then _auto_submit_execute_ready_queue_items, which fires any EXECUTE_READY orders.
        if queue_watching_items and hasattr(self, "refresh_watchlist_intraday_cache"):
            worker = getattr(self, "intraday_bulk_worker", None)
            if worker is None or not worker.isRunning():
                self.refresh_watchlist_intraday_cache(
                    show_messages=False,
                    triggered_by_live=True,
                    source="buylist monitor",
                    symbols=[item.symbol for item in queue_watching_items],
                    purpose="buyboard_orb",
                )

        for item in stop_reprice_items:
            self._buylist_refresh_item_data(item)
            current_price = self.latest_intraday_prices.get(item.symbol, 0.0)
            if current_price > 0:
                self._maybe_reprice_stop_loss_sell(item, env, current_price)

        for item in active_items:
            self._buylist_refresh_item_data(item)
            try:
                current_price = float(self.latest_intraday_prices.get(item.symbol, 0.0))
            except (TypeError, ValueError):
                current_price = 0.0
            if not math.isfinite(current_price) or current_price <= 0:
                continue

            if item.monitoring_status == "ACTIVE":
                if not getattr(
                    item, "_legacy_active_entry_block_notice_logged", False
                ):
                    item._legacy_active_entry_block_notice_logged = True
                    self.append_log(
                        f"[Buylist/{env}] {item.symbol} skipping legacy ACTIVE auto-buy; "
                        "entry blocked because only EXECUTE_READY execution-queue "
                        "strategies may submit entry orders."
                    )
                continue

            if item.monitoring_status == "BOUGHT":
                auto_order_blocked = self._buylist_auto_order_blocked(
                    item
                ) or self._legacy_auto_execution_blocked(
                    env,
                    item.symbol,
                    account_no=getattr(item, "kis_account_no", "") or "",
                    is_protective_exit=True,
                )
                exit_order_pending = getattr(item, "_exit_order_pending", False)
                if (
                    item.stop_loss > 0
                    and current_price <= item.stop_loss
                    and not auto_order_blocked
                    and not getattr(item, "_stop_order_pending", False)
                ):
                    item._stop_order_pending = True
                    self.append_log(
                        f"[Buylist/{env}] STOP HIT — {item.symbol} ${current_price:.2f} "
                        f"<= stop ${item.stop_loss:.2f}. Submitting SELL ALL."
                    )
                    self._submit_kis_sell_order(
                        item,
                        item.shares_held,
                        reason="stop-loss",
                        order_price=self._stop_loss_sell_limit_price(current_price),
                    )
                    continue
                elif (
                    item.stop_loss > 0
                    and current_price <= item.stop_loss
                    and auto_order_blocked
                    and not getattr(item, "_auto_order_block_notice_logged", False)
                ):
                    item._auto_order_block_notice_logged = True
                    self.append_log(
                        f"[Buylist/{env}] STOP still hit for {item.symbol}, but auto KIS order is blocked: "
                        f"{getattr(item, 'auto_order_block_reason', '')}"
                    )
                    continue

                if auto_order_blocked or exit_order_pending:
                    continue

                days_held = self._buylist_days_held(item)
                alert_changed = self._update_manual_exit_alerts(item, env, days_held)
                if alert_changed:
                    self._save_buylist_state()
        self._populate_buylist_env_table(env)

    def _restore_monitorable_buylist_error_positions(self, items, env: str) -> None:
        """Keep held positions monitorable after a rejected exit order."""
        changed = False
        for item in items:
            if getattr(item, "monitoring_status", "") != "ERROR":
                continue
            try:
                shares_held = int(getattr(item, "shares_held", 0) or 0)
            except (TypeError, ValueError):
                shares_held = 0
            if shares_held <= 0:
                continue

            item.monitoring_status = "BOUGHT"
            item._stop_order_pending = False
            changed = True
            self.append_log(
                f"[Buylist/{env}] {item.symbol} restored from ERROR to BOUGHT "
                f"because {shares_held} shares are still marked held."
            )
        if changed:
            self._save_buylist_state()

    @staticmethod
    def _buylist_auto_order_blocked(item) -> bool:
        from src.ui.buylist.controller import BuylistController

        return BuylistController.auto_order_blocked(item)

    def _set_buylist_auto_order_block(self, item, reason: str) -> None:
        item.auto_order_block_reason = str(reason or "").strip()
        item._buy_order_pending = False
        item._stop_order_pending = False
        item._exit_order_pending = False
        item._auto_order_block_notice_logged = False

    def _clear_buylist_auto_order_block(self, item) -> None:
        if hasattr(item, "auto_order_block_reason"):
            item.auto_order_block_reason = ""
        item._auto_order_block_notice_logged = False

    @staticmethod
    def _set_attr_if_changed(item, attr: str, value) -> bool:
        from src.ui.buylist.controller import BuylistController

        return BuylistController.set_attr_if_changed(item, attr, value)

    def _update_manual_exit_alerts(self, item, env: str, days_held: int) -> bool:
        changed = False
        suggested_action = "Review manually; no automatic sell submitted."

        try:
            shares_held = int(getattr(item, "shares_held", 0) or 0)
        except (TypeError, ValueError):
            shares_held = 0

        partial_reason = ""
        if (
            shares_held > 0
            and 3 <= days_held <= 5
            and not getattr(item, "sell_half_done", False)
        ):
            partial_reason = (
                "Position is within 3-5 trading day partial-exit review window."
            )

        changed |= self._set_attr_if_changed(
            item, "partial_exit_review_alert", bool(partial_reason)
        )
        changed |= self._set_attr_if_changed(
            item, "partial_exit_review_reason", partial_reason
        )

        ema_reason = ""
        if shares_held > 0 and getattr(item, "sell_half_done", False):
            momentum_signal = self._momentum_exit_signal(item)
            if momentum_signal:
                ema_reason = f"Close below {momentum_signal} - review manual exit next market open."

        changed |= self._set_attr_if_changed(
            item, "ema_trailing_stop_alert", bool(ema_reason)
        )
        changed |= self._set_attr_if_changed(
            item, "ema_trailing_stop_reason", ema_reason
        )

        if partial_reason or ema_reason:
            changed |= self._set_attr_if_changed(
                item, "suggested_action", suggested_action
            )
        elif str(getattr(item, "suggested_action", "") or "") == suggested_action:
            changed |= self._set_attr_if_changed(item, "suggested_action", "")

        partial_notice_key = "_partial_exit_review_notice_reason"
        if partial_reason and getattr(item, partial_notice_key, "") != partial_reason:
            setattr(item, partial_notice_key, partial_reason)
            self.append_log(
                f"[Buylist/{env}] {item.symbol}: {partial_reason} {suggested_action}"
            )
        elif not partial_reason and getattr(item, partial_notice_key, ""):
            setattr(item, partial_notice_key, "")

        ema_notice_key = "_ema_trailing_stop_notice_reason"
        if ema_reason and getattr(item, ema_notice_key, "") != ema_reason:
            setattr(item, ema_notice_key, ema_reason)
            self.append_log(
                f"[Buylist/{env}] {item.symbol}: {ema_reason} {suggested_action}"
            )
        elif not ema_reason and getattr(item, ema_notice_key, ""):
            setattr(item, ema_notice_key, "")

        return changed

    def _buylist_days_held(self, item) -> int:
        buy_date = getattr(item, "buy_date", None)
        if not buy_date:
            return 0
        buy_day = self._buylist_market_session_date_from_value(buy_date)
        session_today = self._us_market_session_date()
        if buy_day is None or buy_day >= session_today:
            return 0
        if hasattr(self, "_nyse_holidays"):
            holidays: set = set()
            for year in range(buy_day.year, session_today.year + 1):
                holidays |= self._nyse_holidays(year)
            days = 0
            cursor = buy_day
            while cursor < session_today:
                if cursor.weekday() < 5 and cursor not in holidays:
                    days += 1
                cursor += dt.timedelta(days=1)
            return days
        return (session_today - buy_day).days

    @staticmethod
    def _us_market_session_date(now: Optional[dt.datetime] = None) -> dt.date:
        from src.ui.buylist.controller import BuylistController

        return BuylistController.market_session_date(now)

    @staticmethod
    def _buylist_market_session_date_from_value(value) -> Optional[dt.date]:
        from src.ui.buylist.controller import BuylistController

        return BuylistController.market_session_date_from_value(value)

    @staticmethod
    def _partial_exit_quantity(shares_held: int) -> int:
        from src.ui.buylist.controller import BuylistController

        return BuylistController.partial_exit_quantity(shares_held)

    @staticmethod
    def _momentum_exit_signal(item) -> str:
        from src.ui.buylist.controller import BuylistController

        return BuylistController.momentum_exit_signal(item)

    @staticmethod
    def _completed_daily_close_rows(
        daily_rows,
        now: Optional[dt.datetime] = None,
    ) -> List[Tuple[dt.date, float]]:
        from src.ui.buylist.controller import BuylistController

        return BuylistController.completed_daily_close_rows(daily_rows, now)

    def _buylist_refresh_item_data(self, item) -> None:
        """Fetch latest 30d daily closes and compute 10/20 EMA for a buylist item.

        Calls Yahoo Finance v8 chart API directly with a browser User-Agent — yfinance's
        internal requests get blocked by Yahoo when the default bot UA is used.
        Falls back to yfinance.Ticker.history() if the direct call fails.
        """
        import requests as _req

        symbol = item.symbol
        closes = None
        daily_rows = None

        # Primary: direct Yahoo Finance v8 chart API (browser UA bypasses Yahoo's bot block)
        try:
            session = getattr(self, "_yf_session", None)
            if session is None:
                session = _req.Session()
                session.headers["User-Agent"] = (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                )
                self._yf_session = session

            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
            r = session.get(
                url,
                params={"interval": "1d", "range": "30d", "events": "div,splits"},
                timeout=15,
            )
            r.raise_for_status()
            payload = r.json()
            result = payload["chart"]["result"][0]
            raw_closes = result["indicators"]["quote"][0]["close"]
            timestamps = result.get("timestamp", []) or []
            daily_rows = []
            for ts_value, close_value in zip(timestamps, raw_closes):
                if close_value is None:
                    continue
                session_date = (
                    dt.datetime.fromtimestamp(float(ts_value), dt.timezone.utc)
                    .astimezone(US_MARKET_ZONE)
                    .date()
                )
                daily_rows.append((session_date, float(close_value)))
            closes = (
                [close for _session_date, close in daily_rows]
                if daily_rows
                else [float(c) for c in raw_closes if c is not None]
            )
        except Exception as exc:
            self.append_log(
                f"[Buylist] Direct fetch failed for {symbol}: {exc} — trying yfinance fallback."
            )

        # Fallback: yfinance Ticker.history() with stderr suppressed
        if not closes:
            import io
            import sys

            import yfinance as yf

            _stderr = sys.stderr
            try:
                sys.stderr = io.StringIO()
                hist = yf.Ticker(symbol).history(period="30d", interval="1d")
                if not hist.empty:
                    daily_rows = []
                    for index_value, close_value in hist["Close"].dropna().items():
                        if hasattr(index_value, "to_pydatetime"):
                            timestamp = index_value.to_pydatetime()
                        else:
                            timestamp = index_value
                        if isinstance(timestamp, dt.datetime):
                            if timestamp.tzinfo is None:
                                timestamp = timestamp.replace(tzinfo=US_MARKET_ZONE)
                            session_date = timestamp.astimezone(US_MARKET_ZONE).date()
                        elif isinstance(timestamp, dt.date):
                            session_date = timestamp
                        else:
                            continue
                        daily_rows.append((session_date, float(close_value)))
                    closes = [close for _session_date, close in daily_rows]
            except Exception:
                pass
            finally:
                sys.stderr = _stderr

        if not closes:
            self.append_log(
                f"[Buylist] No price data for {symbol} — skipping this cycle."
            )
            return

        completed_rows = self._completed_daily_close_rows(daily_rows)
        if completed_rows:
            closes = [close for _session_date, close in completed_rows]

        latest_close = closes[-1]
        try:
            existing_live_price = float(
                self.latest_intraday_prices.get(symbol, 0.0) or 0.0
            )
        except (TypeError, ValueError):
            existing_live_price = 0.0
        if existing_live_price <= 0:
            self.latest_intraday_prices[symbol] = latest_close
        item._latest_daily_close = latest_close
        item._ema10 = self._compute_ema(closes, 10)
        item._ema20 = self._compute_ema(closes, 20)

    @staticmethod
    def _compute_ema(prices: list, period: int) -> float:
        """Compute exponential moving average."""
        from src.ui.buylist.controller import BuylistController

        return BuylistController.compute_ema(prices, period)

    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # Buylist Dashboard — KIS order submission
    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
