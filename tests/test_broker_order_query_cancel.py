from datetime import datetime
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.usefixtures("authorized_full_live")

import src.ui.buylist.actions as buylist_actions_module
from src.api import kis_order
from src.core.order_state import (RESERVED_MOO_EXECUTION, BrokerOrder,
                                  BrokerOrderStatusSnapshot, OrderIntent,
                                  OrderSide, OrderStatus)
from src.services.order_ledger import append_order, load_orders
from src.services.order_reconciliation import (
    _select_snapshot_for_order, cancel_and_reconcile_order,
    query_and_reconcile_unresolved_orders,
    reconcile_order_with_broker_snapshot)
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


def test_reconciliation_does_not_fall_back_when_known_broker_id_is_missing():
    order = _order(broker_order_id="KIS-EXPECTED")
    other = BrokerOrderStatusSnapshot(
        environment="SIM",
        account_no="12345678-01",
        symbol="AAPL",
        broker_order_id="KIS-OTHER",
        side=OrderSide.BUY,
        status=OrderStatus.FILLED,
        filled_quantity=10,
    )

    assert _select_snapshot_for_order(order, [other]) is None


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


def test_kis_history_normalizes_unpadded_order_number_to_submit_identity():
    snapshot = kis_order.parse_broker_order_status_snapshot(
        {
            "pdno": "AAPL",
            "odno": "1234",
            "orgn_odno": "0",
            "rvse_cncl_dvsn": "00",
            "ft_ord_qty": "1",
            "ft_ccld_qty": "0",
            "nccs_qty": "1",
        },
        environment="PROD",
        account_no="12345678-01",
        source="history",
    )

    assert snapshot.broker_order_id == "0000001234"
    assert snapshot.status == OrderStatus.WORKING


def test_kis_cancel_history_projects_terminal_status_to_original_order_id():
    snapshot = kis_order.parse_broker_order_status_snapshot(
        {
            "pdno": "AAPL",
            "odno": "5678",
            "orgn_odno": "1234",
            "rvse_cncl_dvsn": "02",
            "rvse_cncl_dvsn_name": "취소",
            "ft_ord_qty": "1",
            "ft_ccld_qty": "0",
            "nccs_qty": "0",
        },
        environment="PROD",
        account_no="12345678-01",
        source="history",
    )

    assert snapshot.broker_order_id == "0000001234"
    assert snapshot.status == OrderStatus.CANCELLED


def test_kis_order_filter_matches_padded_submit_id_to_unpadded_history_id():
    snapshot = BrokerOrderStatusSnapshot(
        environment="PROD",
        account_no="12345678-01",
        symbol="AAPL",
        broker_order_id="1234",
        side=OrderSide.BUY,
        status=OrderStatus.WORKING,
    )

    assert kis_order._matches_order_filter(
        snapshot,
        broker_order_id="0000001234",
    )


def test_kis_reservation_identity_is_not_regular_order_number_padded():
    snapshot = kis_order.parse_broker_order_status_snapshot(
        {
            "pdno": "AAPL",
            "ovrs_rsvn_odno": "1234",
            "ft_ord_qty": "1",
        },
        environment="PROD",
        account_no="12345678-01",
        source="reservation",
    )

    assert snapshot.broker_order_id == "1234"


def test_kis_query_correlates_cancel_history_with_padded_owned_order(monkeypatch):
    class FakeClient:
        def __init__(self, config):
            self.config = config

        def authenticate(self, force_refresh=False):
            return "token"

        def _get_with_headers(self, endpoint, tr_id, params, tr_cont=""):
            if endpoint.endswith("/inquire-nccs"):
                return {"rt_cd": "0", "output": []}, {}
            return {
                "rt_cd": "0",
                "output": [
                    {
                        "pdno": "AAPL",
                        "odno": "1234",
                        "orgn_odno": "0",
                        "sll_buy_dvsn_cd": "02",
                        "rvse_cncl_dvsn": "00",
                        "ft_ord_qty": "1",
                        "ft_ccld_qty": "0",
                        "nccs_qty": "0",
                    },
                    {
                        "pdno": "AAPL",
                        "odno": "5678",
                        "orgn_odno": "1234",
                        "sll_buy_dvsn_cd": "02",
                        "rvse_cncl_dvsn": "02",
                        "rvse_cncl_dvsn_name": "취소",
                        "ft_ord_qty": "1",
                        "ft_ccld_qty": "0",
                        "nccs_qty": "0",
                    },
                ],
            }, {}

    fake_config = SimpleNamespace(
        cano="12345678",
        account_product_code="01",
        base_url="https://kis.example",
    )
    monkeypatch.setattr(kis_order, "load_config", lambda *args, **kwargs: fake_config)
    monkeypatch.setattr(kis_order, "KisAccountClient", FakeClient)

    snapshots = kis_order.query_overseas_order(
        environment="PROD",
        account_no="12345678-01",
        symbol="AAPL",
        broker_order_id="0000001234",
        side="BUY",
    )

    assert {snapshot.broker_order_id for snapshot in snapshots} == {"0000001234"}
    assert {snapshot.status for snapshot in snapshots} == {
        OrderStatus.UNKNOWN,
        OrderStatus.CANCELLED,
    }
    order = _order(
        environment="PROD",
        account_no="12345678-01",
        broker_order_id="0000001234",
        quantity=1,
    )
    selected = _select_snapshot_for_order(order, snapshots)
    assert selected is not None
    assert selected.status == OrderStatus.CANCELLED


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
        environment="PROD",
        account_no="12345678-01",
        symbol="AAPL",
        broker_order_id="KIS-404",
        side="BUY",
    )

    assert snapshot.status == OrderStatus.UNKNOWN
    assert snapshot.raw_response["not_found"] is True
    assert snapshot.broker_order_id == "KIS-404"


def test_kis_order_query_fails_closed_when_open_order_source_fails(monkeypatch):
    class FakeClient:
        def __init__(self, config):
            self.config = config

        def authenticate(self, force_refresh=False):
            return "token"

        def _get_with_headers(self, endpoint, tr_id, params, tr_cont=""):
            if endpoint.endswith("/inquire-nccs"):
                raise RuntimeError("open endpoint unavailable")
            return {"rt_cd": "0", "output": []}, {}

    fake_config = SimpleNamespace(
        cano="12345678",
        account_product_code="01",
        base_url="https://kis.example",
    )
    monkeypatch.setattr(kis_order, "load_config", lambda *a, **k: fake_config)
    monkeypatch.setattr(kis_order, "KisAccountClient", FakeClient)

    with pytest.raises(RuntimeError, match="open-order discovery failed"):
        kis_order.query_overseas_order(
            environment="PROD",
            account_no="12345678-01",
            symbol="",
        )


def test_kis_pagination_fails_closed_on_continuation_without_cursor():
    class FakeClient:
        def _get_with_headers(self, endpoint, tr_id, params, tr_cont=""):
            return {"rt_cd": "0", "output": []}, {"tr_cont": "F"}

    with pytest.raises(RuntimeError, match="continuation was requested without a cursor"):
        kis_order._query_pages(
            FakeClient(),
            endpoint="/test",
            tr_id="TEST",
            params={"CTX_AREA_FK200": "", "CTX_AREA_NK200": ""},
        )


def test_kis_reserved_order_query_parses_broker_reservation_fill(monkeypatch):
    captured_params = []

    class FakeClient:
        def __init__(self, config):
            self.config = config

        def authenticate(self, force_refresh=False):
            return "token"

        def _get_with_headers(self, endpoint, tr_id, params, tr_cont=""):
            assert endpoint.endswith("/order-resv-list")
            assert tr_id == "TTTT3039R"
            captured_params.append(dict(params))
            return {
                "rt_cd": "0",
                "output": [
                    {
                        "pdno": "AAPL",
                        "ovrs_rsvn_odno": "RSV-1",
                        "sll_buy_dvsn_cd": "01",
                        "ft_ord_qty": "4",
                        "ft_ccld_qty": "4",
                    }
                ],
            }, {}

    fake_config = SimpleNamespace(
        cano="12345678",
        account_product_code="01",
        base_url="https://kis.example",
    )
    monkeypatch.setattr(kis_order, "load_config", lambda *args, **kwargs: fake_config)
    monkeypatch.setattr(kis_order, "KisAccountClient", FakeClient)

    [snapshot] = kis_order.query_overseas_reserved_order(
        environment="PROD",
        account_no="12345678-01",
        symbol="AAPL",
        broker_order_id="RSV-1",
        side="SELL",
    )

    assert snapshot.status == OrderStatus.FILLED
    assert snapshot.side == OrderSide.SELL
    assert snapshot.filled_quantity == 4
    assert snapshot.broker_order_id == "RSV-1"
    start = datetime.strptime(captured_params[0]["INQR_STRT_DT"], "%Y%m%d")
    end = datetime.strptime(captured_params[0]["INQR_END_DT"], "%Y%m%d")
    assert (end - start).days == 6


def test_kis_reserved_order_query_rejects_seven_day_span_before_request():
    with pytest.raises(ValueError, match="shorter than 7 days"):
        kis_order._reserved_order_dates("20260801", "20260808")


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


def test_reconcile_service_routes_reserved_moo_to_reservation_query(
    monkeypatch, tmp_path
):
    path = tmp_path / "orders.json"
    order = _order(
        environment="PROD",
        side=OrderSide.SELL,
        broker_order_id="RSV-1",
        quantity=4,
    )
    order.intent = OrderIntent.PARTIAL_EXIT
    order.execution_policy = RESERVED_MOO_EXECUTION
    append_order(order, path=path)
    monkeypatch.setattr(
        kis_order,
        "query_overseas_order",
        lambda **kwargs: pytest.fail("reserved order used regular order query"),
    )
    monkeypatch.setattr(
        kis_order,
        "query_overseas_reserved_order",
        lambda **kwargs: [
            BrokerOrderStatusSnapshot(
                environment="PROD",
                account_no=kwargs["account_no"],
                symbol=kwargs["symbol"],
                broker_order_id=kwargs["broker_order_id"],
                side=OrderSide.SELL,
                status=OrderStatus.FILLED,
                quantity_requested=4,
                filled_quantity=4,
            )
        ],
    )

    [updated] = query_and_reconcile_unresolved_orders(
        environment="PROD",
        account_no="12345678-01",
        execution_policy=RESERVED_MOO_EXECUTION,
        path=path,
    )

    assert updated.status == OrderStatus.FILLED
    assert updated.filled_quantity == 4


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


def test_cancel_and_reconcile_routes_reserved_moo_to_reservation_cancel(
    monkeypatch, tmp_path
):
    path = tmp_path / "orders.json"
    order = _order(
        environment="PROD",
        side=OrderSide.SELL,
        broker_order_id="RSV-1",
        quantity=4,
    )
    order.execution_policy = RESERVED_MOO_EXECUTION
    order.submitted_at = "2026-07-27T08:00:00+00:00"
    append_order(order, path=path)
    captured = []
    monkeypatch.setattr(
        kis_order,
        "cancel_overseas_order",
        lambda **kwargs: pytest.fail("reserved order used regular cancel"),
    )
    monkeypatch.setattr(
        kis_order,
        "cancel_overseas_reserved_order",
        lambda **kwargs: captured.append(kwargs)
        or BrokerOrderStatusSnapshot(
            environment="PROD",
            account_no=kwargs["account_no"],
            symbol="",
            broker_order_id=kwargs["broker_order_id"],
            side=OrderSide.SELL,
            status=OrderStatus.CANCELLED,
        ),
    )

    updated = cancel_and_reconcile_order(order.client_order_id, path=path)

    assert updated.status == OrderStatus.CANCELLED
    assert captured[0]["reservation_date"] == "20260727"


def test_reconciliation_query_and_cancel_use_injected_broker(tmp_path):
    path = tmp_path / "orders.json"
    order = _order(broker_order_id="KIS-1")
    append_order(order, path=path)

    class FakeBroker:
        def __init__(self):
            self.query_calls = []
            self.cancel_calls = []

        def get_order(self, **kwargs):
            self.query_calls.append(kwargs)
            return [_snapshot(OrderStatus.WORKING, remaining=10)]

        def cancel_order(self, **kwargs):
            self.cancel_calls.append(kwargs)
            return _snapshot(
                OrderStatus.CANCEL_REQUESTED,
                remaining=kwargs["quantity"],
            )

    broker = FakeBroker()
    [queried] = query_and_reconcile_unresolved_orders(path=path, broker=broker)
    cancelled = cancel_and_reconcile_order(
        order.client_order_id,
        path=path,
        broker=broker,
    )

    assert queried.status == OrderStatus.WORKING
    assert cancelled.status == OrderStatus.CANCEL_REQUESTED
    assert broker.query_calls[0]["is_reserved"] is False
    assert broker.cancel_calls[0]["is_reserved"] is False


def test_check_order_status_unknown_not_found_keeps_manual_verification_message():
    logs = []
    item = SimpleNamespace(
        symbol="AAPL",
        environment="PROD",
        monitoring_status="UNKNOWN_SUBMISSION_STATE",
        status="UNKNOWN_SUBMISSION_STATE",
        kis_order_id="",
        breakout_method="",
    )

    class Manager:
        def get(self, symbol, environment=None):
            return item

    order = _order(
        environment="PROD",
        status=OrderStatus.UNKNOWN_SUBMISSION_STATE,
        broker_order_id="",
    )
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
        buylist_actions_module.QMessageBox,
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
    monkeypatch.setattr(buylist_actions_module, "KisOrderCancelWorker", FakeCancelWorker)
    monkeypatch.setattr(
        buylist_actions_module.QMessageBox,
        "question",
        lambda *args, **kwargs: questions.append(args) or buylist_actions_module.QMessageBox.Yes,
    )

    MainWindow._buylist_cancel_selected_order(window, "SIM")

    assert questions
    assert started == [order.client_order_id]


def test_reserved_cancel_confirmation_identifies_market_on_open_reservation():
    order = _order(
        environment="PROD",
        side=OrderSide.SELL,
        broker_order_id="RSV-1",
    )
    order.execution_policy = RESERVED_MOO_EXECUTION
    window = MainWindow.__new__(MainWindow)

    message = MainWindow._format_cancel_order_confirmation(window, order)

    assert "market-on-open reservation" in message
    assert "final only after KIS confirms" in message


def test_cancel_error_warns_order_is_still_active_and_checks_status(monkeypatch):
    logs = []
    warnings = []
    checked = []
    window = MainWindow.__new__(MainWindow)
    window.append_log = logs.append
    window._buylist_check_order_status = lambda env: checked.append(env)
    monkeypatch.setattr(
        buylist_actions_module.QMessageBox,
        "warning",
        lambda _parent, title, message: warnings.append((title, message)),
    )
    monkeypatch.setattr(
        buylist_actions_module.QTimer,
        "singleShot",
        lambda _delay, callback: callback(),
    )

    MainWindow._on_broker_order_cancel_error(
        window, "PROD", "reservation already forwarded"
    )

    assert checked == ["PROD"]
    assert warnings[0][0] == "Cancellation not confirmed"
    assert "still active" in warnings[0][1]
    assert any("cancel failed" in message for message in logs)
