"""Buylist query, cancellation, and manual-action workflows."""

from typing import Any, Dict, List, Optional, Tuple

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import (QDialog, QDialogButtonBox, QLabel, QMessageBox,
                             QSlider, QSpinBox, QTableWidget, QVBoxLayout)

from src.api.kis_order import format_overseas_order_price
from src.core.order_state import (REGULAR_LIMIT_EXECUTION,
                                  RESERVED_MOO_EXECUTION, BrokerOrder,
                                  OrderIntent, OrderSide, OrderStatus)
from src.services.order_ledger import find_open_orders, load_order_ledger
from src.ui.workers import KisOrderCancelWorker, KisOrderQueryWorker

from .constants import (STOP_LOSS_REPRICE_MIN_DROP_PCT,
                        STOP_LOSS_SELL_LIMIT_DISCOUNT_PCT)


class BuylistActionsMixin:
    def _open_orders_for_buylist_item(self, item, env: str) -> List[BrokerOrder]:
        if item is None:
            return []
        self.order_ledger = load_order_ledger()
        account_no = self._order_lookup_account_no_for_item(item, env)
        if not account_no:
            return []
        matches = find_open_orders(
            self.order_ledger,
            environment=env,
            account_no=account_no,
            symbol=getattr(item, "symbol", ""),
        )
        kis_order_id = str(getattr(item, "kis_order_id", "") or "")
        if kis_order_id:
            exact = [
                order
                for order in matches
                if order.broker_order_id == kis_order_id
                or order.client_order_id == kis_order_id
            ]
            if exact:
                return exact
        return matches

    def _open_broker_orders_for_buylist_item(
        self,
        item,
        env: str,
        *,
        side: Optional[OrderSide] = None,
        intent: Optional[OrderIntent] = None,
    ) -> List[BrokerOrder]:
        if item is None:
            return []
        self.order_ledger = load_order_ledger()
        account_no = self._order_lookup_account_no_for_item(item, env)
        if not account_no:
            return []
        symbol = getattr(item, "symbol", "")
        matches = find_open_orders(
            self.order_ledger,
            environment=env,
            account_no=account_no,
            symbol=symbol,
            side=side,
            intent=intent,
        )
        return matches

    @staticmethod
    def _order_unfilled_quantity(order: BrokerOrder) -> int:
        try:
            return max(0, int(order.remaining_quantity or 0))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _order_filled_quantity(order: BrokerOrder) -> int:
        try:
            return max(0, int(order.filled_quantity or 0))
        except (TypeError, ValueError):
            return 0

    def _selected_open_broker_order(
        self, env: str
    ) -> Tuple[Optional[Any], Optional[BrokerOrder]]:
        item = self._buylist_selected_item(env)
        if item is None:
            return None, None
        orders = self._open_orders_for_buylist_item(item, env)
        return item, (orders[0] if orders else None)

    def _apply_broker_order_status_updates_to_buylist(
        self, updated_orders: List[BrokerOrder]
    ) -> None:
        manager = self.__dict__.get("execution_queue_manager")
        changed = False
        queue_changed = False

        for order in updated_orders:
            if order.status in {OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED}:
                continue
            try:
                item = self.buylist_manager.get(order.symbol, order.environment)
            except TypeError:
                item = self.buylist_manager.get(order.symbol)
            if item is None:
                continue

            if manager is None and self._is_execution_queue_buylist_item(item):
                manager = self._ensure_execution_queue_manager()

            if order.side == OrderSide.BUY:
                queue_item = (
                    self._execution_queue_item_for_buylist_item(item)
                    if manager is not None
                    else None
                )
                if (
                    manager is not None
                    and queue_item is not None
                    and self._is_execution_queue_buylist_item(item)
                ):
                    if order.status == OrderStatus.UNKNOWN_SUBMISSION_STATE:
                        manager.mark_order_submitted(
                            order.symbol,
                            order_id=order.broker_order_id or order.client_order_id,
                            order_status=OrderStatus.UNKNOWN_SUBMISSION_STATE.value,
                            environment=order.environment,
                        )
                    elif order.status in {
                        OrderStatus.ACCEPTED,
                        OrderStatus.WORKING,
                        OrderStatus.CANCEL_REQUESTED,
                    }:
                        manager.mark_order_submitted(
                            order.symbol,
                            order_id=order.broker_order_id or order.client_order_id,
                            order_status=order.status.value,
                            environment=order.environment,
                        )
                    elif order.status in {
                        OrderStatus.CANCELLED,
                        OrderStatus.REJECTED,
                        OrderStatus.EXPIRED,
                    }:
                        manager.mark_order_failed(
                            order.symbol,
                            order_status=order.status.value,
                            environment=order.environment,
                        )
                    queue_changed = True
                    queue_status = self._execution_queue_status_for_buylist_item(item)
                else:
                    queue_status = None

                if order.status == OrderStatus.UNKNOWN_SUBMISSION_STATE:
                    new_status = OrderStatus.UNKNOWN_SUBMISSION_STATE.value
                elif order.status in {
                    OrderStatus.CANCELLED,
                    OrderStatus.REJECTED,
                    OrderStatus.EXPIRED,
                }:
                    new_status = queue_status or "ACTIVE"
                elif order.status == OrderStatus.CANCEL_REQUESTED:
                    new_status = queue_status or "ORDER_SUBMITTED"
                else:
                    new_status = queue_status or "BUY_SUBMITTED"
            else:
                if order.status in {
                    OrderStatus.CANCELLED,
                    OrderStatus.REJECTED,
                    OrderStatus.EXPIRED,
                }:
                    new_status = (
                        "BOUGHT"
                        if int(getattr(item, "shares_held", 0) or 0) > 0
                        else "WATCHING"
                    )
                elif order.status == OrderStatus.CANCEL_REQUESTED:
                    new_status = "SELL_SUBMITTED"
                elif (
                    getattr(order, "execution_policy", REGULAR_LIMIT_EXECUTION)
                    == RESERVED_MOO_EXECUTION
                    and order.intent
                    in {
                        OrderIntent.PARTIAL_EXIT,
                        OrderIntent.PARTIAL_TAKE_PROFIT,
                    }
                ):
                    new_status = "PARTIAL_EXIT_RESERVED"
                elif (
                    getattr(order, "execution_policy", REGULAR_LIMIT_EXECUTION)
                    == RESERVED_MOO_EXECUTION
                ):
                    new_status = "SELL_RESERVED"
                elif order.intent in {
                    OrderIntent.PARTIAL_EXIT,
                    OrderIntent.PARTIAL_TAKE_PROFIT,
                }:
                    new_status = "PARTIAL_EXIT_SUBMITTED"
                else:
                    new_status = "SELL_SUBMITTED"

            if getattr(item, "monitoring_status", "") != new_status:
                item.monitoring_status = new_status
                item.status = new_status
                changed = True
            kis_order_id = order.broker_order_id or order.client_order_id
            if kis_order_id and getattr(item, "kis_order_id", "") != kis_order_id:
                item.kis_order_id = kis_order_id
                changed = True

        if queue_changed:
            self._save_execution_queue_state()
        if changed:
            self._save_buylist_state()

    def _on_broker_order_query_finished(
        self, updated_orders: List[BrokerOrder]
    ) -> None:
        self.order_ledger = load_order_ledger()
        self.apply_confirmed_order_fills_to_buylist(updated_orders)
        self._apply_broker_order_status_updates_to_buylist(updated_orders)
        self.populate_buylist_dashboard()
        if hasattr(self, "update_dashboard_summary"):
            self.update_dashboard_summary()

        if not updated_orders:
            self.append_log(
                "Broker order status check finished: no unresolved matching orders were updated."
            )
            return
        counts: Dict[str, int] = {}
        unknown_manual = 0
        for order in updated_orders:
            status = order.status.value
            counts[status] = counts.get(status, 0) + 1
            raw_status = order.raw_status_response or {}
            raw_response = (
                raw_status.get("raw_response", {})
                if isinstance(raw_status, dict)
                else {}
            )
            if order.status == OrderStatus.UNKNOWN_SUBMISSION_STATE or (
                isinstance(raw_response, dict) and raw_response.get("not_found")
            ):
                unknown_manual += 1
        summary = ", ".join(f"{key}={value}" for key, value in sorted(counts.items()))
        self.append_log(
            f"Broker order status check updated {len(updated_orders)} order(s): {summary}."
        )
        if unknown_manual:
            self.append_log(
                "Broker query did not find clear evidence for one or more unknown submissions; manual verification is still required."
            )

    def _buylist_check_order_status(self, env: str) -> None:
        worker = self.__dict__.get("broker_order_query_worker")
        if worker is not None and worker.isRunning():
            self.append_log("Broker order status check is already running.")
            return

        item = self._buylist_selected_item(env)
        if item is not None and not self._selected_order_account_for_item(item, env):
            self._warn_order_account_unavailable(item, env)
            return
        account_no = self._first_account_no_for_environment(env) or ""
        if not account_no:
            self.append_log(
                f"Broker order status check blocked for {env}: no KIS account is selected."
            )
            QMessageBox.warning(
                self,
                "KIS account required",
                "Select a configured KIS account before checking broker order status.",
            )
            return
        symbol = None
        broker_order_id = None
        client_order_id = None
        if item is not None:
            orders = self._open_orders_for_buylist_item(item, env)
            if not orders:
                QMessageBox.information(
                    self,
                    "No unresolved order",
                    f"No unresolved local broker order exists for {item.symbol}.",
                )
                return
            order = orders[0]
            account_no = order.account_no or account_no
            symbol = order.symbol
            broker_order_id = order.broker_order_id
            client_order_id = order.client_order_id

        self.broker_order_query_worker = KisOrderQueryWorker(
            environment=env,
            account_no=account_no or None,
            symbol=symbol,
            broker_order_id=broker_order_id,
            client_order_id=client_order_id,
        )
        self.broker_order_query_worker.finished_query.connect(
            self._on_broker_order_query_finished
        )
        self.broker_order_query_worker.error_occurred.connect(
            lambda message: self.append_log(
                f"Broker order status check failed: {message}"
            )
        )
        self._track_buylist_aux_worker(
            self.broker_order_query_worker, "broker_order_query_worker"
        )
        self.broker_order_query_worker.start()
        scope = symbol or "all unresolved orders"
        self.append_log(
            f"Checking broker order status for {env} {account_no or '<ledger accounts>'} {scope}."
        )

    @staticmethod
    def _cancel_allowed_for_order(order: BrokerOrder) -> bool:
        return order.status in {
            OrderStatus.SUBMITTING,
            OrderStatus.ACCEPTED,
            OrderStatus.WORKING,
            OrderStatus.PARTIALLY_FILLED,
        }

    def _format_cancel_order_confirmation(self, order: BrokerOrder) -> str:
        remaining = order.remaining_quantity or max(
            0, order.quantity_requested - order.filled_quantity
        )
        lines = [
            f"Environment: {order.environment}",
            f"Account: {order.account_no or '<unknown account>'}",
            f"Symbol: {order.symbol}",
            f"Broker order id: {order.broker_order_id}",
            f"Side: {order.side.value}",
            f"Quantity remaining: {remaining}",
        ]
        if order.execution_policy == RESERVED_MOO_EXECUTION:
            lines.extend(
                [
                    "Type: KIS market-on-open reservation",
                    "",
                    "Cancel before KIS forwards the reservation at the U.S. open.",
                    "Cancellation is final only after KIS confirms it.",
                ]
            )
        else:
            lines.extend(["", "This is a broker-side cancel request."])
        return "\n".join(lines)

    def _buylist_cancel_selected_order(self, env: str) -> None:
        worker = self.__dict__.get("broker_order_cancel_worker")
        if worker is not None and worker.isRunning():
            self.append_log("Broker order cancel is already running.")
            return

        item, order = self._selected_open_broker_order(env)
        if item is None:
            QMessageBox.warning(
                self,
                "No selection",
                "Select a buylist row with an unresolved broker order first.",
            )
            return
        if order is None:
            QMessageBox.warning(
                self,
                "No open order",
                f"No unresolved local broker order exists for {item.symbol}.",
            )
            return
        if not order.broker_order_id:
            QMessageBox.warning(
                self,
                "Cancel blocked",
                f"{order.symbol} cannot be cancelled from the app because the local order has no broker order id.",
            )
            return
        if not self._cancel_allowed_for_order(order):
            QMessageBox.warning(
                self,
                "Cancel blocked",
                f"{order.symbol} order is {order.status.value}; cancel is allowed only for known open broker orders.",
            )
            return
        # Simulated orders do not require a configured KIS account.  For a
        # real broker order, validate ownership only after basic local order
        # checks so the user receives the most actionable error (for example,
        # a missing broker id) and no account lookup masks it.
        if (
            str(getattr(order, "environment", env) or env).strip().upper() == "PROD"
            and not self._selected_order_account_for_item(item, env)
        ):
            self._warn_order_account_unavailable(item, env)
            return

        reply = QMessageBox.question(
            self,
            f"Cancel {order.environment} Order",
            self._format_cancel_order_confirmation(order)
            + "\n\nCancel this broker order?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        self.broker_order_cancel_worker = KisOrderCancelWorker(order.client_order_id)
        self.broker_order_cancel_worker.finished_cancel.connect(
            self._on_broker_order_cancel_finished
        )
        self.broker_order_cancel_worker.error_occurred.connect(
            lambda message, e=env: self._on_broker_order_cancel_error(e, message)
        )
        self._track_buylist_aux_worker(
            self.broker_order_cancel_worker, "broker_order_cancel_worker"
        )
        self.broker_order_cancel_worker.start()
        self.append_log(
            f"Cancel requested at broker for {order.environment} {order.symbol} order {order.broker_order_id}."
        )

    def _on_broker_order_cancel_finished(self, order: BrokerOrder) -> None:
        self.order_ledger = load_order_ledger()
        self.apply_confirmed_order_fills_to_buylist([order])
        self._apply_broker_order_status_updates_to_buylist([order])
        self.populate_buylist_dashboard()
        if hasattr(self, "update_dashboard_summary"):
            self.update_dashboard_summary()
        self.append_log(
            f"Broker cancel response for {order.symbol} {order.broker_order_id or order.client_order_id}: {order.status.value}."
        )
        if (
            order.execution_policy == RESERVED_MOO_EXECUTION
            and order.status == OrderStatus.CANCELLED
        ):
            QMessageBox.information(
                self,
                "Reservation cancelled",
                f"KIS confirmed cancellation of the market-on-open reservation for "
                f"{order.symbol}. No sell will be released from this reservation.",
            )

    def _on_broker_order_cancel_error(self, env: str, message: str) -> None:
        self.append_log(f"Broker order cancel failed: {message}")
        QMessageBox.warning(
            self,
            "Cancellation not confirmed",
            "KIS did not confirm the cancellation.\n\n"
            "Treat the order as still active until its broker status is checked. "
            f"Details: {message}",
        )
        QTimer.singleShot(
            1000,
            lambda e=env: self._buylist_check_order_status(e),
        )

    @staticmethod
    def _better_ready_orb_candidate(queue_item) -> Optional[Any]:
        if queue_item is None or getattr(queue_item, "manual_window_lock", False):
            return None
        current = getattr(queue_item, "selected_candidate", None)
        current_window = str(
            getattr(current, "window", "")
            or getattr(queue_item, "selected_window", "")
            or ""
        )
        try:
            current_score = float(getattr(current, "score", 0.0) or 0.0)
        except (TypeError, ValueError):
            current_score = 0.0
        candidates = list((getattr(queue_item, "candidates", {}) or {}).values())
        if not candidates:
            return None
        ready = [
            candidate
            for candidate in candidates
            if bool(getattr(candidate, "valid", False))
            and str(
                getattr(
                    getattr(candidate, "status", ""),
                    "value",
                    getattr(candidate, "status", ""),
                )
            ).upper()
            == "EXECUTE_READY"
        ]
        if not ready:
            return None
        best = max(
            ready, key=lambda candidate: float(getattr(candidate, "score", 0.0) or 0.0)
        )
        if str(getattr(best, "window", "") or "") == current_window:
            return None
        try:
            best_score = float(getattr(best, "score", 0.0) or 0.0)
        except (TypeError, ValueError):
            best_score = 0.0
        return best if best_score > current_score else None

    def _auto_replace_working_entry_queue_items(self, env: str) -> None:
        active_attr = f"_buylist_{env.lower()}_monitor_active"
        if not getattr(self, active_attr, False):
            return
        worker = self.__dict__.get("broker_order_cancel_worker")
        if worker is not None and worker.isRunning():
            return
        if not hasattr(self, "buylist_manager"):
            return

        manager = self.__dict__.get("execution_queue_manager")
        if manager is None:
            return

        for item in [it for it in self.buylist_manager.items if it.environment == env]:
            if str(getattr(item, "monitoring_status", "") or "").upper() not in {
                "ORDER_PENDING",
                "ORDER_SUBMITTED",
            }:
                continue
            if getattr(item, "_buy_replace_pending", False):
                continue
            queue_item = self._queue_item_for_buylist_item(item)
            replacement = self._better_ready_orb_candidate(queue_item)
            if replacement is None:
                continue
            orders = self._open_broker_orders_for_buylist_item(
                item,
                env,
                side=OrderSide.BUY,
                intent=OrderIntent.ENTRY,
            )
            order = next(
                (
                    candidate_order
                    for candidate_order in orders
                    if self._cancel_allowed_for_order(candidate_order)
                    and self._order_filled_quantity(candidate_order) == 0
                    and candidate_order.broker_order_id
                ),
                None,
            )
            if order is None:
                continue

            item._buy_replace_pending = True
            self.broker_order_cancel_worker = KisOrderCancelWorker(
                order.client_order_id
            )
            self.broker_order_cancel_worker.finished_cancel.connect(
                lambda updated, it=item, repl=replacement: self._on_entry_replacement_cancel_finished(
                    it, repl, updated
                )
            )
            self.broker_order_cancel_worker.error_occurred.connect(
                lambda message, it=item: self._on_entry_replacement_cancel_error(
                    it, message
                )
            )
            self._track_buylist_aux_worker(
                self.broker_order_cancel_worker, "broker_order_cancel_worker"
            )
            self.broker_order_cancel_worker.start()
            self.append_log(
                f"[Buylist/{env}] {item.symbol} found better ORB {replacement.window} "
                f"(score {float(getattr(replacement, 'score', 0.0) or 0.0):.1f}); "
                f"canceling working BUY before replacement."
            )
            return

    def _on_entry_replacement_cancel_error(self, item, message: str) -> None:
        item._buy_replace_pending = False
        self.append_log(
            f"[Buylist/{getattr(item, 'environment', 'PROD')}] Buy replacement cancel failed for {item.symbol}: {message}"
        )

    def _on_entry_replacement_cancel_finished(
        self, item, replacement, order: BrokerOrder
    ) -> None:
        item._buy_replace_pending = False
        self.order_ledger = load_order_ledger()
        self.apply_confirmed_order_fills_to_buylist([order])
        self._apply_broker_order_status_updates_to_buylist([order])

        if order.status not in {
            OrderStatus.CANCELLED,
            OrderStatus.REJECTED,
            OrderStatus.EXPIRED,
        }:
            self.populate_buylist_dashboard()
            self.append_log(
                f"[Buylist/{order.environment}] Replacement for {order.symbol} is waiting; "
                f"cancel response is {order.status.value}, so no new BUY was submitted."
            )
            return

        manager = self.__dict__.get("execution_queue_manager")
        queue_item = self._queue_item_for_buylist_item(item)
        if manager is None or queue_item is None:
            return
        queue_item.selected_candidate = replacement
        queue_item.selected_window = str(getattr(replacement, "window", "") or "")
        queue_item.locked = False
        queue_item.locked_reason = None
        queue_item.order_status = None
        queue_item.order_id = None
        manager.mark_order_submitted(
            item.symbol, order_status="PENDING", environment=order.environment
        )
        item.monitoring_status = (
            self._execution_queue_status_for_buylist_item(item) or "ORDER_PENDING"
        )
        item.status = item.monitoring_status
        replacement_shares = int(getattr(replacement, "shares", 0) or 0)
        item._buy_order_pending = True
        self._save_buylist_state()
        self._save_execution_queue_state()
        self.populate_buylist_dashboard()
        self.append_log(
            f"[Buylist/{order.environment}] Replacing BUY for {item.symbol} with {replacement.window} "
            f"ORB: {replacement_shares} shares @ ${float(getattr(replacement, 'entry_trigger', 0.0) or 0.0):.2f}."
        )
        self._submit_kis_buy_order(
            item,
            quantity=replacement_shares,
            order_price=float(getattr(replacement, "entry_trigger", 0.0) or 0.0),
        )

    @staticmethod
    def _stop_loss_sell_limit_price(current_price: float) -> float:
        try:
            price = float(current_price or 0.0)
        except (TypeError, ValueError):
            price = 0.0
        if price <= 0:
            return 0.01
        discounted = price * (1.0 - STOP_LOSS_SELL_LIMIT_DISCOUNT_PCT)
        return max(0.0001, float(format_overseas_order_price(discounted)))

    def _maybe_reprice_stop_loss_sell(
        self, item, env: str, current_price: float
    ) -> None:
        if getattr(item, "_stop_reprice_pending", False):
            return
        if self._buylist_auto_order_blocked(item):
            return
        try:
            stop_loss = float(getattr(item, "stop_loss", 0.0) or 0.0)
        except (TypeError, ValueError):
            stop_loss = 0.0
        if stop_loss <= 0 or current_price > stop_loss:
            return

        worker = self.__dict__.get("broker_order_cancel_worker")
        if worker is not None and worker.isRunning():
            return

        orders = self._open_broker_orders_for_buylist_item(
            item,
            env,
            side=OrderSide.SELL,
            intent=OrderIntent.STOP_LOSS,
        )
        order = next(
            (
                candidate_order
                for candidate_order in orders
                if self._cancel_allowed_for_order(candidate_order)
                and candidate_order.broker_order_id
            ),
            None,
        )
        if order is None:
            return

        new_limit = self._stop_loss_sell_limit_price(current_price)
        try:
            old_limit = float(order.limit_price or 0.0)
        except (TypeError, ValueError):
            old_limit = 0.0
        if old_limit > 0 and new_limit >= old_limit * (
            1.0 - STOP_LOSS_REPRICE_MIN_DROP_PCT
        ):
            return

        item._stop_reprice_pending = True
        self.broker_order_cancel_worker = KisOrderCancelWorker(order.client_order_id)
        self.broker_order_cancel_worker.finished_cancel.connect(
            lambda updated, it=item, px=new_limit: self._on_stop_reprice_cancel_finished(
                it, px, updated
            )
        )
        self.broker_order_cancel_worker.error_occurred.connect(
            lambda message, it=item: self._on_stop_reprice_cancel_error(it, message)
        )
        self._track_buylist_aux_worker(
            self.broker_order_cancel_worker, "broker_order_cancel_worker"
        )
        self.broker_order_cancel_worker.start()
        self.append_log(
            f"[Buylist/{env}] Stop-loss SELL for {item.symbol} is stale "
            f"(old limit ${old_limit:.2f}, current ${current_price:.2f}); canceling to resubmit lower."
        )

    def _on_stop_reprice_cancel_error(self, item, message: str) -> None:
        item._stop_reprice_pending = False
        self.append_log(
            f"[Buylist/{getattr(item, 'environment', 'PROD')}] Stop-loss reprice cancel failed for {item.symbol}: {message}"
        )

    def _on_stop_reprice_cancel_finished(
        self, item, new_limit: float, order: BrokerOrder
    ) -> None:
        item._stop_reprice_pending = False
        self.order_ledger = load_order_ledger()
        self.apply_confirmed_order_fills_to_buylist([order])
        self._apply_broker_order_status_updates_to_buylist([order])

        if order.status not in {
            OrderStatus.CANCELLED,
            OrderStatus.REJECTED,
            OrderStatus.EXPIRED,
        }:
            self.populate_buylist_dashboard()
            self.append_log(
                f"[Buylist/{order.environment}] Stop-loss reprice for {order.symbol} is waiting; "
                f"cancel response is {order.status.value}, so no replacement SELL was submitted."
            )
            return

        try:
            quantity = int(getattr(item, "shares_held", 0) or 0)
        except (TypeError, ValueError):
            quantity = 0
        if quantity <= 0:
            self.populate_buylist_dashboard()
            return
        item.monitoring_status = "BOUGHT"
        item.status = "BOUGHT"
        item._stop_order_pending = True
        self._save_buylist_state()
        self.populate_buylist_dashboard()
        self.append_log(
            f"[Buylist/{order.environment}] Resubmitting stop-loss SELL for {item.symbol}: "
            f"{quantity} shares @ aggressive limit ${new_limit:.2f}."
        )
        self._submit_kis_sell_order(
            item, quantity, reason="stop-loss reprice", order_price=new_limit
        )

    def _request_eod_entry_buy_cancellation(self, item, env: str) -> bool:
        orders = self._open_broker_orders_for_buylist_item(
            item,
            env,
            side=OrderSide.BUY,
            intent=OrderIntent.ENTRY,
        )
        requested = False
        for order in orders:
            if order.status == OrderStatus.UNKNOWN_SUBMISSION_STATE:
                self.append_log(
                    f"[Buylist/{env}] EOD cancel skipped for {order.symbol}: submission state is unknown; reconcile first."
                )
                continue
            if not order.broker_order_id:
                self.append_log(
                    f"[Buylist/{env}] EOD cancel skipped for {order.symbol}: local order has no broker order id."
                )
                continue
            if not self._cancel_allowed_for_order(order):
                continue
            if not self.request_cancel_order(order.client_order_id):
                continue
            requested = True
        return requested

    def _buylist_selected_item(self, env: str):
        """Return the BuylistItem for the selected row in the given env table, or None."""
        table_attr = f"buylist_{env.lower()}_table"
        if not hasattr(self, table_attr):
            return None
        table: QTableWidget = getattr(self, table_attr)
        if not table.selectedItems():
            return None
        row = table.currentRow()
        sym_cell = table.item(row, 0)
        if not sym_cell:
            return None
        return self.buylist_manager.get(sym_cell.text().strip().upper(), env)

    def _buylist_activate_selected(self, env: str) -> None:
        """Activate the selected buylist item for entry monitoring."""
        item = self._buylist_selected_item(env)
        if not item:
            QMessageBox.warning(
                self, "No selection", "Select a buylist row to activate."
            )
            return
        if item.monitoring_status == "BOUGHT":
            QMessageBox.information(
                self,
                "Already bought",
                f"{item.symbol} is already in a BOUGHT position.",
            )
            return
        if not self._is_execution_queue_buylist_item(item):
            message = (
                f"{item.symbol} uses the retired legacy entry workflow. "
                "No BUY order was submitted. Add or refresh this symbol as an "
                "execution-queue strategy before activating entry monitoring."
            )
            self.append_log(f"[Buylist/{env}] {message}")
            QMessageBox.warning(self, "Legacy entry retired", message)
            return
        if self._is_execution_queue_buylist_item(item):
            account_no = self._selected_order_account_for_item(item, env)
            if not account_no:
                self._warn_order_account_unavailable(item, env)
                return
            item.kis_account_no = account_no
            item.orb_monitor_enabled = True
            self._save_state()
            active_attr = f"_buylist_{env.lower()}_monitor_active"
            was_running = getattr(self, active_attr, False)
            if not was_running:
                self._toggle_buylist_monitor(env)
            self.populate_buylist_dashboard()
            status = str(getattr(item, "monitoring_status", "") or "")
            started_note = (
                "Monitor started." if not was_running else "Monitor already running."
            )
            if status == "EXECUTE_READY":
                msg = (
                    f"{item.symbol} activated — EXECUTE_READY.\n\n"
                    f"{started_note} Will auto-submit the BUY on the next 60-second cycle.\n\n"
                    "Use 'Submit Buy' to submit immediately."
                )
            else:
                msg = (
                    f"{item.symbol} activated (status: {status}).\n\n"
                    f"{started_note} Checking every 60 seconds — will auto-submit the BUY "
                    "when price crosses the ORB entry trigger.\n\n"
                    "Use 'Review Order' to see the planned entry/stop/shares."
                )
            QMessageBox.information(self, "Activated", msg)
            return

    def _buylist_deactivate_selected(self, env: str) -> None:
        item = self._buylist_selected_item(env)
        if not item:
            QMessageBox.warning(
                self, "No selection", "Select a buylist row to deactivate."
            )
            return
        if (
            item.monitoring_status != "BOUGHT"
            and self._is_execution_queue_buylist_item(item)
        ):
            item.orb_monitor_enabled = False
            self._save_state()
            self.populate_buylist_dashboard()
            self.append_log(
                f"[Buylist/{env}] {item.symbol} deactivated — monitor will no longer auto-buy this item."
            )
            QMessageBox.information(
                self,
                "Deactivated",
                f"{item.symbol} monitoring disabled. Monitor column will show OFF.\n\n"
                "Click Activate again to re-enable auto-buy for this item.",
            )
            return
        if item.monitoring_status == "BOUGHT":
            reply = QMessageBox.question(
                self,
                "Reset position?",
                f"{item.symbol} is BOUGHT ({item.shares_held} shares @ ${item.avg_cost:.2f}).\n\n"
                f"Reset to WATCHING — no sell order is placed.\n"
                "Only use this to discard an incorrect local position entry.",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return
            item.shares_held = 0
            item.avg_cost = 0.0
            item.buy_date = None
            item.sell_half_done = False
            item.kis_order_id = ""
            item.position_percent = 0.0
            item.monitoring_status = "WATCHING"
            self._clear_buylist_auto_order_block(item)
            self._save_state()
            self.populate_buylist_dashboard()
            self.append_log(
                f"[Buylist/{env}] {item.symbol} position reset to WATCHING (no KIS order placed)."
            )
            return
        item.monitoring_status = "WATCHING"
        self._clear_buylist_auto_order_block(item)
        self._save_state()
        self.populate_buylist_dashboard()
        self.append_log(f"[Buylist/{env}] {item.symbol} deactivated.")

    def _buylist_sell_half_selected(self, env: str) -> None:
        item = self._buylist_selected_item(env)
        if not item:
            QMessageBox.warning(self, "No selection", "Select a buylist row to sell.")
            return
        if item.monitoring_status != "BOUGHT" or item.shares_held <= 0:
            QMessageBox.warning(
                self, "No position", f"{item.symbol} has no open position."
            )
            return
        if self._warn_if_open_sell_order(item, env):
            return
        qty_third = max(1, item.shares_held // 3)
        qty_half = max(1, item.shares_held // 2)

        dialog = QDialog(self)
        dialog.setWindowTitle("Partial Sell — Day Rule")
        layout = QVBoxLayout()

        info_label = QLabel(
            f"Sell partial position in {item.symbol}  ({item.shares_held} shares held).\n"
            f"Choose any amount between 1/3 ({qty_third} shares) and 1/2 ({qty_half} shares):\n\n"
            "Before the U.S. regular session, PROD uses a broker-held "
            "market-on-open reservation. It prioritizes selling over price protection."
        )
        info_label.setWordWrap(True)
        layout.addWidget(info_label)

        slider = QSlider(Qt.Horizontal)
        slider.setMinimum(qty_third)
        slider.setMaximum(qty_half)
        slider.setValue(qty_third)
        slider.setEnabled(qty_half > qty_third)
        layout.addWidget(slider)

        spin = QSpinBox()
        spin.setMinimum(qty_third)
        spin.setMaximum(qty_half)
        spin.setValue(qty_third)
        layout.addWidget(spin)

        pct_label = QLabel(f"{qty_third / item.shares_held:.1%} of position")
        layout.addWidget(pct_label)

        slider.valueChanged.connect(spin.setValue)
        spin.valueChanged.connect(slider.setValue)

        def _on_value_changed(value: int) -> None:
            pct_label.setText(f"{value / item.shares_held:.1%} of position")

        spin.valueChanged.connect(_on_value_changed)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)

        dialog.setLayout(layout)
        if dialog.exec_() == QDialog.Accepted:
            self._submit_kis_sell_order(item, spin.value(), reason="partial sell")

    def _buylist_sell_all_selected(self, env: str) -> None:
        item = self._buylist_selected_item(env)
        if not item:
            QMessageBox.warning(self, "No selection", "Select a buylist row to sell.")
            return
        if item.monitoring_status != "BOUGHT" or item.shares_held <= 0:
            QMessageBox.warning(
                self, "No position", f"{item.symbol} has no open position."
            )
            return
        if self._warn_if_open_sell_order(item, env):
            return
        execution_policy = self._manual_sell_execution_policy(env)
        order_description = (
            "This will create a KIS market-on-open reservation. The broker will "
            "release it at the next regular open; there is no limit-price protection."
            if execution_policy == RESERVED_MOO_EXECUTION
            else "This will submit a limit sell order using current/live fallback price."
        )
        reply = QMessageBox.question(
            self,
            "Confirm Sell All",
            f"Sell all {item.shares_held} shares of {item.symbol}?\n"
            f"{order_description}",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            self._submit_kis_sell_order(
                item, item.shares_held, reason="manual sell all"
            )

    def _buylist_remove_selected(self, env: str) -> None:
        item = self._buylist_selected_item(env)
        if not item:
            QMessageBox.warning(self, "No selection", "Select a buylist row to remove.")
            return
        if item.monitoring_status in ("ACTIVE", "BOUGHT"):
            reply = QMessageBox.question(
                self,
                "Confirm Remove",
                f"{item.symbol} is currently {item.monitoring_status}. Remove anyway?",
                QMessageBox.Yes | QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return
        self.buylist_manager.remove(item.symbol, env)
        self._save_state()
        self.populate_buylist_dashboard()
        self.append_log(f"[Buylist/{env}] {item.symbol} removed from buylist.")

    def _buylist_move_to_breakeven_selected(self, env: str) -> None:
        item = self._buylist_selected_item(env)
        if not item:
            QMessageBox.warning(self, "No selection", "Select a buylist row first.")
            return
        if item.monitoring_status != "BOUGHT" or item.shares_held <= 0:
            QMessageBox.warning(
                self, "No position", f"{item.symbol} has no open bought position."
            )
            return
        breakeven = item.avg_cost if item.avg_cost > 0 else item.entry_price
        if breakeven <= 0:
            QMessageBox.warning(
                self, "No price", f"No avg cost or entry price set for {item.symbol}."
            )
            return
        if item.stop_loss >= breakeven:
            QMessageBox.information(
                self,
                "Already at Breakeven",
                f"{item.symbol} stop (${item.stop_loss:.2f}) is already at or above breakeven (${breakeven:.2f}).",
            )
            return
        reply = QMessageBox.question(
            self,
            "Move Stop to Breakeven",
            f"Move {item.symbol} stop loss from ${item.stop_loss:.2f} to breakeven ${breakeven:.2f}?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        old_stop = item.stop_loss
        item.stop_loss = breakeven
        self._save_state()
        self.populate_buylist_dashboard()
        self.append_log(
            f"[Buylist/{env}] {item.symbol} stop manually moved to breakeven "
            f"${breakeven:.2f} (was ${old_stop:.2f})."
        )

    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # Buylist Dashboard — monitor timer
    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
