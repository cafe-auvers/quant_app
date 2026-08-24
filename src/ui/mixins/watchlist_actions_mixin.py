"""Lightweight Watchlist actions without restoring the retired Watchlist tab."""

from __future__ import annotations

import copy
import math
from typing import Any

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QKeySequence
from PyQt5.QtWidgets import QMessageBox, QPushButton, QShortcut

from src.core.board_workflow import MoveToWatchlist
from src.core.trade_card_state import BoardStatus
from src.services.planning_membership_service import is_passive_planning_card
from src.ui.buyboard.board import _command_kwargs
from src.ui.buyboard.card import card_drag_payload
from src.ui.planning_membership_worker import (
    PlanningMembershipOutcome,
    PlanningMembershipRequest,
    PlanningMembershipWorker,
)


class WatchlistActionsMixin:
    """Keep Watchlist as a passive persisted stage exposed through small actions."""

    @staticmethod
    def _normalized_watchlist_symbol(symbol: Any) -> str:
        return str(symbol or "").strip().upper()

    def _add_watchlist_candidate(
        self,
        symbol: str,
        *,
        name: str = "",
        entry_price: Any = None,
        source: str = "",
    ) -> bool:
        symbol = self._normalized_watchlist_symbol(symbol)
        watchlist = self.__dict__.get("watchlist")
        if not symbol or watchlist is None:
            QMessageBox.warning(self, "Watchlist", "A valid symbol is required.")
            return False
        existing = watchlist.get(symbol)
        projection_lookup = getattr(self, "_chart_buyboard_projection", None)
        projection = (
            projection_lookup(symbol) if callable(projection_lookup) else None
        )
        card = projection.card if projection is not None else None
        archived_canonical = bool(card is not None and not card.watchlist_member)
        if existing is not None and not archived_canonical:
            self._update_watchlist_action_surfaces()
            QMessageBox.information(
                self, "Watchlist", f"{symbol} is already in Watchlist."
            )
            return False
        return self._start_planning_membership_change(
            "add",
            symbol,
            name=name or symbol,
            entry_price=entry_price,
            source=source,
        )

    def _update_watchlist_action_surfaces(self) -> None:
        refresh_sidebar = getattr(self, "refresh_sidebar_sources", None)
        if callable(refresh_sidebar):
            current = None
            combo = self.__dict__.get("sidebar_source_combo")
            if combo is not None:
                current = combo.currentData()
            refresh_sidebar(selected_source=current)
        self._refresh_watchlist_symbol_navigation()
        update_tradingview = getattr(self, "_update_tradingview_watchlist_btn", None)
        if callable(update_tradingview):
            update_tradingview()
        update_summary = getattr(self, "update_dashboard_summary", None)
        if callable(update_summary):
            update_summary()

    def _refresh_watchlist_symbol_navigation(self) -> None:
        """Refresh retained chart navigation without rebuilding the old tab."""

        refresh = getattr(self, "populate_tradingview_watchlist_symbols", None)
        if callable(refresh):
            refresh()

    def add_current_tradingview_symbol_to_watchlist(self) -> None:
        combo = self.__dict__.get("tradingview_symbol_combo")
        symbol = self._normalized_watchlist_symbol(
            combo.currentText() if combo is not None else ""
        )
        if not symbol:
            QMessageBox.information(
                self, "No symbol", "Load a symbol before adding it to Watchlist."
            )
            return
        selected = getattr(self, "_get_sidebar_selected_data", lambda: None)() or {}
        selected_symbol = self._normalized_watchlist_symbol(selected.get("symbol"))
        name = selected.get("name") if selected_symbol == symbol else symbol
        entry_price = selected.get("price") if selected_symbol == symbol else None
        if self._tradingview_symbol_in_watchlist(symbol):
            self._remove_watchlist_candidate(symbol, confirm=False)
            return
        self._add_watchlist_candidate(
            symbol,
            name=name or symbol,
            entry_price=entry_price,
            source="TradingView",
        )

    def _tradingview_symbol_in_watchlist(self, symbol: str) -> bool:
        symbol = self._normalized_watchlist_symbol(symbol)
        if not symbol:
            return False
        projection = self._chart_buyboard_projection(symbol)
        card = projection.card if projection is not None else None
        if card is not None:
            return bool(card.watchlist_member)
        watchlist = self.__dict__.get("watchlist")
        return bool(watchlist is not None and watchlist.get(symbol) is not None)

    def _update_tradingview_watchlist_btn(self, _text: str = "") -> None:
        button = self.__dict__.get("tradingview_add_watchlist_button")
        if button is None:
            return
        combo = self.__dict__.get("tradingview_symbol_combo")
        symbol = self._normalized_watchlist_symbol(
            combo.currentText() if combo is not None else ""
        )
        projection = self._chart_buyboard_projection(symbol) if symbol else None
        card = projection.card if projection is not None else None
        in_watchlist = self._tradingview_symbol_in_watchlist(symbol)
        if card is None:
            supports_watchlist_toggle = True
        elif in_watchlist:
            supports_watchlist_toggle = bool(
                card.board_status == BoardStatus.BUYLIST
                or (
                    card.board_status == BoardStatus.WATCHLIST
                    and is_passive_planning_card(card)
                )
            )
        else:
            supports_watchlist_toggle = bool(
                card.board_status in {BoardStatus.WATCHLIST, BoardStatus.BUYLIST}
                and is_passive_planning_card(card)
            )
        if in_watchlist:
            button.setText("Remove from Watchlist (W)")
        elif not supports_watchlist_toggle:
            button.setText(f"In {card.board_status.value.replace('_', ' ').title()}")
        else:
            button.setText("Add to Watchlist (W)")
        button.setEnabled(bool(symbol) and supports_watchlist_toggle)
        button.setStyleSheet(
            "background-color: #c0392b; color: white; font-weight: 600;"
            if in_watchlist
            else (
                "background-color: #27ae60; color: white; font-weight: 600;"
                if supports_watchlist_toggle
                else ""
            )
        )

    @staticmethod
    def _layout_containing_widget(layout, target):
        if layout is None:
            return None, -1
        for index in range(layout.count()):
            item = layout.itemAt(index)
            if item.widget() is target:
                return layout, index
            child_layout = item.layout()
            if child_layout is not None:
                found, found_index = WatchlistActionsMixin._layout_containing_widget(
                    child_layout, target
                )
                if found is not None:
                    return found, found_index
        return None, -1

    def _install_tradingview_watchlist_controls(self) -> None:
        """Restore the small W action without touching the removed tab UI."""

        if self.__dict__.get("tradingview_add_watchlist_button") is not None:
            return
        widget = self.__dict__.get("tradingview_widget")
        if widget is None:
            return
        button = QPushButton("Add to Watchlist (W)", widget)
        button.setObjectName("tradingviewAddWatchlistButton")
        button.clicked.connect(self.add_current_tradingview_symbol_to_watchlist)
        queue_button = self.__dict__.get("tradingview_queue_btn")
        target_layout, target_index = self._layout_containing_widget(
            widget.layout(), queue_button
        )
        if target_layout is not None:
            target_layout.insertWidget(target_index, button)
        else:
            widget.layout().addWidget(button)
        self.tradingview_add_watchlist_button = button

        shortcut = QShortcut(QKeySequence("W"), widget)
        shortcut.setContext(Qt.WidgetWithChildrenShortcut)
        shortcut.activated.connect(self.add_current_tradingview_symbol_to_watchlist)
        self.tradingview_watchlist_shortcut = shortcut
        combo = self.__dict__.get("tradingview_symbol_combo")
        if combo is not None:
            combo.currentTextChanged.connect(self._update_tradingview_watchlist_btn)
        self._update_tradingview_watchlist_btn()

    def _planning_membership_account(self) -> str:
        resolver = getattr(self, "_selected_dashboard_kis_profile", None)
        if not callable(resolver):
            return ""
        try:
            profile = resolver() or {}
        except Exception:
            return ""
        if str(profile.get("environment") or "PROD").strip().upper() != "PROD":
            return ""
        return str(profile.get("account_no") or "").strip()

    def _planning_membership_buffer_pct(self) -> float:
        reader = getattr(self, "_buyboard_orb_buffer_pct", None)
        try:
            value = float(reader()) if callable(reader) else 0.001
        except (TypeError, ValueError, OverflowError):
            value = 0.001
        return value if math.isfinite(value) and 0.0 <= value <= 1.0 else 0.001

    def _start_planning_membership_change(
        self,
        operation: str,
        symbol: str,
        *,
        name: str = "",
        entry_price: Any = None,
        source: str = "",
    ) -> bool:
        symbol = self._normalized_watchlist_symbol(symbol)
        if not symbol:
            return False
        worker = self.__dict__.get("_planning_membership_worker")
        if worker is not None:
            try:
                if worker.isRunning():
                    QMessageBox.information(
                        self,
                        "Planning change in progress",
                        "Wait for the current Watchlist change to finish.",
                    )
                    return False
            except RuntimeError:
                pass
        engine_resolver = getattr(self, "_buyboard_engine", None)
        engine = engine_resolver() if callable(engine_resolver) else None
        account_no = self._planning_membership_account()
        if engine is None or not account_no:
            QMessageBox.warning(
                self,
                "Shared planning unavailable",
                "Shared planning storage and an exact production KIS account are "
                "required. The Watchlist was not changed.",
            )
            return False

        request = PlanningMembershipRequest(
            operation=operation,
            symbol=symbol,
            watchlist=copy.deepcopy(self.watchlist),
            buylist_manager=copy.deepcopy(self.buylist_manager),
            engine=engine,
            default_account_no=account_no,
            buffer_pct=self._planning_membership_buffer_pct(),
            name=name or symbol,
            entry_price=entry_price,
            source=str(source or ""),
        )
        worker = PlanningMembershipWorker(request)
        worker.completed.connect(self._on_planning_membership_completed)
        self._planning_membership_worker = worker
        self._planning_membership_pending = True
        self._update_sidebar_watchlist_actions()
        update_queue = getattr(self, "_update_tradingview_queue_btn", None)
        if callable(update_queue):
            update_queue()
        track = getattr(self, "_track_worker", None)
        if callable(track):
            track("_planning_membership_worker", worker)
        worker.start()
        return True

    def _promote_watchlist_candidate(self, symbol: str) -> bool:
        return self._start_planning_membership_change("promote", symbol)

    def _remove_watchlist_candidate(
        self, symbol: str, *, confirm: bool = True
    ) -> bool:
        symbol = self._normalized_watchlist_symbol(symbol)
        if not symbol:
            return False
        if confirm:
            answer = QMessageBox.question(
                self,
                "Remove from Watchlist",
                f"Remove {symbol} from Watchlist?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return False
        return self._start_planning_membership_change("remove", symbol)

    @staticmethod
    def _planning_membership_result_succeeded(result: object) -> bool:
        if result is None:
            return False
        if hasattr(result, "succeeded"):
            return bool(getattr(result, "succeeded"))
        action = str(getattr(result, "action", "") or "").lower()
        return bool(getattr(result, "changed", False)) or action in {
            "promoted",
            "removed",
            "unchanged",
            "synced",
        }

    def _on_planning_membership_completed(
        self, outcome: PlanningMembershipOutcome
    ) -> None:
        self._planning_membership_pending = False
        result = outcome.result
        succeeded = not outcome.error and self._planning_membership_result_succeeded(
            result
        )
        if succeeded:
            symbol = outcome.request.symbol
            if outcome.request.operation == "add":
                completed_item = outcome.request.watchlist.get(symbol)
                # A same-symbol state-sync result that arrived while SQL was
                # running is newer UI state; membership already agrees, so do
                # not overwrite its metadata. Unrelated symbols are untouched.
                if completed_item is not None and self.watchlist.get(symbol) is None:
                    self.watchlist.items.append(copy.deepcopy(completed_item))
            elif outcome.request.operation == "promote":
                completed_item = getattr(result, "buylist_item", None)
                if completed_item is not None:
                    self.buylist_manager.add(copy.deepcopy(completed_item))
            elif outcome.request.operation == "remove":
                self.watchlist.remove(symbol)
            save = getattr(self, "_save_state", None)
            if callable(save):
                save()
            selected_source = {
                "type": "buylist" if outcome.request.operation == "promote" else "watchlist"
            }
            refresh_sidebar = getattr(self, "refresh_sidebar_sources", None)
            if callable(refresh_sidebar):
                refresh_sidebar(selected_source=selected_source)
            self._refresh_watchlist_symbol_navigation()
            refresh_board = getattr(self, "refresh_buyboard", None)
            if callable(refresh_board):
                refresh_board()
            append_log = getattr(self, "append_log", None)
            if callable(append_log):
                verb = (
                    "Added to Buylist"
                    if outcome.request.operation == "promote"
                    else (
                        "Removed from Watchlist"
                        if outcome.request.operation == "remove"
                        else "Added to Watchlist"
                    )
                )
                source = str(getattr(outcome.request, "source", "") or "")
                prefix = f"[{source}] " if source else "[Watchlist] "
                append_log(f"{prefix}{verb}: {outcome.request.symbol}.")
        else:
            message = outcome.error or str(
                getattr(result, "message", "Watchlist membership was not changed.")
            )
            QMessageBox.warning(self, "Watchlist", message)
        self._update_sidebar_watchlist_actions()
        self._update_tradingview_watchlist_btn()
        update_queue = getattr(self, "_update_tradingview_queue_btn", None)
        if callable(update_queue):
            update_queue()

    def set_chart_target_price(self, symbol: str, breakout_price: float) -> None:
        symbol = self._normalized_watchlist_symbol(symbol)
        projection = self._chart_buyboard_projection(symbol)
        card = projection.card if projection is not None else None
        watchlist = self.__dict__.get("watchlist")
        in_watchlist = bool(
            watchlist is not None and symbol and watchlist.get(symbol) is not None
        )
        archived_watchlist_card = bool(
            card is not None
            and card.board_status == BoardStatus.WATCHLIST
            and not card.watchlist_member
        )
        if archived_watchlist_card or (projection is None and not in_watchlist):
            QMessageBox.information(
                self,
                "Add to Watchlist first",
                f"Add {symbol} to Watchlist before setting its breakout price.",
            )
            return
        super().set_chart_target_price(symbol, breakout_price)

    def _chart_queue_toggle(self, symbol: str) -> None:
        symbol = self._normalized_watchlist_symbol(symbol)
        projection = self._chart_buyboard_projection(symbol)
        card = projection.card if projection is not None else None
        watch_item = (
            self.watchlist.get(symbol)
            if symbol and self.__dict__.get("watchlist") is not None
            else None
        )
        canonical_watchlist_member = bool(
            card is not None
            and card.board_status == BoardStatus.WATCHLIST
            and card.watchlist_member
        )
        if card is not None and card.board_status == BoardStatus.BUYLIST:
            if not is_passive_planning_card(card):
                QMessageBox.warning(
                    self,
                    "Active position or order",
                    f"{symbol} has active broker/order state and cannot move to Watchlist.",
                )
                return
            payload = card_drag_payload(projection)
            if self._dispatch_chart_command(
                MoveToWatchlist(**_command_kwargs(payload)),
                interaction_fingerprint=payload["state_fingerprint"],
            ):
                append_log = getattr(self, "append_log", None)
                if callable(append_log):
                    append_log(f"[Chart] Requested move to Watchlist for {symbol}.")
            return
        if card is not None and card.board_status != BoardStatus.WATCHLIST:
            # Canonical active state outranks the retained Watchlist mirror.
            # BUY_TODAY remains a Buylist member, so its legacy mirror may
            # legitimately still contain the symbol; that must not turn Q
            # back into an "add to Buylist" action after activation.
            super()._chart_queue_toggle(symbol)
            return
        if (
            card is not None
            and card.board_status == BoardStatus.WATCHLIST
            and not card.watchlist_member
        ):
            QMessageBox.information(
                self,
                "Add to Watchlist first",
                f"Add {symbol} to Watchlist before moving it to Buylist.",
            )
            return
        if canonical_watchlist_member or watch_item is not None:
            target = self._chart_positive_price(
                getattr(card, "breakout_price", None)
                if card is not None
                else getattr(watch_item, "breakout_price", None)
            )
            if target is None:
                QMessageBox.information(
                    self,
                    "Breakout price required",
                    f"Set a positive breakout price for {symbol} before moving it to Buylist.",
                )
                return
            self._promote_watchlist_candidate(symbol)
            return
        super()._chart_queue_toggle(symbol)

    def _apply_chart_queue_btn_state(self, symbol: str, button) -> None:
        symbol = self._normalized_watchlist_symbol(symbol)
        projection = self._chart_buyboard_projection(symbol)
        card = projection.card if projection is not None else None
        watch_item = (
            self.watchlist.get(symbol)
            if symbol and self.__dict__.get("watchlist") is not None
            else None
        )
        canonical_watchlist_member = bool(
            card is not None
            and card.board_status == BoardStatus.WATCHLIST
            and card.watchlist_member
        )
        pending = bool(self.__dict__.get("_planning_membership_pending", False))
        if card is not None and card.board_status == BoardStatus.BUYLIST:
            if not is_passive_planning_card(card):
                button.setText("Position / Order Active")
                button.setEnabled(False)
                button.setStyleSheet("")
                return
            button.setText("Move to Watchlist (Q)")
            button.setEnabled(not pending)
            button.setStyleSheet(
                "background-color: #c0392b; color: white; font-weight: 600;"
            )
            return
        if card is not None and card.board_status != BoardStatus.WATCHLIST:
            # Do not let a stale/retained Watchlist item paint an active
            # canonical card as a green "Add to Buylist" action.
            super()._apply_chart_queue_btn_state(symbol, button)
            return
        if (
            card is not None
            and card.board_status == BoardStatus.WATCHLIST
            and not card.watchlist_member
        ):
            button.setText("Add to Watchlist First")
            button.setEnabled(False)
            button.setStyleSheet("")
            return
        if canonical_watchlist_member or watch_item is not None:
            target = self._chart_positive_price(
                getattr(card, "breakout_price", None)
                if card is not None
                else getattr(watch_item, "breakout_price", None)
            )
            button.setText(
                "Add to Buylist (Q)" if target is not None else "Set Breakout First"
            )
            button.setEnabled(target is not None and not pending)
            button.setStyleSheet(
                "background-color: #27ae60; color: white; font-weight: 600;"
                if target is not None
                else ""
            )
            return
        super()._apply_chart_queue_btn_state(symbol, button)

    def _on_buyboard_projection_completed(
        self, projections, error: str, generation: int
    ) -> None:
        """Converge JSON mirrors only after canonical state is observed."""

        super()._on_buyboard_projection_completed(projections, error, generation)
        if generation != self.__dict__.get("_buyboard_projection_generation", 0):
            return
        if int(self.__dict__.get("_buyboard_interaction_depth", 0) or 0) > 0:
            return
        if error or projections is None:
            return
        from src.services.planning_membership_service import (
            sync_legacy_planning_membership_from_card,
        )

        changed = False
        selected_account = self._planning_membership_account()
        for value in tuple(projections or ()):
            card = getattr(value, "card", value)
            if getattr(card, "board_status", None) not in {
                BoardStatus.WATCHLIST,
                BoardStatus.BUYLIST,
            }:
                continue
            if not selected_account or str(getattr(card, "account_no", "") or "") != selected_account:
                # The compact Watchlist mirror is account-agnostic. Never let
                # two account cards race to define its stage.
                continue
            result = sync_legacy_planning_membership_from_card(
                self.watchlist, self.buylist_manager, card
            )
            changed = bool(getattr(result, "changed", False)) or changed
        if not changed:
            return
        save = getattr(self, "_save_state", None)
        if callable(save):
            save()
        self._update_watchlist_action_surfaces()
        update_queue = getattr(self, "_update_tradingview_queue_btn", None)
        if callable(update_queue):
            update_queue()
