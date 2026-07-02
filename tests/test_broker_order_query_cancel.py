from types import SimpleNamespace

import pytest

from src.api import kis_order
from src.core.order_state import (
    BrokerOrder,
    BrokerOrderStatusSnapshot,
    OrderIntent,
    OrderSide,
    OrderStatus,
)
from src.services.order_ledger import append_order, load_orders
from src.services.order_reconciliation import (
    cancel_and_reconcile_order,
    query_and_reconcile_unresolved_orders,
    reconcile_order_with_broker_snapshot,
)
import src.ui.mixins.buylist_mixin as buylist_mixin_module
from src.ui.main_window import MainWindow


def _order(
    *,
    environment="SIM",
    account_no="12345678-01",
    symbol="AAPL",
    status=OrderStatus.ACCEPTED,
    broker_order_id="KIS-1",
    side=OrderSide.BUY,
    quantity=10,
) -> BrokerOrder:
    order = BrokerOrder.create(
        environment=environment,
        account_no=account_no,
        symbol=symbol,
        side=side,
        intent=OrderIntent.ENTRY,
        quantity_requested=quantity,
        limit_price=100.0,
        status=status,
        buylist_symbol_key=f"{environment}:{account_no}:{symbol}",
    )
    order.broker_order_id = broker_order_id
    return order


def _snapshot(
    status: OrderStatus,
    *,
    environment="SIM",
    account_no="12345678-01",
    symbol="AAPL",
    broker_order_id="KIS-1",
    filled=0,
    remaining=0,
) -> BrokerOrderStatusSnapshot:
    return BrokerOrderStatusSnapshot(
        environment=environment,
        account_no=account_no,
        symbol=symbol,
        broker_order_id=broker_order_id,
        side=OrderSide.BUY,
        status=status,
        quantity_requested=filled + remaining,
        filled_quantity=filled,
        remaining_quantity=remaining,
        avg_fill_price=101.25,
        raw_response={"status": status.value},
    )


def test_kis_query_parses_filled_partial_working_and_cancelled_rows():
    filled = kis_order.parse_broker_order_status_snapshot(
        {
            "pdno": "AAPL",
            "odno": "KIS-1",
            "sll_buy_dvsn_cd": "02",
            "ft_ord_qty": "10",
            "ft_ccld_qty": "10",
            "nccs_qty": "0",
        },
        environment="SIM",
        account_no="12345678-01",
    )
    partial = kis_order.parse_broker_order_status_snapshot(
        {"pdno": "AAPL", "odno": "KIS-1", "ft_ord_qty": "10", "ft_ccld_qty": "4", "nccs_qty": "6"},
        environment="SIM",
        account_no="12345678-01",
    )
    working = kis_order.parse_broker_order_status_snapshot(
        {"pdno": "AAPL", "odno": "KIS-1", "ft_ord_qty": "10", "ft_ccld_qty": "0", "nccs_qty": "10"},
        environment="SIM",
        account_no="12345678-01",
        source="open_orders",
    )
    cancelled = kis_order.parse_broker_order_status_snapshot(
        {"pdno": "AAPL", "odno": "KIS-1", "ft_ord_qty": "10", "ft_ccld_qty": "0", "nccs_qty": "0", "prcs_stat_name": "CANCELLED"},
        environment="SIM",
        account_no="12345678-01",
    )

    assert filled.status == OrderStatus.FILLED
    assert partial.status == OrderStatus.PARTIALLY_FILLED
    assert working.status == OrderStatus.WORKING
    assert cancelled.status == OrderStatus.CANCELLED


def test_query_overseas_order_returns_unknown_not_found_without_credentials(monkeypatch):
    class FakeClient:
        def __init__(self, config):
            self.config = config

        def authenticate(self, force_refresh=False):
            return "token"

        def _get_with_headers(self, endpoint, tr_id, params, tr_cont=""):
            return {"rt_cd": "0", "output": []}, {}

    fake_config = SimpleNamespace(
        cano="12345678",
        account_product_code="01",
        base_url="https://kis.example",
    )
    monkeypatch.setattr(kis_order, "load_config", lambda *args, **kwargs: fake_config)
    monkeypatch.setattr(kis_order, "KisAccountClient", FakeClient)

    [snapshot] = kis_order.query_overseas_order(
        environment="SIM",
        account_no="12345678-01",
        symbol="AAPL",
        broker_order_id="KIS-404",
        side="BUY",
    )

    assert snapshot.status == OrderStatus.UNKNOWN
    assert snapshot.raw_response["not_found"] is True
    assert snapshot.broker_order_id == "KIS-404"


def test_reconcile_unknown_submission_keeps_unknown_on_unknown_snapshot():
    order = _order(status=OrderStatus.UNKNOWN_SUBMISSION_STATE, broker_order_id="")
    snapshot = _snapshot(OrderStatus.UNKNOWN, broker_order_id="", remaining=0)

    updated = reconcile_order_with_broker_snapshot(order, snapshot)

    assert updated.status == OrderStatus.UNKNOWN_SUBMISSION_STATE


def test_reconcile_unknown_submission_filled_cancelled_and_partial():
    unknown = _order(status=OrderStatus.UNKNOWN_SUBMISSION_STATE)
    filled = reconcile_order_with_broker_snapshot(unknown, _snapshot(OrderStatus.FILLED, filled=10, remaining=0))
    assert filled.status == OrderStatus.FILLED
    assert filled.filled_quantity == 10
    assert filled.remaining_quantity == 0

    accepted = _order(status=OrderStatus.ACCEPTED)
    cancelled = reconcile_order_with_broker_snapshot(accepted, _snapshot(OrderStatus.CANCELLED, remaining=0))
    assert cancelled.status == OrderStatus.CANCELLED

    partial = _order(status=OrderStatus.ACCEPTED)
    updated = reconcile_order_with_broker_snapshot(partial, _snapshot(OrderStatus.PARTIALLY_FILLED, filled=4, remaining=6))
    assert updated.status == OrderStatus.PARTIALLY_FILLED
    assert updated.filled_quantity == 4
    assert updated.remaining_quantity == 6


def test_query_and_reconcile_unresolved_orders_filters_and_continues_after_failure(monkeypatch, tmp_path):
    path = tmp_path / "orders.json"
    sim = _order(environment="SIM", account_no="11111111-01", symbol="AAPL", broker_order_id="SIM-1")
    prod = _order(environment="PROD", account_no="22222222-01", symbol="AAPL", broker_order_id="PROD-1")
    closed = _order(environment="SIM", account_no="11111111-01", symbol="MSFT", status=OrderStatus.FILLED)
    failing = _order(environment="SIM", account_no="11111111-01", symbol="TSLA", broker_order_id="FAIL")
    append_order(sim, path=path)
    append_order(prod, path=path)
    append_order(closed, path=path)
    append_order(failing, path=path)

    def fake_query(**kwargs):
        if kwargs["symbol"] == "TSLA":
            raise RuntimeError("temporary KIS error")
        return [_snapshot(OrderStatus.FILLED, environment=kwargs["environment"], account_no=kwargs["account_no"], symbol=kwargs["symbol"], broker_order_id=kwargs["broker_order_id"], filled=10)]

    monkeypatch.setattr(kis_order, "query_overseas_order", fake_query)

    updated = query_and_reconcile_unresolved_orders(environment="SIM", account_no="11111111-01", path=path)
    loaded = {(order.environment, order.symbol): order for order in load_orders(path)}

    assert [order.symbol for order in updated] == ["AAPL"]
    assert loaded[("SIM", "AAPL")].status == OrderStatus.FILLED
    assert loaded[("SIM", "TSLA")].status == OrderStatus.ACCEPTED
    assert "temporary KIS error" in loaded[("SIM", "TSLA")].error_message
    assert next(order for order in load_orders(path) if order.environment == "PROD").status == OrderStatus.ACCEPTED


def test_reconcile_service_prefers_terminal_snapshot_over_open_row(monkeypatch, tmp_path):
    path = tmp_path / "orders.json"
    order = _order(status=OrderStatus.ACCEPTED, broker_order_id="KIS-1")
    append_order(order, path=path)

    monkeypatch.setattr(
        kis_order,
        "query_overseas_order",
        lambda **kwargs: [
            _snapshot(OrderStatus.WORKING, broker_order_id="KIS-1", remaining=10),
            _snapshot(OrderStatus.FILLED, broker_order_id="KIS-1", filled=10),
        ],
    )

    [updated] = query_and_reconcile_unresolved_orders(environment="SIM", account_no="12345678-01", path=path)

    assert updated.status == OrderStatus.FILLED
    assert load_orders(path)[0].status == OrderStatus.FILLED


def test_cancel_and_reconcile_requires_broker_id_and_updates_order(monkeypatch, tmp_path):
    path = tmp_path / "orders.json"
    blocked = _order(broker_order_id="")
    append_order(blocked, path=path)

    with pytest.raises(ValueError, match="broker_order_id"):
        cancel_and_reconcile_order(blocked.client_order_id, path=path)

    open_order = _order(broker_order_id="KIS-1")
    append_order(open_order, path=path)
    monkeypatch.setattr(
        kis_order,
        "cancel_overseas_order",
        lambda **kwargs: _snapshot(
            OrderStatus.CANCEL_REQUESTED,
            broker_order_id=kwargs["broker_order_id"],
            remaining=kwargs["quantity"],
        ),
    )

    updated = cancel_and_reconcile_order(open_order.client_order_id, path=path)

    assert updated.status == OrderStatus.CANCEL_REQUESTED
    assert load_orders(path)[1].status == OrderStatus.CANCEL_REQUESTED


def test_check_order_status_unknown_not_found_keeps_manual_verification_message():
    logs = []
    item = SimpleNamespace(
        symbol="AAPL",
        environment="SIM",
        monitoring_status="UNKNOWN_SUBMISSION_STATE",
        status="UNKNOWN_SUBMISSION_STATE",
        kis_order_id="",
        breakout_method="",
    )

    class Manager:
        def get(self, symbol, environment=None):
            return item

    order = _order(status=OrderStatus.UNKNOWN_SUBMISSION_STATE, broker_order_id="")
    order.raw_status_response = {"raw_response": {"not_found": True}}
    window = MainWindow.__new__(MainWindow)
    window.buylist_manager = Manager()
    window.order_ledger = [order]
    window.append_log = logs.append
    window.populate_buylist_dashboard = lambda: None
    window.update_dashboard_summary = lambda: None
    window.apply_confirmed_order_fills_to_buylist = lambda orders: None
    window._save_buylist_state = lambda: None

    MainWindow._on_broker_order_query_finished(window, [order])

    assert item.monitoring_status == "UNKNOWN_SUBMISSION_STATE"
    assert any("manual verification is still required" in message for message in logs)


def test_cancel_order_ui_is_blocked_without_broker_order_id(monkeypatch):
    warnings = []
    item = SimpleNamespace(symbol="AAPL")
    order = _order(status=OrderStatus.ACCEPTED, broker_order_id="")
    window = MainWindow.__new__(MainWindow)
    window._selected_open_broker_order = lambda env: (item, order)
    window.append_log = lambda message: None
    monkeypatch.setattr(
        buylist_mixin_module.QMessageBox,
        "warning",
        lambda parent, title, message: warnings.append((title, message)),
    )

    MainWindow._buylist_cancel_selected_order(window, "SIM")

    assert warnings
    assert warnings[0][0] == "Cancel blocked"
    assert "no broker order id" in warnings[0][1]


def test_cancel_order_ui_confirms_and_starts_worker(monkeypatch):
    questions = []
    started = []
    item = SimpleNamespace(symbol="AAPL")
    order = _order(status=OrderStatus.WORKING, broker_order_id="KIS-1")

    class FakeSignal:
        def connect(self, callback):
            self.callback = callback

    class FakeCancelWorker:
        def __init__(self, client_order_id):
            self.client_order_id = client_order_id
            self.finished_cancel = FakeSignal()
            self.error_occurred = FakeSignal()
            self.finished = FakeSignal()

        def isRunning(self):
            return False

        def start(self):
            started.append(self.client_order_id)

    window = MainWindow.__new__(MainWindow)
    window.broker_order_cancel_worker = None
    window._selected_open_broker_order = lambda env: (item, order)
    window.append_log = lambda message: None
    monkeypatch.setattr(buylist_mixin_module, "KisOrderCancelWorker", FakeCancelWorker)
    monkeypatch.setattr(
        buylist_mixin_module.QMessageBox,
        "question",
        lambda *args, **kwargs: questions.append(args) or buylist_mixin_module.QMessageBox.Yes,
    )

    MainWindow._buylist_cancel_selected_order(window, "SIM")

    assert questions
    assert started == [order.client_order_id]
