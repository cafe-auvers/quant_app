"""Buylist order submission and fill application."""

import datetime as dt
import math
from typing import Any, Dict, List, Optional, Tuple

from PyQt5.QtCore import QThread, QTimer
from PyQt5.QtWidgets import QMessageBox

from src.api.kis_order import is_ambiguous_order_submission_error
from src.core.order_state import (OPEN_ORDER_STATUSES, REGULAR_LIMIT_EXECUTION,
                                  RESERVED_MOO_EXECUTION, BrokerOrder,
                                  OrderIntent, OrderSide, OrderStatus)
from src.risk.pre_trade import (PreTradeRiskDecision,
                                assess_orb_entry_candidate,
                                orb_candidate_plan_id)
from src.services.order_ledger import (append_order, find_open_orders,
                                       has_open_order, load_order_ledger,
                                       merge_orders, update_order)
from src.ui.workers import KisOrderWorker, OrderReconciliationWorker

from .constants import (KST_ZONE, US_MARKET_CLOSE_TIME, US_MARKET_OPEN_TIME,
                        US_MARKET_ZONE)


class BuylistOrdersMixin:
    def _selected_trade_account_no_for_environment(
        self, environment: str
    ) -> Optional[str]:
        """Return the explicitly selected trade account for an environment."""
        combo = self.__dict__.get("trade_kis_account_combo")
        if combo is None:
            return None
        try:
            profile = combo.currentData()
        except RuntimeError:
            return None
        if not isinstance(profile, dict):
            return None
        selected_environment = str(
            profile.get("environment") or environment or ""
        ).strip().upper()
        if selected_environment != str(environment or "").strip().upper():
            return None
        account_no = str(profile.get("account_no") or "").strip()
        return account_no or None

    def _first_account_no_for_environment(self, environment: str) -> Optional[str]:
        """Resolve an account, preferring the account actively selected by the user.

        The legacy discovery fallback is retained only for non-UI callers.  When
        the trade account combo exists, a missing/mismatched selection must not
        silently route an order to the first configured account.
        """
        combo = self.__dict__.get("trade_kis_account_combo")
        if combo is not None:
            return self._selected_trade_account_no_for_environment(environment)
        try:
            from src.api.kis_account_snapshot_dual import \
                discover_account_profiles

            profiles = [
                p
                for p in discover_account_profiles()
                if p.get("environment") == environment
            ]
            if not profiles:
                return None
            return profiles[0].get("account_no") or None
        except Exception as exc:
            self.append_log(f"KIS account discovery failed for {environment}: {exc}")
            return None

    def _selected_order_account_for_item(self, item, environment: str) -> Optional[str]:
        """Fail closed when a position belongs to another KIS account."""
        account_no = self._first_account_no_for_environment(environment)
        if not account_no:
            return None
        assigned_account = str(getattr(item, "kis_account_no", "") or "").strip()
        if assigned_account and assigned_account != account_no:
            return None
        return account_no

    def _order_lookup_account_no_for_item(self, item, environment: str) -> str:
        """Use a position's owner for automated order lifecycle work.

        User-initiated submissions still go through
        ``_selected_order_account_for_item``.  This lookup is intentionally
        separate so an account-combo change cannot make the EOD/reprice monitor
        lose sight of an already submitted order in its owning account.
        """
        assigned_account = str(getattr(item, "kis_account_no", "") or "").strip()
        if assigned_account:
            return assigned_account
        return self._first_account_no_for_environment(environment) or ""

    def _warn_order_account_unavailable(self, item, environment: str) -> None:
        assigned_account = str(getattr(item, "kis_account_no", "") or "").strip()
        selected_account = self._first_account_no_for_environment(environment)
        if assigned_account and selected_account and assigned_account != selected_account:
            message = (
                f"{getattr(item, 'symbol', 'This position')} belongs to KIS account "
                f"{assigned_account}, but account {selected_account} is selected. "
                "Switch back to the owning account before submitting an order."
            )
        else:
            message = (
                "Select a configured KIS account for this environment before "
                "submitting an order."
            )
        self.append_log(f"KIS order blocked: {message}")
        QMessageBox.warning(self, "KIS account required", message)

    @staticmethod
    def _sell_intent_for_reason(reason: str) -> OrderIntent:
        reason_text = (reason or "").lower()
        if "stop" in reason_text:
            return OrderIntent.STOP_LOSS
        if "partial" in reason_text or "half" in reason_text:
            return OrderIntent.PARTIAL_EXIT
        if "momentum" in reason_text or "ema" in reason_text:
            return OrderIntent.MOMENTUM_EXIT
        if "manual" in reason_text or "all" in reason_text or "exit" in reason_text:
            return OrderIntent.MANUAL_EXIT
        return OrderIntent.UNKNOWN

    def _has_duplicate_open_order(
        self,
        environment: str,
        account_no: str,
        symbol: str,
        side: OrderSide,
        intent: OrderIntent,
    ) -> bool:
        self.order_ledger = load_order_ledger()
        return has_open_order(
            environment=environment,
            account_no=account_no or "",
            symbol=symbol,
            side=side,
            intent=intent,
        )

    def _has_open_sell_order(
        self, environment: str, account_no: str, symbol: str
    ) -> bool:
        self.order_ledger = load_order_ledger()
        return has_open_order(
            environment=environment,
            account_no=account_no or "",
            symbol=symbol,
            side=OrderSide.SELL,
        )

    def _warn_if_open_sell_order(self, item, env: str) -> bool:
        account_no = self._selected_order_account_for_item(item, env) or ""
        if not account_no:
            self._warn_order_account_unavailable(item, env)
            return True
        if not self._has_open_sell_order(env, account_no, item.symbol):
            return False
        QMessageBox.warning(
            self,
            "Open sell order",
            f"An open SELL order already exists for {item.symbol}. "
            "Reconcile or cancel it before submitting another sell order.",
        )
        return True

    def _save_buylist_state(self) -> None:
        save_state = getattr(self, "_save_state", None)
        if callable(save_state):
            save_state()
            self._save_execution_queue_state()
            return

        manager_save = getattr(getattr(self, "buylist_manager", None), "save", None)
        if callable(manager_save):
            manager_save()
        self._save_execution_queue_state()

    def _buylist_order_price(self, item, *fallbacks) -> float:
        live_price = getattr(item, "current_price", None)
        if not live_price:
            live_price = getattr(self, "latest_intraday_prices", {}).get(
                getattr(item, "symbol", ""), 0.0
            )
        for value in (
            live_price,
            *fallbacks,
            getattr(item, "stop_loss", 0.0),
            getattr(item, "avg_cost", 0.0),
            getattr(item, "entry_price", 0.0),
        ):
            try:
                price = float(value or 0.0)
            except (TypeError, ValueError, OverflowError):
                continue
            if math.isfinite(price) and price > 0:
                return max(0.01, price)
        return 0.01

    @staticmethod
    def _buylist_order_environment(item) -> str:
        return str(
            getattr(item, "environment", None)
            or getattr(item, "market", None)
            or "REAL"
        ).upper()

    def _buylist_order_quantity(
        self, item, order_price: float, quantity: Optional[int] = None
    ) -> int:
        try:
            qty = int(quantity or 0)
        except (TypeError, ValueError, OverflowError):
            qty = 0
        if qty > 0:
            return qty

        account_size = (
            self._get_account_balance_for_env(self._buylist_order_environment(item))
            if hasattr(self, "_get_account_balance_for_env")
            else 0.0
        )
        position_percent = float(getattr(item, "position_percent", 0.0) or 0.0)
        if account_size > 0 and position_percent > 0 and order_price > 0:
            return max(1, int((account_size * position_percent / 100.0) // order_price))
        return max(1, int(getattr(item, "shares_held", 0) or 1))

    @staticmethod
    def _coerce_positive_order_quantity(value: Any) -> Optional[int]:
        """Accept only whole, finite, positive share quantities."""
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if not math.isfinite(number) or number <= 0 or not number.is_integer():
            return None
        try:
            quantity = int(number)
        except (ValueError, OverflowError):
            return None
        return quantity if quantity > 0 else None

    def _warn_invalid_order_value(self, item, message: str) -> None:
        self.append_log(f"KIS order blocked for {getattr(item, 'symbol', '')}: {message}")
        QMessageBox.warning(self, "Invalid order", message)

    @staticmethod
    def _us_regular_market_is_open(
        now: Optional[dt.datetime] = None,
    ) -> bool:
        market_now = now or dt.datetime.now(US_MARKET_ZONE)
        if market_now.tzinfo is None:
            market_now = market_now.replace(tzinfo=KST_ZONE).astimezone(
                US_MARKET_ZONE
            )
        else:
            market_now = market_now.astimezone(US_MARKET_ZONE)
        return (
            market_now.weekday() < 5
            and US_MARKET_OPEN_TIME <= market_now.time() < US_MARKET_CLOSE_TIME
        )

    def _manual_sell_execution_policy(
        self,
        env: str,
        now: Optional[dt.datetime] = None,
    ) -> str:
        if str(env or "").upper() == "PROD" and not self._us_regular_market_is_open(
            now
        ):
            return RESERVED_MOO_EXECUTION
        return REGULAR_LIMIT_EXECUTION

    def _submit_kis_buy_order(
        self,
        item,
        quantity: Optional[int] = None,
        limit_price: Optional[float] = None,
        order_price: Optional[float] = None,
        pre_trade_risk_decision: Optional[PreTradeRiskDecision] = None,
    ) -> None:
        """Submit a KIS buy order without treating broker acceptance as a fill."""
        submission_guard = getattr(
            self, "_state_sync_allows_order_submission", None
        )
        if callable(submission_guard) and not submission_guard():
            item._buy_order_pending = False
            return
        env = self._buylist_order_environment(item)
        account_no = self._selected_order_account_for_item(item, env)
        if not account_no:
            item._buy_order_pending = False
            manager = self.__dict__.get("execution_queue_manager")
            if manager is not None:
                manager.mark_order_failed(
                    item.symbol, order_status="NO_ACCOUNT", environment=env
                )
                queue_status = self._execution_queue_status_for_buylist_item(item)
                if queue_status:
                    item.monitoring_status = queue_status
                    item.status = queue_status
                self._save_execution_queue_state()
            self._save_buylist_state()
            self.populate_buylist_dashboard()
            self._warn_order_account_unavailable(item, env)
            return
        intent = OrderIntent.ENTRY
        risk_candidate = None
        if self._is_pre_entry_execution_queue_buylist_item(item):
            manager = self.__dict__.get("execution_queue_manager")
            if manager is None:
                manager = self._ensure_execution_queue_manager()
            queue_item = (
                self._execution_queue_item_for_buylist_item(item)
                if manager is not None
                else None
            )
            candidate = (
                getattr(queue_item, "selected_candidate", None)
                if queue_item is not None
                else None
            )
            queue_status = (
                self._execution_queue_value(getattr(queue_item, "status", ""))
                if queue_item is not None
                else ""
            )
            if candidate is not None:
                risk_candidate = candidate
            if candidate is not None and queue_status == "EXECUTE_READY":
                if quantity is None:
                    quantity = int(getattr(candidate, "shares", 0) or 0)
                if order_price is None and limit_price is None:
                    order_price = float(getattr(candidate, "entry_trigger", 0.0) or 0.0)
        if self._has_duplicate_open_order(
            env, account_no, item.symbol, OrderSide.BUY, intent
        ):
            item._buy_order_pending = False
            manager = self.__dict__.get("execution_queue_manager")
            if manager is None and self._is_execution_queue_buylist_item(item):
                manager = self._ensure_execution_queue_manager()
            if manager is not None:
                manager.mark_order_failed(
                    item.symbol, order_status="DUPLICATE", environment=env
                )
                queue_item = self._execution_queue_item_for_buylist_item(item)
                if queue_item is not None:
                    item.monitoring_status = self._execution_queue_value(
                        queue_item.status
                    )
                    item.status = item.monitoring_status
                self._save_execution_queue_state()
            self.append_log(
                f"Open BUY ENTRY order already exists for {item.symbol} in {env} account {account_no}. "
                "Reconcile or cancel it before submitting another order."
            )
            return

        if quantity is not None:
            quantity = self._coerce_positive_order_quantity(quantity)
            if quantity is None:
                self._warn_invalid_order_value(
                    item, "Share quantity must be a positive whole finite number."
                )
                return

        explicit_price = None
        invalid_explicit_price = False
        for value in (order_price, limit_price):
            if value is None:
                continue
            try:
                price = float(value or 0.0)
            except (TypeError, ValueError, OverflowError):
                invalid_explicit_price = True
                continue
            if math.isfinite(price) and price > 0:
                explicit_price = price
                break
            invalid_explicit_price = True
        if invalid_explicit_price:
            self._warn_invalid_order_value(
                item, "Limit price must be a positive finite number."
            )
            return
        order_price = (
            max(0.01, explicit_price)
            if explicit_price is not None
            else self._buylist_order_price(item)
        )
        quantity = self._buylist_order_quantity(item, order_price, quantity)
        if quantity <= 0 or not math.isfinite(float(order_price)) or order_price <= 0:
            self._warn_invalid_order_value(
                item, "Order price and share quantity must be positive finite values."
            )
            return
        if pre_trade_risk_decision is None:
            plan_id = orb_candidate_plan_id(risk_candidate)
            pre_trade_risk_decision = assess_orb_entry_candidate(
                risk_candidate,
                environment=env,
                account_no=account_no,
                symbol=item.symbol,
                quantity=quantity,
                reference_price=order_price,
                plan_id=plan_id,
            )
        else:
            plan_id = pre_trade_risk_decision.plan_id
        strategy_id = pre_trade_risk_decision.strategy_id
        try:
            worker = KisOrderWorker(
                env,
                item.symbol,
                quantity,
                order_price,
                "buy",
                account_no=account_no,
                intent=intent,
                buylist_symbol_key=f"{env}:{item.symbol}",
                pre_trade_risk_decision=pre_trade_risk_decision,
                strategy_id=strategy_id,
                plan_id=plan_id,
            )
            self.kis_order_worker = worker
            self._track_buylist_order_worker(worker)
            worker.finished_order.connect(
                lambda order, it=item: self._on_buy_order_accepted(it, order)
            )
            worker.error_occurred.connect(
                lambda error, it=item: self._on_order_error(it.symbol, "buy", error, it)
            )
            worker.start()
            self.append_log(
                f"BUY submitted for {item.symbol}: {quantity} shares @ limit ${order_price:.2f}"
            )
        except Exception as exc:
            item._buy_order_pending = False
            manager = self.__dict__.get("execution_queue_manager")
            if manager is None and self._is_execution_queue_buylist_item(item):
                manager = self._ensure_execution_queue_manager()
            if manager is not None:
                manager.mark_order_failed(
                    item.symbol, order_status="ERROR", environment=env
                )
                queue_item = self._execution_queue_item_for_buylist_item(item)
                if queue_item is not None:
                    item.monitoring_status = self._execution_queue_value(
                        queue_item.status
                    )
                    item.status = item.monitoring_status
                self._save_execution_queue_state()
            if manager is None:
                item.monitoring_status = "ERROR"
            self._save_buylist_state()
            self.populate_buylist_dashboard()
            QMessageBox.warning(self, "KIS order failed", str(exc))

    def _submit_kis_sell_order(
        self,
        item,
        quantity: int,
        reason: str,
        order_price: Optional[float] = None,
        limit_price: Optional[float] = None,
    ) -> None:
        """Submit a KIS sell order without reducing local position until fill confirmation."""
        submission_guard = getattr(
            self, "_state_sync_allows_order_submission", None
        )
        if callable(submission_guard) and not submission_guard():
            item._stop_order_pending = False
            item._exit_order_pending = False
            return
        env = self._buylist_order_environment(item)
        account_no = self._selected_order_account_for_item(item, env)
        if not account_no:
            item._stop_order_pending = False
            item._exit_order_pending = False
            self._save_buylist_state()
            self.populate_buylist_dashboard()
            self._warn_order_account_unavailable(item, env)
            return
        quantity = self._coerce_positive_order_quantity(quantity)
        if quantity is None:
            self._warn_invalid_order_value(
                item, "Share quantity must be a positive whole finite number."
            )
            return
        intent = self._sell_intent_for_reason(reason)
        manual_exit = intent in {
            OrderIntent.PARTIAL_EXIT,
            OrderIntent.PARTIAL_TAKE_PROFIT,
            OrderIntent.MANUAL_EXIT,
        }
        execution_policy = (
            self._manual_sell_execution_policy(env)
            if manual_exit
            else REGULAR_LIMIT_EXECUTION
        )
        if self._has_open_sell_order(env, account_no, item.symbol):
            item._stop_order_pending = False
            item._exit_order_pending = False
            self.append_log(
                f"Open SELL order already exists for {item.symbol} in {env} account {account_no}. "
                "Reconcile or cancel it before submitting another order."
            )
            return

        explicit_price = None
        invalid_explicit_price = False
        for value in (order_price, limit_price):
            if value is None:
                continue
            try:
                price = float(value or 0.0)
            except (TypeError, ValueError, OverflowError):
                invalid_explicit_price = True
                continue
            if math.isfinite(price) and price > 0:
                explicit_price = price
                break
            invalid_explicit_price = True
        if invalid_explicit_price:
            self._warn_invalid_order_value(
                item, "Limit price must be a positive finite number."
            )
            return
        if execution_policy == RESERVED_MOO_EXECUTION:
            order_price = 0.0
        else:
            order_price = (
                max(0.01, explicit_price)
                if explicit_price is not None
                else self._buylist_order_price(item)
            )
        if (
            execution_policy != RESERVED_MOO_EXECUTION
            and (not math.isfinite(float(order_price)) or order_price <= 0)
        ):
            self._warn_invalid_order_value(
                item, "Order price must be a positive finite number."
            )
            return
        try:
            worker_kwargs = {
                "account_no": account_no,
                "intent": intent,
                "buylist_symbol_key": f"{env}:{item.symbol}",
            }
            if execution_policy == RESERVED_MOO_EXECUTION:
                worker_kwargs["execution_policy"] = execution_policy
            worker = KisOrderWorker(
                env,
                item.symbol,
                quantity,
                order_price,
                "sell",
                **worker_kwargs,
            )
            self.kis_order_worker = worker
            self._track_buylist_order_worker(worker)
            worker.finished_order.connect(
                lambda order, it=item, rsn=reason: self._on_sell_order_accepted(
                    it, quantity, rsn, order
                )
            )
            worker.error_occurred.connect(
                lambda error, it=item: self._on_order_error(
                    it.symbol, "sell", error, it
                )
            )
            worker.start()
            if execution_policy == RESERVED_MOO_EXECUTION:
                self.append_log(
                    f"SELL reservation submitted for {item.symbol}: {quantity} shares "
                    f"@ market-on-open ({reason})"
                )
            else:
                self.append_log(
                    f"SELL submitted for {item.symbol}: {quantity} shares @ limit "
                    f"${order_price:.2f} ({reason})"
                )
        except Exception as exc:
            item._stop_order_pending = False
            item._exit_order_pending = False
            item.monitoring_status = "ERROR"
            self._save_buylist_state()
            self.populate_buylist_dashboard()
            QMessageBox.warning(self, "KIS order failed", str(exc))

    def _record_broker_order(self, order: BrokerOrder) -> None:
        append_order(order)
        self.order_ledger = load_order_ledger()

    def _on_buy_order_accepted(self, item, order: BrokerOrder) -> None:
        self._record_broker_order(order)
        if order.account_no:
            item.kis_account_no = str(order.account_no)
        manager = self.__dict__.get("execution_queue_manager")
        queue_item = (
            self._execution_queue_item_for_buylist_item(item)
            if manager is not None
            else None
        )
        env = self._buylist_order_environment(item)

        if order.status == OrderStatus.UNKNOWN_SUBMISSION_STATE:
            item._buy_order_pending = True
            order_id = order.broker_order_id or order.client_order_id
            if manager is not None and queue_item is not None:
                manager.mark_order_submitted(
                    item.symbol,
                    order_id=order_id,
                    order_status=OrderStatus.UNKNOWN_SUBMISSION_STATE.value,
                    environment=env,
                )
                item.monitoring_status = (
                    self._execution_queue_status_for_buylist_item(item)
                    or OrderStatus.UNKNOWN_SUBMISSION_STATE.value
                )
            else:
                item.monitoring_status = OrderStatus.UNKNOWN_SUBMISSION_STATE.value
            item.status = item.monitoring_status
            item.kis_order_id = order_id
            self._save_buylist_state()
            self._save_execution_queue_state()
            self.populate_buylist_dashboard()
            self.append_log(
                f"WARNING: BUY submission result UNKNOWN for {item.symbol}: "
                f"{order.error_message or 'no broker confirmation received'}. "
                "Reconcile KIS account/orders before retry."
            )
            QMessageBox.warning(
                self,
                "KIS order submission unknown",
                f"{item.symbol} buy order submission result is unknown.\n\n"
                "Verify KIS account/order status before clearing this state or submitting again.",
            )
            QTimer.singleShot(5000, self.reconcile_open_orders)
            return

        if order.status == OrderStatus.REJECTED:
            item._buy_order_pending = False
            if manager is not None:
                manager.mark_order_failed(
                    item.symbol, order_status="REJECTED", environment=env
                )
                queue_item = self._execution_queue_item_for_buylist_item(item)
            queue_status = self._execution_queue_status_for_buylist_item(item)
            item.monitoring_status = queue_status or "ERROR"
            item.status = item.monitoring_status
            self._save_buylist_state()
            self.populate_buylist_dashboard()
            self.append_log(
                f"BUY rejected for {item.symbol}: {order.error_message or 'broker rejected order'} "
                f"(status restored to {item.monitoring_status})"
            )
            QMessageBox.warning(
                self,
                "KIS order rejected",
                f"{item.symbol} buy order was rejected.\n\n{order.error_message or 'No broker error message provided.'}",
            )
            return

        item._buy_order_pending = False
        if manager is not None and queue_item is not None:
            manager.mark_order_submitted(
                item.symbol,
                order_id=order.broker_order_id or order.client_order_id,
                order_status=self._execution_queue_value(order.status).upper()
                or "SUBMITTED",
                environment=env,
            )
            item.monitoring_status = (
                self._execution_queue_status_for_buylist_item(item) or "ORDER_SUBMITTED"
            )
        else:
            item.monitoring_status = "BUY_SUBMITTED"
        item.status = item.monitoring_status
        item.kis_order_id = order.broker_order_id or order.client_order_id
        self._clear_buylist_auto_order_block(item)
        self._save_buylist_state()
        self.populate_buylist_dashboard()
        self.append_log(
            f"BUY order accepted by broker for {item.symbol}: {order.quantity} shares; waiting for fill confirmation"
        )
        QTimer.singleShot(5000, self.reconcile_open_orders)

    def _on_sell_order_accepted(
        self, item, quantity: int, reason: str, order: BrokerOrder
    ) -> None:
        item._stop_order_pending = False
        item._exit_order_pending = False
        self._record_broker_order(order)
        if order.account_no:
            item.kis_account_no = str(order.account_no)

        if order.status == OrderStatus.UNKNOWN_SUBMISSION_STATE:
            item._stop_order_pending = True
            item.monitoring_status = OrderStatus.UNKNOWN_SUBMISSION_STATE.value
            item.status = item.monitoring_status
            item.kis_order_id = order.broker_order_id or order.client_order_id
            self._save_buylist_state()
            self.populate_buylist_dashboard()
            self.append_log(
                f"WARNING: SELL submission result UNKNOWN for {item.symbol}: "
                f"{order.error_message or 'no broker confirmation received'}. "
                "Reconcile KIS account/orders before retry."
            )
            QMessageBox.warning(
                self,
                "KIS order submission unknown",
                f"{item.symbol} sell order submission result is unknown.\n\n"
                "Verify KIS account/order status before clearing this state or submitting again.",
            )
            QTimer.singleShot(5000, self.reconcile_open_orders)
            return

        if order.status == OrderStatus.REJECTED:
            try:
                shares_held = int(getattr(item, "shares_held", 0) or 0)
            except (TypeError, ValueError):
                shares_held = 0
            item.monitoring_status = "BOUGHT" if shares_held > 0 else "WATCHING"
            self._save_buylist_state()
            self.populate_buylist_dashboard()
            self.append_log(
                f"SELL rejected for {item.symbol}: {order.error_message or 'broker rejected order'} "
                f"(status restored to {item.monitoring_status})"
            )
            QMessageBox.warning(
                self,
                "KIS order rejected",
                f"{item.symbol} sell order was rejected.\n\n{order.error_message or 'No broker error message provided.'}",
            )
            return

        is_reserved_moo = (
            getattr(order, "execution_policy", REGULAR_LIMIT_EXECUTION)
            == RESERVED_MOO_EXECUTION
        )
        if order.intent in {OrderIntent.PARTIAL_EXIT, OrderIntent.PARTIAL_TAKE_PROFIT}:
            item.monitoring_status = (
                "PARTIAL_EXIT_RESERVED"
                if is_reserved_moo
                else "PARTIAL_EXIT_SUBMITTED"
            )
        else:
            item.monitoring_status = (
                "SELL_RESERVED" if is_reserved_moo else "SELL_SUBMITTED"
            )
        item.kis_order_id = order.broker_order_id or order.client_order_id
        self._clear_buylist_auto_order_block(item)
        self._save_buylist_state()
        self.populate_buylist_dashboard()
        if is_reserved_moo:
            self.append_log(
                f"KIS accepted the market-on-open SELL reservation for {item.symbol}: "
                f"{quantity} shares ({reason}); the broker will release it at the "
                "next U.S. regular open"
            )
        else:
            self.append_log(
                f"SELL order accepted by broker for {item.symbol}: {quantity} shares "
                f"({reason}); waiting for fill confirmation"
            )
        QTimer.singleShot(5000, self.reconcile_open_orders)

    def _on_buy_order_filled(
        self, item, quantity: int, order_price: float, result: dict
    ) -> None:
        """Backward-compatible slot: broker acceptance is no longer treated as filled."""
        env = self._buylist_order_environment(item)
        account_no = self._first_account_no_for_environment(env) or ""
        order = BrokerOrder.create(
            environment=env,
            account_no=account_no,
            symbol=item.symbol,
            side=OrderSide.BUY,
            intent=OrderIntent.ENTRY,
            quantity_requested=quantity,
            limit_price=order_price,
            buylist_symbol_key=f"{env}:{item.symbol}",
        )
        order.status = OrderStatus.ACCEPTED
        order.raw_submit_response = result or {}
        output = (result or {}).get("output") if isinstance(result, dict) else None
        if isinstance(output, dict):
            order.broker_order_id = str(output.get("ODNO") or output.get("odno") or "")
        self._on_buy_order_accepted(item, order)

    def _on_sell_order_filled(
        self, item, quantity: int, reason: str, result: dict
    ) -> None:
        """Backward-compatible slot: broker acceptance is no longer treated as filled."""
        env = self._buylist_order_environment(item)
        account_no = self._first_account_no_for_environment(env) or ""
        order_price = self._buylist_order_price(item)
        order = BrokerOrder.create(
            environment=env,
            account_no=account_no,
            symbol=item.symbol,
            side=OrderSide.SELL,
            intent=self._sell_intent_for_reason(reason),
            quantity_requested=quantity,
            limit_price=order_price,
            buylist_symbol_key=f"{env}:{item.symbol}",
        )
        order.status = OrderStatus.ACCEPTED
        order.raw_submit_response = result or {}
        output = (result or {}).get("output") if isinstance(result, dict) else None
        if isinstance(output, dict):
            order.broker_order_id = str(output.get("ODNO") or output.get("odno") or "")
        self._on_sell_order_accepted(item, quantity, reason, order)

    def reconcile_open_orders(self) -> None:
        if (
            self.order_reconciliation_worker
            and self.order_reconciliation_worker.isRunning()
        ):
            return

        self.order_ledger = load_order_ledger()
        open_orders = [
            order
            for order in find_open_orders(self.order_ledger)
            if order.environment == "PROD"
        ]
        if not open_orders:
            self._pending_reconciliation_groups = []
            return

        grouped: Dict[Tuple[str, str], List[BrokerOrder]] = {}
        for order in open_orders:
            grouped.setdefault((order.environment, order.account_no), []).append(order)

        if not self._pending_reconciliation_groups:
            self._pending_reconciliation_groups = sorted(grouped.keys())

        environment = ""
        account_no = ""
        while self._pending_reconciliation_groups:
            environment, account_no = self._pending_reconciliation_groups.pop(0)
            if (environment, account_no) in grouped:
                break
        else:
            return

        previous_snapshot = self.kis_account_snapshots.get(
            (environment, account_no), {}
        )
        self.order_reconciliation_worker = OrderReconciliationWorker(
            environment,
            account_no,
            grouped[(environment, account_no)],
            previous_snapshot=previous_snapshot,
        )
        self.order_reconciliation_worker.finished_reconciliation.connect(
            lambda orders, snapshot, env=environment, acct=account_no: self._on_order_reconciliation_finished(
                env, acct, orders, snapshot
            )
        )
        self.order_reconciliation_worker.error_occurred.connect(
            lambda message: self.append_log(f"Order reconciliation failed: {message}")
        )
        self._track_worker(
            "order_reconciliation_worker", self.order_reconciliation_worker
        )
        self.order_reconciliation_worker.start()
        self.append_log(
            f"Reconciling {len(grouped[(environment, account_no)])} open broker order(s) for {environment} {account_no or '<unknown account>'}"
        )

    def _on_order_reconciliation_finished(
        self,
        environment: str,
        account_no: str,
        updated_orders: List[BrokerOrder],
        snapshot: dict,
    ) -> None:
        self.kis_account_snapshots[(environment, account_no)] = snapshot or {}
        merge_orders(updated_orders)
        self.order_ledger = load_order_ledger()
        self.apply_confirmed_order_fills_to_buylist(updated_orders)
        self.sync_buylist_positions_from_kis_snapshots(
            {(environment, account_no): snapshot}
        )

        if self._pending_reconciliation_groups:
            QTimer.singleShot(1000, self.reconcile_open_orders)

    @staticmethod
    def _buylist_to_float(value: Any) -> float:
        try:
            return float(value or 0.0)
        except (TypeError, ValueError):
            return 0.0

    @classmethod
    def _buylist_snapshot_holdings(
        cls, snapshot: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        holdings: List[Dict[str, Any]] = []
        if not isinstance(snapshot, dict):
            return holdings
        for section_name in ("domestic", "overseas"):
            section = snapshot.get(section_name)
            if isinstance(section, dict):
                holdings.extend(
                    item
                    for item in section.get("holdings", [])
                    if isinstance(item, dict)
                )
        return holdings

    def sync_buylist_positions_from_kis_snapshots(
        self, snapshots: Optional[Dict[Any, dict]] = None
    ) -> int:
        """Sync held buylist positions to real KIS account holdings when snapshots are available."""
        from src.ui.controllers.account_controller import AccountController
        from src.ui.controllers.base import get_controller

        controller = get_controller(self, "account_controller", AccountController)
        return controller.sync_positions_from_kis(snapshots)

    def sync_positions_from_kis(
        self, snapshots: Optional[Dict[Any, dict]] = None
    ) -> int:
        from src.ui.controllers.account_controller import AccountController
        from src.ui.controllers.base import get_controller

        controller = get_controller(self, "account_controller", AccountController)
        return controller.sync_positions_from_kis(snapshots)

    def apply_confirmed_order_fills_to_buylist(
        self, updated_orders: List[BrokerOrder]
    ) -> None:
        changed = False
        for order in updated_orders:
            try:
                item = self.buylist_manager.get(order.symbol, order.environment)
            except TypeError:
                item = self.buylist_manager.get(order.symbol)
            if not item:
                continue

            try:
                filled_qty = max(0, int(order.filled_quantity or 0))
            except (TypeError, ValueError, OverflowError):
                filled_qty = 0
            try:
                applied_qty = max(
                    0, int(getattr(order, "applied_filled_quantity", 0) or 0)
                )
            except (TypeError, ValueError, OverflowError):
                applied_qty = 0
            newly_filled_qty = max(0, filled_qty - applied_qty)
            if filled_qty <= 0 or newly_filled_qty <= 0:
                continue

            if order.side == OrderSide.BUY:
                if order.account_no:
                    item.kis_account_no = str(order.account_no)
                manager = self.__dict__.get("execution_queue_manager")
                if manager is None and self._is_execution_queue_buylist_item(item):
                    manager = self._ensure_execution_queue_manager()
                if manager is not None:
                    manager.mark_order_filled(
                        order.symbol,
                        order_id=order.broker_order_id or order.client_order_id,
                        order_status=self._execution_queue_value(order.status).upper(),
                        environment=str(
                            getattr(item, "environment", "")
                            or getattr(order, "environment", "")
                            or "PROD"
                        ).upper(),
                    )
                    queue_status = self._execution_queue_status_for_buylist_item(item)
                    if queue_status:
                        item.status = queue_status
                item.shares_held = filled_qty
                if order.avg_fill_price:
                    item.avg_cost = float(order.avg_fill_price)
                if not getattr(item, "buy_date", None):
                    item.buy_date = dt.datetime.now(US_MARKET_ZONE)
                item.kis_order_id = order.broker_order_id or order.client_order_id
                item.monitoring_status = (
                    "BOUGHT" if order.status == OrderStatus.FILLED else "BUY_PARTIAL"
                )
                if item.avg_cost and item.shares_held:
                    item.position_percent = 100.0
                self.append_log(
                    f"BUY fill confirmed for {order.symbol}: {filled_qty}/{order.quantity} shares"
                )
                order.applied_filled_quantity = filled_qty
                update_order(order)
                changed = True
                continue

            previous_shares = max(0, int(getattr(item, "shares_held", 0) or 0))
            if order.account_no:
                item.kis_account_no = str(order.account_no)
            remaining_shares = max(0, previous_shares - newly_filled_qty)
            item.shares_held = remaining_shares
            item.kis_order_id = order.broker_order_id or order.client_order_id
            if order.intent in {
                OrderIntent.PARTIAL_EXIT,
                OrderIntent.PARTIAL_TAKE_PROFIT,
            }:
                item.sell_half_done = True
                if getattr(item, "avg_cost", 0):
                    item.stop_loss = max(
                        float(item.stop_loss or 0), float(item.avg_cost)
                    )
            if remaining_shares <= 0:
                item.monitoring_status = "SOLD"
            else:
                item.monitoring_status = "BOUGHT"
            self.append_log(
                f"SELL fill confirmed for {order.symbol}: {filled_qty}/{order.quantity} shares; {remaining_shares} remaining"
            )
            order.applied_filled_quantity = filled_qty
            update_order(order)
            changed = True

        if changed:
            self._save_buylist_state()
            self.populate_buylist_dashboard()
            self.order_ledger = load_order_ledger()

    def request_cancel_order(self, client_order_id: str) -> bool:
        self.order_ledger = load_order_ledger()
        target = next(
            (
                order
                for order in self.order_ledger
                if order.client_order_id == client_order_id
            ),
            None,
        )
        if target is None:
            self.append_log(
                f"Cancel request skipped: order {client_order_id} not found"
            )
            return False
        if target.status not in OPEN_ORDER_STATUSES:
            self.append_log(
                f"Cancel request skipped: order {client_order_id} is already {target.status.value}"
            )
            return False
        if not target.broker_order_id:
            self.append_log(
                f"Cancel request skipped: order {client_order_id} has no broker order id"
            )
            return False
        try:
            from src.services.order_reconciliation import \
                cancel_and_reconcile_order

            updated = cancel_and_reconcile_order(client_order_id)
        except Exception as exc:
            self.append_log(
                f"Cancel request failed for {target.symbol} order {client_order_id}: {exc}"
            )
            return False
        self.order_ledger = load_order_ledger()
        self.apply_confirmed_order_fills_to_buylist([updated])
        self._apply_broker_order_status_updates_to_buylist([updated])
        self.populate_buylist_dashboard()
        self.append_log(
            f"Cancel requested for {updated.symbol} {updated.side.value} order {client_order_id}: {updated.status.value}"
        )
        return True

    def _on_order_error(self, symbol: str, side: str, error: str, item=None) -> None:
        side_text = str(side).lower()
        ambiguous = is_ambiguous_order_submission_error(error)
        if item is not None:
            item._buy_order_pending = bool(ambiguous and side_text == "buy")
            item._stop_order_pending = bool(ambiguous and side_text == "sell")
            item._exit_order_pending = bool(ambiguous and side_text == "sell")
            manager = self.__dict__.get("execution_queue_manager")
            if manager is None and self._is_execution_queue_buylist_item(item):
                manager = self._ensure_execution_queue_manager()
            if manager is not None and side_text == "buy":
                environment = str(getattr(item, "environment", "") or "PROD").upper()
                if ambiguous:
                    manager.mark_order_submitted(
                        symbol,
                        order_status=OrderStatus.UNKNOWN_SUBMISSION_STATE.value,
                        environment=environment,
                    )
                else:
                    manager.mark_order_failed(
                        symbol,
                        order_status="ERROR",
                        environment=environment,
                    )
                queue_item = self._execution_queue_item_for_buylist_item(item)
                if queue_item is not None:
                    item.monitoring_status = self._execution_queue_value(
                        queue_item.status
                    )
                    item.status = item.monitoring_status
                self._save_execution_queue_state()
            elif ambiguous:
                item.monitoring_status = OrderStatus.UNKNOWN_SUBMISSION_STATE.value
                item.status = item.monitoring_status
            self._save_buylist_state()
            self.populate_buylist_dashboard()
        if ambiguous:
            self.append_log(
                f"WARNING: KIS {side.upper()} submission result UNKNOWN for {symbol}: {error}. "
                "Reconcile KIS account/orders before retry."
            )
            QMessageBox.warning(
                self,
                f"Order Submission Unknown - {symbol}",
                f"{side.upper()} order submission result is unknown.\n\n"
                "Verify KIS account/order status before clearing this state or submitting again.",
            )
            return
        self.append_log(
            f"[Buylist] KIS {side.upper()} order FAILED for {symbol}: {error}"
        )
        QMessageBox.warning(
            self, f"Order Failed — {symbol}", f"{side.upper()} order error:\n{error}"
        )

    def _track_buylist_order_worker(self, worker: QThread) -> None:
        """Keep every live order worker reachable until it has actually finished."""
        workers = self.__dict__.get("_buylist_order_workers")
        if workers is None:
            workers = []
            self._buylist_order_workers = workers
        if worker not in workers:
            workers.append(worker)
        if isinstance(worker, QThread):
            self._track_worker(
                "kis_order_worker",
                worker,
                collection_name="_buylist_order_workers",
            )
            return
        finished = getattr(worker, "finished", None)
        if hasattr(finished, "connect"):
            finished.connect(lambda current=worker: self._cleanup_order_worker(current))

    def _track_buylist_aux_worker(self, worker: QThread, attribute_name: str) -> None:
        """Track cancel/query workers without allowing an old finish to clear a new one."""
        workers = self.__dict__.get("_buylist_aux_workers")
        if workers is None:
            workers = []
            self._buylist_aux_workers = workers
        if worker not in workers:
            workers.append(worker)
        if isinstance(worker, QThread):
            self._track_worker(
                attribute_name,
                worker,
                collection_name="_buylist_aux_workers",
            )
            return
        finished = getattr(worker, "finished", None)
        if hasattr(finished, "connect"):
            finished.connect(
                lambda current=worker, attr=attribute_name: self._cleanup_aux_worker(
                    attr, current
                )
            )

    def _cleanup_order_worker(self, worker: QThread) -> None:
        workers = self.__dict__.get("_buylist_order_workers", [])
        if worker in workers:
            workers.remove(worker)
        if self.__dict__.get("kis_order_worker") is worker:
            self.kis_order_worker = None

    def _cleanup_aux_worker(self, attribute_name: str, worker: QThread) -> None:
        workers = self.__dict__.get("_buylist_aux_workers", [])
        if worker in workers:
            workers.remove(worker)
        if self.__dict__.get(attribute_name) is worker:
            setattr(self, attribute_name, None)
