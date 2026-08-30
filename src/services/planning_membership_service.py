"""Canonical-safe Watchlist and Buylist membership operations.

The dedicated Watchlist tab is gone, but Watchlist remains the passive first
stage of the planning workflow.  These helpers keep its compact JSON model
aligned with canonical TradeCards without constructing an execution-queue
row, selecting an ORB window, publishing Buy Today, or touching the broker.
"""
from __future__ import annotations

import copy
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from src.core.trade_card_state import (
    BoardStatus,
    TradeCardState,
    is_passive_planning_card,
)
from src.core.watchlist import BuylistItem, WatchlistItem
from src.services import trade_card_repository
from src.services.buylist_membership_service import (
    BuylistMembershipSyncResult,
    reconcile_buylist_item,
)


class PlanningMembershipError(RuntimeError):
    """A requested planning-stage membership change was not safely applicable."""


@dataclass(frozen=True)
class PlanningMembershipResult:
    """Outcome returned after canonical and compatibility state agree."""

    action: str
    symbol: str
    card: Optional[TradeCardState] = None
    buylist_item: Optional[BuylistItem] = None
    changed: bool = False


def _symbol(value: object) -> str:
    return str(value or "").strip().upper()


def _account(value: object) -> str:
    return str(value or "").strip()


def _finite_nonnegative(value: object, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return float(default)
    if not math.isfinite(number) or number < 0:
        return float(default)
    return number


def _optional_positive(value: object) -> Optional[float]:
    number = _finite_nonnegative(value)
    return number if number > 0 else None


def _canonical_card_for_selected_account(
    engine: Engine,
    symbol: str,
    account_no: str,
) -> Optional[TradeCardState]:
    rows = trade_card_repository.list_trade_cards_for_symbol(
        engine,
        "PROD",
        symbol,
        raise_on_error=True,
    )
    if len(rows) > 1:
        accounts = ", ".join(sorted({row.account_no for row in rows}))
        raise PlanningMembershipError(
            f"{symbol} has canonical cards in multiple accounts ({accounts}); "
            "resolve the duplicate before changing Watchlist membership"
        )
    if not rows:
        return None
    card = rows[0]
    if card.account_no != account_no:
        raise PlanningMembershipError(
            f"{symbol} belongs to canonical account {card.account_no}; "
            f"select that account instead of {account_no}"
        )
    return card


def _passive_buylist_item(item) -> bool:
    status = str(getattr(item, "monitoring_status", "WATCHING") or "WATCHING").upper()
    protected = {
        "BOUGHT",
        "FILLED",
        "BUY_SUBMITTED",
        "BUY_PARTIAL",
        "SELL_SUBMITTED",
        "PARTIAL_EXIT_SUBMITTED",
        "PARTIAL_EXIT_RESERVED",
        "SELL_RESERVED",
        "SOLD",
    }
    return bool(
        status not in protected
        and int(getattr(item, "shares_held", 0) or 0) <= 0
        and not str(getattr(item, "kis_order_id", "") or "").strip()
        and not bool(getattr(item, "orb_monitor_enabled", False))
    )


def _clear_executable_geometry(card: TradeCardState) -> None:
    card.selected_orb_window = None
    card.position_percent = 0.0
    card.planned_quantity = 0
    card.target_position_quantity = 0
    card.entry_orb_window = None
    card.entry_orb_high = None
    card.entry_orb_low = None
    card.entry_trigger = None
    card.clear_orb_generation_metadata()
    card.stop_adr = None
    card.entry_runtime_status = None
    card.entry_block_reason = ""
    card.session_date = None
    card.next_retry_at = None
    card.entry_attempt_group_id = ""
    card.entry_attempt_count = 0
    card.buy_today_note = ""


def _buylist_from_watchlist(
    item: WatchlistItem,
    *,
    account_no: str,
    buffer_pct: float,
) -> BuylistItem:
    """Map only passive planning metadata; never import a selected ORB plan."""

    resolved_buffer = _finite_nonnegative(buffer_pct, 0.001)
    if resolved_buffer > 1.0:
        resolved_buffer = 0.001
    return BuylistItem(
        symbol=_symbol(item.symbol),
        name=str(item.name or item.symbol),
        entry_price=_finite_nonnegative(item.entry_price),
        target_price=0.0,
        stop_loss=_finite_nonnegative(item.stop_loss),
        total_score=0.0,
        status="WATCHING",
        technical_score=0.0,
        setup_score=0.0,
        risk_score=0.0,
        news_score=0.0,
        timing_score=0.0,
        rr=0.0,
        stop_adr=0.0,
        position_percent=0.0,
        ai_summary="",
        warnings=[],
        notes=str(item.notes or ""),
        added_date=item.added_date,
        risk_percent=1.0,
        trade_plan="",
        monitoring_status="WATCHING",
        kis_account_no=_account(account_no),
        environment="PROD",
        breakout_price=_optional_positive(item.breakout_price),
        buffer_pct=resolved_buffer,
        orb_monitor_enabled=False,
    )


def _watchlist_from_buylist(item, *, card: Optional[TradeCardState] = None) -> WatchlistItem:
    breakout = (
        getattr(card, "breakout_price", None)
        if card is not None
        else getattr(item, "breakout_price", None)
    )
    return WatchlistItem(
        symbol=_symbol(getattr(item, "symbol", "") or getattr(card, "symbol", "")),
        name=str(
            getattr(item, "name", "")
            or getattr(card, "name", "")
            or getattr(item, "symbol", "")
        ),
        entry_price=_optional_positive(getattr(item, "entry_price", None)),
        breakout_price=_optional_positive(breakout),
        stop_loss=_optional_positive(getattr(item, "stop_loss", None)),
        notes=str(getattr(item, "notes", "") or ""),
        added_date=getattr(item, "added_date", datetime.now(timezone.utc)),
        # Deliberately discard the old Watchlist-tab ORB selection.  Watchlist
        # membership is passive; ORB choice belongs to Buy Board planning.
        selected_orb_plan=None,
    )


def _sync_watchlist_item(target: WatchlistItem, source: WatchlistItem) -> bool:
    fields = (
        "name",
        "entry_price",
        "breakout_price",
        "stop_loss",
        "notes",
        "selected_orb_plan",
    )
    before = tuple(getattr(target, field) for field in fields)
    for field in fields:
        setattr(target, field, getattr(source, field))
    return before != tuple(getattr(target, field) for field in fields)


def add_watchlist_candidate(
    watchlist,
    *,
    symbol: str,
    name: str = "",
    entry_price: Optional[float] = None,
    breakout_price: Optional[float] = None,
    engine: Optional[Engine],
    default_account_no: str,
    buffer_pct: float = 0.001,
) -> PlanningMembershipResult:
    """Add one passive Watchlist candidate, canonical first and local second."""

    normalized_symbol = _symbol(symbol)
    account_no = _account(default_account_no)
    if not normalized_symbol:
        raise PlanningMembershipError("A symbol is required")
    if engine is None or not account_no:
        raise PlanningMembershipError(
            "Shared planning storage and a selected production account are required; "
            "the Watchlist item was not added"
        )
    try:
        current = _canonical_card_for_selected_account(
            engine, normalized_symbol, account_no
        )
        if current is None:
            card = TradeCardState(
                environment="PROD",
                account_no=account_no,
                symbol=normalized_symbol,
                name=str(name or normalized_symbol),
                board_status=BoardStatus.WATCHLIST,
                watchlist_member=True,
                buylist_member=False,
                breakout_price=_optional_positive(breakout_price),
                buffer_pct=min(1.0, _finite_nonnegative(buffer_pct, 0.001)),
            )
            stored = trade_card_repository.create_trade_card(engine, card)
        else:
            if (
                current.board_status not in {
                    BoardStatus.WATCHLIST,
                    BoardStatus.BUYLIST,
                }
                or not is_passive_planning_card(current)
            ):
                raise PlanningMembershipError(
                    f"{normalized_symbol} is already in {current.board_status.value}; "
                    "its Watchlist membership cannot be changed"
                )
            updated = copy.deepcopy(current)
            updated.watchlist_member = True
            updated.name = str(name or updated.name or normalized_symbol)
            if breakout_price is not None:
                updated.breakout_price = _optional_positive(breakout_price)
            if current.board_status == BoardStatus.WATCHLIST:
                updated.buylist_member = False
                _clear_executable_geometry(updated)
            else:
                # Watchlist is an independent saved-symbol membership. Adding
                # it to a passive Buylist card must not demote or rewrite the
                # card's Buylist plan.
                updated.buylist_member = True
            stored = trade_card_repository.update_trade_card(
                engine, updated, expected_version=current.version
            )
    except SQLAlchemyError as exc:
        raise PlanningMembershipError(
            "Shared planning storage is unavailable; the Watchlist item was not added"
        ) from exc

    local = watchlist.add(normalized_symbol, str(name or normalized_symbol), entry_price)
    local.breakout_price = _optional_positive(
        breakout_price
        if breakout_price is not None
        else getattr(stored, "breakout_price", None)
    )
    return PlanningMembershipResult(
        action="added_to_watchlist",
        symbol=normalized_symbol,
        card=stored,
        changed=True,
    )


def promote_watchlist_to_buylist(
    watchlist,
    buylist_manager,
    symbol: str,
    *,
    engine: Optional[Engine],
    default_account_no: str,
    buffer_pct: float = 0.001,
) -> PlanningMembershipResult:
    """Add a passive Watchlist candidate to Buylist without unsaving it."""

    normalized_symbol = _symbol(symbol)
    source = watchlist.get(normalized_symbol)
    if source is None:
        raise PlanningMembershipError(f"{normalized_symbol or 'Symbol'} is not in Watchlist")
    account_no = _account(default_account_no)
    if engine is None or not account_no:
        raise PlanningMembershipError(
            "Shared planning storage and a selected production account are required; "
            "the Watchlist item was not promoted"
        )
    existing_item = buylist_manager.get(normalized_symbol, "PROD")
    if existing_item is not None and not _passive_buylist_item(existing_item):
        raise PlanningMembershipError(
            f"{normalized_symbol} already has active Buylist/order state"
        )
    candidate = (
        copy.deepcopy(existing_item)
        if existing_item is not None
        else _buylist_from_watchlist(
            source,
            account_no=account_no,
            buffer_pct=buffer_pct,
        )
    )
    candidate.kis_account_no = account_no
    candidate.environment = "PROD"

    try:
        _canonical_card_for_selected_account(engine, normalized_symbol, account_no)
        sync: BuylistMembershipSyncResult = reconcile_buylist_item(
            engine,
            candidate,
            default_account_no=account_no,
            watchlist=watchlist,
            explicit_watchlist_promotion=True,
        )
        card = sync.card
        if sync.action == "conflicted" or card is None:
            raise PlanningMembershipError(
                f"{normalized_symbol} changed concurrently; refresh and try again"
            )
        if (
            card.board_status != BoardStatus.BUYLIST
            or not is_passive_planning_card(card)
        ):
            raise PlanningMembershipError(
                f"{normalized_symbol} is already in {card.board_status.value}; "
                "its canonical lifecycle was left unchanged"
            )
        # The canonical CAS result may contain a newer chart target than the
        # request's copied Watchlist snapshot. Normalize the JSON mirror from
        # that committed result before exposing Buylist.
        candidate.breakout_price = _optional_positive(card.breakout_price)
        candidate.buffer_pct = min(
            1.0, _finite_nonnegative(card.buffer_pct, 0.001)
        )
        candidate.kis_account_no = _account(card.account_no)
        candidate.name = str(card.name or candidate.name or normalized_symbol)
    except SQLAlchemyError as exc:
        raise PlanningMembershipError(
            "Shared planning storage is unavailable; the Watchlist item was not promoted"
        ) from exc

    # Buylist is an additional planning membership. The saved Watchlist row
    # deliberately remains so the symbol is still available in that view.
    buylist_manager.add(candidate)
    return PlanningMembershipResult(
        action="promoted_to_buylist",
        symbol=normalized_symbol,
        card=card,
        buylist_item=candidate,
        changed=True,
    )


def remove_watchlist_candidate(
    watchlist,
    symbol: str,
    *,
    engine: Optional[Engine],
    default_account_no: str,
) -> PlanningMembershipResult:
    """Remove Watchlist membership while preserving an overlapping Buylist."""

    normalized_symbol = _symbol(symbol)
    source = watchlist.get(normalized_symbol)
    account_no = _account(default_account_no)
    if engine is None or not account_no:
        raise PlanningMembershipError(
            "Shared planning storage is unavailable; the Watchlist item was not removed"
        )
    stored = None
    try:
        current = _canonical_card_for_selected_account(
            engine, normalized_symbol, account_no
        )
        if current is not None:
            if current.board_status not in {
                BoardStatus.WATCHLIST,
                BoardStatus.BUYLIST,
            } or (
                current.board_status == BoardStatus.WATCHLIST
                and not is_passive_planning_card(current)
            ):
                raise PlanningMembershipError(
                    f"{normalized_symbol} is not a passive Watchlist candidate"
                )
            archived = copy.deepcopy(current)
            archived.watchlist_member = False
            if current.board_status == BoardStatus.WATCHLIST:
                archived.buylist_member = False
                archived.breakout_price = None
                _clear_executable_geometry(archived)
                archived.board_status_updated_at = datetime.now(timezone.utc)
            else:
                # W removes only the independent Watchlist membership. The
                # Buylist card and all planning/execution metadata survive, so
                # durable evidence must not block this independent toggle.
                archived.buylist_member = True
            stored = trade_card_repository.update_trade_card(
                engine, archived, expected_version=current.version
            )
        elif source is None:
            return PlanningMembershipResult("unchanged", normalized_symbol)
    except SQLAlchemyError as exc:
        raise PlanningMembershipError(
            "Shared planning storage is unavailable; the Watchlist item was not removed"
        ) from exc
    watchlist.remove(normalized_symbol)
    return PlanningMembershipResult(
        action="removed_from_watchlist",
        symbol=normalized_symbol,
        card=stored,
        changed=True,
    )


def sync_legacy_planning_membership_from_card(
    watchlist,
    buylist_manager,
    card: TradeCardState,
) -> PlanningMembershipResult:
    """Project a canonical passive planning card into the two JSON mirrors.

    Call this only after observing a successfully committed canonical card
    (including after an operator-queued command is consumed).  It performs no
    database or broker work.
    """

    symbol = _symbol(card.symbol)
    if not is_passive_planning_card(card):
        return PlanningMembershipResult("ignored_non_passive", symbol, card=card)
    watch_item = watchlist.get(symbol)
    buy_item = buylist_manager.get(symbol, "PROD")

    if card.board_status == BoardStatus.WATCHLIST:
        if not card.watchlist_member:
            changed = bool(watchlist.remove(symbol))
            if buy_item is not None and _passive_buylist_item(buy_item):
                changed = bool(buylist_manager.remove(symbol, "PROD")) or changed
            return PlanningMembershipResult(
                "archived_watchlist" if changed else "unchanged",
                symbol,
                card=card,
                changed=changed,
            )
        source = buy_item or watch_item or card
        converted = _watchlist_from_buylist(source, card=card)
        item_changed = False
        if watch_item is None:
            watchlist.items.append(converted)
            item_changed = True
        else:
            item_changed = _sync_watchlist_item(watch_item, converted)
        removed = False
        if buy_item is not None and _passive_buylist_item(buy_item):
            removed = buylist_manager.remove(symbol, "PROD")
        return PlanningMembershipResult(
            "synced_watchlist",
            symbol,
            card=card,
            changed=bool(item_changed or removed),
        )

    if card.board_status == BoardStatus.BUYLIST:
        if buy_item is not None and not _passive_buylist_item(buy_item):
            return PlanningMembershipResult(
                "ignored_non_passive_mirror", symbol, card=card
            )
        item_changed = False
        if buy_item is None:
            source = watch_item or _watchlist_from_buylist(card, card=card)
            buy_item = _buylist_from_watchlist(
                source,
                account_no=card.account_no,
                buffer_pct=card.buffer_pct,
            )
            buy_item.breakout_price = card.breakout_price
            buylist_manager.add(buy_item)
            item_changed = True
        else:
            desired_breakout = _optional_positive(card.breakout_price)
            desired_buffer = min(
                1.0, _finite_nonnegative(card.buffer_pct, 0.001)
            )
            desired_account = _account(card.account_no)
            desired_name = str(card.name or buy_item.name or symbol)
            before = (
                buy_item.breakout_price,
                buy_item.buffer_pct,
                buy_item.kis_account_no,
                buy_item.name,
            )
            buy_item.breakout_price = desired_breakout
            buy_item.buffer_pct = desired_buffer
            buy_item.kis_account_no = desired_account
            buy_item.name = desired_name
            after = (
                buy_item.breakout_price,
                buy_item.buffer_pct,
                buy_item.kis_account_no,
                buy_item.name,
            )
            item_changed = before != after
        watch_changed = False
        if card.watchlist_member:
            converted = _watchlist_from_buylist(buy_item, card=card)
            if watch_item is None:
                watchlist.items.append(converted)
                watch_changed = True
            else:
                watch_changed = _sync_watchlist_item(watch_item, converted)
        else:
            watch_changed = bool(watchlist.remove(symbol))
        return PlanningMembershipResult(
            "synced_buylist",
            symbol,
            card=card,
            buylist_item=buy_item,
            changed=bool(watch_changed or item_changed),
        )

    return PlanningMembershipResult("ignored_non_planning", symbol, card=card)


__all__ = [
    "PlanningMembershipError",
    "PlanningMembershipResult",
    "add_watchlist_candidate",
    "is_passive_planning_card",
    "promote_watchlist_to_buylist",
    "remove_watchlist_candidate",
    "sync_legacy_planning_membership_from_card",
]
