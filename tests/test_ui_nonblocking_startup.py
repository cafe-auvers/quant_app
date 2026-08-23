from types import SimpleNamespace
from datetime import datetime, timezone
import threading
import time
from zoneinfo import ZoneInfo

import pandas as pd
from PyQt5.QtCore import QCoreApplication

from src.core import execution_config
from src.core.board_workflow import (
    BoardActionContext,
    BoardCardProjection,
    BoardProjectionContext,
    MoveToBuylist,
)
from src.core.execution_queue import (
    ExecutionQueueItem,
    OrbCandidate,
    OrbCandidateStatus,
)
from src.core.trade_card_state import (
    BoardStatus,
    EntryRuntimeStatus,
    PositionRuntimeStatus,
    TradeCardState,
)
from src.services import trade_card_repository as trade_card_repo
from src.ui.buyboard import board as buyboard_board
from src.ui.buyboard import controller as buyboard_controller
from src.ui.charts.controller_data_flow import ChartsDataFlowMixin
from src.ui.mixins.dashboard_mixin import DashboardMixin
from src.ui.main_window import MainWindow, MarketDataStatusResult


def test_market_data_status_formatting_never_queries_the_database():
    window = SimpleNamespace(
        db_enabled=True,
        db_engine=object(),
        _cached_market_data_status=None,
    )

    assert DashboardMixin._format_market_data_status(window) == "Checking..."


def test_market_data_status_result_controls_1d_and_1h_freshness_independently():
    engine = object()
    polls = []
    window = SimpleNamespace(
        db_engine=engine,
        _database_shutting_down=False,
        _format_market_data_status_from_date=lambda _value: "Up to date",
        _poll_refresh_status=lambda: polls.append(True),
        update_dashboard_summary=lambda: None,
    )
    result = MarketDataStatusResult(
        engine=engine,
        latest_daily=datetime(2026, 8, 21),
        latest_hourly=datetime(2026, 8, 20),
        expected_date=datetime(2026, 8, 21).date(),
        daily_is_stale=False,
        hourly_is_stale=True,
    )

    MainWindow._on_market_data_status_completed(window, result)

    assert window._historical_data_freshness == {"1d": "fresh", "1h": "stale"}
    assert window._historical_data_expected_date.isoformat() == "2026-08-21"
    assert polls == [True]


def test_buyboard_projection_worker_uses_authoritative_services(monkeypatch):
    from src.ui.buyboard.columns import BOARD_COLUMN_ORDER
    from src.services import trade_card_bootstrap

    calls = []
    projections = [object()]
    monkeypatch.setattr(
        trade_card_bootstrap,
        "bootstrap_trade_cards_from_current_state",
        lambda engine, **kwargs: (
            calls.append(("bootstrap", engine, kwargs))
            or SimpleNamespace(canonical_cards=("cached-card",))
        ),
    )
    monkeypatch.setattr(
        buyboard_controller.execution_workflow_service,
        "list_board_projections",
        lambda engine, **kwargs: (
            calls.append(("projection", engine, kwargs)) or projections
        ),
    )
    request = buyboard_controller.BuyboardProjectionRequest(
        engine=object(),
        context=BoardProjectionContext(),
        buylist_manager=object(),
        watchlist=object(),
        default_account_no="account",
        account_snapshots={},
        account_snapshot_fetched_at={},
        runtime_running=True,
        generation=4,
    )
    completed = []
    worker = buyboard_controller.BuyboardProjectionWorker(request)
    worker.completed.connect(
        lambda result, error, generation: completed.append(
            (result, error, generation)
        )
    )

    worker.run()

    assert [call[0] for call in calls] == ["bootstrap", "projection"]
    assert calls[1][2]["board_statuses"] == (
        *BOARD_COLUMN_ORDER,
        BoardStatus.WATCHLIST,
    )
    assert calls[1][2]["prefetched_cards"] == ("cached-card",)
    assert completed == [(projections, "", 4)]


def test_minute_projection_check_skips_bootstrap_and_payload_when_unchanged(
    monkeypatch,
):
    revision = (("cards", 3, 7, "now"),)
    context = BoardProjectionContext(readiness_generation=9)
    expected = (revision, context)
    monkeypatch.setattr(
        buyboard_controller.execution_workflow_service,
        "get_board_projection_revision",
        lambda *_args, **_kwargs: revision,
    )
    monkeypatch.setattr(
        buyboard_controller.execution_workflow_service,
        "list_board_projections",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("unchanged timer check downloaded the projection")
        ),
    )
    from src.services import trade_card_bootstrap

    monkeypatch.setattr(
        trade_card_bootstrap,
        "bootstrap_trade_cards_from_current_state",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("unchanged timer check ran bootstrap")
        ),
    )
    request = buyboard_controller.BuyboardProjectionRequest(
        engine=object(),
        context=context,
        buylist_manager=object(),
        watchlist=object(),
        default_account_no="account",
        account_snapshots={},
        account_snapshot_fetched_at={},
        runtime_running=True,
        generation=5,
        revision_only=True,
        expected_revision=expected,
    )
    completed = []
    worker = buyboard_controller.BuyboardProjectionWorker(request)
    worker.completed.connect(lambda *args: completed.append(args))

    worker.run()

    assert completed == [(None, "", 5)]
    assert worker.resolved_revision == expected
    assert (
        buyboard_controller.BuyboardMixin._BUYBOARD_PROJECTION_REFRESH_MS
        == int(execution_config.COORDINATION_BOARD_PROJECTION_SECONDS * 1000)
    )


def test_changed_minute_projection_uses_one_revision_read_and_no_bootstrap(
    monkeypatch,
):
    old_revision = (("cards", 2, 6, "old"),)
    new_revision = (("cards", 3, 7, "new"),)
    context = BoardProjectionContext(readiness_generation=9)
    revision_calls = []
    monkeypatch.setattr(
        buyboard_controller.execution_workflow_service,
        "get_board_projection_revision",
        lambda *_args, **_kwargs: revision_calls.append(True) or new_revision,
    )
    projections = [object()]
    monkeypatch.setattr(
        buyboard_controller.execution_workflow_service,
        "list_board_projections",
        lambda *_args, **_kwargs: projections,
    )
    from src.services import trade_card_bootstrap

    monkeypatch.setattr(
        trade_card_bootstrap,
        "bootstrap_trade_cards_from_current_state",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("minute canonical refresh ran compatibility bootstrap")
        ),
    )
    request = buyboard_controller.BuyboardProjectionRequest(
        engine=object(),
        context=context,
        buylist_manager=object(),
        watchlist=object(),
        default_account_no="account",
        account_snapshots={},
        account_snapshot_fetched_at={},
        runtime_running=True,
        generation=6,
        revision_only=True,
        expected_revision=(old_revision, context),
    )
    completed = []
    worker = buyboard_controller.BuyboardProjectionWorker(request)
    worker.completed.connect(lambda *args: completed.append(args))

    worker.run()

    assert revision_calls == [True]
    assert completed == [(projections, "", 6)]
    assert worker.resolved_revision == (new_revision, context)


class _Signal:
    def __init__(self):
        self.callback = None

    def connect(self, callback):
        self.callback = callback

    def emit(self, *args):
        if self.callback is not None:
            self.callback(*args)


class _ProjectionWorkerStub:
    instances = []

    def __init__(self, request):
        self.request = request
        self.completed = _Signal()
        self.finished = _Signal()
        self.started = False
        self.__class__.instances.append(self)

    def isRunning(self):
        return self.started

    def start(self):
        self.started = True


class _ProjectionWindow(buyboard_controller.BuyboardMixin):
    def __init__(self, request):
        self.request = request
        self.tracked = []

    def _buyboard_projection_request(self, generation):
        return self.request

    def _track_worker(self, name, worker):
        self.tracked.append((name, worker))

    def _on_buyboard_projection_completed(self, *_args):
        return None


class _InteractiveProjectionWindow(buyboard_controller.BuyboardMixin):
    def __init__(self):
        self._buyboard_projection_generation = 3
        self.refresh_count = 0

    def refresh_buyboard(self):
        self.refresh_count += 1


class _PendingColumn:
    def __init__(self):
        self.states = []

    def set_pending_card_keys(self, keys):
        self.states.append(set(keys))


class _CommandWindow(buyboard_controller.BuyboardMixin):
    def __init__(self):
        self.pc_db_engine = object()
        self.buyboard_columns = {"buylist": _PendingColumn()}
        self.refresh_count = 0
        self.results = []

    def refresh_buyboard(self):
        self.refresh_count += 1

    def _on_buyboard_command_completed(self, result):
        self.results.append(result)
        super()._on_buyboard_command_completed(result)


def test_refresh_buyboard_starts_worker_without_running_db_read_on_gui_thread(
    monkeypatch,
):
    _ProjectionWorkerStub.instances = []
    request = object()
    window = _ProjectionWindow(request)
    populated = []
    monkeypatch.setattr(
        buyboard_controller, "BuyboardProjectionWorker", _ProjectionWorkerStub
    )
    monkeypatch.setattr(
        buyboard_board,
        "populate_buyboard_columns",
        lambda _window, values: populated.append(values),
    )
    monkeypatch.setattr(
        buyboard_controller.execution_workflow_service,
        "list_board_projections",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("projection DB read ran on the GUI thread")
        ),
    )

    buyboard_controller.BuyboardMixin.refresh_buyboard(window)

    worker = _ProjectionWorkerStub.instances[0]
    assert worker.request is request
    assert worker.started is True
    assert window.tracked == [("_buyboard_projection_worker", worker)]
    assert populated == []


def test_database_outage_renders_local_snapshot_read_only_instead_of_emptying_board(
    monkeypatch, tmp_path
):
    snapshot_path = tmp_path / "trade_cards.json"
    monkeypatch.setattr(
        trade_card_repo, "LOCAL_TRADE_CARDS_FILE", snapshot_path
    )
    trade_card_repo.save_local_trade_cards_snapshot(
        [
            TradeCardState(
                environment="PROD",
                account_no="1",
                symbol="MAX",
                board_status=BoardStatus.BUY_TODAY,
                # The local canonical snapshot, not the compatibility queue,
                # owns the published target.  Recovery may enrich that target
                # with current-session ORB geometry but must never invent it.
                breakout_price=13.28,
            )
        ],
        path=snapshot_path,
    )
    candidate = OrbCandidate(
        symbol="MAX",
        window="30m",
        orb_high=13.40,
        orb_low=13.00,
        breakout_price=13.28,
        breakout_trigger=13.29328,
        entry_trigger=13.40,
        current_price=13.46,
        source_session_date=datetime.now(
            ZoneInfo("America/New_York")
        ).date().isoformat(),
        stop_loss=13.00,
        shares=10,
        status=OrbCandidateStatus.WAITING_BREAKOUT,
        valid=True,
    )
    queue_item = ExecutionQueueItem(
        symbol="MAX",
        account_no="1",
        breakout_price=13.28,
        current_price=13.46,
        candidates={"30m": candidate},
        last_updated=datetime.now(timezone.utc),
    )
    window = _ProjectionWindow(None)
    window._buyboard_configured_accounts = (("PROD", "1"),)
    window.execution_queue_manager = SimpleNamespace(
        get_item=lambda symbol, environment: queue_item
    )
    populated = []
    monkeypatch.setattr(
        buyboard_board,
        "populate_buyboard_columns",
        lambda _window, values: populated.append(tuple(values)),
    )

    buyboard_controller.BuyboardMixin.refresh_buyboard(window)

    assert len(populated) == 1
    assert len(populated[0]) == 1
    projection = populated[0][0]
    assert isinstance(projection, BoardCardProjection)
    assert projection.card.symbol == "MAX"
    assert projection.card.breakout_price == 13.28
    assert projection.card.market_data_last_trusted_price == 13.46
    assert projection.card.entry_runtime_status == EntryRuntimeStatus.WAITING_BREAKOUT
    assert projection.reconciliation_blocked is True
    assert "last local snapshot" in projection.engine_restrictions[0]
    assert window._buyboard_recovery_snapshot_active is True


def test_database_outage_retains_last_in_memory_board_before_using_disk_snapshot(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(
        trade_card_repo,
        "LOCAL_TRADE_CARDS_FILE",
        tmp_path / "missing-trade-cards.json",
    )
    card = TradeCardState(
        environment="PROD",
        account_no="1",
        symbol="WEX",
        board_status=BoardStatus.BUY_TODAY,
        breakout_price=191.39,
    )
    window = _ProjectionWindow(None)
    window._buyboard_configured_accounts = (("PROD", "1"),)
    window._buyboard_current_projections = (BoardCardProjection(card=card),)
    populated = []
    monkeypatch.setattr(
        buyboard_board,
        "populate_buyboard_columns",
        lambda _window, values: populated.append(tuple(values)),
    )

    buyboard_controller.BuyboardMixin.refresh_buyboard(window)

    assert populated[0][0].card.symbol == "WEX"
    assert populated[0][0].card.breakout_price == 191.39
    assert populated[0][0].reconciliation_blocked is True


def test_buy_today_orb_symbols_fall_back_to_local_snapshot(monkeypatch, tmp_path):
    snapshot_path = tmp_path / "trade_cards.json"
    monkeypatch.setattr(
        trade_card_repo, "LOCAL_TRADE_CARDS_FILE", snapshot_path
    )
    trade_card_repo.save_local_trade_cards_snapshot(
        [
            TradeCardState(
                environment="PROD",
                account_no="1",
                symbol="WEX",
                board_status=BoardStatus.BUY_TODAY,
            ),
            TradeCardState(
                environment="PROD",
                account_no="1",
                symbol="MAX",
                board_status=BoardStatus.BUYLIST,
            ),
        ],
        path=snapshot_path,
    )
    window = buyboard_controller.BuyboardMixin()
    window._buyboard_current_projections = ()
    window._buyboard_configured_accounts = (("PROD", "1"),)

    assert window._buy_today_orb_symbols() == ["WEX"]


def test_recovery_projection_uses_fresh_kis_snapshot_to_close_stale_position():
    card = TradeCardState(
        environment="PROD",
        account_no="1",
        symbol="STIM",
        board_status=BoardStatus.OPEN_POSITION,
        broker_quantity=401,
        orderable_quantity=401,
        position_runtime_status=PositionRuntimeStatus.OPEN,
    )
    window = buyboard_controller.BuyboardMixin()
    window._buyboard_recovery_source_cards = (card,)
    window._buyboard_configured_accounts = (("PROD", "1"),)
    window.kis_account_snapshots = {
        ("PROD", "1"): {
            "domestic": {"holdings": []},
            "overseas": {"holdings": []},
        }
    }
    window.kis_account_snapshot_fetched_at = {
        ("PROD", "1"): datetime.now(timezone.utc)
    }

    projection = window._buyboard_recovery_projections()[0]

    assert projection.card.board_status == BoardStatus.CLOSED
    assert projection.card.broker_quantity == 0
    assert projection.card.position_runtime_status == PositionRuntimeStatus.CLOSED


def test_recovery_projection_promotes_buy_today_when_kis_confirms_holding():
    card = TradeCardState(
        environment="PROD",
        account_no="1",
        symbol="WEX",
        board_status=BoardStatus.BUY_TODAY,
    )
    window = buyboard_controller.BuyboardMixin()
    window._buyboard_recovery_source_cards = (card,)
    window._buyboard_configured_accounts = (("PROD", "1"),)
    window.kis_account_snapshots = {
        ("PROD", "1"): {
            "overseas": {
                "holdings": [
                    {
                        "symbol": "WEX",
                        "quantity": 7,
                        "orderable_quantity": 6,
                        "average_price": 193.25,
                    }
                ]
            }
        }
    }
    window.kis_account_snapshot_fetched_at = {
        ("PROD", "1"): datetime.now(timezone.utc)
    }

    projection = window._buyboard_recovery_projections()[0]

    assert projection.card.board_status == BoardStatus.OPEN_POSITION
    assert projection.card.broker_quantity == 7
    assert projection.card.orderable_quantity == 6
    assert projection.card.average_entry_price == 193.25


def test_busy_buyboard_projection_is_coalesced_without_invalidating_its_result():
    window = _ProjectionWindow(object())
    running = _ProjectionWorkerStub(object())
    running.started = True
    window._buyboard_projection_worker = running
    window._buyboard_projection_generation = 7

    buyboard_controller.BuyboardMixin.refresh_buyboard(window)

    assert window._buyboard_projection_generation == 7
    assert window._buyboard_refresh_pending is True


def test_twenty_busy_refreshes_start_exactly_one_trailing_projection(monkeypatch):
    _ProjectionWorkerStub.instances = []
    window = _ProjectionWindow(object())
    monkeypatch.setattr(
        buyboard_controller, "BuyboardProjectionWorker", _ProjectionWorkerStub
    )

    window.refresh_buyboard()
    first = _ProjectionWorkerStub.instances[0]
    for _ in range(20):
        window.refresh_buyboard()

    assert len(_ProjectionWorkerStub.instances) == 1
    first.started = False
    first.finished.emit()

    assert len(_ProjectionWorkerStub.instances) == 2
    assert _ProjectionWorkerStub.instances[1].started is True
    assert not window.__dict__.get("_buyboard_refresh_pending", False)


def test_board_command_with_500ms_database_latency_does_not_block_dispatch(
    monkeypatch,
):
    app = QCoreApplication.instance() or QCoreApplication([])
    window = _CommandWindow()
    monkeypatch.setattr(
        buyboard_controller,
        "_action_context",
        lambda *_args: BoardActionContext(),
    )

    def slow_request(*_args, **_kwargs):
        time.sleep(0.5)

    monkeypatch.setattr(
        buyboard_controller.execution_workflow_service,
        "request_board_action",
        slow_request,
    )
    command = MoveToBuylist(
        environment="PROD",
        account_no="1",
        symbol="AAPL",
        expected_card_version=1,
    )

    started_at = time.perf_counter()
    assert window._buyboard_dispatch_command(command) is True
    dispatch_ms = (time.perf_counter() - started_at) * 1000.0

    assert dispatch_ms < 50.0
    assert window.buyboard_columns["buylist"].states[-1] == {"PROD:1:AAPL"}

    deadline = time.monotonic() + 2.0
    while not window.results and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)
    app.processEvents()
    worker = window._buyboard_command_worker
    worker.request_stop()
    assert worker.wait(1000)

    assert len(window.results) == 1
    assert window.results[0].succeeded is True
    assert window.results[0].elapsed_ms >= 450.0
    assert window.refresh_count == 1
    assert window.buyboard_columns["buylist"].states[-1] == set()


def test_board_commands_share_one_serial_worker(monkeypatch):
    app = QCoreApplication.instance() or QCoreApplication([])
    window = _CommandWindow()
    first_started = threading.Event()
    release_first = threading.Event()
    calls = []
    monkeypatch.setattr(
        buyboard_controller,
        "_action_context",
        lambda *_args: BoardActionContext(),
    )

    def request(_engine, command, **_kwargs):
        calls.append(command.symbol)
        if command.symbol == "AAPL":
            first_started.set()
            assert release_first.wait(1.0)

    monkeypatch.setattr(
        buyboard_controller.execution_workflow_service,
        "request_board_action",
        request,
    )
    first = MoveToBuylist(
        environment="PROD",
        account_no="1",
        symbol="AAPL",
        expected_card_version=1,
    )
    second = MoveToBuylist(
        environment="PROD",
        account_no="1",
        symbol="MSFT",
        expected_card_version=1,
    )

    window._buyboard_dispatch_command(first)
    worker = window._buyboard_command_worker
    assert first_started.wait(1.0)
    window._buyboard_dispatch_command(second)

    assert window._buyboard_command_worker is worker
    assert calls == ["AAPL"]
    release_first.set()
    deadline = time.monotonic() + 2.0
    while len(window.results) < 2 and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)
    app.processEvents()
    worker.request_stop()
    assert worker.wait(1000)

    assert calls == ["AAPL", "MSFT"]
    assert len(window.results) == 2


def test_projection_rebuild_is_deferred_until_user_interaction_finishes(
    monkeypatch,
):
    window = _InteractiveProjectionWindow()
    populated = []
    monkeypatch.setattr(
        buyboard_board,
        "populate_buyboard_columns",
        lambda _window, values: populated.append(values),
    )

    window._set_buyboard_interaction_active(True)
    window._on_buyboard_projection_completed(["fresh"], "", 3)
    assert populated == []

    window._set_buyboard_interaction_active(False)
    assert populated == [["fresh"]]


def test_action_refresh_discards_projection_captured_before_interaction(
    monkeypatch,
):
    window = _InteractiveProjectionWindow()
    populated = []
    monkeypatch.setattr(
        buyboard_board,
        "populate_buyboard_columns",
        lambda _window, values: populated.append(values),
    )

    window._set_buyboard_interaction_active(True)
    window._on_buyboard_projection_completed(["pre-action"], "", 3)
    buyboard_controller.BuyboardMixin.refresh_buyboard(window)
    window._set_buyboard_interaction_active(False)

    assert populated == []
    assert window.refresh_count == 1


def test_buyboard_live_metric_refresh_only_repaints_existing_widgets(monkeypatch):
    calls = []

    class _Column:
        def refresh_live_metrics(self, quote_lookup, equity_lookup):
            calls.append((quote_lookup("AAPL"), equity_lookup("PROD", "1")))
            return 1

    window = SimpleNamespace(buyboard_columns={"open": _Column()})
    monkeypatch.setattr(
        buyboard_board,
        "_quote_lookup_for",
        lambda _window: lambda _symbol: 123.45,
    )
    monkeypatch.setattr(
        buyboard_board,
        "_account_equity_lookup_for",
        lambda _window: lambda _environment, _account: 100_000.0,
    )

    assert buyboard_board.refresh_buyboard_live_metrics(window) == 1
    assert calls == [(123.45, 100_000.0)]
    assert buyboard_controller.BuyboardMixin._BUYBOARD_LIVE_METRIC_REFRESH_MS == 750


def test_buyboard_live_metric_refresh_pauses_during_drag(monkeypatch):
    calls = []
    window = SimpleNamespace(_buyboard_interaction_depth=1)
    monkeypatch.setattr(
        buyboard_board,
        "refresh_buyboard_live_metrics",
        lambda _window: calls.append("refresh"),
    )

    buyboard_controller.BuyboardMixin._refresh_buyboard_live_metrics(window)
    assert calls == []

    window._buyboard_interaction_depth = 0
    buyboard_controller.BuyboardMixin._refresh_buyboard_live_metrics(window)
    assert calls == ["refresh"]


def test_buyboard_orb_refresh_targets_only_buy_today_and_is_independent_of_readiness(
    monkeypatch,
):
    monkeypatch.setattr(
        buyboard_controller, "is_buyboard_engine_enabled", lambda: True
    )
    monkeypatch.setattr(
        buyboard_controller, "is_regular_session_open", lambda: True
    )
    requests = []

    class _Window:
        _BUYBOARD_ORB_DATA_REFRESH_MS = 60_000
        _buyboard_projection_values = (
            buyboard_controller.BuyboardMixin._buyboard_projection_values
        )
        _buy_today_orb_symbols = (
            buyboard_controller.BuyboardMixin._buy_today_orb_symbols
        )
        _buyboard_monitored_symbols = (
            buyboard_controller.BuyboardMixin._buyboard_monitored_symbols
        )

        def __init__(self):
            self._buyboard_current_projections = (
                TradeCardState(
                    environment="PROD",
                    account_no="1",
                    symbol="WEX",
                    board_status=BoardStatus.BUY_TODAY,
                ),
                TradeCardState(
                    environment="PROD",
                    account_no="1",
                    symbol="MAX",
                    board_status=BoardStatus.BUYLIST,
                ),
                TradeCardState(
                    environment="PROD",
                    account_no="1",
                    symbol="STIM",
                    board_status=BoardStatus.OPEN_POSITION,
                ),
            )

        def refresh_watchlist_intraday_cache(self, **kwargs):
            requests.append(kwargs)

    window = _Window()
    buyboard_controller.BuyboardMixin._refresh_buyboard_orb_data(window)

    assert requests == [
        {
            "show_messages": False,
            "triggered_by_live": True,
            "source": "Buy Today ORB",
                "symbols": ["WEX"],
            "purpose": "buyboard_orb",
        }
    ]


def test_buyboard_orb_completion_refreshes_only_successful_kis_symbols():
    queue_refreshes = []

    class _Control:
        def setEnabled(self, _enabled):
            pass

        def setText(self, _text):
            pass

    window = SimpleNamespace(
        intraday_bulk_purpose="buyboard_orb",
        _buyboard_orb_refresh_symbols=("WEX", "MAX"),
        refresh_intraday_button=_Control(),
        progress_label=_Control(),
        append_log=lambda _message: None,
        latest_intraday_prices={},
        _load_cached_intraday_interval=lambda symbol, interval, window_days: (
            pd.DataFrame(
                {"Close": [195.2]},
                index=pd.to_datetime(["2026-08-19T19:59:00Z"]),
            )
            if symbol == "WEX"
            else pd.DataFrame()
        ),
        _latest_intraday_session=lambda frame: frame,
        refresh_execution_queue=lambda env, **kwargs: queue_refreshes.append(
            (env, kwargs)
        ),
    )

    ChartsDataFlowMixin._on_intraday_bulk_finished(
        window,
        updated=["WEX"],
        failed=["MAX: KIS data unavailable"],
    )

    assert queue_refreshes == [
        (
            "PROD",
            {
                "show_log": False,
                "symbols": ["WEX"],
                "create_missing": False,
            },
        )
    ]
    assert window.intraday_bulk_purpose == "watchlist"
    assert window.latest_intraday_prices == {"WEX": 195.2}
