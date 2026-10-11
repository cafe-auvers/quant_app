"""Pure journal projections. Recording a trade never implies strategy success."""
from __future__ import annotations

import datetime as dt
import hashlib
import math
from collections import defaultdict, deque
from typing import Any
from zoneinfo import ZoneInfo

NY_ZONE = ZoneInfo("America/New_York")
PLAN_FIELDS = (
    "breakout_price", "selected_orb_window", "entry_orb_window", "entry_orb_high",
    "entry_orb_low", "entry_trigger", "entry_execution_price", "risk_percent",
    "position_percent", "planned_quantity", "active_stop_price", "stop_adr",
    "entry_orb_score", "entry_order_generation", "entry_range_closed_at",
    "entry_breakout_confirmed_at", "is_ep",
)
NOTE_FIELDS = (
    "thesis", "market_context", "execution_review", "lesson", "emotions", "tags",
)


def parse_time(value: Any) -> dt.datetime | None:
    try:
        result = dt.datetime.fromisoformat(str(value))
    except (ValueError, TypeError):
        return None
    return result.replace(tzinfo=dt.timezone.utc) if result.tzinfo is None else result


def journal_id(order) -> str:
    identity = f"{order.environment}:{order.account_no}:{order.client_order_id}"
    return hashlib.sha256(identity.encode()).hexdigest()[:24]


def fill_facts(order, observed_at: dt.datetime) -> dict[str, Any] | None:
    """Keep confirmed cumulative fills, including partly filled cancelled orders."""
    quantity = int(order.filled_quantity or 0)
    price = float(order.average_fill_price or 0)
    if quantity <= 0 or price <= 0 or not math.isfinite(price):
        return None
    submitted = parse_time(order.submission_started_at or order.prepared_at)
    if order.market_session_date:
        session = dt.date.fromisoformat(str(order.market_session_date))
    elif submitted:
        session = submitted.astimezone(NY_ZONE).date()
    else:
        return None
    broker_seen = parse_time(order.last_broker_seen_at or order.last_reconciled_at)
    return {
        "journal_id": journal_id(order), "symbol": order.symbol,
        "environment": order.environment, "side": order.side.value,
        "intent": order.intent.value, "session_date": session.isoformat(),
        "quantity": quantity, "average_fill_price": price,
        "status": order.status.value, "submitted_at": submitted.isoformat() if submitted else None,
        "fill_observed_at": broker_seen.isoformat() if broker_seen else observed_at.isoformat(),
        "broker_execution_at": None,
        "time_basis": "fill observation; exact broker execution time unavailable",
        "account_key": hashlib.sha256(f"{order.environment}:{order.account_no}".encode()).hexdigest()[:16],
    }


def select_entry_plan(order, events: list[dict]) -> dict[str, Any]:
    """Freeze the last matching order generation, or label a legacy recovery."""
    submitted = parse_time(order.submission_started_at or order.prepared_at)
    linked, prior = [], []
    for event in events:
        occurred = parse_time(event["occurred_at"])
        payloads = event["payload"].get("cards", [event["payload"]])
        for payload in payloads:
            if (payload.get("symbol") != order.symbol
                    or payload.get("account_no") != order.account_no
                    or payload.get("environment") != order.environment):
                continue
            row = (occurred, payload)
            if payload.get("entry_client_order_id") == order.client_order_id:
                linked.append(row)
            elif (order.attempt_group_id and payload.get("entry_attempt_group_id") == order.attempt_group_id
                  and occurred and submitted and occurred <= submitted):
                linked.append(row)
            elif occurred and submitted and occurred <= submitted:
                prior.append(row)
    candidates = linked or prior
    if not candidates:
        return {"provenance": "missing; entry plan not recorded", "fields": {}}
    def key(row):
        return row[0] or dt.datetime.min.replace(tzinfo=dt.timezone.utc)
    before_submit = [row for row in candidates if row[0] and submitted and row[0] <= submitted]
    occurred, payload = max(before_submit, key=key) if before_submit else min(candidates, key=key)
    return {
        "provenance": "order-linked snapshot" if linked else "recovered pre-submission plan; order link unverified",
        "recorded_at": occurred.isoformat() if occurred else None,
        "fields": {key: payload.get(key) for key in PLAN_FIELDS},
    }


def estimated_gross_results(trades: list[dict]) -> dict[str, float | None]:
    """FIFO estimate from cumulative order averages, never an account return."""
    lots = defaultdict(deque)
    results = {}
    for trade in sorted(trades, key=lambda row: (row.get("submitted_at") or row["session_date"], row["journal_id"])):
        key = (trade["environment"], trade["account_key"], trade["symbol"])
        quantity, price = trade["quantity"], trade["average_fill_price"]
        if trade["side"] == "BUY":
            lots[key].append([quantity, price])
            continue
        remaining, cost = quantity, 0.0
        while remaining and lots[key]:
            lot = lots[key][0]
            used = min(remaining, lot[0])
            cost += used * lot[1]
            remaining -= used
            lot[0] -= used
            if not lot[0]:
                lots[key].popleft()
        results[trade["journal_id"]] = None if remaining else round(quantity * price - cost, 4)
    return results
