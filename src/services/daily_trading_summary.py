"""Durable, trader-facing daily Kanban summary.

The low-level event journal is useful for diagnostics, but it cannot answer
what the published plan contained or why a Buy Today card did not become a
position.  This module records those business events in the shared
coordination store and combines them with durable execution orders.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import threading
import weakref
from dataclasses import dataclass
from typing import Any, Iterable, Optional, Sequence
from zoneinfo import ZoneInfo

from sqlalchemy import (
    Column,
    Date,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    select,
)
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from src.core.execution_order_record import ExecutionOrderStatus
from src.core.exit_policy import market_session_date, market_session_date_from_value
from src.core.order_state import OrderIntent, OrderSide
from src.core.trade_card_state import BoardStatus, EntryRuntimeStatus, TradeCardState
from src.infrastructure.database.coordination_engine import coordination_read_connection

logger = logging.getLogger(__name__)

EVENT_PLAN_PUBLISHED = "PLAN_PUBLISHED"
EVENT_BUY_TODAY_ADDED = "BUY_TODAY_ADDED"
EVENT_CARD_SNAPSHOT = "CARD_SNAPSHOT"
NY_ZONE = ZoneInfo("America/New_York")

_ensured_engines: "weakref.WeakSet[Engine]" = weakref.WeakSet()
_ensure_lock = threading.Lock()


@dataclass(frozen=True)
class DailyPlanItem:
    source: str
    symbol: str
    account_no: str
    breakout_price: Optional[float]
    planned_quantity: int
    outcome: str
    reason_category: str
    reason: str


@dataclass(frozen=True)
class DailyPositionItem:
    symbol: str
    account_no: str
    quantity: int
    average_price: float
    status: str


@dataclass(frozen=True)
class DailyOrderActivity:
    occurred_at: str
    symbol: str
    account_no: str
    activity: str
    quantity: int
    price: float
    status: str
    reason: str = ""


@dataclass(frozen=True)
class DailyRejectedOrbCombination:
    symbol: str
    risk_percent: float
    window: str
    classification: str
    status: str
    orb_high: Optional[float]
    breakout_price: Optional[float]
    breakout_trigger: Optional[float]
    entry_trigger: Optional[float]
    stop_price: Optional[float]
    shares: int
    capital_percent: float
    stop_adr: Optional[float]
    reason: str


@dataclass(frozen=True)
class DailyTradingSummary:
    session_date: dt.date
    published_at: str
    plan_items: tuple[DailyPlanItem, ...]
    positions: tuple[DailyPositionItem, ...]
    activities: tuple[DailyOrderActivity, ...]
    rejected_orb_combinations: tuple[DailyRejectedOrbCombination, ...] = ()
    note: str = ""


def _table(metadata: MetaData) -> Table:
    return Table(
        "daily_trading_events",
        metadata,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("event_key", String(255), nullable=False),
        Column("session_date", Date, nullable=False),
        Column("occurred_at", DateTime, nullable=False),
        Column("event_type", String(40), nullable=False),
        Column("environment", String(10), nullable=False),
        Column("account_no", String(32), nullable=False, server_default=""),
        Column("symbol", String(20), nullable=False, server_default=""),
        Column("payload", Text(length=16_777_215), nullable=False),
        UniqueConstraint("event_key", name="uq_daily_trading_events_key"),
    )


def ensure_daily_trading_events_table(engine: Engine) -> Table:
    metadata = MetaData()
    table = _table(metadata)
    if engine in _ensured_engines:
        return table
    with _ensure_lock:
        if engine not in _ensured_engines:
            metadata.create_all(engine)
            _ensured_engines.add(engine)
    return table


def _utc_naive(value: Optional[dt.datetime] = None) -> dt.datetime:
    value = value or dt.datetime.now(dt.timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc).replace(tzinfo=None)


def _canonical_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _append_event(
    engine: Engine,
    *,
    event_key: str,
    session_date: dt.date,
    occurred_at: Optional[dt.datetime],
    event_type: str,
    environment: str = "PROD",
    account_no: str = "",
    symbol: str = "",
    payload: dict[str, Any],
) -> None:
    table = ensure_daily_trading_events_table(engine)
    statement = table.insert().values(
        event_key=event_key[:255],
        session_date=session_date,
        occurred_at=_utc_naive(occurred_at),
        event_type=event_type,
        environment=str(environment or "PROD").upper(),
        account_no=str(account_no or ""),
        symbol=str(symbol or "").upper(),
        payload=_canonical_json(payload),
    )
    if engine.dialect.name == "mysql":
        statement = statement.prefix_with("IGNORE")
    elif engine.dialect.name == "sqlite":
        statement = statement.prefix_with("OR IGNORE")
    try:
        with engine.begin() as conn:
            conn.execute(statement)
    except IntegrityError:
        # The event key deliberately makes repeated snapshots idempotent.
        return


def _card_plan_payload(card: TradeCardState) -> dict[str, Any]:
    return {
        "environment": card.environment,
        "account_no": card.account_no,
        "symbol": card.symbol,
        "name": card.name,
        "breakout_price": card.breakout_price,
        "planned_quantity": int(card.planned_quantity or card.target_position_quantity or 0),
        "position_percent": float(card.position_percent or 0.0),
        "selected_orb_window": card.selected_orb_window,
    }


def record_plan_published(
    engine: Engine,
    *,
    session_date: dt.date,
    cards: Sequence[TradeCardState],
    revisions: Optional[dict[str, int]] = None,
    occurred_at: Optional[dt.datetime] = None,
) -> None:
    rows = sorted(
        (_card_plan_payload(card) for card in cards),
        key=lambda item: (item["symbol"], item["account_no"]),
    )
    payload = {"cards": rows, "revisions": dict(revisions or {})}
    revision_key = _canonical_json(payload["revisions"]) or _canonical_json(rows)
    _append_event(
        engine,
        event_key=f"plan:{session_date.isoformat()}:{_digest(revision_key)}",
        session_date=session_date,
        occurred_at=occurred_at,
        event_type=EVENT_PLAN_PUBLISHED,
        payload=payload,
    )


def record_plan_published_best_effort(*args, **kwargs) -> None:
    try:
        record_plan_published(*args, **kwargs)
    except Exception:
        logger.exception("Could not record the verified daily plan publication")


def record_buy_today_added(
    engine: Engine,
    card: TradeCardState,
    *,
    command_id: str,
    occurred_at: Optional[dt.datetime] = None,
) -> None:
    session = card.session_date or market_session_date(occurred_at)
    _append_event(
        engine,
        event_key=f"buy-today:{command_id}",
        session_date=session,
        occurred_at=occurred_at,
        event_type=EVENT_BUY_TODAY_ADDED,
        environment=card.environment,
        account_no=card.account_no,
        symbol=card.symbol,
        payload=_card_plan_payload(card),
    )


def record_buy_today_added_best_effort(*args, **kwargs) -> None:
    try:
        record_buy_today_added(*args, **kwargs)
    except Exception:
        logger.exception("Could not record a Buy Today addition")


_SNAPSHOT_FIELDS = (
    "board_status",
    "session_date",
    "last_buy_today_session_date",
    "rejected_orb_snapshot",
    "buy_today_note",
    "breakout_price",
    "selected_orb_window",
    "planned_quantity",
    "target_position_quantity",
    "entry_runtime_status",
    "entry_block_reason",
    "entry_orb_window",
    "entry_orb_high",
    "entry_orb_low",
    "entry_trigger",
    "broker_quantity",
    "orderable_quantity",
    "average_entry_price",
    "position_runtime_status",
    "pending_partial_sell_quantity",
    "reserved_sell_quantity",
    "exit_all_required",
    "sell_all_at_market_open",
    "last_exit_error",
)


def _card_snapshot_payload(card: TradeCardState) -> dict[str, Any]:
    full = card.to_dict()
    payload = {key: full.get(key) for key in _SNAPSHOT_FIELDS}
    payload.update(
        environment=card.environment,
        account_no=card.account_no,
        symbol=card.symbol,
        name=card.name,
    )
    return payload


def record_trade_card_snapshot(
    engine: Engine,
    card: TradeCardState,
    *,
    occurred_at: Optional[dt.datetime] = None,
) -> None:
    payload = _card_snapshot_payload(card)
    if card.board_status in {BoardStatus.BUY_TODAY, BoardStatus.ENTRY_PENDING}:
        session = card.session_date or market_session_date(
            occurred_at or card.updated_at
        )
    elif card.board_status == BoardStatus.BUYLIST and card.buy_today_note:
        session = card.last_buy_today_session_date or market_session_date(
            occurred_at or card.updated_at
        )
    else:
        # Position/exit changes belong to the day they happened, not the
        # original entry session that may still be carried on the card.
        session = market_session_date(occurred_at or card.updated_at)
    signature = _canonical_json(payload)
    _append_event(
        engine,
        event_key=(
            f"card:{session.isoformat()}:{card.environment}:{card.account_no}:"
            f"{card.symbol}:{_digest(signature)}"
        ),
        session_date=session,
        occurred_at=occurred_at or card.updated_at,
        event_type=EVENT_CARD_SNAPSHOT,
        environment=card.environment,
        account_no=card.account_no,
        symbol=card.symbol,
        payload=payload,
    )


def record_trade_card_snapshot_best_effort(*args, **kwargs) -> None:
    try:
        record_trade_card_snapshot(*args, **kwargs)
    except Exception:
        logger.exception("Could not record a daily trade-card snapshot")


def _load_events(engine: Engine, *, through_date: dt.date) -> list[dict[str, Any]]:
    table = ensure_daily_trading_events_table(engine)
    with coordination_read_connection(engine) as conn:
        rows = conn.execute(
            select(table)
            .where(table.c.session_date <= through_date)
            .order_by(table.c.occurred_at.asc(), table.c.id.asc())
        ).fetchall()
    events: list[dict[str, Any]] = []
    for row in rows:
        mapping = row._mapping
        try:
            payload = json.loads(mapping["payload"] or "{}")
        except (TypeError, ValueError):
            payload = {}
        events.append(
            {
                "session_date": mapping["session_date"],
                "occurred_at": mapping["occurred_at"],
                "event_type": mapping["event_type"],
                "environment": mapping["environment"],
                "account_no": mapping["account_no"],
                "symbol": mapping["symbol"],
                "payload": payload,
            }
        )
    return events


def _parse_order_time(order) -> Optional[dt.datetime]:
    for value in (
        order.last_reconciled_at,
        order.last_broker_seen_at,
        order.acknowledged_at,
        order.submission_started_at,
        order.prepared_at,
    ):
        if not value:
            continue
        try:
            parsed = dt.datetime.fromisoformat(str(value))
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed
    return None


def _order_session(order) -> Optional[dt.date]:
    raw_value = order.market_session_date
    value = None
    if isinstance(raw_value, str):
        try:
            # This field is a market-session date, not a timestamp. Parsing
            # YYYY-MM-DD as midnight KST would incorrectly shift it to the
            # previous New York date.
            value = dt.date.fromisoformat(raw_value.strip())
        except ValueError:
            value = market_session_date_from_value(raw_value)
    else:
        value = market_session_date_from_value(raw_value)
    if value is not None:
        return value
    timestamp = _parse_order_time(order)
    return market_session_date(timestamp) if timestamp is not None else None


def _display_time(value: Optional[dt.datetime]) -> str:
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(NY_ZONE).strftime("%H:%M:%S ET")


def _comparable_utc(value: Optional[dt.datetime]) -> dt.datetime:
    if value is None:
        return dt.datetime.min.replace(tzinfo=dt.timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc)


def _reason_category(reason: str) -> str:
    text = str(reason or "").lower()
    if not text:
        return ""
    if "broker" in text and ("reject" in text or "not accepted" in text):
        return "BROKER REJECTED"
    if any(word in text for word in ("data unavailable", "market-data", "websocket", "feed", "system issue", "reconciliation", "stale quote")):
        return "SYSTEM / DATA ISSUE"
    if "orb" in text and any(word in text for word in ("invalid", "did not", "rejected", "before a valid")):
        return "ORB NOT MET"
    if any(word in text for word in ("breakout", "entry trigger", "price cleared", "waiting for price")):
        return "BREAKOUT NOT REACHED"
    if "capital" in text:
        return "CAPITAL BLOCKED"
    if any(word in text for word in ("risk", "stop is too wide", "adr")):
        return "RISK REJECTED"
    if any(word in text for word in ("cancel", "removed")):
        return "CANCELLED"
    return "NO ENTRY"


def _entry_outcome(snapshot: dict[str, Any], entry_orders: Sequence[Any]) -> tuple[str, str, str]:
    filled = sum(max(0, int(order.filled_quantity or 0)) for order in entry_orders)
    if filled:
        return "OPEN POSITION", "", f"Entry filled: {filled} share(s)."
    rejected = next(
        (
            order
            for order in reversed(entry_orders)
            if order.status
            in {
                ExecutionOrderStatus.REJECTED,
                ExecutionOrderStatus.NOT_ACCEPTED_CONFIRMED,
                ExecutionOrderStatus.CANCELLED_LOCALLY,
            }
        ),
        None,
    )
    if rejected is not None:
        reason = "Entry order was rejected or not accepted."
        return "REJECTED", "BROKER REJECTED", reason
    pending = next(
        (
            order
            for order in reversed(entry_orders)
            if order.status
            not in {
                ExecutionOrderStatus.FILLED,
                ExecutionOrderStatus.CANCELLED,
                ExecutionOrderStatus.REJECTED,
                ExecutionOrderStatus.EXPIRED,
                ExecutionOrderStatus.CANCELLED_LOCALLY,
                ExecutionOrderStatus.NOT_ACCEPTED_CONFIRMED,
            }
        ),
        None,
    )
    if pending is not None:
        return "ENTRY ORDER PENDING", "", f"Order status: {pending.status.value}."

    board = str(snapshot.get("board_status") or "")
    runtime = str(snapshot.get("entry_runtime_status") or "")
    reason = str(snapshot.get("buy_today_note") or snapshot.get("entry_block_reason") or "").strip()
    quantity = int(snapshot.get("broker_quantity") or 0)
    if quantity > 0 or board in {
        BoardStatus.OPEN_POSITION.value,
        BoardStatus.PARTIAL_SELL.value,
        BoardStatus.SELL_ALL.value,
    }:
        return "OPEN POSITION", "", reason
    if runtime == EntryRuntimeStatus.WAITING_BREAKOUT.value:
        trigger = snapshot.get("entry_trigger") or snapshot.get("breakout_price")
        reason = reason or (
            f"Price has not cleared the entry trigger ${float(trigger):,.2f}."
            if trigger
            else "Price has not reached the breakout trigger."
        )
        return "WAITING BREAKOUT", "BREAKOUT NOT REACHED", reason
    if runtime == EntryRuntimeStatus.ORB_FORMING.value:
        return "ORB FORMING", "", reason or "Waiting for the ORB window to complete."
    if runtime == EntryRuntimeStatus.RISK_INVALID.value:
        reason = reason or "The risk plan is invalid."
        return "REJECTED", "RISK REJECTED", reason
    if runtime == EntryRuntimeStatus.DATA_UNAVAILABLE.value:
        reason = reason or "Required market data is unavailable."
        return "BLOCKED", "SYSTEM / DATA ISSUE", reason
    if runtime == EntryRuntimeStatus.WAITING_FOR_CAPITAL.value:
        reason = reason or "Capital is unavailable."
        return "BLOCKED", "CAPITAL BLOCKED", reason
    if reason:
        return "NO ENTRY", _reason_category(reason), reason
    if board == BoardStatus.ENTRY_PENDING.value:
        return "ENTRY PENDING", "SYSTEM / DATA ISSUE", "No durable entry order was found."
    if board == BoardStatus.BUY_TODAY.value:
        return "MONITORING", "", "No final outcome has been recorded yet."
    return "NO RECORDED OUTCOME", "", "This day predates the daily summary ledger or no final event was recorded."


def _activity_label(order) -> str:
    if order.side == OrderSide.BUY:
        return "ENTRY"
    return {
        OrderIntent.PARTIAL_EXIT: "PARTIAL SELL",
        OrderIntent.PARTIAL_TAKE_PROFIT: "PARTIAL TAKE PROFIT",
        OrderIntent.STOP_LOSS: "FULL SELL - STOP LOSS",
        OrderIntent.MOMENTUM_EXIT: "FULL SELL - MOMENTUM",
        OrderIntent.MANUAL_EXIT: "FULL SELL",
    }.get(order.intent, "SELL")


def build_daily_trading_summary(
    engine: Engine,
    session_date: dt.date,
    *,
    current_cards: Optional[Sequence[TradeCardState]] = None,
) -> DailyTradingSummary:
    from src.services.execution_order_repository import list_execution_orders
    from src.services.trade_card_repository import list_trade_cards

    events = _load_events(engine, through_date=session_date)
    day_events = [event for event in events if event["session_date"] == session_date]
    orders = list_execution_orders(engine, environment="PROD")
    day_orders = [order for order in orders if _order_session(order) == session_date]

    plan_events = [event for event in day_events if event["event_type"] == EVENT_PLAN_PUBLISHED]
    publication = plan_events[-1] if plan_events else None
    published_at = _display_time(publication["occurred_at"]) if publication else ""
    plan_rows: list[tuple[str, dict[str, Any]]] = []
    published_keys: set[tuple[str, str]] = set()
    publication_time = publication["occurred_at"] if publication else None
    if publication:
        for payload in publication["payload"].get("cards", []):
            key = (str(payload.get("account_no") or ""), str(payload.get("symbol") or "").upper())
            published_keys.add(key)
            plan_rows.append(("PUBLISHED PLAN", payload))

    for event in day_events:
        if event["event_type"] != EVENT_BUY_TODAY_ADDED:
            continue
        if publication_time is not None and event["occurred_at"] <= publication_time:
            continue
        payload = event["payload"]
        key = (str(payload.get("account_no") or ""), str(payload.get("symbol") or "").upper())
        if key in published_keys or any(
            (str(row.get("account_no") or ""), str(row.get("symbol") or "").upper()) == key
            for _, row in plan_rows
        ):
            continue
        plan_rows.append(("ADDED LATER", payload))

    snapshots_for_day: dict[tuple[str, str], dict[str, Any]] = {}
    position_snapshots: dict[
        tuple[str, str], tuple[dict[str, Any], Optional[dt.datetime]]
    ] = {}
    for event in events:
        if event["event_type"] != EVENT_CARD_SNAPSHOT:
            continue
        key = (event["account_no"], event["symbol"])
        position_snapshots[key] = (event["payload"], event["occurred_at"])
        if event["session_date"] == session_date:
            snapshots_for_day[key] = event["payload"]

    if current_cards is None and session_date == market_session_date():
        current_cards = list_trade_cards(engine, environment="PROD", raise_on_error=True)
    current_cards = list(current_cards or [])
    if session_date == market_session_date():
        for card in current_cards:
            key = (card.account_no, card.symbol)
            payload = _card_snapshot_payload(card)
            if card.session_date == session_date or card.last_buy_today_session_date == session_date:
                snapshots_for_day[key] = payload
            if card.broker_quantity > 0 or card.board_status in {
                BoardStatus.OPEN_POSITION,
                BoardStatus.PARTIAL_SELL,
                BoardStatus.SELL_ALL,
            }:
                position_snapshots[key] = (
                    payload,
                    dt.datetime.now(dt.timezone.utc),
                )
        if not plan_rows:
            recovered = [
                card
                for card in current_cards
                if (
                    card.session_date == session_date
                    and card.board_status
                    in {
                        BoardStatus.BUY_TODAY,
                        BoardStatus.ENTRY_PENDING,
                        BoardStatus.OPEN_POSITION,
                    }
                )
                or (
                    card.last_buy_today_session_date == session_date
                    and bool(card.rejected_orb_snapshot)
                )
            ]
            for card in sorted(recovered, key=lambda item: (item.kanban_priority, item.symbol)):
                plan_rows.append(("RECOVERED SNAPSHOT", _card_plan_payload(card)))

    plan_items: list[DailyPlanItem] = []
    rejected_orb_combinations: list[DailyRejectedOrbCombination] = []
    for source, payload in plan_rows:
        account = str(payload.get("account_no") or "")
        symbol = str(payload.get("symbol") or "").upper()
        key = (account, symbol)
        entry_orders = [
            order
            for order in day_orders
            if order.account_no == account
            and order.symbol == symbol
            and order.side == OrderSide.BUY
            and order.intent == OrderIntent.ENTRY
        ]
        outcome, category, reason = _entry_outcome(snapshots_for_day.get(key, {}), entry_orders)
        plan_items.append(
            DailyPlanItem(
                source=source,
                symbol=symbol,
                account_no=account,
                breakout_price=payload.get("breakout_price"),
                planned_quantity=int(payload.get("planned_quantity") or 0),
                outcome=outcome,
                reason_category=category,
                reason=reason,
            )
        )
        rejected_snapshot = dict(
            snapshots_for_day.get(key, {}).get("rejected_orb_snapshot") or {}
        )
        if rejected_snapshot:
            from src.core.execution_queue import OrbCandidateStatus
            from src.core.orb_combinations import (
                orb_position_combinations_from_snapshot,
            )

            for combination in orb_position_combinations_from_snapshot(
                rejected_snapshot
            ):
                if combination.valid:
                    classification = "VALID"
                elif combination.status in {
                    OrbCandidateStatus.FORMING,
                    OrbCandidateStatus.NOT_AVAILABLE,
                }:
                    classification = "FORMING"
                else:
                    classification = "INVALID"
                rejected_orb_combinations.append(
                    DailyRejectedOrbCombination(
                        symbol=symbol,
                        risk_percent=combination.risk_percent,
                        window=combination.window,
                        classification=classification,
                        status=combination.status.value,
                        orb_high=combination.orb_high,
                        breakout_price=combination.breakout_price,
                        breakout_trigger=combination.breakout_trigger,
                        entry_trigger=combination.entry_trigger,
                        stop_price=combination.stop_price,
                        shares=combination.shares,
                        capital_percent=combination.capital_percent,
                        stop_adr=combination.stop_adr,
                        reason=combination.reason,
                    )
                )

    net_fills: dict[tuple[str, str], dict[str, Any]] = {}
    for order in orders:
        order_day = _order_session(order)
        if order_day is None or order_day > session_date or int(order.filled_quantity or 0) <= 0:
            continue
        key = (order.account_no, order.symbol)
        state = net_fills.setdefault(
            key,
            {
                "quantity": 0.0,
                "buy_cost": 0.0,
                "buy_qty": 0.0,
                "last_fill_at": None,
            },
        )
        order_time = _parse_order_time(order)
        if _comparable_utc(order_time) >= _comparable_utc(state["last_fill_at"]):
            state["last_fill_at"] = order_time
        quantity = float(order.filled_quantity or 0)
        if order.side == OrderSide.BUY:
            state["quantity"] += quantity
            state["buy_qty"] += quantity
            state["buy_cost"] += quantity * float(order.average_fill_price or 0.0)
        else:
            state["quantity"] -= quantity

    position_keys = set(net_fills) | set(position_snapshots)
    positions: list[DailyPositionItem] = []
    for key in sorted(position_keys, key=lambda item: (item[1], item[0])):
        account, symbol = key
        fill_state = net_fills.get(key, {})
        quantity = max(0, int(fill_state.get("quantity", 0) or 0))
        average = (
            float(fill_state.get("buy_cost", 0.0)) / float(fill_state.get("buy_qty", 0.0))
            if fill_state.get("buy_qty", 0.0)
            else 0.0
        )
        snapshot, snapshot_time = position_snapshots.get(key, ({}, None))
        snapshot_quantity = int(snapshot.get("broker_quantity") or 0)
        use_snapshot = key not in net_fills or _comparable_utc(
            snapshot_time
        ) >= _comparable_utc(fill_state.get("last_fill_at"))
        if use_snapshot:
            quantity = snapshot_quantity
            average = float(snapshot.get("average_entry_price") or average)
        if quantity <= 0:
            continue
        positions.append(
            DailyPositionItem(
                symbol=symbol,
                account_no=account,
                quantity=quantity,
                average_price=average,
                status=(
                    str(snapshot.get("board_status") or "OPEN_POSITION")
                    if use_snapshot
                    else "OPEN_POSITION"
                ).replace("_", " "),
            )
        )

    activities = tuple(
        DailyOrderActivity(
            occurred_at=_display_time(_parse_order_time(order)),
            symbol=order.symbol,
            account_no=order.account_no,
            activity=_activity_label(order),
            quantity=int(order.filled_quantity or order.submitted_quantity or 0),
            price=float(order.average_fill_price or order.submitted_limit_price or 0.0),
            status=order.status.value.replace("_", " "),
        )
        for order in sorted(day_orders, key=lambda item: _parse_order_time(item) or dt.datetime.min.replace(tzinfo=dt.timezone.utc))
    )

    if publication:
        note = f"Verified plan published at {published_at}."
    elif plan_rows:
        note = "Publication happened before this ledger was installed; current canonical cards are shown as a recovered snapshot."
    elif day_orders:
        note = "No plan publication was logged for this day; durable order activity is still shown."
    else:
        note = "No daily trading events were recorded for this date."
    return DailyTradingSummary(
        session_date=session_date,
        published_at=published_at,
        plan_items=tuple(plan_items),
        positions=tuple(positions),
        activities=activities,
        rejected_orb_combinations=tuple(rejected_orb_combinations),
        note=note,
    )
