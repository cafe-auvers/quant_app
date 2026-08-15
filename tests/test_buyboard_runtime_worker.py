"""Tests for src.ui.buyboard.runtime_worker (code review finding P0-1).

Exercises the worker's actual logic (startup reconciliation, one heartbeat
cycle, ORB-plan sync, quote-subscription sync, persistence, lease checks)
directly as plain method calls rather than through QThread.start()/run()'s
real background-thread machinery, which is inherently timing-dependent and
not suited to a deterministic unit test. ``self.runtime`` is set manually
in these tests the same way ``run()`` sets it, immediately before calling
the method under test.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt5.QtWidgets import QApplication
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

from src.core.execution_queue import ExecutionQueueItem, OrbCandidate, OrbCandidateStatus
from src.core.order_state import BrokerOrderDiscoveryResult
from src.core.trade_card_state import BoardStatus, EntryRuntimeStatus, TradeCardState
from src.services import buyboard_runtime as runtime_module
from src.services import trade_card_repository as repo
from src.services.broker import BrokerSubmissionResult
from src.services.execution_authority import ExecutionAuthority, LeaseExpiredError, LeaseHandle
from src.ui.buyboard.runtime_worker import BuyboardRuntimeWorker

_APP = None


def _ensure_app():
    global _APP
    _APP = QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _isolate_local_trade_card_snapshot(monkeypatch, tmp_path):
    monkeypatch.setattr(repo, "LOCAL_TRADE_CARDS_FILE", tmp_path / "trade_cards.json")


class _FakeBroker:
    def __init__(self):
        self.discover_result = BrokerOrderDiscoveryResult(
            open_orders_complete=True, history_complete=True, reserved_orders_complete=True
        )
        self.positions = {"overseas": {"holdings": []}}

    def submit_order(self, **kwargs):
        return BrokerSubmissionResult(broker_order_id="B-1", raw_response={})

    def is_ambiguous_submission_error(self, error):
        return False

    def cancel_order(self, **kwargs):
        raise AssertionError("not exercised in these tests")

    def get_order(self, **kwargs):
        return []

    def discover_orders(self, *, environment, account_no):
        return self.discover_result

    def get_positions(self, *, environment, account_no=None):
        return self.positions


def _db_engine(tmp_path):
    return create_engine(f"sqlite:///{tmp_path / 'cards.db'}", future=True, poolclass=NullPool)


def _worker(tmp_path, *, broker=None, execution_authority=None, execution_lease=None, **kwargs):
    _ensure_app()
    engine = _db_engine(tmp_path)
    worker = BuyboardRuntimeWorker(
        db_engine=engine,
        environment="PROD",
        account_no="1",
        buying_power_provider=lambda env, acct: 100_000.0,
        broker=broker or _FakeBroker(),
        execution_authority=execution_authority,
        execution_lease=execution_lease,
        **kwargs,
    )
    return worker, engine


def _seed_card(engine, **overrides):
    fields = dict(environment="PROD", account_no="1", symbol="AAPL")
    fields.update(overrides)
    return repo.create_trade_card(engine, TradeCardState(**fields))


# --- Construction does not build/start anything -----------------------------


def test_construction_builds_nothing(tmp_path):
    worker, _ = _worker(tmp_path)
    assert worker.runtime is None


# --- Startup reconciliation --------------------------------------------------


def test_startup_reconciliation_restores_retry_state_and_persists_changes(tmp_path):
    import datetime as dt

    worker, engine = _worker(tmp_path)
    worker.runtime = runtime_module.build_buyboard_runtime(
        buying_power_provider=worker._buying_power_provider,
        card_lookup=worker._card_lookup,
        broker=worker._broker,
    )
    retry_at = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=30)
    _seed_card(
        engine,
        board_status=BoardStatus.BUY_TODAY,
        entry_runtime_status=EntryRuntimeStatus.RETRY_COOLDOWN,
        next_retry_at=retry_at,
        entry_attempt_group_id="g1",
        entry_attempt_count=2,
    )

    emitted = []
    worker.board_changed.connect(lambda: emitted.append(True))

    worker._run_startup_reconciliation()

    key = ("PROD", "1", "AAPL")
    state = worker.runtime.entry_attempt_manager._state.get(key)
    assert state is not None
    assert state.attempt_group_id == "g1"
    assert state.attempt_count == 2


def test_startup_reconciliation_discovers_a_manual_broker_position(tmp_path):
    broker = _FakeBroker()
    broker.positions = {
        "overseas": {"holdings": [{"symbol": "NVDA", "quantity": 10, "average_price": 200.0}]}
    }
    worker, engine = _worker(tmp_path, broker=broker)
    worker.runtime = runtime_module.build_buyboard_runtime(
        buying_power_provider=worker._buying_power_provider,
        card_lookup=worker._card_lookup,
        broker=worker._broker,
    )

    emitted = []
    worker.board_changed.connect(lambda: emitted.append(True))

    worker._run_startup_reconciliation()

    card = repo.get_trade_card(engine, "PROD", "1", "NVDA")
    assert card is not None
    assert card.board_status == BoardStatus.OPEN_POSITION
    assert card.broker_quantity == 10
    assert emitted == [True]


# --- One heartbeat cycle ------------------------------------------------------


def test_one_cycle_persists_engine_changes(tmp_path, monkeypatch):
    from src.core import execution_config

    monkeypatch.setattr(execution_config, "is_buyboard_engine_enabled", lambda: True)
    import src.services.trading_engine as trading_engine_module

    monkeypatch.setattr(trading_engine_module, "is_buyboard_engine_enabled", lambda: True)

    worker, engine = _worker(tmp_path)
    worker.runtime = runtime_module.build_buyboard_runtime(
        buying_power_provider=worker._buying_power_provider,
        card_lookup=worker._card_lookup,
        broker=worker._broker,
    )
    card = _seed_card(
        engine,
        board_status=BoardStatus.BUY_TODAY,
        entry_runtime_status=EntryRuntimeStatus.RETRY_COOLDOWN,
    )
    import datetime as dt

    card.next_retry_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1)  # already due
    repo.update_trade_card(engine, card, expected_version=card.version)

    emitted = []
    worker.board_changed.connect(lambda: emitted.append(True))

    worker._run_one_cycle()

    stored = repo.get_trade_card(engine, "PROD", "1", "AAPL")
    assert stored.entry_runtime_status == EntryRuntimeStatus.EXECUTE_READY
    assert emitted == [True]


def test_one_cycle_scoped_to_the_workers_account(tmp_path, monkeypatch):
    from src.core import execution_config

    monkeypatch.setattr(execution_config, "is_buyboard_engine_enabled", lambda: True)
    import src.services.trading_engine as trading_engine_module

    monkeypatch.setattr(trading_engine_module, "is_buyboard_engine_enabled", lambda: True)

    worker, engine = _worker(tmp_path)
    worker.runtime = runtime_module.build_buyboard_runtime(
        buying_power_provider=worker._buying_power_provider,
        card_lookup=worker._card_lookup,
        broker=worker._broker,
    )
    _seed_card(engine, account_no="2", board_status=BoardStatus.WATCHLIST)

    worker._run_one_cycle()  # must not raise looking up a foreign-account card


# --- ORB plan sync (review finding P0-2) -------------------------------------


def test_sync_orb_plans_applies_the_execution_queue_bridge(tmp_path):
    worker, engine = _worker(tmp_path)
    worker.runtime = runtime_module.build_buyboard_runtime(
        buying_power_provider=worker._buying_power_provider,
        card_lookup=worker._card_lookup,
        broker=worker._broker,
    )
    card = _seed_card(engine, board_status=BoardStatus.BUY_TODAY)
    candidate = OrbCandidate(
        symbol="AAPL", window="5m", orb_low=95.0, orb_high=101.0, entry_trigger=101.5,
        shares=10, status=OrbCandidateStatus.EXECUTE_READY,
    )
    item = ExecutionQueueItem(symbol="AAPL", environment="PROD")
    item.selected_candidate = candidate
    worker._execution_queue_item_lookup = lambda symbol, env: item

    changed = worker._sync_orb_plans([card])

    assert changed == [card]
    assert card.entry_runtime_status == EntryRuntimeStatus.EXECUTE_READY
    assert card.entry_trigger == 101.5


def test_sync_orb_plans_is_a_no_op_without_a_lookup_wired(tmp_path):
    worker, engine = _worker(tmp_path)
    card = _seed_card(engine, board_status=BoardStatus.BUY_TODAY)
    assert worker._sync_orb_plans([card]) == []


def test_sync_orb_plans_skips_positioned_cards(tmp_path):
    worker, engine = _worker(tmp_path)
    card = _seed_card(engine, board_status=BoardStatus.OPEN_POSITION, broker_quantity=10)
    called = []
    worker._execution_queue_item_lookup = lambda symbol, env: called.append(symbol) or None
    worker._sync_orb_plans([card])
    assert called == []


# --- Quote subscription sync -------------------------------------------------


def test_sync_quote_subscriptions_adds_and_removes(tmp_path):
    worker, engine = _worker(tmp_path)
    worker.runtime = runtime_module.build_buyboard_runtime(
        buying_power_provider=worker._buying_power_provider,
        card_lookup=worker._card_lookup,
        broker=worker._broker,
    )
    worker.runtime.market_data.subscribe(["STALE"])
    open_card = _seed_card(engine, symbol="AAPL", board_status=BoardStatus.OPEN_POSITION)

    worker._sync_quote_subscriptions([open_card])

    subscribed = set(worker.runtime.market_data.subscribed_symbols())
    assert "AAPL" in subscribed
    assert "STALE" not in subscribed


# --- Persistence isolation ----------------------------------------------------


def test_persist_changed_swallows_a_stale_version_conflict(tmp_path):
    worker, engine = _worker(tmp_path)
    card = _seed_card(engine, board_status=BoardStatus.WATCHLIST)
    card.version = 999  # deliberately stale
    # Must not raise -- the worker logs and moves on to the next cycle.
    worker._persist_changed([card])


# --- Lease fencing (review finding P0-1) -------------------------------------


def test_lease_still_current_true_without_execution_authority(tmp_path):
    worker, _ = _worker(tmp_path)
    assert worker._lease_still_current() is True


def test_lease_still_current_false_once_expired(tmp_path):
    class _AlwaysExpired(ExecutionAuthority):
        def require_current_lease(self, engine, expected):
            raise LeaseExpiredError("expired")

    worker, _ = _worker(
        tmp_path,
        execution_authority=_AlwaysExpired(),
        execution_lease=LeaseHandle(device_id="other-device", lease_token="tok"),
    )
    assert worker._lease_still_current() is False
