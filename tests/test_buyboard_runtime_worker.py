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
from src.services.realtime_market_data import QuoteSnapshot, RestPollingMarketDataService
from src.ui.buyboard.runtime_worker import BuyboardRuntimeWorker


def _dummy_market_data() -> RestPollingMarketDataService:
    """A lightweight, network-free quote source for tests that care about
    real elapsed time between _run_one_cycle() calls -- the default
    KIS-only quote fetcher build_buyboard_runtime() falls back to makes a
    genuine (slow, failing-without-credentials) network call for every
    subscribed symbol, which otherwise burns several real seconds per
    cycle and corrupts any test asserting on refresh-interval timing.
    """
    return RestPollingMarketDataService(
        quote_fetcher=lambda symbol: QuoteSnapshot(symbol=symbol, last_price=100.0)
    )

_APP = None


def _ensure_app():
    global _APP
    _APP = QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _isolate_local_trade_card_snapshot(monkeypatch, tmp_path):
    monkeypatch.setattr(repo, "LOCAL_TRADE_CARDS_FILE", tmp_path / "trade_cards.json")


@pytest.fixture(autouse=True)
def _isolated_buying_power_cache():
    from src.services import buying_power_cache

    buying_power_cache.clear()
    yield
    buying_power_cache.clear()


class _FakeBroker:
    def __init__(self):
        self.discover_result = BrokerOrderDiscoveryResult(
            open_orders_complete=True, history_complete=True, reserved_orders_complete=True
        )
        self.positions = {"overseas": {"holdings": []}}
        # Optional per-account override -- when set, get_positions returns
        # positions_by_account.get(account_no) instead of the single shared
        # self.positions, so multi-account tests can give two accounts
        # genuinely different holdings.
        self.positions_by_account: dict = {}
        self.get_positions_calls: list = []

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
        self.get_positions_calls.append(account_no)
        if self.positions_by_account:
            return self.positions_by_account.get(
                account_no, {"overseas": {"holdings": []}}
            )
        return self.positions


def _db_engine(tmp_path):
    return create_engine(f"sqlite:///{tmp_path / 'cards.db'}", future=True, poolclass=NullPool)


def _worker(
    tmp_path, *, broker=None, execution_authority=None, execution_lease=None,
    account_no="1", **kwargs,
):
    _ensure_app()
    engine = _db_engine(tmp_path)
    # account_discovery defaults to [] (not the real KIS-config-backed
    # discovery) so these tests stay hermetic regardless of what KIS
    # accounts happen to be configured in the developer's own .env --
    # tests that specifically want to exercise discovery override it.
    kwargs.setdefault("account_discovery", lambda: [])
    worker = BuyboardRuntimeWorker(
        db_engine=engine,
        environment="PROD",
        account_no=account_no,
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


def test_startup_reconciliation_scopes_each_account_to_its_own_holdings(tmp_path):
    """Regression for the multi-account bug: a single unscoped
    ``get_positions(account_no="")`` call previously reconciled *every*
    account's cards against one account's holdings, spuriously discovering
    phantom blank-account-no positions for every other account's real
    holdings. Each real account_no must be queried and reconciled
    independently.
    """
    broker = _FakeBroker()
    broker.positions_by_account = {
        "1": {"overseas": {"holdings": [{"symbol": "AAPL", "quantity": 10, "average_price": 100.0}]}},
        "2": {"overseas": {"holdings": [{"symbol": "MSFT", "quantity": 5, "average_price": 300.0}]}},
    }
    # account_no="" here models the real production wiring
    # (main_window.py's unscoped worker) -- self._distinct_account_numbers
    # must derive both real accounts purely from the seeded cards.
    worker, engine = _worker(tmp_path, broker=broker, account_no="")
    worker.runtime = runtime_module.build_buyboard_runtime(
        buying_power_provider=worker._buying_power_provider,
        card_lookup=worker._card_lookup,
        broker=worker._broker,
    )
    _seed_card(engine, account_no="1", symbol="AAPL", board_status=BoardStatus.WATCHLIST)
    _seed_card(engine, account_no="2", symbol="MSFT", board_status=BoardStatus.WATCHLIST)

    worker._run_startup_reconciliation()

    aapl = repo.get_trade_card(engine, "PROD", "1", "AAPL")
    msft = repo.get_trade_card(engine, "PROD", "2", "MSFT")
    assert aapl.board_status == BoardStatus.OPEN_POSITION
    assert aapl.broker_quantity == 10
    assert msft.board_status == BoardStatus.OPEN_POSITION
    assert msft.broker_quantity == 5
    # No phantom blank-account-no card was created for either symbol.
    assert repo.get_trade_card(engine, "PROD", "", "AAPL") is None
    assert repo.get_trade_card(engine, "PROD", "", "MSFT") is None
    assert set(broker.get_positions_calls) == {"1", "2"}


def test_distinct_account_numbers_includes_the_workers_own_scoped_account(tmp_path):
    """A specifically-scoped worker must always query its own account even
    before any card exists for it (needed to discover a first manual
    position with zero pre-existing cards)."""
    worker, _ = _worker(tmp_path, account_no="1")
    assert worker._distinct_account_numbers([]) == ["1"]


def test_startup_reconciliation_marks_incomplete_when_one_account_fails(tmp_path):
    """Review finding P0: "startup reconciliation reports success after
    account failures" -- one account's get_positions failure must not
    leave startup_reconciliation_complete True; the health check must be
    able to tell a genuinely-unreconciled account apart from a clean run.
    """

    class _PartiallyFailingBroker(_FakeBroker):
        def get_positions(self, *, environment, account_no=None):
            self.get_positions_calls.append(account_no)
            if account_no == "2":
                raise RuntimeError("simulated KIS outage for account 2")
            return {"overseas": {"holdings": []}}

    broker = _PartiallyFailingBroker()
    worker, engine = _worker(tmp_path, broker=broker, account_no="")
    worker.runtime = runtime_module.build_buyboard_runtime(
        buying_power_provider=worker._buying_power_provider,
        card_lookup=worker._card_lookup,
        broker=worker._broker,
    )
    _seed_card(engine, account_no="1", symbol="AAPL", board_status=BoardStatus.WATCHLIST)
    _seed_card(engine, account_no="2", symbol="MSFT", board_status=BoardStatus.WATCHLIST)

    worker._run_startup_reconciliation()

    assert worker.startup_reconciliation_complete is False
    assert "2" in worker.startup_reconciliation_errors
    assert "1" not in worker.startup_reconciliation_errors
    # The healthy account's own reconciliation still ran.
    assert "1" in worker._account_reconciled_at
    assert "2" not in worker._account_reconciled_at


def test_run_one_cycle_excludes_cards_from_accounts_with_startup_errors(tmp_path, monkeypatch):
    """Review finding P0: "the worker still enters its normal runtime loop
    ... regardless of whether reconciliation completed successfully" --
    Buy Board must not decide entries/exits for an account whose broker
    truth was never confirmed, even though the worker as a whole keeps
    running (and keeps retrying that account's periodic refresh)."""
    from src.core import execution_config

    monkeypatch.setattr(execution_config, "is_buyboard_engine_enabled", lambda: True)
    import src.services.trading_engine as trading_engine_module

    monkeypatch.setattr(trading_engine_module, "is_buyboard_engine_enabled", lambda: True)

    broker = _FakeBroker()
    worker, engine = _worker(tmp_path, broker=broker)
    worker.runtime = runtime_module.build_buyboard_runtime(
        buying_power_provider=worker._buying_power_provider,
        card_lookup=worker._card_lookup,
        broker=worker._broker,
        market_data=_dummy_market_data(),
    )
    card = _seed_card(
        engine,
        board_status=BoardStatus.BUY_TODAY,
        entry_runtime_status=EntryRuntimeStatus.RETRY_COOLDOWN,
    )
    import datetime as dt

    card.next_retry_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1)  # already due
    repo.update_trade_card(engine, card, expected_version=card.version)
    # Account "1" never actually reconciled at startup.
    worker.startup_reconciliation_errors = {"1": "simulated KIS outage"}
    # Prevent the periodic-refresh path (which legitimately still runs for
    # every account) from clearing the error out from under this test.
    worker._account_balance_refreshed_at["1"] = dt.datetime.now(dt.timezone.utc)
    worker._account_reconciled_at["1"] = dt.datetime.now(dt.timezone.utc)

    worker._run_one_cycle()

    stored = repo.get_trade_card(engine, "PROD", "1", "AAPL")
    # Never touched: still RETRY_COOLDOWN, not recovered to EXECUTE_READY.
    assert stored.entry_runtime_status == EntryRuntimeStatus.RETRY_COOLDOWN


def test_periodic_reconciliation_success_clears_startup_reconciliation_error(tmp_path, monkeypatch):
    """Review finding P0: "no periodic recovery path that removes an
    account from startup_reconciliation_errors ... this can leave the
    application permanently reporting the Buy Board as unhealthy.\""""
    import datetime as dt

    from src.core import execution_config

    monkeypatch.setattr(execution_config, "is_buyboard_engine_enabled", lambda: True)
    import src.services.trading_engine as trading_engine_module

    monkeypatch.setattr(trading_engine_module, "is_buyboard_engine_enabled", lambda: True)

    broker = _FakeBroker()
    worker, engine = _worker(tmp_path, broker=broker)
    worker.runtime = runtime_module.build_buyboard_runtime(
        buying_power_provider=worker._buying_power_provider,
        card_lookup=worker._card_lookup,
        broker=worker._broker,
        market_data=_dummy_market_data(),
    )
    _seed_card(engine, board_status=BoardStatus.WATCHLIST)
    # Simulates a prior startup failure for this account that has since
    # become reachable again.
    worker.startup_reconciliation_errors = {"1": "simulated KIS outage"}
    worker.startup_reconciliation_complete = False
    long_ago = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)
    worker._account_balance_refreshed_at["1"] = long_ago
    worker._account_reconciled_at["1"] = long_ago

    worker._run_one_cycle()

    assert "1" not in worker.startup_reconciliation_errors
    assert worker.startup_reconciliation_complete is True


def test_startup_reconciliation_complete_when_every_account_succeeds(tmp_path):
    worker, engine = _worker(tmp_path, account_no="1")
    worker.runtime = runtime_module.build_buyboard_runtime(
        buying_power_provider=worker._buying_power_provider,
        card_lookup=worker._card_lookup,
        broker=worker._broker,
    )

    worker._run_startup_reconciliation()

    assert worker.startup_reconciliation_complete is True
    assert worker.startup_reconciliation_errors == {}


def test_distinct_account_numbers_discovers_cardless_configured_accounts(tmp_path):
    """Review finding P1: "accounts without existing cards remain
    undiscoverable" -- a configured KIS account with zero TradeCards must
    still be queried by the unscoped production worker."""
    worker, _ = _worker(
        tmp_path, account_no="", account_discovery=lambda: ["9", "1"]
    )
    card = TradeCardState(environment="PROD", account_no="1", symbol="AAPL")

    accounts = worker._distinct_account_numbers([card])

    assert set(accounts) == {"1", "9"}


def test_distinct_account_numbers_does_not_discover_for_a_scoped_worker(tmp_path):
    """A specifically-scoped worker must not reach into every configured
    account -- only its own."""
    discovery_calls = []
    worker, _ = _worker(
        tmp_path, account_no="1",
        account_discovery=lambda: discovery_calls.append(True) or ["9"],
    )

    accounts = worker._distinct_account_numbers([])

    assert accounts == ["1"]
    assert discovery_calls == []


# --- Periodic buying-power refresh / full reconciliation (review findings) --


def test_periodic_refresh_populates_buying_power_cache_on_first_cycle(tmp_path, monkeypatch):
    from src.core import execution_config
    from src.services import buying_power_cache

    monkeypatch.setattr(execution_config, "is_buyboard_engine_enabled", lambda: True)
    import src.services.trading_engine as trading_engine_module

    monkeypatch.setattr(trading_engine_module, "is_buyboard_engine_enabled", lambda: True)

    broker = _FakeBroker()
    broker.positions = {
        "overseas": {
            "holdings": [],
            "summary_by_exchange": {"NASD": {"cash_balance_usd": 5000.0}},
        }
    }
    worker, engine = _worker(tmp_path, broker=broker)
    worker.runtime = runtime_module.build_buyboard_runtime(
        buying_power_provider=worker._buying_power_provider,
        card_lookup=worker._card_lookup,
        broker=worker._broker,
        market_data=_dummy_market_data(),
    )
    _seed_card(engine, board_status=BoardStatus.BUY_TODAY)

    worker._run_one_cycle()

    snapshot = buying_power_cache.get_snapshot("PROD", "1")
    assert snapshot is not None
    assert snapshot.usable_buying_power_usd == 5000.0
    assert "1" in worker._account_balance_refreshed_at


def test_periodic_refresh_does_not_requery_before_its_interval(tmp_path, monkeypatch):
    from src.core import execution_config

    monkeypatch.setattr(execution_config, "is_buyboard_engine_enabled", lambda: True)
    import src.services.trading_engine as trading_engine_module

    monkeypatch.setattr(trading_engine_module, "is_buyboard_engine_enabled", lambda: True)

    broker = _FakeBroker()
    worker, engine = _worker(tmp_path, broker=broker)
    worker.runtime = runtime_module.build_buyboard_runtime(
        buying_power_provider=worker._buying_power_provider,
        card_lookup=worker._card_lookup,
        broker=worker._broker,
        market_data=_dummy_market_data(),
    )
    _seed_card(engine, board_status=BoardStatus.BUY_TODAY)

    worker._run_one_cycle()
    calls_after_first = len(broker.get_positions_calls)
    assert calls_after_first >= 1

    worker._run_one_cycle()  # immediately after -- well within the refresh interval
    assert len(broker.get_positions_calls) == calls_after_first


def test_periodic_refresh_requeries_once_the_interval_has_elapsed(tmp_path, monkeypatch):
    import datetime as dt

    from src.core import execution_config

    monkeypatch.setattr(execution_config, "is_buyboard_engine_enabled", lambda: True)
    import src.services.trading_engine as trading_engine_module

    monkeypatch.setattr(trading_engine_module, "is_buyboard_engine_enabled", lambda: True)

    broker = _FakeBroker()
    worker, engine = _worker(tmp_path, broker=broker)
    worker.runtime = runtime_module.build_buyboard_runtime(
        buying_power_provider=worker._buying_power_provider,
        card_lookup=worker._card_lookup,
        broker=worker._broker,
        market_data=_dummy_market_data(),
    )
    _seed_card(engine, board_status=BoardStatus.BUY_TODAY)

    worker._run_one_cycle()
    calls_after_first = len(broker.get_positions_calls)

    # Force both cadences to look overdue without waiting in real time.
    long_ago = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)
    worker._account_balance_refreshed_at["1"] = long_ago
    worker._account_reconciled_at["1"] = long_ago

    worker._run_one_cycle()
    assert len(broker.get_positions_calls) == calls_after_first + 1


def test_periodic_reconciliation_discovers_external_position_change(tmp_path, monkeypatch):
    """FULL_RECONCILIATION_SECONDS cadence: a manual sale made mid-session
    (broker quantity dropped to zero) must be discovered without waiting
    for another application restart."""
    import datetime as dt

    from src.core import execution_config

    monkeypatch.setattr(execution_config, "is_buyboard_engine_enabled", lambda: True)
    import src.services.trading_engine as trading_engine_module

    monkeypatch.setattr(trading_engine_module, "is_buyboard_engine_enabled", lambda: True)

    broker = _FakeBroker()
    broker.positions = {"overseas": {"holdings": []}}  # broker now reports flat
    worker, engine = _worker(tmp_path, broker=broker)
    worker.runtime = runtime_module.build_buyboard_runtime(
        buying_power_provider=worker._buying_power_provider,
        card_lookup=worker._card_lookup,
        broker=worker._broker,
        market_data=_dummy_market_data(),
    )
    _seed_card(
        engine,
        board_status=BoardStatus.OPEN_POSITION,
        broker_quantity=10,
        orderable_quantity=10,
    )
    worker._account_balance_refreshed_at["1"] = dt.datetime.now(dt.timezone.utc)
    worker._account_reconciled_at["1"] = dt.datetime.now(dt.timezone.utc) - dt.timedelta(
        seconds=execution_config.FULL_RECONCILIATION_SECONDS + 1
    )

    worker._run_one_cycle()

    card = repo.get_trade_card(engine, "PROD", "1", "AAPL")
    assert card.board_status == BoardStatus.CLOSED
    assert card.broker_quantity == 0


def test_periodic_refresh_also_reconciles_unresolved_entry_order_state(tmp_path, monkeypatch):
    """Review finding P1: "full reconciliation still reconciles positions,
    not the full account" -- the periodic pass must also resolve an
    ENTRY_PENDING card whose local order lookup finds nothing (not just
    broker positions), the same way startup reconciliation already does."""
    import datetime as dt

    from src.core import execution_config

    monkeypatch.setattr(execution_config, "is_buyboard_engine_enabled", lambda: True)
    import src.services.trading_engine as trading_engine_module

    monkeypatch.setattr(trading_engine_module, "is_buyboard_engine_enabled", lambda: True)

    broker = _FakeBroker()  # discover_orders defaults to a complete, empty result
    worker, engine = _worker(tmp_path, broker=broker)
    worker.runtime = runtime_module.build_buyboard_runtime(
        buying_power_provider=worker._buying_power_provider,
        card_lookup=worker._card_lookup,
        broker=worker._broker,
        market_data=_dummy_market_data(),
    )
    _seed_card(engine, board_status=BoardStatus.ENTRY_PENDING, broker_quantity=0)
    worker._account_balance_refreshed_at["1"] = dt.datetime.now(dt.timezone.utc)
    worker._account_reconciled_at["1"] = dt.datetime.now(dt.timezone.utc) - dt.timedelta(
        seconds=execution_config.FULL_RECONCILIATION_SECONDS + 1
    )

    worker._run_one_cycle()

    card = repo.get_trade_card(engine, "PROD", "1", "AAPL")
    # No order was ever actually submitted (empty local ledger); a complete
    # broker-wide discovery finding nothing means the entry never went
    # through -- returns to Buylist rather than sitting stuck forever.
    assert card.board_status == BoardStatus.BUYLIST


# --- Stalled-liquidation critical alert (review: "a card warning is --------
# --- insufficient when the user is asleep") ---------------------------------


def test_stalled_cancel_warning_fires_alert_exactly_once(tmp_path):
    worker, _ = _worker(tmp_path)
    alerts = []
    worker.alert.connect(alerts.append)
    card = TradeCardState(
        environment="PROD", account_no="1", symbol="AAPL",
        board_status=BoardStatus.SELL_ALL, warnings=["EXIT_CANCEL_STALLED"],
    )

    worker._emit_stalled_liquidation_alerts([card])
    worker._emit_stalled_liquidation_alerts([card])  # still stalled next tick

    assert len(alerts) == 1
    assert "CRITICAL" in alerts[0]
    assert "AAPL" in alerts[0]


def test_stalled_cancel_alert_fires_again_after_resolving_and_recurring(tmp_path):
    worker, _ = _worker(tmp_path)
    alerts = []
    worker.alert.connect(alerts.append)
    card = TradeCardState(
        environment="PROD", account_no="1", symbol="AAPL",
        board_status=BoardStatus.SELL_ALL, warnings=["EXIT_CANCEL_STALLED"],
    )

    worker._emit_stalled_liquidation_alerts([card])
    card.warnings = []  # resolved
    worker._emit_stalled_liquidation_alerts([card])
    card.warnings = ["EXIT_CANCEL_STALLED"]  # stalls again
    worker._emit_stalled_liquidation_alerts([card])

    assert len(alerts) == 2


def test_no_stalled_warning_does_not_alert(tmp_path):
    worker, _ = _worker(tmp_path)
    alerts = []
    worker.alert.connect(alerts.append)
    card = TradeCardState(
        environment="PROD", account_no="1", symbol="AAPL", board_status=BoardStatus.SELL_ALL,
    )

    worker._emit_stalled_liquidation_alerts([card])
    assert alerts == []


def test_unreconciled_broker_order_warning_fires_alert_exactly_once(tmp_path):
    """Review finding P1: "UNRECONCILED_BROKER_ORDER should be a critical
    notification" -- not merely a card warning."""
    worker, _ = _worker(tmp_path)
    alerts = []
    worker.alert.connect(alerts.append)
    card = TradeCardState(
        environment="PROD", account_no="1", symbol="AAPL",
        board_status=BoardStatus.ENTRY_PENDING, warnings=["UNRECONCILED_BROKER_ORDER"],
    )

    worker._emit_stalled_liquidation_alerts([card])
    worker._emit_stalled_liquidation_alerts([card])  # still present next tick

    assert len(alerts) == 1
    assert "CRITICAL" in alerts[0]
    assert "AAPL" in alerts[0]


def test_exit_cancel_stalled_and_unreconciled_broker_order_alert_independently(tmp_path):
    """Two different critical warnings on two different cards must each
    alert -- one warning's dedup state must not suppress the other."""
    worker, _ = _worker(tmp_path)
    alerts = []
    worker.alert.connect(alerts.append)
    stalled_exit = TradeCardState(
        environment="PROD", account_no="1", symbol="AAPL",
        board_status=BoardStatus.SELL_ALL, warnings=["EXIT_CANCEL_STALLED"],
    )
    unreconciled_order = TradeCardState(
        environment="PROD", account_no="1", symbol="MSFT",
        board_status=BoardStatus.ENTRY_PENDING, warnings=["UNRECONCILED_BROKER_ORDER"],
    )

    worker._emit_stalled_liquidation_alerts([stalled_exit, unreconciled_order])

    assert len(alerts) == 2
    assert any("AAPL" in message for message in alerts)
    assert any("MSFT" in message for message in alerts)


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
