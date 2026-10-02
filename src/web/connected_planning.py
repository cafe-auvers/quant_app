"""Narrow CONNECTED-mode gateway for passive canonical planning writes."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine

from src.core.watchlist import WatchlistItem
from src.services.state_sync import get_operator_control, load_local_device_role
from src.services.trade_card_repository import (
    TradeCardNotFoundError,
    TradeCardVersionConflictError,
    get_trade_card,
)
from src.utils.market_calendar import (
    current_or_next_nyse_session_date,
    is_regular_session_open,
)

from .canonical_planning import (
    CanonicalPlanningSource,
    CanonicalPlanningUnavailable,
    build_canonical_write_engine,
)
from .config import WebConfig
from .operator_identity import mobile_web_role
from .store import ConflictError, ValidationError, normalize_symbol


ALLOWED_PASSIVE_OPERATIONS = frozenset(
    {
        "add_watchlist",
        "remove_watchlist",
        "promote_buylist",
        "remove_buylist",
        "move_watchlist",
        "set_breakout",
        "clear_breakout",
    }
)


class ConnectedPlanningUnavailable(RuntimeError):
    pass


class _MemoryWatchlist:
    """Small compatibility adapter; canonical TiDB remains the authority."""

    def __init__(self, card=None) -> None:
        self.active_session_date = current_or_next_nyse_session_date()
        self.items: list[WatchlistItem] = []
        if card is not None and bool(card.watchlist_member):
            self.items.append(
                WatchlistItem(
                    symbol=card.symbol,
                    name=card.name or card.symbol,
                    breakout_price=card.breakout_price,
                )
            )

    def get(self, symbol: str):
        normalized = str(symbol or "").strip().upper()
        return next((item for item in self.items if item.symbol == normalized), None)

    def add(self, symbol: str, name: str, entry_price=None):
        existing = self.get(symbol)
        if existing is not None:
            return existing
        item = WatchlistItem(
            symbol=str(symbol).upper(),
            name=str(name or symbol),
            entry_price=entry_price,
        )
        self.items.append(item)
        return item

    def remove(self, symbol: str) -> bool:
        existing = self.get(symbol)
        if existing is None:
            return False
        self.items.remove(existing)
        return True


@dataclass
class ConnectedPlanningService:
    config: WebConfig
    source: CanonicalPlanningSource
    engine: Engine | None = None
    unavailable_reason: str = "Connected planning writes are disabled"

    @classmethod
    def from_config(
        cls, config: WebConfig, source: CanonicalPlanningSource
    ) -> "ConnectedPlanningService":
        if not config.canonical_planning_writes:
            return cls(config, source)
        repository = config.resolved_pc_repository
        if repository is None or not repository.is_dir():
            return cls(config, source, unavailable_reason="The configured PC repository is unavailable")
        try:
            engine = build_canonical_write_engine(repository)
        except Exception as exc:
            return cls(
                config,
                source,
                unavailable_reason=f"Canonical planning write database unavailable ({type(exc).__name__})",
            )
        return cls(config, source, engine=engine, unavailable_reason="")

    @property
    def available(self) -> bool:
        return bool(self.config.canonical_planning_writes and self.engine is not None)

    @property
    def allowed_operations(self) -> frozenset[str]:
        return frozenset(self.config.connected_passive_operations)

    def close(self) -> None:
        if self.engine is not None:
            self.engine.dispose()

    def capabilities(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "operations": sorted(self.allowed_operations if self.available else ()),
            "reason": "" if self.available else self.unavailable_reason,
        }

    def _account_no(self) -> str:
        if self.engine is None:
            raise ConnectedPlanningUnavailable(self.unavailable_reason)
        configured = str(self.config.canonical_account_no or "").strip()
        if configured:
            return configured
        with self.engine.connect() as connection:
            accounts = [
                str(value)
                for value in connection.execute(
                    text(
                        "SELECT DISTINCT account_no FROM trade_cards "
                        "WHERE environment=:environment ORDER BY account_no LIMIT 2"
                    ),
                    {"environment": self.config.canonical_environment},
                ).scalars()
            ]
        if len(accounts) != 1:
            raise ConnectedPlanningUnavailable(
                "Set canonical_account_no because the canonical scope is not unambiguous"
            )
        return accounts[0]

    def _current_card(self, account_no: str, symbol: str):
        if self.engine is None:
            raise ConnectedPlanningUnavailable(self.unavailable_reason)
        return get_trade_card(
            self.engine,
            self.config.canonical_environment,
            account_no,
            symbol,
            raise_on_error=True,
        )

    def _require_operator_control(self) -> None:
        if self.engine is None:
            raise ConnectedPlanningUnavailable(self.unavailable_reason)
        repository = self.config.resolved_pc_repository
        if repository is None:
            raise ConnectedPlanningUnavailable("The PC identity path is unavailable")
        role_path = Path(repository) / "data" / "device_role.json"
        if not role_path.is_file():
            raise ConnectedPlanningUnavailable("The PC device identity is unavailable")
        role = load_local_device_role(role_path)
        control = get_operator_control(self.engine)
        if not control.success or control.control is None:
            raise ConnectedPlanningUnavailable(
                control.error or "Operator Control could not be verified"
            )
        if control.control.locked:
            raise ConnectedPlanningUnavailable("Operator Control is locked")
        allowed_operator_ids = {
            role.device_id,
            mobile_web_role(self.config).device_id,
        }
        if control.control.device_id not in allowed_operator_ids:
            raise ConnectedPlanningUnavailable(
                "This web app is not the current Operator Control owner"
            )

    def _check_revision(self, card, expected_revision: int) -> None:
        current_revision = int(card.version) if card is not None else 0
        if current_revision != int(expected_revision):
            current = self.source.get_plan(
                card.symbol, force=True, include_inactive=True
            ).get("card") if card else None
            raise ConflictError("Stale planning revision", current)

    def _publish_change_pulse(self, command_id: str) -> bool:
        repository = self.config.resolved_pc_repository
        if repository is None:
            return False
        from src.services.coordination_change_pulse import (
            record_external_change_pulses,
        )

        return record_external_change_pulses(
            repository / "data",
            f"web:{command_id}",
            tables=("trade_cards",),
        )

    def apply(
        self,
        *,
        command_id: str,
        operation: str,
        symbol: str,
        expected_revision: int,
        breakout_price: float | None,
    ) -> dict[str, Any]:
        if not self.available:
            raise ConnectedPlanningUnavailable(self.unavailable_reason)
        operation = str(operation or "").strip().lower()
        if operation not in self.allowed_operations:
            raise ValidationError("This connected planning operation is not enabled")
        if operation not in ALLOWED_PASSIVE_OPERATIONS:
            raise ValidationError("Unsupported connected planning operation")
        symbol = normalize_symbol(symbol)
        account_no = self._account_no()
        card = self._current_card(account_no, symbol)
        self._check_revision(card, expected_revision)
        environment = self.config.canonical_environment
        from src.core.board_workflow import (
            BoardActionContext,
            ClearBreakoutPrice,
            MoveToBuylist,
            MoveToWatchlist,
            RemoveFromBuylist,
            SetBreakoutPrice,
        )
        from src.services import execution_workflow_service, planning_membership_service

        try:
            if operation == "add_watchlist":
                if card is not None and bool(card.watchlist_member) and (
                    card.watchlist_session_date == current_or_next_nyse_session_date()
                ):
                    raise ConflictError(
                        "Symbol is already in Watchlist",
                        self.source.get_plan(
                            symbol, force=True, include_inactive=True
                        ).get("card"),
                    )
                planning_membership_service.add_watchlist_candidate(
                    _MemoryWatchlist(card),
                    symbol=symbol,
                    name=(card.name if card is not None else symbol),
                    breakout_price=(card.breakout_price if card is not None else None),
                    engine=self.engine,
                    default_account_no=account_no,
                    local_snapshot_path=self.config.data_dir / "canonical_trade_cards_recovery.json",
                    record_summary=False,
                )
            elif operation == "remove_watchlist":
                if card is None or not bool(card.watchlist_member):
                    raise ConflictError(
                        "Symbol is not in Watchlist",
                        self.source.get_plan(
                            symbol, force=True, include_inactive=True
                        ).get("card"),
                    )
                planning_membership_service.remove_watchlist_candidate(
                    _MemoryWatchlist(card),
                    symbol,
                    engine=self.engine,
                    default_account_no=account_no,
                    local_snapshot_path=self.config.data_dir / "canonical_trade_cards_recovery.json",
                    record_summary=False,
                )
            else:
                if operation in {"set_breakout", "clear_breakout"}:
                    self._require_operator_control()
                common = {
                    "environment": environment,
                    "account_no": account_no,
                    "symbol": symbol,
                    "expected_card_version": int(expected_revision),
                    "command_id": command_id,
                }
                if operation == "promote_buylist":
                    command = MoveToBuylist(**common)
                elif operation == "remove_buylist":
                    command = RemoveFromBuylist(**common)
                elif operation == "move_watchlist":
                    command = MoveToWatchlist(**common)
                elif operation == "set_breakout":
                    if breakout_price is None:
                        raise ValidationError("breakout_price is required")
                    command = SetBreakoutPrice(price=float(breakout_price), **common)
                else:
                    command = ClearBreakoutPrice(**common)
                execution_workflow_service.request_board_action(
                    self.engine,
                    command,
                    context=BoardActionContext(
                        regular_session_open=is_regular_session_open(),
                        local_operator_control=operation in {"set_breakout", "clear_breakout"},
                    ),
                    local_snapshot_path=self.config.data_dir / "canonical_trade_cards_recovery.json",
                    record_summary=False,
                )
        except ConflictError:
            raise
        except (TradeCardVersionConflictError, TradeCardNotFoundError) as exc:
            current = self.source.get_plan(
                symbol, force=True, include_inactive=True
            ).get("card")
            raise ConflictError(str(exc), current) from exc
        except (
            planning_membership_service.PlanningMembershipError,
            execution_workflow_service.BoardCommandRejectedError,
        ) as exc:
            current = self.source.get_plan(
                symbol, force=True, include_inactive=True
            ).get("card")
            raise ConflictError(str(exc), current) from exc
        except CanonicalPlanningUnavailable:
            raise

        notification_published = self._publish_change_pulse(command_id)
        projected = self.source.get_plan(
            symbol, force=True, include_inactive=True
        ).get("card")
        return {
            "status": "SAVED TO CANONICAL STORE",
            "card": projected,
            "idempotent_replay": False,
            "command_id": command_id,
            "change_notification": (
                "PUBLISHED" if notification_published else "POLLING RECOVERY"
            ),
        }


__all__ = [
    "ALLOWED_PASSIVE_OPERATIONS",
    "ConnectedPlanningService",
    "ConnectedPlanningUnavailable",
]
