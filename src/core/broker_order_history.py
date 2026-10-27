"""Resolve KIS original rows only when an exact zero-fill cancel proves retirement."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Sequence

from src.core.order_state import BrokerOrderStatusSnapshot, OrderStatus


def _identity(row: BrokerOrderStatusSnapshot) -> tuple:
    return (row.environment, row.account_no, row.symbol, row.side, row.broker_order_id)


def _broker_time(
    row: BrokerOrderStatusSnapshot, *, domestic: bool = False
) -> datetime | None:
    date_key, time_key = (
        ("dmst_ord_dt", "thco_ord_tmd") if domestic else ("ord_dt", "ord_tmd")
    )
    date = str(row.raw_response.get(date_key) or "")
    time = str(row.raw_response.get(time_key) or "")
    if len(date) != 8 or len(time) != 6 or not (date + time).isdigit():
        return None
    try:
        return datetime.strptime(date + time, "%Y%m%d%H%M%S")
    except ValueError:
        return None


def _cancel_is_later(
    original: BrokerOrderStatusSnapshot, cancel: BrokerOrderStatusSnapshot
) -> bool:
    # ord_dt is the trading date, while Korean clock times can cross midnight.
    # Use the broker's domestic calendar date/time pair when it is supplied.
    session_date = str(original.raw_response.get("ord_dt") or "")
    if session_date != str(cancel.raw_response.get("ord_dt") or ""):
        return False
    try:
        if len(session_date) != 8:
            return False
        datetime.strptime(session_date, "%Y%m%d")
    except ValueError:
        return False
    domestic = any(
        row.raw_response.get(key)
        for row in (original, cancel)
        for key in ("dmst_ord_dt", "thco_ord_tmd")
    )
    original_time = _broker_time(original, domestic=domestic)
    cancel_time = _broker_time(cancel, domestic=domestic)
    return bool(
        original_time is not None
        and cancel_time is not None
        and timedelta(0) <= cancel_time - original_time <= timedelta(days=1)
    )


def _cancel_code(row: BrokerOrderStatusSnapshot) -> str:
    return str(row.raw_response.get("rvse_cncl_dvsn")
               or row.raw_response.get("rvse_cncl_dvsn_cd") or "")


def authoritative_order_history(
    rows: Sequence[BrokerOrderStatusSnapshot],
) -> list[BrokerOrderStatusSnapshot]:
    """Keep conflicting/ambiguous evidence; retire only a proved cancelled original.

    KIS can label the original zero-fill, zero-open-quantity row PARTIALLY_FILLED
    after cancellation. A unique, later cancellation row for the same full
    identity and quantity resolves that history. Neither row alone is enough.
    """
    retired: set[int] = set()
    for index, original in enumerate(rows):
        if not (original.broker_order_id and original.quantity_requested > 0
                and original.status == OrderStatus.PARTIALLY_FILLED
                and original.filled_quantity == original.remaining_quantity == 0
                and _cancel_code(original) == "00"):
            continue
        identity = _identity(original)
        same = [row for row in rows if _identity(row) == identity]
        originals = [row for row in same if _cancel_code(row) == "00"]
        cancellations = [row for row in same if _cancel_code(row) == "02"]
        if len(originals) != 1 or len(cancellations) != 1:
            continue
        cancel = cancellations[0]
        # Any other observation of fills or live quantity keeps the whole
        # identity unresolved rather than hiding a contradictory observation.
        if any(row.filled_quantity or row.remaining_quantity for row in same):
            continue
        if (cancel.status == OrderStatus.CANCELLED
                and cancel.quantity_requested == original.quantity_requested
                and _cancel_is_later(original, cancel)):
            retired.add(index)
    return [row for index, row in enumerate(rows) if index not in retired]
