from __future__ import annotations

from types import SimpleNamespace

import src.ui.main_window as main_window_module
from src.core.runtime_readiness import EngineReadiness, RuntimeDeviceState
from src.ui.main_window import MainWindow, _buyboard_readiness_display


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
