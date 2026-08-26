from __future__ import annotations

from types import SimpleNamespace

import src.ui.main_window as main_window_module
from src.core.runtime_readiness import EngineReadiness, RuntimeDeviceState
from src.ui.main_window import (
    MainWindow,
    _buyboard_readiness_display,
    _live_execution_status_text,
)


def _readiness(**overrides) -> EngineReadiness:
    values = dict(
        lease_current=False,
        startup_reconciliation_complete=True,
        account_reconciliation_fresh=True,
        websocket_connected=True,
        critical_trade_subscriptions_acked=True,
        critical_quote_subscriptions_acked=True,
        critical_quotes_fresh=True,
        accumulator_draining_within_budget=True,
        database_writable=True,
        device_active=False,
    )
    values.update(overrides)
    return EngineReadiness(**values)


def test_live_execution_status_names_the_controlled_scope(monkeypatch):
    monkeypatch.setattr(
        main_window_module.execution_config,
        "KIS_LIVE_EXECUTION_MODE",
        "CONTROLLED_LIVE",
    )
    monkeypatch.setattr(
        main_window_module.execution_config,
        "KIS_CONTROLLED_LIVE_SYMBOLS",
        ("STIM",),
    )
    monkeypatch.setattr(
        main_window_module.execution_config,
        "KIS_CONTROLLED_LIVE_MAX_ENTRY_NOTIONAL",
        0.01,
    )

    assert _live_execution_status_text(True) == (
        "Enabled (CONTROLLED_LIVE: STIM, max $0.01/entry)"
    )


def test_closed_session_stale_quote_is_informational_and_symbol_scoped():
    display = _buyboard_readiness_display(
        _readiness(critical_quotes_fresh=False),
        device_state=RuntimeDeviceState.STANDBY,
        regular_session_open=False,
        seconds_until_open=3_661,
        auto_claim_enabled=True,
    )

    assert display.completed == 7
    assert display.total == 7
    assert display.indeterminate is False
    assert "readiness 7/7" in display.label
    assert "Market opens in 01:01:01" in display.tooltip
    assert "Per-symbol execution guard" in display.tooltip
    assert "overall board readiness is unchanged" in display.tooltip
    assert "Automatic PC claim is enabled" in display.tooltip


def test_premarket_successor_stays_ready_without_global_quote_flicker():
    display = _buyboard_readiness_display(
        _readiness(critical_quotes_fresh=False),
        device_state=RuntimeDeviceState.STANDBY_READY,
        regular_session_open=False,
        seconds_until_open=300,
        auto_claim_enabled=True,
    )

    assert display.completed == 7
    assert display.total == 7
    assert "STANDBY_READY" in display.label
    assert "Market opens in 00:05:00" in display.tooltip
    assert "execution waits" not in display.label


def test_single_session_handoff_is_shown_as_ready_without_claiming_execution():
    display = _buyboard_readiness_display(
        _readiness(
            websocket_connected=False,
            critical_trade_subscriptions_acked=False,
            critical_quote_subscriptions_acked=False,
        ),
        device_state=RuntimeDeviceState.STANDBY_READY,
        single_session_handoff_ready=True,
    )

    assert display.completed == 7
    assert display.total == 7
    assert "STANDBY_READY" in display.label
    assert "KIS WebSocket transfers" in display.label
    assert "execution-closed" in display.tooltip


def test_premarket_main_is_ready_with_per_symbol_quote_guards():
    display = _buyboard_readiness_display(
        _readiness(critical_quotes_fresh=False),
        device_state=RuntimeDeviceState.STANDBY,
        regular_session_open=False,
        seconds_until_open=120,
        is_main_device=True,
    )

    assert "Main lease held" in display.label
    assert "per-symbol execution guards active" in display.label
    assert "execution waits" not in display.label
    assert "Market opens in 00:02:00" in display.tooltip


def test_confirmed_queue_delay_uses_a_stable_actionable_message():
    display = _buyboard_readiness_display(
        _readiness(accumulator_draining_within_budget=False),
        device_state=RuntimeDeviceState.STANDBY,
        regular_session_open=True,
    )

    assert display.completed == 6
    assert display.total == 7
    assert "missed its drain budget three times" in display.label
    assert "sustained market-data queue delay" in display.tooltip


def test_multiple_readiness_failures_are_all_named_in_the_label():
    display = _buyboard_readiness_display(
        _readiness(
            account_reconciliation_fresh=False,
            database_writable=False,
        ),
        device_state=RuntimeDeviceState.STANDBY,
        regular_session_open=True,
    )

    assert display.completed == 5
    assert display.total == 7
    assert "2 checks pending" in display.label
    assert "fresh broker account snapshot" in display.label
    assert "local Kanban operational state writable" in display.label


def test_live_reconciliation_uses_indeterminate_progress_without_fake_eta():
    display = _buyboard_readiness_display(
        _readiness(account_reconciliation_fresh=False),
        device_state=RuntimeDeviceState.STANDBY,
        reconciliation_accounts=("12345678-01",),
        regular_session_open=True,
    )

    assert display.indeterminate is True
    assert "final broker reconciliation for 12345678-01" in display.label
    assert "ETA unavailable" in display.label


def test_active_projection_explains_that_live_trading_is_a_separate_gate():
    display = _buyboard_readiness_display(
        _readiness(
            account_reconciliation_fresh=False,
            critical_quotes_fresh=False,
        ),
        device_state=RuntimeDeviceState.ACTIVE,
        regular_session_open=True,
    )

    assert display.completed == 7
    assert display.total == 7
    assert "readiness 7/7" in display.label
    assert "ACTIVE" in display.label
    assert "Live Trading" in display.label
    assert "Per-symbol execution guard" in display.tooltip


def test_active_runtime_stays_latched_while_legacy_handoff_is_running(monkeypatch):
    monkeypatch.setattr(
        main_window_module.execution_config,
        "is_buyboard_engine_enabled",
        lambda: True,
    )
    monkeypatch.setattr(main_window_module, "is_regular_session_open", lambda: True)
    runtime = SimpleNamespace(
        isRunning=lambda: True,
        device_state=RuntimeDeviceState.ACTIVE,
        engine_readiness=lambda **_kwargs: _readiness(
            lease_current=True,
            device_active=True,
            account_reconciliation_fresh=False,
            critical_quotes_fresh=False,
        ),
        reconciliation_accounts_in_progress=set(),
    )
    window = MainWindow.__new__(MainWindow)
    window._buyboard_runtime_worker = runtime
    window.handoff_reconciliation_worker = SimpleNamespace(isRunning=lambda: True)
    window.state_sync_worker = None
    window.state_sync_role = SimpleNamespace(is_main=True)
    window._auto_claim_main_enabled = True

    display = MainWindow._current_buyboard_readiness_display(window)

    assert display.completed == 7
    assert display.total == 7
    assert display.indeterminate is False
    assert "readiness 7/7" in display.label
    assert "ACTIVE" in display.label
    assert "final broker reconciliation" not in display.label


def test_database_outage_reports_kis_recovery_instead_of_runtime_unavailable(
    monkeypatch,
):
    monkeypatch.setattr(
        main_window_module.execution_config,
        "is_buyboard_engine_enabled",
        lambda: True,
    )
    window = MainWindow.__new__(MainWindow)
    window._buyboard_runtime_worker = None
    window.state_sync_worker = None
    window.handoff_reconciliation_worker = None
    window.pc_db_engine = None
    window._pc_database_ready = False
    window.kis_account_snapshots = {("PROD", "1"): {"overseas": {"holdings": []}}}
    window.kis_account_snapshot_fetched_at = {
        ("PROD", "1"): object()
    }

    display = MainWindow._current_buyboard_readiness_display(window)

    assert "KIS holdings/prices monitored" in display.label
    assert "execution disabled" in display.label
    assert "runtime worker unavailable" not in display.label
    assert "Historical data is not an execution requirement" in display.tooltip
    assert "local Kanban operational file itself could not be opened" in display.tooltip
    assert "App execution is locked" in display.tooltip
    assert "Never duplicate an order" in display.tooltip


def test_routine_reconciliation_uses_debounced_operator_projection(monkeypatch):
    monkeypatch.setattr(
        main_window_module.execution_config,
        "is_buyboard_engine_enabled",
        lambda: True,
    )
    monkeypatch.setattr(main_window_module, "is_regular_session_open", lambda: True)
    strict = _readiness(
        startup_reconciliation_complete=False,
        account_reconciliation_fresh=False,
    )
    stable = _readiness()
    runtime = SimpleNamespace(
        isRunning=lambda: True,
        device_state=RuntimeDeviceState.STANDBY_READY,
        engine_readiness=lambda **_kwargs: strict,
        readiness_for_operator_display=lambda readiness: stable,
        reconciliation_accounts_for_operator_display=lambda: (),
        reconciliation_accounts_in_progress={"12345678-01"},
    )
    window = MainWindow.__new__(MainWindow)
    window._buyboard_runtime_worker = runtime
    window.handoff_reconciliation_worker = None
    window.state_sync_worker = None
    window.state_sync_role = SimpleNamespace(is_main=False)
    window._auto_claim_main_enabled = True

    display = MainWindow._current_buyboard_readiness_display(window)

    assert display.completed == 7
    assert display.total == 7
    assert display.indeterminate is False
    assert "STANDBY_READY" in display.label
    assert "final broker reconciliation" not in display.label


def test_standby_runtime_still_shows_running_handoff(monkeypatch):
    monkeypatch.setattr(
        main_window_module.execution_config,
        "is_buyboard_engine_enabled",
        lambda: True,
    )
    window = MainWindow.__new__(MainWindow)
    window._buyboard_runtime_worker = SimpleNamespace(
        isRunning=lambda: True,
        device_state=RuntimeDeviceState.STANDBY,
    )
    window.handoff_reconciliation_worker = SimpleNamespace(isRunning=lambda: True)
    window.state_sync_worker = None

    display = MainWindow._current_buyboard_readiness_display(window)

    assert display.indeterminate is True
    assert "final broker reconciliation" in display.label
