from types import SimpleNamespace

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
        self.build_calls = 0

    def has_pending_or_submitted_order(self, symbol):
        return self.pending

    def build_or_update_from_watchlist_item(self, item, intraday_by_window, **kwargs):
        self.build_calls += 1
        self.duplicate_pending_order = kwargs["duplicate_pending_order"]
        return _queue_item(item.symbol, self.status)


class FakeBuylistManager:
    def __init__(self):
        self.items = {}

    def get(self, symbol, env):
        return self.items.get((symbol, env))

    def add(self, item):
        self.items[(item.symbol, item.environment)] = item


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
