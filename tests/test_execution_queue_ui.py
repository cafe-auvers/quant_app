from types import SimpleNamespace

import pandas as pd
import pytest

import src.ui.mixins.buylist_mixin as buylist_mixin_module
from src.core.watchlist import BuylistManager, Watchlist, WatchlistItem
from src.ui.main_window import MainWindow


def _intraday(minutes=31, high=100.0, low=98.0, close=101.0):
    index = pd.date_range("2026-07-01 09:30", periods=minutes, freq="min")
    rows = []
    for i, _ts in enumerate(index):
        rows.append(
            {
                "Open": 99.0,
                "High": high + (0.01 if i == 0 else 0.0),
                "Low": low - (0.01 if i == 0 else 0.0),
                "Close": close,
                "Volume": 1000,
            }
        )
    return pd.DataFrame(rows, index=index)


def _line_edit(text: str):
    return SimpleNamespace(text=lambda: text)


def _build_queue_window(monkeypatch, tmp_path):
    monkeypatch.setattr(
        buylist_mixin_module,
        "EXECUTION_QUEUE_FILE",
        tmp_path / "execution_queue.json",
    )

    watchlist = Watchlist()
    watchlist.items.append(
        WatchlistItem(
            symbol="AAPL",
            name="Apple",
            breakout_price=100.0,
            stop_loss=98.0,
            notes="manual breakout",
        )
    )
    watchlist.items.append(
        WatchlistItem(
            symbol="MSFT",
            name="Microsoft",
            breakout_price=200.0,
            stop_loss=198.0,
        )
    )

    window = MainWindow.__new__(MainWindow)
    window.watchlist = watchlist
    window.buylist_manager = BuylistManager()
    window.latest_intraday_prices = {}
    window.risk_percent_input = _line_edit("0.50")
    window.append_log = lambda _message: None
    window.populate_buylist_dashboard = lambda: None
    window.update_dashboard_summary = lambda: None
    window._save_state = lambda: None
    window._get_account_balance_for_env = lambda _env: 100000.0
    window._first_account_no_for_environment = lambda _env: "12345678"
    window._has_duplicate_open_order = lambda *args, **kwargs: False
    window._watchlist_orb_buffer_pct = lambda: 0.001
    window._watchlist_orb_signal_price = lambda _symbol: 101.0
    window._calculate_adr_percent_for_symbol = lambda _symbol: 5.0
    window._load_cached_intraday_interval = lambda *_args, **_kwargs: _intraday()
    window._latest_intraday_session = lambda frame: frame
    return window


def test_refresh_execution_queue_does_not_create_rows_from_watchlist(monkeypatch, tmp_path):
    window = _build_queue_window(monkeypatch, tmp_path)

    refreshed = MainWindow.refresh_execution_queue(window, "SIM", show_log=False)

    assert refreshed == 0
    assert window.buylist_manager.items == []


def test_intentional_selected_symbol_creates_one_buylist_queue_item(monkeypatch, tmp_path):
    window = _build_queue_window(monkeypatch, tmp_path)

    refreshed = MainWindow.refresh_execution_queue(
        window,
        "SIM",
        show_log=False,
        symbols=["AAPL"],
        create_missing=True,
    )

    assert refreshed == 1
    assert len(window.buylist_manager.items) == 1
    item = window.buylist_manager.get("AAPL", "SIM")
    assert item is not None
    assert item.monitoring_status == "EXECUTE_READY"
    assert item.breakout_method == "execution_queue:1m"
    assert item.entry_price == pytest.approx(100.1)
    assert item._planned_shares > 0
    assert item.shares_held == 0
    assert window.execution_queue_manager.items["AAPL"].selected_window == "1m"
