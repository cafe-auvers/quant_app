"""Guarded mobile operator actions delegated through the hosting PC identity."""
from __future__ import annotations

import json
import math
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.core.execution_config import COORDINATION_DEVICE_HEARTBEAT_MAX_AGE_SECONDS
from src.core.runtime_readiness import RuntimeDeviceState
from src.core.trade_card_state import BoardStatus
from src.services.operator_commands import OperatorCommandError
from src.services.runtime_device_state_repository import list_runtime_device_states
from src.services.state_sync import (
    LocalDeviceRole,
    get_main_device,
    get_operator_control,
    load_local_device_role,
    set_operator_control,
)
from src.utils.device_identity import runtime_device_kind
from src.utils.market_calendar import (
    current_or_next_nyse_session_date,
    is_regular_session_open,
)

from .canonical_planning import CanonicalPlanningSource
from .config import WebConfig
from .connected_planning import ConnectedPlanningService
from .operator_identity import mobile_web_role
from .store import ConflictError, ValidationError, normalize_symbol


ALLOWED_OPERATOR_OPERATIONS = frozenset(
    {
        "move_watchlist",
        "remove_buylist",
        "move_buylist",
        "activate_buy_today",
        "deactivate_buy_today",
        "cancel_entry",
        "request_partial_sell",
        "cancel_partial_sell",
        "request_sell_all",
        "cancel_sell_all",
        "set_orb_stop",
        "set_breakeven_stop",
        "set_manual_stop",
        "reorder_card",
        "publish_today_plan",
    }
)


class ConnectedOperatorUnavailable(RuntimeError):
    pass


class ConnectedOperatorService:
    """Expose durable intent without giving the browser a broker connection.

    The browser can own manual Operator Control through a stable Mobile Web
    identity, or act as a delegated surface for the hosting PC/laptop. It never
    receives a device credential, claims execution ownership, constructs a
    broker, or transfers the execution lease.
    """

    def __init__(
        self,
        config: WebConfig,
        source: CanonicalPlanningSource,
        planning: ConnectedPlanningService,
    ) -> None:
        self.config = config
        self.source = source
        self.planning = planning

    @property
    def engine(self):
        return self.planning.engine

    @property
    def allowed_operations(self) -> frozenset[str]:
        return frozenset(self.config.connected_operator_operations)

    @property
    def available(self) -> bool:
        return bool(self.engine is not None and self.allowed_operations)

    def _repository(self) -> Path:
        repository = self.config.resolved_pc_repository
        if repository is None or not repository.is_dir():
            raise ConnectedOperatorUnavailable(
                "The configured PC repository is unavailable"
            )
        return repository

    def _role(self):
        role_path = self._repository() / "data" / "device_role.json"
        if not role_path.is_file():
            raise ConnectedOperatorUnavailable("The PC device identity is unavailable")
        return load_local_device_role(role_path)

    def _mobile_role(self) -> LocalDeviceRole:
        """Return one stable, non-executor identity for this private web app."""

        return mobile_web_role(self.config)

    def operator_control_targets(
        self, selected_device_id: str | None = None
    ) -> list[dict[str, Any]]:
        """List fresh PC/laptop targets plus the always-available mobile app."""

        if self.engine is None:
            return []
        if selected_device_id is None:
            current = get_operator_control(self.engine)
            selected_id = (
                current.control.device_id
                if current.success
                and current.control is not None
                and not current.control.locked
                else ""
            )
        else:
            selected_id = str(selected_device_id or "")
        now = datetime.now(timezone.utc)
        eligible_states = {
            RuntimeDeviceState.STARTING,
            RuntimeDeviceState.STANDBY,
            RuntimeDeviceState.STANDBY_READY,
            RuntimeDeviceState.ACTIVE,
        }
        targets: list[dict[str, Any]] = []
        records = list_runtime_device_states(self.engine)
        for key in ("PC", "LAPTOP"):
            matches = [
                record
                for record in records
                if runtime_device_kind(record.hostname, record.details).upper() == key
            ]
            matches.sort(key=lambda record: record.updated_at, reverse=True)
            record = matches[0] if matches else None
            age = (
                max(0.0, (now - record.updated_at).total_seconds())
                if record is not None
                else None
            )
            available = bool(
                record is not None
                and record.state in eligible_states
                and age is not None
                and age <= float(COORDINATION_DEVICE_HEARTBEAT_MAX_AGE_SECONDS)
            )
            targets.append(
                {
                    "key": key.lower(),
                    "label": "PC" if key == "PC" else "Laptop",
                    "available": available,
                    "selected": bool(record and record.device_id == selected_id),
                    "device_id": record.device_id if record else "",
                    "hostname": record.hostname if record else "",
                    "state": record.state.value if record else "UNAVAILABLE",
                    "heartbeat_age_seconds": round(age, 1) if age is not None else None,
                    "reason": ""
                    if available
                    else "Device app is not currently available",
                }
            )
        mobile = self._mobile_role()
        targets.append(
            {
                "key": "mobile",
                "label": "Mobile",
                "available": True,
                "selected": mobile.device_id == selected_id,
                "device_id": mobile.device_id,
                "hostname": mobile.hostname,
                "state": "READY",
                "heartbeat_age_seconds": 0.0,
                "reason": "",
            }
        )
        return targets

    def set_operator_control_target(self, target: str) -> dict[str, Any]:
        """Assign manual control without changing the execution owner."""

        if self.engine is None:
            raise ConnectedOperatorUnavailable("Operator database is unavailable")
        key = str(target or "").strip().lower()
        targets = {row["key"]: row for row in self.operator_control_targets()}
        selected = targets.get(key)
        if selected is None:
            raise ValidationError("Operator Control must be PC, Laptop, or Mobile")
        if not selected.get("available"):
            raise ConflictError(
                f"{selected.get('label', key)} is not currently available"
            )
        if key == "mobile":
            owner = self._mobile_role()
        else:
            owner = LocalDeviceRole(
                str(selected.get("device_id") or ""),
                str(selected.get("hostname") or selected.get("label") or ""),
                False,
            )
        result = set_operator_control(self.engine, self._mobile_role(), owner)
        if not result.success:
            raise ConnectedOperatorUnavailable(
                result.error or "Operator Control could not be changed"
            )
        self._publish_change_pulse(f"operator-control-{uuid.uuid4()}", "synced_state")
        return self.authority()

    def _operator_role(
        self,
        operator_device_id: str,
        targets: list[dict[str, Any]] | None = None,
    ):
        mobile = self._mobile_role()
        if operator_device_id == mobile.device_id:
            return mobile, "MOBILE"
        host = self._role()
        if operator_device_id == host.device_id:
            kind = next(
                (
                    str(target.get("label") or "").upper()
                    for target in (targets or ())
                    if target.get("device_id") == host.device_id
                ),
                runtime_device_kind(host.hostname, {}).upper(),
            )
            return host, "LAPTOP" if kind == "LAPTOP" else "PC"
        return None, "OTHER"

    def authority(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "available": self.available,
            "delegated": False,
            "operator_control": "UNAVAILABLE",
            "execution_owner": "UNKNOWN",
            "same_device": False,
            "operations": sorted(self.allowed_operations),
            "targets": [],
            "reason": "",
        }
        if not self.available or self.engine is None:
            result["reason"] = "Mobile operator controls are not enabled"
            return result
        try:
            operator = get_operator_control(self.engine)
            owner = get_main_device(self.engine)
            selected_device_id = (
                operator.control.device_id
                if operator.success and operator.control is not None
                else ""
            )
            result["targets"] = self.operator_control_targets(selected_device_id)
        except Exception as exc:
            result["reason"] = f"Shared control is unavailable ({type(exc).__name__})"
            return result
        if not operator.success or operator.control is None:
            result["reason"] = operator.error or "Operator Control is unavailable"
            return result
        if operator.control.locked:
            result["operator_control"] = "LOCKED"
            result["reason"] = "Operator Control is locked"
            return result
        operator_role, operator_label = self._operator_role(
            operator.control.device_id, result["targets"]
        )
        operator_match = operator_role is not None
        main = owner.main_device if owner.success else None
        executor_match = bool(main and operator_role and main.device_id == operator_role.device_id)
        result.update(
            {
                "delegated": operator_match,
                "operator_control": (
                    operator_label if operator_match else operator.control.hostname or "OTHER"
                ),
                "execution_owner": (
                    "THIS PC" if executor_match else main.hostname if main else "UNKNOWN"
                ),
                "same_device": operator_match and executor_match,
            }
        )
        if not operator_match:
            result["reason"] = "Another device owns Operator Control"
        elif not executor_match:
            result["reason"] = (
                "Planning intent syncs now; broker actions remain with the Execution Owner"
            )
        return result

    def _require_operator_authority(self, operation: str):
        if operation not in self.allowed_operations:
            raise ValidationError("This mobile operator operation is not enabled")
        status = self.authority()
        if not status.get("delegated"):
            raise ConnectedOperatorUnavailable(
                str(status.get("reason") or "Mobile operator authority is unavailable")
            )
        operator = get_operator_control(self.engine)
        if not operator.success or operator.control is None:
            raise ConnectedOperatorUnavailable("Operator Control is unavailable")
        role, _label = self._operator_role(operator.control.device_id)
        if role is None:
            raise ConnectedOperatorUnavailable("Another device owns Operator Control")
        return role, status

    def _publish_change_pulse(self, token: str, *tables: str) -> bool:
        from src.services.coordination_change_pulse import record_external_change_pulses

        return record_external_change_pulses(
            self._repository() / "data",
            f"web-operator:{token}",
            tables=tables,
        )

    def board_snapshot(self, *, force: bool = False) -> dict[str, Any]:
        """Return the canonical six-column Kanban projection."""

        return self.source.list_board(force=force)

    def apply_board_action(
        self,
        *,
        command_id: str,
        action: str,
        symbol: str,
        expected_revision: int,
        quantity: int | None = None,
        price: float | None = None,
        target_priority: int | None = None,
    ) -> dict[str, Any]:
        """Apply one desktop-equivalent typed Kanban command.

        Presentation moves and Buy Today planning intent commit immediately.
        Broker-facing entry/exit intent is queued when Operator Control is not
        also the Execution Owner. In neither case can the browser contact the
        broker.
        """

        action = str(action or "").strip().lower()
        role, authority = self._require_operator_authority(action)
        if self.engine is None:
            raise ConnectedOperatorUnavailable("Operator database is unavailable")
        from src.core.board_workflow import (
            ActivateForToday,
            BoardActionContext,
            CancelEntry,
            CancelPartialSell,
            CancelQueuedSellAll,
            MoveToBuylist,
            MoveToWatchlist,
            RemoveFromBuylist,
            ReorderCard,
            RequestPartialSell,
            RequestSellAll,
            SetBreakevenStop,
            SetManualStop,
            SetOrbStop,
        )
        from src.services import execution_workflow_service
        from src.services.trade_card_repository import (
            TradeCardNotFoundError,
            TradeCardVersionConflictError,
        )

        symbol = normalize_symbol(symbol)
        account_no = self.planning._account_no()
        card = self.planning._current_card(account_no, symbol)
        self.planning._check_revision(card, expected_revision)
        if card is None:
            raise ConflictError("No canonical Buy Board card exists")
        if action == "activate_buy_today":
            if card.board_status != BoardStatus.BUYLIST or not card.buylist_member:
                raise ConflictError("Buy Today activation requires a Buylist card")
            breakout = self.source._positive_number(card.breakout_price)
            if breakout is None:
                raise ConflictError("Set a breakout price before Buy Today activation")
        if action == "deactivate_buy_today" and card.board_status != BoardStatus.BUY_TODAY:
            raise ConflictError("Buy Today cancellation requires a Buy Today card")

        common = {
            "environment": self.config.canonical_environment,
            "account_no": account_no,
            "symbol": symbol,
            "expected_card_version": int(expected_revision),
            "command_id": command_id,
        }
        command_types = {
            "move_watchlist": lambda: MoveToWatchlist(**common),
            "remove_buylist": lambda: RemoveFromBuylist(**common),
            "move_buylist": lambda: MoveToBuylist(**common),
            "activate_buy_today": lambda: ActivateForToday(**common),
            "deactivate_buy_today": lambda: CancelEntry(**common),
            "cancel_entry": lambda: CancelEntry(**common),
            "request_partial_sell": lambda: RequestPartialSell(
                quantity=int(quantity or 0), **common
            ),
            "cancel_partial_sell": lambda: CancelPartialSell(**common),
            "request_sell_all": lambda: RequestSellAll(**common),
            "cancel_sell_all": lambda: CancelQueuedSellAll(**common),
            "set_orb_stop": lambda: SetOrbStop(**common),
            "set_breakeven_stop": lambda: SetBreakevenStop(**common),
            "set_manual_stop": lambda: SetManualStop(
                price=float(price or 0.0), **common
            ),
            "reorder_card": lambda: ReorderCard(
                target_priority=int(target_priority or 0), **common
            ),
        }
        factory = command_types.get(action)
        if factory is None:
            raise ValidationError("Unsupported mobile Buy Board action")
        command = factory()
        if action == "request_partial_sell" and int(quantity or 0) <= 0:
            raise ValidationError("Partial-sell quantity must be positive")
        if action == "set_manual_stop":
            try:
                manual_price = float(price or 0.0)
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValidationError("Manual stop must be a positive price") from exc
            if not math.isfinite(manual_price) or manual_price <= 0:
                raise ValidationError("Manual stop must be a positive price")

        presentation_actions = {
            "move_watchlist",
            "remove_buylist",
            "move_buylist",
            "reorder_card",
        }
        direct_intent_actions = {"activate_buy_today", "deactivate_buy_today"}
        queued = (
            not bool(authority.get("same_device"))
            and action not in presentation_actions
            and action not in direct_intent_actions
        )
        try:
            if queued:
                from src.services.operator_command_service import (
                    enqueue_board_operator_command,
                )

                queue_result = enqueue_board_operator_command(self.engine, role, command)
                canonical_version = int(card.version)
                command_status = queue_result.command.status.value
            else:
                result = execution_workflow_service.request_board_action(
                    self.engine,
                    command,
                    context=BoardActionContext(
                        regular_session_open=is_regular_session_open(),
                        session_date=current_or_next_nyse_session_date(),
                        local_operator_control=True,
                    ),
                    # Buy Today is the explicit handoff from passive planning
                    # into Kanban execution intent. Claim symbol ownership in
                    # the same transaction, exactly like the desktop action.
                    claim_kanban_ownership=action == "activate_buy_today",
                    local_snapshot_path=(
                        self.config.data_dir / "canonical_trade_cards_recovery.json"
                    ),
                    record_summary=True,
                )
                canonical_version = int(result.card.version) if result.card else 0
                command_status = "COMPLETED"
        except (
            execution_workflow_service.BoardCommandRejectedError,
            execution_workflow_service.BoardRuntimeFenceError,
            TradeCardNotFoundError,
            TradeCardVersionConflictError,
            OperatorCommandError,
        ) as exc:
            current = self.source.get_plan(
                symbol, force=True, include_inactive=True
            ).get("card")
            raise ConflictError(str(exc), current) from exc

        self._publish_change_pulse(
            command_id, "operator_commands" if queued else "trade_cards"
        )
        board = self.board_snapshot(force=True)
        projected = next(
            (row for row in board["rows"] if row["symbol"] == symbol), None
        )
        return {
            "status": "REQUEST QUEUED" if queued else "KANBAN UPDATED",
            "card": projected,
            "board_revision": board["revision"],
            "command_id": command_id,
            "published": bool(action == "activate_buy_today" and not queued),
            "executable_intent": action not in presentation_actions,
            "broker_order_placed": False,
            "queued": queued,
            "command_status": command_status,
            "canonical_version": canonical_version,
        }

    def set_buy_today(
        self,
        *,
        command_id: str,
        symbol: str,
        expected_revision: int,
        enabled: bool,
    ) -> dict[str, Any]:
        result = self.apply_board_action(
            command_id=command_id,
            action="activate_buy_today" if enabled else "deactivate_buy_today",
            symbol=symbol,
            expected_revision=expected_revision,
        )
        # Keep the chart-planning endpoint's established public card shape;
        # the dedicated Buy Board endpoint uses the richer Kanban projection.
        result["card"] = self.source.get_plan(
            symbol, force=True, include_inactive=True
        ).get("card")
        result["status"] = (
            "BUY TODAY REQUEST QUEUED"
            if result["queued"] and enabled
            else "REMOVE FROM TODAY REQUEST QUEUED"
            if result["queued"]
            else "BUY TODAY ACTIVATED"
            if enabled
            else "REMOVED FROM TODAY"
        )
        return result

    @staticmethod
    def _load_document(path: Path) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConnectedOperatorUnavailable(
                f"Planning file is unavailable: {path.name}"
            ) from exc
        if not isinstance(value, dict):
            raise ConnectedOperatorUnavailable(
                f"Planning file must contain an object: {path.name}"
            )
        return value

    def _planning_documents(self) -> tuple[Path, dict[str, dict[str, Any]]]:
        data_dir = self._repository() / "data"
        names = (
            "watchlist.json",
            "buylist.json",
            "trade_plans.json",
            "execution_queue.json",
            "scanner_setups.json",
            "settings.json",
        )
        return data_dir, {
            name: self._load_document(data_dir / name) for name in names
        }

    def _verify_buy_today_queue(self, execution_queue: dict[str, Any]) -> None:
        today = self.source.list_plans("BUY_TODAY", force=True).get("rows", [])
        raw_items = execution_queue.get("items", {})
        queue_rows = (
            list(raw_items.values()) if isinstance(raw_items, dict) else raw_items
        )
        if not isinstance(queue_rows, list):
            raise ConnectedOperatorUnavailable("Execution queue items are invalid")
        queue_by_symbol = {
            str(row.get("symbol") or "").strip().upper(): row
            for row in queue_rows
            if isinstance(row, dict) and str(row.get("symbol") or "").strip()
        }
        problems: list[str] = []
        for card in today:
            symbol = str(card.get("symbol") or "").strip().upper()
            row = queue_by_symbol.get(symbol)
            if row is None:
                problems.append(f"{symbol}: execution queue item is missing")
                continue
            try:
                card_breakout = float(card.get("breakout_price") or 0.0)
                queue_breakout = float(row.get("breakout_price") or 0.0)
            except (TypeError, ValueError, OverflowError):
                problems.append(f"{symbol}: breakout target is invalid")
                continue
            if not (
                math.isfinite(card_breakout)
                and card_breakout > 0
                and math.isclose(
                    card_breakout, queue_breakout, rel_tol=1e-9, abs_tol=1e-9
                )
            ):
                problems.append(
                    f"{symbol}: queue breakout does not match the canonical target"
                )
        if problems:
            raise ConnectedOperatorUnavailable(
                "Today's plan is not ready: " + "; ".join(problems)
            )

    def publish_today_plan(self, *, command_id: str) -> dict[str, Any]:
        role, _authority = self._require_operator_authority("publish_today_plan")
        if self.engine is None:
            raise ConnectedOperatorUnavailable("Operator database is unavailable")
        if is_regular_session_open():
            raise ConnectedOperatorUnavailable(
                "Market is open. Full plan publish is disabled; use guarded live "
                "Buy Board controls instead."
            )
        data_dir, documents = self._planning_documents()
        self._verify_buy_today_queue(documents["execution_queue.json"])
        from src.services.app_state import publish_trading_plan

        result = publish_trading_plan(
            self.engine,
            role,
            documents["watchlist.json"],
            documents["buylist.json"],
            documents["trade_plans.json"],
            documents["execution_queue.json"],
            market_is_open=False,
            metadata_path=data_dir / "state_metadata.json",
            scanner_setups_dict=documents["scanner_setups.json"],
            settings_dict=documents["settings.json"],
        )
        if not result.success:
            raise ConnectedOperatorUnavailable(
                result.error or "Today's plan did not pass publication verification"
            )
        self._publish_change_pulse(command_id, "app_state_sync")
        return {
            "status": "TODAY'S PLAN PUBLISHED",
            "command_id": command_id,
            "revisions": result.revisions,
            "verified_at": (
                result.verified_at.isoformat() if result.verified_at else None
            ),
            "execution_owner": result.execution_owner_hostname,
            "execution_owner_heartbeat_fresh": (
                result.execution_owner_heartbeat_fresh
            ),
            "broker_order_placed": False,
        }

    def command_status(self, command_id: str) -> dict[str, Any]:
        if self.engine is None:
            raise ConnectedOperatorUnavailable("Operator database is unavailable")
        from src.services.operator_commands import (
            TERMINAL_OPERATOR_COMMAND_STATUSES,
            get_operator_command,
        )

        command = get_operator_command(self.engine, command_id)
        if command is None:
            raise ConnectedOperatorUnavailable("Operator command was not found")
        return {
            "command_id": command.command_id,
            "status": command.status.value,
            "terminal": command.status in TERMINAL_OPERATOR_COMMAND_STATUSES,
            "success": command.status.value in {"COMPLETED", "FILLED"},
            "error": command.error_message,
            "symbol": command.symbol,
        }


__all__ = [
    "ALLOWED_OPERATOR_OPERATIONS",
    "ConnectedOperatorService",
    "ConnectedOperatorUnavailable",
]
