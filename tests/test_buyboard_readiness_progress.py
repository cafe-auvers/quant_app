from __future__ import annotations

from src.core.runtime_readiness import EngineReadiness, RuntimeDeviceState
from src.ui.main_window import _buyboard_readiness_display


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


def test_closed_session_quote_wait_shows_exact_gate_and_market_open_eta():
    display = _buyboard_readiness_display(
        _readiness(critical_quotes_fresh=False),
        device_state=RuntimeDeviceState.STANDBY,
        regular_session_open=False,
        seconds_until_open=3_661,
        auto_claim_enabled=True,
    )

    assert display.completed == 7
    assert display.total == 8
    assert display.indeterminate is False
    assert "readiness 7/8" in display.label
    assert "market opens in 01:01:01" in display.label
    assert "fresh regular-session quotes" in display.tooltip
    assert "Automatic PC claim is enabled" in display.tooltip


def test_premarket_successor_shows_ready_for_main_transfer_without_quote():
    display = _buyboard_readiness_display(
        _readiness(critical_quotes_fresh=False),
        device_state=RuntimeDeviceState.STANDBY_READY,
        regular_session_open=False,
        seconds_until_open=300,
        auto_claim_enabled=True,
    )

    assert display.completed == 7
    assert "STANDBY_READY for Main transfer" in display.label
    assert "market opens in 00:05:00" in display.label
    assert "execution waits" in display.label


def test_premarket_main_shows_lease_held_while_execution_waits_for_quote():
    display = _buyboard_readiness_display(
        _readiness(critical_quotes_fresh=False),
        device_state=RuntimeDeviceState.STANDBY,
        regular_session_open=False,
        seconds_until_open=120,
        is_main_device=True,
    )

    assert "Main lease held" in display.label
    assert "execution waits" in display.label
    assert "market opens in 00:02:00" in display.label


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

    assert display.completed == 8
    assert display.total == 8
    assert "readiness 8/8" in display.label
    assert "ACTIVE" in display.label
    assert "Live Trading" in display.label
    assert "Current action guards" in display.tooltip
    assert "fresh regular-session quotes" in display.tooltip
