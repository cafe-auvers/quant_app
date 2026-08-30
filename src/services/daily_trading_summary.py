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
from dataclasses import asdict, dataclass
from typing import Any, Optional, Sequence

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
from zoneinfo import ZoneInfo

from src.core.execution_order_record import ExecutionOrderStatus
from src.core.exit_policy import market_session_date, market_session_date_from_value
from src.core.order_state import OrderIntent, OrderSide
from src.core.trade_card_state import BoardStatus, EntryRuntimeStatus, TradeCardState
from src.infrastructure.database.coordination_engine import coordination_read_connection

logger = logging.getLogger(__name__)

EVENT_PLAN_PUBLISHED = "PLAN_PUBLISHED"
EVENT_BUY_TODAY_ADDED = "BUY_TODAY_ADDED"
EVENT_CARD_SNAPSHOT = "CARD_SNAPSHOT"
PLAN_ORIGIN_TODAYS_PLAN = "TODAY'S PLAN"
PLAN_ORIGIN_ADDED_INTRADAY = "ADDED INTRADAY"
PLAN_ORIGIN_UNKNOWN = "RECOVERED / UNKNOWN"
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
    origin: str = PLAN_ORIGIN_UNKNOWN
    orb_window: str = ""
    orb_high: Optional[float] = None
    orb_low: Optional[float] = None
    entry_trigger: Optional[float] = None
    stop_adr: Optional[float] = None
    orb_detail_count: int = 0


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
    account_no: str = ""
    source: str = ""
    origin: str = PLAN_ORIGIN_UNKNOWN
    selected: bool = False


@dataclass(frozen=True)
class DailyTradingSummary:
    session_date: dt.date
    published_at: str
    plan_items: tuple[DailyPlanItem, ...]
    positions: tuple[DailyPositionItem, ...]
    activities: tuple[DailyOrderActivity, ...]
    rejected_orb_combinations: tuple[DailyRejectedOrbCombination, ...] = ()
    orb_details: tuple[DailyRejectedOrbCombination, ...] = ()
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
        "risk_percent": float(card.risk_percent or 0.0),
        "buffer_pct": float(card.buffer_pct or 0.0),
        "entry_orb_window": card.entry_orb_window,
        "entry_orb_high": card.entry_orb_high,
        "entry_orb_low": card.entry_orb_low,
        "entry_trigger": card.entry_trigger,
        "stop_adr": card.stop_adr,
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
    "risk_percent",
    "buffer_pct",
    "position_percent",
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


def _publication_origin(
    occurred_at: Optional[dt.datetime], session_date: dt.date
) -> str:
    """Classify a verified publication without promoting live revisions.

    A publication verified by the regular 09:30 New York open is part of the
    day's planned list. A new symbol first published after the open is an
    intraday addition. Existing planned symbols keep their original origin
    when the plan is republished later.
    """

    market_open = dt.datetime.combine(
        session_date,
        dt.time(9, 30),
        tzinfo=NY_ZONE,
    )
    return (
        PLAN_ORIGIN_TODAYS_PLAN
        if _comparable_utc(occurred_at) <= market_open.astimezone(dt.timezone.utc)
        else PLAN_ORIGIN_ADDED_INTRADAY
    )


def _is_orb_rejection(snapshot: dict[str, Any]) -> bool:
    note = str(snapshot.get("buy_today_note") or "").strip().lower()
    return bool(snapshot.get("rejected_orb_snapshot")) or (
        "buy today rejected" in note and "orb" in note
    )


def _card_rejection_session(card: TradeCardState) -> Optional[dt.date]:
    """Recover the session for legacy ORB rejections missing new metadata."""

    if card.last_buy_today_session_date is not None:
        return card.last_buy_today_session_date
    if (
        card.board_status == BoardStatus.BUYLIST
        and card.previous_board_status == BoardStatus.BUY_TODAY
        and _is_orb_rejection(_card_snapshot_payload(card))
    ):
        return market_session_date(card.board_status_updated_at or card.updated_at)
    return None


def _snapshot_is_recoverable_plan(
    snapshot: dict[str, Any], session_date: dt.date
) -> bool:
    """Recover legacy plans after their current-card feedback is cleared.

    Older installations did not always record a publication/addition event.
    Their card snapshots are still durable session history, so the summary
    must not depend on stale feedback remaining on today's canonical card.
    """

    def snapshot_date(value: Any) -> Optional[dt.date]:
        if isinstance(value, str):
            try:
                # These fields are market-session dates. Parsing YYYY-MM-DD
                # as midnight KST would shift them to the prior New York day.
                return dt.date.fromisoformat(value.strip())
            except ValueError:
                pass
        return market_session_date_from_value(value)

    board_status = str(snapshot.get("board_status") or "")
    card_session = snapshot_date(snapshot.get("session_date"))
    feedback_session = snapshot_date(
        snapshot.get("last_buy_today_session_date")
    )
    entry_lifecycle = {
        BoardStatus.BUY_TODAY.value,
        BoardStatus.ENTRY_PENDING.value,
        BoardStatus.OPEN_POSITION.value,
        BoardStatus.PARTIAL_SELL.value,
        BoardStatus.SELL_ALL.value,
        BoardStatus.CLOSED.value,
    }
    return bool(
        (card_session == session_date and board_status in entry_lifecycle)
        or feedback_session == session_date
        or snapshot.get("buy_today_note")
        or _is_orb_rejection(snapshot)
    )


def _queue_items_for_session(
    engine: Engine, session_date: dt.date
) -> dict[tuple[str, str], Any]:
    """Load matching current-session ORB geometry for ledger recovery.

    The queue is mutable, so it is used only when its own timestamp belongs to
    the selected session. Frozen rejected snapshots remain authoritative once
    available.
    """

    try:
        from src.core.execution_queue import ExecutionQueueManager
        from src.services.state_sync import EXECUTION_QUEUE_KEY, PULL_OK, pull_state

        pulled = pull_state(engine, EXECUTION_QUEUE_KEY)
        if pulled.status != PULL_OK or pulled.state is None:
            return {}
        manager = ExecutionQueueManager.from_dict(pulled.state.payload)
    except Exception:
        logger.exception("Could not load ORB queue details for daily summary")
        return {}
    items: dict[tuple[str, str], Any] = {}
    for item in manager.items.values():
        if market_session_date(item.last_updated) != session_date:
            continue
        key = (
            str(item.account_no or ""),
            str(item.symbol or "").strip().upper(),
        )
        items[key] = item
    return items


def _frozen_orb_snapshot(queue_item: Any, *, buffer_pct: float) -> dict[str, Any]:
    from src.core.orb_combinations import build_orb_position_combinations

    combinations = build_orb_position_combinations(
        queue_item,
        account_equity=0.0,
        buffer_pct=buffer_pct,
    )
    serialized = []
    for combination in combinations:
        payload = asdict(combination)
        payload["status"] = combination.status.value
        serialized.append(payload)
    return {
        "buffer_pct": float(buffer_pct),
        "queue_item": queue_item.to_dict(),
        "combinations": serialized,
    }


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
    if _is_orb_rejection(snapshot):
        return (
            "ORB REJECTED",
            "ORB NOT MET",
            reason or "Every supported ORB window was rejected.",
        )
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

    plan_events = [
        event
        for event in day_events
        if event["event_type"] == EVENT_PLAN_PUBLISHED
    ]
    publication = plan_events[-1] if plan_events else None
    published_at = _display_time(publication["occurred_at"]) if publication else ""
    plan_rows: list[tuple[str, str, dict[str, Any]]] = []
    plan_row_indexes: dict[tuple[str, str], int] = {}

    def upsert_plan_row(
        source: str, origin: str, payload: dict[str, Any]
    ) -> None:
        key = (
            str(payload.get("account_no") or ""),
            str(payload.get("symbol") or "").upper(),
        )
        if not key[1]:
            return
        existing = plan_row_indexes.get(key)
        if existing is None:
            plan_row_indexes[key] = len(plan_rows)
            plan_rows.append((source, origin, payload))
            return
        original_source, original_origin, _old_payload = plan_rows[existing]
        priority = {
            PLAN_ORIGIN_UNKNOWN: 0,
            PLAN_ORIGIN_ADDED_INTRADAY: 1,
            PLAN_ORIGIN_TODAYS_PLAN: 2,
        }
        if priority.get(origin, 0) > priority.get(original_origin, 0):
            original_source = source
            original_origin = origin
        elif (
            origin == original_origin == PLAN_ORIGIN_ADDED_INTRADAY
            and source == "ADDED LATER"
        ):
            # An explicit activation is the clearest source when a later
            # live plan revision also happens to contain the same symbol.
            original_source = source
        plan_rows[existing] = (original_source, original_origin, payload)

    # A republish is a revision, not a replacement of history. Preserve the
    # union so an early pre-plan that was ORB-rejected before a later publish
    # remains visible in the day's review.
    for event in day_events:
        if event["event_type"] == EVENT_PLAN_PUBLISHED:
            origin = _publication_origin(event["occurred_at"], session_date)
            for payload in event["payload"].get("cards", []):
                upsert_plan_row("PUBLISHED PLAN", origin, payload)
        elif event["event_type"] == EVENT_BUY_TODAY_ADDED:
            upsert_plan_row(
                "ADDED LATER",
                PLAN_ORIGIN_ADDED_INTRADAY,
                event["payload"],
            )

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

    if current_cards is None:
        current_cards = list_trade_cards(engine, environment="PROD", raise_on_error=True)
    current_cards = list(current_cards or [])
    queue_items = _queue_items_for_session(engine, session_date)
    current_session = market_session_date()
    now = dt.datetime.now(dt.timezone.utc)
    recovered: list[tuple[TradeCardState, str]] = []
    for card in current_cards:
        key = (card.account_no, card.symbol)
        payload = _card_snapshot_payload(card)
        rejection_session = _card_rejection_session(card)
        belongs_to_plan = bool(
            card.session_date == session_date
            or card.last_buy_today_session_date == session_date
            or rejection_session == session_date
        )
        queue_item = queue_items.get(key)
        if (
            belongs_to_plan
            and _is_orb_rejection(payload)
            and not payload.get("rejected_orb_snapshot")
            and queue_item is not None
        ):
            payload["rejected_orb_snapshot"] = _frozen_orb_snapshot(
                queue_item,
                buffer_pct=float(card.buffer_pct or 0.0),
            )
        if belongs_to_plan:
            snapshots_for_day[key] = payload

        if session_date == current_session:
            # Current canonical reconciliation is the authority for today's
            # position list, including explicit zero quantities. This clears
            # stale legacy BUY fills that have no matching sell-order row.
            position_snapshots[key] = (payload, now)
        elif card.board_status == BoardStatus.CLOSED:
            closed_at = card.board_status_updated_at or card.updated_at
            if market_session_date(closed_at) <= session_date:
                position_snapshots[key] = (payload, closed_at)

        if (
            card.session_date == session_date
            and card.board_status
            in {
                BoardStatus.BUY_TODAY,
                BoardStatus.ENTRY_PENDING,
                BoardStatus.OPEN_POSITION,
            }
        ):
            recovered.append((card, "RECOVERED SNAPSHOT"))
        elif rejection_session == session_date:
            recovered.append((card, "RECOVERED ORB REJECTION"))

    for card, source in sorted(
        recovered,
        key=lambda item: (item[0].kanban_priority, item[0].symbol),
    ):
        upsert_plan_row(
            source,
            PLAN_ORIGIN_UNKNOWN,
            _card_plan_payload(card),
        )

    # Legacy plans may have no explicit publication/addition event. Their
    # session snapshots remain the durable history after the canonical card
    # is cleaned for a later session. Rebuild a plan row from that ledger so
    # removing yesterday's Buylist feedback never erases Daily Summary.
    for key, payload in sorted(snapshots_for_day.items()):
        if not _snapshot_is_recoverable_plan(payload, session_date):
            continue
        if (
            _is_orb_rejection(payload)
            and not payload.get("rejected_orb_snapshot")
            and key in queue_items
        ):
            payload = dict(payload)
            payload["rejected_orb_snapshot"] = _frozen_orb_snapshot(
                queue_items[key],
                buffer_pct=float(payload.get("buffer_pct") or 0.0),
            )
            snapshots_for_day[key] = payload
        upsert_plan_row(
            (
                "RECOVERED ORB REJECTION"
                if _is_orb_rejection(payload)
                else "RECOVERED SNAPSHOT"
            ),
            PLAN_ORIGIN_UNKNOWN,
            payload,
        )

    plan_items: list[DailyPlanItem] = []
    rejected_orb_combinations: list[DailyRejectedOrbCombination] = []
    orb_details: list[DailyRejectedOrbCombination] = []
    for source, origin, payload in plan_rows:
        account = str(payload.get("account_no") or "")
        symbol = str(payload.get("symbol") or "").upper()
        key = (account, symbol)
        snapshot = snapshots_for_day.get(key, {})
        entry_orders = [
            order
            for order in day_orders
            if order.account_no == account
            and order.symbol == symbol
            and order.side == OrderSide.BUY
            and order.intent == OrderIntent.ENTRY
        ]
        outcome, category, reason = _entry_outcome(snapshot, entry_orders)
        rejected_snapshot = dict(snapshot.get("rejected_orb_snapshot") or {})
        from src.core.execution_queue import OrbCandidateStatus
        from src.core.orb_combinations import (
            build_orb_position_combinations,
            orb_position_combinations_from_snapshot,
        )

        combinations = []
        if rejected_snapshot:
            combinations = orb_position_combinations_from_snapshot(
                rejected_snapshot
            )
        elif key in queue_items:
            combinations = build_orb_position_combinations(
                queue_items[key],
                account_equity=0.0,
                buffer_pct=float(
                    snapshot.get("buffer_pct")
                    or payload.get("buffer_pct")
                    or 0.0
                ),
            )

        selected_window = str(
            snapshot.get("entry_orb_window")
            or snapshot.get("selected_orb_window")
            or payload.get("entry_orb_window")
            or payload.get("selected_orb_window")
            or ""
        )
        try:
            selected_risk = float(
                snapshot.get("risk_percent")
                or payload.get("risk_percent")
                or 0.0
            )
        except (TypeError, ValueError):
            selected_risk = 0.0
        detail_start = len(orb_details)
        for combination in combinations:
            if combination.valid:
                classification = "VALID"
            elif combination.status in {
                OrbCandidateStatus.FORMING,
                OrbCandidateStatus.NOT_AVAILABLE,
            }:
                classification = "FORMING"
            else:
                classification = "INVALID"
            detail = DailyRejectedOrbCombination(
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
                account_no=account,
                source=source,
                origin=origin,
                selected=bool(
                    selected_window
                    and combination.window == selected_window
                    and selected_risk > 0
                    and abs(combination.risk_percent - selected_risk) < 1e-9
                ),
            )
            orb_details.append(detail)
            if rejected_snapshot:
                rejected_orb_combinations.append(detail)

        if not combinations and selected_window:
            # Historical active cards may predate full queue snapshots. Keep
            # their selected executable geometry inspectable rather than
            # showing a blank ORB-details panel.
            orb_details.append(
                DailyRejectedOrbCombination(
                    symbol=symbol,
                    risk_percent=selected_risk,
                    window=selected_window,
                    classification="SELECTED",
                    status=str(snapshot.get("entry_runtime_status") or "SELECTED"),
                    orb_high=snapshot.get("entry_orb_high"),
                    breakout_price=(
                        snapshot.get("breakout_price")
                        or payload.get("breakout_price")
                    ),
                    breakout_trigger=None,
                    entry_trigger=snapshot.get("entry_trigger"),
                    stop_price=snapshot.get("entry_orb_low"),
                    shares=int(
                        snapshot.get("planned_quantity")
                        or payload.get("planned_quantity")
                        or 0
                    ),
                    capital_percent=float(
                        snapshot.get("position_percent")
                        or payload.get("position_percent")
                        or 0.0
                    ),
                    stop_adr=snapshot.get("stop_adr"),
                    reason=reason,
                    account_no=account,
                    source=source,
                    origin=origin,
                    selected=True,
                )
            )

        detail_count = len(orb_details) - detail_start
        plan_items.append(
            DailyPlanItem(
                source=source,
                symbol=symbol,
                account_no=account,
                breakout_price=(
                    snapshot.get("breakout_price")
                    or payload.get("breakout_price")
                ),
                planned_quantity=int(
                    snapshot.get("planned_quantity")
                    or payload.get("planned_quantity")
                    or 0
                ),
                outcome=outcome,
                reason_category=category,
                reason=reason,
                origin=origin,
                orb_window=(
                    selected_window
                    or ("1m / 5m / 30m" if _is_orb_rejection(snapshot) else "")
                ),
                orb_high=snapshot.get("entry_orb_high"),
                orb_low=snapshot.get("entry_orb_low"),
                entry_trigger=snapshot.get("entry_trigger"),
                stop_adr=snapshot.get("stop_adr"),
                orb_detail_count=detail_count,
            )
        )

    net_fills: dict[tuple[str, str], dict[str, Any]] = {}
    for order in orders:
        # Legacy imported orders without a durable market-session identity are
        # suitable activity evidence but not a complete position ledger. A
        # lone old BUY (STIM was the concrete production example) must not be
        # carried forward forever when an external/manual close is absent.
        if not order.market_session_date:
            continue
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
        note = f"Latest verified plan revision published at {published_at}."
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
        orb_details=tuple(orb_details),
        note=note,
    )
