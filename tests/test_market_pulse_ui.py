import datetime as dt
import os
from dataclasses import replace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication, QTabWidget, QVBoxLayout, QWidget

from src.core.market_pulse import (
    INDUSTRIES_THEMES,
    MARKET_SEGMENTS,
    SECTORS,
    MarketPulseRow,
    MarketPulseSnapshot,
    rank_market_pulse_rows,
)
from src.ui.market_pulse import MarketPulseMixin, MarketPulseTableModel


_APP = None


def _row(section, ticker, daily, rank=0):
    return MarketPulseRow(
        section=section,
        display_name=ticker,
        ticker=ticker,
        display_order=1,
        rank=rank,
        close=100.0,
        daily_return=daily,
        weekly_return=daily,
        monthly_return=daily,
        pct_above_52w_low=0.25,
        pct_below_52w_high=-0.08,
        source_session_date=dt.date.today(),
    )


def _snapshot():
    rows = rank_market_pulse_rows(
        [
            _row(MARKET_SEGMENTS, "IWO", 0.0127),
            _row(SECTORS, "XLK", 0.02),
            _row(INDUSTRIES_THEMES, "URA", -0.01),
        ]
    )
    return MarketPulseSnapshot(
        as_of_date=dt.date.today(),
        refreshed_at=dt.datetime.now(dt.timezone.utc),
        source="cached_test",
        rows=rows,
        failures={},
    )


class _Service:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.refresh_calls = 0
        self.engine = None

    def load_cached_snapshot(self):
        return self.snapshot

    def set_engine(self, engine):
        self.engine = engine

    def refresh(self):
        self.refresh_calls += 1
        return self.snapshot


class _Host(MarketPulseMixin, QWidget):
    def __init__(self, service):
        super().__init__()
        self.market_pulse_service = service
        self.market_pulse_worker = None
        self.db_engine = None
        self.logs = []
        layout = QVBoxLayout(self)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs)
        self.market_pulse_widget = QWidget()
        self.tabs.addTab(self.market_pulse_widget, "Market Pulse")
        self._build_market_pulse_tab()

    def append_log(self, text):
        self.logs.append(text)


def test_table_model_formats_percentage_once_and_keeps_missing_last():
    model = MarketPulseTableModel(
        [_row(SECTORS, "BBB", None, rank=2), _row(SECTORS, "AAA", 0.0127, rank=1)]
    )

    assert model.data(model.index(0, 4), Qt.DisplayRole) == "+1.3%"
    assert model.rows[-1].ticker == "BBB"
    model.sort(3, Qt.AscendingOrder)
    assert {row.ticker: row.rank for row in model.rows} == {"AAA": 1, "BBB": 2}


def test_component_columns_open_the_in_app_tradingview_chart():
    global _APP
    _APP = QApplication.instance() or QApplication([])
    component_row = _row(SECTORS, "GDX", 0.02)
    component_row = replace(component_row, stock1="AEM", stock2="NEM")
    model = MarketPulseTableModel([component_row])
    host = _Host(_Service(_snapshot()))
    host.tradingview_widget = QWidget()
    host.tabs.addTab(host.tradingview_widget, "TradingView")
    state = {"sidebar_symbol": "OLD", "chart_symbol": "OLD"}
    loaded = []
    host._select_sidebar_universe_symbol = lambda symbol: state.update(
        sidebar_symbol=symbol
    )
    host._set_tradingview_symbol = lambda symbol: state.update(chart_symbol=symbol)
    # MainWindow.on_tab_changed reapplies the sidebar selection when the
    # TradingView tab becomes active. This reproduced the old-symbol overwrite.
    host.tabs.currentChanged.connect(
        lambda _index: host._set_tradingview_symbol(state["sidebar_symbol"])
    )
    host.load_tradingview_chart = lambda **kwargs: loaded.append(kwargs)

    host._open_market_pulse_component_chart(model.index(0, 9))

    assert state == {"sidebar_symbol": "AEM", "chart_symbol": "AEM"}
    assert host.tabs.currentWidget() is host.tradingview_widget
    assert loaded == [{"force": True}]
    host.close()


def test_market_pulse_page_loads_cache_and_renders_all_sections():
    global _APP
    _APP = QApplication.instance() or QApplication([])
    host = _Host(_Service(_snapshot()))
    host.show()
    QApplication.processEvents()

    assert host.market_pulse_as_of_label.text().startswith("As of:")
    assert set(host.market_pulse_models) == {
        MARKET_SEGMENTS,
        SECTORS,
        INDUSTRIES_THEMES,
    }
    assert all(model.rowCount() == 1 for model in host.market_pulse_models.values())
    assert host.market_pulse_refresh_button.isEnabled()
    host._set_market_pulse_loading(True)
    assert host.market_pulse_loading_bar.isVisible()
    assert not host.market_pulse_refresh_button.isEnabled()
    host._on_market_pulse_refresh_error("offline")
    assert "last successful snapshot remains displayed" in host.market_pulse_message_label.text()
    assert all(model.rowCount() == 1 for model in host.market_pulse_models.values())
    host.close()
