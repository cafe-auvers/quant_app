import datetime as dt
from types import SimpleNamespace

import pandas as pd

from src.ui.main_window import MainWindow


class _DummyView:
    def setPlainText(self, text):
        self.text = text


def _render_with_buylist_item(monkeypatch, buylist_item):
    history = pd.DataFrame(
        {
            "Open": [100.0],
            "High": [105.0],
            "Low": [95.0],
            "Close": [102.0],
            "Volume": [1000],
        },
        index=pd.date_range("2026-01-02", periods=1, freq="D"),
    )
    captured = {}
    window = MainWindow.__new__(MainWindow)
    window.chart_drawings = {}
    window.tradingview_refresh_timestamps = {}
    window.buylist_manager = SimpleNamespace(get=lambda symbol: buylist_item)
    window._load_chart_history_for_timeframe = (
        lambda symbol, timeframe, use_live_fallback=True, window_days=7: history
    )

    def capture_chart(*args, **kwargs):
        captured.update(kwargs)
        return "<html>ok</html>"

    monkeypatch.setattr(
        MainWindow,
        "_generate_tradingview_lightweight_chart_html",
        staticmethod(capture_chart),
    )
    window._render_tradingview_chart_view(
        _DummyView(),
        symbol="AAPL",
        tradingview_symbol="AAPL",
        timeframe="1D",
        base_options={"show_volume": True, "show_ema": True, "show_rs": False},
        now=dt.datetime(2026, 1, 2, tzinfo=dt.timezone.utc),
        force=True,
        view_key="single",
    )
    return captured


def test_orb_plan_does_not_render_buy_or_stop_lines(monkeypatch):
    captured = _render_with_buylist_item(
        monkeypatch,
        SimpleNamespace(
            monitoring_status="EXECUTE_READY",
            shares_held=0,
            avg_cost=0.0,
            entry_price=200.0,
            stop_loss=190.0,
        ),
    )

    assert captured["buy_price"] is None
    assert captured["stop_loss"] is None


def test_filled_position_renders_actual_cost_and_stop_lines(monkeypatch):
    captured = _render_with_buylist_item(
        monkeypatch,
        SimpleNamespace(
            monitoring_status="BUY_PARTIAL",
            shares_held=10,
            avg_cost=201.5,
            entry_price=200.0,
            stop_loss=190.0,
        ),
    )

    assert captured["buy_price"] == 201.5
    assert captured["stop_loss"] == 190.0
