from __future__ import annotations

import datetime as dt
import json
import math
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import BigInteger, Column, DateTime, MetaData, String, Table, Text, text
from sqlalchemy.engine import Engine

from src.core.trade_card_state import BoardStatus, TradeCardState, can_withdraw_sell_all_intent
from src.core.buy_today_feedback import buy_today_feedback_is_current
from src.services.monitor_equity import read_monitor_equity_snapshot
from src.infrastructure.database.coordination_engine import (
    create_coordination_connection_engine,
    normalize_coordination_database_config,
)
from src.utils.market_calendar import current_or_next_nyse_session_date

from .config import WebConfig
from .executor_status import executor_status
from .store import normalize_symbol


_COORDINATION_KEYS = (
    "COORD_DB_BACKEND",
    "COORD_DB_HOST",
    "COORD_DB_PORT",
    "COORD_DB_USER",
    "COORD_DB_PASSWORD",
    "COORD_DB_NAME",
    "COORD_DB_SSL_CA",
    "COORD_DB_SCHEMA",
)


class CanonicalPlanningUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class _Snapshot:
    cards: tuple[TradeCardState, ...]
    revision: str
    loaded_at: float


def _read_json_mapping(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"Configuration must be an object: {path}")
    return value


def _read_env_mapping(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _coordination_values(repository: Path) -> dict[str, str]:
    values: dict[str, Any] = {}
    values.update(_read_json_mapping(repository / "config" / "runtime.json"))
    values.update(_read_json_mapping(repository / "config" / "runtime.local.json"))
    values.update(_read_env_mapping(repository / ".env"))
    selected = {key: str(values.get(key) or "").strip() for key in _COORDINATION_KEYS}
    ca_value = selected["COORD_DB_SSL_CA"]
    if ca_value:
        ca_path = Path(ca_value).expanduser()
        if not ca_path.is_absolute():
            ca_path = repository / ca_path
        selected["COORD_DB_SSL_CA"] = str(ca_path.resolve())
    return selected


def _coordination_engine(repository: Path, *, read_only: bool) -> Engine:
    values = _coordination_values(repository)
    required = ("COORD_DB_HOST", "COORD_DB_USER", "COORD_DB_PASSWORD", "COORD_DB_NAME")
    if any(not values[key] for key in required):
        raise CanonicalPlanningUnavailable(
            "The PC coordination database credentials are incomplete"
        )
    engine = create_coordination_connection_engine(
        normalize_coordination_database_config(values), read_only=read_only
    )
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception:
        engine.dispose()
        raise
    return engine


def _build_read_engine(repository: Path) -> Engine:
    return _coordination_engine(repository, read_only=True)


def build_canonical_write_engine(repository: Path) -> Engine:
    """Build the canonical transactional engine without schema bootstrap."""

    return _coordination_engine(repository, read_only=False)


class CanonicalPlanningSource:
    """Read-only projection of the PC's canonical TradeCards for mobile use.

    This adapter intentionally contains no mutation method and never calls a
    repository schema bootstrap. Its engine is used only for bounded SELECTs.
    """

    def __init__(
        self,
        *,
        enabled: bool,
        environment: str = "PROD",
        account_no: str = "",
        engine: Engine | None = None,
        unavailable_reason: str = "Canonical planning reads are disabled",
        cache_seconds: float = 2.0,
        equity_path: Path | None = None,
    ) -> None:
        self.enabled = bool(enabled)
        self.environment = str(environment or "PROD").strip().upper()
        self.configured_account_no = str(account_no or "").strip()
        self.engine = engine
        self.unavailable_reason = str(unavailable_reason or "Canonical planning unavailable")
        self.cache_seconds = max(0.0, float(cache_seconds))
        self.equity_path = equity_path
        self._resolved_account_no = ""
        self._snapshot: _Snapshot | None = None
        self._lock = threading.RLock()

    @classmethod
    def from_config(cls, config: WebConfig) -> "CanonicalPlanningSource":
        if config.mode != "CONNECTED" or not config.canonical_planning_reads:
            return cls(enabled=False)
        repository = config.resolved_pc_repository
        if repository is None or not repository.is_dir():
            return cls(
                enabled=True,
                environment=config.canonical_environment,
                account_no=config.canonical_account_no,
                unavailable_reason="The configured PC repository is unavailable",
            )
        try:
            engine = _build_read_engine(repository)
        except Exception as exc:
            return cls(
                enabled=True,
                environment=config.canonical_environment,
                account_no=config.canonical_account_no,
                unavailable_reason=f"Canonical planning database unavailable ({type(exc).__name__})",
            )
        return cls(
            enabled=True,
            environment=config.canonical_environment,
            account_no=config.canonical_account_no,
            engine=engine,
            equity_path=repository / "data" / "monitor_equity.json",
        )

    @property
    def available(self) -> bool:
        return bool(self.enabled and self.engine is not None)

    @property
    def source_name(self) -> str:
        if self.engine is not None and self.engine.dialect.name == "postgresql":
            return "CANONICAL_SUPABASE"
        return "CANONICAL_LOCAL" if self.engine is not None else "CANONICAL_UNAVAILABLE"

    def close(self) -> None:
        if self.engine is not None:
            self.engine.dispose()

    def _account_no(self, connection) -> str:
        if self._resolved_account_no:
            return self._resolved_account_no
        configured = self.configured_account_no
        if configured:
            exists = connection.execute(
                text(
                    "SELECT 1 FROM trade_cards "
                    "WHERE environment=:environment AND account_no=:account_no LIMIT 1"
                ),
                {"environment": self.environment, "account_no": configured},
            ).first()
            if exists is None:
                raise CanonicalPlanningUnavailable(
                    "The configured canonical account has no TradeCards"
                )
            self._resolved_account_no = configured
            return configured
        accounts = [
            str(value)
            for value in connection.execute(
                text(
                    "SELECT DISTINCT account_no FROM trade_cards "
                    "WHERE environment=:environment ORDER BY account_no LIMIT 2"
                ),
                {"environment": self.environment},
            ).scalars()
        ]
        if len(accounts) != 1:
            raise CanonicalPlanningUnavailable(
                "Set canonical_account_no because the canonical scope is not unambiguous"
            )
        self._resolved_account_no = accounts[0]
        return accounts[0]

    @staticmethod
    def _card_from_row(row) -> TradeCardState:
        raw = json.loads(row.payload) if isinstance(row.payload, str) else dict(row.payload)
        raw["version"] = int(row.version)
        return TradeCardState.from_dict(raw)

    def _load_snapshot(self, *, force: bool = False) -> _Snapshot:
        if not self.available or self.engine is None:
            raise CanonicalPlanningUnavailable(self.unavailable_reason)
        with self._lock:
            now = time.monotonic()
            if (
                not force
                and self.cache_seconds > 0
                and self._snapshot is not None
                and now - self._snapshot.loaded_at <= self.cache_seconds
            ):
                return self._snapshot
            try:
                with self.engine.connect() as connection:
                    account_no = self._account_no(connection)
                    parameters = {"environment": self.environment, "account_no": account_no}
                    if not force and self._snapshot is not None:
                        stamp = connection.execute(
                            text(
                                "SELECT COUNT(*) AS card_count, COALESCE(SUM(version), 0) AS version_sum, "
                                "MAX(updated_at) AS newest FROM trade_cards "
                                "WHERE environment=:environment AND account_no=:account_no"
                            ), parameters,
                        ).one()
                        revision = self._revision(stamp.card_count, stamp.version_sum, stamp.newest)
                        if revision == self._snapshot.revision:
                            self._snapshot = _Snapshot(self._snapshot.cards, revision, now)
                            return self._snapshot
                from src.services.coordination_snapshot import read_versioned_rows

                table = Table(
                    "trade_cards", MetaData(),
                    Column("environment", String(10)),
                    Column("account_no", String(32)),
                    Column("symbol", String(20)),
                    Column("board_status", String(32)),
                    Column("payload", Text),
                    Column("version", BigInteger),
                    Column("updated_at", DateTime),
                )
                rows = read_versioned_rows(
                    self.engine, table,
                    cache_key=("web_trade_cards", self.environment, account_no),
                    key_columns=("environment", "account_no", "symbol"),
                    revision_column="version",
                    conditions=(table.c.environment == self.environment, table.c.account_no == account_no),
                )
            except CanonicalPlanningUnavailable:
                raise
            except Exception as exc:
                raise CanonicalPlanningUnavailable(
                    f"Canonical planning read failed ({type(exc).__name__})"
                ) from exc
            cards = tuple(self._card_from_row(row) for row in rows)
            version_sum = sum(int(card.version or 0) for card in cards)
            newest = max(
                (row.updated_at for row in rows if row.updated_at is not None),
                default=None,
            )
            revision = self._revision(len(cards), version_sum, newest)
            self._snapshot = _Snapshot(cards, revision, now)
            return self._snapshot

    @staticmethod
    def _revision(count, version_sum, newest) -> str:
        if isinstance(newest, str):
            try:
                newest = dt.datetime.fromisoformat(newest)
            except ValueError:
                pass
        newest_text = newest.isoformat() if newest is not None and hasattr(newest, "isoformat") else str(newest or "")
        return f"{int(count)}:{int(version_sum)}:{newest_text}"

    @staticmethod
    def _positive_number(value: object) -> float | None:
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        return number if math.isfinite(number) and number > 0 else None

    def _project(self, card: TradeCardState) -> dict[str, Any]:
        active_session = current_or_next_nyse_session_date()
        watchlist_member = bool(
            card.watchlist_member
            and card.watchlist_session_date is not None
            and card.watchlist_session_date == active_session
        )
        buylist_member = bool(
            card.buylist_member
            and card.board_status
            in {
                BoardStatus.BUYLIST,
                BoardStatus.BUY_TODAY,
                BoardStatus.ENTRY_PENDING,
                BoardStatus.OPEN_POSITION,
                BoardStatus.PARTIAL_SELL,
                BoardStatus.SELL_ALL,
            }
        )
        buy_today_member = card.board_status == BoardStatus.BUY_TODAY
        quantity = max(0, int(card.broker_quantity or 0))
        remaining = max(0, int(card.entry_remaining_target_quantity or 0))
        purchase_status = (
            "PARTIALLY_BOUGHT"
            if quantity and remaining
            else "BOUGHT"
            if quantity
            else "SOLD"
            if card.board_status == BoardStatus.CLOSED
            and card.session_date == active_session and card.average_entry_price > 0
            else "ENTRY_PENDING"
            if card.board_status == BoardStatus.ENTRY_PENDING
            else ""
        )
        # Display completed entries without restoring executable Buy Today intent.
        buy_today_display_member = buy_today_member or bool(
            card.session_date == active_session and purchase_status
        )
        breakout_price = self._positive_number(card.breakout_price)
        stage = (
            "BUYLIST"
            if buylist_member
            else "WATCHLIST"
            if watchlist_member
            else "BREAKOUT"
            if breakout_price is not None
            else "NOT PLANNED"
        )
        return {
            "symbol": card.symbol,
            "name": card.name or card.symbol,
            "stage": stage,
            "display_stage": (
                stage
                if stage in {"BREAKOUT", "NOT PLANNED"}
                else card.board_status.value
            ),
            "canonical_stage": card.board_status.value,
            "watchlist_member": watchlist_member,
            "buylist_member": buylist_member,
            "buy_today_member": buy_today_member,
            "buy_today_display_member": buy_today_display_member,
            "entry_cancellation_pending": bool(
                card.entry_block_reason == "cancel_requested"
                or card.entry_cancel_in_flight
            ),
            "purchase_status": purchase_status,
            "broker_quantity": quantity,
            "entry_remaining_target_quantity": remaining,
            "average_entry_price": self._positive_number(card.average_entry_price),
            "breakout_price": breakout_price,
            "is_ep": card.is_ep,
            "version": int(card.version),
            "card_version": int(card.version),
            "updated_at": card.updated_at.isoformat(),
            "session_date": card.session_date.isoformat() if card.session_date else None,
            "watchlist_session_date": (
                card.watchlist_session_date.isoformat()
                if card.watchlist_session_date
                else None
            ),
            "source": self.source_name,
        }

    def list_plans(
        self, stage: str | None = None, *, force: bool = False
    ) -> dict[str, Any]:
        snapshot = self._load_snapshot(force=force)
        rows = [self._project(card) for card in snapshot.cards]
        rows = [
            row
            for row in rows
            if row["watchlist_member"]
            or row["buylist_member"]
            or row["buy_today_member"]
            or row["buy_today_display_member"]
        ]
        if stage:
            stage_key = str(stage).strip().upper()
            membership_key = {
                "WATCHLIST": "watchlist_member",
                "BUYLIST": "buylist_member",
                "BUY_TODAY": "buy_today_member",
            }.get(stage_key)
            rows = [row for row in rows if membership_key and row[membership_key]]
        rows.sort(key=lambda row: (row["symbol"]))
        return {"rows": rows, "revision": snapshot.revision}

    def list_buy_today(self) -> dict[str, Any]:
        snapshot = self._load_snapshot()
        rows = [self._project(card) for card in snapshot.cards]
        rows = [row for row in rows if row["buy_today_display_member"]]
        rows.sort(key=lambda row: row["symbol"])
        return {"rows": rows, "revision": snapshot.revision}

    def orb_monitor_inputs(self) -> dict[str, Any]:
        """Read account scope and shared settings without write authority or bootstrap."""
        if not self.available:
            raise CanonicalPlanningUnavailable(self.unavailable_reason)
        with self._lock, self.engine.connect() as connection:
            account_no = self._account_no(connection)
            payload = connection.execute(
                text("SELECT payload FROM app_state_sync WHERE state_key=:key"),
                {"key": "settings"},
            ).scalar()
        document = json.loads(payload) if isinstance(payload, str) else payload
        if document is not None and not isinstance(document, dict):
            raise CanonicalPlanningUnavailable("Shared settings are invalid")
        return {"account_no": account_no, "document": document}

    @staticmethod
    def _enum_value(value: object) -> str | None:
        if value is None:
            return None
        return str(getattr(value, "value", value) or "") or None

    @staticmethod
    def _date_value(value: object) -> str | None:
        return value.isoformat() if value is not None and hasattr(value, "isoformat") else None

    def _project_board_card(self, card: TradeCardState) -> dict[str, Any]:
        """Return the canonical Kanban facts needed by the mobile controller.

        This is deliberately a projection, not ``TradeCardState.to_dict()``:
        account identifiers, client order ids, and unrelated recovery fields
        never need to enter the browser.
        """

        return {
            "symbol": card.symbol,
            "name": card.name or card.symbol,
            "board_status": card.board_status.value,
            "previous_board_status": self._enum_value(card.previous_board_status),
            "version": int(card.version),
            "kanban_priority": int(card.kanban_priority or 0),
            "risk_percent": float(card.risk_percent),
            "breakout_price": self._positive_number(card.breakout_price),
            "is_ep": card.is_ep,
            "buffer_pct": float(card.buffer_pct or 0.0),
            "session_date": self._date_value(card.session_date),
            "selected_orb_window": card.selected_orb_window,
            "entry_orb_window": card.entry_orb_window,
            "entry_orb_high": card.entry_orb_high,
            "entry_orb_low": card.entry_orb_low,
            "entry_trigger": card.entry_trigger,
            "entry_execution_price": card.entry_execution_price,
            "entry_breakout_trigger": card.entry_breakout_trigger,
            "entry_remaining_target_quantity": max(
                0, int(card.entry_remaining_target_quantity or 0)
            ),
            "next_retry_at": self._date_value(card.next_retry_at),
            "entry_runtime_status": self._enum_value(card.entry_runtime_status),
            "entry_block_reason": str(card.entry_block_reason or ""),
            "buy_today_note": (
                str(card.buy_today_note or "")
                if card.board_status == BoardStatus.BUYLIST
                and buy_today_feedback_is_current(card)
                else ""
            ),
            "entry_order_pending": bool(
                card.entry_client_order_id
                or card.entry_pending_attempt_number
                or card.entry_submission_unresolved
            ),
            "entry_cancel_in_flight": bool(card.entry_cancel_in_flight),
            "entry_remaining_target_quantity": max(0, int(card.entry_remaining_target_quantity or 0)),
            "planned_quantity": max(0, int(card.planned_quantity or 0)),
            "target_position_quantity": max(
                0, int(card.target_position_quantity or 0)
            ),
            "position_runtime_status": self._enum_value(
                card.position_runtime_status
            ),
            "broker_quantity": max(0, int(card.broker_quantity or 0)),
            "orderable_quantity": max(0, int(card.orderable_quantity or 0)),
            "average_entry_price": float(card.average_entry_price or 0.0),
            "stop_type": self._enum_value(card.stop_type),
            "active_stop_price": card.active_stop_price,
            "stop_quantity": max(0, int(card.stop_quantity or 0)),
            "pending_stop_type": self._enum_value(card.pending_stop_type),
            "pending_stop_price": card.pending_stop_price,
            "pending_stop_quantity": max(0, int(card.pending_stop_quantity or 0)),
            "pending_partial_sell_quantity": max(
                0, int(card.pending_partial_sell_quantity or 0)
            ),
            "reserved_sell_quantity": max(0, int(card.reserved_sell_quantity or 0)),
            "sell_all_at_market_open": bool(card.sell_all_at_market_open),
            "can_cancel_sell_all": can_withdraw_sell_all_intent(card),
            "exit_all_required": bool(card.exit_all_required),
            "exit_order_pending": bool(
                card.exit_client_order_id
                or card.exit_pending_attempt_number
                or card.exit_submission_unresolved
            ),
            "exit_cancel_in_flight": bool(card.exit_cancel_in_flight),
            "last_exit_error": str(card.last_exit_error or ""),
            "next_exit_retry_at": self._date_value(card.next_exit_retry_at),
            "last_reported_price": self._positive_number(card.market_data_last_trusted_price),
            "price_as_of": self._date_value(card.market_data_last_trusted_at),
            "warnings": [str(item) for item in card.warnings if str(item).strip()],
            "updated_at": card.updated_at.isoformat(),
            "source": self.source_name,
        }

    def list_board(self, *, force: bool = False) -> dict[str, Any]:
        """Project the same six visible lifecycle columns as desktop Kanban."""

        snapshot = self._load_snapshot(force=force)
        visible = {
            BoardStatus.BUYLIST,
            BoardStatus.BUY_TODAY,
            BoardStatus.ENTRY_PENDING,
            BoardStatus.OPEN_POSITION,
            BoardStatus.PARTIAL_SELL,
            BoardStatus.SELL_ALL,
        }
        rows = [
            self._project_board_card(card)
            for card in snapshot.cards
            if card.board_status in visible
        ]
        rows.sort(
            key=lambda row: (
                -int(row["kanban_priority"]),
                str(row["symbol"]),
            )
        )
        nav, nav_as_of, nav_note = None, None, "Account NAV unavailable; refresh the PC account"
        account_no = self._resolved_account_no or self.configured_account_no
        if self.equity_path is not None and account_no:
            nav, nav_as_of, nav_note = read_monitor_equity_snapshot(
                self.equity_path, self.environment, account_no,
                dt.datetime.now(dt.timezone.utc),
            )
        return {
            "rows": rows, "revision": snapshot.revision,
            "account_nav_usd": nav, "account_nav_as_of": nav_as_of,
            "account_nav_note": nav_note,
        }

    def get_plan(
        self,
        symbol: str,
        *,
        force: bool = False,
        include_inactive: bool = False,
    ) -> dict[str, Any]:
        normalized = normalize_symbol(symbol)
        snapshot = self._load_snapshot(force=force)
        card = next((card for card in snapshot.cards if card.symbol == normalized), None)
        projected = self._project(card) if card is not None else None
        if projected is not None and not include_inactive and not (
            any(
                projected[key]
                for key in ("watchlist_member", "buylist_member", "buy_today_display_member")
            )
            or projected["breakout_price"] is not None
        ):
            projected = None
        return {
            "card": projected,
            "revision": snapshot.revision,
        }

    def connectivity(self) -> dict[str, Any]:
        if not self.available or self.engine is None:
            return {
                "state": "UNAVAILABLE",
                "reason": self.unavailable_reason,
                "revision": None,
                "executor": "UNKNOWN",
            }
        try:
            with self.engine.connect() as connection:
                account_no = self._account_no(connection)
                collection = connection.execute(
                    text(
                        "SELECT COUNT(*) AS row_count, "
                        "COALESCE(SUM(version), 0) AS version_sum, "
                        "MAX(updated_at) AS updated_at FROM trade_cards "
                        "WHERE environment=:environment AND account_no=:account_no"
                    ),
                    {"environment": self.environment, "account_no": account_no},
                ).first()
                updated_at = collection.updated_at
                if isinstance(updated_at, str):
                    updated_at = dt.datetime.fromisoformat(updated_at)
                revision = (
                    f"{int(collection.row_count)}:{int(collection.version_sum)}:"
                    f"{updated_at.isoformat() if updated_at else ''}"
                )
                main_payload = connection.execute(
                    text(
                        "SELECT payload FROM app_state_sync "
                        "WHERE state_key='__main_device__' LIMIT 1"
                    )
                ).scalar()
                main = json.loads(main_payload) if main_payload else {}
                executor = executor_status(connection, main)
            return {
                "state": "AVAILABLE",
                "reason": "",
                "revision": revision,
                "scope": self.environment,
                **executor,
            }
        except Exception as exc:
            return {
                "state": "UNAVAILABLE",
                "reason": str(exc),
                "revision": None,
                "executor": "UNKNOWN",
            }


def build_canonical_planning_source(config: WebConfig) -> CanonicalPlanningSource:
    return CanonicalPlanningSource.from_config(config)


__all__ = [
    "CanonicalPlanningSource",
    "CanonicalPlanningUnavailable",
    "build_canonical_write_engine",
    "build_canonical_planning_source",
]
