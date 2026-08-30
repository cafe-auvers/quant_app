"""Final-boundary mutation interception for Gate-3 shadow qualification.

This adapter never calls a destructive broker method and never fabricates an
acknowledgement, order, or fill.  It durably records a visibly labelled
``WOULD_*`` event and raises :class:`ShadowMutationIntercepted`; the shadow
runner treats that exception as the terminal outcome of the decision branch.
Read-only broker methods may be delegated to the real guarded gateway.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping, Optional
from uuid import uuid4

from src.core.execution_request import (
    CancelExecutionRequest,
    ReplaceExecutionRequest,
    SubmitExecutionRequest,
)
from src.core.order_state import OrderSide

SHADOW_LABEL = "SHADOW_ONLY_NO_BROKER_ACK_OR_FILL"
SHADOW_EVENT_TYPES = frozenset(
    {"WOULD_SUBMIT", "WOULD_CANCEL", "WOULD_REPLACE", "WOULD_SELL"}
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _canonical_value(value: Any) -> Any:
    if is_dataclass(value):
        return _canonical_value(asdict(value))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, Mapping):
        return {
            str(key): _canonical_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _request_digest(request: Any) -> str:
    payload = json.dumps(
        _canonical_value(request),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _account_reference(account_no: str) -> str:
    value = str(account_no or "").strip()
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16] if value else ""


@dataclass(frozen=True)
class ShadowEvent:
    schema_version: int
    event_id: str
    event_type: str
    occurred_at: str
    label: str
    environment: str
    account_ref: str
    symbol: str
    side: str
    quantity: int
    limit_price: float
    command_id: str
    parent_client_order_id: str
    request_sha256: str
    captured_live_replay: bool = False

    def __post_init__(self) -> None:
        if self.event_type not in SHADOW_EVENT_TYPES:
            raise ValueError(f"Unsupported shadow event type: {self.event_type}")
        if self.label != SHADOW_LABEL:
            raise ValueError("Shadow event must carry the non-broker label")
        if not self.event_id or not self.command_id or not self.request_sha256:
            raise ValueError("Shadow event identity is incomplete")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ShadowStoreAudit:
    event_count: int
    event_counts: dict[str, int]
    parse_error_count: int
    label_mismatch_count: int
    duplicate_event_id_count: int
    sha256: str

    @property
    def passed(self) -> bool:
        return not (
            self.parse_error_count
            or self.label_mismatch_count
            or self.duplicate_event_id_count
        )


class ShadowEventStore:
    """Append-only JSONL store that must be isolated from production ledgers."""

    def __init__(
        self,
        path: Path,
        *,
        production_paths: tuple[Path, ...] = (),
    ) -> None:
        resolved = Path(path).expanduser().resolve()
        if resolved.suffixes[-2:] != [".shadow", ".jsonl"]:
            raise ValueError("Shadow store path must end with .shadow.jsonl")
        for production_path in production_paths:
            production = Path(production_path).expanduser().resolve()
            if resolved == production or production in resolved.parents:
                raise ValueError("Shadow store cannot be inside a production ledger path")
        self.path = resolved
        self._lock = threading.RLock()

    def append(self, event: ShadowEvent) -> None:
        payload = json.dumps(event.to_dict(), sort_keys=True, separators=(",", ":"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self.path.open("a", encoding="utf-8") as handle:
            handle.write(payload + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def read_all(self) -> list[ShadowEvent]:
        if not self.path.exists():
            return []
        return [
            ShadowEvent(**json.loads(line))
            for line in self.path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def sha256(self) -> str:
        return hashlib.sha256(
            self.path.read_bytes() if self.path.exists() else b""
        ).hexdigest()

    def audit(self) -> ShadowStoreAudit:
        """Parse every durable row and report integrity without hiding errors."""

        counts = {event_type: 0 for event_type in sorted(SHADOW_EVENT_TYPES)}
        parse_errors = 0
        label_mismatches = 0
        duplicate_ids = 0
        seen_ids: set[str] = set()
        lines = (
            self.path.read_text(encoding="utf-8").splitlines()
            if self.path.exists()
            else []
        )
        event_count = 0
        for line in lines:
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
                if not isinstance(payload, dict):
                    raise ValueError("shadow row is not an object")
                if payload.get("label") != SHADOW_LABEL:
                    label_mismatches += 1
                event = ShadowEvent(**payload)
            except (TypeError, ValueError, json.JSONDecodeError):
                parse_errors += 1
                continue
            event_count += 1
            counts[event.event_type] += 1
            if event.event_id in seen_ids:
                duplicate_ids += 1
            seen_ids.add(event.event_id)
        return ShadowStoreAudit(
            event_count=event_count,
            event_counts=counts,
            parse_error_count=parse_errors,
            label_mismatch_count=label_mismatches,
            duplicate_event_id_count=duplicate_ids,
            sha256=self.sha256(),
        )


class ShadowMutationIntercepted(RuntimeError):
    """Expected terminal result for a shadow-only mutation candidate."""

    def __init__(self, event: ShadowEvent) -> None:
        super().__init__(f"{event.event_type} intercepted ({event.event_id})")
        self.event = event


class ShadowExecutionBoundary:
    """Intercept destructive methods at the same boundary as production."""

    def __init__(
        self,
        *,
        store: ShadowEventStore,
        read_delegate: Any = None,
        order_context_lookup: Optional[Callable[[str], Mapping[str, Any]]] = None,
        clock: Callable[[], datetime] = _utc_now,
        captured_live_replay: bool = False,
    ) -> None:
        self.store = store
        self._read_delegate = read_delegate
        self._order_context_lookup = order_context_lookup
        self._clock = clock
        self._captured_live_replay = bool(captured_live_replay)

    @property
    def mode(self) -> str:
        return "SHADOW_ONLY"

    def _context(self, client_order_id: str) -> Mapping[str, Any]:
        if self._order_context_lookup is None:
            return {}
        context = self._order_context_lookup(str(client_order_id or ""))
        return dict(context or {})

    def _intercept(
        self,
        *,
        event_type: str,
        request: Any,
        command_id: str,
        environment: str,
        account_no: str,
        symbol: str,
        side: str = "",
        quantity: int = 0,
        limit_price: float = 0.0,
        parent_client_order_id: str = "",
    ) -> None:
        if not str(command_id or "").strip():
            raise ValueError("A stable command identity is required in shadow mode")
        if not str(environment or "").strip() or not str(account_no or "").strip():
            raise ValueError("Environment and account identity are required in shadow mode")
        if not str(symbol or "").strip():
            raise ValueError("Symbol identity is required in shadow mode")
        event = ShadowEvent(
            schema_version=1,
            event_id=uuid4().hex,
            event_type=event_type,
            occurred_at=self._clock().astimezone(timezone.utc).isoformat(),
            label=SHADOW_LABEL,
            environment=str(environment).upper(),
            account_ref=_account_reference(account_no),
            symbol=str(symbol).upper(),
            side=str(side or "").upper(),
            quantity=max(0, int(quantity or 0)),
            limit_price=max(0.0, float(limit_price or 0.0)),
            command_id=str(command_id).strip(),
            parent_client_order_id=str(parent_client_order_id or "").strip(),
            request_sha256=_request_digest(request),
            captured_live_replay=self._captured_live_replay,
        )
        self.store.append(event)
        raise ShadowMutationIntercepted(event)

    def submit_guarded(self, request: SubmitExecutionRequest) -> None:
        event_type = "WOULD_SELL" if request.side == OrderSide.SELL else "WOULD_SUBMIT"
        self._intercept(
            event_type=event_type,
            request=request,
            command_id=request.client_order_id,
            environment=request.environment,
            account_no=request.account_no,
            symbol=request.symbol,
            side=request.side.value,
            quantity=request.quantity,
            limit_price=request.limit_price,
            parent_client_order_id=request.replaces_execution_order_id,
        )

    def cancel_guarded(self, request: CancelExecutionRequest) -> None:
        context = self._context(request.client_order_id)
        self._intercept(
            event_type="WOULD_CANCEL",
            request=request,
            command_id=request.cancel_command_id,
            environment=request.environment,
            account_no=request.account_no,
            symbol=request.symbol or str(context.get("symbol") or ""),
            side=request.side or str(context.get("side") or ""),
            quantity=request.quantity or int(context.get("quantity") or 0),
            parent_client_order_id=request.client_order_id,
        )

    def replace_guarded(self, request: ReplaceExecutionRequest) -> None:
        context = self._context(request.client_order_id)
        self._intercept(
            event_type="WOULD_REPLACE",
            request=request,
            command_id=request.replace_command_id,
            environment=request.environment,
            account_no=request.account_no,
            symbol=str(context.get("symbol") or ""),
            side=str(context.get("side") or "BUY"),
            quantity=request.new_quantity,
            limit_price=request.new_limit_price,
            parent_client_order_id=request.client_order_id,
        )

    def submit_order(self, **kwargs: Any) -> None:
        side = kwargs.get("side")
        side_value = side.value if isinstance(side, Enum) else str(side or "")
        command_id = str(kwargs.get("client_order_id") or "").strip()
        self._intercept(
            event_type="WOULD_SELL" if side_value.upper() == "SELL" else "WOULD_SUBMIT",
            request=kwargs,
            command_id=command_id,
            environment=str(kwargs.get("environment") or ""),
            account_no=str(kwargs.get("account_no") or ""),
            symbol=str(kwargs.get("symbol") or ""),
            side=side_value,
            quantity=int(kwargs.get("quantity") or 0),
            limit_price=float(kwargs.get("limit_price") or 0.0),
        )

    def cancel_order(self, **kwargs: Any) -> None:
        self._intercept(
            event_type="WOULD_CANCEL",
            request=kwargs,
            command_id=str(kwargs.get("cancel_command_id") or ""),
            environment=str(kwargs.get("environment") or ""),
            account_no=str(kwargs.get("account_no") or ""),
            symbol=str(kwargs.get("ownership_symbol") or kwargs.get("symbol") or ""),
            parent_client_order_id=str(kwargs.get("client_order_id") or ""),
        )

    def get_order(self, **kwargs: Any) -> Any:
        return self._read("get_order", **kwargs)

    def discover_orders(self, **kwargs: Any) -> Any:
        return self._read("discover_orders", **kwargs)

    def get_positions(self, **kwargs: Any) -> Any:
        return self._read("get_positions", **kwargs)

    def _read(self, method_name: str, **kwargs: Any) -> Any:
        if self._read_delegate is None:
            raise RuntimeError(f"No read-only delegate configured for {method_name}")
        return getattr(self._read_delegate, method_name)(**kwargs)

    @staticmethod
    def is_ambiguous_submission_error(error: BaseException) -> bool:
        # Interception happens locally before any real broker call; the result
        # is definitively shadow-only, never an unknown broker submission.
        return not isinstance(error, ShadowMutationIntercepted)

    @staticmethod
    def is_ambiguous_cancellation_error(error: BaseException) -> bool:
        return not isinstance(error, ShadowMutationIntercepted)


__all__ = [
    "SHADOW_EVENT_TYPES",
    "SHADOW_LABEL",
    "ShadowEvent",
    "ShadowEventStore",
    "ShadowStoreAudit",
    "ShadowExecutionBoundary",
    "ShadowMutationIntercepted",
]
