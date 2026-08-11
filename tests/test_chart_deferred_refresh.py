"""Tests for the deferred-refresh optimizations that avoid rebuilding the
watchlist table / dashboard summary / hidden chart tabs synchronously while
the user is mid-interaction on a different tab (breakout price set, drawing
added). See mark_watchlist_and_dashboard_dirty / refresh_other_chart_views_for_symbol
in the mixins for the full rationale.
"""
from types import SimpleNamespace

from src.ui.main_window import MainWindow


class _FakeTabs:
    def __init__(self, current):
        self._current = current

    def currentWidget(self):
        return self._current


def _base_window(current_widget):
    window = MainWindow.__new__(MainWindow)
    window.watchlist_widget = object()
    window.dashboard_widget = object()
    window.charts_widget = object()
    window.intraday_charts_widget = object()
    window.tradingview_widget = object()
    window.tabs = _FakeTabs(current_widget)
    window._watchlist_table_dirty = False
    window._dashboard_summary_dirty = False
    window._charts_tab_chart_stale = False
    window._intraday_tab_chart_stale = False
    window._tradingview_tab_chart_stale = False
    return window


# --- mark_watchlist_and_dashboard_dirty / _flush_dirty_watchlist_and_dashboard ---

def test_mark_watchlist_and_dashboard_dirty_defers_when_tabs_hidden():
    window = _base_window(current_widget=object())  # some other tab active
    calls = []
    window.populate_watchlist_table = lambda: calls.append("watchlist")
    window.update_dashboard_summary = lambda: calls.append("dashboard")

    window.mark_watchlist_and_dashboard_dirty()

    assert calls == []
    assert window._watchlist_table_dirty is True
    assert window._dashboard_summary_dirty is True


def test_mark_watchlist_and_dashboard_dirty_refreshes_visible_watchlist_tab():
    window = _base_window(current_widget=None)
    window.tabs = _FakeTabs(window.watchlist_widget)
    calls = []
    window.populate_watchlist_table = lambda: calls.append("watchlist")
    window.update_dashboard_summary = lambda: calls.append("dashboard")

    window.mark_watchlist_and_dashboard_dirty()

    # Watchlist tab is visible -> refreshed immediately, never left stale.
    assert calls == ["watchlist"]
    assert window._watchlist_table_dirty is False
    # Dashboard isn't visible right now -> deferred rather than paid for now.
    assert window._dashboard_summary_dirty is True


def test_flush_dirty_watchlist_and_dashboard_catches_up_on_tab_switch():
    window = _base_window(current_widget=None)
    window._watchlist_table_dirty = True
    window._dashboard_summary_dirty = True
    window.tabs = _FakeTabs(window.dashboard_widget)
    calls = []
    window.populate_watchlist_table = lambda: calls.append("watchlist")
    window.update_dashboard_summary = lambda: calls.append("dashboard")

    window._flush_dirty_watchlist_and_dashboard()

    # Only the tab the user actually switched into gets caught up.
    assert calls == ["dashboard"]
    assert window._dashboard_summary_dirty is False
    assert window._watchlist_table_dirty is True


def test_flush_dirty_watchlist_and_dashboard_noop_when_nothing_dirty():
    window = _base_window(current_widget=None)
    window.tabs = _FakeTabs(window.watchlist_widget)
    calls = []
    window.populate_watchlist_table = lambda: calls.append("watchlist")
    window.update_dashboard_summary = lambda: calls.append("dashboard")

    window._flush_dirty_watchlist_and_dashboard()

    assert calls == []


# --- refresh_other_chart_views_for_symbol / flush_stale_chart_views ---

def test_refresh_other_chart_views_marks_hidden_tabs_stale_without_reloading():
    calls = []
    window = _base_window(current_widget=None)
    window.tabs = _FakeTabs(window.tradingview_widget)
    window.chart_symbol_input = object()
    window._get_chart_symbol = lambda: "AAPL"
    window.intraday_symbol_combo = SimpleNamespace(currentText=lambda: "AAPL")
    window.tradingview_symbol_combo = SimpleNamespace(currentText=lambda: "AAPL")
    window.plot_selected_symbol = lambda **kw: calls.append("charts")
    window.plot_intraday_watchlist_symbol = lambda **kw: calls.append("intraday")
    window.load_tradingview_chart = lambda **kw: calls.append("tradingview")

    window.refresh_other_chart_views_for_symbol("AAPL")

    # Nothing should reload synchronously -- the whole point is to avoid
    # rebuilding chart HTML for tabs the user isn't looking at right now.
    assert calls == []
    assert window._charts_tab_chart_stale is True
    assert window._intraday_tab_chart_stale is True
    # TradingView tab is the active one, not an "other" tab -> not marked.
    assert window._tradingview_tab_chart_stale is False


def test_refresh_other_chart_views_ignores_non_matching_symbol():
    calls = []
    window = _base_window(current_widget=None)
    window.tabs = _FakeTabs(window.tradingview_widget)
    window.chart_symbol_input = object()
    window._get_chart_symbol = lambda: "MSFT"
    window.intraday_symbol_combo = SimpleNamespace(currentText=lambda: "MSFT")
    window.tradingview_symbol_combo = SimpleNamespace(currentText=lambda: "MSFT")
    window.plot_selected_symbol = lambda **kw: calls.append("charts")
    window.plot_intraday_watchlist_symbol = lambda **kw: calls.append("intraday")
    window.load_tradingview_chart = lambda **kw: calls.append("tradingview")

    window.refresh_other_chart_views_for_symbol("AAPL")

    assert calls == []
    assert window._charts_tab_chart_stale is False
    assert window._intraday_tab_chart_stale is False


def test_flush_stale_chart_views_refreshes_only_the_now_active_tab():
    calls = []
    window = _base_window(current_widget=None)
    window._charts_tab_chart_stale = True
    window._intraday_tab_chart_stale = True
    window.tabs = _FakeTabs(window.charts_widget)
    window.plot_selected_symbol = lambda **kw: calls.append("charts")
    window.plot_intraday_watchlist_symbol = lambda **kw: calls.append("intraday")
    window.load_tradingview_chart = lambda **kw: calls.append("tradingview")

    window.flush_stale_chart_views()

    assert calls == ["charts"]
    assert window._charts_tab_chart_stale is False
    # Intraday tab is still stale -- it isn't the tab the user switched to.
    assert window._intraday_tab_chart_stale is True


def test_flush_stale_chart_views_noop_when_nothing_stale():
    calls = []
    window = _base_window(current_widget=None)
    window.tabs = _FakeTabs(window.charts_widget)
    window.plot_selected_symbol = lambda **kw: calls.append("charts")
    window.plot_intraday_watchlist_symbol = lambda **kw: calls.append("intraday")
    window.load_tradingview_chart = lambda **kw: calls.append("tradingview")

    window.flush_stale_chart_views()

    assert calls == []
