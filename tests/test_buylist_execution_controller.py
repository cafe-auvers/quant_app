from types import SimpleNamespace

import src.ui.controllers.buylist_execution_controller as controller_module
from src.ui.controllers.buylist_execution_controller import (
    BuylistExecutionController,
    ExecutionQueueRefreshRequest,
)


def _target(symbol="AAPL"):
    return SimpleNamespace(
        symbol=symbol,
        name=symbol,
        breakout_price=100.0,
        stop_loss=98.0,
        notes="",
    )


def _candidate():
    return SimpleNamespace(
        window="1m",
        entry_trigger=100.1,
        orb_high=100.0,
        stop_loss=98.0,
        shares=10,
        capital_percent=10.0,
        stop_adr=40.0,
        risk_percent=0.01,
        warnings=[],
        reason="Ready",
        valid=True,
        score=90.0,
    )


def _queue_item(symbol="AAPL", status="EXECUTE_READY"):
    candidate = _candidate()
    return SimpleNamespace(
        symbol=symbol,
        status=status,
        selected_candidate=candidate,
        selected_window=candidate.window,
        warnings=[],
        candidates={"1m": candidate},
    )


class FakeQueueManager:
    def __init__(self, *, status="EXECUTE_READY", pending=False):
        self.status = status
        self.pending = pending
        self.duplicate_pending_order = None
        self.pending_environment = None
        self.build_environment = None
        self.build_calls = 0

    def has_pending_or_submitted_order(self, symbol, environment="SIM"):
        self.pending_environment = environment
        return self.pending

    def build_or_update_from_watchlist_item(self, item, intraday_by_window, **kwargs):
        self.build_calls += 1
        self.duplicate_pending_order = kwargs["duplicate_pending_order"]
        self.build_environment = kwargs["environment"]
        return _queue_item(item.symbol, self.status)


class FakeBuylistManager:
    def __init__(self):
        self.items = {}

    def get(self, symbol, env):
        return self.items.get((symbol, env))

    def add(self, item):
        self.items[(item.symbol, item.environment)] = item


def _existing_buylist_item(**overrides):
    data = {
        "symbol": "AAPL",
        "name": "Apple",
        "entry_price": 1.23,
        "stop_loss": 0.45,
        "total_score": 1.0,
        "status": "WATCHING",
        "stop_adr": 2.0,
        "position_percent": 3.0,
        "ai_summary": "old",
        "warnings": ["old warning"],
        "notes": "old note",
        "risk_percent": 4.0,
        "trade_plan": "old plan",
        "monitoring_status": "WATCHING",
        "environment": "SIM",
        "breakout_price": 99.0,
        "breakout_method": "execution_queue:5m",
        "buffer_pct": 0.002,
        "shares_held": 0,
        "avg_cost": 0.0,
        "buy_date": None,
        "sell_half_done": False,
    }
    data.update(overrides)
    return SimpleNamespace(**data)


def _request(**overrides):
    data = {
        "env": "SIM",
        "manager": FakeQueueManager(),
        "buylist_manager": FakeBuylistManager(),
        "target_items": [_target()],
        "missing_symbols": [],
        "requested_symbols": ["AAPL"],
        "account_size": 100000.0,
        "risk_percent": 0.01,
        "buffer_pct": 0.001,
        "account_no": "12345678",
        "latest_intraday_session": lambda frame: frame,
        "load_intraday_interval": lambda symbol, interval, window_days: [],
        "signal_price_for_symbol": lambda symbol: 101.0,
        "set_latest_intraday_price": lambda symbol, price: None,
        "has_duplicate_open_order": lambda *args: False,
        "adr_percent_for_symbol": lambda symbol: 5.0,
    }
    data.update(overrides)
    return ExecutionQueueRefreshRequest(**data)


def test_empty_target_items_returns_zero_and_preserves_missing_symbols():
    controller = BuylistExecutionController(SimpleNamespace())
    result = controller.refresh_execution_queue(
        _request(target_items=[], manager=None, missing_symbols=["ZZZ"], requested_symbols=["ZZZ"])
    )

    assert result.refreshed == 0
    assert result.target_count == 0
    assert result.missing_symbols == ["ZZZ"]
    assert result.failures == []


def test_missing_manager_with_targets_returns_failure():
    controller = BuylistExecutionController(SimpleNamespace())
    result = controller.refresh_execution_queue(_request(manager=None))

    assert result.refreshed == 0
    assert result.target_count == 1
    assert result.failures == ["Execution queue manager is unavailable."]


def test_successful_fake_queue_refresh_increments_refreshed_and_status_counts():
    controller = BuylistExecutionController(SimpleNamespace())
    result = controller.refresh_execution_queue(_request())

    assert result.refreshed == 1
    assert result.status_counts == {"EXECUTE_READY": 1}
    assert result.failures == []


def test_duplicate_pending_order_is_passed_to_queue_builder():
    controller = BuylistExecutionController(SimpleNamespace())
    manager = FakeQueueManager(pending=True)

    result = controller.refresh_execution_queue(_request(manager=manager))

    assert result.refreshed == 1
    assert manager.build_calls == 1
    assert manager.duplicate_pending_order is True
    assert manager.pending_environment == "SIM"
    assert manager.build_environment == "SIM"


def test_callback_failure_is_captured_and_refresh_continues():
    controller = BuylistExecutionController(SimpleNamespace())

    def fail_load(symbol, interval, window_days):
        raise RuntimeError("cache unavailable")

    result = controller.refresh_execution_queue(
        _request(
            target_items=[_target("AAPL"), _target("MSFT")],
            load_intraday_interval=fail_load,
        )
    )

    assert result.refreshed == 0
    assert result.status_counts == {}
    assert result.failures == [
        "AAPL: cache unavailable",
        "MSFT: cache unavailable",
    ]


def test_apply_queue_item_preserves_existing_volatile_compatibility_mirrors():
    controller = BuylistExecutionController(SimpleNamespace())
    manager = FakeBuylistManager()
    existing = _existing_buylist_item()
    manager.items[(existing.symbol, existing.environment)] = existing

    controller.apply_execution_queue_item_to_buylist(
        _queue_item(),
        _target(),
        "SIM",
        0.001,
        buylist_manager=manager,
    )

    assert existing.entry_price == 1.23
    assert existing.stop_loss == 0.45
    assert existing.position_percent == 3.0
    assert existing.stop_adr == 2.0
    assert existing.risk_percent == 4.0
    assert existing.trade_plan == "old plan"
    assert existing.monitoring_status == "EXECUTE_READY"
    assert existing.breakout_method == "execution_queue:1m"
    assert existing._planned_shares == 10
    assert existing._execution_entry_trigger == 100.1


def test_apply_queue_item_does_not_overwrite_bought_position_fields():
    controller = BuylistExecutionController(SimpleNamespace())
    manager = FakeBuylistManager()
    existing = _existing_buylist_item(
        monitoring_status="BOUGHT",
        status="BOUGHT",
        entry_price=55.0,
        stop_loss=50.0,
        shares_held=8,
        avg_cost=54.25,
        buy_date="2026-07-01",
        sell_half_done=True,
        position_percent=12.5,
    )
    manager.items[(existing.symbol, existing.environment)] = existing

    controller.apply_execution_queue_item_to_buylist(
        _queue_item(),
        _target(),
        "SIM",
        0.001,
        buylist_manager=manager,
    )

    assert existing.monitoring_status == "BOUGHT"
    assert existing.entry_price == 55.0
    assert existing.stop_loss == 50.0
    assert existing.shares_held == 8
    assert existing.avg_cost == 54.25
    assert existing.buy_date == "2026-07-01"
    assert existing.sell_half_done is True
    assert existing.position_percent == 12.5
    assert not hasattr(existing, "_planned_shares")


def test_submit_selected_queue_order_uses_queue_candidate_not_buylist_mirrors(monkeypatch):
    item = _existing_buylist_item(
        environment="PROD",
        monitoring_status="EXECUTE_READY",
        entry_price=1.23,
        stop_loss=0.45,
        breakout_method="execution_queue:1m",
    )
    sim_queue_item = _queue_item()
    sim_queue_item.selected_candidate.entry_trigger = 12.34
    sim_queue_item.selected_candidate.shares = 1
    prod_queue_item = _queue_item()
    prod_queue_item.selected_candidate.entry_trigger = 123.45
    prod_queue_item.selected_candidate.stop_loss = 120.0
    prod_queue_item.selected_candidate.shares = 7
    submissions = []

    class Manager:
        def __init__(self):
            self.items = {("SIM", "AAPL"): sim_queue_item, ("PROD", "AAPL"): prod_queue_item}
            self.mark_calls = []

        def get_item(self, symbol, environment="SIM"):
            return self.items.get((environment, symbol))

        def mark_order_submitted(self, symbol, order_id="", order_status="SUBMITTED", environment="SIM"):
            self.mark_calls.append((symbol, order_id, order_status, environment))
            self.items[(environment, symbol)].status = "ORDER_PENDING"

    manager = Manager()
    window = SimpleNamespace(
        _buylist_selected_item=lambda env: item,
        _queue_item_for_buylist_item=lambda selected: manager.get_item(selected.symbol, selected.environment),
        _buylist_auto_order_blocked=lambda selected: False,
        _first_account_no_for_environment=lambda env: "12345678",
        _has_duplicate_open_order=lambda *args: False,
        _format_execution_queue_order_review=lambda env, selected, queue: "review",
        _ensure_execution_queue_manager=lambda: manager,
        _execution_queue_status_for_buylist_item=lambda selected: "ORDER_PENDING",
        _save_buylist_state=lambda: None,
        _save_execution_queue_state=lambda: None,
        populate_buylist_dashboard=lambda: None,
        _submit_kis_buy_order=lambda selected, **kwargs: submissions.append((selected, kwargs)),
    )
    monkeypatch.setattr(
        controller_module.QMessageBox,
        "question",
        lambda *args, **kwargs: controller_module.QMessageBox.Yes,
    )

    BuylistExecutionController(window).submit_selected_queue_order("PROD")

    assert manager.mark_calls == [("AAPL", "", "PENDING", "PROD")]
    assert submissions == [(item, {"quantity": 7, "order_price": 123.45})]
    assert item.entry_price == 1.23
    assert item.stop_loss == 0.45
    assert item.monitoring_status == "ORDER_PENDING"
