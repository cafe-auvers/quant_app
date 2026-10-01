"""Trading rules and watchlist management."""

import contextlib
import copy
import logging
import math
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from zoneinfo import ZoneInfo

from src.utils.market_calendar import current_or_next_nyse_session_date

logger = logging.getLogger(__name__)
RejectedRecordHandler = Optional[Callable[[int, Any, Exception], None]]
US_MARKET_ZONE = ZoneInfo("America/New_York")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_timestamp(
    value: Any,
    *,
    default_timezone=timezone.utc,
    normalize_timezone=timezone.utc,
) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=default_timezone)
    return parsed.astimezone(normalize_timezone)


def _report_rejected_record(
    collection: str,
    index: int,
    raw_record: Any,
    error: Exception,
    handler: RejectedRecordHandler,
) -> None:
    identity = (
        raw_record.get("symbol")
        if isinstance(raw_record, dict)
        else f"index {index}"
    )
    logger.warning(
        "Rejected %s record %r at index %d: %s",
        collection,
        identity,
        index,
        error,
    )
    if handler is not None:
        handler(index, raw_record, error)


@dataclass
class WatchlistItem:
    """A stock in a watchlist."""

    symbol: str
    name: str
    entry_price: Optional[float] = None
    target_price: Optional[float] = (
        None  # Deprecated; migrated to breakout_price on load.
    )
    breakout_price: Optional[float] = None
    stop_loss: Optional[float] = None
    notes: str = ""
    added_date: datetime = field(default_factory=_utc_now)
    ai_analysis: Optional[Dict] = None
    # The plan explicitly selected in the Watchlist ORB panel.  The execution
    # queue uses this to retain the user's chosen ORB window/risk/buffer rather
    # than silently replacing it with a newly auto-ranked candidate.
    selected_orb_plan: Optional[Dict[str, Any]] = None

    def __post_init__(self) -> None:
        self.added_date = _parse_timestamp(self.added_date)
        self.selected_orb_plan = _normalize_selected_orb_plan(
            self.selected_orb_plan
        )


def _normalize_selected_orb_plan(value: Any) -> Optional[Dict[str, Any]]:
    """Keep persisted ORB selections small, JSON-safe, and finite.

    Watchlist state is user-editable local JSON, so an invalid optional plan
    should be ignored without rejecting an otherwise valid watchlist item.
    """
    if not isinstance(value, dict):
        return None

    window = value.get("window")
    if not isinstance(window, str) or not window.strip():
        return None

    normalized: Dict[str, Any] = {"window": window.strip()}
    string_keys = {"selected_at"}
    float_keys = {
        "risk_percent",
        "entry_trigger",
        "entry_floor",
        "floor_price",
        "execution_price",
        "stop_price",
        "breakout_price",
        "buffer_pct",
        "capital_percent",
        "stop_adr",
    }
    for key in string_keys:
        raw = value.get(key)
        if isinstance(raw, str) and raw.strip():
            normalized[key] = raw.strip()
    for key in float_keys:
        raw = value.get(key)
        try:
            number = float(raw)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            normalized[key] = number
    if value.get("shares") is not None:
        try:
            shares = int(value.get("shares"))
        except (TypeError, ValueError):
            shares = -1
        if shares >= 0:
            normalized["shares"] = shares
    return normalized


@dataclass
class TradePlan:
    """A trade plan with setup details."""

    symbol: str
    entry_price: float
    stop_loss: float
    take_profit: float
    position_size: int
    reason: str
    entry_date: datetime = field(default_factory=_utc_now)
    status: str = "active"  # active, filled, closed, cancelled
    notes: str = ""
    risk_percent: float = 0.01

    def __post_init__(self) -> None:
        self.entry_date = _parse_timestamp(self.entry_date)


class Watchlist:
    """Session-dated watchlists with one mutable current-session view.

    ``items`` deliberately remains the compatibility surface used by the
    planning and chart workflows.  It always addresses the current NYSE
    session.  Older sessions can only be read through ``items_for_session``.
    """

    SCHEMA_VERSION = 2
    SESSION_DATE_CONVENTION = "nyse_close_to_close"

    def __init__(
        self,
        name: str = "Default",
        *,
        current_session_date: Optional[date] = None,
    ):
        """
        Initialize a watchlist.

        Args:
            name: Watchlist name
        """
        self.name = name
        self._current_session_date_override = current_session_date
        self._items_by_session: Dict[str, List[WatchlistItem]] = {}
        self.created_date = _utc_now()

    @property
    def active_session_date(self) -> date:
        """The editable watchlist date under the NYSE close-to-close rule."""

        return (
            self._current_session_date_override
            or current_or_next_nyse_session_date()
        )

    @staticmethod
    def _session_key(session_date: Any) -> str:
        if isinstance(session_date, datetime):
            resolved = current_or_next_nyse_session_date(session_date)
        elif isinstance(session_date, date):
            resolved = session_date
        else:
            resolved = date.fromisoformat(str(session_date))
        return resolved.isoformat()

    @property
    def items(self) -> List[WatchlistItem]:
        """Return the mutable current-session list used by live workflows."""

        return self._items_by_session.setdefault(
            self.active_session_date.isoformat(), []
        )

    @items.setter
    def items(self, value: List[WatchlistItem]) -> None:
        self._items_by_session[self.active_session_date.isoformat()] = list(value)

    @property
    def session_dates(self) -> tuple[date, ...]:
        """Return every persisted watchlist session in chronological order."""

        dates = []
        for value in self._items_by_session:
            with contextlib.suppress(ValueError):
                dates.append(date.fromisoformat(value))
        return tuple(sorted(dates))

    def items_for_session(self, session_date: Any) -> tuple[WatchlistItem, ...]:
        """Return a detached, read-only snapshot for ``session_date``."""

        try:
            key = self._session_key(session_date)
        except (TypeError, ValueError):
            return ()
        return tuple(copy.deepcopy(self._items_by_session.get(key, ())))

    def has_session(self, session_date: Any) -> bool:
        try:
            key = self._session_key(session_date)
        except (TypeError, ValueError):
            return False
        return key in self._items_by_session

    def add(self, symbol: str, name: str, entry_price=...) -> WatchlistItem:
        """Add or update a stock in the watchlist."""
        symbol = symbol.strip().upper()
        existing = self.get(symbol)
        if existing is not None:
            existing.name = name or existing.name
            if entry_price is not ...:
                existing.entry_price = entry_price
            return existing

        item = WatchlistItem(
            symbol=symbol,
            name=name,
            entry_price=None if entry_price is ... else entry_price,
        )
        self.items.append(item)
        return item

    def remove(self, symbol: str) -> bool:
        """Remove a stock from watchlist. Returns True if found."""
        symbol = str(symbol or "").strip().upper()
        if not symbol:
            return False
        original_len = len(self.items)
        self.items = [item for item in self.items if item.symbol != symbol]
        return len(self.items) < original_len

    def get(self, symbol: str) -> Optional[WatchlistItem]:
        """Get a watchlist item by symbol."""
        symbol = str(symbol or "").strip().upper()
        if not symbol:
            return None
        for item in self.items:
            if item.symbol == symbol:
                return item
        return None

    def to_dict(self) -> Dict:
        """Convert to dictionary for serialization."""

        def serialize_item(item: WatchlistItem) -> Dict[str, Any]:
            return {
                "symbol": item.symbol,
                "name": item.name,
                "entry_price": item.entry_price,
                "breakout_price": item.breakout_price,
                "target_price": item.target_price,
                "stop_loss": item.stop_loss,
                "notes": item.notes,
                "added_date": item.added_date.isoformat(),
                "ai_analysis": item.ai_analysis,
                "selected_orb_plan": item.selected_orb_plan,
            }

        return {
            "schema_version": self.SCHEMA_VERSION,
            "session_date_convention": self.SESSION_DATE_CONVENTION,
            "name": self.name,
            "created_date": self.created_date.isoformat(),
            # Kept for older scripts and app builds. It mirrors only the
            # editable current session; schema-v2 readers use ``sessions``.
            "items": [serialize_item(item) for item in self.items],
            "sessions": {
                session_key: {
                    "date": session_key,
                    "items": [serialize_item(item) for item in items],
                }
                for session_key, items in sorted(self._items_by_session.items())
            },
        }

    @classmethod
    def from_dict(
        cls,
        data: Dict,
        *,
        on_rejected: RejectedRecordHandler = None,
        current_session_date: Optional[date] = None,
    ) -> "Watchlist":
        """Create a watchlist from serialized data."""
        if not isinstance(data, dict):
            data = {}
        watchlist = cls(
            name=data.get("name", "Default"),
            current_session_date=current_session_date,
        )
        created_date = data.get("created_date")
        if created_date:
            with contextlib.suppress(ValueError):
                watchlist.created_date = _parse_timestamp(created_date)

        def optional_float(value):
            if value in (None, ""):
                return None
            try:
                return float(value)
            except (TypeError, ValueError):
                return None

        def deserialize_item(
            raw_item: Any,
            *,
            index: int,
            collection: str,
        ) -> Optional[WatchlistItem]:
            try:
                if not isinstance(raw_item, dict):
                    raise TypeError("record is not an object")
                added_date = raw_item.get("added_date")
                try:
                    parsed_added_date = (
                        _parse_timestamp(added_date)
                        if added_date
                        else _utc_now()
                    )
                except ValueError:
                    parsed_added_date = _utc_now()

                migrated_breakout_price = optional_float(raw_item.get("breakout_price"))
                legacy_target_price = optional_float(raw_item.get("target_price"))
                if migrated_breakout_price is None and legacy_target_price is not None:
                    migrated_breakout_price = legacy_target_price

                symbol = str(raw_item.get("symbol", "")).upper()
                if not symbol:
                    raise ValueError("symbol is required")
                return WatchlistItem(
                    symbol=symbol,
                    name=raw_item.get("name", ""),
                    entry_price=optional_float(raw_item.get("entry_price")),
                    stop_loss=optional_float(raw_item.get("stop_loss")),
                    target_price=legacy_target_price,
                    breakout_price=migrated_breakout_price,
                    notes=raw_item.get("notes", ""),
                    added_date=parsed_added_date,
                    ai_analysis=raw_item.get("ai_analysis"),
                    selected_orb_plan=_normalize_selected_orb_plan(
                        raw_item.get("selected_orb_plan")
                    ),
                )
            except Exception as exc:
                _report_rejected_record(
                    collection, index, raw_item, exc, on_rejected
                )
                return None

        sessions = data.get("sessions")
        if isinstance(sessions, dict):
            record_index = 0
            for raw_session_key, raw_session in sessions.items():
                try:
                    session_key = cls._session_key(raw_session_key)
                except (TypeError, ValueError) as exc:
                    _report_rejected_record(
                        "watchlist session",
                        record_index,
                        {"date": raw_session_key},
                        exc,
                        on_rejected,
                    )
                    record_index += 1
                    continue
                raw_items = (
                    raw_session.get("items", [])
                    if isinstance(raw_session, dict)
                    else raw_session
                )
                if not isinstance(raw_items, list):
                    _report_rejected_record(
                        "watchlist session",
                        record_index,
                        raw_session,
                        TypeError("items is not a list"),
                        on_rejected,
                    )
                    record_index += 1
                    continue
                restored_items = watchlist._items_by_session.setdefault(
                    session_key, []
                )
                for raw_item in raw_items:
                    item = deserialize_item(
                        raw_item,
                        index=record_index,
                        collection="watchlist",
                    )
                    record_index += 1
                    if item is not None and not any(
                        existing.symbol == item.symbol for existing in restored_items
                    ):
                        restored_items.append(item)
        else:
            # Schema v1 was one ever-growing list. Partition it by the NYSE
            # session in which each item was added so no historical symbol is
            # lost while today's working list starts cleanly.
            raw_items = data.get("items", [])
            if not isinstance(raw_items, list):
                raw_items = []
            for index, raw_item in enumerate(raw_items):
                item = deserialize_item(
                    raw_item,
                    index=index,
                    collection="watchlist",
                )
                if item is None:
                    continue
                session_key = current_or_next_nyse_session_date(
                    item.added_date
                ).isoformat()
                restored_items = watchlist._items_by_session.setdefault(
                    session_key, []
                )
                if not any(
                    existing.symbol == item.symbol for existing in restored_items
                ):
                    restored_items.append(item)
        return watchlist


class TradePlanManager:
    """Trade plan manager."""

    def __init__(self):
        """Initialize trade plan manager."""
        self.plans: List[TradePlan] = []

    def add_plan(self, plan: TradePlan) -> None:
        """Add a new trade plan."""
        self.plans.append(plan)

    def get_active_plans(self) -> List[TradePlan]:
        """Get all active trade plans."""
        return [plan for plan in self.plans if plan.status == "active"]

    def update_plan_status(self, symbol: str, status: str) -> bool:
        """Update the status of a trade plan. Returns True if found."""
        for plan in self.plans:
            if plan.symbol == symbol:
                plan.status = status
                return True
        return False

    def to_dict(self) -> Dict:
        """Convert to dictionary for serialization."""
        return {
            "plans": [
                {
                    "symbol": plan.symbol,
                    "entry_price": plan.entry_price,
                    "stop_loss": plan.stop_loss,
                    "take_profit": plan.take_profit,
                    "position_size": plan.position_size,
                    "reason": plan.reason,
                    "entry_date": plan.entry_date.isoformat(),
                    "status": plan.status,
                    "notes": plan.notes,
                    "risk_percent": getattr(plan, "risk_percent", 0.01),
                }
                for plan in self.plans
            ]
        }

    @classmethod
    def from_dict(
        cls,
        data: Dict,
        *,
        on_rejected: RejectedRecordHandler = None,
    ) -> "TradePlanManager":
        """Create a trade plan manager from serialized data."""
        manager = cls()
        for index, raw_plan in enumerate(data.get("plans", [])):
            try:
                if not isinstance(raw_plan, dict):
                    raise TypeError("record is not an object")
                entry_date = raw_plan.get("entry_date")
                try:
                    parsed_entry_date = (
                        _parse_timestamp(entry_date)
                        if entry_date
                        else _utc_now()
                    )
                except ValueError:
                    parsed_entry_date = _utc_now()
                symbol = str(raw_plan.get("symbol", "")).upper()
                if not symbol:
                    raise ValueError("symbol is required")
                manager.plans.append(
                    TradePlan(
                        symbol=symbol,
                        entry_price=float(raw_plan.get("entry_price", 0.0)),
                        stop_loss=float(raw_plan.get("stop_loss", 0.0)),
                        take_profit=float(raw_plan.get("take_profit", 0.0)),
                        position_size=int(raw_plan.get("position_size", 0)),
                        reason=raw_plan.get("reason", ""),
                        entry_date=parsed_entry_date,
                        status=raw_plan.get("status", "active"),
                        notes=raw_plan.get("notes", ""),
                        risk_percent=float(raw_plan.get("risk_percent", 0.01)),
                    )
                )
            except Exception as exc:
                _report_rejected_record(
                    "trade plan", index, raw_plan, exc, on_rejected
                )
                continue

        return manager


@dataclass
class BuylistItem:
    """A stock in the buylist."""

    symbol: str
    name: str
    entry_price: float
    target_price: float  # Deprecated; kept for backward-compatible JSON/tests only.
    stop_loss: float
    total_score: float
    status: str
    technical_score: float
    setup_score: float
    risk_score: float
    news_score: float
    timing_score: float
    rr: float
    stop_adr: float
    position_percent: float
    ai_summary: str
    warnings: List[str]
    notes: str = ""
    added_date: datetime = field(default_factory=_utc_now)
    risk_percent: float = 1.0
    trade_plan: str = ""
    monitoring_status: str = "WATCHING"  # WATCHING / ACTIVE / BOUGHT / SOLD
    shares_held: int = 0
    avg_cost: float = 0.0
    buy_date: Optional[datetime] = None
    sell_half_done: bool = False
    kis_order_id: str = ""
    # KIS account that owns this buylist position.  This must travel with the
    # saved item so holdings from another configured account cannot be applied
    # to the same symbol after an account switch or restart.
    kis_account_no: str = ""
    environment: str = "PROD"
    breakout_price: Optional[float] = (
        None  # daily chart structural breakout level (user-entered)
    )
    confirmation_price: Optional[float] = (
        None  # optional full-confirmation level above breakout
    )
    breakout_method: str = ""  # e.g. "manual_trendline", "manual_pivot_high"
    # Legacy planning metadata; PASSIVE_PULLBACK_V1 does not use it to alter
    # the confirmation trigger or execution price.
    buffer_pct: float = 0.001
    auto_order_block_reason: str = ""
    orb_monitor_enabled: bool = (
        False  # user explicitly activated monitoring for this queue item
    )
    partial_exit_review_alert: bool = False
    partial_exit_review_reason: str = ""
    ema_trailing_stop_alert: bool = False
    ema_trailing_stop_reason: str = ""
    suggested_action: str = ""

    def __post_init__(self) -> None:
        self.environment = str(self.environment or "PROD").strip().upper()
        if self.environment != "PROD":
            raise ValueError("Buylist items must use the PROD environment")
        self.kis_account_no = str(self.kis_account_no or "").strip()
        # ``FILLED`` belongs to the execution-queue/order lifecycle, while the
        # Buy Dashboard uses ``BOUGHT`` for an owned position. Repair older
        # persisted rows where the queue status leaked into the dashboard field.
        if (
            str(self.monitoring_status or "").strip().upper() == "FILLED"
            and int(self.shares_held or 0) > 0
        ):
            self.monitoring_status = "BOUGHT"
        self.added_date = _parse_timestamp(self.added_date)
        if self.buy_date is not None:
            self.buy_date = _parse_timestamp(
                self.buy_date,
                default_timezone=US_MARKET_ZONE,
                normalize_timezone=US_MARKET_ZONE,
            )

    def to_dict(self) -> Dict:
        """Convert to dictionary for serialization."""
        return {
            "symbol": self.symbol,
            "name": self.name,
            "entry_price": self.entry_price,
            "target_price": self.target_price,
            "stop_loss": self.stop_loss,
            "total_score": self.total_score,
            "status": self.status,
            "technical_score": self.technical_score,
            "setup_score": self.setup_score,
            "risk_score": self.risk_score,
            "news_score": self.news_score,
            "timing_score": self.timing_score,
            "rr": self.rr,
            "stop_adr": self.stop_adr,
            "position_percent": self.position_percent,
            "ai_summary": self.ai_summary,
            "warnings": self.warnings,
            "notes": self.notes,
            "added_date": self.added_date.isoformat(),
            "risk_percent": self.risk_percent,
            "trade_plan": self.trade_plan,
            "monitoring_status": self.monitoring_status,
            "shares_held": self.shares_held,
            "avg_cost": self.avg_cost,
            "buy_date": self.buy_date.isoformat() if self.buy_date else None,
            "sell_half_done": self.sell_half_done,
            "kis_order_id": self.kis_order_id,
            "kis_account_no": self.kis_account_no,
            "environment": self.environment,
            "breakout_price": self.breakout_price,
            "confirmation_price": self.confirmation_price,
            "breakout_method": self.breakout_method,
            "buffer_pct": self.buffer_pct,
            "auto_order_block_reason": self.auto_order_block_reason,
            "orb_monitor_enabled": self.orb_monitor_enabled,
            "partial_exit_review_alert": self.partial_exit_review_alert,
            "partial_exit_review_reason": self.partial_exit_review_reason,
            "ema_trailing_stop_alert": self.ema_trailing_stop_alert,
            "ema_trailing_stop_reason": self.ema_trailing_stop_reason,
            "suggested_action": self.suggested_action,
        }

    @classmethod
    def from_dict(cls, data: Dict) -> "BuylistItem":
        """Create a BuylistItem from serialized data."""
        environment = str(data.get("environment") or "").strip().upper()
        if environment != "PROD":
            raise ValueError("Ignoring non-PROD legacy buylist item")
        added_date_str = data.get("added_date")
        try:
            added_date = (
                _parse_timestamp(added_date_str)
                if added_date_str
                else _utc_now()
            )
        except ValueError:
            added_date = _utc_now()
        legacy_target_price = float(data.get("target_price", 0.0))
        breakout_price = (
            float(data["breakout_price"])
            if data.get("breakout_price") is not None
            else (legacy_target_price if legacy_target_price > 0 else None)
        )
        return cls(
            symbol=str(data.get("symbol", "")).upper(),
            name=str(data.get("name", "")),
            entry_price=float(data.get("entry_price", 0.0)),
            target_price=legacy_target_price,
            stop_loss=float(data.get("stop_loss", 0.0)),
            total_score=float(data.get("total_score", 0.0)),
            status=str(data.get("status", "WATCHING")),
            technical_score=float(data.get("technical_score", 0.0)),
            setup_score=float(data.get("setup_score", 0.0)),
            risk_score=float(data.get("risk_score", 0.0)),
            news_score=float(data.get("news_score", 0.0)),
            timing_score=float(data.get("timing_score", 0.0)),
            rr=float(data.get("rr", 0.0)),
            stop_adr=float(data.get("stop_adr", 0.0)),
            position_percent=float(data.get("position_percent", 0.0)),
            ai_summary=str(data.get("ai_summary", "")),
            warnings=list(data.get("warnings", [])),
            notes=str(data.get("notes", "")),
            added_date=added_date,
            risk_percent=float(data.get("risk_percent", 1.0)),
            trade_plan=str(data.get("trade_plan", "")),
            environment=environment,
            monitoring_status=str(data.get("monitoring_status", "WATCHING")),
            shares_held=int(data.get("shares_held", 0)),
            avg_cost=float(data.get("avg_cost", 0.0)),
            buy_date=(
                _parse_timestamp(
                    data["buy_date"],
                    default_timezone=US_MARKET_ZONE,
                    normalize_timezone=US_MARKET_ZONE,
                )
                if data.get("buy_date")
                else None
            ),
            sell_half_done=bool(data.get("sell_half_done", False)),
            kis_order_id=str(data.get("kis_order_id", "")),
            kis_account_no=str(data.get("kis_account_no", "")),
            breakout_price=breakout_price,
            confirmation_price=(
                float(data["confirmation_price"])
                if data.get("confirmation_price") is not None
                else None
            ),
            breakout_method=str(data.get("breakout_method", "")),
            buffer_pct=float(data.get("buffer_pct", 0.001)),
            auto_order_block_reason=str(data.get("auto_order_block_reason", "")),
            orb_monitor_enabled=bool(data.get("orb_monitor_enabled", False)),
            partial_exit_review_alert=bool(
                data.get("partial_exit_review_alert", False)
            ),
            partial_exit_review_reason=str(data.get("partial_exit_review_reason", "")),
            ema_trailing_stop_alert=bool(data.get("ema_trailing_stop_alert", False)),
            ema_trailing_stop_reason=str(data.get("ema_trailing_stop_reason", "")),
            suggested_action=str(data.get("suggested_action", "")),
        )


class BuylistManager:
    """Buylist manager."""

    def __init__(self):
        self.items: List[BuylistItem] = []

    def add(self, item: BuylistItem) -> None:
        """Add or update an item in the buylist (keyed by symbol + environment)."""
        self.items = [
            it
            for it in self.items
            if not (it.symbol == item.symbol and it.environment == item.environment)
        ]
        self.items.append(item)

    def remove(self, symbol: str, environment: Optional[str] = None) -> bool:
        """Remove a stock from the buylist. Returns True if found."""
        symbol = symbol.strip().upper()
        original_len = len(self.items)
        if environment:
            self.items = [
                it
                for it in self.items
                if not (it.symbol == symbol and it.environment == environment)
            ]
        else:
            self.items = [it for it in self.items if it.symbol != symbol]
        return len(self.items) < original_len

    def get(
        self, symbol: str, environment: Optional[str] = None
    ) -> Optional["BuylistItem"]:
        """Get a buylist item by symbol (and optionally environment)."""
        symbol = symbol.strip().upper()
        for item in self.items:
            if item.symbol == symbol:
                if environment is None or item.environment == environment:
                    return item
        return None

    def to_dict(self) -> Dict:
        """Convert to dictionary for serialization."""
        return {"items": [item.to_dict() for item in self.items]}

    @classmethod
    def from_dict(
        cls,
        data: Dict,
        *,
        on_rejected: RejectedRecordHandler = None,
    ) -> "BuylistManager":
        """Create a BuylistManager from serialized data."""
        manager = cls()
        for index, item_data in enumerate(data.get("items", [])):
            if (
                isinstance(item_data, dict)
                and str(item_data.get("environment") or "").strip().upper() != "PROD"
            ):
                continue
            try:
                manager.items.append(BuylistItem.from_dict(item_data))
            except Exception as exc:
                _report_rejected_record(
                    "buylist", index, item_data, exc, on_rejected
                )
                continue
        return manager
