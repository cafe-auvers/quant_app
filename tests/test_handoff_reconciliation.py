"""Tests for the pre-resume broker reconciliation safety gate.

The single most safety-critical piece of the automatic laptop<->PC handoff:
before a newly-main device resumes monitoring/auto-submission, it must
confirm -- against the broker directly, never against synced local state --
that nothing is already in flight. These tests exercise that gate with a
stub broker so no real KIS credentials or network access are ever involved.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.core.order_state import (
    BrokerOrderDiscoveryResult,
    BrokerOrderStatusSnapshot,
    OrderSide,
    OrderStatus,
)
from src.services.handoff_reconciliation import (
    PostClaimReconciliationResult,
    in_flight_buylist_items,
    reset_runtime_only_order_flags,
    run_post_claim_broker_reconciliation,
)


class StubBroker:
    """Records every call; never touches a real broker."""

    def __init__(self, *, open_orders=None, positions=None, raise_on_get_order=None, raise_on_get_positions=None):
        self.open_orders = open_orders or []
        self.positions = positions or {}
        self.raise_on_get_order = raise_on_get_order
        self.raise_on_get_positions = raise_on_get_positions
        self.get_order_calls = []
        self.get_positions_calls = []
        self.submit_order_calls = []

    def get_order(self, **kwargs):
        self.get_order_calls.append(kwargs)
        if self.raise_on_get_order:
            raise self.raise_on_get_order
        return self.open_orders

    def discover_orders(self, **kwargs):
        self.get_order_calls.append(kwargs)
        if self.raise_on_get_order:
            raise self.raise_on_get_order
        return BrokerOrderDiscoveryResult(
            snapshots=list(self.open_orders),
            open_orders_complete=True,
            history_complete=True,
            reserved_orders_complete=True,
        )

    def get_positions(self, **kwargs):
        self.get_positions_calls.append(kwargs)
        if self.raise_on_get_positions:
            raise self.raise_on_get_positions
        return self.positions

    def submit_order(self, **kwargs):
        # Never expected to be called by reconciliation -- recorded so the
        # contract test can assert on it.
        self.submit_order_calls.append(kwargs)
        raise AssertionError("Reconciliation must never submit a broker order")


def _item(
    symbol,
    *,
    environment="PROD",
    status="BOUGHT",
    shares_held=0,
    avg_cost=0.0,
    kis_account_no="12345678-01",
):
    return SimpleNamespace(
        symbol=symbol,
        environment=environment,
        monitoring_status=status,
        shares_held=shares_held,
        avg_cost=avg_cost,
        kis_account_no=kis_account_no,
    )


def _manager(*items):
    return SimpleNamespace(items=list(items))


def _open_order_snapshot(symbol, side=OrderSide.SELL, status=OrderStatus.WORKING):
    return BrokerOrderStatusSnapshot(
        environment="PROD",
        account_no="12345678-01",
        symbol=symbol,
        side=side,
        status=status,
    )


def _holdings(symbol, quantity, average_price):
    return {
        "overseas": {
            "holdings": [
                {"symbol": symbol, "quantity": quantity, "average_price": average_price}
            ]
        }
    }


# --- in_flight_buylist_items / reset_runtime_only_order_flags -------------


def test_in_flight_buylist_items_only_includes_handoff_monitorable_statuses():
    bought = _item("AAPL", status="BOUGHT")
    execute_ready = _item("MSFT", status="EXECUTE_READY")
    watching = _item("TSLA", status="WATCHING")
    sold = _item("NVDA", status="SOLD")
    legacy_active = _item("AMD", status="ACTIVE")
    manager = _manager(bought, execute_ready, watching, sold, legacy_active)

    items = in_flight_buylist_items(manager)

    symbols = {item.symbol for item in items}
    assert symbols == {"AAPL", "MSFT"}
    # Legacy ACTIVE must never be treated as handoff-resumable -- monitoring
    # already refuses legacy ACTIVE auto-buy.
    assert "AMD" not in symbols


def test_in_flight_buylist_items_filters_by_environment():
    prod_item = _item("AAPL", environment="PROD", status="BOUGHT")
    sim_item = _item("AAPL", environment="SIM", status="BOUGHT")
    manager = _manager(prod_item, sim_item)

    items = in_flight_buylist_items(manager, environment="PROD")

    assert items == [prod_item]


def test_reset_runtime_only_order_flags_marks_only_in_flight_items():
    bought = _item("AAPL", status="BOUGHT")
    watching = _item("TSLA", status="WATCHING")
    manager = _manager(bought, watching)

    touched = reset_runtime_only_order_flags(manager)

    assert touched == [bought]
    assert bought._buy_order_pending is True
    assert bought._stop_order_pending is True
    assert bought._exit_order_pending is True
    assert not hasattr(watching, "_buy_order_pending")


def test_reset_runtime_only_order_flags_handles_empty_manager():
    assert reset_runtime_only_order_flags(SimpleNamespace(items=[])) == []
    assert reset_runtime_only_order_flags(None) == []


# --- run_post_claim_broker_reconciliation ---------------------------------


def test_reconciliation_is_ok_noop_when_nothing_in_flight():
    manager = _manager(_item("AAPL", status="WATCHING"))
    broker = StubBroker()

    result = run_post_claim_broker_reconciliation(manager, broker=broker)

    assert result.ok is True
    assert result.reconciled_symbols == []
    assert broker.get_order_calls == []


def test_reconciliation_clears_symbol_when_broker_confirms_no_open_order():
    item = _item("AAPL", status="BOUGHT", shares_held=10, avg_cost=100.0)
    item._buy_order_pending = True
    item._stop_order_pending = True
    manager = _manager(item)
    broker = StubBroker(open_orders=[], positions=_holdings("AAPL", 10, 100.0))

    result = run_post_claim_broker_reconciliation(manager, broker=broker)

    assert result.ok is True
    assert result.reconciled_symbols == ["AAPL"]
    assert result.blocked_symbols == []
    assert item._buy_order_pending is False
    assert item._stop_order_pending is False
    # Account-wide discovery is partitioned by the item's persisted account.
    assert broker.get_order_calls[0]["account_no"] == "12345678-01"


def test_reconciliation_blocks_symbol_with_open_broker_order_local_ledger_never_knew_about():
    """The exact scenario the first design draft missed.

    The PC's local orders.json has never heard of this order (it was placed
    by the laptop) -- discovery must still find it via the account-wide
    broker query, not a local-ledger-driven lookup.
    """
    item = _item("AAPL", status="EXECUTE_READY")
    manager = _manager(item)
    broker = StubBroker(open_orders=[_open_order_snapshot("AAPL", side=OrderSide.BUY)])

    result = run_post_claim_broker_reconciliation(manager, broker=broker)

    assert result.ok is False
    assert result.blocked_symbols == ["AAPL"]
    assert result.reconciled_symbols == []


def test_reconciliation_ignores_closed_orders_for_the_same_symbol():
    item = _item("AAPL", status="BOUGHT", shares_held=10, avg_cost=100.0)
    manager = _manager(item)
    broker = StubBroker(
        open_orders=[_open_order_snapshot("AAPL", status=OrderStatus.FILLED)],
        positions=_holdings("AAPL", 10, 100.0),
    )

    result = run_post_claim_broker_reconciliation(manager, broker=broker)

    assert result.ok is True
    assert result.reconciled_symbols == ["AAPL"]


def test_reconciliation_blocks_when_broker_shows_no_position_for_bought_item():
    item = _item("AAPL", status="BOUGHT", shares_held=10, avg_cost=100.0)
    manager = _manager(item)
    broker = StubBroker(open_orders=[], positions={})

    result = run_post_claim_broker_reconciliation(manager, broker=broker)

    assert result.ok is False
    assert result.blocked_symbols == ["AAPL"]
    # An unambiguous disagreement is left for manual review, not silently corrected.
    assert item.shares_held == 10


def test_reconciliation_corrects_shares_held_from_broker_truth_when_unambiguous():
    item = _item("AAPL", status="BOUGHT", shares_held=10, avg_cost=100.0)
    manager = _manager(item)
    # No open order, but broker holds a different (nonzero) quantity than
    # local state -- a fill that raced the shutdown. Correct, don't block.
    broker = StubBroker(open_orders=[], positions=_holdings("AAPL", 15, 101.5))

    result = run_post_claim_broker_reconciliation(manager, broker=broker)

    assert result.ok is True
    assert result.reconciled_symbols == ["AAPL"]
    assert item.shares_held == 15
    assert item.avg_cost == 101.5


def test_reconciliation_blocks_pre_entry_item_when_broker_already_has_position():
    item = _item("AAPL", status="EXECUTE_READY", shares_held=0)
    broker = StubBroker(
        open_orders=[],
        positions=_holdings("AAPL", 10, 101.5),
    )

    result = run_post_claim_broker_reconciliation(_manager(item), broker=broker)

    assert result.ok is False
    assert result.blocked_symbols == ["AAPL"]
    assert not hasattr(item, "_buy_order_pending") or item._buy_order_pending is not False


def test_reconciliation_blocks_account_on_unknown_symbol_less_snapshot():
    item = _item("AAPL", status="EXECUTE_READY")
    unknown = _open_order_snapshot("", status=OrderStatus.UNKNOWN)
    broker = StubBroker(open_orders=[unknown], positions={})

    result = run_post_claim_broker_reconciliation(_manager(item), broker=broker)

    assert result.ok is False
    assert result.blocked_symbols == ["AAPL"]
    assert any("ambiguous" in error.lower() for error in result.errors)


def test_reconciliation_blocks_account_when_any_order_source_is_incomplete():
    item = _item("AAPL", status="EXECUTE_READY")

    class IncompleteBroker(StubBroker):
        def discover_orders(self, **kwargs):
            return BrokerOrderDiscoveryResult(
                open_orders_complete=False,
                history_complete=True,
                reserved_orders_complete=True,
                errors=["inquire-nccs unavailable"],
            )

    result = run_post_claim_broker_reconciliation(
        _manager(item), broker=IncompleteBroker()
    )

    assert result.ok is False
    assert result.blocked_symbols == ["AAPL"]
    assert any("inquire-nccs" in error for error in result.errors)


def test_reconciliation_blocks_reserved_open_order():
    item = _item("AAPL", status="SELL_RESERVED", shares_held=10)
    reserved = _open_order_snapshot("AAPL", side=OrderSide.SELL)
    broker = StubBroker(
        open_orders=[reserved],
        positions=_holdings("AAPL", 10, 100.0),
    )

    result = run_post_claim_broker_reconciliation(_manager(item), broker=broker)

    assert result.ok is False
    assert result.blocked_symbols == ["AAPL"]


def test_reconciliation_partitions_items_by_persisted_account():
    first = _item("AAPL", status="BOUGHT", shares_held=10, kis_account_no="11111111-01")
    second = _item("MSFT", status="BOUGHT", shares_held=5, kis_account_no="22222222-01")

    class MultiAccountBroker(StubBroker):
        def discover_orders(self, **kwargs):
            self.get_order_calls.append(kwargs)
            return BrokerOrderDiscoveryResult(
                open_orders_complete=True,
                history_complete=True,
                reserved_orders_complete=True,
            )

        def get_positions(self, **kwargs):
            self.get_positions_calls.append(kwargs)
            if kwargs["account_no"] == "11111111-01":
                return _holdings("AAPL", 10, 100.0)
            return _holdings("MSFT", 5, 200.0)

    broker = MultiAccountBroker()
    result = run_post_claim_broker_reconciliation(
        _manager(first, second), broker=broker
    )

    assert result.ok is True
    assert {call["account_no"] for call in broker.get_order_calls} == {
        "11111111-01",
        "22222222-01",
    }
    assert {call["account_no"] for call in broker.get_positions_calls} == {
        "11111111-01",
        "22222222-01",
    }


def test_reconciliation_blocks_item_without_persisted_account():
    item = _item("AAPL", status="EXECUTE_READY", kis_account_no="")
    broker = StubBroker()

    result = run_post_claim_broker_reconciliation(_manager(item), broker=broker)

    assert result.ok is False
    assert result.blocked_symbols == ["AAPL"]
    assert broker.get_order_calls == []


def test_reconciliation_blocks_all_in_flight_symbols_on_open_order_query_failure():
    manager = _manager(_item("AAPL", status="BOUGHT"), _item("MSFT", status="EXECUTE_READY"))
    broker = StubBroker(raise_on_get_order=RuntimeError("KIS API unavailable"))

    result = run_post_claim_broker_reconciliation(manager, broker=broker)

    assert result.ok is False
    assert set(result.blocked_symbols) == {"AAPL", "MSFT"}
    assert result.errors


def test_reconciliation_blocks_all_in_flight_symbols_on_positions_query_failure():
    manager = _manager(_item("AAPL", status="BOUGHT"))
    broker = StubBroker(raise_on_get_positions=RuntimeError("KIS API unavailable"))

    result = run_post_claim_broker_reconciliation(manager, broker=broker)

    assert result.ok is False
    assert result.blocked_symbols == ["AAPL"]


def test_reconciliation_never_calls_submit_order(monkeypatch):
    """Contract test: this module must be read-only against the broker."""
    item = _item("AAPL", status="BOUGHT", shares_held=10, avg_cost=100.0)
    manager = _manager(item)
    broker = StubBroker(open_orders=[], positions=_holdings("AAPL", 10, 100.0))

    run_post_claim_broker_reconciliation(manager, broker=broker)

    assert broker.submit_order_calls == []


def test_reconciliation_emits_started_and_completed_events():
    item = _item("AAPL", status="BOUGHT", shares_held=10, avg_cost=100.0)
    manager = _manager(item)
    broker = StubBroker(open_orders=[], positions=_holdings("AAPL", 10, 100.0))
    events = []

    def fake_recorder(event_type, **kwargs):
        events.append((event_type, kwargs))

    run_post_claim_broker_reconciliation(manager, broker=broker, event_recorder=fake_recorder)

    event_types = [event_type for event_type, _ in events]
    assert event_types[0].value == "RECONCILIATION_STARTED"
    assert event_types[-1].value == "RECONCILIATION_COMPLETED"


def test_reconciliation_event_recorder_failure_never_breaks_reconciliation():
    item = _item("AAPL", status="BOUGHT", shares_held=10, avg_cost=100.0)
    manager = _manager(item)
    broker = StubBroker(open_orders=[], positions=_holdings("AAPL", 10, 100.0))

    def broken_recorder(*args, **kwargs):
        raise RuntimeError("journal write failed")

    result = run_post_claim_broker_reconciliation(manager, broker=broker, event_recorder=broken_recorder)

    assert result.ok is True
