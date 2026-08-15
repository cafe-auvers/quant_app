"""Tests for src.services.buying_power_cache (code review finding P0-1)."""
from __future__ import annotations

import datetime as dt

import pytest

from src.services import buying_power_cache


@pytest.fixture(autouse=True)
def _isolated_cache():
    buying_power_cache.clear()
    yield
    buying_power_cache.clear()


def test_no_snapshot_recorded_fails_closed():
    provider = buying_power_cache.make_buying_power_provider()
    assert provider("PROD", "123-01") == 0.0


def test_fresh_snapshot_is_returned():
    buying_power_cache.record_snapshot(
        environment="PROD", account_no="123-01",
        usable_buying_power_usd=5_000.0, total_equity_usd=20_000.0,
    )
    provider = buying_power_cache.make_buying_power_provider()
    equity_provider = buying_power_cache.make_account_equity_provider()

    assert provider("PROD", "123-01") == 5_000.0
    assert equity_provider("PROD", "123-01") == 20_000.0


def test_stale_snapshot_fails_closed():
    clock_time = dt.datetime(2026, 1, 5, 14, 30, tzinfo=dt.timezone.utc)
    buying_power_cache.record_snapshot(
        environment="PROD", account_no="123-01",
        usable_buying_power_usd=5_000.0, total_equity_usd=20_000.0,
        received_at=clock_time,
    )
    provider = buying_power_cache.make_buying_power_provider(
        max_age_seconds=15.0,
        clock=lambda: clock_time + dt.timedelta(seconds=16),
    )

    assert provider("PROD", "123-01") == 0.0


def test_snapshot_within_max_age_is_still_usable():
    clock_time = dt.datetime(2026, 1, 5, 14, 30, tzinfo=dt.timezone.utc)
    buying_power_cache.record_snapshot(
        environment="PROD", account_no="123-01",
        usable_buying_power_usd=5_000.0, total_equity_usd=20_000.0,
        received_at=clock_time,
    )
    provider = buying_power_cache.make_buying_power_provider(
        max_age_seconds=15.0,
        clock=lambda: clock_time + dt.timedelta(seconds=14),
    )

    assert provider("PROD", "123-01") == 5_000.0


def test_snapshots_are_scoped_per_account_not_shared_across_accounts():
    """The manual-account-size figure this replaces ignored account_no
    entirely; two accounts in the same environment must never share one
    number here."""
    buying_power_cache.record_snapshot(
        environment="PROD", account_no="111-01",
        usable_buying_power_usd=1_000.0, total_equity_usd=1_000.0,
    )
    buying_power_cache.record_snapshot(
        environment="PROD", account_no="222-01",
        usable_buying_power_usd=9_000.0, total_equity_usd=9_000.0,
    )
    provider = buying_power_cache.make_buying_power_provider()

    assert provider("PROD", "111-01") == 1_000.0
    assert provider("PROD", "222-01") == 9_000.0
    assert provider("PROD", "333-01") == 0.0  # never-seen account fails closed


def test_negative_values_are_clamped_to_zero():
    snapshot = buying_power_cache.record_snapshot(
        environment="PROD", account_no="123-01",
        usable_buying_power_usd=-50.0, total_equity_usd=-1.0,
    )
    assert snapshot.usable_buying_power_usd == 0.0
    assert snapshot.total_equity_usd == 0.0


def test_environment_and_account_are_normalized():
    buying_power_cache.record_snapshot(
        environment="prod", account_no="123-01",
        usable_buying_power_usd=100.0, total_equity_usd=100.0,
    )
    provider = buying_power_cache.make_buying_power_provider()
    assert provider("PROD", "123-01") == 100.0
