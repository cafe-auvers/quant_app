from types import SimpleNamespace

import pytest

from src.risk.position_sizer import PositionSizer
from src.ui.main_window import MainWindow
from src.ui.mixins.dashboard_mixin import DashboardMixin


class _TextInput:
    def __init__(self, text: str):
        self._text = text

    def text(self):
        return self._text

    def setText(self, text: str):
        self._text = text

    def blockSignals(self, _blocked: bool):
        return False


class _Label:
    def __init__(self):
        self.text = ""

    def setText(self, text: str):
        self.text = text


class _Combo:
    def __init__(self, *, text: str = "", data=None):
        self._text = text
        self._data = data

    def currentText(self):
        return self._text

    def currentData(self):
        return self._data


def test_numeric_parsers_reject_nonfinite_and_negative_values():
    window = MainWindow.__new__(MainWindow)

    for text in ("nan", "inf", "-inf", "-1"):
        assert MainWindow._parse_float(window, _TextInput(text), 2.5) == 2.5


def test_dashboard_snapshot_callback_keeps_the_profile_that_started_request():
    stored_syncs = []
    requested = {
        "environment": "PROD",
        "account_no": "11111111-01",
        "label": "PROD 11******-01",
    }
    window = SimpleNamespace(
        kis_account_snapshots={},
        _schedule_kis_refresh_button_enable=lambda: None,
        sync_buylist_positions_from_kis_snapshots=lambda snapshots: stored_syncs.append(
            snapshots
        ),
        kis_account_status_label=SimpleNamespace(setText=lambda _text: None),
        usd_krw_rate_input=_TextInput("1450.00"),
        _parse_float=lambda _input, default: default,
        kis_account_summary_label=SimpleNamespace(setText=lambda _text: None),
        _format_kis_snapshot_summary=lambda snapshot, fx_rate=0.0: "summary",
        populate_kis_holdings_table=lambda _holdings: None,
        _flatten_kis_holdings=lambda _snapshot: [],
        apply_cached_trade_account_size=lambda: None,
        # _on_kis_snapshot_finished now also refreshes FX (which itself calls
        # apply_cached_trade_account_size once the fresh rate lands), so a
        # single click on "Refresh KIS Snapshot" leaves sizing ready too.
        refresh_usd_krw_rate=lambda show_messages=False: None,
        append_log=lambda _message: None,
        reconcile_open_orders=lambda: None,
    )

    DashboardMixin._on_kis_snapshot_finished(window, {"account": "ignored"}, requested)

    key = ("PROD", "11111111-01")
    assert key in window.kis_account_snapshots
    assert stored_syncs == [{key: {"account": "ignored"}}]


def test_live_fx_callback_updates_watchlist_and_dashboard_snapshot():
    profile = {
        "environment": "PROD",
        "account_no": "11111111-01",
        "label": "PROD 11******-01",
    }
    snapshot = {
        "environment": "PROD",
        "account": "11******-01",
        "domestic": {"summary": {"cash_total_krw": 1_450_000}},
    }
    window = MainWindow.__new__(MainWindow)
    window.usd_krw_rate_input = _TextInput("")
    window.usd_krw_rate_status_label = _Label()
    window.kis_environment_combo = _Combo(text="PROD")
    window.kis_account_combo = _Combo(data=profile)
    window.kis_account_snapshots = {("PROD", "11111111-01"): snapshot}
    window.kis_account_summary_label = _Label()
    window.append_log = lambda _message: None
    window.apply_cached_trade_account_size = lambda: None

    MainWindow._on_usd_krw_rate_finished(
        window, 1450.0, "yfinance KRW=X", "2026-08-09 12:00"
    )

    assert window.usd_krw_rate_input.text() == "1450.00"
    assert "1450.00" in window.usd_krw_rate_status_label.text
    assert "@ 1450.00 KRW/USD" in window.kis_account_summary_label.text


def test_kis_account_conversion_clears_stale_size_without_live_fx_rate():
    profile = {"environment": "PROD", "account_no": "11111111-01"}
    account_input = _TextInput("25000.00")
    logs = []
    window = SimpleNamespace(
        trade_kis_environment_combo=_Combo(text="PROD"),
        trade_kis_account_combo=_Combo(data=profile),
        account_size_input=account_input,
        usd_krw_rate_input=_TextInput(""),
        kis_account_snapshots={
            ("PROD", "11111111-01"): {
                "domestic": {"summary": {"total_evaluation_krw": 10_000_000}}
            }
        },
        _parse_float=lambda input_widget, default: (
            float(input_widget.text()) if input_widget.text() else default
        ),
        append_log=logs.append,
        update_trade_plan_feedback=lambda: None,
        recalculate_watchlist_scoreboard_sizes=lambda: None,
    )

    DashboardMixin.apply_cached_trade_account_size(window)

    assert account_input.text() == ""
    assert logs == [
        "KIS position sizing unavailable for PROD: live USD/KRW rate is unavailable."
    ]
    assert window._execution_queue_account_sizing_dirty is True


def test_account_size_projection_does_not_reload_market_data_on_ui_thread():
    profile = {"environment": "PROD", "account_no": "11111111-01"}
    account_input = _TextInput("25000.00")
    refresh_calls = []
    window = SimpleNamespace(
        trade_kis_environment_combo=_Combo(text="PROD"),
        trade_kis_account_combo=_Combo(data=profile),
        account_size_input=account_input,
        usd_krw_rate_input=_TextInput(""),
        kis_account_snapshots={},
        _parse_float=lambda input_widget, default: (
            float(input_widget.text()) if input_widget.text() else default
        ),
        append_log=lambda _message: None,
        refresh_execution_queue=lambda *args, **kwargs: refresh_calls.append(
            (args, kwargs)
        ),
    )

    DashboardMixin.apply_cached_trade_account_size(window)

    assert refresh_calls == []
    assert window._execution_queue_account_sizing_dirty is True


def test_kis_account_conversion_clears_stale_size_without_snapshot():
    profile = {"environment": "PROD", "account_no": "11111111-01"}
    account_input = _TextInput("25000.00")
    logs = []
    window = SimpleNamespace(
        trade_kis_environment_combo=_Combo(text="PROD"),
        trade_kis_account_combo=_Combo(data=profile),
        account_size_input=account_input,
        usd_krw_rate_input=_TextInput("1450.00"),
        kis_account_snapshots={},
        _parse_float=lambda input_widget, default: (
            float(input_widget.text()) if input_widget.text() else default
        ),
        append_log=logs.append,
        update_trade_plan_feedback=lambda: None,
        recalculate_watchlist_scoreboard_sizes=lambda: None,
    )

    DashboardMixin.apply_cached_trade_account_size(window)

    assert account_input.text() == ""
    assert logs == [
        "KIS position sizing unavailable for PROD: "
        "snapshot not loaded for (PROD, 11111111-01)."
    ]


def test_configured_kis_account_balance_does_not_use_stale_widget_without_fx():
    profile = {
        "environment": "PROD",
        "account_no": "11111111-01",
    }
    window = MainWindow.__new__(MainWindow)
    window.trade_kis_account_combo = _Combo(data=profile)
    window.account_size_input = _TextInput("25000.00")
    window.usd_krw_rate_input = _TextInput("")
    window.kis_account_snapshots = {
        ("PROD", "11111111-01"): {
            "domestic": {"summary": {"total_evaluation_krw": 10_000_000}}
        }
    }

    assert MainWindow._get_account_balance_for_env(window, "PROD") == 0.0


def test_kis_account_value_rejects_nonfinite_broker_totals():
    snapshot = {
        "domestic": {"summary": {"total_evaluation_krw": float("inf")}},
        "overseas": {"frcr_evlu_tota_krw": "not-a-number"},
    }

    assert MainWindow._extract_kis_account_value_krw(snapshot, fx_rate=1450.0) is None


def test_kis_account_value_includes_us_stocks_in_entire_capital_sizing():
    fx_rate = 1450.0
    snapshot = {
        "domestic": {
            "summary": {
                # $2,000 cash converted to KRW.
                "cash_total_krw": 2_900_000,
                "total_evaluation_krw": 2_900_000,
            }
        },
        "overseas": {
            "holdings": [
                {
                    "symbol": "AAPL",
                    "quantity": 80,
                    "current_price": 100,
                    # Some KIS responses omit the explicit evaluation field;
                    # quantity * current price must still value the holding.
                    "evaluation_amount": 0,
                }
            ],
        },
    }

    breakdown = MainWindow._extract_kis_account_value_krw(
        snapshot, fx_rate=fx_rate, return_breakdown=True
    )

    assert breakdown["total_krw"] == pytest.approx(14_500_000)
    assert breakdown["ovrs_stock_usd"] == pytest.approx(8_000)
    account_value_usd = breakdown["total_krw"] / fx_rate
    assert account_value_usd == pytest.approx(10_000)
    summary = MainWindow._format_kis_snapshot_summary(snapshot, fx_rate=fx_rate)
    assert "US stocks: $8,000.00" in summary
    assert "Total (est.): 14,500,000 KRW = $10,000.00 USD" in summary

    sizing = PositionSizer(account_value_usd).size_fixed_percent(
        entry_price=100, percent=0.20
    )
    assert sizing.dollar_amount == pytest.approx(2_000)


def test_kis_foreign_total_adds_usd_cash_to_us_stock_value():
    snapshot = {
        "overseas": {
            # $8,000 stock plus $2,000 USD cash, converted by KIS to KRW.
            "frcr_evlu_tota_krw": 14_500_000,
            "tot_asst_krw": 14_500_000,
            "holdings": [
                {
                    "symbol": "AAPL",
                    "evaluation_amount": 8_000,
                }
            ],
        }
    }

    breakdown = MainWindow._extract_kis_account_value_krw(
        snapshot, fx_rate=1450.0, return_breakdown=True
    )

    assert breakdown["total_krw"] == pytest.approx(14_500_000)
    assert breakdown["ovrs_stock_usd"] == pytest.approx(8_000)
    assert breakdown["ovrs_cash_usd"] == pytest.approx(2_000)


def test_kis_account_value_uses_overseas_summary_when_holding_value_is_missing():
    snapshot = {
        "domestic": {"summary": {"cash_total_krw": 2_900_000}},
        "overseas": {
            "holdings": [{"symbol": "AAPL", "quantity": 80}],
            "summary_by_exchange": {
                "NASD": {"foreign_stock_evaluation": 8_000},
                # Repeated global totals must not be double-counted.
                "NYSE": {"foreign_stock_evaluation": 8_000},
            },
        },
    }

    breakdown = MainWindow._extract_kis_account_value_krw(
        snapshot, fx_rate=1450.0, return_breakdown=True
    )

    assert breakdown["ovrs_stock_usd"] == pytest.approx(8_000)
    assert breakdown["total_krw"] == pytest.approx(14_500_000)


@pytest.mark.parametrize("fx_rate", [0.0, float("inf")])
def test_snapshot_summary_marks_sizing_unavailable_without_valid_fx(fx_rate):
    snapshot = {
        "environment": "PROD",
        "account": "11******-01",
        "domestic": {"summary": {"total_evaluation_krw": 10_000_000}},
    }

    summary = MainWindow._format_kis_snapshot_summary(snapshot, fx_rate=fx_rate)

    assert "Position sizing: unavailable until USD/KRW is refreshed." in summary


def test_trade_snapshot_callback_keeps_the_profile_that_started_request():
    requested = {
        "environment": "PROD",
        "account_no": "11111111-01",
        "label": "PROD 11******-01",
    }
    stored_syncs = []
    window = SimpleNamespace(
        kis_account_snapshots={},
        sync_buylist_positions_from_kis_snapshots=lambda snapshots: stored_syncs.append(
            snapshots
        ),
        apply_cached_trade_account_size=lambda: None,
        refresh_usd_krw_rate=lambda show_messages=False: None,
        append_log=lambda _message: None,
        reconcile_open_orders=lambda: None,
    )

    DashboardMixin._on_trade_account_snapshot_finished(
        window, {"account": "ignored"}, requested
    )

    key = ("PROD", "11111111-01")
    assert key in window.kis_account_snapshots
    assert stored_syncs == [{key: {"account": "ignored"}}]


def test_order_worker_tracking_keeps_prior_worker_until_it_finishes():
    class Signal:
        def __init__(self):
            self.callbacks = []

        def connect(self, callback):
            self.callbacks.append(callback)

        def emit(self):
            for callback in list(self.callbacks):
                callback()

    class Worker:
        def __init__(self):
            self.finished = Signal()

    window = MainWindow.__new__(MainWindow)
    first = Worker()
    second = Worker()

    window.kis_order_worker = first
    MainWindow._track_buylist_order_worker(window, first)
    window.kis_order_worker = second
    MainWindow._track_buylist_order_worker(window, second)

    assert window._buylist_order_workers == [first, second]
    first.finished.emit()
    assert window._buylist_order_workers == [second]
    assert window.kis_order_worker is second


def test_buylist_item_cannot_be_submitted_from_a_different_selected_account():
    class Combo:
        def currentData(self):
            return {"environment": "PROD", "account_no": "22222222-01"}

    window = MainWindow.__new__(MainWindow)
    window.trade_kis_account_combo = Combo()
    item = SimpleNamespace(kis_account_no="11111111-01")

    assert MainWindow._selected_order_account_for_item(window, item, "PROD") is None
