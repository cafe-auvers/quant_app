from types import SimpleNamespace

from src.core.board_workflow import BoardProjectionContext
from src.ui.buyboard import board as buyboard_board
from src.ui.buyboard import controller as buyboard_controller
from src.ui.mixins.dashboard_mixin import DashboardMixin


def test_market_data_status_formatting_never_queries_the_database():
    window = SimpleNamespace(
        db_enabled=True,
        db_engine=object(),
        _cached_market_data_status=None,
    )

    assert DashboardMixin._format_market_data_status(window) == "Checking..."


def test_buyboard_projection_worker_uses_authoritative_services(monkeypatch):
    from src.ui.buyboard.columns import BOARD_COLUMN_ORDER
    from src.services import trade_card_bootstrap

    calls = []
    projections = [object()]
    monkeypatch.setattr(
        trade_card_bootstrap,
        "bootstrap_trade_cards_from_current_state",
        lambda engine, **kwargs: calls.append(("bootstrap", engine, kwargs)),
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
    assert calls[1][2]["board_statuses"] == BOARD_COLUMN_ORDER
    assert completed == [(projections, "", 4)]


class _Signal:
    def __init__(self):
        self.callback = None

    def connect(self, callback):
        self.callback = callback


class _ProjectionWorkerStub:
    instances = []

    def __init__(self, request):
        self.request = request
        self.completed = _Signal()
        self.started = False
        self.__class__.instances.append(self)

    def isRunning(self):
        return self.started

    def start(self):
        self.started = True


class _ProjectionWindow:
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


def test_busy_buyboard_projection_is_coalesced_without_invalidating_its_result():
    window = _ProjectionWindow(object())
    running = _ProjectionWorkerStub(object())
    running.started = True
    window._buyboard_projection_worker = running
    window._buyboard_projection_generation = 7

    buyboard_controller.BuyboardMixin.refresh_buyboard(window)

    assert window._buyboard_projection_generation == 7


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
