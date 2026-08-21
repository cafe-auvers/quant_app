"""Route chart planning controls through canonical Buy Board commands.

Watchlist remains a passive persisted planning stage. Canonical card state
wins whenever it is available, while its local mirror remains usable for
offline, non-executable chart review. This mixin deliberately sits ahead of
:class:`BuyboardMixin` and
:class:`ChartsControllerMixin` in ``MainWindow``'s MRO.
"""

from __future__ import annotations

import math
from contextlib import contextmanager
from typing import Any, Iterator

from PyQt5.QtWidgets import QMessageBox

from src.core.board_workflow import (
    ActivateForToday,
    BoardCardProjection,
    CancelEntry,
    ClearBreakoutPrice,
    SetBreakoutPrice,
)
from src.core.trade_card_state import BoardStatus
from src.ui.buyboard.board import _command_kwargs
from src.ui.buyboard.card import card_drag_payload


class _ChartWatchlistItemView:
    """Read-only compatibility item with a canonical breakout target."""

    def __init__(self, item: Any, *, symbol: str, breakout_price: float | None):
        self._item = item
        self.symbol = str(getattr(item, "symbol", symbol) or symbol).upper()
        self.name = str(getattr(item, "name", self.symbol) or self.symbol)
        self.breakout_price = breakout_price

    def __getattr__(self, name: str) -> Any:
        if self._item is None:
            raise AttributeError(name)
        return getattr(self._item, name)


class _CanonicalChartWatchlistView:
    """Compatibility-shaped, read-only view used only while rendering."""

    def __init__(self, owner: Any, source: Any):
        self._owner = owner
        self._source = source

    @property
    def items(self):
        return tuple(getattr(self._source, "items", ()) or ())

    def get(self, symbol: str, *_args, **_kwargs):
        symbol = str(symbol or "").strip().upper()
        source_get = getattr(self._source, "get", None)
        item = source_get(symbol) if callable(source_get) else None
        card = self._owner._chart_buyboard_card(
            symbol, self._owner._chart_command_environment()
        )
        target = self._owner._chart_positive_price(
            getattr(card, "breakout_price", None)
            if card is not None
            else getattr(item, "breakout_price", None)
        )
        if item is None and card is None:
            return None
        return _ChartWatchlistItemView(
            item,
            symbol=symbol,
            breakout_price=target,
        )

    def __getattr__(self, name: str) -> Any:
        if name in {"add", "remove", "save"}:
            raise RuntimeError("Chart rendering cannot mutate Watchlist state directly")
        if self._source is None:
            raise AttributeError(name)
        return getattr(self._source, name)


class ChartCommandRoutingMixin:
    """Make chart Set/Clear/Queue/Activate canonical Buy Board gestures."""

    @staticmethod
    def _chart_positive_price(value: Any) -> float | None:
        try:
            price = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        return price if math.isfinite(price) and price > 0 else None

    def _chart_command_environment(self) -> str:
        combo = self.__dict__.get("watchlist_env_combo")
        value = combo.currentText() if combo is not None else "PROD"
        return str(value or "PROD").strip().upper()

    def _chart_selected_account(self, environment: str) -> str:
        resolver = getattr(self, "_selected_dashboard_kis_profile", None)
        if not callable(resolver):
            return ""
        try:
            profile = resolver() or {}
        except Exception:
            return ""
        profile_environment = str(
            profile.get("environment") or "PROD"
        ).strip().upper()
        if profile_environment != str(environment or "PROD").strip().upper():
            return ""
        return str(profile.get("account_no") or "").strip()

    def _chart_buyboard_projection(self, symbol: str, environment: str | None = None):
        symbol = str(symbol or "").strip().upper()
        environment = str(
            environment or self._chart_command_environment()
        ).strip().upper()
        if not symbol:
            return None
        candidates = []
        for value in tuple(
            self.__dict__.get("_buyboard_current_projections", ()) or ()
        ):
            card = getattr(value, "card", value)
            if (
                str(getattr(card, "environment", "") or "").strip().upper()
                == environment
                and str(getattr(card, "symbol", "") or "").strip().upper()
                == symbol
            ):
                candidates.append(
                    value
                    if isinstance(value, BoardCardProjection)
                    else BoardCardProjection(card=card)
                )
        selected_account = self._chart_selected_account(environment)
        if selected_account:
            selected = [
                value
                for value in candidates
                if str(value.card.account_no or "").strip() == selected_account
            ]
            if len(selected) == 1:
                return selected[0]
            # The selected Dashboard account is authoritative.  Never let a
            # single card belonging to another account override it.
            return None
        return candidates[0] if len(candidates) == 1 else None

    def _chart_buyboard_card(self, symbol: str, env: str):
        projection = self._chart_buyboard_projection(symbol, env)
        return projection.card if projection is not None else None

    def _chart_command_identity(self, symbol: str):
        environment = self._chart_command_environment()
        projection = self._chart_buyboard_projection(symbol, environment)
        if projection is not None:
            payload = card_drag_payload(projection)
            return projection, _command_kwargs(payload), payload["state_fingerprint"]

        account_no = self._chart_selected_account(environment)
        if not account_no:
            QMessageBox.warning(
                self,
                "Account required",
                "Select the exact Dashboard KIS account before creating a Buy Board plan.",
            )
            return None, None, ""
        return (
            None,
            {
                "environment": environment,
                "account_no": account_no,
                "symbol": str(symbol or "").strip().upper(),
                "expected_card_version": 0,
            },
            "",
        )

    def _dispatch_chart_command(
        self, command, *, interaction_fingerprint: str = ""
    ) -> bool:
        dispatcher = getattr(self, "_buyboard_dispatch_command", None)
        if not callable(dispatcher):
            QMessageBox.warning(
                self, "Buy Board unavailable", "Canonical Buy Board commands are unavailable."
            )
            return False
        return bool(
            dispatcher(
                command,
                interaction_fingerprint=str(interaction_fingerprint or ""),
            )
        )

    def set_chart_target_price(self, symbol: str, breakout_price: float) -> None:
        symbol = str(symbol or "").strip().upper()
        price = self._chart_positive_price(breakout_price)
        if not symbol or price is None:
            return
        _projection, common, fingerprint = self._chart_command_identity(symbol)
        if common is None:
            return
        rounded_price = round(price, 2)
        buffer_pct = 0.001
        buffer_reader = getattr(self, "_buyboard_orb_buffer_pct", None)
        if callable(buffer_reader):
            try:
                parsed_buffer = float(buffer_reader())
            except (TypeError, ValueError, OverflowError):
                parsed_buffer = 0.001
            if math.isfinite(parsed_buffer) and 0.0 <= parsed_buffer <= 1.0:
                buffer_pct = parsed_buffer
        if not self._dispatch_chart_command(
            SetBreakoutPrice(
                price=rounded_price,
                buffer_pct=buffer_pct,
                **common,
            ),
            interaction_fingerprint=fingerprint,
        ):
            return
        sync = getattr(self, "_sync_tradingview_target_price", None)
        if callable(sync):
            sync(symbol, rounded_price)
        reset = getattr(self, "_reset_chart_mode_buttons", None)
        if callable(reset):
            reset()
        mark_stale = getattr(self, "refresh_other_chart_views_for_symbol", None)
        if callable(mark_stale):
            mark_stale(symbol)
        append_log = getattr(self, "append_log", None)
        if callable(append_log):
            append_log(
                f"[Chart] Requested canonical breakout price for {symbol}: "
                f"{rounded_price:.2f}."
            )

    def clear_chart_target_price(self, symbol: str) -> None:
        symbol = str(symbol or "").strip().upper()
        if not symbol:
            return
        projection = self._chart_buyboard_projection(symbol)
        if projection is None:
            QMessageBox.information(
                self,
                "No canonical plan",
                f"{symbol} has no Buy Board plan to clear.",
            )
            return
        payload = card_drag_payload(projection)
        if not self._dispatch_chart_command(
            ClearBreakoutPrice(**_command_kwargs(payload)),
            interaction_fingerprint=payload["state_fingerprint"],
        ):
            return
        sync = getattr(self, "_sync_tradingview_target_price", None)
        if callable(sync):
            sync(symbol, None)
        reset = getattr(self, "_reset_chart_mode_buttons", None)
        if callable(reset):
            reset()
        mark_stale = getattr(self, "refresh_other_chart_views_for_symbol", None)
        if callable(mark_stale):
            mark_stale(symbol)
        append_log = getattr(self, "append_log", None)
        if callable(append_log):
            append_log(f"[Chart] Requested canonical breakout removal for {symbol}.")

    def _chart_queue_toggle(self, symbol: str) -> None:
        symbol = str(symbol or "").strip().upper()
        if not symbol:
            return
        projection = self._chart_buyboard_projection(symbol)
        if projection is None:
            QMessageBox.information(
                self,
                "Breakout price required",
                f"Set a breakout price for {symbol}; that creates its Buylist plan.",
            )
            return
        card = projection.card
        payload = card_drag_payload(projection)
        common = _command_kwargs(payload)
        target = self._chart_positive_price(card.breakout_price)
        if card.board_status == BoardStatus.BUYLIST and target is not None:
            command = ClearBreakoutPrice(**common)
            message = f"[Chart] Requested Buylist plan removal for {symbol}."
        elif card.board_status == BoardStatus.WATCHLIST and target is not None:
            command = SetBreakoutPrice(price=target, **common)
            message = f"[Chart] Requested canonical Buylist plan for {symbol}."
        elif card.board_status in {BoardStatus.BUY_TODAY, BoardStatus.ENTRY_PENDING}:
            command = CancelEntry(**common)
            message = f"[Chart] Requested Buy Today deactivation for {symbol}."
        elif max(0, int(card.broker_quantity or 0)) > 0:
            QMessageBox.warning(
                self,
                "Active position",
                f"{symbol} has an active position and cannot be removed here.",
            )
            return
        else:
            QMessageBox.information(
                self,
                "Breakout price required",
                f"Set a positive breakout price for {symbol} before activating it.",
            )
            return
        if self._dispatch_chart_command(
            command, interaction_fingerprint=payload["state_fingerprint"]
        ):
            append_log = getattr(self, "append_log", None)
            if callable(append_log):
                append_log(message)

    def _chart_activate_toggle(self, symbol: str, start_monitor: bool = False) -> None:
        del start_monitor  # Activation is now entirely canonical/runtime-owned.
        symbol = str(symbol or "").strip().upper()
        if not symbol:
            return
        projection = self._chart_buyboard_projection(symbol)
        if projection is None:
            QMessageBox.information(
                self,
                "Not in Buylist",
                f"Set a breakout price for {symbol} before activating it.",
            )
            return
        card = projection.card
        payload = card_drag_payload(projection)
        common = _command_kwargs(payload)
        if card.board_status == BoardStatus.BUYLIST:
            if self._chart_positive_price(card.breakout_price) is None:
                QMessageBox.information(
                    self,
                    "Breakout price required",
                    f"Set a positive breakout price for {symbol} before activating it.",
                )
                return
            command = ActivateForToday(**common)
            message = f"[Chart] Requested Buy Today activation for {symbol}."
        elif card.board_status in {BoardStatus.BUY_TODAY, BoardStatus.ENTRY_PENDING}:
            command = CancelEntry(**common)
            message = f"[Chart] Requested Buy Today deactivation for {symbol}."
        elif max(0, int(card.broker_quantity or 0)) > 0:
            QMessageBox.information(
                self, "Position already open", f"{symbol} is already an open position."
            )
            return
        else:
            QMessageBox.information(
                self,
                "Buy Board",
                f"{symbol} cannot be activated from {card.board_status.value}.",
            )
            return
        if self._dispatch_chart_command(
            command, interaction_fingerprint=payload["state_fingerprint"]
        ):
            append_log = getattr(self, "append_log", None)
            if callable(append_log):
                append_log(message)

    def _apply_chart_queue_btn_state(self, symbol: str, btn) -> None:
        card = self._chart_buyboard_card(symbol, self._chart_command_environment())
        target = self._chart_positive_price(
            getattr(card, "breakout_price", None) if card is not None else None
        )
        btn.setEnabled(False)
        btn.setStyleSheet("")
        if card is not None and max(0, int(card.broker_quantity or 0)) > 0:
            btn.setText("Position Open")
        elif card is not None and card.board_status in {
            BoardStatus.BUY_TODAY,
            BoardStatus.ENTRY_PENDING,
        }:
            btn.setText("Deactivate (Q)")
            btn.setEnabled(True)
            btn.setStyleSheet(
                "background-color: #c0392b; color: white; font-weight: 600;"
            )
        elif card is not None and card.board_status == BoardStatus.BUYLIST and target:
            btn.setText("Remove Buy Plan (Q)")
            btn.setEnabled(True)
            btn.setStyleSheet(
                "background-color: #c0392b; color: white; font-weight: 600;"
            )
        else:
            btn.setText("Set Breakout to Queue")

    def _apply_chart_activate_btn_state(self, symbol: str, btn) -> None:
        card = self._chart_buyboard_card(symbol, self._chart_command_environment())
        target = self._chart_positive_price(
            getattr(card, "breakout_price", None) if card is not None else None
        )
        btn.setEnabled(False)
        btn.setStyleSheet("")
        if card is not None and max(0, int(card.broker_quantity or 0)) > 0:
            btn.setText("Position Open")
        elif card is not None and card.board_status in {
            BoardStatus.BUY_TODAY,
            BoardStatus.ENTRY_PENDING,
        }:
            btn.setText("Deactivate (A)")
            btn.setEnabled(True)
            btn.setStyleSheet(
                "background-color: #c0392b; color: white; font-weight: 600;"
            )
        elif card is not None and card.board_status == BoardStatus.BUYLIST and target:
            btn.setText("Activate (A)")
            btn.setEnabled(True)
            btn.setStyleSheet(
                "background-color: #27ae60; color: white; font-weight: 600;"
            )
        else:
            btn.setText("Activate (A)")

    @contextmanager
    def _canonical_chart_watchlist_view(self) -> Iterator[None]:
        sentinel = object()
        original = self.__dict__.get("watchlist", sentinel)
        if isinstance(original, _CanonicalChartWatchlistView):
            yield
            return
        source = None if original is sentinel else original
        self.__dict__["watchlist"] = _CanonicalChartWatchlistView(self, source)
        try:
            yield
        finally:
            if original is sentinel:
                self.__dict__.pop("watchlist", None)
            else:
                self.__dict__["watchlist"] = original

    def load_tradingview_chart(self, *args, **kwargs):
        with self._canonical_chart_watchlist_view():
            return super().load_tradingview_chart(*args, **kwargs)

    def plot_selected_symbol(self, *args, **kwargs):
        with self._canonical_chart_watchlist_view():
            return super().plot_selected_symbol(*args, **kwargs)

    def plot_intraday_watchlist_symbol(self, *args, **kwargs):
        with self._canonical_chart_watchlist_view():
            return super().plot_intraday_watchlist_symbol(*args, **kwargs)

    def _on_buyboard_projection_completed(
        self, projections, error: str, generation: int
    ) -> None:
        before = self.__dict__.get("_buyboard_current_projections")
        super()._on_buyboard_projection_completed(projections, error, generation)
        after = self.__dict__.get("_buyboard_current_projections")
        if after is before:
            return
        update_queue = getattr(self, "_update_tradingview_queue_btn", None)
        if callable(update_queue):
            update_queue()
        combo = self.__dict__.get("tradingview_symbol_combo")
        symbol = combo.currentText().strip().upper() if combo is not None else ""
        if not symbol:
            return
        card = self._chart_buyboard_card(symbol, self._chart_command_environment())
        target = self._chart_positive_price(
            getattr(card, "breakout_price", None) if card is not None else None
        )
        sync = getattr(self, "_sync_tradingview_target_price", None)
        if callable(sync):
            sync(symbol, target)

    def _on_buyboard_command_completed(self, result) -> None:
        """Roll an optimistic chart line back immediately on command failure."""

        super()._on_buyboard_command_completed(result)
        command = getattr(getattr(result, "request", None), "command", None)
        if bool(getattr(result, "succeeded", False)) or not isinstance(
            command, (SetBreakoutPrice, ClearBreakoutPrice)
        ):
            return
        symbol = str(getattr(command, "symbol", "") or "").strip().upper()
        environment = str(
            getattr(command, "environment", "")
            or self._chart_command_environment()
        ).strip().upper()
        card = self._chart_buyboard_card(symbol, environment)
        target = self._chart_positive_price(
            getattr(card, "breakout_price", None) if card is not None else None
        )
        sync = getattr(self, "_sync_tradingview_target_price", None)
        if symbol and callable(sync):
            sync(symbol, target)
