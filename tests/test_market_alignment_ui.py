from __future__ import annotations

import datetime as dt

import pandas as pd

from src.core.market_alignment import (
    ContextState,
    MarketAlignmentSnapshot,
)
from src.ui.charts.render_alignment import build_market_alignment_overlay
from src.ui.charts import controller_data_flow
from src.ui.charts.controller_data_flow import ChartsDataFlowMixin
from src.ui.charts.render_lightweight import ChartLightweightRenderMixin


def _snapshot(*, stale=False, provisional=False):
    details = {
        "market": {
            "benchmark": "SPY",
            "return_5d": 0.012,
            "conditions": [
                {"name": "close_above_sma20", "result": True},
                {"name": "close_above_sma50", "result": True},
                {"name": "return_5d_positive", "result": True},
            ],
        },
        "segment": {
            "return_5d": 0.018,
            "spy_return_5d": 0.012,
            "conditions": [{"name": "close_above_sma20", "result": True}],
        },
        "sector": {
            "return_5d": 0.024,
            "performance_percentile_20d": 86,
            "conditions": [{"name": "outperforming_spy_5d", "result": True}],
        },
        "industry": {
            "return_5d": 0.016,
            "sector_return_5d": 0.024,
            "performance_percentile_20d": 83,
        },
        "metadata": {"themes": ["AI", "Data Center"]},
    }
    return MarketAlignmentSnapshot(
        symbol="NVDA",
        as_of_date=dt.date(2026, 8, 21),
        feature_version="1.0",
        market_rs=94,
        market_rs_source="scanner_growth_rank_1m",
        industry_peer_rs=87,
        peer_basis="industry",
        peer_count=32,
        peer_group_id="semiconductors",
        peer_group_name="Semiconductors",
        leadership_score=91.2,
        leadership_label="STRONG",
        market_state=ContextState.GREEN,
        market_conditions_passed=3,
        segment_name="Mega-Cap",
        segment_proxy="MGK",
        segment_state=ContextState.GREEN,
        segment_conditions_passed=3,
        sector_name="Technology",
        sector_proxy="XLK",
        sector_state=ContextState.GREEN,
        sector_conditions_passed=3,
        industry_name="Semiconductors",
        industry_proxy_or_index="SOXX",
        industry_state=ContextState.YELLOW,
        industry_conditions_passed=2,
        context_points=7,
        context_available_components=3 if provisional else 4,
        context_label="SUPPORTIVE",
        is_provisional=provisional,
        classification_source="nasdaq",
        calculated_at=dt.datetime(2026, 8, 22, 1, 2, tzinfo=dt.timezone.utc),
        calculation_details=details,
        is_stale=stale,
    )


def _history():
    dates = pd.bdate_range("2026-07-01", periods=40)
    close = pd.Series(range(100, 140), index=dates, dtype=float)
    return pd.DataFrame(
        {
            "Open": close - 1,
            "High": close + 1,
            "Low": close - 2,
            "Close": close,
            "Volume": [1_000_000] * len(close),
        },
        index=dates,
    )


def test_compact_overlay_matches_required_layout_and_has_accessible_indicators():
    overlay = build_market_alignment_overlay(_snapshot())

    assert '<span class="alignment-score">91</span>' in overlay
    assert '<span class="alignment-label">STRONG</span>' in overlay
    assert "CONTEXT: SUPPORTIVE" in overlay
    for label, state in (("MKT", "Green"), ("SEG", "Green"), ("SEC", "Green"), ("IND", "Yellow")):
        assert f'aria-label="{label}: {state}"' in overlay
    assert "●" in overlay


def test_details_format_values_once_and_expose_every_calculation_section():
    overlay = build_market_alignment_overlay(_snapshot())

    for section in (
        "Leadership",
        "Broad market",
        "Market segment",
        "Sector",
        "Industry",
        "Metadata",
    ):
        assert section in overlay
    assert "+1.2%" in overlay
    assert "+1.8%" in overlay
    assert "91 / 100" in overlay
    assert "EOD as of" in overlay
    assert "2026-08-21" in overlay
    assert "AI, Data Center" in overlay


def test_unknown_and_stale_states_are_explicit_not_red():
    unknown = build_market_alignment_overlay(None)
    stale = build_market_alignment_overlay(_snapshot(stale=True))

    assert '<span class="alignment-score">—</span>' in unknown
    assert "CONTEXT: UNKNOWN" in unknown
    assert unknown.count('data-state="UNKNOWN"') == 4
    assert "○" in unknown
    assert "No published EOD snapshot" in unknown
    assert "STALE" in stale


def test_provisional_details_show_incomplete_data_indicator():
    overlay = build_market_alignment_overlay(_snapshot(provisional=True))

    assert "Provisional (incomplete data)" in overlay
    assert "not fully evaluated" in overlay


def test_chart_html_anchors_overlay_upper_right_and_toggle_is_dom_only():
    page = ChartLightweightRenderMixin._generate_tradingview_lightweight_chart_html(
        "NASDAQ:NVDA",
        _history(),
        alignment_snapshot=_snapshot(),
    )

    assert '#market-alignment-overlay {' in page
    assert "position: absolute;" in page
    assert "top: 10px;" in page
    assert "right: 68px;" in page
    assert "left: auto;" in page
    assert "left: 10px;" not in page
    assert page.index('id="chart"') < page.index('id="market-alignment-overlay"')
    assert "marketAlignmentDetailsOpen" in page
    assert "alignmentDetails.classList.toggle" in page
    assert "window.updateMarketAlignmentOverlay" in page
    assert "createChart" in page
    assert "load_tradingview_chart" not in page


def test_cached_candle_page_updates_only_the_overlay(monkeypatch):
    class Page:
        def __init__(self):
            self.scripts = []

        def runJavaScript(self, script):
            self.scripts.append(script)

    class View:
        def __init__(self):
            self._page = Page()

        def page(self):
            return self._page

    monkeypatch.setattr(controller_data_flow, "QWebEngineView", View)
    view = View()

    assert ChartsDataFlowMixin._update_market_alignment_overlay_in_view(
        view, _snapshot()
    ) is True
    assert len(view.page().scripts) == 1
    assert "window.updateMarketAlignmentOverlay" in view.page().scripts[0]
    assert "CONTEXT: SUPPORTIVE" in view.page().scripts[0]
    assert "createChart" not in view.page().scripts[0]
