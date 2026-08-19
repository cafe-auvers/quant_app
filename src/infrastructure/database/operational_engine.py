"""Always-local persistence for Kanban execution state.

Historical daily/hourly/intraday databases are optional market-data inputs.
They must never decide whether the Buy Board can persist cards, reconcile KIS
orders, or execute.  This small SQLite database owns only operational state
such as cards, leases, commands, orders, and the live-trading control.
"""
from __future__ import annotations

import os
import logging
from pathlib import Path
from typing import Optional

from sqlalchemy import create_engine, event, func, select, text
from sqlalchemy.engine import Engine

from src.utils.config import DATA_DIR

LOCAL_OPERATIONAL_DB_ENV = "OPERATIONAL_DB_PATH"
LOCAL_OPERATIONAL_DB_FILE = DATA_DIR / "kanban_operational.sqlite3"
logger = logging.getLogger(__name__)


def _seed_existing_trade_cards(engine: Engine) -> None:
    """Import the recovery snapshot once, before projections create new rows."""

    from src.services import trade_card_repository as repository

    table = repository.ensure_trade_cards_table(engine)
    with engine.connect() as conn:
        if int(conn.execute(select(func.count()).select_from(table)).scalar_one()) > 0:
            return
    cards = repository.load_local_trade_cards_snapshot(
        path=repository.LOCAL_TRADE_CARDS_FILE
    )
    if not cards:
        return
    with engine.begin() as conn:
        for card in cards:
            repository.insert_trade_card(conn, card)


def operational_database_path(path: Optional[Path] = None) -> Path:
    if path is not None:
        return Path(path)
    configured = str(os.environ.get(LOCAL_OPERATIONAL_DB_ENV, "") or "").strip()
    return Path(configured) if configured else LOCAL_OPERATIONAL_DB_FILE


def init_local_operational_engine(
    db_path: Optional[Path] = None,
) -> Optional[Engine]:
    """Open the durable local Kanban store independently of market history."""

    path = operational_database_path(db_path).resolve(strict=False)
    database_existed = path.exists()
    engine: Optional[Engine] = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        engine = create_engine(
            f"sqlite+pysqlite:///{path.as_posix()}",
            future=True,
            connect_args={"timeout": 10.0, "check_same_thread": False},
        )

        @event.listens_for(engine, "connect")
        def _configure_sqlite(connection, _record) -> None:
            cursor = connection.cursor()
            try:
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA synchronous=FULL")
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute("PRAGMA busy_timeout=10000")
            finally:
                cursor.close()

        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        if not database_existed:
            # Sync revisions identify one specific database. A newly-created
            # operational file must not inherit revision numbers from an old
            # deleted file or from the optional shared/history database.
            from src.services import app_state
            from src.utils.storage import save_json

            save_json(app_state.KANBAN_STATE_METADATA_FILE, {})
        try:
            _seed_existing_trade_cards(engine)
        except Exception:
            # A stale recovery file must not make the durable store unavailable.
            # The cutover migration and KIS reconciliation can still rebuild it.
            logger.exception("Could not seed the local Kanban trade-card store")
        return engine
    except Exception:
        if engine is not None:
            engine.dispose()
        return None
