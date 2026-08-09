from types import SimpleNamespace

import pytest

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

    window.watchlist_buffer_pct_input = _TextInput("-0.10")
    assert MainWindow._watchlist_orb_buffer_pct(window) == pytest.approx(0.001)
    window.watchlist_buffer_pct_input = _TextInput("nan")
    assert MainWindow._watchlist_orb_buffer_pct(window) == pytest.approx(0.001)
    window.watchlist_buffer_pct_input = _TextInput("inf")
    assert MainWindow._watchlist_orb_buffer_pct(window) == pytest.approx(0.001)


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


def test_kis_account_conversion_waits_for_live_fx_rate():
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
    )

    DashboardMixin.apply_cached_trade_account_size(window)

    assert account_input.text() == "25000.00"
    assert logs == [
        "KIS account conversion deferred until a live USD/KRW rate is available."
    ]


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
