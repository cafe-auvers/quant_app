from dataclasses import replace

import pytest

from src.core.broker_order_history import authoritative_order_history
from src.core.order_state import BrokerOrderStatusSnapshot, OrderSide, OrderStatus


def _rows():
    original = BrokerOrderStatusSnapshot(
        environment="PROD", account_no="test", symbol="EFOR", broker_order_id="exact",
        side=OrderSide.BUY, status=OrderStatus.PARTIALLY_FILLED, quantity_requested=73,
        raw_response={"rvse_cncl_dvsn":"00", "ord_dt":"20261005", "ord_tmd":"000446"})
    cancel = replace(original, status=OrderStatus.CANCELLED,
                     raw_response={"rvse_cncl_dvsn":"02", "ord_dt":"20261005", "ord_tmd":"045916"})
    return original, cancel


@pytest.mark.parametrize("reverse", [False, True])
def test_exact_zero_fill_cancel_retires_original_independent_of_row_order(reverse):
    original, cancel = _rows()
    rows = [original,cancel]
    assert authoritative_order_history(list(reversed(rows)) if reverse else rows) == [cancel]


@pytest.mark.parametrize("field,value", [
    ("broker_order_id","other"), ("account_no","other"), ("symbol","OTHER"),
    ("side",OrderSide.SELL), ("environment","PAPER"), ("quantity_requested",72),
    ("filled_quantity",1), ("remaining_quantity",1), ("status",OrderStatus.UNKNOWN),
])
def test_cancel_with_mismatched_identity_quantity_or_state_does_not_hide_original(field,value):
    original,cancel = _rows()
    rows = [original,replace(cancel, **{field:value})]
    assert authoritative_order_history(rows) == rows


@pytest.mark.parametrize("proof", [
    {"rvse_cncl_dvsn":"02","ord_dt":"20261004","ord_tmd":"045916"},
    {"rvse_cncl_dvsn":"02","ord_dt":"20261005","ord_tmd":"000445"},
    {"rvse_cncl_dvsn":"02","ord_dt":"20261005","ord_tmd":"295916"},
    {"rvse_cncl_dvsn":"02"}, {},
])
def test_missing_invalid_or_earlier_cancel_time_does_not_hide_original(proof):
    original,cancel = _rows()
    rows = [original,replace(cancel,raw_response=proof)]
    assert authoritative_order_history(rows) == rows


def test_duplicate_cancel_or_contradictory_fill_stays_unresolved():
    original,cancel = _rows()
    for rows in ([original,cancel,cancel], [original,cancel,replace(original,filled_quantity=1)],
                 [original], [replace(original,filled_quantity=1),cancel]):
        assert authoritative_order_history(rows) == rows
