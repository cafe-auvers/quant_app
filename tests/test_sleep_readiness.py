"""Tests for the guarded-sleep readiness snapshot."""
from __future__ import annotations

from types import SimpleNamespace

from src.core.order_state import OrderStatus
from src.services.sleep_readiness import build_sleep_readiness_snapshot


def _window(
    *, is_main, items=(), open_orders=(), reconciling=False, reconciliation_required=False
):
    handoff_worker = None
    if reconciling:
        handoff_worker = SimpleNamespace(isRunning=lambda: True)
    return SimpleNamespace(
        state_sync_role=SimpleNamespace(is_main=is_main),
        buylist_manager=SimpleNamespace(items=list(items)),
        order_ledger=list(open_orders),
        handoff_reconciliation_worker=handoff_worker,
        _handoff_reconciliation_required=reconciliation_required,
    )


def test_pull_only_device_is_always_safe_to_sleep():
    item = SimpleNamespace(environment="PROD", monitoring_status="BOUGHT")
    window = _window(is_main=False, items=[item])

    snapshot = build_sleep_readiness_snapshot(window)

    assert snapshot["is_main_device"] is False
    assert snapshot["safe_to_sleep"] is True


def test_main_device_with_nothing_in_flight_is_safe_to_sleep():
    window = _window(is_main=True, items=[])

    snapshot = build_sleep_readiness_snapshot(window)

    assert snapshot["safe_to_sleep"] is True


def test_main_device_with_in_flight_prod_symbol_is_unsafe_to_sleep():
    item = SimpleNamespace(environment="PROD", monitoring_status="BOUGHT")
    window = _window(is_main=True, items=[item])

    snapshot = build_sleep_readiness_snapshot(window)

    assert snapshot["in_flight_prod_symbol_count"] == 1
    assert snapshot["safe_to_sleep"] is False


def test_main_device_reconciling_is_unsafe_to_sleep():
    window = _window(is_main=True, reconciling=True)

    snapshot = build_sleep_readiness_snapshot(window)

    assert snapshot["handoff_reconciliation_in_progress"] is True
    assert snapshot["safe_to_sleep"] is False


def test_main_device_waiting_for_handoff_reconciliation_is_unsafe_to_sleep():
    window = _window(is_main=True, reconciliation_required=True)

    snapshot = build_sleep_readiness_snapshot(window)

    assert snapshot["handoff_reconciliation_required"] is True
    assert snapshot["safe_to_sleep"] is False


def test_legacy_active_status_does_not_count_as_in_flight():
    item = SimpleNamespace(environment="PROD", monitoring_status="ACTIVE")
    window = _window(is_main=True, items=[item])

    snapshot = build_sleep_readiness_snapshot(window)

    assert snapshot["in_flight_prod_symbol_count"] == 0
    assert snapshot["safe_to_sleep"] is True


def test_sim_items_never_count_as_in_flight():
    item = SimpleNamespace(environment="SIM", monitoring_status="BOUGHT")
    window = _window(is_main=True, items=[item])

    snapshot = build_sleep_readiness_snapshot(window)

    assert snapshot["in_flight_prod_symbol_count"] == 0
    assert snapshot["safe_to_sleep"] is True


def test_stale_sim_open_order_does_not_block_prod_sleep_readiness():
    sim_order = SimpleNamespace(environment="SIM", status=OrderStatus.WORKING)
    window = _window(is_main=True, open_orders=[sim_order])

    snapshot = build_sleep_readiness_snapshot(window)

    assert snapshot["has_open_broker_orders"] is False
    assert snapshot["safe_to_sleep"] is True


def test_prod_open_order_still_blocks_sleep_readiness():
    prod_order = SimpleNamespace(environment="PROD", status=OrderStatus.WORKING)
    window = _window(is_main=True, open_orders=[prod_order])

    snapshot = build_sleep_readiness_snapshot(window)

    assert snapshot["has_open_broker_orders"] is True
    assert snapshot["safe_to_sleep"] is False
