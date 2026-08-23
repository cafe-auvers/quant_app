import datetime as dt
import json
import threading
from types import SimpleNamespace

import pytest
import requests

pytestmark = pytest.mark.usefixtures("authorized_full_live")

import src.ui.buylist.actions as buylist_actions_module
import src.ui.buylist.constants as buylist_constants
import src.ui.buylist.orders as buylist_orders_module
import src.ui.main_window as main_window_module
from src.api import kis_order
from src.api.kis_account_snapshot_dual import (KisAccountClient,
                                               KisEnvironment,
                                               KisRateLimitError,
                                               KisTokenError)
from src.core import execution_config
from src.core.order_state import (REGULAR_LIMIT_EXECUTION,
                                  RESERVED_MOO_EXECUTION, BrokerOrder,
                                  OrderIntent, OrderSide, OrderStatus,
                                  generate_client_order_id)
from src.risk.pre_trade import PreTradeRiskDecision
from src.services import order_execution_service
from src.services.order_execution_service import (
    DuplicateOpenOrderError, submit_guarded_overseas_order)
from src.services.order_ledger import (OrderLedgerCorruptionError,
                                       append_order,
                                       clear_unknown_submission_order,
                                       find_open_orders, has_open_order,
                                       has_open_order_for_buylist_item,
                                       load_order_ledger, load_orders,
                                       update_order)
from src.services.order_reconciliation import reconcile_orders_with_snapshot
from src.services.position_manager import compute_breakeven_stop_price
from src.ui.main_window import MainWindow

RISK_STRATEGY_ID = "TEST"
RISK_PLAN_ID = "TEST:AAPL"


def _risk_approval(
    quantity,
    *,
    environment="SIM",
    account_no="12345678",
    symbol="AAPL",
    reference_price=100.0,
):
    return PreTradeRiskDecision.approve(
        environment=environment,
        account_no=account_no,
        symbol=symbol,
        side=OrderSide.BUY,
        intent=OrderIntent.ENTRY,
        quantity=quantity,
        reference_price=reference_price,
        exchange="NASD",
        execution_policy=REGULAR_LIMIT_EXECUTION,
        strategy_id=RISK_STRATEGY_ID,
        plan_id=RISK_PLAN_ID,
    )


def _risk_metadata():
    return {"strategy_id": RISK_STRATEGY_ID, "plan_id": RISK_PLAN_ID}


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


def test_load_orders_missing_is_empty_but_malformed_fails_closed(tmp_path):
    path = tmp_path / "orders.json"

    assert load_orders(path=path) == []

    path.write_text("{bad json", encoding="utf-8")

    with pytest.raises(OrderLedgerCorruptionError, match="submission is blocked"):
        load_orders(path=path)


def test_load_orders_recovers_from_valid_backup(tmp_path):
    path = tmp_path / "orders.json"
    order = _order()
    path.write_text("{bad json", encoding="utf-8")
    path.with_suffix(".json.bak").write_text(
        json.dumps({"orders": [order.to_dict()]}),
        encoding="utf-8",
    )

    assert load_orders(path=path)[0].client_order_id == order.client_order_id


def test_mutation_after_backup_recovery_preserves_good_backup(tmp_path):
    path = tmp_path / "orders.json"
    recovered = _order()
    new_order = _order(side=OrderSide.SELL, intent=OrderIntent.PARTIAL_EXIT)
    new_order.client_order_id = f"{new_order.client_order_id}-new"
    path.write_text("{bad json", encoding="utf-8")
    backup_path = path.with_suffix(".json.bak")
    backup_path.write_text(
        json.dumps({"orders": [recovered.to_dict()]}),
        encoding="utf-8",
    )

    append_order(new_order, path=path)

    assert {order.client_order_id for order in load_orders(path)} == {
        recovered.client_order_id,
        new_order.client_order_id,
    }
    backup_payload = json.loads(backup_path.read_text(encoding="utf-8"))
    assert backup_payload == {"orders": [recovered.to_dict()]}
    assert len(list(tmp_path.glob("orders.json.corrupt-*"))) == 1


def test_load_orders_rejects_one_malformed_record_instead_of_skipping_it(tmp_path):
    path = tmp_path / "orders.json"
    path.write_text(
        json.dumps({"orders": [_order().to_dict(), {"symbol": "MSFT"}]}),
        encoding="utf-8",
    )

    with pytest.raises(OrderLedgerCorruptionError, match="invalid record"):
        load_orders(path=path)


def test_guarded_submission_does_not_call_broker_with_corrupt_ledger(
    monkeypatch, tmp_path, trading_enabled
):
    path = tmp_path / "orders.json"
    path.write_text("{bad json", encoding="utf-8")
    monkeypatch.setattr(
        kis_order,
        "place_overseas_order",
        lambda **kwargs: pytest.fail("broker must not be called"),
    )

    with pytest.raises(OrderLedgerCorruptionError):
        submit_guarded_overseas_order(
            environment="PROD",
            account_no="12345678-01",
            symbol="AAPL",
            side=OrderSide.BUY,
            intent=OrderIntent.ENTRY,
            quantity=1,
            limit_price=100.0,
            path=path,
            pre_trade_risk_decision=_risk_approval(
                1,
                environment="PROD",
                account_no="12345678-01",
            ),
            **_risk_metadata(),
        )


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


def test_submit_guarded_order_refuses_when_trading_disabled(monkeypatch, tmp_path):
    from src.services import trading_state
    from src.services.order_execution_service import TradingDisabledError

    path = tmp_path / "orders.json"
    trading_state.set_trading_enabled(False)
    monkeypatch.setattr(
        kis_order,
        "place_overseas_order",
        lambda **kwargs: pytest.fail("broker must not be called while trading is disabled"),
    )

    with pytest.raises(TradingDisabledError):
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

    # No ledger entry at all -- not even a CREATED/UNKNOWN_SUBMISSION_STATE row.
    assert load_orders(path=path) == []


def test_submit_guarded_order_refuses_prod_and_sim_alike_when_disabled(monkeypatch, tmp_path):
    from src.services import trading_state
    from src.services.order_execution_service import TradingDisabledError

    path = tmp_path / "orders.json"
    trading_state.set_trading_enabled(False)
    monkeypatch.setattr(
        kis_order,
        "place_overseas_order",
        lambda **kwargs: pytest.fail("broker must not be called while trading is disabled"),
    )

    for environment in ("PROD", "SIM"):
        with pytest.raises(TradingDisabledError):
            submit_guarded_overseas_order(
                environment=environment,
                account_no="12345678",
                symbol="AAPL",
                side=OrderSide.BUY,
                intent=OrderIntent.ENTRY,
                quantity=1,
                limit_price=100.0,
                path=path,
            )


def test_submit_guarded_order_proceeds_normally_once_trading_enabled(monkeypatch, tmp_path):
    from src.services import trading_state

    path = tmp_path / "orders.json"
    trading_state.set_trading_enabled(True)
    monkeypatch.setattr(
        kis_order, "place_overseas_order", lambda **kwargs: {"rt_cd": "0", "output": {"ODNO": "OK"}}
    )

    order = submit_guarded_overseas_order(
        environment="SIM",
        account_no="12345678",
        symbol="AAPL",
        side=OrderSide.BUY,
        intent=OrderIntent.ENTRY,
        quantity=1,
        limit_price=100.0,
        path=path,
        pre_trade_risk_decision=_risk_approval(1),
        **_risk_metadata(),
    )

    assert order.status == OrderStatus.ACCEPTED


def test_submit_guarded_order_persists_created_before_api_and_accepted_after(
    monkeypatch, tmp_path, trading_enabled
):
    path = tmp_path / "orders.json"
    captured_statuses = []
    real_reserve = order_execution_service.reserve_order_if_no_matching_open

    def capture_reserve(order, *, allow_duplicate=False, path=path):
        captured_statuses.append(order.status)
        return real_reserve(order, allow_duplicate=allow_duplicate, path=path)

    def fake_place_overseas_order(**kwargs):
        persisted = load_orders(path=path)
        assert persisted[0].status == OrderStatus.UNKNOWN_SUBMISSION_STATE
        return {"rt_cd": "0", "output": {"ODNO": "KIS-123"}}

    monkeypatch.setattr(order_execution_service, "reserve_order_if_no_matching_open", capture_reserve)
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
        pre_trade_risk_decision=_risk_approval(3, reference_price=191.23),
        **_risk_metadata(),
    )

    assert captured_statuses == [OrderStatus.CREATED]
    assert order.status == OrderStatus.ACCEPTED
    assert order.broker_order_id == "KIS-123"
    assert order.filled_quantity == 0
    assert order.remaining_quantity == 3
    assert load_orders(path=path)[0].status == OrderStatus.ACCEPTED


def test_disabling_during_local_reservation_prevents_broker_call(
    monkeypatch, tmp_path, trading_enabled
):
    from src.services import trading_state
    from src.services.order_execution_service import TradingDisabledError

    path = tmp_path / "orders.json"
    real_reserve = order_execution_service.reserve_order_if_no_matching_open

    def reserve_then_disable(order, *, allow_duplicate=False, path=path):
        match = real_reserve(order, allow_duplicate=allow_duplicate, path=path)
        trading_state.set_trading_enabled(False)
        return match

    monkeypatch.setattr(
        order_execution_service,
        "reserve_order_if_no_matching_open",
        reserve_then_disable,
    )
    monkeypatch.setattr(
        kis_order,
        "place_overseas_order",
        lambda **kwargs: pytest.fail("broker must not be called after disarming"),
    )

    with pytest.raises(TradingDisabledError):
        submit_guarded_overseas_order(
            environment="SIM",
            account_no="12345678",
            symbol="AAPL",
            side=OrderSide.BUY,
            intent=OrderIntent.ENTRY,
            quantity=1,
            limit_price=100.0,
            path=path,
            pre_trade_risk_decision=_risk_approval(1),
            **_risk_metadata(),
        )

    [persisted] = load_orders(path=path)
    assert persisted.status == OrderStatus.REJECTED
    assert "kill switch off" in persisted.error_message


def test_low_level_kis_boundary_rechecks_after_authentication(
    monkeypatch, trading_enabled
):
    from src.services import trading_state
    from src.services.trading_state import TradingDisabledError

    class FakeSession:
        def post(self, *args, **kwargs):
            pytest.fail("disarming during authentication must prevent the HTTP POST")

    class FakeClient:
        session = FakeSession()

        def authenticate(self, force_refresh=False):
            trading_state.set_trading_enabled(False)
            return "token"

        def _headers(self, tr_id, tr_cont=""):
            return {"tr_id": tr_id}

    fake_config = SimpleNamespace(
        base_url="https://kis.example",
        cano="12345678",
        account_product_code="01",
    )
    monkeypatch.setattr(kis_order, "load_config", lambda *args, **kwargs: fake_config)
    monkeypatch.setattr(kis_order, "KisAccountClient", lambda _config: FakeClient())

    with pytest.raises(TradingDisabledError):
        kis_order.place_overseas_order(
            environment="PROD",
            account_no="12345678-01",
            symbol="AAPL",
            quantity=1,
            price=100.0,
            side="buy",
        )


def test_submit_guarded_order_blocks_duplicate_but_isolates_account_env_and_closed(
    monkeypatch, tmp_path, trading_enabled
):
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
            pre_trade_risk_decision=_risk_approval(1),
            **_risk_metadata(),
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
        pre_trade_risk_decision=_risk_approval(1, account_no="99999999"),
        **_risk_metadata(),
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
        pre_trade_risk_decision=_risk_approval(1, environment="PROD"),
        **_risk_metadata(),
    )

    assert other_account.status == OrderStatus.ACCEPTED
    assert other_env.status == OrderStatus.ACCEPTED

    closed = _order(status=OrderStatus.FILLED)
    closed.client_order_id = "closed-old-order"
    append_order(closed, path=path)
    assert has_open_order("SIM", "12345678", "AAPL", side=OrderSide.BUY, intent=OrderIntent.ENTRY, path=path)


def test_same_symbol_account_with_closed_previous_order_does_not_block(
    monkeypatch, tmp_path, trading_enabled
):
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
        pre_trade_risk_decision=_risk_approval(1),
        **_risk_metadata(),
    )

    assert order.status == OrderStatus.ACCEPTED
    assert order.status != OrderStatus.FILLED


def test_concurrent_guarded_submissions_reserve_only_one_order(
    monkeypatch, tmp_path, trading_enabled
):
    path = tmp_path / "orders.json"
    start = threading.Barrier(2)
    submitted = []
    results = []

    def fake_place_overseas_order(**kwargs):
        submitted.append(kwargs)
        return {"rt_cd": "0", "output": {"ODNO": "KIS-ONE"}}

    monkeypatch.setattr(kis_order, "place_overseas_order", fake_place_overseas_order)

    def submit():
        start.wait()
        try:
            results.append(
                submit_guarded_overseas_order(
                    environment="PROD",
                    account_no="12345678-01",
                    symbol="AAPL",
                    side=OrderSide.BUY,
                    intent=OrderIntent.ENTRY,
                    quantity=1,
                    limit_price=100.0,
                    path=path,
                    pre_trade_risk_decision=_risk_approval(
                        1,
                        environment="PROD",
                        account_no="12345678-01",
                    ),
                    **_risk_metadata(),
                )
            )
        except DuplicateOpenOrderError as exc:
            results.append(exc)

    threads = [threading.Thread(target=submit), threading.Thread(target=submit)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)

    assert all(not thread.is_alive() for thread in threads)
    assert len(submitted) == 1
    assert len(load_orders(path)) == 1
    assert sum(isinstance(result, DuplicateOpenOrderError) for result in results) == 1


def test_order_ledger_lock_retries_windows_permission_collision(monkeypatch, tmp_path):
    # order_ledger._exclusive_ledger_lock is a re-export of
    # src.utils.file_lock.exclusive_file_lock (shared with
    # src.services.capital_allocator) -- the retry loop itself, and the
    # os/time names it actually calls, now live in that module.
    from src.services import order_ledger
    from src.utils import file_lock

    path = tmp_path / "orders.json"
    lock_path = path.with_suffix(".json.lock")
    original_open = file_lock.os.open
    calls = {"count": 0}

    def windows_open(candidate, flags, mode=0o777):
        calls["count"] += 1
        if calls["count"] == 1:
            lock_path.write_text("other owner", encoding="utf-8")
            raise PermissionError(13, "permission denied", str(candidate))
        return original_open(candidate, flags, mode)

    def release_other_owner(_seconds):
        lock_path.unlink()

    monkeypatch.setattr(file_lock.os, "open", windows_open)
    monkeypatch.setattr(file_lock.time, "sleep", release_other_owner)

    with order_ledger._exclusive_ledger_lock(path):
        assert lock_path.exists()

    assert calls["count"] == 2
    assert not lock_path.exists()


def test_concurrent_independent_ledger_appends_preserve_both_orders(tmp_path):
    path = tmp_path / "orders.json"
    start = threading.Barrier(2)
    first = _order()
    second = _order(side=OrderSide.SELL, intent=OrderIntent.PARTIAL_EXIT)
    second.client_order_id = f"{second.client_order_id}-second"

    def append_after_barrier(order):
        start.wait()
        append_order(order, path=path)

    threads = [
        threading.Thread(target=append_after_barrier, args=(first,)),
        threading.Thread(target=append_after_barrier, args=(second,)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)

    assert all(not thread.is_alive() for thread in threads)
    assert {order.client_order_id for order in load_orders(path)} == {
        first.client_order_id,
        second.client_order_id,
    }


@pytest.mark.parametrize(
    "error",
    [
        requests.exceptions.Timeout("read timed out"),
        requests.exceptions.ConnectionError("connection reset by peer"),
        RuntimeError("KIS HTTP error from /order: HTTP 502: {}"),
        RuntimeError("KIS HTTP error from /order: HTTP 503: {}"),
        RuntimeError("KIS HTTP error from /order: HTTP 504: {}"),
        RuntimeError("KIS returned non-JSON response from /order. HTTP 200: <html></html>"),
    ],
)
def test_order_submission_classifier_treats_network_and_gateway_errors_as_ambiguous(error):
    assert kis_order.is_ambiguous_order_submission_error(error) is True


@pytest.mark.parametrize(
    "error",
    [
        ValueError("quantity must be positive, got 0"),
        RuntimeError("KIS API error from /order: ABC invalid quantity. Raw={'rt_cd': '1'}"),
        RuntimeError("KIS rejected account"),
        RuntimeError("insufficient funds"),
        RuntimeError("unsupported route/account"),
    ],
)
def test_order_submission_classifier_treats_clear_rejections_as_not_ambiguous(error):
    assert kis_order.is_ambiguous_order_submission_error(error) is False


def test_order_submission_classifier_treats_kill_switch_as_not_submitted():
    from src.services.trading_state import TradingDisabledError

    error = TradingDisabledError(environment="PROD", symbol="AAPL")
    assert kis_order.is_ambiguous_order_submission_error(error) is False


def test_unknown_submission_order_is_open_persistent_and_blocks_duplicate(
    monkeypatch, tmp_path, trading_enabled
):
    path = tmp_path / "orders.json"
    order = _order(status=OrderStatus.UNKNOWN_SUBMISSION_STATE)
    order.error_message = "read timed out"
    append_order(order, path=path)

    loaded = load_order_ledger(path=path)
    assert loaded[0].status == OrderStatus.UNKNOWN_SUBMISSION_STATE
    assert find_open_orders(loaded, environment="SIM", account_no="12345678", symbol="AAPL")
    assert has_open_order("SIM", "12345678", "AAPL", side=OrderSide.BUY, intent=OrderIntent.ENTRY, path=path)

    monkeypatch.setattr(kis_order, "place_overseas_order", lambda **kwargs: pytest.fail("duplicate was not blocked"))
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
            pre_trade_risk_decision=_risk_approval(1),
            **_risk_metadata(),
        )


def test_clear_unknown_submission_order_requires_verification_and_unblocks(tmp_path):
    path = tmp_path / "orders.json"
    order = _order(status=OrderStatus.UNKNOWN_SUBMISSION_STATE)
    append_order(order, path=path)

    with pytest.raises(ValueError):
        clear_unknown_submission_order(order.client_order_id, path=path)

    cleared = clear_unknown_submission_order(order.client_order_id, verified=True, path=path)

    assert cleared is not None
    assert cleared.status == OrderStatus.EXPIRED
    assert not has_open_order("SIM", "12345678", "AAPL", side=OrderSide.BUY, intent=OrderIntent.ENTRY, path=path)


def test_submit_guarded_order_persists_unknown_state_on_ambiguous_error(
    monkeypatch, tmp_path, trading_enabled
):
    path = tmp_path / "orders.json"

    def fake_place_overseas_order(**kwargs):
        raise requests.exceptions.Timeout("read timed out")

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
        pre_trade_risk_decision=_risk_approval(3, reference_price=191.23),
        **_risk_metadata(),
    )

    loaded = load_orders(path=path)
    assert order.status == OrderStatus.UNKNOWN_SUBMISSION_STATE
    assert order.broker_order_id == ""
    assert order.client_order_id
    assert loaded[0].status == OrderStatus.UNKNOWN_SUBMISSION_STATE
    assert loaded[0].client_order_id == order.client_order_id
    assert "timed out" in loaded[0].error_message


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


def test_kis_parse_response_classifies_token_issuance_throttle():
    response = _FakeKisResponse(
        403,
        {
            "error_code": "EGW00133",
            "error_description": "access-token issuance is limited to once per minute",
        },
    )

    with pytest.raises(KisRateLimitError, match="EGW00133"):
        KisAccountClient._parse_response(response, endpoint="/oauth2/tokenP", check_rt_cd=False)


def test_place_overseas_order_refreshes_expired_token_once(
    monkeypatch, trading_enabled
):
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
        environment=KisEnvironment.PROD.value,
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
    assert posts[0]["headers"]["tr_id"] == "TTTT1006U"


def test_reserved_moo_sell_uses_kis_broker_reservation_contract(
    monkeypatch, trading_enabled
):
    posts = []

    class FakeSession:
        def post(self, url, headers, json, timeout):
            posts.append(
                {
                    "url": url,
                    "headers": headers,
                    "json": json,
                    "timeout": timeout,
                }
            )
            return _FakeKisResponse(
                200,
                {
                    "rt_cd": "0",
                    "output": {"OVRS_RSVN_ODNO": "RSV-123"},
                },
            )

    class FakeClient:
        def __init__(self):
            self.session = FakeSession()

        def authenticate(self, force_refresh=False):
            return "token"

        def _headers(self, tr_id, tr_cont=""):
            return {"tr_id": tr_id}

        def _parse_response(self, response, endpoint, check_rt_cd=True):
            return response.json()

    fake_config = SimpleNamespace(
        base_url="https://kis.example",
        cano="12345678",
        account_product_code="01",
    )
    monkeypatch.setattr(kis_order, "load_config", lambda *args, **kwargs: fake_config)
    monkeypatch.setattr(kis_order, "KisAccountClient", lambda _config: FakeClient())

    result = kis_order.place_overseas_reserved_market_on_open_sell(
        environment="PROD",
        account_no="12345678-01",
        symbol="AAPL",
        quantity=4,
        exchange="NASD",
    )

    assert result["output"]["OVRS_RSVN_ODNO"] == "RSV-123"
    assert len(posts) == 1
    assert posts[0]["url"].endswith("/uapi/overseas-stock/v1/trading/order-resv")
    assert posts[0]["headers"]["tr_id"] == "TTTT3016U"
    assert posts[0]["json"] == {
        "CANO": "12345678",
        "ACNT_PRDT_CD": "01",
        "PDNO": "AAPL",
        "OVRS_EXCG_CD": "NASD",
        "FT_ORD_QTY": "4",
        "FT_ORD_UNPR3": "0",
        "ORD_SVR_DVSN_CD": "0",
        "ORD_DVSN": "31",
    }


def test_guarded_reserved_moo_sell_is_durable_and_does_not_call_regular_order(
    monkeypatch, tmp_path, trading_enabled
):
    path = tmp_path / "orders.json"
    submitted = []
    monkeypatch.setattr(
        kis_order,
        "place_overseas_order",
        lambda **kwargs: pytest.fail("reserved sell used the regular order endpoint"),
    )
    monkeypatch.setattr(
        kis_order,
        "place_overseas_reserved_market_on_open_sell",
        lambda **kwargs: submitted.append(kwargs)
        or {"rt_cd": "0", "output": {"OVRS_RSVN_ODNO": "RSV-123"}},
    )

    order = submit_guarded_overseas_order(
        environment="PROD",
        account_no="12345678-01",
        symbol="AAPL",
        side=OrderSide.SELL,
        intent=OrderIntent.PARTIAL_EXIT,
        quantity=4,
        limit_price=191.23,
        execution_policy=RESERVED_MOO_EXECUTION,
        path=path,
    )

    assert order.status == OrderStatus.ACCEPTED
    assert order.execution_policy == RESERVED_MOO_EXECUTION
    assert order.limit_price == 0.0
    assert order.broker_order_id == "RSV-123"
    assert submitted[0]["quantity"] == 4
    [persisted] = load_orders(path=path)
    assert persisted.execution_policy == RESERVED_MOO_EXECUTION
    assert persisted.status == OrderStatus.ACCEPTED


def test_manual_prod_sell_uses_reserved_moo_before_open_and_limit_during_session():
    window = MainWindow.__new__(MainWindow)
    before_open_kst = dt.datetime(
        2026, 7, 27, 17, 0, tzinfo=buylist_constants.KST_ZONE
    )
    during_session_kst = dt.datetime(
        2026, 7, 27, 23, 0, tzinfo=buylist_constants.KST_ZONE
    )

    assert (
        MainWindow._manual_sell_execution_policy(
            window, "PROD", before_open_kst
        )
        == RESERVED_MOO_EXECUTION
    )
    assert (
        MainWindow._manual_sell_execution_policy(
            window, "PROD", during_session_kst
        )
        == REGULAR_LIMIT_EXECUTION
    )
    assert (
        MainWindow._manual_sell_execution_policy(
            window, "SIM", before_open_kst
        )
        == REGULAR_LIMIT_EXECUTION
    )


@pytest.mark.parametrize(
    ("price", "expected"),
    [
        (191.239, "191.23"),
        (0.897688, "0.8976"),
        (0.9, "0.9000"),
    ],
)
def test_overseas_order_price_preserves_valid_tick_precision(price, expected):
    assert kis_order.format_overseas_order_price(price) == expected


@pytest.mark.parametrize(
    ("price", "order_type", "error"),
    [
        (float("nan"), "limit", "positive finite"),
        (191.23, "market", "limit orders only"),
    ],
)
def test_place_overseas_order_rejects_invalid_order_contract_before_auth(
    monkeypatch,
    price,
    order_type,
    error,
):
    monkeypatch.setattr(
        kis_order,
        "load_config",
        lambda *args, **kwargs: pytest.fail("invalid order reached KIS authentication"),
    )

    with pytest.raises(ValueError, match=error):
        kis_order.place_overseas_order(
            environment=KisEnvironment.PROD.value,
            account_no="12345678-01",
            symbol="AAPL",
            quantity=1,
            price=price,
            side="sell",
            order_type=order_type,
        )


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


def test_reconciliation_keeps_unknown_submission_without_fill_evidence():
    order = _order(side=OrderSide.BUY, quantity=10, status=OrderStatus.UNKNOWN_SUBMISSION_STATE)

    [updated] = reconcile_orders_with_snapshot(
        [order],
        snapshot=_snapshot("AAPL", 0),
        previous_snapshot=_snapshot("AAPL", 0),
    )

    assert updated.status == OrderStatus.UNKNOWN_SUBMISSION_STATE
    assert updated.filled_quantity == 0
    assert updated.remaining_quantity == 10


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

    monkeypatch.setattr(buylist_orders_module, "append_order", lambda order: recorded.append(order))
    monkeypatch.setattr(buylist_orders_module, "load_order_ledger", lambda: list(recorded))
    monkeypatch.setattr(buylist_orders_module.QTimer, "singleShot", lambda *_args: None)

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


def test_ambiguous_buy_submission_keeps_queue_and_buylist_blocked(monkeypatch):
    logs = []
    recorded = []
    save_calls = []
    item = SimpleNamespace(
        symbol="AAPL",
        environment="SIM",
        _buy_order_pending=True,
        monitoring_status="ORDER_PENDING",
        status="ORDER_PENDING",
        breakout_method="execution_queue:1m",
        shares_held=0,
        avg_cost=0.0,
        buy_date="",
        position_percent=0.0,
        kis_order_id="",
    )

    class Manager:
        def __init__(self):
            self.queue_item = SimpleNamespace(status="ORDER_PENDING")
            self.mark_calls = []

        def get_item(self, symbol, environment="SIM"):
            assert symbol == "AAPL"
            assert environment == "SIM"
            return self.queue_item

        def mark_order_submitted(self, symbol, order_id="", order_status="SUBMITTED", environment="SIM"):
            self.mark_calls.append((symbol, order_id, order_status, environment))
            self.queue_item.status = order_status

    manager = Manager()
    window = MainWindow.__new__(MainWindow)
    window.order_ledger = []
    window.buylist_manager = SimpleNamespace()
    window.execution_queue_manager = manager
    window._save_state = lambda: save_calls.append("state")
    window._save_execution_queue_state = lambda: save_calls.append("queue")
    window.populate_buylist_dashboard = lambda: None
    window.append_log = logs.append
    window.reconcile_open_orders = lambda: None

    monkeypatch.setattr(buylist_orders_module, "append_order", lambda order: recorded.append(order))
    monkeypatch.setattr(buylist_orders_module, "load_order_ledger", lambda: list(recorded))
    monkeypatch.setattr(buylist_orders_module.QTimer, "singleShot", lambda *_args: None)
    monkeypatch.setattr(buylist_orders_module.QMessageBox, "warning", lambda *args, **kwargs: None)

    order = _order(side=OrderSide.BUY, quantity=5, status=OrderStatus.UNKNOWN_SUBMISSION_STATE)
    order.error_message = "read timed out"

    MainWindow._on_buy_order_accepted(window, item, order)

    assert item.monitoring_status == "UNKNOWN_SUBMISSION_STATE"
    assert item.status == "UNKNOWN_SUBMISSION_STATE"
    assert item._buy_order_pending is True
    assert item.kis_order_id == order.client_order_id
    assert manager.mark_calls == [("AAPL", order.client_order_id, "UNKNOWN_SUBMISSION_STATE", "SIM")]
    assert recorded == [order]
    assert "UNKNOWN" in logs[-1]
    assert "Reconcile KIS account/orders before retry" in logs[-1]
    assert save_calls == ["state", "queue", "queue"]


def test_ambiguous_buy_order_error_keeps_duplicate_protection_active(monkeypatch):
    logs = []
    save_calls = []
    item = SimpleNamespace(
        symbol="AAPL",
        environment="SIM",
        _buy_order_pending=True,
        _stop_order_pending=False,
        monitoring_status="ORDER_PENDING",
        status="ORDER_PENDING",
        breakout_method="execution_queue:1m",
    )

    class Manager:
        def __init__(self):
            self.queue_item = SimpleNamespace(status="ORDER_PENDING")
            self.mark_calls = []

        def get_item(self, symbol, environment="SIM"):
            return self.queue_item

        def mark_order_submitted(self, symbol, order_id="", order_status="SUBMITTED", environment="SIM"):
            self.mark_calls.append((symbol, order_id, order_status, environment))
            self.queue_item.status = order_status

    manager = Manager()
    window = MainWindow.__new__(MainWindow)
    window.execution_queue_manager = manager
    window._save_state = lambda: save_calls.append("state")
    window._save_execution_queue_state = lambda: save_calls.append("queue")
    window.populate_buylist_dashboard = lambda: None
    window.append_log = logs.append

    monkeypatch.setattr(buylist_orders_module.QMessageBox, "warning", lambda *args, **kwargs: None)

    MainWindow._on_order_error(window, "AAPL", "buy", "read timed out", item)

    assert item.monitoring_status == "UNKNOWN_SUBMISSION_STATE"
    assert item.status == "UNKNOWN_SUBMISSION_STATE"
    assert item._buy_order_pending is True
    assert manager.mark_calls == [("AAPL", "", "UNKNOWN_SUBMISSION_STATE", "SIM")]
    assert any("submission result UNKNOWN" in message for message in logs)
    assert save_calls == ["queue", "state", "queue"]


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

    monkeypatch.setattr(buylist_orders_module, "append_order", lambda order: recorded.append(order))
    monkeypatch.setattr(buylist_orders_module, "load_order_ledger", lambda: list(recorded))
    monkeypatch.setattr(buylist_orders_module.QTimer, "singleShot", lambda *_args: None)

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


def test_reserved_partial_sell_acceptance_shows_reserved_until_fill(monkeypatch):
    logs = []
    item = SimpleNamespace(
        symbol="AAPL",
        environment="PROD",
        _stop_order_pending=False,
        _exit_order_pending=False,
        monitoring_status="BOUGHT",
        shares_held=10,
        avg_cost=100.0,
        stop_loss=90.0,
        sell_half_done=False,
        kis_order_id="",
    )
    window = MainWindow.__new__(MainWindow)
    window.order_ledger = []
    window._save_state = lambda: None
    window.populate_buylist_dashboard = lambda: None
    window.append_log = logs.append
    window._clear_buylist_auto_order_block = lambda _item: None
    monkeypatch.setattr(buylist_orders_module, "append_order", lambda _order: None)
    monkeypatch.setattr(buylist_orders_module, "load_order_ledger", lambda: [])
    monkeypatch.setattr(buylist_orders_module.QTimer, "singleShot", lambda *_args: None)

    order = BrokerOrder.create(
        environment="PROD",
        account_no="12345678-01",
        symbol="AAPL",
        side=OrderSide.SELL,
        intent=OrderIntent.PARTIAL_EXIT,
        quantity_requested=4,
        limit_price=0.0,
        status=OrderStatus.ACCEPTED,
        execution_policy=RESERVED_MOO_EXECUTION,
    )
    order.broker_order_id = "RSV-1"

    MainWindow._on_sell_order_accepted(window, item, 4, "partial sell", order)

    assert item.monitoring_status == "PARTIAL_EXIT_RESERVED"
    assert item.shares_held == 10
    assert item.sell_half_done is False
    assert item.kis_order_id == "RSV-1"
    assert any("next U.S. regular open" in message for message in logs)


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

    monkeypatch.setattr(buylist_orders_module, "append_order", lambda order: None)
    monkeypatch.setattr(buylist_orders_module, "load_order_ledger", lambda: [])
    monkeypatch.setattr(buylist_orders_module.QMessageBox, "warning", lambda *args, **kwargs: None)

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


def test_production_sell_rejection_does_not_create_sim_retry_block(monkeypatch):
    logs = []
    save_calls = []
    item = SimpleNamespace(
        symbol="AAPL",
        environment="PROD",
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

    monkeypatch.setattr(buylist_orders_module, "append_order", lambda order: None)
    monkeypatch.setattr(buylist_orders_module, "load_order_ledger", lambda: [])
    monkeypatch.setattr(buylist_orders_module.QMessageBox, "warning", lambda *args, **kwargs: None)

    order = _order(
        side=OrderSide.SELL,
        quantity=10,
        intent=OrderIntent.STOP_LOSS,
        status=OrderStatus.REJECTED,
    )
    order.environment = "PROD"
    order.error_message = "broker rejected order"

    MainWindow._on_sell_order_accepted(window, item, 10, "stop-loss", order)

    assert item.monitoring_status == "BOUGHT"
    assert item._stop_order_pending is False
    assert item.auto_order_block_reason == ""
    assert save_calls == [True]
    assert not any("Auto KIS order retries blocked" in message for message in logs)


def test_monitor_skips_blocked_stop_loss_auto_order():
    logs = []
    submitted = []
    item = SimpleNamespace(
        symbol="AAPL",
        environment="PROD",
        monitoring_status="BOUGHT",
        shares_held=10,
        avg_cost=100.0,
        stop_loss=95.0,
        sell_half_done=False,
        entry_price=100.0,
        auto_order_block_reason="Manual review required before retry.",
    )
    window = MainWindow.__new__(MainWindow)
    window.buylist_manager = SimpleNamespace(items=[item])
    window.latest_intraday_prices = {"AAPL": 90.0}
    window._buylist_refresh_item_data = lambda _item: None
    window._populate_buylist_env_table = lambda _env: None
    window._submit_kis_sell_order = lambda *args, **kwargs: submitted.append(args)
    window.append_log = logs.append

    MainWindow._run_buylist_monitor_cycle(window, "PROD")
    MainWindow._run_buylist_monitor_cycle(window, "PROD")

    assert submitted == []
    assert item.monitoring_status == "BOUGHT"
    assert len([message for message in logs if "auto KIS order is blocked" in message]) == 1


def test_monitor_submits_stop_loss_with_aggressive_limit():
    submitted = []
    item = SimpleNamespace(
        symbol="AAPL",
        environment="SIM",
        monitoring_status="BOUGHT",
        shares_held=10,
        avg_cost=100.0,
        stop_loss=50.0,
        sell_half_done=False,
        entry_price=100.0,
        auto_order_block_reason="",
    )
    window = MainWindow.__new__(MainWindow)
    window.buylist_manager = SimpleNamespace(items=[item])
    window.latest_intraday_prices = {"AAPL": 49.0}
    window._buylist_refresh_item_data = lambda _item: None
    window._populate_buylist_env_table = lambda _env: None
    window._submit_kis_sell_order = lambda *args, **kwargs: submitted.append((args, kwargs))
    window.append_log = lambda _message: None

    MainWindow._run_buylist_monitor_cycle(window, "SIM")

    assert len(submitted) == 1
    assert submitted[0][0] == (item, 10)
    assert submitted[0][1]["reason"] == "stop-loss"
    assert submitted[0][1]["order_price"] == pytest.approx(48.75)


def test_stop_loss_limit_preserves_sub_dollar_precision():
    assert MainWindow._stop_loss_sell_limit_price(0.8799) == pytest.approx(0.8755)


def test_monitor_creates_partial_exit_review_alert_without_auto_sell():
    submitted = []
    logs = []
    item = SimpleNamespace(
        symbol="AAPL",
        environment="SIM",
        monitoring_status="BOUGHT",
        shares_held=9,
        avg_cost=100.0,
        stop_loss=90.0,
        sell_half_done=False,
        entry_price=100.0,
        auto_order_block_reason="",
        buy_date=None,
    )
    window = MainWindow.__new__(MainWindow)
    window.buylist_manager = SimpleNamespace(items=[item])
    window.latest_intraday_prices = {"AAPL": 110.0}
    window._buylist_refresh_item_data = lambda _item: None
    window._buylist_days_held = lambda _item: 3
    window._populate_buylist_env_table = lambda _env: None
    window._submit_kis_sell_order = lambda *args, **kwargs: submitted.append((args, kwargs))
    window._save_state = lambda: None
    window.append_log = logs.append

    MainWindow._run_buylist_monitor_cycle(window, "SIM")

    assert submitted == []
    assert item.shares_held == 9
    assert item.sell_half_done is False
    assert getattr(item, "partial_exit_review_alert") is True
    assert "3-5 trading day partial-exit review window" in item.partial_exit_review_reason
    assert item.suggested_action == "Review manually; no automatic sell submitted."
    assert not getattr(item, "_exit_order_pending", False)
    assert any("no automatic sell submitted" in message for message in logs)


def test_monitor_partial_exit_review_alert_does_not_depend_on_intraday_gain():
    submitted = []
    item = SimpleNamespace(
        symbol="AAPL",
        environment="SIM",
        monitoring_status="BOUGHT",
        shares_held=9,
        avg_cost=100.0,
        stop_loss=90.0,
        sell_half_done=False,
        entry_price=100.0,
        auto_order_block_reason="",
        buy_date=None,
    )
    window = MainWindow.__new__(MainWindow)
    window.buylist_manager = SimpleNamespace(items=[item])
    window.latest_intraday_prices = {"AAPL": 99.0}
    window._buylist_refresh_item_data = lambda _item: None
    window._buylist_days_held = lambda _item: 3
    window._populate_buylist_env_table = lambda _env: None
    window._submit_kis_sell_order = lambda *args, **kwargs: submitted.append((args, kwargs))
    window._save_state = lambda: None
    window.append_log = lambda _message: None

    MainWindow._run_buylist_monitor_cycle(window, "SIM")

    assert submitted == []
    assert getattr(item, "partial_exit_review_alert") is True
    assert item.shares_held == 9
    assert item.sell_half_done is False
    assert not getattr(item, "_exit_order_pending", False)


def test_monitor_creates_ema10_exit_alert_without_auto_sell():
    submitted = []
    logs = []
    item = SimpleNamespace(
        symbol="AAPL",
        environment="SIM",
        monitoring_status="BOUGHT",
        shares_held=6,
        avg_cost=100.0,
        stop_loss=90.0,
        sell_half_done=True,
        entry_price=100.0,
        auto_order_block_reason="",
        buy_date=None,
        _ema10=100.0,
        _ema20=95.0,
        _latest_daily_close=94.0,
    )
    window = MainWindow.__new__(MainWindow)
    window.buylist_manager = SimpleNamespace(items=[item])
    window.latest_intraday_prices = {"AAPL": 110.0}
    window._buylist_refresh_item_data = lambda _item: None
    window._buylist_days_held = lambda _item: 8
    window._populate_buylist_env_table = lambda _env: None
    window._submit_kis_sell_order = lambda *args, **kwargs: submitted.append((args, kwargs))
    window._save_state = lambda: None
    window.append_log = logs.append

    MainWindow._run_buylist_monitor_cycle(window, "SIM")

    assert submitted == []
    assert item.monitoring_status == "BOUGHT"
    assert item.shares_held == 6
    assert getattr(item, "ema_trailing_stop_alert") is True
    assert "Close below 10 EMA" in item.ema_trailing_stop_reason
    assert item.suggested_action == "Review manually; no automatic sell submitted."
    assert not getattr(item, "_exit_order_pending", False)
    assert any("Close below 10 EMA" in message for message in logs)
    assert MainWindow._sell_intent_for_reason("momentum exit below 10 EMA") == OrderIntent.MOMENTUM_EXIT


def test_monitor_creates_ema20_exit_alert_without_auto_sell():
    submitted = []
    item = SimpleNamespace(
        symbol="AAPL",
        environment="SIM",
        monitoring_status="BOUGHT",
        shares_held=6,
        avg_cost=100.0,
        stop_loss=90.0,
        sell_half_done=True,
        entry_price=100.0,
        auto_order_block_reason="",
        buy_date=None,
        _ema10=95.0,
        _ema20=100.0,
        _latest_daily_close=97.0,
    )
    window = MainWindow.__new__(MainWindow)
    window.buylist_manager = SimpleNamespace(items=[item])
    window.latest_intraday_prices = {"AAPL": 110.0}
    window._buylist_refresh_item_data = lambda _item: None
    window._buylist_days_held = lambda _item: 8
    window._populate_buylist_env_table = lambda _env: None
    window._submit_kis_sell_order = lambda *args, **kwargs: submitted.append((args, kwargs))
    window._save_state = lambda: None
    window.append_log = lambda _message: None

    MainWindow._run_buylist_monitor_cycle(window, "SIM")

    assert submitted == []
    assert item.monitoring_status == "BOUGHT"
    assert item.shares_held == 6
    assert getattr(item, "ema_trailing_stop_alert") is True
    assert "Close below 20 EMA" in item.ema_trailing_stop_reason
    assert not getattr(item, "_exit_order_pending", False)


def test_buylist_days_held_uses_us_market_session_date_for_kst_naive_timestamp():
    item = SimpleNamespace(
        buy_date=dt.datetime(2026, 7, 9, 22, 45),
    )
    window = MainWindow.__new__(MainWindow)
    window._us_market_session_date = lambda: dt.date(2026, 7, 9)

    assert MainWindow._buylist_days_held(window, item) == 0

    window._us_market_session_date = lambda: dt.date(2026, 7, 10)

    assert MainWindow._buylist_days_held(window, item) == 1


def test_buylist_days_held_converts_after_midnight_kst_to_previous_us_session():
    item = SimpleNamespace(
        buy_date=dt.datetime(2026, 7, 10, 4, 0),
    )
    window = MainWindow.__new__(MainWindow)
    window._us_market_session_date = lambda: dt.date(2026, 7, 10)

    assert MainWindow._buylist_days_held(window, item) == 1


def test_completed_daily_close_rows_excludes_current_session_before_close():
    rows = [
        (dt.date(2026, 7, 8), 100.0),
        (dt.date(2026, 7, 9), 102.0),
    ]
    before_close = dt.datetime(2026, 7, 9, 15, 59, tzinfo=buylist_constants.US_MARKET_ZONE)
    after_close = dt.datetime(2026, 7, 9, 16, 1, tzinfo=buylist_constants.US_MARKET_ZONE)

    assert MainWindow._completed_daily_close_rows(rows, before_close) == [(dt.date(2026, 7, 8), 100.0)]
    assert MainWindow._completed_daily_close_rows(rows, after_close) == rows


def test_stop_loss_sell_reprice_starts_cancel_when_price_moves_lower(monkeypatch):
    started = []

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

    item = SimpleNamespace(
        symbol="AAPL",
        environment="SIM",
        monitoring_status="SELL_SUBMITTED",
        shares_held=10,
        stop_loss=50.0,
        auto_order_block_reason="",
    )
    order = _order(side=OrderSide.SELL, intent=OrderIntent.STOP_LOSS, status=OrderStatus.ACCEPTED)
    order.broker_order_id = "KIS-STOP"
    order.limit_price = 49.0
    window = MainWindow.__new__(MainWindow)
    window.broker_order_cancel_worker = None
    window._open_broker_orders_for_buylist_item = lambda *args, **kwargs: [order]
    window.append_log = lambda _message: None

    monkeypatch.setattr(buylist_actions_module, "KisOrderCancelWorker", FakeCancelWorker)

    MainWindow._maybe_reprice_stop_loss_sell(window, item, "SIM", 45.0)

    assert item._stop_reprice_pending is True
    assert started == [order.client_order_id]


def test_better_ready_orb_candidate_requires_higher_score_and_respects_manual_lock():
    current = SimpleNamespace(window="1m", score=50.0, valid=True, status="EXECUTE_READY")
    better = SimpleNamespace(window="5m", score=51.0, valid=True, status="EXECUTE_READY")
    queue_item = SimpleNamespace(
        manual_window_lock=False,
        selected_candidate=current,
        selected_window="1m",
        candidates={"1m": current, "5m": better},
    )

    assert MainWindow._better_ready_orb_candidate(queue_item) is better

    better.score = 50.0
    assert MainWindow._better_ready_orb_candidate(queue_item) is None

    better.score = 51.0
    queue_item.manual_window_lock = True
    assert MainWindow._better_ready_orb_candidate(queue_item) is None


def test_market_close_requests_cancel_for_unfilled_entry_buy_before_reset():
    cancel_calls = []
    open_orders = []
    item = SimpleNamespace(
        symbol="AAPL",
        environment="SIM",
        monitoring_status="ORDER_SUBMITTED",
        status="ORDER_SUBMITTED",
        breakout_method="execution_queue:1m",
        orb_monitor_enabled=True,
        _buy_order_pending=True,
        _auto_order_block_notice_logged=True,
        _orb_queue_required_notice_logged=True,
    )
    queue_item = SimpleNamespace(
        locked=True,
        locked_reason="Order submitted",
        manual_window_lock=False,
        candidates={"1m": object()},
        selected_window="1m",
        selected_candidate=object(),
        order_status="ACCEPTED",
        order_id="KIS-BUY",
        warnings=["old"],
        status="ORDER_SUBMITTED",
    )
    order = _order(side=OrderSide.BUY, intent=OrderIntent.ENTRY, status=OrderStatus.ACCEPTED)
    order.broker_order_id = "KIS-BUY"
    open_orders.append(order)

    class Manager:
        def get_item(self, symbol, environment):
            assert (symbol, environment) == ("AAPL", "SIM")
            return queue_item

    def request_cancel(client_order_id):
        cancel_calls.append(client_order_id)
        open_orders.clear()
        item.monitoring_status = "EXECUTE_READY"
        return True

    window = MainWindow.__new__(MainWindow)
    window.buylist_manager = SimpleNamespace(items=[item])
    window.execution_queue_manager = Manager()
    window._open_broker_orders_for_buylist_item = lambda *args, **kwargs: list(open_orders)
    window.request_cancel_order = request_cancel
    window._clear_buylist_auto_order_block = lambda selected: None
    window._save_buylist_state = lambda: None
    window._save_execution_queue_state = lambda: None
    window.populate_buylist_dashboard = lambda: None
    window.append_log = lambda _message: None

    MainWindow._deactivate_pre_entry_orb_monitoring(window)

    assert cancel_calls == [order.client_order_id]
    assert item.monitoring_status == "WATCHING"
    assert item._buy_order_pending is False
    assert queue_item.status.value == "WATCHING"
    assert queue_item.candidates == {}
    assert queue_item.selected_window is None


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


def test_buylist_sell_half_selected_submits_manual_partial_with_allowed_range(monkeypatch):
    submitted = []
    created_spins = []

    class FakeSignal:
        def connect(self, _callback):
            pass

    class FakeDialog:
        Accepted = 1

        def __init__(self, *_args, **_kwargs):
            pass

        def setWindowTitle(self, _title):
            pass

        def setLayout(self, _layout):
            pass

        def accept(self):
            pass

        def reject(self):
            pass

        def exec_(self):
            return self.Accepted

    class FakeLayout:
        def addWidget(self, _widget):
            pass

    class FakeLabel:
        def __init__(self, _text=""):
            self.text = _text

        def setWordWrap(self, _enabled):
            pass

        def setText(self, text):
            self.text = text

    class FakeSlider:
        def __init__(self, *_args, **_kwargs):
            self.valueChanged = FakeSignal()

        def setMinimum(self, value):
            self.minimum = value

        def setMaximum(self, value):
            self.maximum = value

        def setValue(self, value):
            self.value = value

        def setEnabled(self, enabled):
            self.enabled = enabled

    class FakeSpin:
        def __init__(self, *_args, **_kwargs):
            self.valueChanged = FakeSignal()
            created_spins.append(self)

        def setMinimum(self, value):
            self.minimum = value

        def setMaximum(self, value):
            self.maximum = value

        def setValue(self, value):
            self._value = value

        def value(self):
            return self._value

    class FakeButtonBox:
        Ok = 1
        Cancel = 2

        def __init__(self, *_args, **_kwargs):
            self.accepted = FakeSignal()
            self.rejected = FakeSignal()

    item = SimpleNamespace(
        symbol="AAPL",
        monitoring_status="BOUGHT",
        shares_held=10,
    )
    window = MainWindow.__new__(MainWindow)
    window._buylist_selected_item = lambda _env: item
    window._warn_if_open_sell_order = lambda _item, _env: False
    window._submit_kis_sell_order = lambda it, qty, reason: submitted.append((it, qty, reason))

    monkeypatch.setattr(buylist_actions_module, "QDialog", FakeDialog)
    monkeypatch.setattr(buylist_actions_module, "QVBoxLayout", FakeLayout)
    monkeypatch.setattr(buylist_actions_module, "QLabel", FakeLabel)
    monkeypatch.setattr(buylist_actions_module, "QSlider", FakeSlider)
    monkeypatch.setattr(buylist_actions_module, "QSpinBox", FakeSpin)
    monkeypatch.setattr(buylist_actions_module, "QDialogButtonBox", FakeButtonBox)

    MainWindow._buylist_sell_half_selected(window, "SIM")

    assert len(created_spins) == 1
    assert created_spins[0].minimum == 3
    assert created_spins[0].maximum == 5
    assert submitted == [(item, 3, "partial sell")]
    assert MainWindow._sell_intent_for_reason("partial sell") == OrderIntent.PARTIAL_EXIT


def test_buylist_sell_half_selected_requires_bought_position(monkeypatch):
    warnings = []
    submitted = []
    item = SimpleNamespace(
        symbol="AAPL",
        monitoring_status="WATCHING",
        shares_held=0,
    )
    window = MainWindow.__new__(MainWindow)
    window._buylist_selected_item = lambda _env: item
    window._warn_if_open_sell_order = lambda _item, _env: pytest.fail("duplicate check should not run")
    window._submit_kis_sell_order = lambda *args, **kwargs: submitted.append((args, kwargs))

    monkeypatch.setattr(buylist_actions_module.QMessageBox, "warning", lambda *args, **kwargs: warnings.append(args))

    MainWindow._buylist_sell_half_selected(window, "SIM")

    assert submitted == []
    assert warnings


def test_buylist_activate_explicitly_retires_legacy_entry(monkeypatch):
    warnings = []
    logs = []
    item = SimpleNamespace(
        symbol="AAPL",
        monitoring_status="WATCHING",
        breakout_method="manual_pivot_high",
    )
    window = MainWindow.__new__(MainWindow)
    window._buylist_selected_item = lambda _env: item
    window.append_log = logs.append
    window._save_state = lambda: pytest.fail("legacy activation must not save")
    window._toggle_buylist_monitor = lambda _env: pytest.fail(
        "legacy activation must not start monitoring"
    )

    monkeypatch.setattr(
        buylist_actions_module.QMessageBox,
        "warning",
        lambda *args, **kwargs: warnings.append(args),
    )

    MainWindow._buylist_activate_selected(window, "PROD")

    assert item.monitoring_status == "WATCHING"
    assert warnings
    assert "Legacy entry retired" in warnings[0]
    assert "No BUY order was submitted" in logs[0]


def test_buylist_activation_dispatches_buy_today_kanban_command(monkeypatch):
    from src.core.board_workflow import BoardCardProjection
    from src.core.trade_card_state import BoardStatus, TradeCardState
    from src.ui.buyboard.drag_commands import ActivateForToday

    item = SimpleNamespace(
        symbol="AAPL",
        monitoring_status="WAITING_BREAKOUT",
        kis_account_no="",
        orb_monitor_enabled=False,
    )
    window = MainWindow.__new__(MainWindow)
    window._buylist_selected_item = lambda _env: item
    window._is_execution_queue_buylist_item = lambda _item: True
    window._selected_order_account_for_item = lambda _item, _env: "12345678-01"
    window._warn_order_account_unavailable = lambda *a: pytest.fail(
        "configured account should be accepted"
    )
    card = TradeCardState(
        environment="PROD",
        account_no="12345678-01",
        symbol="AAPL",
        board_status=BoardStatus.BUYLIST,
    )
    window._buyboard_current_projections = (BoardCardProjection(card=card),)
    dispatched = []
    window._buyboard_dispatch_command = lambda command, **kwargs: (
        dispatched.append((command, kwargs)) or True
    )
    logs = []
    window.append_log = logs.append
    monkeypatch.setattr(
        buylist_actions_module.QMessageBox, "information", lambda *a, **k: None
    )

    MainWindow._buylist_activate_selected(window, "PROD")

    assert len(dispatched) == 1
    assert isinstance(dispatched[0][0], ActivateForToday)
    assert dispatched[0][0].account_no == "12345678-01"
    assert item.kis_account_no == ""
    assert item.orb_monitor_enabled is False
    assert "Buy Today activation" in logs[0]


def test_buylist_activation_blocks_when_no_account_can_be_persisted():
    item = SimpleNamespace(
        symbol="AAPL",
        monitoring_status="WAITING_BREAKOUT",
        kis_account_no="",
        orb_monitor_enabled=False,
    )
    window = MainWindow.__new__(MainWindow)
    window._buylist_selected_item = lambda _env: item
    window._is_execution_queue_buylist_item = lambda _item: True
    window._selected_order_account_for_item = lambda _item, _env: None
    warnings = []
    window._warn_order_account_unavailable = lambda *a: warnings.append(True)
    window._save_state = lambda: pytest.fail("blocked activation must not save")

    MainWindow._buylist_activate_selected(window, "PROD")

    assert warnings == [True]
    assert item.orb_monitor_enabled is False


def test_buylist_move_to_breakeven_requires_bought_position(monkeypatch):
    warnings = []
    item = SimpleNamespace(
        symbol="AAPL",
        monitoring_status="WATCHING",
        shares_held=0,
        avg_cost=100.0,
        entry_price=95.0,
        stop_loss=90.0,
    )
    window = MainWindow.__new__(MainWindow)
    window._buylist_selected_item = lambda _env: item

    monkeypatch.setattr(buylist_actions_module.QMessageBox, "warning", lambda *args, **kwargs: warnings.append(args))

    MainWindow._buylist_move_to_breakeven_selected(window, "SIM")

    assert item.stop_loss == 90.0
    assert warnings


def test_buylist_move_to_breakeven_uses_avg_cost_and_never_lowers_stop(monkeypatch):
    questions = []
    infos = []
    saves = []
    item = SimpleNamespace(
        symbol="AAPL",
        monitoring_status="BOUGHT",
        shares_held=10,
        avg_cost=100.0,
        entry_price=95.0,
        stop_loss=90.0,
    )
    window = MainWindow.__new__(MainWindow)
    window._buylist_selected_item = lambda _env: item
    window._save_state = lambda: saves.append(True)
    window.populate_buylist_dashboard = lambda: None
    window.append_log = lambda _message: None

    monkeypatch.setattr(
        buylist_actions_module.QMessageBox,
        "question",
        lambda *args, **kwargs: questions.append(args) or buylist_actions_module.QMessageBox.Yes,
    )
    monkeypatch.setattr(buylist_actions_module.QMessageBox, "information", lambda *args, **kwargs: infos.append(args))

    MainWindow._buylist_move_to_breakeven_selected(window, "SIM")

    assert item.stop_loss == compute_breakeven_stop_price(100.0)
    assert saves == [True]
    assert questions

    MainWindow._buylist_move_to_breakeven_selected(window, "SIM")

    assert item.stop_loss == compute_breakeven_stop_price(100.0)
    assert infos


def test_buylist_move_to_breakeven_falls_back_to_entry_price(monkeypatch):
    item = SimpleNamespace(
        symbol="AAPL",
        monitoring_status="BOUGHT",
        shares_held=10,
        avg_cost=0.0,
        entry_price=95.0,
        stop_loss=90.0,
    )
    window = MainWindow.__new__(MainWindow)
    window._buylist_selected_item = lambda _env: item
    window._save_state = lambda: None
    window.populate_buylist_dashboard = lambda: None
    window.append_log = lambda _message: None

    monkeypatch.setattr(
        buylist_actions_module.QMessageBox,
        "question",
        lambda *args, **kwargs: buylist_actions_module.QMessageBox.Yes,
    )

    MainWindow._buylist_move_to_breakeven_selected(window, "SIM")

    assert item.stop_loss == compute_breakeven_stop_price(95.0)


def test_buylist_sell_all_selected_confirms_limit_order_and_submits_full_quantity(monkeypatch):
    questions = []
    submitted = []
    item = SimpleNamespace(
        symbol="AAPL",
        monitoring_status="BOUGHT",
        shares_held=7,
    )
    window = MainWindow.__new__(MainWindow)
    window._buylist_selected_item = lambda _env: item
    window._warn_if_open_sell_order = lambda _item, _env: False
    window._submit_kis_sell_order = lambda it, qty, reason: submitted.append((it, qty, reason))

    monkeypatch.setattr(
        buylist_actions_module.QMessageBox,
        "question",
        lambda *args, **kwargs: questions.append(args) or buylist_actions_module.QMessageBox.Yes,
    )

    MainWindow._buylist_sell_all_selected(window, "SIM")

    assert submitted == [(item, 7, "manual sell all")]
    assert "limit sell order" in questions[0][2]


def test_buylist_sell_all_selected_blocks_when_open_sell_exists(monkeypatch):
    questions = []
    submitted = []
    item = SimpleNamespace(
        symbol="AAPL",
        monitoring_status="BOUGHT",
        shares_held=7,
    )
    window = MainWindow.__new__(MainWindow)
    window._buylist_selected_item = lambda _env: item
    window._warn_if_open_sell_order = lambda _item, _env: True
    window._submit_kis_sell_order = lambda *args, **kwargs: submitted.append((args, kwargs))

    monkeypatch.setattr(buylist_actions_module.QMessageBox, "question", lambda *args, **kwargs: questions.append(args))

    MainWindow._buylist_sell_all_selected(window, "SIM")

    assert submitted == []
    assert questions == []


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
            pre_trade_risk_decision=None,
            strategy_id="",
            plan_id="",
            **_kwargs,
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
            self.pre_trade_risk_decision = pre_trade_risk_decision
            self.strategy_id = strategy_id
            self.plan_id = plan_id
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
    window._has_open_sell_order = lambda *args: False
    window.buylist_manager = SimpleNamespace()
    window.populate_buylist_dashboard = lambda: None

    monkeypatch.setattr(buylist_orders_module, "KisOrderWorker", FakeKisOrderWorker)

    MainWindow._submit_kis_sell_order(window, item, 10, "stop-loss")

    assert len(created_workers) == 1
    worker = created_workers[0]
    assert worker.environment == "SIM"
    assert worker.symbol == "AAPL"
    assert worker.quantity == 10
    assert worker.price == pytest.approx(
        88.5 * (1.0 - execution_config.SELL_MARKETABLE_DISCOUNT_PCT)
    )
    assert worker.side == "sell"
    assert worker.account_no == "12345678"
    assert worker.intent == OrderIntent.STOP_LOSS
    assert worker.buylist_symbol_key == "SIM:AAPL"
    assert worker.started is True
    assert any("SELL submitted for AAPL" in message for message in logs)


def test_submit_manual_prod_sell_before_open_passes_selected_quantity_to_reserved_moo(
    monkeypatch,
):
    created_workers = []
    logs = []

    class FakeSignal:
        def connect(self, _callback):
            pass

    class FakeKisOrderWorker:
        def __init__(self, environment, symbol, quantity, price, side, **kwargs):
            self.environment = environment
            self.symbol = symbol
            self.quantity = quantity
            self.price = price
            self.side = side
            self.execution_policy = kwargs.get("execution_policy")
            self.finished_order = FakeSignal()
            self.error_occurred = FakeSignal()
            self.started = False
            created_workers.append(self)

        def start(self):
            self.started = True

    item = SimpleNamespace(
        symbol="AAPL",
        environment="PROD",
        monitoring_status="BOUGHT",
        shares_held=10,
        avg_cost=100.0,
        stop_loss=90.0,
        entry_price=95.0,
    )
    window = MainWindow.__new__(MainWindow)
    window.append_log = logs.append
    window._first_account_no_for_environment = lambda _environment: "12345678-01"
    window._has_open_sell_order = lambda *args: False
    window._manual_sell_execution_policy = lambda _env: RESERVED_MOO_EXECUTION

    monkeypatch.setattr(buylist_orders_module, "KisOrderWorker", FakeKisOrderWorker)

    MainWindow._submit_kis_sell_order(window, item, 4, "partial sell")

    [worker] = created_workers
    assert worker.environment == "PROD"
    assert worker.quantity == 4
    assert worker.price == 0.0
    assert worker.side == "sell"
    assert worker.execution_policy == RESERVED_MOO_EXECUTION
    assert worker.started is True
    assert any("market-on-open" in message for message in logs)


def test_submit_kis_sell_order_blocks_any_existing_open_sell(monkeypatch):
    logs = []
    created_workers = []

    class FakeKisOrderWorker:
        def __init__(self, *args, **kwargs):
            created_workers.append((args, kwargs))

    item = SimpleNamespace(
        symbol="AAPL",
        environment="SIM",
        _stop_order_pending=True,
        _exit_order_pending=True,
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
    window._has_open_sell_order = lambda environment, account_no, symbol: True

    monkeypatch.setattr(buylist_orders_module, "KisOrderWorker", FakeKisOrderWorker)

    MainWindow._submit_kis_sell_order(window, item, 10, "manual sell all")

    assert created_workers == []
    assert item._stop_order_pending is False
    assert item._exit_order_pending is False
    assert any("Open SELL order already exists" in message for message in logs)


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
            pre_trade_risk_decision=None,
            strategy_id="",
            plan_id="",
            **_kwargs,
        ):
            self.environment = environment
            self.symbol = symbol
            self.quantity = quantity
            self.price = price
            self.side = side
            self.account_no = account_no
            self.intent = intent
            self.buylist_symbol_key = buylist_symbol_key
            self.pre_trade_risk_decision = pre_trade_risk_decision
            self.strategy_id = strategy_id
            self.plan_id = plan_id
            self.finished_order = FakeSignal()
            self.error_occurred = FakeSignal()
            self.started = False
            created_workers.append(self)

        def start(self):
            self.started = True

    item = SimpleNamespace(
        symbol="AAPL",
        environment="PROD",
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

    monkeypatch.setattr(buylist_orders_module, "KisOrderWorker", FakeKisOrderWorker)

    decision = _risk_approval(
        7,
        environment="PROD",
        reference_price=123.45,
    )
    MainWindow._submit_kis_buy_order(
        window,
        item,
        quantity=7,
        order_price=123.45,
        pre_trade_risk_decision=decision,
    )

    assert len(created_workers) == 1
    worker = created_workers[0]
    assert worker.price == 123.45
    assert worker.quantity == 7
    assert worker.side == "buy"
    assert worker.intent == OrderIntent.ENTRY
    assert worker.pre_trade_risk_decision is decision
    assert worker.strategy_id == RISK_STRATEGY_ID
    assert worker.plan_id == RISK_PLAN_ID
    assert worker.started is True
    assert any("BUY submitted for AAPL: 7 shares @ limit $123.45" in message for message in logs)


def test_submit_kis_buy_order_uses_the_account_selected_in_the_ui(monkeypatch):
    created_workers = []

    class FakeSignal:
        def connect(self, _callback):
            pass

    class FakeKisOrderWorker:
        def __init__(self, environment, symbol, quantity, price, side, **kwargs):
            self.environment = environment
            self.symbol = symbol
            self.quantity = quantity
            self.price = price
            self.side = side
            self.account_no = kwargs.get("account_no")
            self.finished_order = FakeSignal()
            self.error_occurred = FakeSignal()
            created_workers.append(self)

        def start(self):
            pass

    class Combo:
        def currentData(self):
            return {
                "environment": "PROD",
                "account_no": "22222222-01",
                "label": "PROD 22******-01",
            }

    item = SimpleNamespace(
        symbol="AAPL",
        environment="PROD",
        kis_account_no="",
        _buy_order_pending=True,
        monitoring_status="ORDER_PENDING",
        breakout_method="execution_queue:1m",
        shares_held=0,
        avg_cost=0.0,
        stop_loss=90.0,
        entry_price=100.0,
        position_percent=10.0,
    )
    window = MainWindow.__new__(MainWindow)
    window.trade_kis_account_combo = Combo()
    window.latest_intraday_prices = {"AAPL": 101.0}
    window.append_log = lambda _message: None
    window._has_duplicate_open_order = lambda *args: False
    window._ensure_execution_queue_manager = lambda: SimpleNamespace(items={})

    monkeypatch.setattr(buylist_orders_module, "KisOrderWorker", FakeKisOrderWorker)

    decision = _risk_approval(
        3,
        environment="PROD",
        account_no="22222222-01",
        reference_price=101.0,
    )
    MainWindow._submit_kis_buy_order(
        window,
        item,
        quantity=3,
        order_price=101.0,
        pre_trade_risk_decision=decision,
    )

    assert len(created_workers) == 1
    assert created_workers[0].account_no == "22222222-01"


def test_low_level_buy_submission_retires_non_queue_entry_before_worker(monkeypatch):
    created_workers = []
    monkeypatch.setattr(
        buylist_orders_module,
        "KisOrderWorker",
        lambda *_args, **_kwargs: created_workers.append(True),
    )
    logs = []
    item = SimpleNamespace(
        symbol="AAPL",
        environment="PROD",
        monitoring_status="ACTIVE",
        breakout_method="",
        _buy_order_pending=True,
    )
    window = MainWindow.__new__(MainWindow)
    window.append_log = logs.append

    MainWindow._submit_kis_buy_order(window, item, quantity=3, order_price=100.0)
    MainWindow._submit_kis_buy_order(window, item, quantity=3, order_price=100.0)

    assert item._buy_order_pending is False
    assert created_workers == []
    assert len(logs) == 1
    assert "only execution-queue strategies" in logs[0]


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

    monkeypatch.setattr(buylist_orders_module, "update_order", lambda order: order)
    monkeypatch.setattr(buylist_orders_module, "load_order_ledger", lambda: [])

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
        environment="PROD",
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
        {("PROD", "50194787-01"): snapshot},
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


def test_buylist_position_sync_converts_filled_queue_status_to_bought():
    save_calls = []
    queue_item = SimpleNamespace(status="FILLED")

    class QueueManager:
        def get_item(self, symbol, environment):
            assert (symbol, environment) == ("STIM", "PROD")
            return queue_item

        def mark_order_filled(self, symbol, order_status, environment):
            assert (symbol, order_status, environment) == (
                "STIM",
                "FILLED",
                "PROD",
            )

    item = SimpleNamespace(
        symbol="STIM",
        environment="PROD",
        kis_account_no="63187258-01",
        monitoring_status="FILLED",
        status="FILLED",
        breakout_method="execution_queue:1m",
        shares_held=791,
        avg_cost=2.88,
        buy_date=None,
        _buy_order_pending=True,
    )
    window = MainWindow.__new__(MainWindow)
    window.buylist_manager = SimpleNamespace(items=[item])
    window.execution_queue_manager = QueueManager()
    window.order_ledger = []
    window.append_log = lambda _message: None
    window._save_state = lambda: save_calls.append(True)
    window.populate_buylist_dashboard = lambda: None

    changed = MainWindow.sync_buylist_positions_from_kis_snapshots(
        window,
        {("PROD", "63187258-01"): _snapshot("STIM", 791, 2.88)},
    )

    assert changed == 1
    assert item.monitoring_status == "BOUGHT"
    assert item.status == "FILLED"
    assert item.shares_held == 791
    assert item.avg_cost == 2.88
    assert save_calls == [True]


def test_buylist_position_sync_clears_a_stale_position_when_kis_confirms_flat():
    logs = []
    save_calls = []
    item = SimpleNamespace(
        symbol="STIM",
        environment="PROD",
        kis_account_no="63187258-01",
        monitoring_status="BOUGHT",
        shares_held=401,
        avg_cost=2.88,
        position_percent=100.0,
        _buy_order_pending=False,
    )
    window = MainWindow.__new__(MainWindow)
    window.buylist_manager = SimpleNamespace(items=[item])
    window.order_ledger = []
    window.append_log = logs.append
    window._save_state = lambda: save_calls.append(True)
    window.populate_buylist_dashboard = lambda: None

    changed = MainWindow.sync_buylist_positions_from_kis_snapshots(
        window,
        {("PROD", "63187258-01"): _snapshot("OTHER", 1, 10.0)},
    )

    assert changed == 1
    assert item.shares_held == 0
    assert item.position_percent == 0.0
    assert item.monitoring_status == "SOLD"
    assert save_calls == [True]
    assert any("KIS confirms STIM is flat" in message for message in logs)


def test_buylist_position_sync_uses_item_account_not_largest_same_symbol_holding():
    item = SimpleNamespace(
        symbol="MRVL",
        environment="PROD",
        kis_account_no="22222222-01",
        monitoring_status="BOUGHT",
        shares_held=0,
        avg_cost=0.0,
        buy_date=None,
        _buy_order_pending=True,
    )
    window = MainWindow.__new__(MainWindow)
    window.buylist_manager = SimpleNamespace(items=[item])
    window.order_ledger = []
    window.append_log = lambda _message: None
    window._save_state = lambda: None
    window.populate_buylist_dashboard = lambda: None

    changed = MainWindow.sync_buylist_positions_from_kis_snapshots(
        window,
        {
            ("PROD", "11111111-01"): _snapshot("MRVL", 100, 111.0),
            ("PROD", "22222222-01"): _snapshot("MRVL", 7, 222.0),
        },
    )

    assert changed == 1
    assert item.shares_held == 7
    assert item.avg_cost == 222.0
    assert item.kis_account_no == "22222222-01"


def test_cancelled_order_with_confirmed_partial_fill_updates_buylist(monkeypatch):
    save_calls = []
    item = SimpleNamespace(
        symbol="AAPL",
        environment="SIM",
        kis_account_no="",
        shares_held=10,
        avg_cost=100.0,
        stop_loss=90.0,
        sell_half_done=False,
        monitoring_status="SELL_SUBMITTED",
        kis_order_id="",
    )

    class Manager:
        def get(self, symbol, environment=None):
            assert (symbol, environment) == ("AAPL", "SIM")
            return item

    order = _order(
        side=OrderSide.SELL,
        quantity=10,
        intent=OrderIntent.PARTIAL_EXIT,
        status=OrderStatus.CANCELLED,
    )
    order.filled_quantity = 4
    order.remaining_quantity = 6

    window = MainWindow.__new__(MainWindow)
    window.buylist_manager = Manager()
    window._save_state = lambda: save_calls.append(True)
    window.populate_buylist_dashboard = lambda: None
    window.append_log = lambda _message: None

    monkeypatch.setattr(buylist_orders_module, "update_order", lambda value: value)
    monkeypatch.setattr(buylist_orders_module, "load_order_ledger", lambda: [])

    MainWindow.apply_confirmed_order_fills_to_buylist(window, [order])

    assert item.shares_held == 6
    assert item.sell_half_done is True
    assert item.kis_account_no == "12345678"
    assert order.applied_filled_quantity == 4
    assert save_calls == [True]


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
        environment="PROD",
        monitoring_status="BOUGHT",
        kis_order_id="",
    )

    class Manager:
        def get(self, symbol, environment=None):
            assert symbol == "AAPL"
            assert environment == "PROD"
            return item

    order = _order(
        side=OrderSide.SELL,
        quantity=5,
        intent=OrderIntent.STOP_LOSS,
        status=OrderStatus.ACCEPTED,
    )
    order.environment = "PROD"
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


def test_startup_unresolved_buy_does_not_demote_confirmed_position():
    save_calls = []
    item = SimpleNamespace(
        symbol="STIM",
        environment="PROD",
        monitoring_status="BOUGHT",
        shares_held=791,
        kis_order_id="",
    )

    class Manager:
        def get(self, symbol, environment=None):
            assert (symbol, environment) == ("STIM", "PROD")
            return item

    order = _order(side=OrderSide.BUY, quantity=791, status=OrderStatus.WORKING)
    order.environment = "PROD"
    order.symbol = "STIM"
    order.broker_order_id = "KIS-STIM"

    window = MainWindow.__new__(MainWindow)
    window.order_ledger = [order]
    window.buylist_manager = Manager()
    window.append_log = lambda _message: None
    window._save_state = lambda: save_calls.append(True)
    window.populate_buylist_dashboard = lambda: None

    MainWindow._apply_unresolved_order_startup_state(window)

    assert item.monitoring_status == "BOUGHT"
    assert item.kis_order_id == "KIS-STIM"
    assert save_calls == [True]
