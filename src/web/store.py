from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping

from src.utils.market_calendar import US_MARKET_ZONE, nyse_regular_session_close_time


UTC = dt.timezone.utc
SYMBOL_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,19}$")
TIMEFRAMES = {"1D", "1H"}


class StoreError(RuntimeError):
    pass


class ConflictError(StoreError):
    def __init__(self, message: str, current: Mapping[str, Any] | None = None):
        super().__init__(message)
        self.current = dict(current or {})


class ValidationError(StoreError):
    pass


def utc_now() -> str:
    return dt.datetime.now(UTC).isoformat()


def normalize_symbol(value: object) -> str:
    symbol = str(value or "").strip().upper()
    if not SYMBOL_PATTERN.fullmatch(symbol):
        raise ValidationError("Invalid instrument symbol")
    return symbol


def _canonical_hash(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _finite_positive(value: object, name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{name} must be a number") from exc
    if not math.isfinite(number) or number <= 0:
        raise ValidationError(f"{name} must be positive")
    return round(number, 4)


def _plain_timestamp(value: object, name: str) -> str:
    text = str(value or "").strip()
    if not text or len(text) > 40 or "<" in text or ">" in text:
        raise ValidationError(f"{name} is invalid")
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError(f"{name} must be an ISO timestamp") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _plain_date(value: object, name: str) -> dt.date:
    text = str(value or "").strip()
    try:
        parsed = dt.date.fromisoformat(text)
    except ValueError as exc:
        raise ValidationError(f"{name} must be a YYYY-MM-DD date") from exc
    if parsed.isoformat() != text:
        raise ValidationError(f"{name} must be a YYYY-MM-DD date")
    return parsed


@dataclass(frozen=True)
class SessionRecord:
    username: str
    csrf_token: str
    expires_at: str


class WebStore:
    """Isolated SQLite authority for local auth, sandbox plans, and drawings."""

    def __init__(self, path: str | Path):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._schema_lock = threading.Lock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path, timeout=10.0, isolation_level=None, check_same_thread=False
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._schema_lock, self.connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS web_users (
                    username TEXT PRIMARY KEY,
                    password_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS web_sessions (
                    token_hash TEXT PRIMARY KEY,
                    username TEXT NOT NULL REFERENCES web_users(username),
                    csrf_token TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS login_attempts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    remote_key TEXT NOT NULL,
                    attempted_at TEXT NOT NULL,
                    succeeded INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_login_attempts_remote_time
                    ON login_attempts(remote_key, attempted_at);
                CREATE TABLE IF NOT EXISTS planning_cards (
                    symbol TEXT PRIMARY KEY,
                    stage TEXT NOT NULL CHECK(stage IN ('WATCHLIST', 'BUYLIST')),
                    breakout_price REAL,
                    watchlist_member INTEGER NOT NULL DEFAULT 1,
                    buylist_member INTEGER NOT NULL DEFAULT 0,
                    version INTEGER NOT NULL,
                    updated_at TEXT NOT NULL,
                    updated_by TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS watchlist_session_state (
                    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                    active_session_date TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS buy_today_drafts (
                    symbol TEXT PRIMARY KEY,
                    card_version INTEGER NOT NULL,
                    preview_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    updated_by TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS shared_buy_today_drafts (
                    symbol TEXT PRIMARY KEY,
                    card_version INTEGER NOT NULL,
                    preview_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    updated_by TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sandbox_commands (
                    command_id TEXT PRIMARY KEY,
                    payload_hash TEXT NOT NULL,
                    response_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    actor TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS connected_planning_commands (
                    command_id TEXT PRIMARY KEY,
                    payload_hash TEXT NOT NULL,
                    response_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    actor TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS drawings (
                    drawing_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    start_ts TEXT NOT NULL,
                    start_price REAL NOT NULL,
                    end_ts TEXT NOT NULL,
                    end_price REAL NOT NULL,
                    timeframe TEXT NOT NULL CHECK(timeframe IN ('1D', '1H')),
                    revision INTEGER NOT NULL,
                    author TEXT NOT NULL,
                    deleted INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_drawings_symbol
                    ON drawings(symbol, deleted, updated_at);
                CREATE TABLE IF NOT EXISTS retained_symbols (
                    symbol TEXT PRIMARY KEY,
                    last_used_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS web_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    symbol TEXT,
                    detail_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            planning_columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(planning_cards)"
                ).fetchall()
            }
            if "watchlist_member" not in planning_columns:
                connection.execute(
                    "ALTER TABLE planning_cards ADD COLUMN "
                    "watchlist_member INTEGER NOT NULL DEFAULT 1"
                )
            if "buylist_member" not in planning_columns:
                connection.execute(
                    "ALTER TABLE planning_cards ADD COLUMN "
                    "buylist_member INTEGER NOT NULL DEFAULT 0"
                )
                connection.execute(
                    "UPDATE planning_cards SET buylist_member=1 "
                    "WHERE stage='BUYLIST'"
                )

    def upsert_user(self, username: str, password_hash: str, *, replace: bool) -> None:
        username = username.strip().lower()
        if not username or len(username) > 80 or any(c in username for c in "<>\r\n"):
            raise ValidationError("Invalid username")
        now = utc_now()
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT username FROM web_users WHERE username=?", (username,)
            ).fetchone()
            if existing and not replace:
                connection.rollback()
                raise ConflictError("Local web user already exists")
            connection.execute(
                """
                INSERT INTO web_users(username, password_hash, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(username) DO UPDATE SET
                    password_hash=excluded.password_hash,
                    updated_at=excluded.updated_at
                """,
                (username, password_hash, now, now),
            )
            if existing:
                connection.execute(
                    "DELETE FROM web_sessions WHERE username=?", (username,)
                )
            connection.commit()

    def user_count(self) -> int:
        with self.connection() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM web_users").fetchone()[0])

    def password_hash(self, username: str) -> str | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT password_hash FROM web_users WHERE username=?",
                (username.strip().lower(),),
            ).fetchone()
        return str(row[0]) if row else None

    def login_is_limited(
        self, remote_key: str, *, window_minutes: int = 15, maximum: int = 5
    ) -> bool:
        cutoff = (dt.datetime.now(UTC) - dt.timedelta(minutes=window_minutes)).isoformat()
        with self.connection() as connection:
            count = connection.execute(
                """
                SELECT COUNT(*) FROM login_attempts
                WHERE remote_key=? AND attempted_at>=? AND succeeded=0
                """,
                (remote_key[:120], cutoff),
            ).fetchone()[0]
        return int(count) >= maximum

    def record_login_attempt(self, remote_key: str, succeeded: bool) -> None:
        with self.connection() as connection:
            connection.execute(
                "INSERT INTO login_attempts(remote_key, attempted_at, succeeded) VALUES (?, ?, ?)",
                (remote_key[:120], utc_now(), int(succeeded)),
            )
            cutoff = (dt.datetime.now(UTC) - dt.timedelta(days=2)).isoformat()
            connection.execute(
                "DELETE FROM login_attempts WHERE attempted_at<?", (cutoff,)
            )

    def create_session(
        self,
        *,
        token_hash: str,
        username: str,
        csrf_token: str,
        expires_at: str,
    ) -> None:
        now = utc_now()
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO web_sessions(
                    token_hash, username, csrf_token, created_at, expires_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (token_hash, username, csrf_token, now, expires_at, now),
            )
            connection.execute("DELETE FROM web_sessions WHERE expires_at<=?", (now,))

    def get_session(self, token_hash: str) -> SessionRecord | None:
        now = utc_now()
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT username, csrf_token, expires_at FROM web_sessions
                WHERE token_hash=? AND expires_at>?
                """,
                (token_hash, now),
            ).fetchone()
            if row:
                connection.execute(
                    "UPDATE web_sessions SET last_seen_at=? WHERE token_hash=?",
                    (now, token_hash),
                )
        if not row:
            return None
        return SessionRecord(str(row[0]), str(row[1]), str(row[2]))

    def revoke_session(self, token_hash: str) -> None:
        with self.connection() as connection:
            connection.execute("DELETE FROM web_sessions WHERE token_hash=?", (token_hash,))

    @staticmethod
    def _card(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        watchlist_member = bool(row["watchlist_member"])
        buylist_member = bool(row["buylist_member"])
        breakout_price = row["breakout_price"]
        display_stage = (
            "BUYLIST"
            if buylist_member
            else "WATCHLIST"
            if watchlist_member
            else "BREAKOUT"
            if breakout_price is not None
            else "NOT PLANNED"
        )
        return {
            "symbol": str(row["symbol"]),
            "stage": str(row["stage"]),
            "display_stage": display_stage,
            "watchlist_member": watchlist_member,
            "buylist_member": buylist_member,
            "breakout_price": breakout_price,
            "version": int(row["version"]),
            "updated_at": str(row["updated_at"]),
            "sync_state": "NOT SYNCED TO EXECUTOR",
        }

    def get_plan(self, symbol: str) -> dict[str, Any] | None:
        symbol = normalize_symbol(symbol)
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM planning_cards WHERE symbol=?", (symbol,)
            ).fetchone()
        return self._card(row)

    def list_plans(self, stage: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM planning_cards"
        params: tuple[object, ...] = ()
        if stage:
            stage = stage.strip().upper()
            if stage not in {"WATCHLIST", "BUYLIST"}:
                raise ValidationError("Unknown planning stage")
            membership_column = (
                "watchlist_member" if stage == "WATCHLIST" else "buylist_member"
            )
            query += f" WHERE {membership_column}=1"
        query += " ORDER BY updated_at DESC, symbol"
        with self.connection() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._card(row) for row in rows if row is not None]

    def rollover_watchlist_session(self, session_date: object) -> dict[str, Any]:
        """Advance the local Watchlist once and preserve the completed session.

        The first call adopts the current NYSE session without deleting existing
        state. Later session changes close every current Watchlist membership at
        the completed session's exchange close. Independent Buylist membership,
        breakout prices, Buy Today drafts, and drawings are kept.
        """

        target_day = _plain_date(session_date, "session_date")
        target_value = target_day.isoformat()
        now = utc_now()
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            state_row = connection.execute(
                "SELECT active_session_date FROM watchlist_session_state WHERE singleton=1"
            ).fetchone()
            if state_row is None:
                connection.execute(
                    """
                    INSERT INTO watchlist_session_state(
                        singleton, active_session_date, updated_at
                    ) VALUES (1, ?, ?)
                    """,
                    (target_value, now),
                )
                connection.commit()
                return {
                    "rolled_over": False,
                    "initialized": True,
                    "previous_session_date": None,
                    "session_date": target_value,
                    "archived_symbols": 0,
                }

            previous_value = str(state_row["active_session_date"])
            try:
                previous_day = _plain_date(
                    previous_value, "stored active_session_date"
                )
            except ValidationError:
                previous_day = target_day
                previous_value = target_value

            if target_day <= previous_day:
                if previous_value != str(state_row["active_session_date"]):
                    connection.execute(
                        """
                        UPDATE watchlist_session_state
                        SET active_session_date=?, updated_at=? WHERE singleton=1
                        """,
                        (target_value, now),
                    )
                connection.commit()
                return {
                    "rolled_over": False,
                    "initialized": False,
                    "previous_session_date": previous_value,
                    "session_date": max(previous_day, target_day).isoformat(),
                    "archived_symbols": 0,
                }

            rows = connection.execute(
                "SELECT symbol, version FROM planning_cards "
                "WHERE watchlist_member=1"
            ).fetchall()
            close_at = dt.datetime.combine(
                previous_day,
                nyse_regular_session_close_time(previous_day),
                tzinfo=US_MARKET_ZONE,
            ).astimezone(UTC).isoformat()
            for row in rows:
                detail = {
                    "operation": "remove_watchlist",
                    "symbol": str(row["symbol"]),
                    "expected_revision": int(row["version"]),
                    "breakout_price": None,
                    "reason": "nyse_session_rollover",
                    "completed_session_date": previous_value,
                }
                connection.execute(
                    """
                    INSERT INTO web_audit(event_type, actor, symbol, detail_json, created_at)
                    VALUES ('SANDBOX_PLANNING_COMMAND', 'market-close-rollover', ?, ?, ?)
                    """,
                    (
                        str(row["symbol"]),
                        json.dumps(detail, separators=(",", ":")),
                        close_at,
                    ),
                )

            connection.execute(
                "UPDATE planning_cards SET watchlist_member=0, version=version+1, "
                "updated_at=?, updated_by='market-close-rollover' "
                "WHERE watchlist_member=1",
                (now,),
            )
            connection.execute(
                "DELETE FROM planning_cards "
                "WHERE watchlist_member=0 AND buylist_member=0 "
                "AND breakout_price IS NULL"
            )
            connection.execute(
                """
                UPDATE watchlist_session_state
                SET active_session_date=?, updated_at=? WHERE singleton=1
                """,
                (target_value, now),
            )
            connection.execute(
                """
                INSERT INTO web_audit(event_type, actor, symbol, detail_json, created_at)
                VALUES ('WATCHLIST_SESSION_ROLLED', 'market-close-rollover', NULL, ?, ?)
                """,
                (
                    json.dumps(
                        {
                            "previous_session_date": previous_value,
                            "session_date": target_value,
                            "archived_symbols": len(rows),
                        },
                        separators=(",", ":"),
                    ),
                    now,
                ),
            )
            connection.commit()
        return {
            "rolled_over": True,
            "initialized": False,
            "previous_session_date": previous_value,
            "session_date": target_value,
            "archived_symbols": len(rows),
        }

    def list_watchlist_history(
        self, start_date: object, end_date: object
    ) -> list[dict[str, Any]]:
        """Return unique symbols whose Watchlist membership overlaps the range."""

        start_day = _plain_date(start_date, "start_date")
        end_day = _plain_date(end_date, "end_date")
        if end_day < start_day:
            raise ValidationError("end_date must be on or after start_date")
        if (end_day - start_day).days > 3660:
            raise ValidationError("Watchlist history range cannot exceed 10 years")

        range_start = dt.datetime.combine(start_day, dt.time.min, tzinfo=UTC)
        range_end = dt.datetime.combine(
            end_day + dt.timedelta(days=1), dt.time.min, tzinfo=UTC
        )
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT symbol, detail_json, created_at
                FROM web_audit
                WHERE event_type='SANDBOX_PLANNING_COMMAND' AND created_at<?
                ORDER BY created_at, id
                """,
                (range_end.isoformat(),),
            ).fetchall()

        active_since: dict[str, dt.datetime] = {}
        history: dict[str, dict[str, Any]] = {}

        def record_interval(
            symbol: str, opened_at: dt.datetime, closed_at: dt.datetime
        ) -> None:
            overlap_start = max(opened_at, range_start)
            overlap_end = min(closed_at, range_end)
            if overlap_start >= overlap_end:
                return
            last_inclusive = overlap_end - dt.timedelta(microseconds=1)
            first_seen = overlap_start.date().isoformat()
            last_seen = last_inclusive.date().isoformat()
            existing = history.get(symbol)
            if existing is None:
                history[symbol] = {
                    "symbol": symbol,
                    "first_seen_date": first_seen,
                    "last_seen_date": last_seen,
                    "watchlist_periods": 1,
                }
                return
            existing["first_seen_date"] = min(
                str(existing["first_seen_date"]), first_seen
            )
            existing["last_seen_date"] = max(
                str(existing["last_seen_date"]), last_seen
            )
            existing["watchlist_periods"] = int(existing["watchlist_periods"]) + 1

        for row in rows:
            try:
                detail = json.loads(str(row["detail_json"]))
                operation = str(detail.get("operation") or "").strip().lower()
                symbol = normalize_symbol(row["symbol"])
                timestamp = dt.datetime.fromisoformat(
                    str(row["created_at"]).replace("Z", "+00:00")
                )
                if timestamp.tzinfo is None:
                    timestamp = timestamp.replace(tzinfo=UTC)
                timestamp = timestamp.astimezone(UTC)
            except (AttributeError, TypeError, ValueError, json.JSONDecodeError, ValidationError):
                continue

            if operation in {"add_watchlist", "move_watchlist"}:
                active_since.setdefault(symbol, timestamp)
            elif operation == "remove_watchlist":
                opened_at = active_since.pop(symbol, None)
                if opened_at is None:
                    # A closing event still proves membership immediately before it,
                    # even if the opening event predates the local audit log.
                    opened_at = range_start
                record_interval(symbol, opened_at, timestamp)

        for symbol, opened_at in active_since.items():
            record_interval(symbol, opened_at, range_end)

        return sorted(
            history.values(),
            key=lambda item: (
                str(item["last_seen_date"]),
                str(item["first_seen_date"]),
                str(item["symbol"]),
            ),
            reverse=True,
        )

    def apply_planning_command(
        self,
        *,
        command_id: str,
        operation: str,
        symbol: str,
        expected_revision: int,
        actor: str,
        breakout_price: object = None,
    ) -> dict[str, Any]:
        try:
            uuid.UUID(str(command_id))
        except (TypeError, ValueError, AttributeError) as exc:
            raise ValidationError("command_id must be a UUID") from exc
        symbol = normalize_symbol(symbol)
        operation = operation.strip().lower()
        allowed = {
            "add_watchlist",
            "promote_buylist",
            "remove_buylist",
            "move_watchlist",
            "remove_watchlist",
            "set_breakout",
            "clear_breakout",
        }
        if operation not in allowed:
            raise ValidationError("Unsupported sandbox planning operation")
        if expected_revision < 0:
            raise ValidationError("expected_revision must be nonnegative")
        price = (
            _finite_positive(breakout_price, "breakout_price")
            if operation == "set_breakout"
            else None
        )
        payload = {
            "operation": operation,
            "symbol": symbol,
            "expected_revision": int(expected_revision),
            "breakout_price": price,
        }
        payload_hash = _canonical_hash(payload)
        now = utc_now()
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            prior = connection.execute(
                "SELECT payload_hash, response_json FROM sandbox_commands WHERE command_id=?",
                (str(command_id),),
            ).fetchone()
            if prior:
                if str(prior[0]) != payload_hash:
                    connection.rollback()
                    raise ConflictError("Command ID was already used with a different payload")
                response = json.loads(str(prior[1]))
                response["idempotent_replay"] = True
                connection.commit()
                return response

            row = connection.execute(
                "SELECT * FROM planning_cards WHERE symbol=?", (symbol,)
            ).fetchone()
            current = self._card(row)
            current_revision = int(row["version"]) if row else 0
            if current_revision != int(expected_revision):
                connection.rollback()
                raise ConflictError("Stale planning revision", current)

            if operation == "add_watchlist":
                if row and bool(row["watchlist_member"]):
                    connection.rollback()
                    raise ConflictError("Symbol is already in Watchlist", current)
                if row:
                    connection.execute(
                        """
                        UPDATE planning_cards
                        SET watchlist_member=1, version=version+1,
                            updated_at=?, updated_by=?
                        WHERE symbol=? AND version=?
                        """,
                        (now, actor, symbol, expected_revision),
                    )
                else:
                    connection.execute(
                        """
                        INSERT INTO planning_cards(
                            symbol, stage, breakout_price, watchlist_member,
                            buylist_member, version, updated_at, updated_by
                        ) VALUES (?, 'WATCHLIST', NULL, 1, 0, 1, ?, ?)
                        """,
                        (symbol, now, actor),
                    )
            elif operation == "remove_watchlist":
                if not row or not bool(row["watchlist_member"]):
                    connection.rollback()
                    raise ConflictError("Symbol is not in Watchlist", current)
                if bool(row["buylist_member"]) or row["breakout_price"] is not None:
                    connection.execute(
                        """
                        UPDATE planning_cards
                        SET watchlist_member=0, version=version+1,
                            updated_at=?, updated_by=?
                        WHERE symbol=? AND version=?
                        """,
                        (now, actor, symbol, expected_revision),
                    )
                else:
                    connection.execute(
                        "DELETE FROM planning_cards WHERE symbol=? AND version=?",
                        (symbol, expected_revision),
                    )
            elif operation == "set_breakout" and not row:
                connection.execute(
                    """
                    INSERT INTO planning_cards(
                        symbol, stage, breakout_price, watchlist_member,
                        buylist_member, version, updated_at, updated_by
                    ) VALUES (?, 'WATCHLIST', ?, 0, 0, 1, ?, ?)
                    """,
                    (symbol, price, now, actor),
                )
            else:
                if not row:
                    connection.rollback()
                    raise ConflictError("No planning card exists for this symbol")
                if operation == "promote_buylist":
                    if bool(row["buylist_member"]):
                        connection.rollback()
                        raise ConflictError("Symbol is already in Buylist", current)
                    try:
                        existing_breakout = float(row["breakout_price"])
                    except (TypeError, ValueError, OverflowError):
                        existing_breakout = 0.0
                    if not math.isfinite(existing_breakout) or existing_breakout <= 0:
                        connection.rollback()
                        raise ConflictError(
                            "Set a breakout price before adding this symbol to Buylist",
                            current,
                        )
                    updates = ("BUYLIST", row["breakout_price"], row["watchlist_member"], 1)
                elif operation == "remove_buylist":
                    if not bool(row["buylist_member"]):
                        connection.rollback()
                        raise ConflictError("Symbol is not in Buylist", current)
                    updates = (
                        "WATCHLIST",
                        row["breakout_price"],
                        row["watchlist_member"],
                        0,
                    )
                elif operation == "move_watchlist":
                    if not bool(row["buylist_member"]):
                        connection.rollback()
                        raise ConflictError("Symbol is not in Buylist", current)
                    updates = ("WATCHLIST", row["breakout_price"], row["watchlist_member"], 0)
                elif operation == "set_breakout":
                    updates = (
                        row["stage"],
                        price,
                        row["watchlist_member"],
                        row["buylist_member"],
                    )
                else:
                    if bool(row["buylist_member"]):
                        updates = ("WATCHLIST", None, row["watchlist_member"], 0)
                    elif not bool(row["watchlist_member"]):
                        connection.execute(
                            "DELETE FROM planning_cards WHERE symbol=? AND version=?",
                            (symbol, expected_revision),
                        )
                        updates = None
                    else:
                        updates = (row["stage"], None, row["watchlist_member"], 0)
                if updates is None:
                    changed = 1
                else:
                    changed = connection.execute(
                        """
                        UPDATE planning_cards
                        SET stage=?, breakout_price=?, watchlist_member=?,
                            buylist_member=?, version=version+1,
                            updated_at=?, updated_by=?
                        WHERE symbol=? AND version=?
                        """,
                        (*updates, now, actor, symbol, expected_revision),
                    ).rowcount
                if changed != 1:
                    latest = connection.execute(
                        "SELECT * FROM planning_cards WHERE symbol=?", (symbol,)
                    ).fetchone()
                    connection.rollback()
                    raise ConflictError("Concurrent planning update", self._card(latest))

            result_row = connection.execute(
                "SELECT * FROM planning_cards WHERE symbol=?", (symbol,)
            ).fetchone()
            response = {
                "status": "SAVED LOCALLY",
                "card": self._card(result_row),
                "idempotent_replay": False,
            }
            response_json = json.dumps(response, separators=(",", ":"))
            connection.execute(
                """
                INSERT INTO sandbox_commands(
                    command_id, payload_hash, response_json, created_at, actor
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (str(command_id), payload_hash, response_json, now, actor),
            )
            connection.execute(
                """
                INSERT INTO web_audit(event_type, actor, symbol, detail_json, created_at)
                VALUES ('SANDBOX_PLANNING_COMMAND', ?, ?, ?, ?)
                """,
                (actor, symbol, json.dumps(payload, separators=(",", ":")), now),
            )
            connection.commit()
            return response

    def connected_command_replay(
        self, *, command_id: str, payload: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        try:
            uuid.UUID(str(command_id))
        except (TypeError, ValueError, AttributeError) as exc:
            raise ValidationError("command_id must be a UUID") from exc
        payload_hash = _canonical_hash(payload)
        with self.connection() as connection:
            row = connection.execute(
                "SELECT payload_hash, response_json FROM connected_planning_commands "
                "WHERE command_id=?",
                (str(command_id),),
            ).fetchone()
        if row is None:
            return None
        if str(row["payload_hash"]) != payload_hash:
            raise ConflictError("Command ID was already used with a different payload")
        response = json.loads(str(row["response_json"]))
        response["idempotent_replay"] = True
        return response

    def record_connected_command(
        self,
        *,
        command_id: str,
        payload: Mapping[str, Any],
        response: Mapping[str, Any],
        actor: str,
    ) -> dict[str, Any]:
        payload_hash = _canonical_hash(payload)
        response_payload = dict(response)
        response_json = json.dumps(response_payload, default=str, separators=(",", ":"))
        now = utc_now()
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            prior = connection.execute(
                "SELECT payload_hash, response_json FROM connected_planning_commands "
                "WHERE command_id=?",
                (str(command_id),),
            ).fetchone()
            if prior is not None:
                if str(prior["payload_hash"]) != payload_hash:
                    connection.rollback()
                    raise ConflictError("Command ID was already used with a different payload")
                replay = json.loads(str(prior["response_json"]))
                replay["idempotent_replay"] = True
                connection.commit()
                return replay
            connection.execute(
                "INSERT INTO connected_planning_commands(" 
                "command_id, payload_hash, response_json, created_at, actor" 
                ") VALUES (?, ?, ?, ?, ?)",
                (str(command_id), payload_hash, response_json, now, actor),
            )
            connection.execute(
                "INSERT INTO web_audit(event_type, actor, symbol, detail_json, created_at) "
                "VALUES ('CONNECTED_PLANNING_COMMAND', ?, ?, ?, ?)",
                (
                    actor,
                    str(payload.get("symbol") or ""),
                    json.dumps(dict(payload), default=str, separators=(",", ":")),
                    now,
                ),
            )
            connection.commit()
        return response_payload

    def save_buy_today_preview(
        self, symbol: str, expected_revision: int, actor: str
    ) -> dict[str, Any]:
        card = self.get_plan(symbol)
        if not card or card["stage"] != "BUYLIST":
            raise ConflictError("Buy Today preview requires a Buylist card", card)
        if card["version"] != expected_revision:
            raise ConflictError("Stale planning revision", card)
        preview = {
            "symbol": card["symbol"],
            "breakout_price": card["breakout_price"],
            "executable": False,
            "label": "LOCAL DRAFT — NEVER ACTIVATED",
        }
        now = utc_now()
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO buy_today_drafts(
                    symbol, card_version, preview_json, updated_at, updated_by
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(symbol) DO UPDATE SET
                    card_version=excluded.card_version,
                    preview_json=excluded.preview_json,
                    updated_at=excluded.updated_at,
                    updated_by=excluded.updated_by
                """,
                (card["symbol"], card["version"], json.dumps(preview), now, actor),
            )
        return preview

    def cancel_buy_today_preview(
        self, symbol: str, expected_revision: int, actor: str
    ) -> bool:
        """Remove a local Buy Today draft without changing its Buylist card."""
        symbol = normalize_symbol(symbol)
        if expected_revision < 1:
            raise ValidationError("expected_revision must be positive")
        now = utc_now()
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM planning_cards WHERE symbol=?", (symbol,)
            ).fetchone()
            card = self._card(row)
            removed = connection.execute(
                "DELETE FROM buy_today_drafts WHERE symbol=?",
                (symbol,),
            ).rowcount == 1
            if removed:
                detail = {
                    "operation": "cancel_buy_today_draft",
                    "symbol": symbol,
                    "expected_revision": int(expected_revision),
                }
                connection.execute(
                    """
                    INSERT INTO web_audit(event_type, actor, symbol, detail_json, created_at)
                    VALUES ('BUY_TODAY_DRAFT_CANCELLED', ?, ?, ?, ?)
                    """,
                    (actor, symbol, json.dumps(detail, separators=(",", ":")), now),
                )
            connection.commit()
        return removed

    def list_buy_today_drafts(self) -> list[dict[str, Any]]:
        """Return only drafts that still match the current Buylist revision."""
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT d.symbol, d.card_version, d.preview_json,
                       d.updated_at, d.updated_by, p.breakout_price
                FROM buy_today_drafts AS d
                JOIN planning_cards AS p
                  ON p.symbol=d.symbol
                 AND p.stage='BUYLIST'
                 AND p.version=d.card_version
                ORDER BY d.updated_at DESC, d.symbol
                """
            ).fetchall()
        drafts: list[dict[str, Any]] = []
        for row in rows:
            preview = json.loads(str(row["preview_json"]))
            drafts.append(
                {
                    "symbol": str(row["symbol"]),
                    "stage": "BUY_TODAY_DRAFT",
                    "card_version": int(row["card_version"]),
                    "breakout_price": row["breakout_price"],
                    "executable": False,
                    "label": str(preview.get("label") or "LOCAL DRAFT"),
                    "updated_at": str(row["updated_at"]),
                    "updated_by": str(row["updated_by"]),
                    "sync_state": "NOT SYNCED TO EXECUTOR",
                }
            )
        return drafts

    def save_shared_buy_today_preview(
        self,
        card: dict[str, Any],
        expected_revision: int,
        actor: str,
    ) -> dict[str, Any]:
        """Save a browser-shared draft without publishing canonical Buy Today."""

        symbol = normalize_symbol(card.get("symbol"))
        version = int(card.get("version") or 0)
        if not bool(card.get("buylist_member")) or str(
            card.get("canonical_stage") or card.get("stage") or ""
        ).upper() != "BUYLIST":
            raise ConflictError("Buy Today preview requires a Buylist card", card)
        if version != int(expected_revision):
            raise ConflictError("Stale planning revision", card)
        preview = {
            "symbol": symbol,
            "breakout_price": card.get("breakout_price"),
            "executable": False,
            "label": "SHARED WEB DRAFT - NEVER ACTIVATED",
        }
        now = utc_now()
        detail = {
            "operation": "save_shared_buy_today_draft",
            "symbol": symbol,
            "expected_revision": version,
        }
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO shared_buy_today_drafts(
                    symbol, card_version, preview_json, updated_at, updated_by
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(symbol) DO UPDATE SET
                    card_version=excluded.card_version,
                    preview_json=excluded.preview_json,
                    updated_at=excluded.updated_at,
                    updated_by=excluded.updated_by
                """,
                (symbol, version, json.dumps(preview), now, actor),
            )
            connection.execute(
                """
                INSERT INTO web_audit(event_type, actor, symbol, detail_json, created_at)
                VALUES ('SHARED_BUY_TODAY_DRAFT_SAVED', ?, ?, ?, ?)
                """,
                (actor, symbol, json.dumps(detail, separators=(",", ":")), now),
            )
            connection.commit()
        return preview

    def cancel_shared_buy_today_preview(
        self,
        card: dict[str, Any],
        expected_revision: int,
        actor: str,
    ) -> bool:
        """Cancel a shared draft while leaving the canonical card unchanged."""

        symbol = normalize_symbol(card.get("symbol"))
        version = int(card.get("version") or 0)
        if int(expected_revision) < 1:
            raise ValidationError("expected_revision must be positive")
        now = utc_now()
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            removed = connection.execute(
                "DELETE FROM shared_buy_today_drafts WHERE symbol=?",
                (symbol,),
            ).rowcount == 1
            if removed:
                detail = {
                    "operation": "cancel_shared_buy_today_draft",
                    "symbol": symbol,
                    "expected_revision": version,
                }
                connection.execute(
                    """
                    INSERT INTO web_audit(event_type, actor, symbol, detail_json, created_at)
                    VALUES ('SHARED_BUY_TODAY_DRAFT_CANCELLED', ?, ?, ?, ?)
                    """,
                    (actor, symbol, json.dumps(detail, separators=(",", ":")), now),
                )
            connection.commit()
        return removed

    def list_shared_buy_today_drafts(self) -> list[dict[str, Any]]:
        """Return shared drafts for validation against a canonical snapshot."""

        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT symbol, card_version, preview_json, updated_at, updated_by
                FROM shared_buy_today_drafts ORDER BY updated_at DESC, symbol
                """
            ).fetchall()
        drafts: list[dict[str, Any]] = []
        for row in rows:
            try:
                preview = json.loads(str(row["preview_json"]))
            except (TypeError, ValueError, json.JSONDecodeError):
                preview = {}
            drafts.append(
                {
                    "symbol": str(row["symbol"]),
                    "stage": "BUY_TODAY_DRAFT",
                    "card_version": int(row["card_version"]),
                    "breakout_price": preview.get("breakout_price"),
                    "executable": False,
                    "label": str(
                        preview.get("label")
                        or "SHARED WEB DRAFT - NEVER ACTIVATED"
                    ),
                    "updated_at": str(row["updated_at"]),
                    "updated_by": str(row["updated_by"]),
                    "sync_state": "SHARED WEB DRAFT / NOT EXECUTABLE",
                }
            )
        return drafts

    def buy_today_draft_revision(self, *, shared: bool = False) -> str:
        table = "shared_buy_today_drafts" if shared else "buy_today_drafts"
        with self.connection() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS row_count, MAX(updated_at) AS updated_at "
                f"FROM {table}"
            ).fetchone()
        return f"{int(row['row_count'] or 0)}:{str(row['updated_at'] or '')}"

    @staticmethod
    def _drawing(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": str(row["drawing_id"]),
            "symbol": str(row["symbol"]),
            "type": "line",
            "start_date": str(row["start_ts"]),
            "start_price": float(row["start_price"]),
            "end_date": str(row["end_ts"]),
            "end_price": float(row["end_price"]),
            "timeframe": str(row["timeframe"]),
            "revision": int(row["revision"]),
            "author": str(row["author"]),
            "deleted": bool(row["deleted"]),
            "updated_at": str(row["updated_at"]),
        }

    def list_drawings(
        self, symbol: str, *, include_deleted: bool = False
    ) -> list[dict[str, Any]]:
        symbol = normalize_symbol(symbol)
        query = "SELECT * FROM drawings WHERE symbol=?"
        if not include_deleted:
            query += " AND deleted=0"
        query += " ORDER BY updated_at, drawing_id"
        with self.connection() as connection:
            rows = connection.execute(query, (symbol,)).fetchall()
        return [self._drawing(row) for row in rows]

    def create_drawing(
        self, payload: Mapping[str, Any], *, actor: str
    ) -> dict[str, Any]:
        symbol = normalize_symbol(payload.get("symbol"))
        drawing_id = str(payload.get("id") or uuid.uuid4())
        if len(drawing_id) > 120 or any(c in drawing_id for c in "<>\r\n"):
            raise ValidationError("Invalid drawing ID")
        timeframe = str(payload.get("timeframe") or "1D").upper()
        if timeframe not in TIMEFRAMES:
            raise ValidationError("timeframe must be 1D or 1H")
        values = (
            drawing_id,
            symbol,
            _plain_timestamp(payload.get("start_date"), "start_date"),
            _finite_positive(payload.get("start_price"), "start_price"),
            _plain_timestamp(payload.get("end_date"), "end_date"),
            _finite_positive(payload.get("end_price"), "end_price"),
            timeframe,
            actor,
            utc_now(),
        )
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM drawings WHERE drawing_id=?", (drawing_id,)
            ).fetchone()
            if existing:
                connection.rollback()
                raise ConflictError(
                    "Drawing ID already exists; deleted drawings cannot be revived",
                    self._drawing(existing),
                )
            connection.execute(
                """
                INSERT INTO drawings(
                    drawing_id, symbol, start_ts, start_price, end_ts, end_price,
                    timeframe, revision, author, deleted, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, 0, ?)
                """,
                values,
            )
            row = connection.execute(
                "SELECT * FROM drawings WHERE drawing_id=?", (drawing_id,)
            ).fetchone()
            connection.execute(
                """
                INSERT INTO web_audit(event_type, actor, symbol, detail_json, created_at)
                VALUES ('DRAWING_CREATED', ?, ?, ?, ?)
                """,
                (actor, symbol, json.dumps({"id": drawing_id}), utc_now()),
            )
            connection.commit()
        return self._drawing(row)

    def update_drawing(
        self, drawing_id: str, payload: Mapping[str, Any], *, actor: str
    ) -> dict[str, Any]:
        expected = int(payload.get("expected_revision", -1))
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT * FROM drawings WHERE drawing_id=?", (drawing_id,)
            ).fetchone()
            if not current:
                connection.rollback()
                raise ConflictError("Drawing does not exist")
            current_value = self._drawing(current)
            if bool(current["deleted"]):
                connection.rollback()
                raise ConflictError("Deleted drawing cannot be edited", current_value)
            if int(current["revision"]) != expected:
                connection.rollback()
                raise ConflictError("Stale drawing revision", current_value)
            timeframe = str(payload.get("timeframe", current["timeframe"])).upper()
            if timeframe not in TIMEFRAMES:
                connection.rollback()
                raise ValidationError("timeframe must be 1D or 1H")
            changed = connection.execute(
                """
                UPDATE drawings SET
                    start_ts=?, start_price=?, end_ts=?, end_price=?, timeframe=?,
                    revision=revision+1, author=?, updated_at=?
                WHERE drawing_id=? AND revision=? AND deleted=0
                """,
                (
                    _plain_timestamp(payload.get("start_date"), "start_date"),
                    _finite_positive(payload.get("start_price"), "start_price"),
                    _plain_timestamp(payload.get("end_date"), "end_date"),
                    _finite_positive(payload.get("end_price"), "end_price"),
                    timeframe,
                    actor,
                    utc_now(),
                    drawing_id,
                    expected,
                ),
            ).rowcount
            if changed != 1:
                connection.rollback()
                raise ConflictError("Concurrent drawing update", current_value)
            row = connection.execute(
                "SELECT * FROM drawings WHERE drawing_id=?", (drawing_id,)
            ).fetchone()
            connection.commit()
        return self._drawing(row)

    def delete_drawing(
        self, drawing_id: str, *, expected_revision: int, actor: str
    ) -> dict[str, Any]:
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT * FROM drawings WHERE drawing_id=?", (drawing_id,)
            ).fetchone()
            if not current:
                connection.rollback()
                raise ConflictError("Drawing does not exist")
            current_value = self._drawing(current)
            if int(current["revision"]) != int(expected_revision):
                connection.rollback()
                raise ConflictError("Stale drawing revision", current_value)
            if not bool(current["deleted"]):
                connection.execute(
                    """
                    UPDATE drawings SET deleted=1, revision=revision+1,
                        author=?, updated_at=? WHERE drawing_id=? AND revision=?
                    """,
                    (actor, utc_now(), drawing_id, expected_revision),
                )
            row = connection.execute(
                "SELECT * FROM drawings WHERE drawing_id=?", (drawing_id,)
            ).fetchone()
            connection.commit()
        return self._drawing(row)

    def touch_retained_symbol(self, symbol: str) -> None:
        symbol = normalize_symbol(symbol)
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO retained_symbols(symbol, last_used_at) VALUES (?, ?)
                ON CONFLICT(symbol) DO UPDATE SET last_used_at=excluded.last_used_at
                """,
                (symbol, utc_now()),
            )

    def pinned_symbols(self) -> set[str]:
        with self.connection() as connection:
            planning = connection.execute("SELECT symbol FROM planning_cards").fetchall()
            retained = connection.execute(
                "SELECT symbol FROM retained_symbols ORDER BY last_used_at DESC LIMIT 50"
            ).fetchall()
        return {str(row[0]) for row in (*planning, *retained)}
