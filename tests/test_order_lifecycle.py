import json
from types import SimpleNamespace

import pytest

from src.api import kis_order
from src.api.kis_account_snapshot_dual import KisAccountClient, KisEnvironment, KisTokenError
from src.core.order_state import (
    BrokerOrder,
    OrderIntent,
    OrderSide,
    OrderStatus,
    generate_client_order_id,
)
from src.services.order_ledger import (
    append_order,
    find_open_orders,
    has_open_order,
    has_open_order_for_buylist_item,
    load_orders,
    load_order_ledger,
    update_order,
)
from src.services import order_execution_service
from src.services.order_execution_service import DuplicateOpenOrderError, submit_guarded_overseas_order
from src.services.order_reconciliation import reconcile_orders_with_snapshot
import src.ui.mixins.buylist_mixin as buylist_mixin_module
import src.ui.main_window as main_window_module
from src.ui.main_window import MainWindow


def _snapshot(symbol: str, quantity: int, average_price: float = 0.0) -> dict:
    holdings = []
    if quantity:
        holdings.append(
            {
                "symbol": symbol,
                "quantity": quantity,
                "average_price": average_price,
            }
        )
    return {"overseas": {"holdings": holdings}, "domestic": {"holdings": []}}


def _order(
    *,
    side: OrderSide = OrderSide.BUY,
    quantity: int = 10,
    status: OrderStatus = OrderStatus.ACCEPTED,
    intent: OrderIntent = OrderIntent.ENTRY,
) -> BrokerOrder:
    return BrokerOrder.create(
        environment="SIM",
        account_no="12345678",
        symbol="AAPL",
        side=side,
        intent=intent,
        quantity_requested=quantity,
        limit_price=100.0,
        status=status,
        buylist_symbol_key="SIM:AAPL",
    )


class _FakeKisResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


def test_order_ledger_persists_and_filters_open_orders(tmp_path):
    path = tmp_path / "orders.json"
    order = _order()

    append_order(order, path=path)

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert list(raw.keys()) == ["orders"]
    assert raw["orders"][0]["client_order_id"] == order.client_order_id

    loaded = load_order_ledger(path=path)
    assert loaded[0].client_order_id == order.client_order_id
    assert has_open_order_for_buylist_item(
        "SIM",
        "12345678",
        "AAPL",
        side=OrderSide.BUY,
        path=path,
    )
    assert find_open_orders(loaded, environment="SIM", account_no="12345678", symbol="AAPL")

    order.status = OrderStatus.FILLED
    update_order(order, path=path)

    assert not has_open_order_for_buylist_item(
        "SIM",
        "12345678",
        "AAPL",
        side=OrderSide.BUY,
        path=path,
    )


def test_broker_order_serializes_and_deserializes_requested_fields():
    order = BrokerOrder.create(
        environment="prod",
        account_no="12345678-01",
        symbol="nvda",
        side=OrderSide.BUY,
        intent=OrderIntent.ENTRY,
        quantity_requested=10,
        limit_price=125.5,
        status=OrderStatus.CREATED,
        buylist_symbol_key="PROD:12345678-01:NVDA",
    )

    data = order.to_dict()
    restored = BrokerOrder.from_dict(data)

    assert restored.environment == "PROD"
    assert restored.account_no == "12345678-01"
    assert restored.symbol == "NVDA"
    assert restored.side == OrderSide.BUY
    assert restored.intent == OrderIntent.ENTRY
    assert restored.remaining_quantity == 10
    assert restored.buylist_key == "PROD:12345678-01:NVDA"


def test_generate_client_order_id_contains_idempotency_parts():
    client_order_id = generate_client_order_id(
        "PROD",
        "12345678-01",
        "nvda",
        OrderSide.BUY,
        OrderIntent.ENTRY,
    )

    assert "PROD" in client_order_id
    assert "12345678-01" in client_order_id
    assert "NVDA" in client_order_id
    assert "BUY" in client_order_id
    assert "ENTRY" in client_order_id


def test_load_orders_missing_and_malformed_are_safe(tmp_path):
    path = tmp_path / "orders.json"

    assert load_orders(path=path) == []

    path.write_text("{bad json", encoding="utf-8")

    assert load_orders(path=path) == []


def test_has_open_order_respects_open_closed_account_and_environment(tmp_path):
    path = tmp_path / "orders.json"
    open_order = _order(status=OrderStatus.ACCEPTED)
    append_order(open_order, path=path)
    for closed_status in (OrderStatus.FILLED, OrderStatus.REJECTED, OrderStatus.CANCELLED):
        closed = _order(status=closed_status)
        closed.client_order_id = f"{closed.client_order_id}-{closed_status.value}"
        append_order(closed, path=path)

    assert has_open_order("SIM", "12345678", "AAPL", side=OrderSide.BUY, intent=OrderIntent.ENTRY, path=path)
    assert not has_open_order("PROD", "12345678", "AAPL", side=OrderSide.BUY, intent=OrderIntent.ENTRY, path=path)
    assert not has_open_order("SIM", "99999999", "AAPL", side=OrderSide.BUY, intent=OrderIntent.ENTRY, path=path)

    open_order.status = OrderStatus.FILLED
    update_order(open_order, path=path)

    assert not has_open_order("SIM", "12345678", "AAPL", side=OrderSide.BUY, intent=OrderIntent.ENTRY, path=path)


def test_submit_guarded_order_persists_created_before_api_and_accepted_after(monkeypatch, tmp_path):
    path = tmp_path / "orders.json"
    captured_statuses = []
    real_append = order_execution_service.append_order

    def capture_append(order, path=path):
        captured_statuses.append(order.status)
        return real_append(order, path=path)

    def fake_place_overseas_order(**kwargs):
        persisted = load_orders(path=path)
        assert persisted[0].status == OrderStatus.SUBMITTING
        return {"rt_cd": "0", "output": {"ODNO": "KIS-123"}}

    monkeypatch.setattr(order_execution_service, "append_order", capture_append)
    monkeypatch.setattr(kis_order, "place_overseas_order", fake_place_overseas_order)

    order = submit_guarded_overseas_order(
        environment="SIM",
        account_no="12345678",
        symbol="AAPL",
        side=OrderSide.BUY,
        intent=OrderIntent.ENTRY,
        quantity=3,
        limit_price=191.23,
        path=path,
    )

    assert captured_statuses == [OrderStatus.CREATED]
    assert order.status == OrderStatus.ACCEPTED
    assert order.broker_order_id == "KIS-123"
    assert order.filled_quantity == 0
    assert order.remaining_quantity == 3
    assert load_orders(path=path)[0].status == OrderStatus.ACCEPTED


def test_submit_guarded_order_blocks_duplicate_but_isolates_account_env_and_closed(monkeypatch, tmp_path):
    path = tmp_path / "orders.json"
    append_order(_order(status=OrderStatus.ACCEPTED), path=path)
    monkeypatch.setattr(kis_order, "place_overseas_order", lambda **kwargs: {"rt_cd": "0", "output": {"ODNO": "OK"}})

    with pytest.raises(DuplicateOpenOrderError):
        submit_guarded_overseas_order(
            environment="SIM",
            account_no="12345678",
            symbol="AAPL",
            side=OrderSide.BUY,
            intent=OrderIntent.ENTRY,
            quantity=1,
            limit_price=100.0,
            path=path,
        )

    other_account = submit_guarded_overseas_order(
        environment="SIM",
        account_no="99999999",
        symbol="AAPL",
        side=OrderSide.BUY,
        intent=OrderIntent.ENTRY,
        quantity=1,
        limit_price=100.0,
        path=path,
    )
    other_env = submit_guarded_overseas_order(
        environment="PROD",
        account_no="12345678",
        symbol="AAPL",
        side=OrderSide.BUY,
        intent=OrderIntent.ENTRY,
        quantity=1,
        limit_price=100.0,
        path=path,
    )

    assert other_account.status == OrderStatus.ACCEPTED
    assert other_env.status == OrderStatus.ACCEPTED

    closed = _order(status=OrderStatus.FILLED)
    closed.client_order_id = "closed-old-order"
    append_order(closed, path=path)
    assert has_open_order("SIM", "12345678", "AAPL", side=OrderSide.BUY, intent=OrderIntent.ENTRY, path=path)


def test_same_symbol_account_with_closed_previous_order_does_not_block(monkeypatch, tmp_path):
    path = tmp_path / "orders.json"
    append_order(_order(status=OrderStatus.FILLED), path=path)
    monkeypatch.setattr(kis_order, "place_overseas_order", lambda **kwargs: {"rt_cd": "0", "output": {"ODNO": "OK"}})

    order = submit_guarded_overseas_order(
        environment="SIM",
        account_no="12345678",
        symbol="AAPL",
        side=OrderSide.BUY,
        intent=OrderIntent.ENTRY,
        quantity=1,
        limit_price=100.0,
        path=path,
    )

    assert order.status == OrderStatus.ACCEPTED
    assert order.status != OrderStatus.FILLED


def test_kis_order_worker_import_still_works():
    from src.ui.workers import KisOrderWorker

    assert KisOrderWorker is not None


def test_kis_parse_response_treats_http_token_error_as_token_error():
    response = _FakeKisResponse(
        500,
        {
            "rt_cd": "1",
            "msg_cd": "EGW00123",
            "msg1": "expired token",
        },
    )

    with pytest.raises(KisTokenError):
        KisAccountClient._parse_response(response, endpoint="/order")


def test_place_overseas_order_refreshes_expired_token_once(monkeypatch):
    auth_calls = []
    posts = []

    class FakeSession:
        def __init__(self, client):
            self.client = client

        def post(self, url, headers, json, timeout):
            posts.append(
                {
                    "url": url,
                    "headers": headers,
                    "json": json,
                    "timeout": timeout,
                }
            )
            return self.client.responses.pop(0)

    class FakeClient:
        def __init__(self):
            self.access_token = None
            self.responses = [
                _FakeKisResponse(
                    500,
                    {
                        "rt_cd": "1",
                        "msg_cd": "EGW00123",
                        "msg1": "expired token",
                    },
                ),
                _FakeKisResponse(200, {"rt_cd": "0", "output": {"ODNO": "KIS-123"}}),
            ]
            self.session = FakeSession(self)

        def authenticate(self, force_refresh=False):
            auth_calls.append(force_refresh)
            self.access_token = "fresh-token" if force_refresh else "cached-token"
            return self.access_token

        def _headers(self, tr_id, tr_cont=""):
            if not self.access_token:
                self.authenticate()
            return {"authorization": f"Bearer {self.access_token}", "tr_id": tr_id}

        def _parse_response(self, response, endpoint, check_rt_cd=True):
            return KisAccountClient._parse_response(
                response,
                endpoint=endpoint,
                check_rt_cd=check_rt_cd,
            )

    fake_client = FakeClient()
    fake_config = SimpleNamespace(
        base_url="https://kis.example",
        cano="12345678",
        account_product_code="01",
        app_key="app-key",
        app_secret="app-secret",
    )
    monkeypatch.setattr(kis_order, "load_config", lambda *args, **kwargs: fake_config)
    monkeypatch.setattr(kis_order, "KisAccountClient", lambda _config: fake_client)

    result = kis_order.place_overseas_order(
        environment=KisEnvironment.SIM.value,
        account_no="12345678-01",
        symbol="AAPL",
        quantity=3,
        price=191.23,
        side="sell",
    )

    assert result["output"]["ODNO"] == "KIS-123"
    assert auth_calls == [False, True]
    assert len(posts) == 2
    assert posts[0]["headers"]["authorization"] == "Bearer cached-token"
    assert posts[1]["headers"]["authorization"] == "Bearer fresh-token"


def test_submit_overseas_order_records_acceptance_not_fill(monkeypatch):
    def fake_place_overseas_order(**kwargs):
        return {"rt_cd": "0", "output": {"ODNO": "KIS-123"}}

    monkeypatch.setattr(kis_order, "place_overseas_order", fake_place_overseas_order)

    order = kis_order.submit_overseas_order(
        environment="SIM",
        account_no="12345678",
        symbol="AAPL",
        quantity=3,
        price=191.23,
        side="buy",
        intent=OrderIntent.ENTRY,
    )

    assert order.status == OrderStatus.ACCEPTED
    assert order.broker_order_id == "KIS-123"
    assert order.filled_quantity == 0
    assert order.remaining_quantity == 3


def test_submit_overseas_order_rejection_is_explicit(monkeypatch):
    def fake_place_overseas_order(**kwargs):
        raise RuntimeError("KIS rejected account")

    monkeypatch.setattr(kis_order, "place_overseas_order", fake_place_overseas_order)

    order = kis_order.submit_overseas_order(
        environment="SIM",
        account_no="12345678",
        symbol="AAPL",
        quantity=3,
        price=191.23,
        side="sell",
        intent=OrderIntent.MANUAL_EXIT,
    )

    assert order.status == OrderStatus.REJECTED
    assert "KIS rejected account" in order.error_message
    assert order.filled_quantity == 0


def test_reconciliation_marks_buy_filled_only_from_holdings_delta():
    order = _order(side=OrderSide.BUY, quantity=10)

    [updated] = reconcile_orders_with_snapshot(
        [order],
        snapshot=_snapshot("AAPL", 10, 101.25),
        previous_snapshot=_snapshot("AAPL", 0),
    )

    assert updated.status == OrderStatus.FILLED
    assert updated.filled_quantity == 10
    assert updated.remaining_quantity == 0
    assert updated.avg_fill_price == 101.25


def test_reconciliation_keeps_ambiguous_order_working_without_baseline():
    order = _order(side=OrderSide.BUY, quantity=10)

    [updated] = reconcile_orders_with_snapshot(
        [order],
        snapshot=_snapshot("AAPL", 10, 101.25),
        previous_snapshot=None,
    )

    assert updated.status == OrderStatus.WORKING
    assert updated.filled_quantity == 0


def test_reconciliation_marks_partial_sell_from_holdings_delta():
    order = _order(
        side=OrderSide.SELL,
        quantity=10,
        intent=OrderIntent.PARTIAL_TAKE_PROFIT,
    )

    [updated] = reconcile_orders_with_snapshot(
        [order],
        snapshot=_snapshot("AAPL", 6, 100.0),
        previous_snapshot=_snapshot("AAPL", 10, 100.0),
    )

    assert updated.status == OrderStatus.PARTIALLY_FILLED
    assert updated.filled_quantity == 4
    assert updated.remaining_quantity == 6


def test_buy_acceptance_does_not_mark_position_filled(monkeypatch):
    logs = []
    recorded = []
    save_calls = []
    item = SimpleNamespace(
        symbol="AAPL",
        environment="SIM",
        _buy_order_pending=True,
        monitoring_status="ACTIVE",
        shares_held=0,
        avg_cost=0.0,
        buy_date="",
        position_percent=0.0,
        kis_order_id="",
    )
    window = MainWindow.__new__(MainWindow)
    window.order_ledger = []
    window.buylist_manager = SimpleNamespace()
    window._save_state = lambda: save_calls.append(True)
    window.populate_buylist_dashboard = lambda: None
    window.append_log = logs.append

    monkeypatch.setattr(main_window_module, "append_order", lambda order: recorded.append(order))
    monkeypatch.setattr(main_window_module, "load_order_ledger", lambda: list(recorded))
    monkeypatch.setattr(main_window_module.QTimer, "singleShot", lambda *_args: None)

    order = _order(side=OrderSide.BUY, quantity=5)
    order.broker_order_id = "KIS-1"

    MainWindow._on_buy_order_accepted(window, item, order)

    assert item.monitoring_status == "BUY_SUBMITTED"
    assert item.kis_order_id == "KIS-1"
    assert item.shares_held == 0
    assert item.avg_cost == 0.0
    assert item.buy_date == ""
    assert recorded == [order]
    assert save_calls == [True]
    assert any("waiting for fill confirmation" in message for message in logs)


def test_sell_acceptance_does_not_reduce_position_or_move_stop(monkeypatch):
    recorded = []
    save_calls = []
    item = SimpleNamespace(
        symbol="AAPL",
        environment="SIM",
        _stop_order_pending=True,
        monitoring_status="BOUGHT",
        shares_held=10,
        avg_cost=100.0,
        stop_loss=90.0,
        sell_half_done=False,
        entry_price=100.0,
        current_price=110.0,
        kis_order_id="",
    )
    window = MainWindow.__new__(MainWindow)
    window.order_ledger = []
    window.buylist_manager = SimpleNamespace()
    window._save_state = lambda: save_calls.append(True)
    window.populate_buylist_dashboard = lambda: None
    window.append_log = lambda _message: None

    monkeypatch.setattr(main_window_module, "append_order", lambda order: recorded.append(order))
    monkeypatch.setattr(main_window_module, "load_order_ledger", lambda: list(recorded))
    monkeypatch.setattr(main_window_module.QTimer, "singleShot", lambda *_args: None)

    order = _order(
        side=OrderSide.SELL,
        quantity=5,
        intent=OrderIntent.PARTIAL_TAKE_PROFIT,
    )
    order.broker_order_id = "KIS-2"

    MainWindow._on_sell_order_accepted(window, item, 5, "partial exit", order)

    assert item.monitoring_status == "PARTIAL_EXIT_SUBMITTED"
    assert item.shares_held == 10
    assert item.stop_loss == 90.0
    assert item.sell_half_done is False
    assert item.kis_order_id == "KIS-2"
    assert save_calls == [True]


def test_sell_rejection_keeps_held_position_as_bought(monkeypatch):
    logs = []
    save_calls = []
    item = SimpleNamespace(
        symbol="AAPL",
        environment="SIM",
        _stop_order_pending=True,
        monitoring_status="BOUGHT",
        shares_held=10,
        avg_cost=100.0,
        stop_loss=90.0,
        sell_half_done=False,
        entry_price=100.0,
        kis_order_id="BUY-ORDER",
    )
    window = MainWindow.__new__(MainWindow)
    window.order_ledger = []
    window.buylist_manager = SimpleNamespace()
    window._save_state = lambda: save_calls.append(True)
    window.populate_buylist_dashboard = lambda: None
    window.append_log = logs.append

    monkeypatch.setattr(main_window_module, "append_order", lambda order: None)
    monkeypatch.setattr(main_window_module, "load_order_ledger", lambda: [])
    monkeypatch.setattr(buylist_mixin_module.QMessageBox, "warning", lambda *args, **kwargs: None)

    order = _order(
        side=OrderSide.SELL,
        quantity=10,
        intent=OrderIntent.STOP_LOSS,
        status=OrderStatus.REJECTED,
    )
    order.error_message = "token expired"

    MainWindow._on_sell_order_accepted(window, item, 10, "stop-loss", order)

    assert item.monitoring_status == "BOUGHT"
    assert item.shares_held == 10
    assert item._stop_order_pending is False
    assert save_calls == [True]
    assert any("status restored to BOUGHT" in message for message in logs)


def test_kis_sim_unsupported_sell_rejection_blocks_auto_retry(monkeypatch):
    logs = []
    save_calls = []
    item = SimpleNamespace(
        symbol="AAPL",
        environment="SIM",
        _stop_order_pending=True,
        monitoring_status="BOUGHT",
        shares_held=10,
        avg_cost=100.0,
        stop_loss=90.0,
        sell_half_done=False,
        entry_price=100.0,
        kis_order_id="BUY-ORDER",
        auto_order_block_reason="",
    )
    window = MainWindow.__new__(MainWindow)
    window.order_ledger = []
    window.buylist_manager = SimpleNamespace()
    window._save_state = lambda: save_calls.append(True)
    window.populate_buylist_dashboard = lambda: None
    window.append_log = logs.append

    monkeypatch.setattr(main_window_module, "append_order", lambda order: None)
    monkeypatch.setattr(main_window_module, "load_order_ledger", lambda: [])
    monkeypatch.setattr(buylist_mixin_module.QMessageBox, "warning", lambda *args, **kwargs: None)

    order = _order(
        side=OrderSide.SELL,
        quantity=10,
        intent=OrderIntent.STOP_LOSS,
        status=OrderStatus.REJECTED,
    )
    order.error_message = (
        "KIS API error from /uapi/overseas-stock/v1/trading/order: "
        "90000000 mock investment does not provide this task"
    )

    MainWindow._on_sell_order_accepted(window, item, 10, "stop-loss", order)

    assert item.monitoring_status == "BOUGHT"
    assert item._stop_order_pending is False
    assert "90000000" in item.auto_order_block_reason
    assert save_calls == [True]
    assert any("Auto KIS order retries blocked for AAPL" in message for message in logs)


def test_monitor_skips_blocked_stop_loss_auto_order():
    logs = []
    submitted = []
    item = SimpleNamespace(
        symbol="AAPL",
        environment="SIM",
        monitoring_status="BOUGHT",
        shares_held=10,
        avg_cost=100.0,
        stop_loss=95.0,
        sell_half_done=False,
        entry_price=100.0,
        auto_order_block_reason="KIS SIM rejected overseas order routing for this account/API (90000000).",
    )
    window = MainWindow.__new__(MainWindow)
    window.buylist_manager = SimpleNamespace(items=[item])
    window.latest_intraday_prices = {"AAPL": 90.0}
    window._buylist_refresh_item_data = lambda _item: None
    window._populate_buylist_env_table = lambda _env: None
    window._submit_kis_sell_order = lambda *args, **kwargs: submitted.append(args)
    window.append_log = logs.append

    MainWindow._run_buylist_monitor_cycle(window, "SIM")
    MainWindow._run_buylist_monitor_cycle(window, "SIM")

    assert submitted == []
    assert item.monitoring_status == "BOUGHT"
    assert len([message for message in logs if "auto KIS order is blocked" in message]) == 1


def test_monitor_restores_error_position_with_shares_to_bought():
    logs = []
    save_calls = []
    item = SimpleNamespace(
        symbol="AAPL",
        monitoring_status="ERROR",
        shares_held=10,
        _stop_order_pending=True,
    )
    window = MainWindow.__new__(MainWindow)
    window._save_state = lambda: save_calls.append(True)
    window.append_log = logs.append

    MainWindow._restore_monitorable_buylist_error_positions(window, [item], "SIM")

    assert item.monitoring_status == "BOUGHT"
    assert item._stop_order_pending is False
    assert save_calls == [True]
    assert any("restored from ERROR to BOUGHT" in message for message in logs)


def test_buylist_order_price_uses_intraday_cache_without_current_price():
    item = SimpleNamespace(
        symbol="AAPL",
        environment="SIM",
        stop_loss=90.0,
        avg_cost=100.0,
        entry_price=95.0,
    )
    window = MainWindow.__new__(MainWindow)
    window.latest_intraday_prices = {"AAPL": 88.42}

    assert MainWindow._buylist_order_environment(item) == "SIM"
    assert MainWindow._buylist_order_price(window, item) == 88.42

    window.latest_intraday_prices = {}

    assert MainWindow._buylist_order_price(window, item) == 90.0


def test_submit_kis_sell_order_uses_environment_and_live_price_without_current_price(monkeypatch):
    logs = []
    created_workers = []

    class FakeSignal:
        def __init__(self):
            self.callbacks = []

        def connect(self, callback):
            self.callbacks.append(callback)

    class FakeKisOrderWorker:
        def __init__(
            self,
            environment,
            symbol,
            quantity,
            price,
            side,
            exchange="NASD",
            order_type="limit",
            account_no=None,
            intent=OrderIntent.UNKNOWN,
            buylist_symbol_key="",
        ):
            self.environment = environment
            self.symbol = symbol
            self.quantity = quantity
            self.price = price
            self.side = side
            self.exchange = exchange
            self.order_type = order_type
            self.account_no = account_no
            self.intent = intent
            self.buylist_symbol_key = buylist_symbol_key
            self.finished_order = FakeSignal()
            self.error_occurred = FakeSignal()
            self.started = False
            created_workers.append(self)

        def start(self):
            self.started = True

    item = SimpleNamespace(
        symbol="AAPL",
        environment="SIM",
        _stop_order_pending=True,
        monitoring_status="BOUGHT",
        shares_held=10,
        avg_cost=100.0,
        stop_loss=90.0,
        entry_price=95.0,
    )
    window = MainWindow.__new__(MainWindow)
    window.latest_intraday_prices = {"AAPL": 88.5}
    window.append_log = logs.append
    window._first_account_no_for_environment = lambda environment: "12345678"
    window._has_duplicate_open_order = lambda *args: False
    window.buylist_manager = SimpleNamespace()
    window.populate_buylist_dashboard = lambda: None

    monkeypatch.setattr(buylist_mixin_module, "KisOrderWorker", FakeKisOrderWorker)

    MainWindow._submit_kis_sell_order(window, item, 10, "stop-loss")

    assert len(created_workers) == 1
    worker = created_workers[0]
    assert worker.environment == "SIM"
    assert worker.symbol == "AAPL"
    assert worker.quantity == 10
    assert worker.price == 88.5
    assert worker.side == "sell"
    assert worker.account_no == "12345678"
    assert worker.intent == OrderIntent.STOP_LOSS
    assert worker.buylist_symbol_key == "SIM:AAPL"
    assert worker.started is True
    assert any("SELL submitted for AAPL" in message for message in logs)


def test_submit_kis_buy_order_honors_explicit_order_price_over_live_price(monkeypatch):
    logs = []
    created_workers = []

    class FakeSignal:
        def __init__(self):
            self.callbacks = []

        def connect(self, callback):
            self.callbacks.append(callback)

    class FakeKisOrderWorker:
        def __init__(
            self,
            environment,
            symbol,
            quantity,
            price,
            side,
            exchange="NASD",
            order_type="limit",
            account_no=None,
            intent=OrderIntent.UNKNOWN,
            buylist_symbol_key="",
        ):
            self.environment = environment
            self.symbol = symbol
            self.quantity = quantity
            self.price = price
            self.side = side
            self.account_no = account_no
            self.intent = intent
            self.buylist_symbol_key = buylist_symbol_key
            self.finished_order = FakeSignal()
            self.error_occurred = FakeSignal()
            self.started = False
            created_workers.append(self)

        def start(self):
            self.started = True

    item = SimpleNamespace(
        symbol="AAPL",
        environment="SIM",
        _buy_order_pending=True,
        monitoring_status="ORDER_PENDING",
        breakout_method="execution_queue:1m",
        shares_held=0,
        avg_cost=0.0,
        stop_loss=90.0,
        entry_price=1.23,
        position_percent=50.0,
    )
    window = MainWindow.__new__(MainWindow)
    window.latest_intraday_prices = {"AAPL": 999.0}
    window.append_log = logs.append
    window._first_account_no_for_environment = lambda environment: "12345678"
    window._has_duplicate_open_order = lambda *args: False
    window._ensure_execution_queue_manager = lambda: SimpleNamespace(items={})
    window.buylist_manager = SimpleNamespace()
    window.populate_buylist_dashboard = lambda: None

    monkeypatch.setattr(buylist_mixin_module, "KisOrderWorker", FakeKisOrderWorker)

    MainWindow._submit_kis_buy_order(window, item, quantity=7, order_price=123.45)

    assert len(created_workers) == 1
    worker = created_workers[0]
    assert worker.price == 123.45
    assert worker.quantity == 7
    assert worker.side == "buy"
    assert worker.intent == OrderIntent.ENTRY
    assert worker.started is True
    assert any("BUY submitted for AAPL: 7 shares @ limit $123.45" in message for message in logs)


def test_apply_partial_sell_fill_is_idempotent(monkeypatch):
    save_calls = []
    item = SimpleNamespace(
        symbol="AAPL",
        market="SIM",
        shares_held=10,
        avg_cost=100.0,
        stop_loss=90.0,
        sell_half_done=False,
        kis_order_id="",
        monitoring_status="PARTIAL_EXIT_SUBMITTED",
    )

    class Manager:
        def get(self, symbol, environment=None):
            assert symbol == "AAPL"
            assert environment == "SIM"
            return item

    window = MainWindow.__new__(MainWindow)
    window.buylist_manager = Manager()
    window._save_state = lambda: save_calls.append(True)
    window.populate_buylist_dashboard = lambda: None
    window.append_log = lambda _message: None

    monkeypatch.setattr(main_window_module, "update_order", lambda order: order)
    monkeypatch.setattr(main_window_module, "load_order_ledger", lambda: [])

    order = _order(
        side=OrderSide.SELL,
        quantity=10,
        intent=OrderIntent.PARTIAL_TAKE_PROFIT,
        status=OrderStatus.PARTIALLY_FILLED,
    )
    order.filled_quantity = 4
    order.remaining_quantity = 6

    MainWindow.apply_confirmed_order_fills_to_buylist(window, [order])
    MainWindow.apply_confirmed_order_fills_to_buylist(window, [order])

    assert item.shares_held == 6
    assert item.sell_half_done is True
    assert item.stop_loss == 100.0
    assert order.applied_filled_quantity == 4
    assert len(save_calls) == 1


def test_buylist_position_sync_uses_total_kis_holding_quantity():
    logs = []
    save_calls = []
    populate_calls = []
    item = SimpleNamespace(
        symbol="MRVL",
        environment="SIM",
        monitoring_status="BOUGHT",
        shares_held=23,
        avg_cost=270.0,
        buy_date=None,
        _buy_order_pending=True,
    )
    snapshot = _snapshot("MRVL", 41, 272.25)
    window = MainWindow.__new__(MainWindow)
    window.buylist_manager = SimpleNamespace(items=[item])
    window.append_log = logs.append
    window._save_state = lambda: save_calls.append(True)
    window.populate_buylist_dashboard = lambda: populate_calls.append(True)

    changed = MainWindow.sync_buylist_positions_from_kis_snapshots(
        window,
        {("SIM", "50194787-01"): snapshot},
    )

    assert changed == 1
    assert item.monitoring_status == "BOUGHT"
    assert item.shares_held == 41
    assert item.avg_cost == 272.25
    assert item._buy_order_pending is False
    assert item.buy_date is not None
    assert save_calls == [True]
    assert populate_calls == [True]
    assert any("shares 23 -> 41" in message for message in logs)


def test_buylist_position_sync_leaves_queued_item_without_holding_unchanged():
    item = SimpleNamespace(
        symbol="MRVL",
        environment="SIM",
        monitoring_status="ACTIVE",
        shares_held=0,
        avg_cost=0.0,
        buy_date=None,
    )
    window = MainWindow.__new__(MainWindow)
    window.buylist_manager = SimpleNamespace(items=[item])
    window.append_log = lambda _message: None
    window._save_state = lambda: None
    window.populate_buylist_dashboard = lambda: None

    changed = MainWindow.sync_buylist_positions_from_kis_snapshots(
        window,
        {("SIM", "50194787-01"): _snapshot("AAPL", 10, 100.0)},
    )

    assert changed == 0
    assert item.monitoring_status == "ACTIVE"
    assert item.shares_held == 0
    assert item.avg_cost == 0.0


def test_startup_unresolved_order_state_uses_app_state_save():
    save_calls = []
    populate_calls = []
    item = SimpleNamespace(
        symbol="AAPL",
        environment="SIM",
        monitoring_status="BOUGHT",
        kis_order_id="",
    )

    class Manager:
        def get(self, symbol, environment=None):
            assert symbol == "AAPL"
            assert environment == "SIM"
            return item

    order = _order(
        side=OrderSide.SELL,
        quantity=5,
        intent=OrderIntent.STOP_LOSS,
        status=OrderStatus.ACCEPTED,
    )
    order.broker_order_id = "KIS-STOP"

    window = MainWindow.__new__(MainWindow)
    window.order_ledger = [order]
    window.buylist_manager = Manager()
    window.append_log = lambda _message: None
    window._save_state = lambda: save_calls.append(True)
    window.populate_buylist_dashboard = lambda: populate_calls.append(True)

    MainWindow._apply_unresolved_order_startup_state(window)

    assert item.monitoring_status == "SELL_SUBMITTED"
    assert item.kis_order_id == "KIS-STOP"
    assert save_calls == [True]
    assert populate_calls == [True]
