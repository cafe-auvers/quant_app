from src.core.board_workflow import BoardCardProjection, MoveToWatchlist
from src.core.trade_card_state import BoardStatus, TradeCardState
from src.core.watchlist import BuylistManager, Watchlist
from src.ui.mixins.watchlist_actions_mixin import WatchlistActionsMixin
from src.ui.planning_membership_worker import (
    PlanningMembershipOutcome,
    PlanningMembershipRequest,
    PlanningMembershipWorker,
)


class _BaseActions:
    def _on_buyboard_projection_completed(self, projections, error, generation):
        self.base_projection_call = (projections, error, generation)

    def _chart_queue_toggle(self, symbol):
        self.base_queue_symbol = symbol

    def _apply_chart_queue_btn_state(self, symbol, button):
        self.base_button_symbol = symbol

    def set_chart_target_price(self, symbol, breakout_price):
        self.base_target = (symbol, breakout_price)


class _Window(WatchlistActionsMixin, _BaseActions):
    pass


class _Button:
    def __init__(self):
        self.text = ""
        self.enabled = None
        self.style = ""

    def setText(self, value):
        self.text = value

    def setEnabled(self, value):
        self.enabled = bool(value)

    def setStyleSheet(self, value):
        self.style = value


def _card(status, *, target=25.0):
    return TradeCardState(
        environment="PROD",
        account_no="12345678",
        symbol="WEX",
        name="WEX Inc.",
        board_status=status,
        watchlist_member=status == BoardStatus.WATCHLIST,
        buylist_member=status == BoardStatus.BUYLIST,
        breakout_price=target,
    )


def test_add_watchlist_candidate_routes_through_membership_worker():
    window = _Window()
    window.watchlist = Watchlist()
    calls = []
    window._start_planning_membership_change = (
        lambda operation, symbol, **kwargs: calls.append((operation, symbol, kwargs))
        or True
    )

    assert window._add_watchlist_candidate(
        "wex", name="WEX Inc.", entry_price=19.5, source="Scanner"
    )
    assert window.watchlist.get("WEX") is None
    assert calls == [
        (
            "add",
            "WEX",
            {"name": "WEX Inc.", "entry_price": 19.5, "source": "Scanner"},
        )
    ]


def test_tradingview_w_toggles_existing_watchlist_without_confirmation():
    window = _Window()
    window.watchlist = Watchlist()
    window.watchlist.add("WEX", "WEX Inc.")
    window._chart_buyboard_projection = lambda _symbol: BoardCardProjection(
        card=_card(BoardStatus.WATCHLIST)
    )
    window.tradingview_symbol_combo = type(
        "Combo", (), {"currentText": lambda self: "WEX"}
    )()
    removals = []
    window._remove_watchlist_candidate = (
        lambda symbol, *, confirm=True: removals.append((symbol, confirm)) or True
    )

    window.add_current_tradingview_symbol_to_watchlist()

    assert removals == [("WEX", False)]


def test_tradingview_w_adds_watchlist_membership_to_buylist_symbol():
    window = _Window()
    window.watchlist = Watchlist()
    buylist_card = _card(BoardStatus.BUYLIST)
    buylist_card.watchlist_member = False
    window._chart_buyboard_projection = lambda _symbol: BoardCardProjection(
        card=buylist_card
    )
    window.tradingview_symbol_combo = type(
        "Combo", (), {"currentText": lambda self: "WEX"}
    )()
    window._get_sidebar_selected_data = lambda: {}
    additions = []
    window._add_watchlist_candidate = (
        lambda symbol, **kwargs: additions.append((symbol, kwargs)) or True
    )

    window.add_current_tradingview_symbol_to_watchlist()

    assert additions == [
        (
            "WEX",
            {"name": "WEX", "entry_price": None, "source": "TradingView"},
        )
    ]


def test_tradingview_watchlist_button_toggles_for_buylist_symbol():
    window = _Window()
    window.watchlist = Watchlist()
    window.tradingview_add_watchlist_button = _Button()
    window.tradingview_symbol_combo = type(
        "Combo", (), {"currentText": lambda self: "WEX"}
    )()
    card = _card(BoardStatus.BUYLIST)
    card.watchlist_member = True
    window._chart_buyboard_projection = lambda _symbol: BoardCardProjection(card=card)

    window._update_tradingview_watchlist_btn()

    assert window.tradingview_add_watchlist_button.text == "Remove from Watchlist (W)"
    assert window.tradingview_add_watchlist_button.enabled is True

    card.watchlist_member = False
    window._update_tradingview_watchlist_btn()

    assert window.tradingview_add_watchlist_button.text == "Add to Watchlist (W)"
    assert window.tradingview_add_watchlist_button.enabled is True


def test_passive_add_without_selected_account_fails_before_worker(
    monkeypatch,
):
    class _Signal:
        def connect(self, callback):
            self.callback = callback

    class _Worker:
        def __init__(self, request):
            self.request = request
            self.completed = _Signal()
            self.started = False

        def start(self):
            self.started = True

    monkeypatch.setattr(
        "src.ui.mixins.watchlist_actions_mixin.PlanningMembershipWorker", _Worker
    )
    warnings = []
    monkeypatch.setattr(
        "src.ui.mixins.watchlist_actions_mixin.QMessageBox.warning",
        lambda *_args: warnings.append(_args[-1]),
    )
    window = _Window()
    window.watchlist = Watchlist()
    window.buylist_manager = BuylistManager()
    window._buyboard_engine = lambda: object()
    window._selected_dashboard_kis_profile = lambda: {}
    window._update_sidebar_watchlist_actions = lambda: None

    assert not window._add_watchlist_candidate("WEX", name="WEX Inc.")
    assert not hasattr(window, "_planning_membership_worker")
    assert warnings and "not changed" in warnings[-1]


def test_chart_queue_moves_buylist_back_to_watchlist_command():
    window = _Window()
    projection = BoardCardProjection(card=_card(BoardStatus.BUYLIST))
    window.watchlist = Watchlist()
    window._chart_buyboard_projection = lambda _symbol: projection
    captured = []
    window._dispatch_chart_command = (
        lambda command, **kwargs: captured.append((command, kwargs)) or True
    )
    window.append_log = lambda _message: None

    window._chart_queue_toggle("wex")

    assert len(captured) == 1
    assert isinstance(captured[0][0], MoveToWatchlist)
    assert captured[0][0].expected_card_version == projection.card.version


def test_chart_queue_promotes_watchlist_only_after_breakout_exists():
    window = _Window()
    projection = BoardCardProjection(card=_card(BoardStatus.WATCHLIST, target=25.0))
    window.watchlist = Watchlist()
    window.watchlist.add("WEX", "WEX Inc.").breakout_price = 25.0
    window._chart_buyboard_projection = lambda _symbol: projection
    window._chart_positive_price = lambda value: float(value) if value else None
    promoted = []
    window._promote_watchlist_candidate = lambda symbol: promoted.append(symbol)

    window._chart_queue_toggle("wex")

    assert promoted == ["WEX"]


def test_projection_sync_keeps_watchlist_hidden_but_refreshes_legacy_source():
    window = _Window()
    window.watchlist = Watchlist()
    window.buylist_manager = BuylistManager()
    window._selected_dashboard_kis_profile = lambda: {
        "environment": "PROD",
        "account_no": "12345678",
    }
    saves = []
    sidebar_refreshes = []
    window._save_state = lambda: saves.append(True)
    window.refresh_sidebar_sources = lambda selected_source=None: sidebar_refreshes.append(
        selected_source
    )
    window.update_dashboard_summary = lambda: None
    window._buyboard_projection_generation = 7

    projection = BoardCardProjection(card=_card(BoardStatus.WATCHLIST))
    window._on_buyboard_projection_completed([projection], "", 7)

    assert window.base_projection_call == ([projection], "", 7)
    assert window.watchlist.get("WEX") is not None
    assert window.buylist_manager.get("WEX", "PROD") is None
    assert saves == [True]
    assert len(sidebar_refreshes) == 1


def test_watchlist_surface_refresh_updates_retained_chart_symbol_navigation():
    window = _Window()
    window.refresh_sidebar_sources = lambda **_kwargs: None
    window.update_dashboard_summary = lambda: None
    calls = []
    window.populate_tradingview_watchlist_symbols = lambda: calls.append(True)

    window._update_watchlist_action_surfaces()

    assert calls == [True]


def test_stale_projection_cannot_rewrite_planning_mirrors():
    window = _Window()
    window.watchlist = Watchlist()
    window.buylist_manager = BuylistManager()
    window._buyboard_projection_generation = 8
    window._selected_dashboard_kis_profile = lambda: {
        "environment": "PROD",
        "account_no": "12345678",
    }
    window._save_state = lambda: (_ for _ in ()).throw(
        AssertionError("stale projection must not save")
    )

    window._on_buyboard_projection_completed(
        [BoardCardProjection(card=_card(BoardStatus.WATCHLIST))], "", 7
    )

    assert window.watchlist.get("WEX") is None
    assert window.buylist_manager.get("WEX", "PROD") is None


def test_projection_does_not_sync_during_active_board_interaction():
    window = _Window()
    window.watchlist = Watchlist()
    window.buylist_manager = BuylistManager()
    window._buyboard_projection_generation = 7
    window._buyboard_interaction_depth = 1
    window._selected_dashboard_kis_profile = lambda: {
        "environment": "PROD",
        "account_no": "12345678",
    }

    window._on_buyboard_projection_completed(
        [BoardCardProjection(card=_card(BoardStatus.WATCHLIST))], "", 7
    )

    assert window.watchlist.get("WEX") is None


def test_chart_queue_button_labels_each_passive_stage_explicitly():
    window = _Window()
    window.watchlist = Watchlist()
    window._planning_membership_pending = False
    window._chart_positive_price = lambda value: float(value) if value else None
    button = _Button()

    window._chart_buyboard_projection = lambda _symbol: BoardCardProjection(
        card=_card(BoardStatus.WATCHLIST)
    )
    window._apply_chart_queue_btn_state("WEX", button)
    assert button.text == "Add to Buylist (Q)"
    assert button.enabled

    window._chart_buyboard_projection = lambda _symbol: BoardCardProjection(
        card=_card(BoardStatus.BUYLIST)
    )
    window._apply_chart_queue_btn_state("WEX", button)
    assert button.text == "Move to Watchlist (Q)"
    assert button.enabled


def test_archived_watchlist_tombstone_requires_explicit_readd(monkeypatch):
    window = _Window()
    window.watchlist = Watchlist()
    # A stale local mirror must not override the newer canonical removal.
    window.watchlist.add("WEX", "Stale local membership")
    archived = _card(BoardStatus.WATCHLIST, target=None)
    archived.watchlist_member = False
    projection = BoardCardProjection(card=archived)
    window._chart_buyboard_projection = lambda _symbol: projection
    window._chart_positive_price = lambda value: float(value) if value else None
    messages = []
    monkeypatch.setattr(
        "src.ui.mixins.watchlist_actions_mixin.QMessageBox.information",
        lambda *_args: messages.append(_args[-1]),
    )

    window.set_chart_target_price("WEX", 25.0)
    window._chart_queue_toggle("WEX")
    button = _Button()
    window._apply_chart_queue_btn_state("WEX", button)

    assert not hasattr(window, "base_target")
    assert not hasattr(window, "base_queue_symbol")
    assert len(messages) == 2
    assert button.text == "Add to Watchlist First"
    assert button.enabled is False


def test_add_action_reconciles_stale_local_item_with_canonical_tombstone():
    window = _Window()
    window.watchlist = Watchlist()
    window.watchlist.add("WEX", "Stale local membership")
    archived = _card(BoardStatus.WATCHLIST, target=None)
    archived.watchlist_member = False
    window._chart_buyboard_projection = lambda _symbol: BoardCardProjection(
        card=archived
    )
    calls = []
    window._start_planning_membership_change = (
        lambda operation, symbol, **kwargs: calls.append((operation, symbol)) or True
    )

    assert window._add_watchlist_candidate("WEX", name="WEX Inc.")
    assert calls == [("add", "WEX")]


def test_sidebar_add_is_disabled_for_existing_buylist_source():
    window = _Window()
    window.watchlist = Watchlist()
    window.sidebar_add_watchlist_button = _Button()
    window.sidebar_move_buylist_button = _Button()
    window.sidebar_remove_watchlist_button = _Button()
    window._planning_membership_pending = False
    window._get_sidebar_selected_data = lambda: {
        "symbol": "WEX",
        "source": "buylist",
    }

    # The method is owned by SidebarMixin in the real MainWindow; call it
    # unbound here to keep this regression independent of QWidget setup.
    from src.ui.mixins.sidebar_mixin import SidebarMixin

    SidebarMixin._update_sidebar_watchlist_actions(window)

    assert window.sidebar_add_watchlist_button.enabled is False
    assert window.sidebar_move_buylist_button.enabled is False
    assert window.sidebar_remove_watchlist_button.enabled is False


def test_membership_worker_rejects_offline_promotion_without_changing_mirrors():
    watchlist = Watchlist()
    watchlist.add("WEX", "WEX Inc.").breakout_price = 25.0
    buylist = BuylistManager()
    request = PlanningMembershipRequest(
        operation="promote",
        symbol="WEX",
        watchlist=watchlist,
        buylist_manager=buylist,
        engine=None,
    )
    worker = PlanningMembershipWorker(request)
    outcomes = []
    worker.completed.connect(outcomes.append)

    worker.run()

    assert len(outcomes) == 1
    assert "Shared planning storage" in outcomes[0].error
    assert outcomes[0].result is None
    assert watchlist.get("WEX") is not None
    assert buylist.get("WEX", "PROD") is None


def test_worker_completion_preserves_unrelated_state_added_during_sql_wait():
    request_watchlist = Watchlist()
    request_watchlist.add("WEX", "WEX Inc.").breakout_price = 25.0
    request_buylist = BuylistManager()
    request = PlanningMembershipRequest(
        operation="promote",
        symbol="WEX",
        watchlist=request_watchlist,
        buylist_manager=request_buylist,
        engine=object(),
        default_account_no="12345678",
    )
    from src.core.watchlist import BuylistItem

    completed_item = BuylistItem(
        symbol="WEX",
        name="WEX Inc.",
        entry_price=0.0,
        target_price=0.0,
        stop_loss=0.0,
        total_score=0.0,
        status="WATCHING",
        technical_score=0.0,
        setup_score=0.0,
        risk_score=0.0,
        news_score=0.0,
        timing_score=0.0,
        rr=0.0,
        stop_adr=0.0,
        position_percent=0.0,
        ai_summary="",
        warnings=[],
        monitoring_status="WATCHING",
        kis_account_no="12345678",
        environment="PROD",
        breakout_price=25.0,
    )
    from src.services.planning_membership_service import PlanningMembershipResult

    completed_result = PlanningMembershipResult(
        action="promoted_to_buylist",
        symbol="WEX",
        buylist_item=completed_item,
        changed=True,
    )

    window = _Window()
    window.watchlist = Watchlist()
    window.watchlist.add("WEX", "WEX Inc.").breakout_price = 25.0
    window.watchlist.add("NEW", "Arrived During Worker")
    window.buylist_manager = BuylistManager()
    window._planning_membership_pending = True
    window._save_state = lambda: None
    window.refresh_sidebar_sources = lambda **_kwargs: None
    window.refresh_buyboard = lambda: None
    window.append_log = lambda _message: None
    window._update_sidebar_watchlist_actions = lambda: None

    window._on_planning_membership_completed(
        PlanningMembershipOutcome(
            request=request,
            result=completed_result,
        )
    )

    assert window.watchlist.get("NEW") is not None
    assert window.watchlist.get("WEX") is not None
    assert window.buylist_manager.get("WEX", "PROD") is not None
