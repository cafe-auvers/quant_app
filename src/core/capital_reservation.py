"""Capital reservation model. ``buydashboard_to_kanban.md`` section 22.

No account-level buying-power/capital-reservation concept exists anywhere in
the codebase today (confirmed by the codebase survey behind this redesign --
:mod:`src.risk.orb_position` only checks one trade's capital percent against
total account size, with no notion of *other* concurrently-reserved capital).
This module is the new, additive record; :mod:`src.services.capital_allocator`
owns the account-level lock and persistence built on top of it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Iterable, Optional
from uuid import uuid4


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_timestamp(value: Any) -> Optional[datetime]:
    if value is None or value == "":
        return None
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class CapitalReservationStatus(str, Enum):
    RESERVED = "RESERVED"
    PARTIALLY_CONSUMED = "PARTIALLY_CONSUMED"
    CONSUMED = "CONSUMED"
    RELEASED = "RELEASED"
    EXPIRED = "EXPIRED"


# A reservation still holds capital against future new entries while in
# either of these statuses; anything else has given its capital back.
OPEN_RESERVATION_STATUSES = {
    CapitalReservationStatus.RESERVED,
    CapitalReservationStatus.PARTIALLY_CONSUMED,
}


def _enum_from_value(
    value: Any, enum_cls: type[Enum], default: Enum
) -> Enum:
    if isinstance(value, enum_cls):
        return value
    try:
        return enum_cls(str(value).strip().upper())
    except (TypeError, ValueError):
        return default


@dataclass
class CapitalReservation:
    reservation_id: str
    environment: str
    account_no: str
    symbol: str
    attempt_group_id: str
    requested_notional: float
    remaining_reserved_notional: float
    status: CapitalReservationStatus = CapitalReservationStatus.RESERVED
    version: int = 1
    created_at: datetime = field(default_factory=_utc_now)
    released_at: Optional[datetime] = None
    absence_count: int = 0
    last_absence_snapshot_id: str = ""
    last_absence_observed_at: Optional[str] = None
    last_absence_session_date: Optional[str] = None

    def __post_init__(self) -> None:
        self.environment = str(self.environment or "").upper()
        self.account_no = str(self.account_no or "")
        self.symbol = str(self.symbol or "").upper()
        self.attempt_group_id = str(self.attempt_group_id or "")
        self.requested_notional = float(self.requested_notional or 0.0)
        self.remaining_reserved_notional = float(
            self.remaining_reserved_notional or 0.0
        )
        self.status = _enum_from_value(
            self.status, CapitalReservationStatus, CapitalReservationStatus.RESERVED
        )
        self.version = max(1, int(self.version or 1))
        self.created_at = _parse_timestamp(self.created_at) or _utc_now()
        self.released_at = _parse_timestamp(self.released_at)
        self.absence_count = max(0, int(self.absence_count or 0))
        self.last_absence_snapshot_id = str(self.last_absence_snapshot_id or "")
        self.last_absence_observed_at = (
            str(self.last_absence_observed_at)
            if self.last_absence_observed_at
            else None
        )
        self.last_absence_session_date = (
            str(self.last_absence_session_date)
            if self.last_absence_session_date
            else None
        )

    @classmethod
    def create(
        cls,
        *,
        environment: str,
        account_no: str,
        symbol: str,
        attempt_group_id: str,
        requested_notional: float,
    ) -> "CapitalReservation":
        return cls(
            reservation_id=uuid4().hex,
            environment=environment,
            account_no=account_no,
            symbol=symbol,
            attempt_group_id=attempt_group_id,
            requested_notional=requested_notional,
            remaining_reserved_notional=requested_notional,
        )

    def is_open(self) -> bool:
        return self.status in OPEN_RESERVATION_STATUSES

    def consume(self, notional: float) -> None:
        """A fill consumed ``notional`` of this reservation (section 872-876)."""
        notional = max(0.0, float(notional or 0.0))
        self.remaining_reserved_notional = max(
            0.0, self.remaining_reserved_notional - notional
        )
        if self.remaining_reserved_notional <= 1e-9:
            self.status = CapitalReservationStatus.CONSUMED
        else:
            self.status = CapitalReservationStatus.PARTIALLY_CONSUMED

    def release(self) -> None:
        """Give back any unconsumed remainder (order cancelled/rejected)."""
        self.remaining_reserved_notional = 0.0
        self.status = CapitalReservationStatus.RELEASED
        self.released_at = _utc_now()

    def expire(self) -> None:
        self.remaining_reserved_notional = 0.0
        self.status = CapitalReservationStatus.EXPIRED
        self.released_at = _utc_now()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "reservation_id": self.reservation_id,
            "environment": self.environment,
            "account_no": self.account_no,
            "symbol": self.symbol,
            "attempt_group_id": self.attempt_group_id,
            "requested_notional": self.requested_notional,
            "remaining_reserved_notional": self.remaining_reserved_notional,
            "status": self.status.value,
            "version": self.version,
            "created_at": self.created_at.isoformat(),
            "released_at": self.released_at.isoformat() if self.released_at else None,
            "absence_count": self.absence_count,
            "last_absence_snapshot_id": self.last_absence_snapshot_id,
            "last_absence_observed_at": self.last_absence_observed_at,
            "last_absence_session_date": self.last_absence_session_date,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CapitalReservation":
        return cls(
            reservation_id=str(data.get("reservation_id", "")),
            environment=str(data.get("environment", "")),
            account_no=str(data.get("account_no", "")),
            symbol=str(data.get("symbol", "")),
            attempt_group_id=str(data.get("attempt_group_id", "")),
            requested_notional=float(data.get("requested_notional", 0.0) or 0.0),
            remaining_reserved_notional=float(
                data.get("remaining_reserved_notional", 0.0) or 0.0
            ),
            status=data.get("status", CapitalReservationStatus.RESERVED),
            version=int(data.get("version", 1) or 1),
            created_at=data.get("created_at"),
            released_at=data.get("released_at"),
            absence_count=int(data.get("absence_count", 0) or 0),
            last_absence_snapshot_id=str(
                data.get("last_absence_snapshot_id", "") or ""
            ),
            last_absence_observed_at=data.get("last_absence_observed_at"),
            last_absence_session_date=data.get("last_absence_session_date"),
        )


def available_for_new_entries(
    buying_power: float, active_reservations: Iterable[CapitalReservation]
) -> float:
    """Spec section 865-870: ``buying_power - sum(active_reservations)``.

    Only reservations still in an open status hold capital; anything
    consumed/released/expired has already given it back.
    """
    reserved = sum(
        reservation.remaining_reserved_notional
        for reservation in active_reservations
        if reservation.is_open()
    )
    return float(buying_power or 0.0) - reserved
