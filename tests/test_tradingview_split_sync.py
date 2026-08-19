import json

import pandas as pd

from src.ui.charts import controller_drawing
from src.ui.charts.controller_drawing import ChartsDrawingMixin
from src.ui.charts.render_lightweight import ChartLightweightRenderMixin


class _FakePage:
    def __init__(self):
        self.scripts = []

    def runJavaScript(self, script, callback=None):
        self.scripts.append(script)
        if callback is not None:
            callback(None)


class _FakeWebView:
    def __init__(self, visible=True):
        self._visible = visible
        self._page = _FakePage()

    def isVisible(self):
        return self._visible

    def page(self):
        return self._page


class _FakeTabs:
    def __init__(self, current):
        self.current = current

    def currentWidget(self):
        return self.current


class _FakeCombo:
    def currentText(self):
        return "AAPL"


def _split_window(monkeypatch):
    monkeypatch.setattr(controller_drawing, "QWebEngineView", _FakeWebView)
    window = ChartsDrawingMixin()
    window.tradingview_widget = object()
    window.tabs = _FakeTabs(window.tradingview_widget)
    window.tradingview_symbol_combo = _FakeCombo()
    window.tradingview_chart_view = _FakeWebView()
    window.tradingview_split_chart_view = _FakeWebView()
    return window


def test_split_crosshair_routes_actual_time_and_price_to_the_sibling(monkeypatch):
    window = _split_window(monkeypatch)

    window.sync_tradingview_crosshair(
        "AAPL", "left", "2026-01-02 15:30:00", 200.0, True
    )

    assert window.tradingview_chart_view.page().scripts == []
    assert window.tradingview_split_chart_view.page().scripts == [
        'window.showSyncedCrosshair && window.showSyncedCrosshair('
        '"2026-01-02 15:30:00", 200.0);'
    ]


def test_line_creation_syncs_while_tradingview_line_tool_stays_active():
    window = ChartsDrawingMixin()
    window.chart_drawings = {}
    window._save_state = lambda: None
    window._is_active_tradingview_line_tool_symbol = lambda symbol: True
    window.append_log = lambda message: None
    synced = []
    window._sync_tradingview_drawing = lambda symbol, drawing: synced.append(
        (symbol, drawing)
    )

    window.save_chart_drawing(
        "AAPL",
        json.dumps(
            {
                "id": "line-1",
                "start_date": "2026-01-02",
                "start_price": 11.1,
                "end_date": "2026-01-04",
                "end_price": 13.2,
            }
        ),
    )

    assert synced == [("AAPL", window.chart_drawings["AAPL"][0])]


def test_lightweight_split_html_supports_direct_state_and_crosshair_sync():
    history = pd.DataFrame(
        {
            "Open": [10.0, 10.5],
            "High": [11.0, 11.5],
            "Low": [9.5, 10.0],
            "Close": [10.5, 11.0],
            "Volume": [1000, 1200],
        },
        index=pd.to_datetime(["2026-01-02 14:30", "2026-01-02 15:30"], utc=True),
    )

    chart_html = ChartLightweightRenderMixin._generate_tradingview_lightweight_chart_html(
        "AAPL",
        history,
        options={"timeframe": "1H", "view_key": "right", "sync_crosshair": True},
    )

    assert 'const chartViewKey = "right";' in chart_html
    assert "const crosshairSyncEnabled = true;" in chart_html
    assert "window.applySyncedTargetPrice" in chart_html
    assert "targetMode = false;" in chart_html
    assert "window.upsertSyncedDrawing" in chart_html
    assert "syncChartCrosshair" in chart_html
    assert "window.showSyncedCrosshair" in chart_html
    assert "resolveSyncedTime" in chart_html
    assert "normalizeTimeForSave(time)" in chart_html
    assert "chart.setCrosshairPosition" in chart_html


def test_hourly_initial_range_counts_same_81_sessions_as_daily():
    sessions = pd.bdate_range("2026-01-01", periods=100)
    timestamps = [
        session + pd.Timedelta(hours=hour)
        for session in sessions
        for hour in (14, 15)
    ]
    history = pd.DataFrame(
        {
            "Open": [10.0] * len(timestamps),
            "High": [11.0] * len(timestamps),
            "Low": [9.0] * len(timestamps),
            "Close": [10.5] * len(timestamps),
            "Volume": [1000] * len(timestamps),
        },
        index=pd.DatetimeIndex(timestamps),
    )

    chart_html = ChartLightweightRenderMixin._generate_tradingview_lightweight_chart_html(
        "AAPL", history, options={"timeframe": "1H", "max_history_bars": 1000}
    )

    assert "const visibleDataBars = Math.min(162, candles.length);" in chart_html
    assert "from: Math.max(0, candles.length - visibleDataBars)" in chart_html
