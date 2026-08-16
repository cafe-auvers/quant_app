import datetime as dt
from types import SimpleNamespace

import pandas as pd
import pytest

import src.ui.buylist.view as buylist_view_module
from src.core.execution_queue import (ExecutionQueueStatus, OrbCandidateStatus,
                                      is_pre_entry_execution_queue_item,
                                      queue_key)
from src.core.watchlist import BuylistManager, Watchlist, WatchlistItem
from src.ui.main_window import MainWindow


def _intraday(minutes=31, high=101.0, low=99.0, close=102.0):
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


def test_queue_identity_requires_the_durable_strategy_marker():
    legacy = SimpleNamespace(monitoring_status="WATCHING", breakout_method="")
    queued = SimpleNamespace(
        monitoring_status="WATCHING",
        breakout_method="execution_queue:1m",
    )

    assert is_pre_entry_execution_queue_item(legacy) is False
    assert is_pre_entry_execution_queue_item(queued) is True
    assert MainWindow._is_execution_queue_buylist_item(legacy) is False
    assert MainWindow._is_execution_queue_buylist_item(queued) is True


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
        buylist_view_module,
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
    window._parse_float = lambda input_widget, default=0.0: float(
        input_widget.text() or default
    )
    window._get_account_balance_for_env = lambda _env: 100000.0
    window._first_account_no_for_environment = lambda _env: "12345678"
    window._has_duplicate_open_order = lambda *args, **kwargs: False
    window._watchlist_orb_buffer_pct = lambda: 0.001
    window._watchlist_orb_signal_price = lambda _symbol: 101.0
    window._calculate_adr_percent_for_symbol = lambda _symbol: 5.0
    window._load_cached_intraday_interval = lambda *_args, **_kwargs: _intraday()
    window._latest_intraday_session = lambda frame: frame
    window.buylist_prod_positions_label = None
    window.buylist_prod_capital_label = None
    window.buylist_prod_pnl_label = None
    return window


def test_refresh_execution_queue_does_not_create_rows_from_watchlist(
    monkeypatch, tmp_path
):
    window = _build_queue_window(monkeypatch, tmp_path)

    refreshed = MainWindow.refresh_execution_queue(window, "PROD", show_log=False)

    assert refreshed == 0
    assert window.buylist_manager.items == []


def test_intentional_selected_symbol_creates_one_buylist_queue_item(
    monkeypatch, tmp_path
):
    window = _build_queue_window(monkeypatch, tmp_path)

    refreshed = MainWindow.refresh_execution_queue(
        window,
        "PROD",
        show_log=False,
        symbols=["AAPL"],
        create_missing=True,
    )

    assert refreshed == 1
    assert len(window.buylist_manager.items) == 1
    item = window.buylist_manager.get("AAPL", "PROD")
    assert item is not None
    assert item.monitoring_status == "EXECUTE_READY"
    assert item.breakout_method == "execution_queue:1m"
    assert item.entry_price == pytest.approx(101.01)
    assert not hasattr(item, "_planned_shares")
    assert item.shares_held == 0
    assert (
        window.execution_queue_manager.items[queue_key("AAPL", "PROD")].selected_window
        == "1m"
    )
    result = window._last_execution_queue_refresh_result
    assert result.refreshed == 1
    assert result.missing_symbols == []
    assert result.status_counts == {"EXECUTE_READY": 1}


def test_saved_watchlist_orb_plan_is_reapplied_to_execution_queue(
    monkeypatch, tmp_path
):
    window = _build_queue_window(monkeypatch, tmp_path)
    watch_item = window.watchlist.get("AAPL")
    watch_item.entry_price = 101.01
    watch_item.stop_loss = 98.0
    watch_item.selected_orb_plan = {
        "window": "5m",
        "risk_percent": 0.005,
        "entry_trigger": 101.01,
        "stop_price": 98.0,
        "breakout_price": 100.0,
        "buffer_pct": 0.002,
        "shares": 166,
    }

    MainWindow.refresh_execution_queue(
        window,
        "PROD",
        show_log=False,
        symbols=["AAPL"],
        create_missing=True,
    )

    queue_item = window.execution_queue_manager.get_item("AAPL", "PROD")
    candidate = queue_item.selected_candidate
    buylist_item = window.buylist_manager.get("AAPL", "PROD")
    assert queue_item.manual_window_lock is True
    assert queue_item.selected_window == "5m"
    assert candidate is queue_item.candidates["5m"]
    assert candidate.risk_percent == pytest.approx(0.005)
    assert candidate.breakout_trigger == pytest.approx(100.2)
    assert buylist_item.buffer_pct == pytest.approx(0.002)


def test_unlock_auto_clears_durable_watchlist_orb_selection(
    monkeypatch, tmp_path
):
    window = _build_queue_window(monkeypatch, tmp_path)
    save_calls = []
    window._save_state = lambda: save_calls.append(True)
    watch_item = window.watchlist.get("AAPL")
    watch_item.entry_price = 101.01
    watch_item.stop_loss = 98.0
    watch_item.selected_orb_plan = {
        "window": "5m",
        "risk_percent": 0.005,
        "entry_trigger": 101.01,
        "stop_price": 98.0,
        "breakout_price": 100.0,
        "buffer_pct": 0.002,
    }

    MainWindow.refresh_execution_queue(
        window,
        "PROD",
        show_log=False,
        symbols=["AAPL"],
        create_missing=True,
    )
    queue_item = window.execution_queue_manager.get_item("AAPL", "PROD")
    assert queue_item.manual_window_lock is True
    saves_before_unlock = len(save_calls)

    MainWindow._unlock_execution_queue_item_for_auto(window, queue_item)

    assert watch_item.selected_orb_plan is None
    assert len(save_calls) == saves_before_unlock + 1
    assert queue_item.manual_window_lock is False
    assert queue_item.locked is False

    MainWindow.refresh_execution_queue(window, "PROD", show_log=False)

    assert queue_item.manual_window_lock is False
    assert queue_item.locked is False


def test_watchlist_selected_orb_plan_round_trips_without_nonfinite_values():
    watchlist = Watchlist()
    watchlist.items.append(
        WatchlistItem(
            symbol="AAPL",
            name="Apple",
            selected_orb_plan={
                "window": "5m",
                "risk_percent": 0.005,
                "entry_trigger": float("nan"),
                "stop_price": 98.0,
                "buffer_pct": 0.001,
                "shares": 50,
            },
        )
    )

    restored = Watchlist.from_dict(watchlist.to_dict())

    assert restored.get("AAPL").selected_orb_plan == {
        "window": "5m",
        "risk_percent": 0.005,
        "stop_price": 98.0,
        "buffer_pct": 0.001,
        "shares": 50,
    }


def test_watchlist_lookup_and_remove_normalize_user_symbol_casing():
    watchlist = Watchlist()
    watchlist.add("aapl", "Apple")

    assert watchlist.get(" Aapl ").symbol == "AAPL"
    assert watchlist.remove("aApL") is True
    assert watchlist.items == []


def test_missing_selected_symbol_is_returned_in_refresh_result(monkeypatch, tmp_path):
    window = _build_queue_window(monkeypatch, tmp_path)

    refreshed = MainWindow.refresh_execution_queue(
        window,
        "PROD",
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
        "PROD",
        show_log=False,
        symbols=["AAPL"],
        create_missing=True,
    )

    queue_item = window.execution_queue_manager.items[queue_key("AAPL", "PROD")]
    assert refreshed == 1
    assert queue_item.status == ExecutionQueueStatus.REJECTED
    assert queue_item.selected_candidate is None
    assert queue_item.candidates
    assert all(
        candidate.status == OrbCandidateStatus.REJECTED
        for candidate in queue_item.candidates.values()
    )
    assert all(
        "Duplicate" in candidate.reason for candidate in queue_item.candidates.values()
    )
    assert window._last_execution_queue_refresh_result.status_counts == {"REJECTED": 1}


def test_refresh_result_status_counts_are_correct(monkeypatch, tmp_path):
    window = _build_queue_window(monkeypatch, tmp_path)

    MainWindow.refresh_execution_queue(
        window,
        "PROD",
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
        "PROD",
        show_log=False,
        symbols=["AAPL"],
        create_missing=True,
    )
    item = window.buylist_manager.get("AAPL", "PROD")
    item.monitoring_status = "ACTIVE"
    window.execution_queue_manager.items[queue_key("AAPL", "PROD")].status = (
        ExecutionQueueStatus.ORDER_SUBMITTED
    )

    assert MainWindow._buylist_dashboard_status(window, item) == "ORDER_SUBMITTED"
    assert "ORDER_SUBMITTED" in MainWindow._buylist_compute_alerts(
        window, item, 101.0, 0
    )


def test_buy_dashboard_alerts_unknown_submission_state(monkeypatch, tmp_path):
    window = _build_queue_window(monkeypatch, tmp_path)

    MainWindow.refresh_execution_queue(
        window,
        "PROD",
        show_log=False,
        symbols=["AAPL"],
        create_missing=True,
    )
    item = window.buylist_manager.get("AAPL", "PROD")
    window.execution_queue_manager.items[queue_key("AAPL", "PROD")].status = (
        ExecutionQueueStatus.UNKNOWN_SUBMISSION_STATE
    )

    alerts = MainWindow._buylist_compute_alerts(window, item, 101.0, 0)

    assert "UNKNOWN_SUBMISSION_STATE" in alerts
    assert "UNKNOWN SUBMISSION - RECONCILE BEFORE RETRY" in alerts


def test_buy_dashboard_queue_row_uses_execution_queue_candidate_values(
    monkeypatch, tmp_path
):
    window = _build_queue_window(monkeypatch, tmp_path)
    MainWindow.refresh_execution_queue(
        window,
        "PROD",
        show_log=False,
        symbols=["AAPL"],
        create_missing=True,
    )
    item = window.buylist_manager.get("AAPL", "PROD")
    item.entry_price = 1.23
    item.stop_loss = 0.45
    item.position_percent = 1.0
    table = FakeTable()
    window.buylist_prod_table = table

    MainWindow._populate_buylist_env_table(window, "PROD")

    candidate = window.execution_queue_manager.items[
        queue_key("AAPL", "PROD")
    ].selected_candidate
    assert table.item(0, 4).text() == f"{candidate.entry_trigger:.2f}"
    assert table.item(0, 6).text() == f"{candidate.stop_loss:.2f}"
    assert table.item(0, 9).text() == str(candidate.shares)
    assert table.item(0, 10).text() == f"{candidate.capital_percent:.1f}%"
    assert "Qty 1" not in table.item(0, 12).text()


def test_buy_dashboard_queue_monitor_column_shows_active_watching_item(
    monkeypatch, tmp_path
):
    window = _build_queue_window(monkeypatch, tmp_path)
    MainWindow.refresh_execution_queue(
        window,
        "PROD",
        show_log=False,
        symbols=["AAPL"],
        create_missing=True,
    )
    item = window.buylist_manager.get("AAPL", "PROD")
    item.monitoring_status = "WATCHING"
    item.orb_monitor_enabled = True
    window._buylist_prod_monitor_active = True
    table = FakeTable()
    window.buylist_prod_table = table

    MainWindow._populate_buylist_env_table(window, "PROD")

    assert table.item(0, 3).text() == "ON"


def test_buy_dashboard_uses_production_queue_item(monkeypatch, tmp_path):
    window = _build_queue_window(monkeypatch, tmp_path)
    MainWindow.refresh_execution_queue(
        window,
        "PROD",
        show_log=False,
        symbols=["AAPL"],
        create_missing=True,
    )

    prod_queue = window.execution_queue_manager.get_item("AAPL", "PROD")
    prod_queue.selected_candidate.entry_trigger = 222.22
    prod_queue.selected_candidate.shares = 22
    prod_queue.selected_candidate.capital_percent = 22.0

    window.buylist_manager.get("AAPL", "PROD").entry_price = 2.0
    prod_table = FakeTable()
    window.buylist_prod_table = prod_table

    MainWindow._populate_buylist_env_table(window, "PROD")

    assert prod_table.item(0, 4).text() == "222.22"
    assert prod_table.item(0, 9).text() == "22"
    assert prod_table.item(0, 10).text() == "22.0%"


def test_buy_dashboard_queue_row_falls_back_to_buylist_when_queue_missing(
    monkeypatch, tmp_path
):
    window = _build_queue_window(monkeypatch, tmp_path)
    MainWindow.refresh_execution_queue(
        window,
        "PROD",
        show_log=False,
        symbols=["AAPL"],
        create_missing=True,
    )
    item = window.buylist_manager.get("AAPL", "PROD")
    item.entry_price = 12.34
    item.stop_loss = 11.11
    item.position_percent = 3.4
    del window.execution_queue_manager.items[queue_key("AAPL", "PROD")]
    table = FakeTable()
    window.buylist_prod_table = table

    MainWindow._populate_buylist_env_table(window, "PROD")

    assert table.item(0, 4).text() == "12.34"
    assert table.item(0, 6).text() == "11.11"
    assert table.item(0, 9).text() == "-"
    assert table.item(0, 10).text() == "3.4%"


def test_buy_dashboard_bought_row_uses_position_values_not_queue_projection(
    monkeypatch, tmp_path
):
    window = _build_queue_window(monkeypatch, tmp_path)
    MainWindow.refresh_execution_queue(
        window,
        "PROD",
        show_log=False,
        symbols=["AAPL"],
        create_missing=True,
    )
    item = window.buylist_manager.get("AAPL", "PROD")
    item.monitoring_status = "BOUGHT"
    item.status = "BOUGHT"
    item.shares_held = 12
    item.avg_cost = 100.0
    item.position_percent = 33.3
    item.entry_price = 1.23
    item.stop_loss = 90.0
    window.latest_intraday_prices = {"AAPL": 110.0}
    table = FakeTable()
    window.buylist_prod_table = table

    MainWindow._populate_buylist_env_table(window, "PROD")

    assert table.item(0, 2).text() == "BOUGHT"
    assert table.item(0, 7).text() == "110.00"
    assert table.item(0, 8).text() == "+10.0%"
    assert table.item(0, 9).text() == "12"
    assert table.item(0, 10).text() == "33.3%"


def test_queue_order_review_uses_selected_candidate_values(monkeypatch, tmp_path):
    window = _build_queue_window(monkeypatch, tmp_path)
    MainWindow.refresh_execution_queue(
        window,
        "PROD",
        show_log=False,
        symbols=["AAPL"],
        create_missing=True,
    )
    item = window.buylist_manager.get("AAPL", "PROD")
    item.entry_price = 1.23
    queue_item = window.execution_queue_manager.items[queue_key("AAPL", "PROD")]
    queue_item.selected_candidate.entry_trigger = 123.45
    queue_item.selected_candidate.shares = 7

    review = MainWindow._format_execution_queue_order_review(
        window, "PROD", item, queue_item
    )

    assert "Limit price: $123.45" in review
    assert "Quantity: 7" in review
    assert "Limit price: $1.23" not in review


def test_legacy_active_row_is_blocked_once_before_auto_buy(monkeypatch, tmp_path):
    window = _build_queue_window(monkeypatch, tmp_path)
    logs = []
    submissions = []
    item = SimpleNamespace(
        symbol="AAPL",
        environment="PROD",
        monitoring_status="ACTIVE",
        breakout_method="manual_trendline",
        breakout_price=0.0,
        buffer_pct=0.001,
        entry_price=100.1,
        stop_loss=98.0,
        _buy_order_pending=False,
    )
    window.buylist_manager = SimpleNamespace(items=[item])
    window.latest_intraday_prices = {"AAPL": 101.0}
    window.append_log = logs.append
    window._buylist_refresh_item_data = lambda _item: None
    window._populate_buylist_env_table = lambda _env: None
    window._submit_kis_buy_order = lambda *_args, **_kwargs: submissions.append(True)

    MainWindow._run_buylist_monitor_cycle(window, "PROD")
    MainWindow._run_buylist_monitor_cycle(window, "PROD")

    assert submissions == []
    assert item._buy_order_pending is False
    matching_logs = [
        message for message in logs if "skipping legacy ACTIVE auto-buy" in message
    ]
    assert len(matching_logs) == 1
    assert "only EXECUTE_READY execution-queue" in matching_logs[0]


def _healthy_buyboard_worker():
    """A fake BuyboardRuntimeWorker reporting confirmed health, for tests
    exercising legacy-suppression behavior once the new engine is
    genuinely up (review finding: suppression must be heartbeat/health-
    backed, not only the BUYBOARD_ENGINE_ENABLED flag)."""
    return SimpleNamespace(
        isRunning=lambda: True,
        startup_reconciliation_ran=True,
        startup_reconciliation_complete=True,
        startup_reconciliation_errors={},
        startup_reconciled_accounts={"1"},
        account_action_ready=lambda account_no, symbol, action: True,
        last_heartbeat_at=dt.datetime.now(dt.timezone.utc),
    )


def test_stop_hit_auto_sell_is_suppressed_when_buyboard_engine_enabled(
    monkeypatch, tmp_path
):
    """Mutual-exclusion guard (buydashboard_to_kanban.md review): once the
    new Buy Board engine owns execution, the legacy 60-second monitor must
    never also submit a stop-loss SELL for the same position."""
    window = _build_queue_window(monkeypatch, tmp_path)
    monkeypatch.setenv("BUYBOARD_ENGINE_ENABLED", "true")
    window._buyboard_runtime_worker = _healthy_buyboard_worker()
    logs = []
    submissions = []
    item = SimpleNamespace(
        symbol="AAPL",
        environment="PROD",
        kis_account_no="1",
        monitoring_status="BOUGHT",
        breakout_method="",
        stop_loss=98.0,
        shares_held=10,
        auto_order_block_reason="",
        _stop_order_pending=False,
        _exit_order_pending=False,
        _auto_order_block_notice_logged=False,
    )
    window.buylist_manager = SimpleNamespace(items=[item])
    window.latest_intraday_prices = {"AAPL": 90.0}  # below the 98.0 stop
    window.append_log = logs.append
    window._buylist_refresh_item_data = lambda _item: None
    window._populate_buylist_env_table = lambda _env: None
    window._submit_kis_sell_order = lambda *_args, **_kwargs: submissions.append(True)

    MainWindow._run_buylist_monitor_cycle(window, "PROD")

    assert submissions == []
    assert item._stop_order_pending is False
    assert any(
        "Legacy automatic order submission is suppressed" in message
        for message in logs
    )


def test_auto_submit_execute_ready_is_suppressed_when_buyboard_engine_enabled(
    monkeypatch, tmp_path
):
    window = _build_queue_window(monkeypatch, tmp_path)
    monkeypatch.setenv("BUYBOARD_ENGINE_ENABLED", "true")
    window._buyboard_runtime_worker = _healthy_buyboard_worker()
    logs = []
    submissions = []
    item = SimpleNamespace(
        symbol="AAPL",
        environment="PROD",
        kis_account_no="1",
        monitoring_status="EXECUTE_READY",
        breakout_method="execution_queue:1m",
        orb_monitor_enabled=True,
        _buy_order_pending=False,
    )
    window._buylist_prod_monitor_active = True
    window.buylist_manager = SimpleNamespace(items=[item])
    window.append_log = logs.append
    window._submit_kis_buy_order = lambda *_args, **_kwargs: submissions.append(True)

    MainWindow._auto_submit_execute_ready_queue_items(window, "PROD")

    assert submissions == []
    assert any(
        "Legacy automatic order submission is suppressed" in message
        for message in logs
    )


def test_stop_hit_auto_sell_stays_active_when_buyboard_engine_flag_on_but_unhealthy(
    monkeypatch, tmp_path
):
    """Review finding P0: "legacy execution suppression depends only on
    the feature flag" -- if the new engine has stopped/failed while the
    flag stays on, the legacy monitor must fail OPEN (keep protecting)
    rather than also going dark, and must raise a critical alert."""
    window = _build_queue_window(monkeypatch, tmp_path)
    monkeypatch.setenv("BUYBOARD_ENGINE_ENABLED", "true")
    window._buyboard_runtime_worker = None  # never started / already stopped
    logs = []
    submissions = []
    item = SimpleNamespace(
        symbol="AAPL",
        environment="PROD",
        monitoring_status="BOUGHT",
        breakout_method="",
        stop_loss=98.0,
        shares_held=10,
        auto_order_block_reason="",
        _stop_order_pending=False,
        _exit_order_pending=False,
        _auto_order_block_notice_logged=False,
    )
    window.buylist_manager = SimpleNamespace(items=[item])
    window.latest_intraday_prices = {"AAPL": 90.0}  # below the 98.0 stop
    window.append_log = logs.append
    window._buylist_refresh_item_data = lambda _item: None
    window._populate_buylist_env_table = lambda _env: None
    window._submit_kis_sell_order = lambda *_args, **_kwargs: submissions.append(True)

    MainWindow._run_buylist_monitor_cycle(window, "PROD")

    # Fails open: the legacy monitor still protects the position.
    assert submissions == [True]
    assert any("CRITICAL" in message for message in logs)
    assert any(
        "Buy Board engine is not confirmed healthy" in message for message in logs
    )


def test_one_accounts_startup_failure_does_not_reopen_legacy_exits_for_another_account(
    monkeypatch, tmp_path
):
    """Review finding P0: "partial account failure still creates
    cross-account dual execution" -- account 1 reconciled fine and is
    actively owned by a healthy Buy Board; account 2's startup failure
    must not also fail legacy protective exits open for account 1's
    position, giving it two simultaneously-active exit engines."""
    window = _build_queue_window(monkeypatch, tmp_path)
    monkeypatch.setenv("BUYBOARD_ENGINE_ENABLED", "true")
    window._buyboard_runtime_worker = SimpleNamespace(
        isRunning=lambda: True,
        startup_reconciliation_ran=True,
        startup_reconciliation_complete=False,  # account "2" failed
        startup_reconciliation_errors={"2": "simulated KIS outage"},
        startup_reconciled_accounts={"1"},
        account_action_ready=lambda account_no, symbol, action: account_no == "1",
        last_heartbeat_at=dt.datetime.now(dt.timezone.utc),
    )
    logs = []
    submissions = []
    account_1_item = SimpleNamespace(
        symbol="AAPL",
        environment="PROD",
        kis_account_no="1",  # reconciled fine
        monitoring_status="BOUGHT",
        breakout_method="",
        stop_loss=98.0,
        shares_held=10,
        auto_order_block_reason="",
        _stop_order_pending=False,
        _exit_order_pending=False,
        _auto_order_block_notice_logged=False,
    )
    window.buylist_manager = SimpleNamespace(items=[account_1_item])
    window.latest_intraday_prices = {"AAPL": 90.0}  # below the 98.0 stop
    window.append_log = logs.append
    window._buylist_refresh_item_data = lambda _item: None
    window._populate_buylist_env_table = lambda _env: None
    window._submit_kis_sell_order = lambda *_args, **_kwargs: submissions.append(True)

    MainWindow._run_buylist_monitor_cycle(window, "PROD")

    # Account 1's own Buy Board ownership is healthy -- legacy must stay
    # suppressed for it, even though account 2 is broken elsewhere.
    assert submissions == []


def test_auto_submit_execute_ready_stays_blocked_when_buyboard_engine_flag_on_but_unhealthy(
    monkeypatch, tmp_path
):
    """Review finding P0: unlike protective exits, a *new automatic entry*
    must stay blocked on the legacy side too while the Buy Board engine's
    health is uncertain -- starting a fresh position off an engine whose
    true state is unknown is never safe, even as a fail-safe."""
    window = _build_queue_window(monkeypatch, tmp_path)
    monkeypatch.setenv("BUYBOARD_ENGINE_ENABLED", "true")
    window._buyboard_runtime_worker = None  # never started / already stopped
    logs = []
    submissions = []
    item = SimpleNamespace(
        symbol="AAPL",
        environment="PROD",
        monitoring_status="EXECUTE_READY",
        breakout_method="execution_queue:1m",
        orb_monitor_enabled=True,
        _buy_order_pending=False,
    )
    window._buylist_prod_monitor_active = True
    window.buylist_manager = SimpleNamespace(items=[item])
    window.append_log = logs.append
    window._submit_kis_buy_order = lambda *_args, **_kwargs: submissions.append(True)

    MainWindow._auto_submit_execute_ready_queue_items(window, "PROD")

    assert submissions == []  # entries stay frozen on both engines
    assert any("CRITICAL" in message for message in logs)


def test_buyboard_engine_healthy_true_for_a_recently_started_slow_cycle(tmp_path):
    """Review finding P0: last_heartbeat_at only updates on *completion* --
    a single cycle performing several sequential KIS calls can legitimately
    run long while the worker is demonstrably still active. A recent
    last_cycle_started_at must be enough, even with a stale last_heartbeat_at.
    """
    window = MainWindow.__new__(MainWindow)
    now = dt.datetime.now(dt.timezone.utc)
    window._buyboard_runtime_worker = SimpleNamespace(
        isRunning=lambda: True,
        startup_reconciliation_ran=True,
        startup_reconciliation_complete=True,
        startup_reconciliation_errors={},
        last_cycle_started_at=now - dt.timedelta(seconds=5),  # cycle in flight, still recent
        last_heartbeat_at=now - dt.timedelta(seconds=40),  # last *completed* cycle is old
    )

    assert window._buyboard_engine_healthy() is True


def test_buyboard_engine_healthy_false_when_cycle_start_itself_is_stale(tmp_path):
    window = MainWindow.__new__(MainWindow)
    now = dt.datetime.now(dt.timezone.utc)
    window._buyboard_runtime_worker = SimpleNamespace(
        isRunning=lambda: True,
        startup_reconciliation_ran=True,
        startup_reconciliation_complete=True,
        startup_reconciliation_errors={},
        last_cycle_started_at=now - dt.timedelta(seconds=90),
        last_heartbeat_at=now - dt.timedelta(seconds=90),
    )

    assert window._buyboard_engine_healthy() is False


def test_buyboard_engine_healthy_false_with_startup_reconciliation_errors(tmp_path):
    """Review finding P0: startup_reconciliation_complete alone is not
    enough -- an account-level failure recorded in
    startup_reconciliation_errors must also fail the global (no
    account_no) health check."""
    window = MainWindow.__new__(MainWindow)
    now = dt.datetime.now(dt.timezone.utc)
    window._buyboard_runtime_worker = SimpleNamespace(
        isRunning=lambda: True,
        startup_reconciliation_ran=True,
        startup_reconciliation_complete=False,
        startup_reconciliation_errors={"2": "simulated KIS outage"},
        last_cycle_started_at=now,
        last_heartbeat_at=now,
    )

    assert window._buyboard_engine_healthy() is False


def test_buyboard_engine_healthy_false_before_startup_reconciliation_ever_ran(tmp_path):
    """A worker that hasn't completed even one startup pass yet must not
    be treated as healthy just because its (empty-by-default) errors dict
    happens to be empty."""
    window = MainWindow.__new__(MainWindow)
    now = dt.datetime.now(dt.timezone.utc)
    window._buyboard_runtime_worker = SimpleNamespace(
        isRunning=lambda: True,
        startup_reconciliation_ran=False,
        startup_reconciliation_complete=False,
        startup_reconciliation_errors={},
        last_cycle_started_at=now,
        last_heartbeat_at=now,
    )

    assert window._buyboard_engine_healthy() is False


def test_buyboard_engine_healthy_is_account_scoped(tmp_path):
    """Review finding P0: "partial account failure still creates
    cross-account dual execution" -- account 2's startup failure must not
    make account 1 (which reconciled fine) look unhealthy too."""
    window = MainWindow.__new__(MainWindow)
    now = dt.datetime.now(dt.timezone.utc)
    window._buyboard_runtime_worker = SimpleNamespace(
        isRunning=lambda: True,
        startup_reconciliation_ran=True,
        startup_reconciliation_complete=False,
        startup_reconciliation_errors={"2": "simulated KIS outage"},
        startup_reconciled_accounts={"1"},
        last_cycle_started_at=now,
        last_heartbeat_at=now,
    )

    assert window._buyboard_engine_healthy(account_no="1") is True
    assert window._buyboard_engine_healthy(account_no="2") is False
    assert window._buyboard_engine_healthy() is False  # global check still fails closed


def test_buyboard_engine_health_uses_action_specific_reconciliation_readiness(tmp_path):
    window = MainWindow.__new__(MainWindow)
    now = dt.datetime.now(dt.timezone.utc)
    window._buyboard_runtime_worker = SimpleNamespace(
        isRunning=lambda: True,
        startup_reconciliation_ran=True,
        startup_reconciliation_complete=False,
        startup_reconciliation_errors={"1": "balance and reserved history unavailable"},
        startup_reconciled_accounts=set(),
        account_action_ready=lambda account_no, symbol, action: (
            account_no == "1" and symbol == "AAPL" and action == "PROTECTIVE_EXIT"
        ),
        last_cycle_started_at=now,
        last_heartbeat_at=now,
    )

    assert window._buyboard_engine_healthy(
        account_no="1", action="PROTECTIVE_EXIT", symbol="AAPL"
    ) is True
    assert window._buyboard_engine_healthy(
        account_no="1", action="NEW_ENTRY", symbol="AAPL"
    ) is False


def test_buyboard_engine_healthy_false_for_an_account_never_positively_confirmed(tmp_path):
    """Review finding P0: "unknown accounts can be incorrectly considered
    healthy" -- an account absent from startup_reconciliation_errors but
    ALSO never actually reconciled (a stale/unconfigured kis_account_no,
    or one genuinely not reached yet) must not be treated as healthy just
    because nothing marked it as failed either."""
    window = MainWindow.__new__(MainWindow)
    now = dt.datetime.now(dt.timezone.utc)
    window._buyboard_runtime_worker = SimpleNamespace(
        isRunning=lambda: True,
        startup_reconciliation_ran=True,
        startup_reconciliation_complete=True,
        startup_reconciliation_errors={},
        startup_reconciled_accounts={"1"},  # "9" was never discovered/processed
        last_cycle_started_at=now,
        last_heartbeat_at=now,
    )

    assert window._buyboard_engine_healthy(account_no="9") is False
    assert window._buyboard_engine_healthy(account_no="") is False  # blank fails safely too


# --- Critical notification (review: "a card warning is insufficient when --
# --- the user is asleep") ----------------------------------------------------


def test_ensure_critical_tray_icon_is_a_safe_no_op_on_an_uninitialized_window():
    """MainWindow.__new__(MainWindow) test doubles never ran __init__, so
    their Qt C++ base was never constructed -- touching QSystemTrayIcon/
    windowIcon()/style() on one is undefined behavior in PyQt5 (this has
    been observed to crash the process outright, not raise a catchable
    exception). _qt_base_initialized must gate this before any Qt call."""
    window = MainWindow.__new__(MainWindow)
    assert "_qt_base_initialized" not in window.__dict__

    assert window._ensure_critical_tray_icon() is None


def test_show_critical_notification_still_logs_without_a_tray_icon():
    window = MainWindow.__new__(MainWindow)
    logs = []
    window.append_log = logs.append

    window._show_critical_notification("Test Alert", "something is wrong")

    assert logs == ["something is wrong"]
