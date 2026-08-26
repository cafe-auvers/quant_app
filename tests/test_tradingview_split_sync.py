import json

import pandas as pd

from src.services import app_state
from src.ui.charts import controller_drawing
from src.ui.charts.controller_drawing import ChartsDrawingMixin
from src.ui.charts.controller_layout import ChartsLayoutMixin
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


def test_daily_and_hourly_drawings_share_one_scope():
    window = ChartsDrawingMixin()

    assert window._drawing_timeframes_match("1D", "1H")
    assert window._drawing_timeframes_match("1D", "INTRADAY")
    assert not window._drawing_timeframes_match("1D", "5M")


def test_daily_and_hourly_views_load_the_same_saved_drawings():
    class Window(ChartsDrawingMixin, ChartsLayoutMixin):
        pass

    window = Window()
    window.chart_drawings = {
        "AAPL": [
            {
                "id": "daily-line",
                "type": "line",
                "start_date": "2026-01-02",
                "start_price": 10.0,
                "end_date": "2026-01-05",
                "end_price": 12.0,
                "timeframe": "1D",
            },
            {
                "id": "hourly-line",
                "type": "line",
                "start_date": "2026-01-02 15:30:00",
                "start_price": 11.0,
                "end_date": "2026-01-05 15:30:00",
                "end_price": 13.0,
                "timeframe": "1H",
            },
        ]
    }

    assert {drawing["id"] for drawing in window._build_combined_drawings("AAPL", "1D")} == {
        "daily-line",
        "hourly-line",
    }
    assert {drawing["id"] for drawing in window._build_combined_drawings("AAPL", "1H")} == {
        "daily-line",
        "hourly-line",
    }


def test_hourly_edit_and_delete_apply_to_daily_origin_drawing():
    window = ChartsDrawingMixin()
    window.chart_drawings = {
        "AAPL": [
            {
                "id": "shared-line",
                "type": "line",
                "start_date": "2026-01-02",
                "start_price": 10.0,
                "end_date": "2026-01-05",
                "end_price": 12.0,
                "timeframe": "1D",
            }
        ]
    }
    window._save_state = lambda: None
    window._is_active_tradingview_line_tool_symbol = lambda symbol: True
    window._sync_tradingview_drawing = lambda symbol, drawing: None
    window._remove_tradingview_drawing = lambda symbol, drawing_id, timeframe=None: None
    window.append_log = lambda message: None

    window.update_chart_drawing(
        "AAPL",
        json.dumps(
            {
                "id": "shared-line",
                "start_date": "2026-01-02 15:30:00",
                "start_price": 11.0,
                "end_date": "2026-01-05 15:30:00",
                "end_price": 13.0,
                "timeframe": "1H",
            }
        ),
    )
    assert window.chart_drawings["AAPL"][0]["start_price"] == 11.0

    window.delete_chart_drawing("AAPL", "shared-line", timeframe="1H")
    assert "AAPL" not in window.chart_drawings


def test_hourly_line_creation_pushes_to_both_split_views(monkeypatch):
    window = _split_window(monkeypatch)
    window.chart_drawings = {}
    window.tradingview_line_tool_active = True
    window._save_state = lambda: None
    window.append_log = lambda message: None

    window.save_chart_drawing(
        "AAPL",
        json.dumps(
            {
                "id": "shared-line",
                "start_date": "2026-01-02 15:30:00",
                "start_price": 11.1,
                "end_date": "2026-01-05 15:30:00",
                "end_price": 13.2,
                "timeframe": "1H",
            }
        ),
    )

    assert window.chart_drawings["AAPL"][0]["timeframe"] == "1H"
    for view in (
        window.tradingview_chart_view,
        window.tradingview_split_chart_view,
    ):
        assert len(view.page().scripts) == 1
        assert "window.upsertSyncedDrawing" in view.page().scripts[0]
        assert '"timeframe":"1H"' in view.page().scripts[0]
        assert ', "AAPL")' in view.page().scripts[0]


def test_daily_line_creation_pushes_to_both_split_views(monkeypatch):
    window = _split_window(monkeypatch)
    window.chart_drawings = {}
    window.tradingview_line_tool_active = True
    window._save_state = lambda: None
    window.append_log = lambda message: None

    window.save_chart_drawing(
        "AAPL",
        json.dumps(
            {
                "id": "daily-shared-line",
                "start_date": "2026-08-20",
                "start_price": 11.1,
                "end_date": "2026-09-23",
                "end_price": 13.2,
                "timeframe": "1D",
            }
        ),
    )

    for view in (
        window.tradingview_chart_view,
        window.tradingview_split_chart_view,
    ):
        assert len(view.page().scripts) == 1
        assert "window.upsertSyncedDrawing" in view.page().scripts[0]
        assert '"timeframe":"1D"' in view.page().scripts[0]


def test_finished_sibling_page_reconciles_drawings_missed_while_loading(monkeypatch):
    window = _split_window(monkeypatch)
    window.chart_drawings = {
        "AAPL": [
            {
                "id": "daily-during-hourly-load",
                "type": "line",
                "start_date": "2026-01-02",
                "start_price": 10.0,
                "end_date": "2026-01-05",
                "end_price": 12.0,
                "timeframe": "1D",
            }
        ]
    }

    window._resync_tradingview_drawings_in_view(
        window.tradingview_split_chart_view
    )

    scripts = window.tradingview_split_chart_view.page().scripts
    assert len(scripts) == 1
    assert "window.replaceSyncedDrawings" in scripts[0]
    assert "daily-during-hourly-load" in scripts[0]
    assert '"timeframe":"1D"' in scripts[0]


def test_persisted_drawing_keeps_its_timeframe(tmp_path, monkeypatch):
    drawings_path = tmp_path / "chart_drawings.json"
    monkeypatch.setattr(app_state, "CHART_DRAWINGS_FILE", drawings_path)
    app_state.save_json(
        drawings_path,
        {
            "AAPL": [
                {
                    "id": "hourly-line",
                    "type": "line",
                    "start_date": "2026-01-02 15:30:00",
                    "start_price": 11.1,
                    "end_date": "2026-01-05 15:30:00",
                    "end_price": 13.2,
                    "timeframe": "1H",
                }
            ]
        },
    )

    loaded = app_state.load_chart_drawings_state()

    assert loaded["AAPL"][0]["timeframe"] == "1H"


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
    assert "window.replaceSyncedDrawings" in chart_html
    assert 'new Set(["1D", "1H", "INTRADAY"])' in chart_html
    assert "syncChartCrosshair" in chart_html
    assert "window.showSyncedCrosshair" in chart_html
    assert "resolveSyncedTime" in chart_html
    assert "normalizeTimeForSave(time)" in chart_html
    assert "chart.setCrosshairPosition" in chart_html


def test_hourly_weekend_endpoint_snaps_to_next_daily_axis_bar():
    history = pd.DataFrame(
        {
            "Open": [10.0, 10.5],
            "High": [11.0, 11.5],
            "Low": [9.5, 10.0],
            "Close": [10.5, 11.0],
            "Volume": [1000, 1200],
        },
        index=pd.to_datetime(["2026-08-20", "2026-08-21"]),
    )
    drawing = {
        "id": "pbf-weekend-line",
        "type": "line",
        "start_date": "2026-08-21 19:30:00",
        "start_price": 58.89,
        "end_date": "2026-08-22 03:30:00",
        "end_price": 70.72,
        "timeframe": "1H",
    }

    chart_html = ChartLightweightRenderMixin._generate_tradingview_lightweight_chart_html(
        "PBF",
        history,
        options={"timeframe": "1D"},
        drawings=[drawing],
    )

    assert '"id": "pbf-weekend-line"' in chart_html
    assert '"start": {"time": "2026-08-21"' in chart_html
    assert '"end": {"time": "2026-08-24"' in chart_html
    assert "if (!usesIntradayTime) return snapDailyDrawingTimeToAxis(value, prefer);" in chart_html


def test_daily_future_endpoint_uses_an_hourly_axis_backed_time():
    history = pd.DataFrame(
        {
            "Open": [10.0, 10.5, 11.0, 11.5],
            "High": [11.0, 11.5, 12.0, 12.5],
            "Low": [9.5, 10.0, 10.5, 11.0],
            "Close": [10.5, 11.0, 11.5, 12.0],
            "Volume": [1000, 1200, 1100, 1300],
        },
        index=pd.to_datetime(
            [
                "2026-08-20 14:30",
                "2026-08-20 15:30",
                "2026-08-21 14:30",
                "2026-08-21 15:30",
            ],
            utc=True,
        ),
    )
    drawing = {
        "id": "daily-future-line",
        "type": "line",
        "start_date": "2026-08-20",
        "start_price": 10.0,
        "end_date": "2026-09-23",
        "end_price": 13.0,
        "timeframe": "1D",
    }

    chart_html = ChartLightweightRenderMixin._generate_tradingview_lightweight_chart_html(
        "AAPL",
        history,
        options={"timeframe": "1H"},
        drawings=[drawing],
    )

    hourly_axis_time = int(pd.Timestamp("2026-09-23 15:30", tz="UTC").timestamp())
    assert f'"end": {{"time": {hourly_axis_time}' in chart_html
    assert f'{{"time": {hourly_axis_time}}}' in chart_html
    assert "return nextTime != null" in chart_html


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
