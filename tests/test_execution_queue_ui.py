from types import SimpleNamespace

import pandas as pd
import pytest

import src.ui.mixins.buylist_mixin as buylist_mixin_module
from src.core.execution_queue import ExecutionQueueStatus, OrbCandidateStatus
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


class FakeTable:
    def __init__(self):
        self.rows = []

    def setRowCount(self, count):
        self.rows = [{} for _ in range(count)]

    def rowCount(self):
        return len(self.rows)

    def insertRow(self, row):
        self.rows.insert(row, {})

    def setItem(self, row, column, item):
        self.rows[row][column] = item

    def item(self, row, column):
        return self.rows[row].get(column)

    def columnCount(self):
        return 13


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
    window.account_size_input = _line_edit("100000")
    window.risk_percent_input = _line_edit("0.50")
    window.append_log = lambda _message: None
    window.populate_buylist_dashboard = lambda: None
    window.update_dashboard_summary = lambda: None
    window._save_state = lambda: None
    window._parse_float = lambda input_widget, default=0.0: float(input_widget.text() or default)
    window._get_account_balance_for_env = lambda _env: 100000.0
    window._first_account_no_for_environment = lambda _env: "12345678"
    window._has_duplicate_open_order = lambda *args, **kwargs: False
    window._watchlist_orb_buffer_pct = lambda: 0.001
    window._watchlist_orb_signal_price = lambda _symbol: 101.0
    window._calculate_adr_percent_for_symbol = lambda _symbol: 5.0
    window._load_cached_intraday_interval = lambda *_args, **_kwargs: _intraday()
    window._latest_intraday_session = lambda frame: frame
    window.buylist_sim_positions_label = None
    window.buylist_sim_capital_label = None
    window.buylist_sim_pnl_label = None
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
    result = window._last_execution_queue_refresh_result
    assert result.refreshed == 1
    assert result.missing_symbols == []
    assert result.status_counts == {"EXECUTE_READY": 1}


def test_missing_selected_symbol_is_returned_in_refresh_result(monkeypatch, tmp_path):
    window = _build_queue_window(monkeypatch, tmp_path)

    refreshed = MainWindow.refresh_execution_queue(
        window,
        "SIM",
        show_log=False,
        symbols=["ZZZ"],
        create_missing=True,
    )

    result = window._last_execution_queue_refresh_result
    assert refreshed == 0
    assert result.refreshed == 0
    assert result.target_count == 0
    assert result.missing_symbols == ["ZZZ"]
    assert result.status_counts == {}


def test_duplicate_pending_order_rejects_queue_candidates(monkeypatch, tmp_path):
    window = _build_queue_window(monkeypatch, tmp_path)
    window._has_duplicate_open_order = lambda *args, **kwargs: True

    refreshed = MainWindow.refresh_execution_queue(
        window,
        "SIM",
        show_log=False,
        symbols=["AAPL"],
        create_missing=True,
    )

    queue_item = window.execution_queue_manager.items["AAPL"]
    assert refreshed == 1
    assert queue_item.status == ExecutionQueueStatus.REJECTED
    assert queue_item.selected_candidate is None
    assert queue_item.candidates
    assert all(candidate.status == OrbCandidateStatus.REJECTED for candidate in queue_item.candidates.values())
    assert all("Duplicate" in candidate.reason for candidate in queue_item.candidates.values())
    assert window._last_execution_queue_refresh_result.status_counts == {"REJECTED": 1}


def test_refresh_result_status_counts_are_correct(monkeypatch, tmp_path):
    window = _build_queue_window(monkeypatch, tmp_path)

    MainWindow.refresh_execution_queue(
        window,
        "SIM",
        show_log=False,
        symbols=["AAPL"],
        create_missing=True,
    )

    result = window._last_execution_queue_refresh_result
    assert result.scope == "selected"
    assert result.status_counts == {"EXECUTE_READY": 1}


def test_buy_dashboard_status_uses_execution_queue_status(monkeypatch, tmp_path):
    window = _build_queue_window(monkeypatch, tmp_path)

    MainWindow.refresh_execution_queue(
        window,
        "SIM",
        show_log=False,
        symbols=["AAPL"],
        create_missing=True,
    )
    item = window.buylist_manager.get("AAPL", "SIM")
    item.monitoring_status = "ACTIVE"
    window.execution_queue_manager.items["AAPL"].status = ExecutionQueueStatus.ORDER_SUBMITTED

    assert MainWindow._buylist_dashboard_status(window, item) == "ORDER_SUBMITTED"
    assert "ORDER_SUBMITTED" in MainWindow._buylist_compute_alerts(window, item, 101.0, 0)


def test_buy_dashboard_queue_row_uses_execution_queue_candidate_values(monkeypatch, tmp_path):
    window = _build_queue_window(monkeypatch, tmp_path)
    MainWindow.refresh_execution_queue(
        window,
        "SIM",
        show_log=False,
        symbols=["AAPL"],
        create_missing=True,
    )
    item = window.buylist_manager.get("AAPL", "SIM")
    item.entry_price = 1.23
    item.stop_loss = 0.45
    item.position_percent = 1.0
    item._planned_shares = 1
    table = FakeTable()
    window.buylist_sim_table = table

    MainWindow._populate_buylist_env_table(window, "SIM")

    candidate = window.execution_queue_manager.items["AAPL"].selected_candidate
    assert table.item(0, 4).text() == f"{candidate.entry_trigger:.2f}"
    assert table.item(0, 6).text() == f"{candidate.stop_loss:.2f}"
    assert table.item(0, 9).text() == str(candidate.shares)
    assert table.item(0, 10).text() == f"{candidate.capital_percent:.1f}%"
    assert "Qty 1" not in table.item(0, 12).text()


def test_buy_dashboard_queue_row_falls_back_to_buylist_when_queue_missing(monkeypatch, tmp_path):
    window = _build_queue_window(monkeypatch, tmp_path)
    MainWindow.refresh_execution_queue(
        window,
        "SIM",
        show_log=False,
        symbols=["AAPL"],
        create_missing=True,
    )
    item = window.buylist_manager.get("AAPL", "SIM")
    item.entry_price = 12.34
    item.stop_loss = 11.11
    item.position_percent = 3.4
    item._planned_shares = 6
    del window.execution_queue_manager.items["AAPL"]
    table = FakeTable()
    window.buylist_sim_table = table

    MainWindow._populate_buylist_env_table(window, "SIM")

    assert table.item(0, 4).text() == "12.34"
    assert table.item(0, 6).text() == "11.11"
    assert table.item(0, 9).text() == "6"
    assert table.item(0, 10).text() == "3.4%"


def test_buy_dashboard_bought_row_uses_position_values_not_queue_projection(monkeypatch, tmp_path):
    window = _build_queue_window(monkeypatch, tmp_path)
    MainWindow.refresh_execution_queue(
        window,
        "SIM",
        show_log=False,
        symbols=["AAPL"],
        create_missing=True,
    )
    item = window.buylist_manager.get("AAPL", "SIM")
    item.monitoring_status = "BOUGHT"
    item.status = "BOUGHT"
    item.shares_held = 12
    item.avg_cost = 100.0
    item.position_percent = 33.3
    item.entry_price = 1.23
    item.stop_loss = 90.0
    window.latest_intraday_prices = {"AAPL": 110.0}
    table = FakeTable()
    window.buylist_sim_table = table

    MainWindow._populate_buylist_env_table(window, "SIM")

    assert table.item(0, 2).text() == "BOUGHT"
    assert table.item(0, 7).text() == "110.00"
    assert table.item(0, 8).text() == "+10.0%"
    assert table.item(0, 9).text() == "12"
    assert table.item(0, 10).text() == "33.3%"


def test_queue_order_review_uses_selected_candidate_values(monkeypatch, tmp_path):
    window = _build_queue_window(monkeypatch, tmp_path)
    MainWindow.refresh_execution_queue(
        window,
        "SIM",
        show_log=False,
        symbols=["AAPL"],
        create_missing=True,
    )
    item = window.buylist_manager.get("AAPL", "SIM")
    item.entry_price = 1.23
    queue_item = window.execution_queue_manager.items["AAPL"]
    queue_item.selected_candidate.entry_trigger = 123.45
    queue_item.selected_candidate.shares = 7

    review = MainWindow._format_execution_queue_order_review(window, "SIM", item, queue_item)

    assert "Limit price: $123.45" in review
    assert "Quantity: 7" in review
    assert "Limit price: $1.23" not in review


def test_legacy_orb_active_row_does_not_auto_buy(monkeypatch, tmp_path):
    window = _build_queue_window(monkeypatch, tmp_path)
    logs = []
    submissions = []
    item = SimpleNamespace(
        symbol="AAPL",
        environment="SIM",
        monitoring_status="ACTIVE",
        breakout_method="manual_trendline",
        breakout_price=100.0,
        buffer_pct=0.001,
        entry_price=100.1,
        stop_loss=98.0,
    )
    window.buylist_manager = SimpleNamespace(items=[item])
    window.latest_intraday_prices = {"AAPL": 101.0}
    window.append_log = logs.append
    window._buylist_refresh_item_data = lambda _item: None
    window._populate_buylist_env_table = lambda _env: None
    window._submit_kis_buy_order = lambda *_args, **_kwargs: submissions.append(True)

    MainWindow._run_buylist_monitor_cycle(window, "SIM")

    assert submissions == []
    assert any("skipping legacy ACTIVE auto-buy" in message for message in logs)
