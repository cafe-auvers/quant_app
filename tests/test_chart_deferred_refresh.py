"""Tests for deferred Dashboard and chart refreshes during chart edits."""
from types import SimpleNamespace

from src.ui.main_window import MainWindow


class _FakeTabs:
    def __init__(self, current):
        self._current = current

    def currentWidget(self):
        return self._current


def _base_window(current_widget):
    window = MainWindow.__new__(MainWindow)
    window.dashboard_widget = object()
    window.intraday_charts_widget = object()
    window.tradingview_widget = object()
    window.tabs = _FakeTabs(current_widget)
    window._dashboard_summary_dirty = False
    window._intraday_tab_chart_stale = False
    window._tradingview_tab_chart_stale = False
    return window


# The method names are compatibility callbacks used by shared chart code; the
# retired Watchlist table is no longer part of their behavior.

def test_mark_watchlist_and_dashboard_dirty_defers_when_tabs_hidden():
    window = _base_window(current_widget=object())  # some other tab active
    calls = []
    window.update_dashboard_summary = lambda: calls.append("dashboard")

    window.mark_watchlist_and_dashboard_dirty()

    assert calls == []
    assert window._dashboard_summary_dirty is True


def test_mark_watchlist_and_dashboard_dirty_refreshes_visible_dashboard():
    window = _base_window(current_widget=None)
    window.tabs = _FakeTabs(window.dashboard_widget)
    calls = []
    window.update_dashboard_summary = lambda: calls.append("dashboard")

    window.mark_watchlist_and_dashboard_dirty()

    assert calls == ["dashboard"]
    assert window._dashboard_summary_dirty is False


def test_flush_dirty_watchlist_and_dashboard_catches_up_on_tab_switch():
    window = _base_window(current_widget=None)
    window._dashboard_summary_dirty = True
    window.tabs = _FakeTabs(window.dashboard_widget)
    calls = []
    window.update_dashboard_summary = lambda: calls.append("dashboard")

    window._flush_dirty_watchlist_and_dashboard()

    assert calls == ["dashboard"]
    assert window._dashboard_summary_dirty is False


def test_flush_dirty_watchlist_and_dashboard_noop_when_nothing_dirty():
    window = _base_window(current_widget=None)
    window.tabs = _FakeTabs(window.dashboard_widget)
    calls = []
    window.update_dashboard_summary = lambda: calls.append("dashboard")

    window._flush_dirty_watchlist_and_dashboard()

    assert calls == []


# --- refresh_other_chart_views_for_symbol / flush_stale_chart_views ---

def test_refresh_other_chart_views_marks_hidden_tabs_stale_without_reloading():
    calls = []
    window = _base_window(current_widget=None)
    window.tabs = _FakeTabs(window.tradingview_widget)
    window.intraday_symbol_combo = SimpleNamespace(currentText=lambda: "AAPL")
    window.tradingview_symbol_combo = SimpleNamespace(currentText=lambda: "AAPL")
    window.plot_intraday_watchlist_symbol = lambda **kw: calls.append("intraday")
    window.load_tradingview_chart = lambda **kw: calls.append("tradingview")

    window.refresh_other_chart_views_for_symbol("AAPL")

    # Nothing should reload synchronously -- the whole point is to avoid
    # rebuilding chart HTML for tabs the user isn't looking at right now.
    assert calls == []
    assert window._intraday_tab_chart_stale is True
    # TradingView tab is the active one, not an "other" tab -> not marked.
    assert window._tradingview_tab_chart_stale is False


def test_refresh_other_chart_views_ignores_non_matching_symbol():
    calls = []
    window = _base_window(current_widget=None)
    window.tabs = _FakeTabs(window.tradingview_widget)
    window.intraday_symbol_combo = SimpleNamespace(currentText=lambda: "MSFT")
    window.tradingview_symbol_combo = SimpleNamespace(currentText=lambda: "MSFT")
    window.plot_intraday_watchlist_symbol = lambda **kw: calls.append("intraday")
    window.load_tradingview_chart = lambda **kw: calls.append("tradingview")

    window.refresh_other_chart_views_for_symbol("AAPL")

    assert calls == []
    assert window._intraday_tab_chart_stale is False


def test_flush_stale_chart_views_refreshes_only_the_now_active_tab():
    calls = []
    window = _base_window(current_widget=None)
    window._intraday_tab_chart_stale = True
    window.tabs = _FakeTabs(window.intraday_charts_widget)
    window.plot_intraday_watchlist_symbol = lambda **kw: calls.append("intraday")
    window.load_tradingview_chart = lambda **kw: calls.append("tradingview")

    window.flush_stale_chart_views()

    assert calls == ["intraday"]
    assert window._intraday_tab_chart_stale is False


def test_flush_stale_chart_views_noop_when_nothing_stale():
    calls = []
    window = _base_window(current_widget=None)
    window.tabs = _FakeTabs(window.tradingview_widget)
    window.plot_intraday_watchlist_symbol = lambda **kw: calls.append("intraday")
    window.load_tradingview_chart = lambda **kw: calls.append("tradingview")

    window.flush_stale_chart_views()

    assert calls == []


# --- _on_intraday_fetch_finished only rechecks the symbol that changed ---

def test_intraday_fetch_finished_scopes_execution_queue_refresh_to_symbol():
    """A single symbol's intraday fetch completing used to trigger a refresh
    of the ENTIRE execution queue (two DB reads + a disk order-ledger reload
    per queued item), synchronously on the UI thread, every time. It should
    only recheck the symbol that actually got new data.
    """
    window = MainWindow.__new__(MainWindow)
    window.latest_intraday_sources = {}
    window.append_log = lambda *a, **kw: None
    window.intraday_symbol_combo = SimpleNamespace(currentText=lambda: "")
    # A QMainWindow subclass built via __new__ (no __init__) raises instead
    # of returning False from hasattr() on a genuinely-unset attribute, so
    # every attribute this method's hasattr-guarded branches touch has to be
    # given a harmless value up front.
    window.live_data_source_label = SimpleNamespace(setText=lambda *_a: None)
    window.symbol_input = SimpleNamespace(text=lambda: "")
    window.watchlist_env_combo = SimpleNamespace(currentText=lambda: "PROD")
    window.tradingview_timeframe_combo = SimpleNamespace(currentText=lambda: "1D")
    window.tradingview_widget = object()
    window.tabs = _FakeTabs(object())  # active tab is neither tradingview_widget

    calls = []
    window.refresh_execution_queue = lambda *a, **kw: calls.append((a, kw))

    window._on_intraday_fetch_finished("LIFE", None, 7, "kis")

    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args == ("PROD",)
    assert kwargs.get("symbols") == ["LIFE"]
