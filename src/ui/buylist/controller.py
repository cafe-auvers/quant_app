"""Thin UI adapter for core buylist/exit policy."""

from src.core.exit_policy import (KST_ZONE, US_MARKET_CLOSE_TIME,
                                  US_MARKET_ZONE, auto_order_blocked,
                                  completed_daily_close_rows, compute_ema,
                                  market_session_date,
                                  market_session_date_from_value,
                                  momentum_exit_signal, partial_exit_quantity,
                                  set_attr_if_changed)
from src.ui.controllers.base import WindowController


class BuylistController(WindowController):
    """Expose core policies to the existing window-controller API."""

    auto_order_blocked = staticmethod(auto_order_blocked)
    set_attr_if_changed = staticmethod(set_attr_if_changed)
    market_session_date = staticmethod(market_session_date)
    market_session_date_from_value = staticmethod(market_session_date_from_value)
    partial_exit_quantity = staticmethod(partial_exit_quantity)
    momentum_exit_signal = staticmethod(momentum_exit_signal)
    completed_daily_close_rows = staticmethod(completed_daily_close_rows)
    compute_ema = staticmethod(compute_ema)
