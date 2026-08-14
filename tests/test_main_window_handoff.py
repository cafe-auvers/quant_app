"""Tests for MainWindow's automatic cross-machine main-device handoff wiring.

Follows the existing MainWindow.__new__(MainWindow) + hand-set-attributes
pattern used throughout tests/test_state_sync.py and
tests/test_pc_runtime_status.py -- these are unit tests of the handoff
control flow itself, not integration tests of the real Qt/broker stack.
"""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest
from sqlalchemy.pool import NullPool
from sqlalchemy import create_engine

import src.services.trading_state as trading_state
import src.ui.main_window as main_window_module
from src.services import app_state
from src.services import state_sync as ss
from src.services.app_state import StateReconcileResult
from src.services.execution_authority import ExecutionAuthority, LeaseHandle
from src.services.handoff_reconciliation import PostClaimReconciliationResult
from src.ui.main_window import MainWindow


@pytest.fixture(autouse=True)
def _reset_trading_kill_switch():
    trading_state.reset_trading_state_for_tests()
    yield
    trading_state.reset_trading_state_for_tests()


class _SignalStub:
    def __init__(self):
        self.callbacks = []

    def connect(self, callback):
        self.callbacks.append(callback)

    def emit(self, *args):
        for callback in self.callbacks:
            callback(*args)


class _ButtonStub:
    def __init__(self):
        self.checked = False
        self.signals_blocked = False
        self.enabled = True
        self.text = ""
        self.tooltip = ""
        self.stylesheet = ""

    def blockSignals(self, value):
        self.signals_blocked = value

    def setChecked(self, value):
        self.checked = value

    def setText(self, value):
        self.text = value

    def setToolTip(self, value):
        self.tooltip = value

    def setEnabled(self, value):
        self.enabled = value

    def setStyleSheet(self, value):
        self.stylesheet = value


def _make_engine(tmp_path):
    return create_engine(
        f"sqlite:///{tmp_path / 'shared.db'}", future=True, poolclass=NullPool
    )


def _base_window(*, is_main=False, lease_token="", pc_engine=None, db_ready=True):
    window = MainWindow.__new__(MainWindow)
    window.pc_db_engine = pc_engine
    window._pc_database_ready = db_ready
    window.state_sync_role = ss.LocalDeviceRole(
        "pc-id" if not is_main else "laptop-id", "TESTHOST", is_main
    )
    window._current_lease_token = lease_token
    window._last_successful_reconcile_at = None
    window._auto_claim_main_enabled = False
    window._auto_arm_trading_on_handoff = False
    window.handoff_reconciliation_worker = None
    window._handoff_generation = 0
    window._state_sync_auto_claim = False
    window.state_sync_worker = None
    window.state_save_manager = SimpleNamespace(_is_main_device=is_main)
    logs = []
    window.append_log = logs.append
    window._logs = logs
    return window


# --- _current_execution_lease_kwargs --------------------------------------


def test_lease_kwargs_all_none_when_role_missing():
    window = MainWindow.__new__(MainWindow)
    kwargs = MainWindow._current_execution_lease_kwargs(window)
    assert kwargs == {
        "execution_authority": None,
        "execution_lease": None,
        "lease_engine": None,
    }


def test_lease_kwargs_all_none_when_not_main():
    window = _base_window(is_main=False, lease_token="tok-1")
    kwargs = MainWindow._current_execution_lease_kwargs(window)
    assert kwargs["execution_authority"] is None
    assert kwargs["execution_lease"] is None


def test_lease_kwargs_all_none_when_main_but_no_token():
    window = _base_window(is_main=True, lease_token="")
    kwargs = MainWindow._current_execution_lease_kwargs(window)
    assert kwargs["execution_lease"] is None


def test_lease_kwargs_populated_when_main_with_token():
    engine = object()
    window = _base_window(is_main=True, lease_token="tok-1", pc_engine=engine)
    window.state_sync_role = ss.LocalDeviceRole("laptop-id", "LAPTOP", True)

    kwargs = MainWindow._current_execution_lease_kwargs(window)

    assert isinstance(kwargs["execution_authority"], ExecutionAuthority)
    assert kwargs["execution_lease"] == LeaseHandle(
        device_id="laptop-id", lease_token="tok-1"
    )
    assert kwargs["lease_engine"] is engine


# --- _state_sync_allows_order_submission freshness gate --------------------


def test_order_submission_allowed_when_main_and_reconcile_fresh():
    window = _base_window(is_main=True, lease_token="tok-1")
    window.state_sync_role = ss.LocalDeviceRole("laptop-id", "LAPTOP", True)
    window.state_save_manager = SimpleNamespace(_is_main_device=True)
    window._last_successful_reconcile_at = dt.datetime.now(dt.timezone.utc)

    assert MainWindow._state_sync_allows_order_submission(window) is True


def test_order_submission_blocked_when_reconcile_stale(monkeypatch):
    window = _base_window(is_main=True, lease_token="tok-1")
    window.state_sync_role = ss.LocalDeviceRole("laptop-id", "LAPTOP", True)
    window.state_save_manager = SimpleNamespace(_is_main_device=True)
    window._last_successful_reconcile_at = dt.datetime.now(
        dt.timezone.utc
    ) - dt.timedelta(seconds=200)
    monkeypatch.setattr(main_window_module.QMessageBox, "warning", lambda *a: None)

    assert MainWindow._state_sync_allows_order_submission(window) is False
    assert any("stale" in message.lower() for message in window._logs)


def test_order_submission_blocked_when_never_reconciled(monkeypatch):
    window = _base_window(is_main=True, lease_token="tok-1")
    window.state_sync_role = ss.LocalDeviceRole("laptop-id", "LAPTOP", True)
    window.state_save_manager = SimpleNamespace(_is_main_device=True)
    monkeypatch.setattr(main_window_module.QMessageBox, "warning", lambda *a: None)

    assert MainWindow._state_sync_allows_order_submission(window) is False


# --- _auto_arm_trading_kill_switch gate checklist --------------------------


def test_auto_arm_skipped_when_flag_off():
    window = _base_window(is_main=True, lease_token="tok-1")
    window._auto_arm_trading_on_handoff = False

    MainWindow._auto_arm_trading_kill_switch(window)

    assert trading_state.is_trading_enabled() is False


def test_auto_arm_skipped_when_trading_locked_by_env(monkeypatch):
    window = _base_window(is_main=True, lease_token="tok-1")
    window._auto_arm_trading_on_handoff = True
    monkeypatch.setattr(trading_state, "is_trading_locked_disabled", lambda: True)

    MainWindow._auto_arm_trading_kill_switch(window)

    assert trading_state.is_trading_enabled() is False


def test_auto_arm_skipped_when_not_main():
    window = _base_window(is_main=False, lease_token="")
    window._auto_arm_trading_on_handoff = True

    MainWindow._auto_arm_trading_kill_switch(window)

    assert trading_state.is_trading_enabled() is False


def test_auto_arm_skipped_when_database_not_ready():
    window = _base_window(is_main=True, lease_token="tok-1", db_ready=False)
    window._auto_arm_trading_on_handoff = True

    MainWindow._auto_arm_trading_kill_switch(window)

    assert trading_state.is_trading_enabled() is False


def test_auto_arm_succeeds_when_every_condition_holds():
    window = _base_window(is_main=True, lease_token="tok-1", db_ready=True)
    window.state_sync_role = ss.LocalDeviceRole("laptop-id", "LAPTOP", True)
    window._auto_arm_trading_on_handoff = True
    window.trading_enabled_button = _ButtonStub()

    MainWindow._auto_arm_trading_kill_switch(window)

    assert trading_state.is_trading_enabled() is True
    assert window.trading_enabled_button.checked is True
    assert any("auto-armed" in message.lower() for message in window._logs)


# --- post-claim reconciliation gating (_on_post_claim_reconciliation_finished) --


def _handoff_window():
    window = _base_window(is_main=True, lease_token="tok-1")
    window.state_sync_role = ss.LocalDeviceRole("laptop-id", "LAPTOP", True)
    window.buylist_manager = SimpleNamespace(items=[])
    calls = SimpleNamespace(
        save_buylist=0,
        populate_dashboard=0,
        monitor_started=[],
        auto_armed=0,
        retries=[],
    )
    window._save_buylist_state = lambda: setattr(
        calls, "save_buylist", calls.save_buylist + 1
    )
    window.populate_buylist_dashboard = lambda: setattr(
        calls, "populate_dashboard", calls.populate_dashboard + 1
    )
    window._ensure_buylist_monitor_running = lambda env: (
        calls.monitor_started.append(env) or True
    )
    window._auto_arm_trading_kill_switch = lambda: setattr(
        calls, "auto_armed", calls.auto_armed + 1
    )
    window._calls = calls
    return window


def test_post_claim_success_starts_monitor_and_arms_trading():
    window = _handoff_window()
    window._handoff_generation = 1
    outcome = PostClaimReconciliationResult(ok=True, reconciled_symbols=["AAPL"])

    MainWindow._on_post_claim_reconciliation_finished(window, outcome, 1)

    assert window._calls.monitor_started == ["PROD"]
    assert window._calls.auto_armed == 1
    assert window._calls.save_buylist == 1


def test_post_claim_blocked_never_starts_monitor_or_arms_and_schedules_retry(
    monkeypatch,
):
    window = _handoff_window()
    window._handoff_generation = 1
    monkeypatch.setattr(
        main_window_module.QTimer,
        "singleShot",
        staticmethod(lambda ms, fn: window._calls.retries.append((ms, fn))),
    )
    outcome = PostClaimReconciliationResult(ok=False, blocked_symbols=["AAPL"])

    MainWindow._on_post_claim_reconciliation_finished(window, outcome, 1)

    assert window._calls.monitor_started == []
    assert window._calls.auto_armed == 0
    assert len(window._calls.retries) == 1
    assert window._calls.retries[0][0] == 30_000


def test_post_claim_stale_generation_is_ignored():
    window = _handoff_window()
    window._handoff_generation = 2  # a newer handoff attempt has already started
    outcome = PostClaimReconciliationResult(ok=True, reconciled_symbols=["AAPL"])

    MainWindow._on_post_claim_reconciliation_finished(window, outcome, 1)

    assert window._calls.monitor_started == []
    assert window._calls.auto_armed == 0
    assert window._calls.save_buylist == 0


def test_post_claim_ignored_after_losing_the_lease():
    window = _handoff_window()
    window._handoff_generation = 1
    window.state_sync_role = ss.LocalDeviceRole("laptop-id", "LAPTOP", False)
    outcome = PostClaimReconciliationResult(ok=True, reconciled_symbols=["AAPL"])

    MainWindow._on_post_claim_reconciliation_finished(window, outcome, 1)

    assert window._calls.monitor_started == []
    assert window._calls.auto_armed == 0


def test_retry_post_claim_handoff_noop_when_no_longer_main():
    window = _handoff_window()
    window.state_sync_role = ss.LocalDeviceRole("laptop-id", "LAPTOP", False)
    started = []
    window._begin_post_claim_handoff = lambda: started.append(True)

    MainWindow._retry_post_claim_handoff_if_still_main(window)

    assert started == []


def test_retry_post_claim_handoff_reattempts_when_still_main():
    window = _handoff_window()
    started = []
    window.__dict__["_database_shutting_down"] = False
    window._begin_post_claim_handoff = lambda: started.append(True)

    MainWindow._retry_post_claim_handoff_if_still_main(window)

    assert started == [True]


# --- _begin_post_claim_handoff: reset flags + construct worker -------------


def test_begin_post_claim_handoff_locks_in_flight_items_and_starts_worker(monkeypatch):
    window = _handoff_window()
    bought = SimpleNamespace(
        symbol="AAPL", environment="PROD", monitoring_status="BOUGHT"
    )
    watching = SimpleNamespace(
        symbol="MSFT", environment="PROD", monitoring_status="WATCHING"
    )
    window.buylist_manager = SimpleNamespace(items=[bought, watching])
    window._first_account_no_for_environment = lambda env: "12345678-01"

    created_workers = []

    class FakeHandoffReconciliationWorker:
        def __init__(self, buylist_manager, *, environment, account_no):
            self.buylist_manager = buylist_manager
            self.environment = environment
            self.account_no = account_no
            self.finished_reconciliation = _SignalStub()
            self.error_occurred = _SignalStub()
            self.finished = _SignalStub()
            self.started = False
            created_workers.append(self)

        def start(self):
            self.started = True

    monkeypatch.setattr(
        main_window_module, "HandoffReconciliationWorker", FakeHandoffReconciliationWorker
    )
    window._track_worker = lambda *args, **kwargs: None

    MainWindow._begin_post_claim_handoff(window)

    assert bought._buy_order_pending is True
    assert bought._stop_order_pending is True
    assert not hasattr(watching, "_buy_order_pending")
    assert len(created_workers) == 1
    worker = created_workers[0]
    assert worker.environment == "PROD"
    assert worker.account_no == "12345678-01"
    assert worker.started is True
    assert window._calls.save_buylist == 1


# --- _on_state_sync_completed: automatic-claim detection --------------------


def _sync_completed_window(*, auto_claim_enabled: bool):
    window = _base_window(is_main=False, lease_token="")
    window.state_sync_role = ss.LocalDeviceRole("pc-id", "PC", False)
    window._auto_claim_main_enabled = auto_claim_enabled
    window._database_transition_generation = 0
    window._initial_state_sync_complete = False
    window._last_state_sync_notice = ""
    window._bind_remote_state_engine = lambda *a, **k: None
    window._update_main_device_button = lambda **k: None
    window.update_dashboard_summary = lambda: None
    window.append_log = window._logs.append if hasattr(window, "_logs") else print
    window._state_sync_action = "reconcile"
    return window


def test_auto_claim_not_triggered_when_disabled(monkeypatch):
    window = _sync_completed_window(auto_claim_enabled=False)
    calls = []
    monkeypatch.setattr(
        main_window_module,
        "should_auto_claim_main",
        lambda *a, **k: calls.append(True) or (True, "laptop-id", "stale"),
    )
    started = []
    window._start_state_sync = lambda **kwargs: started.append(kwargs)
    result = StateReconcileResult(is_main_device=False, local_role=window.state_sync_role)

    MainWindow._on_state_sync_completed(window, result, 0)

    assert calls == []
    assert started == []


def test_auto_claim_triggered_when_enabled_and_should_claim_says_yes(monkeypatch):
    window = _sync_completed_window(auto_claim_enabled=True)
    monkeypatch.setattr(
        main_window_module,
        "should_auto_claim_main",
        lambda *a, **k: (True, "laptop-id", "stale heartbeat (90s)"),
    )
    started = []
    window._start_state_sync = lambda **kwargs: started.append(kwargs)
    result = StateReconcileResult(is_main_device=False, local_role=window.state_sync_role)

    MainWindow._on_state_sync_completed(window, result, 0)

    assert started == [
        {"auto_claim": True, "expected_owner_device_id": "laptop-id"}
    ]


def test_auto_claim_not_triggered_when_should_claim_says_no(monkeypatch):
    window = _sync_completed_window(auto_claim_enabled=True)
    monkeypatch.setattr(
        main_window_module, "should_auto_claim_main", lambda *a, **k: (False, "", "")
    )
    started = []
    window._start_state_sync = lambda **kwargs: started.append(kwargs)
    result = StateReconcileResult(is_main_device=False, local_role=window.state_sync_role)

    MainWindow._on_state_sync_completed(window, result, 0)

    assert started == []


def test_successful_auto_claim_activation_begins_post_claim_handoff():
    window = _sync_completed_window(auto_claim_enabled=True)
    window.state_sync_role = ss.LocalDeviceRole("pc-id", "PC", False)
    window._state_sync_action = "activate"
    window._state_sync_auto_claim = True
    begun = []
    window._begin_post_claim_handoff = lambda: begun.append(True)
    result = StateReconcileResult(
        is_main_device=True,
        lease_token="tok-1",
        local_role=ss.LocalDeviceRole("pc-id", "PC", True),
        main_device_hostname="PC",
    )

    MainWindow._on_state_sync_completed(window, result, 0)

    assert begun == [True]
    assert window._current_lease_token == "tok-1"


def test_manual_activation_does_not_begin_post_claim_handoff():
    """Regression guard: manual 'Use This Device as Main' must not auto-arm trading."""
    window = _sync_completed_window(auto_claim_enabled=True)
    window.state_sync_role = ss.LocalDeviceRole("pc-id", "PC", False)
    window._state_sync_action = "activate"
    window._state_sync_auto_claim = False  # manual button click, not auto-claim
    begun = []
    window._begin_post_claim_handoff = lambda: begun.append(True)
    result = StateReconcileResult(
        is_main_device=True,
        lease_token="tok-1",
        local_role=ss.LocalDeviceRole("pc-id", "PC", True),
        main_device_hostname="PC",
    )

    MainWindow._on_state_sync_completed(window, result, 0)

    assert begun == []


# --- _release_main_device_ownership_for_shutdown (closeEvent extraction) ---


def test_release_ownership_for_shutdown_is_noop_when_db_not_ready(tmp_path):
    window = _base_window(is_main=True, lease_token="tok-1", db_ready=False)
    window.state_sync_role = ss.LocalDeviceRole("laptop-id", "LAPTOP", True)

    MainWindow._release_main_device_ownership_for_shutdown(window)  # must not raise


def test_release_ownership_for_shutdown_is_noop_when_not_main(tmp_path):
    engine = _make_engine(tmp_path)
    window = _base_window(is_main=False, lease_token="", pc_engine=engine, db_ready=True)

    MainWindow._release_main_device_ownership_for_shutdown(window)  # must not raise


def test_release_ownership_for_shutdown_publishes_and_releases(monkeypatch, tmp_path):
    engine = _make_engine(tmp_path)
    role = ss.LocalDeviceRole("laptop-id", "LAPTOP", True)
    assert ss.claim_main_device(engine, role).success

    window = _base_window(is_main=True, lease_token="tok-1", pc_engine=engine)
    window.state_sync_role = role
    window.buylist_manager = SimpleNamespace(to_dict=lambda: {"items": []})
    monkeypatch.setattr(main_window_module, "load_json", lambda *a, **k: {})

    MainWindow._release_main_device_ownership_for_shutdown(window)

    assert ss.get_main_device(engine).main_device is None
    assert window.state_sync_role.is_main is False
    assert window._current_lease_token == ""
