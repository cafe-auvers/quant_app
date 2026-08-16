from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

from src.core.runtime_readiness import (
    EngineReadiness,
    RuntimeDeviceState,
    ShutdownExposure,
    decide_shutdown_lease_release,
)
from src.services.runtime_device_state_repository import (
    confirm_standby_handoff,
    find_confirmed_standby_successor,
    get_runtime_device_state,
    save_runtime_device_state,
)


_READY = dict(
    lease_current=True,
    startup_reconciliation_complete=True,
    account_reconciliation_fresh=True,
    websocket_connected=True,
    critical_trade_subscriptions_acked=True,
    critical_quote_subscriptions_acked=True,
    critical_quotes_fresh=True,
    accumulator_draining_within_budget=True,
    database_writable=True,
    device_active=True,
)


@pytest.mark.parametrize("condition", tuple(_READY))
def test_engine_health_fails_closed_for_each_required_condition(condition):
    values = dict(_READY)
    values[condition] = False
    assert EngineReadiness(**values).healthy is False


def test_engine_health_requires_every_condition():
    assert EngineReadiness(**_READY).healthy is True


def test_runtime_device_state_requires_explicit_fresh_handoff_confirmation(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'runtime-state.db'}",
        future=True,
        poolclass=NullPool,
    )
    ready = save_runtime_device_state(
        engine,
        device_id="successor",
        hostname="PC",
        state=RuntimeDeviceState.STANDBY_READY,
    )
    assert (
        find_confirmed_standby_successor(
            engine,
            excluding_device_id="current",
            expected_outgoing_lease_epoch=7,
        )
        is None
    )
    assert confirm_standby_handoff(
        engine,
        device_id="successor",
        readiness_generation=ready.readiness_generation,
        outgoing_lease_epoch=7,
    ) is True
    confirmed = get_runtime_device_state(engine, device_id="successor")
    assert confirmed.updated_at == ready.updated_at
    assert confirmed.confirmed_generation == ready.readiness_generation
    assert confirmed.confirmed_by_lease_epoch == 7
    assert find_confirmed_standby_successor(
        engine,
        excluding_device_id="current",
        expected_outgoing_lease_epoch=0,
    ) is None
    # The successor keeps heartbeating while the outgoing owner publishes
    # and releases; that heartbeat must not erase the confirmation.
    save_runtime_device_state(
        engine,
        device_id="successor",
        hostname="PC",
        state=RuntimeDeviceState.STANDBY_READY,
    )
    assert find_confirmed_standby_successor(
        engine,
        excluding_device_id="current",
        expected_outgoing_lease_epoch=8,
    ) is None
    successor = find_confirmed_standby_successor(
        engine,
        excluding_device_id="current",
        now=dt.datetime.now(dt.timezone.utc),
        expected_outgoing_lease_epoch=7,
    )
    assert successor is not None
    assert successor.state == RuntimeDeviceState.STANDBY_READY
    assert successor.handoff_confirmed is True


def test_readiness_loss_demotes_and_recovery_mints_a_new_generation(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'runtime-generation.db'}",
        future=True,
        poolclass=NullPool,
    )
    first = save_runtime_device_state(
        engine,
        device_id="successor",
        hostname="PC",
        state=RuntimeDeviceState.STANDBY_READY,
    )
    assert confirm_standby_handoff(
        engine,
        device_id="successor",
        readiness_generation=first.readiness_generation,
        outgoing_lease_epoch=4,
    )

    demoted = save_runtime_device_state(
        engine,
        device_id="successor",
        hostname="PC",
        state=RuntimeDeviceState.STANDBY,
    )
    recovered = save_runtime_device_state(
        engine,
        device_id="successor",
        hostname="PC",
        state=RuntimeDeviceState.STANDBY_READY,
    )

    assert demoted.handoff_confirmed is False
    assert recovered.readiness_generation == first.readiness_generation + 1
    assert recovered.handoff_confirmed is False


def test_unknown_exposure_is_never_treated_as_flat():
    exposure = ShutdownExposure(
        inspection_confirmed=False,
        inspection_error="database unavailable",
    )
    decision = decide_shutdown_lease_release(
        exposure,
        successor_standby_ready=False,
        handoff_confirmed=False,
        unattended=True,
    )
    assert exposure.is_clear is False
    assert "UNKNOWN EXPOSURE" in exposure.labels[0]
    assert decision.allowed is False


def test_shutdown_with_open_positions_and_no_successor_is_refused_in_unattended_mode():
    decision = decide_shutdown_lease_release(
        ShutdownExposure(open_positions=("1/AAPL",)),
        successor_standby_ready=False,
        handoff_confirmed=False,
        unattended=True,
    )
    assert decision.allowed is False
    assert "AAPL" in decision.reason


def test_shutdown_proceeds_once_a_successor_is_standby_ready():
    decision = decide_shutdown_lease_release(
        ShutdownExposure(
            open_positions=("1/AAPL",),
            working_orders=("1/MSFT order",),
        ),
        successor_standby_ready=True,
        handoff_confirmed=True,
        unattended=True,
    )
    assert decision.allowed is True


def test_supervised_shutdown_requires_explicit_unprotected_acceptance():
    exposure = ShutdownExposure(open_positions=("1/AAPL",))
    blocked = decide_shutdown_lease_release(
        exposure,
        successor_standby_ready=False,
        handoff_confirmed=False,
        unattended=False,
    )
    accepted = decide_shutdown_lease_release(
        exposure,
        successor_standby_ready=False,
        handoff_confirmed=False,
        unattended=False,
        explicit_unprotected_acceptance=True,
    )
    assert blocked.allowed is False
    assert accepted.allowed is True
