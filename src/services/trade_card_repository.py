"""Persistence for :class:`~src.core.trade_card_state.TradeCardState`.

Follows the same pattern :mod:`src.services.state_sync` already uses for the
``app_state_sync`` table (a SQLAlchemy ``Table`` + idempotent
``metadata.create_all``), but as a genuine per-row table -- one row per
``(environment, account_no, symbol)`` with a real ``UNIQUE`` constraint and a
per-row optimistic-concurrency ``version`` column, per
``buydashboard_to_kanban.md`` section 287-291, rather than one whole-blob
JSON payload per collection.

A local JSON snapshot (``data/trade_cards.json``) is written alongside every
successful database write, using the same atomic-write/``.bak`` convention as
every other state file in :mod:`src.utils.storage`. That snapshot is a
migration/backup/recovery aid only (section 905) -- the database row is
authoritative whenever it is reachable.

This module is purely a repository: it does not decide *what* board_status a
card should have (see :mod:`src.core.kanban_transitions`) and it does not
submit or cancel any broker order.
"""
from __future__ import annotations

import logging
import threading
import weakref
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    func,
    select,
)
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from src.core.kanban_transitions import (
    require_single_card_per_symbol,
    migrate_legacy_status_to_board_status,
)
from src.core.trade_card_state import (
    PositionRuntimeStatus,
    StopType,
    TradeCardState,
)
from src.utils.config import DATA_DIR
from src.utils.storage import load_json, save_json

logger = logging.getLogger(__name__)

LOCAL_TRADE_CARDS_FILE = DATA_DIR / "trade_cards.json"

_ensured_engines: "weakref.WeakSet[Engine]" = weakref.WeakSet()
_ensure_lock = threading.Lock()
_local_change_generations: "weakref.WeakKeyDictionary[Engine, int]" = (
    weakref.WeakKeyDictionary()
)
_local_change_lock = threading.Lock()


def local_trade_card_change_generation(engine: Optional[Engine]) -> int:
    """Return a process-local token incremented after every card SQL write.

    The runtime can poll the remote collection revision once per minute while
    still observing writes made through this process's shared engine on its
    next one-second market cycle.  Database CAS/version checks remain the
    authority; this token is only a safe cache invalidation hint.
    """

    if engine is None:
        return 0
    with _local_change_lock:
        return int(_local_change_generations.get(engine, 0))


def _mark_local_trade_card_changed(engine: Engine) -> None:
    with _local_change_lock:
        _local_change_generations[engine] = int(
            _local_change_generations.get(engine, 0)
        ) + 1


def invalidate_trade_cards_table_cache(engine: Engine) -> None:
    """Forget an ensure result after migration rollback drops the table."""

    with _ensure_lock:
        _ensured_engines.discard(engine)


class TradeCardVersionConflictError(RuntimeError):
    """A write's ``expected_version`` no longer matches the stored row.

    This is the mechanism spec section 317 requires: "The backend must
    reject stale commands when expected_card_version does not match the
    current version." Callers (Kanban drag-command handlers) must treat this
    as "reload and let the user retry," never as a silent overwrite.
    """


class TradeCardNotFoundError(RuntimeError):
    """No row exists for the requested (environment, account_no, symbol)."""


@dataclass(frozen=True)
class TradeCardCollectionRevision:
    """Compact change token used instead of downloading every card each tick."""

    row_count: int
    version_sum: int
    updated_at: Optional[datetime]


def _get_trade_cards_table(metadata: MetaData) -> Table:
    return Table(
        "trade_cards",
        metadata,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("environment", String(10), nullable=False),
        Column("account_no", String(32), nullable=False),
        Column("symbol", String(20), nullable=False),
        Column("board_status", String(32), nullable=False),
        Column("version", BigInteger, nullable=False, server_default="1"),
        Column("payload", Text(length=16_777_215), nullable=False),
        Column("updated_at", DateTime, nullable=False),
        UniqueConstraint(
            "environment", "account_no", "symbol", name="uq_trade_cards_symbol"
        ),
    )


def _ensure_trade_cards_table(engine: Engine) -> Table:
    metadata = MetaData()
    table = _get_trade_cards_table(metadata)
    if engine in _ensured_engines:
        return table
    with _ensure_lock:
        if engine in _ensured_engines:
            return table
        metadata.create_all(engine)
        _ensured_engines.add(engine)
    return table


def ensure_trade_cards_table(engine: Engine) -> Table:
    """Public pre-transaction table setup for atomic account plans."""
    return _ensure_trade_cards_table(engine)


def _server_now(engine: Engine):
    if engine.dialect.name == "mysql":
        return func.utc_timestamp(6)
    return func.current_timestamp()


def _row_to_card(row) -> TradeCardState:
    payload = row.payload
    import json as _json

    data = _json.loads(payload) if isinstance(payload, str) else dict(payload or {})
    data["version"] = int(row.version)
    return TradeCardState.from_dict(data)


def get_trade_card(
    engine: Optional[Engine],
    environment: str,
    account_no: str,
    symbol: str,
    *,
    raise_on_error: bool = False,
) -> Optional[TradeCardState]:
    if engine is None:
        return None
    environment = str(environment or "").upper()
    account_no = str(account_no or "")
    symbol = str(symbol or "").upper()
    try:
        table = _ensure_trade_cards_table(engine)
        with engine.connect() as conn:
            row = conn.execute(
                select(table).where(
                    table.c.environment == environment,
                    table.c.account_no == account_no,
                    table.c.symbol == symbol,
                )
            ).first()
        return _row_to_card(row) if row is not None else None
    except SQLAlchemyError as exc:
        if raise_on_error:
            raise
        logger.info("trade_card_repository: get_trade_card failed: %s", exc)
        return None


def get_trade_card_in_transaction(
    conn,
    environment: str,
    account_no: str,
    symbol: str,
    *,
    for_update: bool = False,
) -> Optional[TradeCardState]:
    """Read a card under a caller-owned workflow transaction."""
    table = _get_trade_cards_table(MetaData())
    statement = select(table).where(
        table.c.environment == str(environment or "").upper(),
        table.c.account_no == str(account_no or ""),
        table.c.symbol == str(symbol or "").upper(),
    )
    if for_update:
        statement = statement.with_for_update()
    row = conn.execute(statement).first()
    return _row_to_card(row) if row is not None else None


def list_trade_cards(
    engine: Optional[Engine],
    *,
    environment: Optional[str] = None,
    account_no: Optional[str] = None,
    raise_on_error: bool = False,
) -> List[TradeCardState]:
    if engine is None:
        return []
    try:
        table = _ensure_trade_cards_table(engine)
        statement = select(table)
        if environment:
            statement = statement.where(table.c.environment == str(environment).upper())
        if account_no:
            statement = statement.where(table.c.account_no == str(account_no))
        with engine.connect() as conn:
            rows = conn.execute(statement).fetchall()
        return [_row_to_card(row) for row in rows]
    except SQLAlchemyError as exc:
        if raise_on_error:
            raise
        logger.info("trade_card_repository: list_trade_cards failed: %s", exc)
        return []


def list_trade_cards_for_symbol(
    engine: Optional[Engine],
    environment: str,
    symbol: str,
    *,
    raise_on_error: bool = False,
) -> List[TradeCardState]:
    """Return canonical rows for one symbol across accounts.

    Watchlist's compact JSON model is account-agnostic.  Membership services
    use this narrow query to prevent a selected fallback account from creating
    a second canonical planning card when the symbol already belongs to a
    different account.
    """

    if engine is None:
        return []
    try:
        table = _ensure_trade_cards_table(engine)
        statement = select(table).where(
            table.c.environment == str(environment or "").upper(),
            table.c.symbol == str(symbol or "").strip().upper(),
        )
        with engine.connect() as conn:
            rows = conn.execute(statement).fetchall()
        return [_row_to_card(row) for row in rows]
    except SQLAlchemyError as exc:
        if raise_on_error:
            raise
        logger.info(
            "trade_card_repository: list_trade_cards_for_symbol failed: %s", exc
        )
        return []


def get_trade_card_collection_revision(
    engine: Optional[Engine],
    *,
    environment: Optional[str] = None,
    account_no: Optional[str] = None,
    raise_on_error: bool = False,
) -> Optional[TradeCardCollectionRevision]:
    """Return a three-value token that changes after any normal card write.

    This query returns one small row and never transfers card JSON payloads.
    The runtime performs a full reload only when the token differs from its
    cache. Exact optimistic versions still protect every subsequent write.
    """

    if engine is None:
        return None
    try:
        table = _ensure_trade_cards_table(engine)
        statement = select(
            func.count(table.c.id),
            func.coalesce(func.sum(table.c.version), 0),
            func.max(table.c.updated_at),
        )
        if environment:
            statement = statement.where(
                table.c.environment == str(environment).upper()
            )
        if account_no:
            statement = statement.where(table.c.account_no == str(account_no))
        with engine.connect() as conn:
            row = conn.execute(statement).one()
        return TradeCardCollectionRevision(
            row_count=int(row[0] or 0),
            version_sum=int(row[1] or 0),
            updated_at=row[2],
        )
    except SQLAlchemyError as exc:
        if raise_on_error:
            raise
        logger.info(
            "trade_card_repository: get_trade_card_collection_revision failed: %s",
            exc,
        )
        return None


def _upsert_local_snapshot_card(
    card: TradeCardState, path: Optional[Path] = None
) -> None:
    """Refreshes just this one card's entry in the local backup snapshot.

    Code review finding P1-13: the module docstring above promises "a local
    JSON snapshot is written alongside every successful database write," but
    ``create_trade_card``/``update_trade_card`` previously never called
    this -- the snapshot only ever updated via the migration path's
    explicit ``sync_local_snapshot_from_database`` call. This closes that
    gap with a single-record upsert (read-modify-write the snapshot file,
    not a full re-fetch of every card from the database) so it stays cheap
    enough to call on every write.

    ``path`` defaults to ``None`` -- resolved against the *current* value of
    the module-level ``LOCAL_TRADE_CARDS_FILE`` at call time, not frozen as
    a function-default value at import time. This is deliberate: a bound
    default (``path=LOCAL_TRADE_CARDS_FILE``) would not pick up a test's
    ``monkeypatch.setattr(trade_card_repository, "LOCAL_TRADE_CARDS_FILE", ...)``,
    which is exactly the class of bug that made the local capital-reservation
    ledger accidentally write to production `data/` earlier in this project
    (see the identical fix in ``EntryAttemptManager``/``capital_allocator``).
    """
    resolved_path = path or LOCAL_TRADE_CARDS_FILE
    cards = load_local_trade_cards_snapshot(path=resolved_path)
    cards = [existing for existing in cards if existing.card_key != card.card_key]
    cards.append(card)
    save_local_trade_cards_snapshot(cards, path=resolved_path)


def sync_trade_card_local_snapshot(
    card: TradeCardState, *, path: Optional[Path] = None
) -> None:
    """Refresh the recovery snapshot after a caller-owned DB transaction."""
    _upsert_local_snapshot_card(card, path=path)


def insert_trade_card(conn, card: TradeCardState) -> TradeCardState:
    """Insert one card using the caller's transaction."""
    table = _get_trade_cards_table(MetaData())
    import json as _json

    payload = card.to_dict()
    payload["version"] = 1
    try:
        conn.execute(
            table.insert().values(
                environment=card.environment,
                account_no=card.account_no,
                symbol=card.symbol,
                board_status=card.board_status.value,
                version=1,
                payload=_json.dumps(payload, separators=(",", ":")),
                updated_at=_server_now(conn.engine),
            )
        )
    except IntegrityError as exc:
        raise TradeCardVersionConflictError(
            f"A trade card already exists for {card.card_key}"
        ) from exc
    _mark_local_trade_card_changed(conn.engine)
    card.version = 1
    return card


def update_trade_card_in_transaction(
    conn,
    card: TradeCardState,
    *,
    expected_version: int,
) -> TradeCardState:
    """Optimistically update one card using the caller's transaction."""
    table = _get_trade_cards_table(MetaData())
    next_version = int(expected_version) + 1
    card.updated_at = datetime.now(timezone.utc)
    import json as _json

    payload = card.to_dict()
    payload["version"] = next_version
    result = conn.execute(
        table.update()
        .where(
            table.c.environment == card.environment,
            table.c.account_no == card.account_no,
            table.c.symbol == card.symbol,
            table.c.version == int(expected_version),
        )
        .values(
            board_status=card.board_status.value,
            version=next_version,
            payload=_json.dumps(payload, separators=(",", ":")),
            updated_at=_server_now(conn.engine),
        )
    )
    if result.rowcount == 0:
        existing = conn.execute(
            select(table.c.version).where(
                table.c.environment == card.environment,
                table.c.account_no == card.account_no,
                table.c.symbol == card.symbol,
            )
        ).first()
        if existing is None:
            raise TradeCardNotFoundError(f"No trade card exists for {card.card_key}")
        raise TradeCardVersionConflictError(
            f"Expected version {expected_version} for {card.card_key}, "
            f"stored version is {existing.version}"
        )
    _mark_local_trade_card_changed(conn.engine)
    card.version = next_version
    return card


def create_trade_card(
    engine: Engine, card: TradeCardState, *, local_snapshot_path: Optional[Path] = None
) -> TradeCardState:
    """Insert a brand-new card. Raises ``TradeCardVersionConflictError`` if a
    row for this (environment, account_no, symbol) already exists -- callers
    that intend to update an existing card must use ``update_trade_card``
    instead, so the one-card-per-symbol invariant can never be bypassed by
    accidentally inserting a second row.
    """
    _ensure_trade_cards_table(engine)
    with engine.begin() as conn:
        insert_trade_card(conn, card)
    _upsert_local_snapshot_card(card, path=local_snapshot_path)
    return card


def update_trade_card(
    engine: Engine,
    card: TradeCardState,
    *,
    expected_version: int,
    local_snapshot_path: Optional[Path] = None,
) -> TradeCardState:
    """Conditionally update an existing row: the write only applies if the
    stored ``version`` still equals ``expected_version`` (spec section 317's
    optimistic-concurrency guard). On success the returned card's ``version``
    is bumped by one.
    """
    _ensure_trade_cards_table(engine)
    with engine.begin() as conn:
        update_trade_card_in_transaction(
            conn, card, expected_version=expected_version
        )
    _upsert_local_snapshot_card(card, path=local_snapshot_path)
    return card


def upsert_trade_card_unconditionally(
    engine: Engine, card: TradeCardState, *, local_snapshot_path: Optional[Path] = None
) -> TradeCardState:
    """Insert-or-blind-overwrite, bypassing the optimistic-version check.

    Reserved for the migration importer, which is seeding rows from legacy
    state with nothing yet to conflict against. Never call this from a
    Kanban command handler -- that path must always go through
    ``update_trade_card`` with a real ``expected_version`` so a stale PC/
    laptop drag can never silently clobber a newer write (spec section 319).
    """
    existing = get_trade_card(engine, card.environment, card.account_no, card.symbol)
    if existing is None:
        return create_trade_card(engine, card, local_snapshot_path=local_snapshot_path)
    return update_trade_card(
        engine, card, expected_version=existing.version, local_snapshot_path=local_snapshot_path
    )


# --- Local JSON snapshot (backup/recovery tier, section 905) ---------------


def save_local_trade_cards_snapshot(
    cards: Iterable[TradeCardState], path=LOCAL_TRADE_CARDS_FILE
) -> None:
    save_json(path, {"cards": [card.to_dict() for card in cards]})


def load_local_trade_cards_snapshot(path=LOCAL_TRADE_CARDS_FILE) -> List[TradeCardState]:
    data = load_json(path, {"cards": []})
    cards: List[TradeCardState] = []
    for raw in data.get("cards", []):
        try:
            cards.append(TradeCardState.from_dict(raw))
        except (TypeError, ValueError) as exc:
            logger.warning("Rejected local trade-card snapshot record: %s", exc)
    return cards


def sync_local_snapshot_from_database(
    engine: Optional[Engine], *, path=LOCAL_TRADE_CARDS_FILE
) -> List[TradeCardState]:
    """Refresh the local backup snapshot from the database's full row set."""
    cards = list_trade_cards(engine)
    if cards:
        save_local_trade_cards_snapshot(cards, path=path)
    return cards


# --- Migration from legacy BuylistItem / ExecutionQueueItem ----------------


@dataclass
class MigrationRow:
    key: str
    action: str  # "create" | "update" | "unchanged"
    card: TradeCardState
    previous_board_status: Optional[str] = None


@dataclass
class MigrationReport:
    rows: List[MigrationRow] = field(default_factory=list)

    @property
    def creates(self) -> List[MigrationRow]:
        return [row for row in self.rows if row.action == "create"]

    @property
    def updates(self) -> List[MigrationRow]:
        return [row for row in self.rows if row.action == "update"]

    def describe(self) -> str:
        lines = [f"{len(self.rows)} legacy buylist row(s) considered"]
        for row in self.rows:
            if row.action == "unchanged":
                continue
            lines.append(
                f"  {row.action}: {row.key} -> "
                f"{row.card.board_status.value}"
                + (
                    f" (was {row.previous_board_status})"
                    if row.previous_board_status
                    else ""
                )
            )
        return "\n".join(lines)


def _selected_orb_plan_of(item: Any) -> Dict[str, Any]:
    plan = getattr(item, "selected_orb_plan", None)
    return plan if isinstance(plan, dict) else {}


def build_trade_card_migration(
    *,
    buylist_manager,
    watchlist=None,
    existing_cards: Optional[List[TradeCardState]] = None,
) -> MigrationReport:
    """Pure, dry-run mapping from legacy ``BuylistItem`` rows to
    ``TradeCardState`` rows, per spec section 25's table. Touches no
    database, no broker, and no file -- callers decide whether/how to
    persist the result.

    Existing broker-confirmed quantity/avg-cost/stop always take priority
    over any guess this function makes; it exists to seed board_status and
    metadata only. Per spec section 981, the caller must run a KIS
    reconciliation pass against the created/updated cards before enabling
    any execution off them -- this function does not do that.
    """
    existing_by_key = {card.card_key: card for card in (existing_cards or [])}
    watchlist_symbols = {
        str(getattr(watch_item, "symbol", "")).upper()
        for watch_item in getattr(watchlist, "items", [])
    }

    report = MigrationReport()
    for item in buylist_manager.items:
        board_status = migrate_legacy_status_to_board_status(
            item.monitoring_status,
            orb_monitor_enabled=bool(getattr(item, "orb_monitor_enabled", False)),
            shares_held=int(getattr(item, "shares_held", 0) or 0),
        )
        shares_held = int(getattr(item, "shares_held", 0) or 0)
        stop_loss = float(getattr(item, "stop_loss", 0.0) or 0.0)
        plan = _selected_orb_plan_of(item)

        card = TradeCardState(
            environment=item.environment,
            account_no=getattr(item, "kis_account_no", "") or "",
            symbol=item.symbol,
            name=item.name,
            board_status=board_status,
            watchlist_member=item.symbol.upper() in watchlist_symbols,
            buylist_member=True,
            return_to_buylist_after_close=False,
            breakout_price=item.breakout_price,
            selected_orb_window=plan.get("window"),
            buffer_pct=float(
                0.001
                if getattr(item, "buffer_pct", None) in (None, "")
                else getattr(item, "buffer_pct")
            ),
            risk_percent=float(getattr(item, "risk_percent", 1.0) or 1.0),
            position_percent=float(
                getattr(item, "position_percent", 0.0) or 0.0
            ),
            planned_quantity=int(plan.get("shares", 0) or 0),
            target_position_quantity=shares_held or int(plan.get("shares", 0) or 0),
            entry_orb_window=plan.get("window"),
            entry_trigger=plan.get("entry_trigger"),
            stop_adr=plan.get("stop_adr"),
            position_runtime_status=(
                PositionRuntimeStatus.OPEN
                if shares_held > 0
                else PositionRuntimeStatus.NONE
            ),
            broker_quantity=shares_held,
            orderable_quantity=shares_held,
            average_entry_price=float(getattr(item, "avg_cost", 0.0) or 0.0),
            # Migrated positions have an unknown stop provenance -- treat any
            # persisted stop as a manual price rather than guessing it was
            # the original ORB low, per the "never recalculate the historical
            # entry ORB" guidance (section 620) applied conservatively.
            stop_type=StopType.MANUAL_PRICE if stop_loss > 0 else None,
            active_stop_price=stop_loss if stop_loss > 0 else None,
            stop_quantity=shares_held if stop_loss > 0 else 0,
            warnings=["migrated_from_buylist"],
        )

        existing = existing_by_key.get(card.card_key)
        if existing is None:
            report.rows.append(MigrationRow(key=card.card_key, action="create", card=card))
        elif existing.board_status != card.board_status:
            card.version = existing.version
            report.rows.append(
                MigrationRow(
                    key=card.card_key,
                    action="update",
                    card=card,
                    previous_board_status=existing.board_status.value,
                )
            )
        else:
            report.rows.append(MigrationRow(key=card.card_key, action="unchanged", card=existing))

    require_single_card_per_symbol(row.card for row in report.rows)
    return report


def migrate_buylist_to_trade_cards(
    engine: Optional[Engine],
    *,
    buylist_manager,
    watchlist=None,
    apply: bool = False,
    local_snapshot_path=LOCAL_TRADE_CARDS_FILE,
) -> MigrationReport:
    """Build the migration report; persist it only when ``apply=True``.

    Dry-run (``apply=False``, the default) never touches the database or the
    local snapshot -- callers should log/review ``report.describe()`` first.
    """
    existing_cards = list_trade_cards(engine) if engine is not None else []
    report = build_trade_card_migration(
        buylist_manager=buylist_manager,
        watchlist=watchlist,
        existing_cards=existing_cards,
    )
    if not apply:
        return report
    if engine is None:
        logger.warning(
            "Trade-card migration requested apply=True with no database engine; "
            "writing local snapshot only."
        )
        save_local_trade_cards_snapshot(
            (row.card for row in report.rows), path=local_snapshot_path
        )
        return report

    for row in report.rows:
        if row.action == "create":
            create_trade_card(engine, row.card, local_snapshot_path=local_snapshot_path)
        elif row.action == "update":
            update_trade_card(
                engine,
                row.card,
                expected_version=row.card.version,
                local_snapshot_path=local_snapshot_path,
            )
    sync_local_snapshot_from_database(engine, path=local_snapshot_path)
    return report
