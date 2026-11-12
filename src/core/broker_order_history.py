"""Resolve KIS originals using exact cancellation or revision evidence."""
from __future__ import annotations

from dataclasses import replace
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


def _accepted_revision(
    original: BrokerOrderStatusSnapshot,
    rows: Sequence[BrokerOrderStatusSnapshot],
) -> BrokerOrderStatusSnapshot | None:
    revisions = [
        row for row in rows
        if _identity(row)[:-1] == _identity(original)[:-1]
        and str(row.raw_response.get("orgn_odno") or "").strip()
        == original.broker_order_id
        and _cancel_code(row) == "01"
    ]
    if len(revisions) != 1:
        return None
    revised = revisions[0]
    if (
        not revised.broker_order_id
        or revised.broker_order_id == original.broker_order_id
        or revised.quantity_requested != original.quantity_requested
        or revised.status not in {
            OrderStatus.ACCEPTED, OrderStatus.WORKING, OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.EXPIRED,
        }
        or min(revised.filled_quantity, revised.remaining_quantity) < 0
        or revised.filled_quantity + revised.remaining_quantity > revised.quantity_requested
        or (revised.status == OrderStatus.FILLED
            and revised.filled_quantity != revised.quantity_requested)
        or (revised.status in {OrderStatus.CANCELLED, OrderStatus.EXPIRED}
            and revised.remaining_quantity != 0)
        or not _cancel_is_later(original, revised)
    ):
        return None
    return revised


def authoritative_order_history(
    rows: Sequence[BrokerOrderStatusSnapshot],
) -> list[BrokerOrderStatusSnapshot]:
    """Keep ambiguous evidence and preserve each revised order's exact identity.

    KIS can label the original zero-fill, zero-open-quantity row PARTIALLY_FILLED
    after cancellation. A unique, later cancellation row for the same full
    identity and quantity resolves that history. An accepted revision can instead
    have a new ID with an explicit original-ID link. Retain a terminal original
    in that case so local/adopted records can reconcile; fills belong to the new
    ID. Neither zero holdings nor a zero remaining quantity alone is proof.
    """
    retired: set[int] = set()
    for index, original in enumerate(rows):
        if not (original.broker_order_id and original.quantity_requested > 0
                and original.status == OrderStatus.PARTIALLY_FILLED
                and original.filled_quantity == original.remaining_quantity == 0
                and _cancel_code(original) in {"00", "01"}):
            continue
        identity = _identity(original)
        same = [row for row in rows if _identity(row) == identity]
        originals = [row for row in same if _cancel_code(row) in {"00", "01"}]
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
    result = []
    for index, original in enumerate(rows):
        if index in retired:
            continue
        if (
            original.broker_order_id
            and original.quantity_requested > 0
            and original.status == OrderStatus.PARTIALLY_FILLED
            and original.filled_quantity == original.remaining_quantity == 0
            and _cancel_code(original) in {"00", "01"}
            and sum(_identity(row) == _identity(original) for row in rows) == 1
        ):
            revision = _accepted_revision(original, rows)
            if revision is not None:
                original = replace(
                    original,
                    status=OrderStatus.CANCELLED,
                    raw_response={
                        **original.raw_response,
                        "broker_history_retirement": "REPLACED",
                        "replacement_order_id": revision.broker_order_id,
                    },
                )
        result.append(original)
    return result
