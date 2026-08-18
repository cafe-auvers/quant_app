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
from src.core.runtime_readiness import RuntimeDeviceState
from src.core.trade_card_state import BoardStatus, TradeCardState
from src.services import trade_card_repository
from src.services.runtime_device_state_repository import (
    confirm_standby_handoff,
    save_runtime_device_state,
)
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


def _base_window(
    *,
    is_main=False,
    lease_token="",
    lease_epoch=None,
    pc_engine=None,
    db_ready=True,
):
    window = MainWindow.__new__(MainWindow)
    window.pc_db_engine = pc_engine
    window._pc_database_ready = db_ready
    window.state_sync_role = ss.LocalDeviceRole(
        "pc-id" if not is_main else "laptop-id", "TESTHOST", is_main
    )
    window._current_lease_token = lease_token
    window._current_lease_epoch = (
        int(lease_epoch) if lease_epoch is not None else (1 if lease_token else 0)
    )
    window._last_successful_reconcile_at = None
    window._auto_claim_main_enabled = False
    window._auto_arm_trading_on_handoff = False
    window.handoff_reconciliation_worker = None
    window._handoff_generation = 0
    window._state_sync_auto_claim = False
    window._handoff_reconciliation_required = False
    window._handoff_allow_auto_arm = False
    window.state_sync_worker = None
    window.state_save_manager = SimpleNamespace(_is_main_device=is_main)
    window._bind_remote_state_engine = lambda _engine, *, is_main_device=None: setattr(
        window.state_save_manager, "_is_main_device", bool(is_main_device)
    )
    logs = []
    window.append_log = logs.append
    window._logs = logs
    return window


def _bind_claimed_lease(window, ownership):
    lease = ownership.main_device
    assert lease is not None
    window._current_lease_token = lease.lease_token
    window._current_lease_epoch = lease.lease_epoch


def _create_open_position(engine):
    trade_card_repository.create_trade_card(
        engine,
        TradeCardState(
            environment="PROD",
            account_no="1",
            symbol="AAPL",
            board_status=BoardStatus.OPEN_POSITION,
            broker_quantity=10,
            orderable_quantity=10,
        ),
    )


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
        device_id="laptop-id", lease_token="tok-1", lease_epoch=1
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


def test_order_submission_blocked_until_handoff_reconciliation_is_clean():
    window = _base_window(is_main=True, lease_token="tok-1")
    window.state_sync_role = ss.LocalDeviceRole("laptop-id", "LAPTOP", True)
    window.state_save_manager = SimpleNamespace(_is_main_device=True)
    window._last_successful_reconcile_at = dt.datetime.now(dt.timezone.utc)
    window._handoff_reconciliation_required = True

    assert MainWindow._state_sync_allows_order_submission(window) is False
    assert any("handoff" in message.lower() for message in window._logs)


# --- shared live-trading control survives Main handoff ----------------------


def test_legacy_auto_arm_hook_does_not_change_shared_control():
    window = _base_window(is_main=True, lease_token="tok-1", db_ready=True)
    window.state_sync_role = ss.LocalDeviceRole("laptop-id", "LAPTOP", True)
    window._auto_arm_trading_on_handoff = True
    window.trading_enabled_button = _ButtonStub()
    trading_state.set_trading_enabled(True)

    MainWindow._auto_arm_trading_kill_switch(window)

    assert trading_state.is_trading_enabled() is True
    assert window.trading_enabled_button.checked is True
    assert any("shared deployment switch" in message.lower() for message in window._logs)


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
    window._persist_post_claim_reconciliation_state = lambda: (
        setattr(calls, "save_buylist", calls.save_buylist + 1) or True,
        "",
    )
    window._handoff_allow_auto_arm = True
    window._calls = calls
    return window


def test_post_claim_success_starts_monitor_without_changing_shared_control():
    window = _handoff_window()
    window._handoff_generation = 1
    outcome = PostClaimReconciliationResult(ok=True, reconciled_symbols=["AAPL"])

    MainWindow._on_post_claim_reconciliation_finished(window, outcome, 1)

    assert window._calls.monitor_started == ["PROD"]
    assert window._calls.auto_armed == 0
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


def test_post_claim_clean_broker_result_stays_blocked_if_state_publish_fails(
    monkeypatch,
):
    window = _handoff_window()
    window._handoff_generation = 1
    window._persist_post_claim_reconciliation_state = lambda: (
        False,
        "MySQL unavailable",
    )
    monkeypatch.setattr(
        main_window_module.QTimer,
        "singleShot",
        staticmethod(lambda ms, fn: window._calls.retries.append((ms, fn))),
    )

    MainWindow._on_post_claim_reconciliation_finished(
        window,
        PostClaimReconciliationResult(ok=True, reconciled_symbols=["AAPL"]),
        1,
    )

    assert window._handoff_reconciliation_required is True
    assert window._last_handoff_blocked_symbols == ("AAPL",)
    assert window._calls.monitor_started == []
    assert window._calls.auto_armed == 0
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
    window._begin_post_claim_handoff = lambda **kwargs: started.append(kwargs)

    MainWindow._retry_post_claim_handoff_if_still_main(window)

    assert started == []


def test_retry_post_claim_handoff_reattempts_when_still_main():
    window = _handoff_window()
    started = []
    window.__dict__["_database_shutting_down"] = False
    window._begin_post_claim_handoff = lambda **kwargs: started.append(kwargs)

    MainWindow._retry_post_claim_handoff_if_still_main(window)

    assert started == [{"allow_auto_arm": True}]


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
        def __init__(self, buylist_manager, *, environment):
            self.buylist_manager = buylist_manager
            self.environment = environment
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


def test_state_sync_projects_shared_live_trading_on_and_off():
    window = _sync_completed_window(auto_claim_enabled=False)
    window._start_state_sync = lambda **_kwargs: None

    MainWindow._on_state_sync_completed(
        window,
        StateReconcileResult(
            is_main_device=False,
            local_role=window.state_sync_role,
            live_trading_enabled=True,
            live_trading_revision=7,
        ),
        0,
    )
    assert trading_state.is_trading_enabled() is True
    assert window._shared_live_trading_available is True
    assert window._shared_live_trading_revision == 7

    MainWindow._on_state_sync_completed(
        window,
        StateReconcileResult(
            is_main_device=False,
            local_role=window.state_sync_role,
            live_trading_enabled=False,
            live_trading_revision=8,
        ),
        0,
    )
    assert trading_state.is_trading_enabled() is False
    assert window._shared_live_trading_revision == 8


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


def test_auto_claim_waits_for_a_fresh_standby_generation(monkeypatch):
    window = _sync_completed_window(auto_claim_enabled=True)
    window._sync_buyboard_runtime_worker = lambda: None
    window._runtime_standby_generation_for_claim = lambda: 0
    monkeypatch.setattr(
        main_window_module.execution_config,
        "is_buyboard_engine_enabled",
        lambda: True,
    )
    monkeypatch.setattr(
        main_window_module,
        "should_auto_claim_main",
        lambda *a, **k: (True, "laptop-id", "stale heartbeat"),
    )
    started = []
    window._start_state_sync = lambda **kwargs: started.append(kwargs)

    MainWindow._on_state_sync_completed(
        window,
        StateReconcileResult(is_main_device=False, local_role=window.state_sync_role),
        0,
    )

    assert started == []


def test_premarket_quote_staleness_does_not_block_standby_generation_claim(
    monkeypatch, tmp_path
):
    engine = _make_engine(tmp_path)
    role = ss.LocalDeviceRole("pc-id", "PC", False)
    record = save_runtime_device_state(
        engine,
        device_id=role.device_id,
        hostname=role.hostname,
        state=RuntimeDeviceState.STANDBY_READY,
    )

    class _Worker:
        _standby_only = True
        device_state = RuntimeDeviceState.STANDBY_READY

        @staticmethod
        def isRunning():
            return True

        @staticmethod
        def engine_readiness(**kwargs):
            return SimpleNamespace(
                standby_ready=False,
                premarket_handoff_ready=True,
            )

        @staticmethod
        def lease_handoff_ready(readiness):
            return readiness.premarket_handoff_ready

    window = MainWindow.__new__(MainWindow)
    window.pc_db_engine = engine
    window.state_sync_role = role
    window._buyboard_runtime_worker = _Worker()
    monkeypatch.setattr(
        main_window_module.execution_config,
        "is_buyboard_engine_enabled",
        lambda: True,
    )

    assert (
        MainWindow._runtime_standby_generation_for_claim(window)
        == record.readiness_generation
    )


def test_successful_auto_claim_activation_begins_post_claim_handoff():
    window = _sync_completed_window(auto_claim_enabled=True)
    window.state_sync_role = ss.LocalDeviceRole("pc-id", "PC", False)
    window._state_sync_action = "activate"
    window._state_sync_auto_claim = True
    begun = []
    window._begin_post_claim_handoff = lambda **kwargs: begun.append(
        kwargs["allow_auto_arm"]
    )
    result = StateReconcileResult(
        is_main_device=True,
        lease_token="tok-1",
        local_role=ss.LocalDeviceRole("pc-id", "PC", True),
        main_device_hostname="PC",
    )

    MainWindow._on_state_sync_completed(window, result, 0)

    assert begun == [True]
    assert window._current_lease_token == "tok-1"


def test_manual_activation_begins_reconciliation_without_auto_arm_permission():
    """Manual 'Use This Device as Main' reconciles but cannot auto-arm trading."""
    window = _sync_completed_window(auto_claim_enabled=True)
    window.state_sync_role = ss.LocalDeviceRole("pc-id", "PC", False)
    window._state_sync_action = "activate"
    window._state_sync_auto_claim = False  # manual button click, not auto-claim
    begun = []
    window._begin_post_claim_handoff = lambda **kwargs: begun.append(
        kwargs["allow_auto_arm"]
    )
    result = StateReconcileResult(
        is_main_device=True,
        lease_token="tok-1",
        local_role=ss.LocalDeviceRole("pc-id", "PC", True),
        main_device_hostname="PC",
    )

    MainWindow._on_state_sync_completed(window, result, 0)

    assert begun == [False]


def test_claim_with_sync_error_keeps_execution_fenced_until_later_reconcile():
    window = _sync_completed_window(auto_claim_enabled=False)
    window.state_sync_role = ss.LocalDeviceRole("pc-id", "PC", False)
    window._state_sync_action = "activate"
    window._state_sync_auto_claim = False
    window._handoff_reconciliation_required = True
    begun = []
    window._begin_post_claim_handoff = lambda **kwargs: begun.append(kwargs)

    claimed_with_error = StateReconcileResult(
        is_main_device=True,
        lease_token="tok-1",
        local_role=ss.LocalDeviceRole("pc-id", "PC", True),
        main_device_hostname="PC",
        errors=["remote state pull failed"],
    )
    MainWindow._on_state_sync_completed(window, claimed_with_error, 0)

    assert window._handoff_reconciliation_required is True
    assert begun == []

    window._state_sync_action = "reconcile"
    clean = StateReconcileResult(
        is_main_device=True,
        lease_token="tok-1",
        local_role=ss.LocalDeviceRole("pc-id", "PC", True),
        main_device_hostname="PC",
    )
    MainWindow._on_state_sync_completed(window, clean, 0)

    assert begun == [{"allow_auto_arm": False}]


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
    claimed = ss.claim_main_device(engine, role)
    assert claimed.success

    window = _base_window(is_main=True, lease_token="tok-1", pc_engine=engine)
    _bind_claimed_lease(window, claimed)
    window.state_sync_role = role
    window.buylist_manager = SimpleNamespace(to_dict=lambda: {"items": []})
    monkeypatch.setattr(main_window_module, "load_json", lambda *a, **k: {})
    monkeypatch.setattr(
        main_window_module, "publish_handoff_snapshot", lambda *a, **k: True
    )

    MainWindow._release_main_device_ownership_for_shutdown(window)

    assert ss.get_main_device(engine).main_device is None
    assert window.state_sync_role.is_main is False
    assert window._current_lease_token == ""


def test_shutdown_retains_ownership_when_strict_publication_fails(
    monkeypatch, tmp_path
):
    engine = _make_engine(tmp_path)
    role = ss.LocalDeviceRole("laptop-id", "LAPTOP", True)
    assert ss.claim_main_device(engine, role).success
    window = _base_window(is_main=True, lease_token="tok-1", pc_engine=engine)
    window.state_sync_role = role
    window.buylist_manager = SimpleNamespace(to_dict=lambda: {"items": []})
    monkeypatch.setattr(main_window_module, "load_json", lambda *a, **k: {})
    monkeypatch.setattr(
        main_window_module, "publish_handoff_snapshot", lambda *a, **k: False
    )

    released = MainWindow._release_main_device_ownership_for_shutdown(window)

    assert released is False
    assert ss.get_main_device(engine).main_device.device_id == "laptop-id"
    assert window.state_sync_role.is_main is True
    assert window.state_save_manager._is_main_device is True


def test_shutdown_retains_ownership_when_final_local_save_failed(
    monkeypatch, tmp_path
):
    engine = _make_engine(tmp_path)
    role = ss.LocalDeviceRole("laptop-id", "LAPTOP", True)
    assert ss.claim_main_device(engine, role).success
    window = _base_window(is_main=True, lease_token="tok-1", pc_engine=engine)
    window.state_sync_role = role
    published = []
    monkeypatch.setattr(
        main_window_module,
        "publish_handoff_snapshot",
        lambda *a, **k: published.append(True) or True,
    )

    released = MainWindow._release_main_device_ownership_for_shutdown(
        window, final_save_succeeded=False
    )

    assert released is False
    assert published == []
    assert ss.get_main_device(engine).main_device.device_id == "laptop-id"


def test_unattended_shutdown_release_is_refused_with_open_position_and_no_successor(
    monkeypatch, tmp_path
):
    engine = _make_engine(tmp_path)
    role = ss.LocalDeviceRole("laptop-id", "LAPTOP", True)
    assert ss.claim_main_device(engine, role).success
    _create_open_position(engine)
    window = _base_window(is_main=True, lease_token="tok-1", pc_engine=engine)
    window.state_sync_role = role
    window._auto_claim_main_enabled = True
    published = []
    monkeypatch.setattr(
        main_window_module,
        "publish_handoff_snapshot",
        lambda *args, **kwargs: published.append(True) or True,
    )

    released = MainWindow._release_main_device_ownership_for_shutdown(window)

    assert released is False
    assert published == []
    assert ss.get_main_device(engine).main_device.device_id == "laptop-id"


def test_shutdown_release_proceeds_with_confirmed_standby_successor(
    monkeypatch, tmp_path
):
    engine = _make_engine(tmp_path)
    role = ss.LocalDeviceRole("laptop-id", "LAPTOP", True)
    claimed = ss.claim_main_device(engine, role)
    assert claimed.success
    _create_open_position(engine)
    save_runtime_device_state(
        engine,
        device_id="pc-id",
        hostname="PC",
        state=RuntimeDeviceState.STANDBY_READY,
    )
    window = _base_window(is_main=True, lease_token="tok-1", pc_engine=engine)
    _bind_claimed_lease(window, claimed)
    window.state_sync_role = role
    window._auto_claim_main_enabled = True
    window.buylist_manager = SimpleNamespace(to_dict=lambda: {"items": []})
    monkeypatch.setattr(main_window_module, "load_json", lambda *args, **kwargs: {})
    monkeypatch.setattr(
        main_window_module,
        "publish_handoff_snapshot",
        lambda *args, **kwargs: True,
    )

    released = MainWindow._release_main_device_ownership_for_shutdown(window)

    assert released is True
    assert ss.get_main_device(engine).main_device is None


def test_exposed_release_rejects_successor_confirmed_for_prior_lease(
    monkeypatch, tmp_path
):
    engine = _make_engine(tmp_path)
    role = ss.LocalDeviceRole("laptop-id", "LAPTOP", True)
    first = ss.claim_main_device(engine, role)
    assert first.success
    assert ss.release_main_device(
        engine,
        role,
        expected_lease_token=first.main_device.lease_token,
        expected_lease_epoch=first.main_device.lease_epoch,
    ).success
    current = ss.claim_main_device_if_unclaimed(engine, role)
    assert current.success
    _create_open_position(engine)
    ready = save_runtime_device_state(
        engine,
        device_id="pc-id",
        hostname="PC",
        state=RuntimeDeviceState.STANDBY_READY,
    )
    assert confirm_standby_handoff(
        engine,
        device_id="pc-id",
        readiness_generation=ready.readiness_generation,
        outgoing_lease_epoch=first.main_device.lease_epoch,
    )
    window = _base_window(is_main=True, lease_token="placeholder", pc_engine=engine)
    _bind_claimed_lease(window, current)
    window.state_sync_role = role
    published = []
    monkeypatch.setattr(
        main_window_module,
        "publish_handoff_snapshot",
        lambda *args, **kwargs: published.append(True) or True,
    )

    released = MainWindow._release_main_device_ownership_for_shutdown(window)

    assert released is False
    assert published == []
    assert ss.get_main_device(engine).main_device.lease_epoch == current.main_device.lease_epoch


def test_exposed_release_with_unknown_current_epoch_fails_closed(
    monkeypatch, tmp_path
):
    engine = _make_engine(tmp_path)
    role = ss.LocalDeviceRole("laptop-id", "LAPTOP", True)
    claimed = ss.claim_main_device(engine, role)
    assert claimed.success
    _create_open_position(engine)
    ready = save_runtime_device_state(
        engine,
        device_id="pc-id",
        hostname="PC",
        state=RuntimeDeviceState.STANDBY_READY,
    )
    assert confirm_standby_handoff(
        engine,
        device_id="pc-id",
        readiness_generation=ready.readiness_generation,
        outgoing_lease_epoch=claimed.main_device.lease_epoch,
    )
    window = _base_window(
        is_main=True,
        lease_token=claimed.main_device.lease_token,
        lease_epoch=0,
        pc_engine=engine,
    )
    window.state_sync_role = role
    published = []
    monkeypatch.setattr(
        main_window_module,
        "publish_handoff_snapshot",
        lambda *args, **kwargs: published.append(True) or True,
    )

    released = MainWindow._release_main_device_ownership_for_shutdown(window)

    assert released is False
    assert published == []
    assert ss.get_main_device(engine).main_device.lease_token == claimed.main_device.lease_token


def test_missing_database_is_explicitly_unknown_exposure():
    window = _base_window(is_main=True, lease_token="tok-1", db_ready=False)

    exposure = MainWindow._execution_shutdown_exposure(window)

    assert exposure.inspection_confirmed is False
    assert exposure.is_clear is False
    assert "UNKNOWN EXPOSURE" in exposure.labels[0]


def test_failed_final_release_aborts_close_and_restores_protection(monkeypatch):
    window = _base_window(is_main=True, lease_token="tok-1")
    window._authorize_execution_shutdown = lambda: True
    window._stop_workers_for_shutdown = lambda *args, **kwargs: True
    window._flush_state_saves_for_shutdown = lambda **kwargs: SimpleNamespace(
        success=True,
        error="",
    )
    window._release_main_device_ownership_for_shutdown = lambda **kwargs: False
    window._authorize_emergency_close_after_release_failure = lambda: False
    restored = []
    window._restore_protection_after_aborted_shutdown = (
        lambda timer_states: restored.append(timer_states)
    )
    for name in (
        "scanner_worker",
        "watchlist_worker",
        "single_ai_worker",
        "kis_order_worker",
        "intraday_fetch_worker",
        "intraday_bulk_worker",
        "kis_account_worker",
        "kis_startup_worker",
        "order_reconciliation_worker",
        "fx_rate_worker",
    ):
        setattr(window, name, None)
    ignored = []
    event = SimpleNamespace(ignore=lambda: ignored.append(True))
    monkeypatch.setattr(
        main_window_module.QMessageBox, "critical", lambda *args, **kwargs: None
    )

    MainWindow.closeEvent(window, event)

    assert ignored == [True]
    assert len(restored) == 1
