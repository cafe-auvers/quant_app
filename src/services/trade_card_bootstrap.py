"""Non-destructive bootstrap of visible Kanban state from existing app state.

The Kanban database is authoritative once a card exists.  This module only
bridges the already-loaded legacy Watchlist/Buylist and fresh cached KIS
holdings into trade-card rows so opening the Buy Board does not require a
manual migration step.  It deliberately performs no network I/O.  Legacy
Buylist state may repair a passive WATCHLIST card to BUYLIST, but never
rewrites a later execution lifecycle.

Fresh cached holdings are allowed to update broker-derived position facts when
the dedicated runtime worker is not running.  The runtime's normal account
reconciliation remains authoritative once it starts.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping, Optional

from sqlalchemy.engine import Engine

from src.core.trade_card_state import BoardStatus, TradeCardState
from src.services import trade_card_repository
from src.services.buylist_membership_service import reconcile_buylist_item
from src.services.position_manager import PositionManager, extract_overseas_holdings


@dataclass(frozen=True)
class TradeCardBootstrapResult:
    """Summary of one idempotent bootstrap pass."""

    created_keys: tuple[str, ...] = ()
    buylist_promoted_keys: tuple[str, ...] = ()
    holding_updated_keys: tuple[str, ...] = ()
    skipped_watchlist_symbols: tuple[str, ...] = ()

    @property
    def changed(self) -> bool:
        return bool(
            self.created_keys
            or self.buylist_promoted_keys
            or self.holding_updated_keys
        )


def _normalized_account(value: object) -> str:
    return str(value or "").strip()


def _normalized_symbol(value: object) -> str:
    return str(value or "").strip().upper()


def _snapshot_is_fresh(
    fetched_at: object,
    *,
    now: datetime,
    max_age_seconds: float,
) -> bool:
    if not isinstance(fetched_at, datetime):
        return False
    observed = fetched_at
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=timezone.utc)
    observed = observed.astimezone(timezone.utc)
    age = (now - observed).total_seconds()
    return 0.0 <= age <= max(0.0, float(max_age_seconds))


def bootstrap_trade_cards_from_current_state(
    engine: Optional[Engine],
    *,
    buylist_manager,
    watchlist=None,
    default_account_no: str = "",
    account_snapshots: Optional[Mapping] = None,
    account_snapshot_fetched_at: Optional[Mapping] = None,
    max_snapshot_age_seconds: float = 120.0,
    now: Optional[datetime] = None,
) -> TradeCardBootstrapResult:
    """Seed/reconcile Watchlist/Buylist cards and project cached holdings.

    Legacy Buylist state may create a missing row and may perform the one safe
    recovery transition ``WATCHLIST -> BUYLIST`` for a passive candidate.  An
    existing later lifecycle may own an order, position, stop, or newer Kanban
    gesture, so it is never moved backwards from legacy metadata.  Fresh
    cached KIS holdings may update broker-derived position facts through
    :class:`PositionManager`, which preserves active entry/exit intent exactly
    like normal runtime reconciliation.

    ``account_snapshots`` is intentionally optional.  The UI passes it only
    while the dedicated Buy Board runtime is not running; once the runtime is
    alive, its account reconciliation is the sole broker-truth projector.
    """

    if engine is None:
        return TradeCardBootstrapResult()

    trade_card_repository.ensure_trade_cards_table(engine)
    existing_cards = trade_card_repository.list_trade_cards(
        engine, environment="PROD"
    )
    existing_by_key = {card.card_key: card for card in existing_cards}
    created_keys: list[str] = []
    buylist_promoted_keys: list[str] = []
    holding_updated_keys: list[str] = []
    skipped_watchlist_symbols: list[str] = []
    fallback_account = _normalized_account(default_account_no)

    # Reuse the shared membership service used by direct Buylist additions.
    # Its asymmetric rule allows only missing-row creation or the legal
    # passive WATCHLIST -> BUYLIST promotion; later lifecycle state wins.
    cloned_buylist_items = []
    buylist_symbols: set[str] = set()
    for item in list(getattr(buylist_manager, "items", ()) or ()):
        if str(getattr(item, "environment", "PROD") or "PROD").upper() != "PROD":
            continue
        symbol = _normalized_symbol(getattr(item, "symbol", ""))
        if not symbol:
            continue
        buylist_symbols.add(symbol)
        account_no = _normalized_account(getattr(item, "kis_account_no", ""))
        if not account_no:
            account_no = fallback_account
        if not account_no:
            continue
        clone = copy.copy(item)
        clone.symbol = symbol
        clone.kis_account_no = account_no
        clone.environment = "PROD"
        cloned_buylist_items.append(clone)

    for item in cloned_buylist_items:
        key = f"PROD:{item.kis_account_no}:{item.symbol}"
        sync = reconcile_buylist_item(
            engine,
            item,
            watchlist=watchlist,
            existing_card=existing_by_key.get(key),
            existing_card_loaded=True,
        )
        stored = sync.card
        if stored is None:
            continue
        existing_by_key[stored.card_key] = stored
        if sync.action == "created":
            existing_cards.append(stored)
            created_keys.append(stored.card_key)
        elif sync.action == "promoted":
            buylist_promoted_keys.append(stored.card_key)

    # WatchlistItem is account-agnostic in the legacy model.  The configured
    # production account gives a deterministic identity for presentation and
    # later Watchlist->Buylist->Buy Today gestures.  Symbols already represented
    # by Buylist state are not duplicated as a second Watchlist card.
    for item in list(getattr(watchlist, "items", ()) or ()):
        symbol = _normalized_symbol(getattr(item, "symbol", ""))
        if not symbol or symbol in buylist_symbols:
            continue
        if not fallback_account:
            skipped_watchlist_symbols.append(symbol)
            continue
        key = f"PROD:{fallback_account}:{symbol}"
        if key in existing_by_key:
            continue
        plan = getattr(item, "selected_orb_plan", None)
        plan = plan if isinstance(plan, dict) else {}
        card = TradeCardState(
            environment="PROD",
            account_no=fallback_account,
            symbol=symbol,
            name=str(getattr(item, "name", "") or symbol),
            board_status=BoardStatus.WATCHLIST,
            watchlist_member=True,
            buylist_member=False,
            breakout_price=getattr(item, "breakout_price", None),
            selected_orb_window=plan.get("window"),
            buffer_pct=float(plan.get("buffer_pct", 0.001) or 0.001),
            planned_quantity=int(plan.get("shares", 0) or 0),
            target_position_quantity=int(plan.get("shares", 0) or 0),
            entry_trigger=plan.get("entry_trigger"),
            stop_adr=plan.get("stop_adr"),
        )
        try:
            stored = trade_card_repository.create_trade_card(engine, card)
        except trade_card_repository.TradeCardVersionConflictError:
            stored = trade_card_repository.get_trade_card(
                engine, "PROD", fallback_account, symbol
            )
            if stored is None:
                continue
        existing_by_key[stored.card_key] = stored
        existing_cards.append(stored)
        created_keys.append(stored.card_key)

    # Before the execution runtime starts, reuse only *fresh* KIS snapshots
    # already fetched by the legacy account worker.  This makes current broker
    # holdings visible immediately without performing network I/O on the Qt
    # thread.  Once the runtime worker is running the controller omits these
    # snapshots and normal account reconciliation owns this projection.
    reference = now or datetime.now(timezone.utc)
    snapshots = account_snapshots or {}
    fetched_map = account_snapshot_fetched_at or {}
    name_by_symbol = {
        _normalized_symbol(getattr(item, "symbol", "")): str(
            getattr(item, "name", "") or _normalized_symbol(getattr(item, "symbol", ""))
        )
        for source in (watchlist, buylist_manager)
        for item in list(getattr(source, "items", ()) or ())
        if _normalized_symbol(getattr(item, "symbol", ""))
    }
    position_manager = PositionManager()

    for raw_key, snapshot in list(getattr(snapshots, "items", lambda: ())()):
        if not isinstance(raw_key, tuple) or len(raw_key) != 2:
            continue
        environment, account_no = raw_key
        if str(environment or "").upper() != "PROD":
            continue
        account_no = _normalized_account(account_no)
        if not account_no or not _snapshot_is_fresh(
            fetched_map.get(raw_key),
            now=reference,
            max_age_seconds=max_snapshot_age_seconds,
        ):
            continue
        holdings = extract_overseas_holdings(snapshot)
        if not holdings:
            continue
        # Deep-copy current rows so an optimistic-write conflict cannot leave
        # the objects used for the next projection pass mutated in memory.
        account_cards = [
            copy.deepcopy(card)
            for card in existing_by_key.values()
            if card.environment == "PROD" and card.account_no == account_no
        ]
        changed = position_manager.reconcile_broker_positions(
            account_cards,
            holdings,
            environment="PROD",
            account_no=account_no,
            symbol_name_lookup=lambda symbol: name_by_symbol.get(symbol, symbol),
        )
        for card in changed:
            prior = existing_by_key.get(card.card_key)
            try:
                if prior is None:
                    stored = trade_card_repository.create_trade_card(engine, card)
                    created_keys.append(stored.card_key)
                else:
                    stored = trade_card_repository.update_trade_card(
                        engine,
                        card,
                        expected_version=prior.version,
                    )
                    holding_updated_keys.append(stored.card_key)
            except (
                trade_card_repository.TradeCardVersionConflictError,
                trade_card_repository.TradeCardNotFoundError,
            ):
                # A concurrent runtime/device write wins.  The next board
                # projection reloads canonical state; never retry as a blind
                # overwrite from a cached account snapshot.
                continue
            existing_by_key[stored.card_key] = stored

    return TradeCardBootstrapResult(
        created_keys=tuple(dict.fromkeys(created_keys)),
        buylist_promoted_keys=tuple(dict.fromkeys(buylist_promoted_keys)),
        holding_updated_keys=tuple(dict.fromkeys(holding_updated_keys)),
        skipped_watchlist_symbols=tuple(dict.fromkeys(skipped_watchlist_symbols)),
    )
